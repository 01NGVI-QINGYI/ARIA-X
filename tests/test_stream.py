"""Streaming: deltas reach the console without being persisted."""

from __future__ import annotations

import http.server
import json
import threading
import unittest

from aria_x.errors import LLMError
from aria_x.events import EventBus
from aria_x.llm.base import ChatMessage
from aria_x.llm.openai import OpenAIProvider


class _SSEHandler(http.server.BaseHTTPRequestHandler):
    chunks: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in self.chunks:
            payload = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args: object) -> None:
        pass


class StreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        _SSEHandler.chunks = ["Hello", ", ", "world", "!"]
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _SSEHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = OpenAIProvider(
            base_url=f"http://127.0.0.1:{self.port}/v1", api_key="test", model="test-model",
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_stream_assembles_text_and_reports_deltas(self) -> None:
        seen: list[str] = []
        response = self.client.chat_stream(
            [ChatMessage.user("hi")], None, on_delta=seen.append,
        )
        self.assertEqual(response.content, "Hello, world!")
        self.assertEqual(seen, ["Hello", ", ", "world", "!"])
        self.assertTrue(response.raw.get("streamed"))

    def test_supports_stream_only_without_tools(self) -> None:
        self.assertTrue(self.client.supports_stream(None))
        self.assertTrue(self.client.supports_stream([]))
        self.assertFalse(self.client.supports_stream([{"name": "t"}]))

    def test_transient_events_are_not_persisted(self) -> None:
        bus = EventBus(replay=10)
        sub = bus.subscribe()
        try:
            bus.emit_transient("llm_delta", data={"chars": 3})
            bus.emit("log", data={"message": "kept"})
            delivered = [sub.queue.get(timeout=1) for _ in range(2)]
            self.assertEqual([e.type for e in delivered], ["llm_delta", "log"])
            # _recent only holds the persisted event
            self.assertEqual([e.type for e in bus.replay(10)], ["log"])
        finally:
            sub.close()


if __name__ == "__main__":
    unittest.main()

class StreamFailureTests(unittest.TestCase):
    """A truncated or non-SSE stream must fail loudly, never return partial text."""

    def _serve(self, chunks):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(chunk)
                self.wfile.flush()

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def _client(self, base: str):
        return OpenAIProvider(base_url=base, api_key="k", model="m")

    def test_truncated_stream_raises_retryable_error(self) -> None:
        base = self._serve([b'data: {"choices":[{"delta":{"content":"half"}}]}\n\n'])  # no [DONE]
        with self.assertRaises(LLMError) as caught:
            self._client(base).chat_stream([ChatMessage.user("hi")], None, on_delta=lambda _p: None)
        self.assertTrue(caught.exception.details.get("retryable"))

    def test_plain_json_response_is_rejected(self) -> None:
        base = self._serve([b'{"choices":[{"message":{"content":"plain"}}]}'])
        with self.assertRaises(LLMError):
            self._client(base).chat_stream([ChatMessage.user("hi")], None, on_delta=lambda _p: None)

    def test_complete_stream_still_succeeds(self) -> None:
        base = self._serve([
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b"data: [DONE]\n\n",
        ])
        response = self._client(base).chat_stream([ChatMessage.user("hi")], None, on_delta=lambda _p: None)
        self.assertEqual(response.content, "ok")


class TransientEventCursorTests(unittest.TestCase):
    """Progress frames must not become a resume cursor and must not carry text."""

    def test_transient_event_has_no_id(self) -> None:
        bus = EventBus(replay=10)
        sub = bus.subscribe()
        try:
            bus.emit_transient("llm_delta", data={"chars": 3})
            event = sub.queue.get(timeout=1)
            self.assertEqual(event.id, "", "a transient frame must not own an event id")
            self.assertNotIn("id: ", event.sse())
        finally:
            sub.close()

    def test_persisted_event_still_has_id(self) -> None:
        bus = EventBus(replay=10)
        sub = bus.subscribe()
        try:
            bus.emit("log", data={"message": "kept"})
            event = sub.queue.get(timeout=1)
            self.assertTrue(event.id.startswith("ev_"))
            self.assertIn("id: ", event.sse())
        finally:
            sub.close()

    def test_sse_encoder_omits_empty_id(self) -> None:
        from aria_x.events import Event
        from aria_x.server.api import _sse_event

        transient = Event(type="llm_delta", data={"chars": 1}, id="")
        encoded = _sse_event(transient).decode()
        self.assertNotIn("id:", encoded)
        persisted = Event(type="log", data={"message": "x"})
        self.assertIn("id:", _sse_event(persisted).decode())

    def test_delta_payload_has_no_model_text(self) -> None:
        from aria_x.agents.base import Agent

        class _Spec:
            role = "worker"

        agent = Agent.__new__(Agent)
        agent.spec = _Spec()
        emitted: list[dict] = []

        class _Bus:
            def emit_transient(self, _type, **kwargs):
                emitted.append(kwargs.get("data") or {})

        agent.ctx = type("C", (), {"events": _Bus(), "project": type("P", (), {"id": "p"})(),
                                   "run_id": "r"})()
        sink = agent._on_delta(type("T", (), {"task_id": "t"})())
        sink("secret-token-value")
        sink("more-secret-token-value")   # second call passes the throttle window
        if emitted:
            self.assertNotIn("preview", emitted[0])
            self.assertFalse(any("secret" in str(v) for v in emitted[0].values()))


class StreamFrameDeliveryTests(unittest.TestCase):
    """Every progress frame must reach the client; dedupe is for real ids only."""

    def _chunks(self, bus):
        from aria_x.server.api import Api

        api = object.__new__(Api)
        api.bus = bus
        api.heartbeat_seconds = 0.05
        return api._event_chunks("p_test", replay=0)

    def test_consecutive_progress_frames_are_all_delivered(self) -> None:
        bus = EventBus(replay=10)
        stream = self._chunks(bus)
        next(stream)                     # retry/connected preamble
        bus.emit_transient("llm_delta", project_id="p_test", data={"chars": 1})
        bus.emit_transient("llm_delta", project_id="p_test", data={"chars": 2})
        bus.emit_transient("llm_delta", project_id="p_test", data={"chars": 3})
        frames = [next(stream) for _ in range(3)]
        stream.close()
        self.assertEqual(len(frames), 3)
        for index, frame in enumerate(frames, start=1):
            self.assertIn(f'"chars": {index}'.encode(), frame)
            self.assertNotIn(b"id:", frame)

    def test_persisted_duplicate_ids_are_still_deduped(self) -> None:
        bus = EventBus(replay=10)
        stream = self._chunks(bus)
        next(stream)
        event = bus.emit("log", project_id="p_test", data={"message": "once"})
        bus._recent.append(event)        # a duplicate of the same id
        first = next(stream)
        stream.close()
        self.assertIn(b"id:", first)

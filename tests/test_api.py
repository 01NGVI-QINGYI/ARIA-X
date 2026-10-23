"""HTTP API + web console tests.

The suite binds the real server on an ephemeral port (``config.server.port = 0``)
against an in-memory store and a fake engagement. That keeps transport,
routing, auth, SSE and static-file behaviour under test without depending on
the concurrently developed orchestrator or any network access.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from aria_x.config import AriaXConfig, ScopeConfig
from aria_x.errors import NotFound
from aria_x.events import EventBus, wait_for
from aria_x.models import Message, Node, Project, Run, Task, Todo
from aria_x.server import Api, create_server, serve
from aria_x.store import Store
from aria_x.util import now_iso


class FakeEngagement:
    """Minimal engine surface from CONTRACTS.md §3.5, backed by the real store."""

    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.events = bus
        self.stopped: list[str | None] = []
        self.run_count = 0
        self.closed = False

    def create_project(self, name: str, goal: str, scope: dict[str, Any] | None = None) -> Project:
        project = Project.new(name, goal, scope=scope or {})
        return self.store.create_project(project)

    def start_background(self, project_id: str, *, goal: str | None = None) -> str:
        self.run_count += 1
        run_id = f"run_test_{self.run_count}"
        self.store.create_run(Run(id=run_id, project_id=project_id, status="running",
                                  mode="auto", started_at=now_iso(),
                                  budget={"limits": {"max_seconds": 60}}))
        self.store.create_task(Task(id=f"task_test_{self.run_count}", run_id=run_id,
                                    project_id=project_id, type="reason", status="running",
                                    agent="planner", started_at=now_iso()))
        self.events.emit("run.started", project_id=project_id, run_id=run_id,
                         data={"goal": goal or "fixture goal"})
        return run_id

    def stop(self, run_id: str | None = None) -> None:
        self.stopped.append(run_id)
        if run_id:
            try:
                self.store.update_run(run_id, status="cancelled", ended_at=now_iso())
            except NotFound:
                pass
        self.events.emit("run.paused", project_id="", run_id=run_id or "", data={"reason": "test"})

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        return {"active": bool(run_id and run_id not in self.stopped), "run_id": run_id,
                "budget": {"limits": {"max_seconds": 60}, "usage": {"llm_calls": 2}}}

    def close(self) -> None:
        self.closed = True


class ServerTestCase(unittest.TestCase):
    auth_token = ""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aria-x-api-")
        root = Path(self.tmp.name)
        self.config = AriaXConfig(name="ARIA-X API test", data_dir=str(root / "data"),
                                 runs_dir=str(root / "runs"))
        self.config.server.host = "127.0.0.1"
        self.config.server.port = 0
        self.config.server.auth_token = self.auth_token
        self.config.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"],
                                        deny=["10.0.0.0/8"])
        self.store = Store(":memory:")
        self.bus = EventBus(replay=100)
        self.engagement = FakeEngagement(self.store, self.bus)
        self.server = create_server(self.config, self.engagement, self.bus, self.store)
        self.server.api.heartbeat_seconds = 0.2
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.project = self.engagement.create_project("Test project", "prove the test")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "server thread did not shut down")
        self.bus.close()
        self.store.close()
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def request(self, method: str, path: str, body: Any = None,
                token: str | None = None, headers: dict[str, str] | None = None
                ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=8)
        request_headers = dict(headers or {})
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        try:
            conn.request(method, path, body=payload, headers=request_headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            conn.close()

    def json_request(self, method: str, path: str, body: Any = None,
                     token: str | None = None) -> tuple[int, Any]:
        status, headers, data = self.request(method, path, body, token=token)
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.fail(f"expected JSON from {method} {path}, got {data[:200]!r}")
        return status, parsed

    def open_stream(self, path: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=8)
        conn.request("GET", path)
        return conn, conn.getresponse()

    def read_sse_until(self, response: http.client.HTTPResponse, predicate, timeout: float = 6.0,
                       start_lines: list[str] | None = None) -> list[str]:
        lines: list[str] = list(start_lines or [])
        event = ""
        deadline = time.monotonic() + timeout
        stream = response.fp
        while time.monotonic() < deadline:
            raw = stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            lines.append(line)
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith(":") and predicate(event, lines):
                return lines
            elif line == "":
                if event and predicate(event, lines):
                    return lines
                event = ""
        self.fail(f"SSE predicate not met within {timeout}s; lines={lines!r}")

    # -- basic endpoints ---------------------------------------------------

    def test_health_and_unknown_routes(self) -> None:
        status, body = self.json_request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("version", body)
        self.assertIn("time", body)

        status, headers, data = self.request("GET", "/api/does-not-exist")
        self.assertEqual(status, 404)
        payload = json.loads(data.decode())
        self.assertEqual(payload["error"], "aria-x.not_found")
        self.assertIn("message", payload)

        status, headers, _ = self.request("POST", "/api/health")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD")

    def test_config_masks_secrets_and_lists_tools(self) -> None:
        self.config.server.auth_token = "supersecret-token"
        status, body = self.json_request("GET", "/api/config", token="supersecret-token")
        self.assertEqual(status, 200)
        self.assertIn("tools", body)
        self.assertGreater(len(body["tools"]), 5)
        self.assertIn("scope_summary", body)
        self.assertEqual(body["scope_summary"]["targets"], ["127.0.0.1"])
        self.assertEqual(body["server"]["auth_token"], "***")
        self.assertNotIn("supersecret-token", json.dumps(body))

    def test_auth_required_for_api_but_not_health_or_static(self) -> None:
        self.config.server.auth_token = "secret-token"
        status, _, data = self.request("GET", "/api/projects")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(data)["error"], "unauthorized")
        status, _, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        status, body = self.json_request("GET", "/api/projects", token="secret-token")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)

    # -- projects ----------------------------------------------------------

    def test_create_list_and_detail_project(self) -> None:
        status, project = self.json_request("POST", "/api/projects",
                                            {"name": "New", "goal": "Find bugs"})
        self.assertEqual(status, 200)
        self.assertEqual(project["name"], "New")
        self.assertTrue(project["id"].startswith("p"))

        status, projects = self.json_request("GET", "/api/projects")
        self.assertEqual(status, 200)
        ids = [p["id"] for p in projects]
        self.assertIn(project["id"], ids)
        self.assertIn(self.project.id, ids)

        # Seed related records so the detail aggregate has content.
        node = self.store.add_node(Node.new(self.project.id, "fact", "port 80 open"))
        finding = self.store.add_finding(self.project.id, "Reflected XSS", "proof",
                                         severity="high", intent_id=node.id)
        self.store.upsert_asset(self.project.id, "host", "127.0.0.1")
        self.store.add_todo(Todo.new(self.project.id, 1, "probe /login"))

        status, detail = self.json_request("GET", f"/api/projects/{self.project.id}")
        self.assertEqual(status, 200)
        for key in ("project", "counts", "runs", "findings", "assets", "todos"):
            self.assertIn(key, detail)
        self.assertEqual(detail["findings"][0]["id"], finding.id)
        self.assertEqual(detail["counts"]["nodes"]["finding"], 1)
        self.assertEqual(len(detail["assets"]), 1)
        self.assertEqual(len(detail["todos"]), 1)

        status, body = self.json_request("GET", "/api/projects/p_missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "aria-x.not_found")

        status, body = self.json_request("POST", "/api/projects", {"name": "No goal"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request")

    def test_graph_endpoint(self) -> None:
        intent = self.store.add_node(Node.new(self.project.id, "intent", "probe web root"))
        fact = self.store.add_node(Node.new(self.project.id, "fact", "server is nginx"))
        self.store.add_edge(self.project.id, intent.id, fact.id, "yields")
        self.store.upsert_asset(self.project.id, "service", "http://127.0.0.1:80")
        status, graph = self.json_request("GET", f"/api/projects/{self.project.id}/graph")
        self.assertEqual(status, 200)
        self.assertEqual({n["id"] for n in graph["nodes"]}, {intent.id, fact.id})
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(len(graph["assets"]), 1)
        self.assertIn("counts", graph)

    def test_export_and_traces_and_audit(self) -> None:
        status, export = self.json_request("GET", f"/api/export/{self.project.id}")
        self.assertEqual(status, 200)
        self.assertEqual(export["project"]["id"], self.project.id)
        for key in ("nodes", "edges", "assets", "todos", "findings", "traces", "audit", "runs"):
            self.assertIn(key, export)

        status, traces = self.json_request("GET", f"/api/projects/{self.project.id}/traces")
        self.assertEqual(status, 200)
        self.assertEqual(traces["traces"], [])
        status, audit = self.json_request("GET", f"/api/projects/{self.project.id}/audit")
        self.assertEqual(status, 200)
        self.assertIn("audit", audit)

    # -- hints -------------------------------------------------------------

    def test_hints_create_node_message_and_event(self) -> None:
        status, body = self.json_request("POST", f"/api/projects/{self.project.id}/hints",
                                         {"content": "Try the /backup endpoint"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        hints = self.store.list_nodes(self.project.id, kind="hint")
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].body, "Try the /backup endpoint")
        messages = self.store.list_messages(self.project.id)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "user")
        self.assertTrue(any(ev.type == "hint" for ev in self.bus.replay(20)))

        status, body = self.json_request("POST", f"/api/projects/{self.project.id}/hints",
                                         {"content": "   "})
        self.assertEqual(status, 400)

    # -- approvals ---------------------------------------------------------

    def test_approval_queue_and_decisions(self) -> None:
        first = self.store.request_approval(self.project.id, "run_x", "task_x", "terminal_exec",
                                            {"command": "id"}, risk="high")
        second = self.store.request_approval(self.project.id, "run_x", "task_x", "http_request",
                                             {"url": "http://127.0.0.1/"}, risk="medium")
        status, approvals = self.json_request(
            "GET", f"/api/approvals?project_id={self.project.id}")
        self.assertEqual(status, 200)
        self.assertEqual({a["id"] for a in approvals}, {first, second})

        status, body = self.json_request("POST", f"/api/approvals/{first}",
                                         {"approved": True, "reason": "lab target"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.store.get_approval(first)["status"], "approved")
        remaining = self.store.list_approvals(self.project.id)
        self.assertEqual([a["id"] for a in remaining], [second])

        status, body = self.json_request("POST", f"/api/approvals/{second}",
                                         {"approved": "yes"})
        self.assertEqual(status, 400)

        status, body = self.json_request("POST", "/api/approvals/ap_missing", {"approved": False})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "aria-x.not_found")

    # -- runs / reports ----------------------------------------------------

    def test_run_lifecycle_and_report_endpoints(self) -> None:
        status, body = self.json_request("POST", f"/api/projects/{self.project.id}/runs",
                                         {"goal": "test goal"})
        self.assertEqual(status, 200)
        run_id = body["run_id"]
        self.assertTrue(body["ok"])

        status, run = self.json_request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(run["run"]["status"], "running")
        self.assertEqual(len(run["tasks"]), 1)
        self.assertIn("budget", run)
        self.assertTrue(run["engine"]["active"])

        status, body = self.json_request("POST", f"/api/runs/{run_id}/stop")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn(run_id, self.engagement.stopped)
        self.assertEqual(self.store.get_run(run_id).status, "cancelled")

        # Reports live under runs/<run_id>/report.{md,html}.
        run_dir = Path(self.config.runs_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.md").write_text("# ARIA-X report\n\nfinding body\n", encoding="utf-8")
        (run_dir / "report.html").write_text("<!doctype html><title>ARIA-X report</title>"
                                             "<h1>ARIA-X report</h1>", encoding="utf-8")

        status, headers, data = self.request("GET", f"/api/report/{run_id}")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"ARIA-X report", data)

        status, headers, data = self.request("GET", f"/api/projects/{self.project.id}/report")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/markdown"))
        self.assertIn(b"# ARIA-X report", data)

        status, _, data = self.request("GET", "/api/report/run_missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data)["error"], "aria-x.not_found")

    def test_report_path_must_stay_inside_runs_dir(self) -> None:
        # A run directory that is a symlink outside runs_dir must not be served.
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "report.md").write_text("secret", encoding="utf-8")
        run_id = "run_evil"
        self.store.create_run(Run(id=run_id, project_id=self.project.id, status="running",
                                  started_at=now_iso()))
        link = Path(self.config.runs_dir) / run_id
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this filesystem")
        status, _, _ = self.request("GET", f"/api/projects/{self.project.id}/report")
        self.assertEqual(status, 404)

    # -- SSE ---------------------------------------------------------------

    def test_sse_replays_then_streams_live_events_with_heartbeats(self) -> None:
        status, body = self.json_request("POST", f"/api/projects/{self.project.id}/runs",
                                         {"goal": "stream me"})
        self.assertEqual(status, 200)
        run_id = body["run_id"]

        conn, response = self.open_stream(
            f"/api/projects/{self.project.id}/events?replay=50")
        try:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
            header_lines: list[str] = []
            first = response.fp.readline().decode().strip()
            header_lines.append(first)
            self.assertTrue(first.startswith("retry:") or first.startswith(":"))
            # Replay: run.started was emitted before the stream connected.
            replay_lines = self.read_sse_until(
                response, lambda event, lines: event == "run.started",
                start_lines=header_lines)
            self.assertTrue(any(line.startswith("data: ") for line in replay_lines))
            self.assertTrue(any("stream me" in line for line in replay_lines
                                if line.startswith("data: ")), replay_lines)

            # Live: a new event must arrive after the replay.
            self.bus.emit("log", project_id=self.project.id, run_id=run_id,
                          data={"level": "info", "message": "live event"})
            live_lines = self.read_sse_until(response, lambda event, lines: event == "log")
            self.assertTrue(any("live event" in line for line in live_lines))

            # Heartbeat comment appears while the stream is idle (test cadence 0.2s).
            heartbeat = self.read_sse_until(
                response, lambda _event, lines: any(": heartbeat" in line for line in lines))
            self.assertTrue(any(": heartbeat" in line for line in heartbeat))
        finally:
            response.close()
            conn.close()

        # Closing the client must eventually unsubscribe the streaming generator.
        self.assertTrue(wait_for(lambda: not self.bus._subscribers, timeout=4.0),
                        "SSE subscription was not released after disconnect")

    def test_sse_requires_token_when_auth_enabled(self) -> None:
        self.config.server.auth_token = "stream-token"
        status, _, _ = self.request("GET", f"/api/projects/{self.project.id}/events")
        self.assertEqual(status, 401)
        conn, response = self.open_stream(
            f"/api/projects/{self.project.id}/events?token=stream-token")
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        finally:
            response.close()
            conn.close()

    # -- static console ----------------------------------------------------

    def test_static_console_assets_and_traversal_protection(self) -> None:
        status, headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"ARIA-X", data)
        self.assertIn("Content-Security-Policy", headers)

        for path, expected in (("/static/app.js", "text/javascript"),
                               ("/static/style.css", "text/css"),
                               ("/static/index.html", "text/html")):
            status, headers, data = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(expected), headers["Content-Type"])
            self.assertTrue(data, path)

        for path in ("/static/", "/static", "/static/../server/api.py",
                     "/static/%2e%2e/server/api.py", "/static/missing.js"):
            status, _, data = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(data)["error"], "not_found", path)

        # HEAD returns headers without a body.
        status, headers, data = self.request("HEAD", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)

    # -- factories / shutdown ---------------------------------------------

    def test_create_server_from_engagement_and_serve_block_false(self) -> None:
        # Store and bus can be discovered from the engagement itself.
        extra = create_server(self.config, self.engagement)
        try:
            self.assertIsInstance(extra, object)
            self.assertEqual(extra.server_address[0], "127.0.0.1")
            self.assertEqual(extra.api.store, self.store)
        finally:
            extra.server_close()

        server = serve(self.config, engagement=self.engagement, store=self.store,
                       bus=self.bus, block=False)
        assert server is not None
        try:
            self.assertGreater(server.server_address[1], 0)
            self.assertIsInstance(server.api, Api)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()

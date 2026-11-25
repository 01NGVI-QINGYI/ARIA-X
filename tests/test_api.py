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

    def open_stream(self, path: str, *, token: str | None = None,
                    headers: dict[str, str] | None = None) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=8)
        req_headers = dict(headers or {})
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        conn.request("GET", path, headers=req_headers)
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
        self.config.llm.provider = "openai"
        self.config.llm.model = "deepseek-chat"
        self.config.llm.api_key = "sk-synth-abcd1234567890-xy99"
        self.config.llm.base_url = "https://synthuser:synthpass@gateway.example.test:8443/v1?key=synthq"
        self.config.llm.extra_headers = {
            "Authorization": "Bearer synth-hdr-secret",
            "X-Opaque-Header": "synth-hdr-opaque",
        }
        self.config.llm.extra_body = {
            "custom_token": "synth-body-secret",
            "nested": {"opaque_field": "synth-nested-secret"},
        }
        self.config.sandbox.env = {
            "GH_TOKEN": "synth-gh-secret",
            "CUSTOM_VAR": "synth-env-opaque",
        }
        self.config.sandbox.extra_args = ["-e", "ARG_SECRET=synth-arg-val"]
        # Long, distinctive userinfo: a 2-character marker would collide with the
        # random 8-character suffix of the test's temporary directory name, which
        # appears in data_dir/runs_dir and made this assertion flaky (~2%/run).
        self.config.scope.targets = [
            "127.0.0.1",
            "https://scopetargetuser:scopetargetpass@lab.example.test/app?tok=qsecret",
        ]
        self.config.scope.deny = [
            "https://scopedenyuser:scopedenypass@deny.example.test/admin?tok=dsecret",
        ]

        status, body = self.json_request("GET", "/api/config", token="supersecret-token")
        self.assertEqual(status, 200)
        dumped = json.dumps(body)

        # Settings-required fields remain intact
        self.assertIn("version", body)
        self.assertIn("tagline", body)
        self.assertIn("tools", body)
        self.assertGreater(len(body["tools"]), 5)
        self.assertTrue(body["auth_required"])
        self.assertIn("scope_summary", body)
        self.assertEqual(body["scope_summary"]["targets"], ["127.0.0.1", "https://lab.example.test/app"])
        self.assertEqual(body["llm"]["provider"], "openai")
        self.assertEqual(body["llm"]["model"], "deepseek-chat")
        self.assertEqual(body["llm"]["base_url"], "https://gateway.example.test:8443/v1")
        self.assertEqual(body["llm"]["api_key"], "***")
        self.assertEqual(body["server"]["auth_token"], "***")
        self.assertIn("budget", body)
        self.assertEqual(body["sandbox"]["backend"], "local")
        self.assertEqual(body["safety"]["approval"], "auto")
        self.assertIn("engine", body)

        # Excluded fields and all credential values/fragments must be absent
        for excluded in ("extra_headers", "extra_body", "scenario"):
            self.assertNotIn(excluded, body["llm"])
        for excluded in ("env", "extra_args"):
            self.assertNotIn(excluded, body["sandbox"])

        for forbidden in (
            "supersecret-token",
            "sk-synth-abcd1234567890-xy99", "sk-s", "xy99",
            "synthuser", "synthpass", "synthq",
            "synth-hdr-secret", "X-Opaque-Header", "synth-hdr-opaque",
            "custom_token", "synth-body-secret", "opaque_field", "synth-nested-secret",
            "GH_TOKEN", "synth-gh-secret", "CUSTOM_VAR", "synth-env-opaque",
            "ARG_SECRET", "synth-arg-val",
            "scopetargetuser", "scopetargetpass", "qsecret",
            "scopedenyuser", "scopedenypass", "dsecret",
        ):
            self.assertNotIn(forbidden, dumped)

        # Live config on server must remain unmutated
        self.assertEqual(self.config.server.auth_token, "supersecret-token")
        self.assertEqual(self.config.llm.api_key, "sk-synth-abcd1234567890-xy99")
        self.assertEqual(
            self.config.llm.base_url,
            "https://synthuser:synthpass@gateway.example.test:8443/v1?key=synthq",
        )
        self.assertEqual(self.config.llm.extra_headers["X-Opaque-Header"], "synth-hdr-opaque")
        self.assertEqual(self.config.llm.extra_body["nested"]["opaque_field"], "synth-nested-secret")
        self.assertEqual(self.config.sandbox.env["GH_TOKEN"], "synth-gh-secret")
        self.assertEqual(self.config.sandbox.extra_args, ["-e", "ARG_SECRET=synth-arg-val"])

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

        status, headers, data = self.request("GET", f"/api/report/{run_id}?format=md")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/markdown"))
        self.assertIn(b"# ARIA-X report", data)

        status, _, data = self.request("GET", f"/api/report/{run_id}?format=xml")
        self.assertEqual(status, 400)

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
        # Legacy ?token= query parameter must be rejected.
        status, _, _ = self.request("GET", f"/api/projects/{self.project.id}/events?token=stream-token")
        self.assertEqual(status, 401)

        conn, response = self.open_stream(
            f"/api/projects/{self.project.id}/events",
            token="stream-token",
        )
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        finally:
            response.close()
            conn.close()

        conn2, response2 = self.open_stream(
            f"/api/projects/{self.project.id}/events",
            headers={"X-ARIA-X-Token": "stream-token"},
        )
        try:
            self.assertEqual(response2.status, 200)
            self.assertEqual(response2.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        finally:
            response2.close()
            conn2.close()

    def test_sse_last_event_id_resumes_all_buffered_events_and_isolates_projects(self) -> None:
        other_project = self.engagement.create_project("Other project", "isolate events")
        big_bus = EventBus(replay=250)
        big_eng = FakeEngagement(self.store, big_bus)
        big_srv = create_server(self.config, big_eng, big_bus, self.store)
        big_srv.api.heartbeat_seconds = 0.2
        big_thread = threading.Thread(
            target=big_srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        big_thread.start()
        host, port = big_srv.server_address[:2]
        try:
            proj_a_events = []
            for idx in range(185):
                proj_a_events.append(big_bus.emit(
                    "log", project_id=self.project.id,
                    data={"seq": idx, "project": "A"},
                ))
                if idx % 5 == 0:
                    big_bus.emit(
                        "log", project_id=other_project.id,
                        data={"seq": idx, "project": "B"},
                    )
            cursor = proj_a_events[9].id  # 175 buffered projA events follow (> 150)

            conn = http.client.HTTPConnection(host, port, timeout=8)
            conn.request(
                "GET",
                f"/api/projects/{self.project.id}/events?replay=150",
                headers={"Last-Event-ID": cursor},
            )
            response = conn.getresponse()
            try:
                self.assertEqual(response.status, 200)
                live_ev = big_bus.emit(
                    "log", project_id=self.project.id,
                    data={"seq": 999, "project": "A"},
                )
                received_seqs: list[int] = []
                received_ids: list[str] = []
                deadline = time.monotonic() + 6.0
                cur_id = ""
                cur_data = ""
                while time.monotonic() < deadline:
                    raw = response.fp.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("id: "):
                        cur_id = line[4:].strip()
                    elif line.startswith("data: "):
                        cur_data = line[6:].strip()
                    elif line == "":
                        if cur_data:
                            payload = json.loads(cur_data)
                            self.assertNotEqual(payload.get("project"), "B")
                            received_ids.append(cur_id)
                            received_seqs.append(payload["seq"])
                            cur_id = ""
                            cur_data = ""
                            if received_seqs[-1] == 999:
                                break
                expected_seqs = list(range(10, 185)) + [999]
                self.assertEqual(received_seqs, expected_seqs)
                self.assertEqual(len(received_ids), len(set(received_ids)))
                self.assertEqual(received_ids[-1], live_ev.id)
                self.assertNotIn(cursor, received_ids)
            finally:
                response.close()
                conn.close()
        finally:
            big_srv.shutdown()
            big_srv.server_close()
            big_thread.join(timeout=5)
            big_bus.close()

    def test_sse_last_event_id_expired_foreign_and_concurrent_handoff(self) -> None:
        other_project = self.engagement.create_project("Project B", "isolate cursor")
        # 1. Expired cursor falls back to bounded replay
        small_bus = EventBus(replay=8)
        small_eng = FakeEngagement(self.store, small_bus)
        small_srv = create_server(self.config, small_eng, small_bus, self.store)
        small_srv.api.heartbeat_seconds = 0.2
        small_thread = threading.Thread(
            target=small_srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        small_thread.start()
        host, port = small_srv.server_address[:2]
        try:
            ev0 = small_bus.emit("log", project_id=self.project.id, data={"seq": 0})
            for i in range(1, 15):
                small_bus.emit("log", project_id=self.project.id, data={"seq": i})
            foreign_ev = small_bus.emit("log", project_id=other_project.id, data={"seq": 777})

            def _read_seqs(path: str, headers: dict[str, str], stop_count: int) -> list[int]:
                conn = http.client.HTTPConnection(host, port, timeout=6)
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                seqs: list[int] = []
                try:
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and len(seqs) < stop_count:
                        raw = resp.fp.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data: "):
                            payload = json.loads(line[6:].strip())
                            seqs.append(payload["seq"])
                finally:
                    resp.close()
                    conn.close()
                return seqs

            # Expired cursor -> bounded replay=3
            self.assertEqual(
                _read_seqs(
                    f"/api/projects/{self.project.id}/events?replay=3",
                    {"Last-Event-ID": ev0.id},
                    3,
                ),
                [12, 13, 14],
            )
            # Foreign project cursor -> treated as non-matching, bounded replay=3 for project A
            self.assertEqual(
                _read_seqs(
                    f"/api/projects/{self.project.id}/events?replay=3",
                    {"Last-Event-ID": foreign_ev.id},
                    3,
                ),
                [12, 13, 14],
            )
        finally:
            small_srv.shutdown()
            small_srv.server_close()
            small_thread.join(timeout=5)
            small_bus.close()

        # 2. Concurrent emission across replay->live handoff has zero drops and zero duplicates
        pre_cursor = self.bus.emit("log", project_id=self.project.id, data={"seq": 0})
        emit_done = threading.Event()

        def _emitter() -> None:
            for i in range(1, 31):
                self.bus.emit("log", project_id=self.project.id, data={"seq": i})
                time.sleep(0.002)
            emit_done.set()

        t = threading.Thread(target=_emitter, daemon=True)
        t.start()
        conn, response = self.open_stream(
            f"/api/projects/{self.project.id}/events?replay=5",
            headers={"Last-Event-ID": pre_cursor.id},
        )
        try:
            seqs: list[int] = []
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and len(seqs) < 30:
                raw = response.fp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data: "):
                    seqs.append(json.loads(line[6:].strip())["seq"])
            self.assertTrue(emit_done.wait(timeout=3.0))
            self.assertEqual(seqs, list(range(1, 31)))
        finally:
            response.close()
            conn.close()
            t.join(timeout=3.0)

    def test_server_logs_redact_sensitive_query_params(self) -> None:
        import logging
        from unittest import mock

        self.config.server.auth_token = "stream-token"
        with self.assertLogs("aria_x.server", level=logging.DEBUG) as captured:
            status, _, _ = self.request(
                "GET",
                f"/api/projects/{self.project.id}/events?replay=10&token=synth-access-tok-111&opaque_secret=synth-custom-q-222",
            )
            self.assertEqual(status, 401)

            with mock.patch.object(
                self.server.api,
                "dispatch",
                side_effect=RuntimeError("boom-secret-check"),
            ):
                status2, _, _ = self.request(
                    "GET",
                    f"/api/projects/{self.project.id}/events?token=synth-exc-tok-444",
                    token="stream-token",
                )
                self.assertEqual(status2, 500)

        joined = "\n".join(captured.output)
        self.assertIn("replay=10", joined)
        self.assertIn("[REDACTED]", joined)
        for secret in (
            "synth-access-tok-111",
            "synth-custom-q-222",
            "synth-exc-tok-444",
            "boom-secret-check",
            self.project.id,
        ):
            self.assertNotIn(secret, joined)
        self.assertIn("/api/projects/{project_id}/events", joined)
        self.assertIn("error_type=RuntimeError", joined)

    def test_server_logs_normalize_routes_unknown_paths_and_strip_exception_bodies(self) -> None:
        import logging
        from unittest import mock
        from aria_x.server.app import AriaXRequestHandler, _normalize_log_path

        self.assertTrue(_normalize_log_path("http://[::1/").startswith("<unparseable:"))
        self.assertTrue(_normalize_log_path("/api/projects/p%00bad").startswith("<unparseable:"))

        with self.assertLogs("aria_x.server", level=logging.DEBUG) as captured:
            # 1. Dynamic API path + query params
            status, _ = self.json_request(
                "GET",
                f"/api/projects/{self.project.id}/graph?limit=25&secret_k=synth-q-222",
            )
            self.assertEqual(status, 200)

            # 2. Dynamic static asset path (404)
            status_static, _, _ = self.request("GET", "/static/synth-static-asset-333.js")
            self.assertEqual(status_static, 404)

            # 3. Unknown non-API path & unknown API path
            status_unk_root, _, _ = self.request("GET", "/totally/unknown/synth-unknown-root-444")
            self.assertEqual(status_unk_root, 404)
            status_unk_api, _, _ = self.request("GET", "/api/nonexistent/synth-unknown-api-555")
            self.assertEqual(status_unk_api, 404)

            # 4. Simulated AriaXError with sensitive exception body
            with mock.patch.object(
                self.server.api,
                "dispatch",
                side_effect=NotFound("aria-secret-body-666"),
            ):
                status_nf, body_nf = self.json_request("GET", "/api/projects/synth-dyn-proj-111")
                self.assertEqual(status_nf, 404)
                self.assertEqual(body_nf["error"], "aria-x.not_found")

            # 5. Simulated unhandled RuntimeError in API dispatch with sensitive body & request payload
            with mock.patch.object(
                self.server.api,
                "dispatch",
                side_effect=RuntimeError("downstream-secret-body-777"),
            ):
                status_err, body_err = self.json_request(
                    "POST",
                    "/api/projects/synth-dyn-proj-111/hints",
                    {"content": "body-secret-888"},
                )
                self.assertEqual(status_err, 500)
                self.assertEqual(body_err["error"], "internal_error")

            # 6. Simulated unhandled ValueError in _handle
            with mock.patch.object(
                AriaXRequestHandler,
                "_handle_api",
                side_effect=ValueError("handle-secret-body-999"),
            ):
                status_h, body_h = self.json_request("GET", "/api/projects/synth-dyn-proj-111")
                self.assertEqual(status_h, 500)
                self.assertEqual(body_h["error"], "internal_error")

        joined = "\n".join(captured.output)
        for expected in (
            "/api/projects/{project_id}/graph?limit=25&[REDACTED]",
            "/static/{asset}",
            "/<unknown:",
            "/api/<unknown:",
            "route=/api/projects/{project_id} status=404 code=aria-x.not_found error_type=NotFound",
            "method=POST route=/api/projects/{project_id}/hints status=500 error_type=RuntimeError",
            "unhandled server error method=GET route=/api/projects/{project_id} status=500 error_type=ValueError",
        ):
            self.assertIn(expected, joined)

        for forbidden in (
            self.project.id,
            "synth-dyn-proj-111",
            "synth-q-222",
            "secret_k",
            "synth-static-asset-333",
            "synth-unknown-root-444",
            "synth-unknown-api-555",
            "aria-secret-body-666",
            "downstream-secret-body-777",
            "body-secret-888",
            "handle-secret-body-999",
            "Traceback",
        ):
            self.assertNotIn(forbidden, joined)

    def test_web_fetch_sse_auth_replay_heartbeat_reconnect_and_switch(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("node not installed")

        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const i18nCode = fs.readFileSync(process.argv[1], "utf8");
const appCode = fs.readFileSync(process.argv[2], "utf8");

const elements = {};
function makeEl(id) {
  return {
    id, hidden: false, disabled: false, value: "", textContent: "",
    className: "", children: [], style: {}, scrollTop: 0, scrollHeight: 100,
    checked: true, dataset: {},
    classList: {
      _set: new Set(),
      add(...xs) { xs.forEach(x => this._set.add(x)); },
      remove(...xs) { xs.forEach(x => this._set.delete(x)); },
      toggle(x, f) { if (f === undefined) { if (this._set.has(x)) this._set.delete(x); else this._set.add(x); } else if (f) this._set.add(x); else this._set.delete(x); },
    },
    setAttribute(k, v) { this[k] = v; },
    removeAttribute(k) { delete this[k]; },
    addEventListener() {},
    querySelector() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs; },
  };
}
for (const id of [
  "conn-pill", "conn-text", "sse-status", "timeline-pulse", "timeline-log",
  "timeline-empty", "timeline-count", "timeline-autoscroll", "timeline-type",
  "project-select", "overview-empty", "overview-content", "project-name",
  "project-goal", "run-id", "run-state", "run-chip", "btn-start", "btn-stop",
  "approval-list", "approval-count", "report-empty", "report-frame",
  "report-meta", "report-open", "report-markdown", "graph-svg", "graph-empty",
  "graph-count", "graph-detail", "graph-legend", "toasts",
]) {
  elements[id] = makeEl(id);
}

const fetchCalls = [];
const timers = new Map();
let nextTimerId = 1;
const clearedTimers = [];

function makeChunkReader(chunks) {
  let idx = 0;
  return {
    async read() {
      if (idx >= chunks.length) return { done: true, value: undefined };
      const item = chunks[idx++];
      if (item instanceof Error) throw item;
      return { done: false, value: Buffer.from(item, "utf8") };
    },
    async cancel() {},
  };
}

let streamBehavior = null;

class FakeAbortController {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
}

const ctx = {
  AbortController: FakeAbortController,
  TextDecoder: class {
    decode(buf) { return buf ? Buffer.from(buf).toString("utf8") : ""; }
  },
  document: {
    documentElement: { lang: "en" },
    title: "",
    hidden: false,
    getElementById: (id) => elements[id] || makeEl(id),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    createElementNS: (_ns, tag) => makeEl(tag),
    addEventListener: () => {},
  },
  window: {
    location: { hash: "#timeline" },
    addEventListener: () => {},
    setTimeout: (fn, ms) => { const id = nextTimerId++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { if (id) { clearedTimers.push(id); timers.delete(id); } },
    setInterval: () => 1,
    clearInterval: () => {},
    history: { replaceState: () => {} },
    scrollTo: () => {},
  },
  localStorage: { getItem: () => "", setItem: () => {}, removeItem: () => {} },
  URL: { createObjectURL: () => "blob:0", revokeObjectURL() {} },
  Blob: class {},
  fetch: async (url, init) => {
    fetchCalls.push({ url, init: init || {} });
    return streamBehavior(url, init || {});
  },
  setTimeout,
  clearTimeout,
};
Object.assign(ctx, { console, encodeURIComponent, decodeURIComponent, Set, Map, Array, Object, String, Number, Boolean, Date, Math, JSON, Error, Promise });
ctx.window.ARIA_X_I18N = undefined;
vm.runInNewContext(i18nCode, ctx);
const patched = appCode.replace(/\}\)\(\);\s*$/, "  globalThis.__TEST__ = { state, connectSSE, disconnectSSE, selectProject, setLanguage };\n})();");
vm.runInNewContext(patched, ctx);

async function flush() {
  for (let i = 0; i < 6; i += 1) await new Promise(r => setTimeout(r, 2));
}

(async () => {
  const { state, connectSSE, disconnectSSE, selectProject, setLanguage } = ctx.__TEST__;
  assert.strictEqual("es" in state, false, "unused state.es must be removed");
  state.token = "synth-web-token";
  state.projectId = "projA";

  // 1. Chunked replay + heartbeat + retry + disconnect reconnect with Last-Event-ID + dedup
  let attempt = 0;
  streamBehavior = async (url, init) => {
    attempt += 1;
    assert(!url.includes("token="), "URL must never contain token: " + url);
    assert(!url.includes("synth-web-token"), "URL must never contain secret: " + url);
    assert.strictEqual(init.headers.Authorization, "Bearer synth-web-token");
    if (attempt === 1) {
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => makeChunkReader([
            "retry: 1200\n: connected\n\n",
            "id: ev_1\nevent: log\ndata: {\"message\": \"first\"}\n\n: heart",
            "beat\n\nid: ev_2\nevent: log\ndata: {\"message\": \"second\"}\n\n",
          ]),
        },
      };
    }
    assert.strictEqual(init.headers["Last-Event-ID"], "ev_2");
    return {
      ok: true,
      status: 200,
      body: {
        getReader: () => makeChunkReader([
          // ev_2 replayed again -> must be deduplicated
          "id: ev_2\nevent: log\ndata: {\"message\": \"second\"}\n\n",
          "id: ev_3\nevent: log\ndata: {\"message\": \"third\"}\n\n",
        ]),
      },
    };
  };

  connectSSE();
  await flush();
  assert.strictEqual(state.events.length, 2, "expected 2 initial events");
  assert.strictEqual(state.sseRetryMs, 1200);
  assert.strictEqual(state.sseLastEventId, "ev_2");
  assert(state.sseReconnectTimer !== null, "expected reconnect timer scheduled");

  // Trigger scheduled reconnect
  const tid = state.sseReconnectTimer;
  const tinfo = timers.get(tid);
  assert(tinfo, "missing reconnect timer in timers map");
  assert.strictEqual(tinfo.ms, 1200);
  timers.delete(tid);
  tinfo.fn();
  await flush();
  assert.strictEqual(state.events.length, 3, "expected ev_2 deduped and ev_3 appended");
  assert.strictEqual(state.events[2].id, "ev_3");

  // Clear reconnect timer scheduled after attempt 2
  if (state.sseReconnectTimer) { timers.delete(state.sseReconnectTimer); state.sseReconnectTimer = null; }

  // 2. Language switch must NOT issue fetch or abort/reconnect SSE
  const callsBeforeLang = fetchCalls.length;
  setLanguage("zh-CN");
  setLanguage("ja");
  setLanguage("en");
  assert.strictEqual(fetchCalls.length, callsBeforeLang);

  // 3. 401 Unauthorized must stop reconnecting
  streamBehavior = async () => ({ ok: false, status: 401 });
  connectSSE();
  await flush();
  assert.strictEqual(state.sseConnected, false);
  assert.strictEqual(state.sseReconnectTimer, null, "must not reconnect on 401");

  // 4. Switching project aborts old stream, clears pending timer, and connects new project
  let releaseOld = null;
  streamBehavior = async (url, init) => {
    if (url.includes("projA")) {
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: () => new Promise((res) => { releaseOld = res; }),
            cancel() {},
          }),
        },
      };
    }
    if (url.includes("projB")) {
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => makeChunkReader(["id: b_1\nevent: log\ndata: {\"message\": \"fromB\"}\n\n"]),
        },
      };
    }
    return { ok: true, status: 200, text: async () => "{}", headers: new Map() };
  };
  connectSSE();
  await flush();
  const oldAbort = state.sseAbort;
  await selectProject("projB");
  await flush();
  assert.strictEqual(oldAbort.signal.aborted, true, "old stream must be aborted on project switch");
  if (releaseOld) releaseOld({ done: false, value: Buffer.from("id: stale_a\nevent: log\ndata: {\"message\": \"stale\"}\n\n") });
  await flush();
  assert(state.events.every(e => e.id !== "stale_a"), "stale projA event must be ignored");
  assert(state.events.some(e => e.id === "b_1"), "projB event must arrive");

  disconnectSSE();
  console.log("WEB_FETCH_SSE_OK");
})().catch((err) => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run([
            node, "-e", script,
            str(Path(__file__).resolve().parents[1] / "src" / "aria_x" / "web" / "i18n.js"),
            str(Path(__file__).resolve().parents[1] / "src" / "aria_x" / "web" / "app.js"),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WEB_FETCH_SSE_OK", proc.stdout)

    def test_web_sse_eof_partial_frame_and_real_text_decoder(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("node not installed")

        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const i18nCode = fs.readFileSync(process.argv[1], "utf8");
const appCode = fs.readFileSync(process.argv[2], "utf8");

const elements = {};
function makeEl(id) {
  return {
    id, hidden: false, disabled: false, value: "", textContent: "",
    className: "", children: [], style: {}, scrollTop: 0, scrollHeight: 100,
    checked: true, dataset: {},
    classList: {
      _set: new Set(),
      add(...xs) { xs.forEach(x => this._set.add(x)); },
      remove(...xs) { xs.forEach(x => this._set.delete(x)); },
      toggle(x, f) { if (f === undefined) { if (this._set.has(x)) this._set.delete(x); else this._set.add(x); } else if (f) this._set.add(x); else this._set.delete(x); },
    },
    setAttribute(k, v) { this[k] = v; },
    removeAttribute(k) { delete this[k]; },
    addEventListener() {},
    querySelector() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs; },
  };
}
for (const id of [
  "conn-pill", "conn-text", "sse-status", "timeline-pulse", "timeline-log",
  "timeline-empty", "timeline-count", "timeline-autoscroll", "timeline-type",
  "project-select", "overview-empty", "overview-content", "project-name",
  "project-goal", "run-id", "run-state", "run-chip", "btn-start", "btn-stop",
  "approval-list", "approval-count", "report-empty", "report-frame",
  "report-meta", "report-open", "report-markdown", "graph-svg", "graph-empty",
  "graph-count", "graph-detail", "graph-legend", "toasts",
]) {
  elements[id] = makeEl(id);
}

const timers = new Map();
let nextTimerId = 1;
const clearedTimers = [];

function makeByteReader(chunks) {
  let idx = 0;
  return {
    async read() {
      if (idx >= chunks.length) return { done: true, value: undefined };
      return { done: false, value: chunks[idx++] };
    },
    async cancel() {},
  };
}

let streamBehavior = null;

class FakeAbortController {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
}

const ctx = {
  AbortController: FakeAbortController,
  TextDecoder: globalThis.TextDecoder,
  Uint8Array: globalThis.Uint8Array,
  document: {
    documentElement: { lang: "en" },
    title: "",
    hidden: false,
    getElementById: (id) => elements[id] || makeEl(id),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    createElementNS: (_ns, tag) => makeEl(tag),
    addEventListener: () => {},
  },
  window: {
    location: { hash: "#timeline" },
    addEventListener: () => {},
    setTimeout: (fn, ms) => { const id = nextTimerId++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { if (id) { clearedTimers.push(id); timers.delete(id); } },
    setInterval: () => 1,
    clearInterval: () => {},
    history: { replaceState: () => {} },
    scrollTo: () => {},
  },
  localStorage: { getItem: () => "", setItem: () => {}, removeItem: () => {} },
  URL: { createObjectURL: () => "blob:0", revokeObjectURL() {} },
  Blob: class {},
  fetch: async (url, init) => streamBehavior(url, init || {}),
  setTimeout,
  clearTimeout,
};
Object.assign(ctx, { console, encodeURIComponent, decodeURIComponent, Set, Map, Array, Object, String, Number, Boolean, Date, Math, JSON, Error, Promise });
ctx.window.ARIA_X_I18N = undefined;
vm.runInNewContext(i18nCode, ctx);
const patched = appCode.replace(/\}\)\(\);\s*$/, "  globalThis.__TEST__ = { state, connectSSE, disconnectSSE, selectProject };\n})();");
vm.runInNewContext(patched, ctx);

async function flush() {
  for (let i = 0; i < 6; i += 1) await new Promise(r => setTimeout(r, 2));
}

(async () => {
  const { state, connectSSE, disconnectSSE, selectProject } = ctx.__TEST__;
  state.token = "synth-tok";
  state.projectId = "projUTF8";

  // 1. Split multi-byte UTF-8 ("中文测试🔐日本語") mid-codepoint across Uint8Array chunks
  //    AND end attempt 1 with an incomplete EOF frame (no trailing blank line)
  const enc = new TextEncoder();
  const completeBytes = enc.encode("id: ev_utf8\nevent: log\ndata: {\"message\": \"中文测试🔐日本語\"}\n\n");
  const cutIndex1 = completeBytes.indexOf(0xe4) + 1; // inside 3-byte '中'
  const cutIndex2 = completeBytes.indexOf(0xf0) + 2; // inside 4-byte '🔐'
  const part1 = completeBytes.slice(0, cutIndex1);
  const part2 = completeBytes.slice(cutIndex1, cutIndex2);
  const part3 = completeBytes.slice(cutIndex2);
  const partialEof = enc.encode("id: ev_cut\nevent: log\ndata: {\"message\": \"half-frame");

  let attempt = 0;
  streamBehavior = async (_url, init) => {
    attempt += 1;
    if (attempt === 1) {
      return {
        ok: true,
        status: 200,
        body: { getReader: () => makeByteReader([part1, part2, part3, partialEof]) },
      };
    }
    assert.strictEqual(init.headers["Last-Event-ID"], "ev_utf8", "cursor must NOT advance to partial EOF frame");
    return {
      ok: true,
      status: 200,
      body: {
        getReader: () => makeByteReader([
          enc.encode("id: ev_cut\nevent: log\ndata: {\"message\": \"full-frame\"}\n\n"),
        ]),
      },
    };
  };

  connectSSE();
  await flush();
  assert.strictEqual(state.events.length, 1, "incomplete EOF frame must NOT enter timeline");
  assert.strictEqual(state.events[0].data.message, "中文测试🔐日本語");
  assert.strictEqual(state.events[0].data.message.includes("\uFFFD"), false, "real TextDecoder must preserve split UTF-8");
  assert.strictEqual(state.sseLastEventId, "ev_utf8");
  assert.strictEqual(state.seenEventIds.has("ev_cut"), false, "incomplete EOF frame must NOT enter seenEventIds");

  // Reconnect delivers the complete ev_cut frame -> must now be accepted
  const tid = state.sseReconnectTimer;
  const tinfo = timers.get(tid);
  assert(tinfo);
  timers.delete(tid);
  tinfo.fn();
  await flush();
  assert.strictEqual(state.events.length, 2);
  assert.strictEqual(state.events[1].id, "ev_cut");
  assert.strictEqual(state.events[1].data.message, "full-frame");
  assert.strictEqual(state.sseLastEventId, "ev_cut");

  // Clearing project aborts stream and cancels pending reconnect timer
  assert(state.sseReconnectTimer !== null);
  const pendingTid = state.sseReconnectTimer;
  await selectProject("");
  assert(clearedTimers.includes(pendingTid), "clearing project must cancel reconnect timer");
  assert.strictEqual(state.sseConnected, false);

  disconnectSSE();
  console.log("WEB_SSE_EOF_UTF8_OK");
})().catch((err) => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run([
            node, "-e", script,
            str(Path(__file__).resolve().parents[1] / "src" / "aria_x" / "web" / "i18n.js"),
            str(Path(__file__).resolve().parents[1] / "src" / "aria_x" / "web" / "app.js"),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WEB_SSE_EOF_UTF8_OK", proc.stdout)

    def test_web_sse_bounded_backoff_and_cancellation(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("node not installed")

        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const i18nCode = fs.readFileSync(process.argv[1], "utf8");
const appCode = fs.readFileSync(process.argv[2], "utf8");

const elements = {};
function makeEl(id) {
  return {
    id, hidden: false, disabled: false, value: "", textContent: "",
    className: "", children: [], style: {}, scrollTop: 0, scrollHeight: 100,
    checked: true, dataset: {},
    classList: {
      _set: new Set(),
      add(...xs) { xs.forEach(x => this._set.add(x)); },
      remove(...xs) { xs.forEach(x => this._set.delete(x)); },
      toggle(x, f) { if (f === undefined) { if (this._set.has(x)) this._set.delete(x); else this._set.add(x); } else if (f) this._set.add(x); else this._set.delete(x); },
    },
    setAttribute(k, v) { this[k] = v; },
    removeAttribute(k) { delete this[k]; },
    addEventListener() {},
    querySelector() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs; },
  };
}
for (const id of [
  "conn-pill", "conn-text", "sse-status", "timeline-pulse", "timeline-log",
  "timeline-empty", "timeline-count", "timeline-autoscroll", "timeline-type",
  "project-select", "overview-empty", "overview-content", "project-name",
  "project-goal", "run-id", "run-state", "run-chip", "btn-start", "btn-stop",
  "approval-list", "approval-count", "report-empty", "report-frame",
  "report-meta", "report-open", "report-markdown", "graph-svg", "graph-empty",
  "graph-count", "graph-detail", "graph-legend", "toasts",
]) {
  elements[id] = makeEl(id);
}

const timers = new Map();
let nextTimerId = 1;
const clearedTimers = [];
let streamBehavior = null;

class FakeAbortController {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
}

let nowMs = 1700000000000;
class FakeDate extends Date {
  static now() { return nowMs; }
}

const ctx = {
  AbortController: FakeAbortController,
  TextDecoder: globalThis.TextDecoder,
  Date: FakeDate,
  document: {
    documentElement: { lang: "en" },
    title: "",
    hidden: false,
    getElementById: (id) => elements[id] || makeEl(id),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    createElementNS: (_ns, tag) => makeEl(tag),
    addEventListener: () => {},
  },
  window: {
    location: { hash: "#timeline" },
    addEventListener: () => {},
    setTimeout: (fn, ms) => { const id = nextTimerId++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { if (id) { clearedTimers.push(id); timers.delete(id); } },
    setInterval: () => 1,
    clearInterval: () => {},
    history: { replaceState: () => {} },
    scrollTo: () => {},
  },
  localStorage: { getItem: () => "", setItem: () => {}, removeItem: () => {} },
  URL: { createObjectURL: () => "blob:0", revokeObjectURL() {} },
  Blob: class {},
  fetch: async (url, init) => streamBehavior(url, init || {}),
  setTimeout,
  clearTimeout,
};
Object.assign(ctx, { console, encodeURIComponent, decodeURIComponent, Set, Map, Array, Object, String, Number, Boolean, Math, JSON, Error, Promise });
ctx.window.ARIA_X_I18N = undefined;
vm.runInNewContext(i18nCode, ctx);
const patched = appCode.replace(/\}\)\(\);\s*$/, "  globalThis.__TEST__ = { state, connectSSE, disconnectSSE, selectProject };\n})();");
vm.runInNewContext(patched, ctx);

async function flush() {
  for (let i = 0; i < 6; i += 1) await new Promise(r => setTimeout(r, 2));
}

(async () => {
  const { state, connectSSE, disconnectSSE, selectProject } = ctx.__TEST__;
  state.token = "synth-tok";
  state.projectId = "projBackoff";
  state.sseRetryMs = 1000;

  let attempt = 0;
  streamBehavior = async () => {
    attempt += 1;
    // Attempts 1..3: immediate 200 OK EOF (< 2000ms) -> must back off!
    if (attempt <= 3) {
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            async read() { nowMs += 100; return { done: true, value: undefined }; },
            async cancel() {},
          }),
        },
      };
    }
    // Attempt 4: 200 OK then immediate reader.read() error (< 2000ms) -> must continue backing off!
    if (attempt === 4) {
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            async read() { nowMs += 50; throw new Error("stream reset"); },
            async cancel() {},
          }),
        },
      };
    }
    // Attempts 5..6: 503 & network error -> hit 30000ms cap
    if (attempt === 5) return { ok: false, status: 503 };
    if (attempt === 6) throw new Error("network down");
    if (attempt === 7) return { ok: false, status: 503 };
    // Attempt 8: stable 200 OK stream (>= 2000ms) -> resets failure count to 0, EOF uses base 1000ms
    if (attempt === 8) {
      let step = 0;
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            async read() {
              step += 1;
              if (step === 1) {
                return { done: false, value: Buffer.from("id: ok_1\nevent: log\ndata: {\"message\": \"stable\"}\n\n") };
              }
              nowMs += 2500;
              return { done: true, value: undefined };
            },
            async cancel() {},
          }),
        },
      };
    }
    // Attempt 9: stable 200 OK (>= 2000ms) then read error -> resets old streak and starts fresh at 1x (1000ms)
    if (attempt === 9) {
      let step = 0;
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            async read() {
              step += 1;
              if (step === 1) {
                nowMs += 2500;
                return { done: false, value: Buffer.from("id: ok_2\nevent: log\ndata: {\"message\": \"before err\"}\n\n") };
              }
              throw new Error("late drop");
            },
            async cancel() {},
          }),
        },
      };
    }
    // Attempt 10: 401 cancels reconnect & resets failure count
    return { ok: false, status: 401 };
  };

  const delays = [];
  connectSSE();
  await flush();
  for (let i = 0; i < 7; i += 1) {
    const tid = state.sseReconnectTimer;
    assert(tid !== null, "expected reconnect timer on attempt " + (i + 1));
    const info = timers.get(tid);
    delays.push(info.ms);
    timers.delete(tid);
    info.fn();
    await flush();
  }
  assert.deepStrictEqual(delays, [1000, 2000, 4000, 8000, 16000, 30000, 30000]);

  // Attempt 8 was stable (>= 2000ms) -> sseFailureCount reset to 0 and clean EOF uses base delay 1000
  assert.strictEqual(state.sseFailureCount, 0);
  const stableTid = state.sseReconnectTimer;
  assert.strictEqual(timers.get(stableTid).ms, 1000);
  const stableInfo = timers.get(stableTid);
  timers.delete(stableTid);
  stableInfo.fn();
  await flush();

  // Attempt 9 was stable before throwing -> starts fresh at sseFailureCount === 1, delay === 1000
  assert.strictEqual(state.sseFailureCount, 1);
  const lateErrTid = state.sseReconnectTimer;
  assert.strictEqual(timers.get(lateErrTid).ms, 1000);
  const lateErrInfo = timers.get(lateErrTid);
  timers.delete(lateErrTid);
  lateErrInfo.fn();
  await flush();

  // Attempt 10 returned 401 -> stops reconnecting and resets failure count
  assert.strictEqual(state.sseReconnectTimer, null, "401 must cancel reconnect");
  assert.strictEqual(state.sseFailureCount, 0);

  // 2. Switching project while backoff timer is pending cancels timer and resets failure count;
  //    then clearing project cancels active stream and keeps reconnect stopped
  let cancelCalled = false;
  streamBehavior = async (url) => {
    if (url.includes("projBackoff")) return { ok: false, status: 502 };
    if (url.includes("projNext")) {
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: () => new Promise(() => {}),
            cancel() { cancelCalled = true; },
          }),
        },
      };
    }
    return { ok: true, status: 200, text: async () => "{}", headers: new Map() };
  };
  connectSSE();
  await flush();
  assert.strictEqual(state.sseFailureCount, 1);
  const pendingTid = state.sseReconnectTimer;
  assert(pendingTid !== null);
  await selectProject("projNext");
  await flush();
  assert(clearedTimers.includes(pendingTid), "project switch must cancel pending backoff timer");
  assert.strictEqual(state.sseFailureCount, 0);
  const activeAbort = state.sseAbort;
  await selectProject("");
  assert.strictEqual(activeAbort.signal.aborted, true, "clearing project must abort active stream");
  assert.strictEqual(state.sseReconnectTimer, null);
  assert.strictEqual(state.sseFailureCount, 0);

  disconnectSSE();
  console.log("WEB_SSE_BACKOFF_OK");
})().catch((err) => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run([
            node, "-e", script,
            str(Path(__file__).resolve().parents[1] / "src" / "aria_x" / "web" / "i18n.js"),
            str(Path(__file__).resolve().parents[1] / "src" / "aria_x" / "web" / "app.js"),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WEB_SSE_BACKOFF_OK", proc.stdout)

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

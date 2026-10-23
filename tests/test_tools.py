"""Tool-layer tests: scope denial, command policy, HTTP parsing, evidence and knowledge."""

from __future__ import annotations

import json
import tempfile
import urllib.error
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from aria_x.config import AriaXConfig, SandboxConfig, ScopeConfig
from aria_x.errors import ScopeViolation
from aria_x.events import EventBus
from aria_x.models import Node, Project, Trace
from aria_x.store import Store
from aria_x.tools.base import ToolContext
from aria_x.tools.builtin import TOOL_NAMES, build_registry
from aria_x.tools.sandbox import MockSandbox, SandboxResult
from aria_x.tools.scope import Scope


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8", **headers: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/ok":
            self._send(200, "hello-aria-x")
        elif self.path == "/json":
            self._send(200, '{"ok":true}', "application/json")
        elif self.path == "/missing":
            self._send(404, "not-found")
        elif self.path == "/redirect":
            self._send(302, "", Location="/ok")
        elif self.path == "/jump":
            self._send(302, "", Location="http://evil.test/")
        elif self.path == "/secret":
            self._send(200, "token=supersecret123")
        elif self.path == "/echo":
            self._send(200, "auth=" + self.headers.get("Authorization", "none"))
        else:
            self._send(404, "unknown")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        self._send(200, body)

    def log_message(self, *args: object) -> None:
        pass


class HTTPFixture:
    server: ThreadingHTTPServer
    thread: threading.Thread
    port: int


def _start_server() -> HTTPFixture:
    fixture = HTTPFixture()
    fixture.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    fixture.port = fixture.server.server_address[1]
    fixture.thread = threading.Thread(target=fixture.server.serve_forever, daemon=True)
    fixture.thread.start()
    return fixture


class ToolTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.http = _start_server()
        cls.base = f"http://127.0.0.1:{cls.http.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.http.server.shutdown()
        cls.http.server.server_close()
        cls.http.thread.join(timeout=5)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self._tmp.name) / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.config = AriaXConfig()
        self.config.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"],
                                        allow_loopback=True, allow_private=True, max_rps=1000.0)
        self.config.sandbox = SandboxConfig(backend="mock", max_output=4000, timeout=5)
        self.project = Project.new("tools-test", "Prove the tool layer works")
        self.store = Store(":memory:")
        self.store.create_project(self.project)
        self.events = EventBus()
        self.scope = Scope(self.config.scope)
        self.sandbox = MockSandbox(self.config.sandbox, self.workdir)
        self.ctx = ToolContext(project=self.project, run_id="r1", task_id="t1", agent="tester",
                               role="worker", store=self.store, config=self.config,
                               events=self.events, scope=self.scope, sandbox=self.sandbox,
                               workdir=self.workdir)
        self.registry = build_registry(self.ctx)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def run_tool(self, name: str, **args: object):
        return self.registry.execute(name, args, self.ctx)

    # -- catalog ---------------------------------------------------------

    def test_catalog_matches_contract(self):
        self.assertEqual(set(self.registry.names()), set(TOOL_NAMES))
        spec = self.registry.get("terminal_exec").spec
        self.assertEqual(spec.risk, "high")
        self.assertTrue(self.registry.get("http_request").spec.requires_scope)

    # -- HTTP ------------------------------------------------------------

    def test_http_request_returns_parsed_response(self):
        result = self.run_tool("http_request", url=f"{self.base}/ok")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["status"], 200)
        self.assertIn("hello-aria-x", result.data["body_excerpt"])
        self.assertEqual(result.data["content_type"], "text/plain; charset=utf-8")
        self.assertGreaterEqual(result.data["elapsed_ms"], 0)

    def test_http_non_2xx_is_a_normal_result(self):
        result = self.run_tool("http_request", url=f"{self.base}/missing")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["status"], 404)
        self.assertFalse(result.data["ok"])
        self.assertIn("not-found", result.data["body_excerpt"])

    def test_http_redirect_is_followed(self):
        result = self.run_tool("http_request", url=f"{self.base}/redirect")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], 200)
        self.assertTrue(result.data["final_url"].endswith("/ok"))

    def test_http_redirect_to_out_of_scope_host_is_denied(self):
        result = self.run_tool("http_request", url=f"{self.base}/jump")
        self.assertFalse(result.ok)
        self.assertIn("scope", result.error)
        denied = [a for a in self.store.list_audit(self.project.id) if a.decision == "denied"]
        self.assertTrue(any("evil.test" in (a.target or "") for a in denied))

    def test_http_post_body_roundtrip(self):
        result = self.run_tool("http_request", url=f"{self.base}/echo", method="POST", body="ping")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["body_excerpt"], "ping")

    def test_http_evidence_written_and_secrets_redacted(self):
        result = self.run_tool("http_request", url=f"{self.base}/secret",
                               headers={"Authorization": "Bearer supersecret123"})
        self.assertTrue(result.ok)
        evidence_path = result.data.get("evidence_path")
        self.assertTrue(evidence_path, "http_request must persist evidence")
        path = self.workdir / evidence_path
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("REDACTED", text)
        self.assertNotIn("supersecret123", text)
        payload = json.loads(text)
        self.assertEqual(payload["data"]["request"]["headers"]["Authorization"], "[REDACTED]")
        decisions = [a.decision for a in self.store.list_audit(self.project.id)]
        self.assertIn("completed", decisions)

    def test_http_out_of_scope_is_denied_before_io(self):
        ctx = self.ctx.child(scope=Scope(ScopeConfig(authorized=True, targets=["example.test"],
                                                     allow_loopback=False, max_rps=1000.0)))
        result = self.registry.execute("http_request", {"url": f"{self.base}/ok"}, ctx)
        self.assertFalse(result.ok)
        self.assertIn("scope", result.error)
        evidence_dir = self.workdir / "evidence"
        self.assertEqual(list(evidence_dir.glob("*.json")) if evidence_dir.exists() else [], [])
        denied = [a for a in self.store.list_audit(self.project.id) if a.decision == "denied"]
        self.assertTrue(denied)

    def test_http_invalid_method_rejected(self):
        result = self.run_tool("http_request", url=f"{self.base}/ok", method="TRACE")
        self.assertFalse(result.ok)
        self.assertIn("method", result.error)

    def test_web_search_offline_fails_cleanly(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("offline")):
            result = self.run_tool("web_search", query="apache 2.4 cve")
        self.assertFalse(result.ok)
        self.assertIn("web search unavailable", result.error)
        self.assertEqual(self.store.list_audit(self.project.id)[0].action, "web_search")

    def test_dns_lookup_localhost(self):
        result = self.run_tool("dns_lookup", host="127.0.0.1", record_type="PTR")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["record_type"], "PTR")
        self.assertTrue(result.data["records"])

    def test_port_scan_finds_open_and_closed(self):
        result = self.run_tool("port_scan", host="127.0.0.1",
                               ports=f"{self.http.port},9", max_ports=2, timeout=0.4)
        self.assertTrue(result.ok, result.error)
        open_ports = {item["port"] for item in result.data["open_ports"]}
        self.assertIn(self.http.port, open_ports)
        self.assertNotIn(9, open_ports)
        self.assertEqual(result.data["scanned"], 2)

    def test_port_scan_rejects_out_of_scope_host(self):
        ctx = self.ctx.child(scope=Scope(ScopeConfig(authorized=True, targets=["10.0.0.0/8"],
                                                     max_rps=1000.0)))
        result = self.registry.execute("port_scan", {"host": "127.0.0.1"}, ctx)
        self.assertFalse(result.ok)
        self.assertIn("scope", result.error)

    def test_tls_probe_against_plain_http_reports_no_handshake(self):
        result = self.run_tool("tls_probe", host="127.0.0.1", port=self.http.port, timeout=3)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.data["handshake_ok"])
        self.assertIn("error", result.data)

    def test_web_probe_returns_statuses(self):
        result = self.run_tool("web_probe", base_url=self.base, paths=["ok", "missing"])
        self.assertTrue(result.ok, result.error)
        by_path = {item["path"]: item for item in result.data["results"]}
        self.assertEqual(by_path["ok"]["status"], 200)
        self.assertEqual(by_path["missing"]["status"], 404)

    # -- terminal + files -------------------------------------------------

    def test_terminal_exec_uses_sandbox(self):
        result = self.run_tool("terminal_exec", command="printf hi")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["exit_code"], 0)
        self.assertIn("mock-ok", result.data["stdout"])
        self.assertEqual(self.sandbox.calls, ["printf hi"])
        traces = self.store.list_traces(self.project.id)
        self.assertTrue(any(t.tool_name == "terminal_exec" for t in traces))

    def test_terminal_policy_blocks_destructive(self):
        result = self.run_tool("terminal_exec", command="mkfs.ext4 /dev/sda1")
        self.assertFalse(result.ok)
        self.assertIn("blocked", result.error)
        self.assertEqual(self.sandbox.calls, [])

    def test_terminal_scope_heuristic_blocks_out_of_scope_ip(self):
        result = self.run_tool("terminal_exec", command="nmap -sV 8.8.8.8")
        self.assertFalse(result.ok)
        self.assertIn("scope", result.error)
        self.assertEqual(self.sandbox.calls, [])

    def test_file_write_read_list(self):
        written = self.run_tool("file_write", path="notes/a.txt", content="alpha")
        self.assertTrue(written.ok, written.error)
        read = self.run_tool("file_read", path="notes/a.txt")
        self.assertTrue(read.ok, read.error)
        self.assertEqual(read.data["content"], "alpha")
        listing = self.run_tool("file_list", path="notes")
        self.assertTrue(listing.ok)
        self.assertEqual(listing.data["entries"][0]["name"], "a.txt")

    def test_file_read_is_jailed(self):
        result = self.run_tool("file_read", path="../escape.txt")
        self.assertFalse(result.ok)
        self.assertIn("escapes", result.error)

    def test_file_write_is_jailed(self):
        result = self.run_tool("file_write", path="/etc/aria-x-should-not-write", content="x")
        self.assertFalse(result.ok)
        self.assertIn("escapes", result.error)

    def test_file_read_missing_file(self):
        result = self.run_tool("file_read", path="nope.txt")
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.error)

    # -- knowledge --------------------------------------------------------

    def _intent(self) -> Node:
        node = Node.new(self.project.id, "intent", "Probe the login form", "try default creds",
                        status="active")
        self.store.add_node(node)
        return node

    def test_add_fact_creates_node_edge_and_asset(self):
        intent = self._intent()
        result = self.run_tool("add_fact", title="Login form present", body="GET /login returned 200",
                               intent_id=intent.id, asset_values=["app.example.test"])
        self.assertTrue(result.ok, result.error)
        node = self.store.find_node(result.data["node_id"])
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, "fact")
        self.assertEqual(len(result.data["assets"]), 1)
        neighbors = self.store.neighbors(self.project.id, intent.id)
        self.assertTrue(any(edge["rel"] == "yields" for edge in neighbors["out"]))

    def test_add_finding_validates_severity_and_persists_evidence(self):
        bad = self.run_tool("add_finding", title="Bad", severity="catastrophic", body="x")
        self.assertFalse(bad.ok)
        self.assertIn("severity", bad.error)
        good = self.run_tool("add_finding", title="SQL injection", severity="high",
                             body="Parameter id is injectable", cvss=8.1,
                             asset_values=["app.example.test"],
                             evidence=[{"kind": "http", "path": "evidence/x.json",
                                        "excerpt": "HTTP 500 with SQL error"}])
        self.assertTrue(good.ok, good.error)
        self.assertEqual(good.data["severity"], "high")
        evidence = self.store.list_evidence(good.data["finding_id"])
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].kind, "http")
        finding = self.store.find_node(good.data["finding_id"])
        self.assertEqual(finding.severity, "high")

    def test_add_finding_rejects_bad_cvss(self):
        result = self.run_tool("add_finding", title="X", severity="low", body="x", cvss=42)
        self.assertFalse(result.ok)

    def test_add_asset_with_parent(self):
        root = self.run_tool("add_asset", kind="root_domain", value="example.test")
        self.assertTrue(root.ok, root.error)
        child = self.run_tool("add_asset", kind="subdomain", value="app.example.test",
                              parent_value="example.test")
        self.assertTrue(child.ok, child.error)
        self.assertEqual(child.data["parent_id"], root.data["id"])

    def test_add_asset_rejects_bad_kind(self):
        result = self.run_tool("add_asset", kind="database", value="x")
        self.assertFalse(result.ok)
        self.assertIn("kind", result.error)

    def test_anchor_node_links_existing_node(self):
        intent = self._intent()
        self.run_tool("add_asset", kind="endpoint", value="https://app.example.test/login")
        result = self.run_tool("anchor_node", node_id=intent.id,
                               asset_value="https://app.example.test/login")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.store.anchors_for_node(intent.id)), 1)

    def test_graph_query_views(self):
        self._intent()
        self.run_tool("add_fact", title="Port 80 open", body="nmap")
        overview = self.run_tool("graph_query", view="overview")
        self.assertTrue(overview.ok)
        self.assertGreaterEqual(overview.data["counts"]["nodes"].get("fact", 0), 1)
        facts = self.run_tool("graph_query", view="facts", query="port")
        self.assertEqual(facts.data["count"], 1)
        for view in ("findings", "assets", "intents", "todos", "traces"):
            self.assertTrue(self.run_tool("graph_query", view=view).ok, view)

    def test_search_traces_excludes_current_task(self):
        for task_id, content in (("other", "nmap found port 8080 open"),
                                 ("t1", "nmap inside my own task")):
            self.store.add_trace(Trace.new(self.project.id, "r1", task_id, "worker", 0,
                                           "tool_result", content, tool_name="terminal_exec"))
        result = self.run_tool("search_traces", query="nmap")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["items"][0]["task_id"], "other")

    def test_memory_store_and_tfidf_search(self):
        first = self.run_tool("memory_store", text="SQL injection on the login form via id parameter",
                              tags=["sqli", "login"])
        second = self.run_tool("memory_store", text="TLS certificate expired on staging host",
                               tags=["tls", "expiry"])
        self.assertTrue(first.ok and second.ok)
        result = self.run_tool("memory_search", query="sql injection login")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["count"], 1)  # zero-similarity notes are not returned
        self.assertIn("SQL injection", result.data["items"][0]["text"])
        self.assertGreater(result.data["items"][0]["score"], 0)

    def test_todo_create_and_update(self):
        created = self.run_tool("todo_update", title="Enumerate login", detail="try admin/admin")
        self.assertTrue(created.ok, created.error)
        updated = self.run_tool("todo_update", todo_id=created.data["todo_id"], status="done")
        self.assertTrue(updated.ok, updated.error)
        self.assertEqual(updated.data["status"], "done")
        invalid = self.run_tool("todo_update", todo_id=created.data["todo_id"], status="bogus")
        self.assertFalse(invalid.ok)

    def test_prove_goal_requires_real_evidence(self):
        missing = self.run_tool("prove_goal", summary="done", evidence_ids=["n_fake"])
        self.assertFalse(missing.ok)
        finding = self.run_tool("add_finding", title="Proof", severity="info", body="evidence")
        proved = self.run_tool("prove_goal", summary="Goal reached via proof",
                               evidence_ids=[finding.data["finding_id"]])
        self.assertTrue(proved.ok, proved.error)
        goal = self.store.find_node(proved.data["goal_id"])
        self.assertEqual(goal.kind, "goal")
        self.assertEqual(goal.status, "proven")

    def test_delegate_specialist_calls_injected_delegate(self):
        self.ctx.delegate = lambda specialist, question: f"{specialist}: {question}"
        result = self.run_tool("delegate_specialist", specialist="researcher", question="What is XSS?")
        self.assertTrue(result.ok, result.error)
        self.assertIn("researcher:", result.data["answer"])
        unknown = self.run_tool("delegate_specialist", specialist="wizard", question="?")
        self.assertFalse(unknown.ok)

    def test_delegate_unavailable(self):
        self.ctx.delegate = None
        result = self.run_tool("delegate_specialist", specialist="coder", question="write fuzz")
        self.assertFalse(result.ok)
        self.assertIn("not available", result.error)

    def test_scope_check_diagnostic(self):
        allowed = self.run_tool("scope_check", target="http://127.0.0.1:8080/x")
        self.assertTrue(allowed.data["decision"]["allowed"])
        denied = self.run_tool("scope_check", target="http://192.0.2.1/")
        self.assertFalse(denied.data["decision"]["allowed"])


if __name__ == "__main__":
    unittest.main()

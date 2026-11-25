"""DNS pinning: the address that was authorized is the address that is contacted."""

from __future__ import annotations

import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aria_x.config import AriaXConfig, SafetyConfig, SandboxConfig, ScopeConfig
from aria_x.events import EventBus
from aria_x.store import Store
from aria_x.tools import net as net_tool
from aria_x.tools import sandbox as sandbox_mod
from aria_x.tools.base import ToolContext
from aria_x.tools.dnspin import pin_resolution
from aria_x.tools.scope import Scope


def _unresolvable(name: str) -> bool:
    try:
        socket.getaddrinfo(name, 80)
    except socket.gaierror:
        return True
    return False


class PinResolutionTests(unittest.TestCase):
    def test_pinned_host_resolves_to_recorded_address(self) -> None:
        with pin_resolution({"rebind.example.test": "127.0.0.1"}):
            infos = socket.getaddrinfo("rebind.example.test", 80)
        self.assertEqual(infos[0][4][0], "127.0.0.1")

    def test_other_hosts_are_untouched(self) -> None:
        baseline = socket.getaddrinfo("localhost", 80)[0][4][0]
        with pin_resolution({"rebind.example.test": "127.0.0.1"}):
            self.assertEqual(socket.getaddrinfo("localhost", 80)[0][4][0], baseline)

    def test_pin_is_released_on_exit(self) -> None:
        self.assertTrue(_unresolvable("rebind.invalid.example"), "test host must not resolve for real")
        with pin_resolution({"rebind.invalid.example": "127.0.0.1"}):
            self.assertEqual(socket.getaddrinfo("rebind.invalid.example", 80)[0][4][0], "127.0.0.1")
        self.assertTrue(_unresolvable("rebind.invalid.example"), "pin must not leak past the context")

    def test_nested_contexts_merge_and_restore(self) -> None:
        with pin_resolution({"a.example.test": "127.0.0.1"}):
            with pin_resolution({"b.example.test": "127.0.0.2"}):
                self.assertEqual(socket.getaddrinfo("a.example.test", 80)[0][4][0], "127.0.0.1")
                self.assertEqual(socket.getaddrinfo("b.example.test", 80)[0][4][0], "127.0.0.2")
            self.assertTrue(_unresolvable("b.example.test"))


class PinnedHttpRequestTests(unittest.TestCase):
    """End-to-end: a hostname pinned to loopback must reach the local server."""

    def setUp(self) -> None:
        seen: list[tuple[str, str]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                seen.append((self.headers.get("Host", ""), self.path))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                pass

        self.seen = seen
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = AriaXConfig(data_dir=str(self.tmp), runs_dir=str(self.tmp / "runs"))
        self.cfg.scope = ScopeConfig(
            authorized=True, targets=["rebind.example.test"],
            allow_private=True, allow_loopback=True, max_rps=100,
        )
        self.cfg.sandbox = SandboxConfig(backend="local")
        self.cfg.safety = SafetyConfig(approval="auto")
        self.store = Store(":memory:")
        self.bus = EventBus()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def _ctx(self, scope: Scope) -> ToolContext:
        return ToolContext(
            project=SimpleNamespace(id="p_test"), run_id="r_test", workdir=self.tmp,
            config=self.cfg, scope=scope, sandbox=sandbox_mod.build_sandbox(self.cfg.sandbox, self.tmp),
            store=self.store, events=self.bus, agent="worker", role="worker", task_id=None,
        )

    def test_authorized_address_is_the_one_contacted(self) -> None:
        scope = Scope(self.cfg.scope, allow_public_targets=True)
        # The scope layer resolves the name once and records the answer.
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
            scope.authorize_host("rebind.example.test", self.port)
        self.assertEqual(scope.pinned_addresses().get("rebind.example.test"), "127.0.0.1")

        result = net_tool.http_request(
            self._ctx(scope),
            {"url": f"http://rebind.example.test:{self.port}/pinned", "method": "GET"},
        )
        self.assertTrue(result.ok, getattr(result, "error", ""))
        self.assertTrue(self.seen, "local server received no request")
        host_header, path = self.seen[0]
        self.assertEqual(path, "/pinned")
        self.assertIn("rebind.example.test", host_header, "Host header must keep the hostname")


if __name__ == "__main__":
    unittest.main()

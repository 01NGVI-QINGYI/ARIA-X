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

class RedirectAndProbePinningTests(unittest.TestCase):
    """Every hop and every TCP-based tool must use the authorized address."""

    def _scope(self):
        cfg = ScopeConfig(authorized=True, targets=["127.0.0.1", "hop2.invalid"],
                          allow_private=True, allow_loopback=True, max_rps=100)
        return Scope(cfg, allow_public_targets=True)

    def test_redirect_hop_uses_the_address_authorized_for_that_hop(self) -> None:
        landed: list[tuple[str, str]] = []

        class Landed(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                landed.append((self.headers.get("Host", ""), self.path))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Landed)
        target_port = target.server_address[1]
        threading.Thread(target=target.serve_forever, daemon=True).start()

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://hop2.invalid:{target_port}/landed")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        start_port = redirector.server_address[1]
        threading.Thread(target=redirector.serve_forever, daemon=True).start()

        tmp = Path(tempfile.mkdtemp())
        cfg = AriaXConfig(data_dir=str(tmp), runs_dir=str(tmp / "runs"))
        cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.1", "hop2.invalid"],
                                allow_private=True, allow_loopback=True, max_rps=100)
        cfg.sandbox = SandboxConfig(backend="local")
        cfg.safety = SafetyConfig(approval="auto")
        scope = self._scope()
        original = scope.authorize_url

        def patched(url: str, method: str = "GET"):
            decision = original(url, method)
            if decision.allowed and "hop2.invalid" in url:
                scope._pinned["hop2.invalid"] = "127.0.0.1"
            return decision

        scope.authorize_url = patched  # type: ignore[assignment]
        store = Store(":memory:")
        bus = EventBus()
        ctx = ToolContext(
            project=SimpleNamespace(id="p"), run_id="r", workdir=tmp, config=cfg, scope=scope,
            sandbox=sandbox_mod.build_sandbox(cfg.sandbox, tmp), store=store, events=bus,
            agent="worker", role="worker", task_id=None,
        )
        try:
            result = net_tool.http_request(
                ctx, {"url": f"http://127.0.0.1:{start_port}/start", "method": "GET"})
            self.assertTrue(result.ok, getattr(result, "error", ""))
            self.assertTrue(landed, "redirect hop did not reach the pinned address")
            self.assertIn("hop2.invalid", landed[0][0])
        finally:
            store.close()
            redirector.shutdown()
            redirector.server_close()
            target.shutdown()
            target.server_close()

    def test_port_scan_uses_the_pinned_address(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        tmp = Path(tempfile.mkdtemp())
        cfg = AriaXConfig(data_dir=str(tmp), runs_dir=str(tmp / "runs"))
        cfg.scope = ScopeConfig(authorized=True, targets=["pinned.invalid"],
                                allow_private=True, allow_loopback=True, max_rps=100, max_ports=8)
        cfg.sandbox = SandboxConfig(backend="local")
        cfg.safety = SafetyConfig(approval="auto")
        scope = Scope(ScopeConfig(authorized=True, targets=["pinned.invalid"],
                                  allow_private=True, allow_loopback=True, max_rps=100),
                      allow_public_targets=True)
        scope._pinned["pinned.invalid"] = "127.0.0.1"
        store = Store(":memory:")
        bus = EventBus()
        ctx = ToolContext(
            project=SimpleNamespace(id="p"), run_id="r", workdir=tmp, config=cfg, scope=scope,
            sandbox=sandbox_mod.build_sandbox(cfg.sandbox, tmp), store=store, events=bus,
            agent="worker", role="worker", task_id=None,
        )
        try:
            result = net_tool.port_scan(
                ctx, {"host": "pinned.invalid", "ports": str(port), "timeout": 0.3})
            self.assertTrue(result.ok, getattr(result, "error", ""))
            payload = result.data or {}
            ports_open = [item["port"] for item in payload.get("open_ports", [])]
            self.assertIn(port, ports_open, f"expected {port} open, got {payload}")
        finally:
            store.close()
            listener.close()

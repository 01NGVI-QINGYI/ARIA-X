"""DNS pinning: the address that was authorized is the address that is contacted."""

from __future__ import annotations

import contextlib
import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aria_x.config import AriaXConfig, SafetyConfig, SandboxConfig, ScopeConfig
from aria_x.errors import ScopeViolation
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

        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
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
        store = Store(":memory:")
        bus = EventBus()
        ctx = ToolContext(
            project=SimpleNamespace(id="p"), run_id="r", workdir=tmp, config=cfg, scope=scope,
            sandbox=sandbox_mod.build_sandbox(cfg.sandbox, tmp), store=store, events=bus,
            agent="worker", role="worker", task_id=None,
        )
        try:
            def fake_resolve(name: str) -> list[str]:
                return ["127.0.0.1"] if "hop2.invalid" in name else ["127.0.0.1"]

            with mock.patch("aria_x.tools.scope._resolve", side_effect=fake_resolve):
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
        store = Store(":memory:")
        bus = EventBus()
        ctx = ToolContext(
            project=SimpleNamespace(id="p"), run_id="r", workdir=tmp, config=cfg, scope=scope,
            sandbox=sandbox_mod.build_sandbox(cfg.sandbox, tmp), store=store, events=bus,
            agent="worker", role="worker", task_id=None,
        )
        try:
            with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
                result = net_tool.port_scan(
                    ctx, {"host": "pinned.invalid", "ports": str(port), "timeout": 0.3})
            self.assertTrue(result.ok, getattr(result, "error", ""))
            payload = result.data or {}
            ports_open = [item["port"] for item in payload.get("open_ports", [])]
            self.assertIn(port, ports_open, f"expected {port} open, got {payload}")
        finally:
            store.close()
            listener.close()


class PinPoisoningTests(unittest.TestCase):
    """A denied resolution must never be pinned for a later allowed request."""

    def _scope(self, targets):
        return Scope(ScopeConfig(authorized=True, targets=targets, allow_loopback=True,
                                 allow_private=True, max_rps=100),
                     allow_public_targets=True)

    def test_denied_address_is_not_pinned(self) -> None:
        s = self._scope(["127.0.0.2/32"])
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
            denied = s.authorize_host("moving.test", 80)
        self.assertFalse(denied.allowed)
        self.assertNotIn("moving.test", s.pinned_addresses(),
                         "a denied resolution must not be recorded")

    def test_allowed_host_pins_an_address_inside_the_rule(self) -> None:
        s = self._scope(["127.0.0.2/32"])
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1", "127.0.0.2"]):
            allowed = s.authorize_host("multi.test", 80)
        self.assertTrue(allowed.allowed)
        pinned = s.pinned_addresses().get("multi.test")
        self.assertEqual(pinned, "127.0.0.2",
                         "the pinned address must be the one that matched the rule")

    def test_later_authorization_updates_the_pin(self) -> None:
        s = self._scope(["127.0.0.0/8"])
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
            s.authorize_host("rotate.test", 80)
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.2"]):
            allowed = s.authorize_host("rotate.test", 80)
        self.assertTrue(allowed.allowed)
        self.assertEqual(s.pinned_addresses().get("rotate.test"), "127.0.0.2",
                         "the address of the current authorization wins")


class PortScanAddressSelectionTests(unittest.TestCase):
    """port_scan must use the address the authorization approved, not ips[0]."""

    def test_multi_answer_scan_targets_the_permitted_address(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        tmp = Path(tempfile.mkdtemp())
        cfg = AriaXConfig(data_dir=str(tmp), runs_dir=str(tmp / "runs"))
        cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.2/32"],
                                allow_private=True, allow_loopback=True, max_rps=100, max_ports=8)
        cfg.sandbox = SandboxConfig(backend="local")
        cfg.safety = SafetyConfig(approval="auto")
        scope = Scope(cfg.scope, allow_public_targets=True)
        store = Store(":memory:")
        bus = EventBus()
        ctx = ToolContext(
            project=SimpleNamespace(id="p"), run_id="r", workdir=tmp, config=cfg, scope=scope,
            sandbox=sandbox_mod.build_sandbox(cfg.sandbox, tmp), store=store, events=bus,
            agent="worker", role="worker", task_id=None,
        )
        try:
            # .1 is outside the /32, .2 is inside: only .2 may be scanned.
            with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1", "127.0.0.2"]):
                result = net_tool.port_scan(
                    ctx, {"host": "multi.test", "ports": str(port), "timeout": 0.3})
            payload = result.data or {}
            scanned = [item["port"] for item in payload.get("open_ports", [])]
            self.assertNotIn(port, scanned,
                             "port_scan must not probe an address that the rule rejected")
        finally:
            store.close()
            listener.close()


class PerRequestPinningTests(unittest.TestCase):
    """Each connection uses the address its own authorization approved."""

    def _scope(self, targets):
        return Scope(ScopeConfig(authorized=True, targets=targets, allow_loopback=True,
                                 allow_private=True, max_rps=100),
                     allow_public_targets=True)

    def test_cidr_wins_when_a_url_rule_also_matches(self) -> None:
        s = self._scope(["https://mix.test/api", "127.0.0.2/32"])
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1", "127.0.0.2"]):
            decision = s.authorize_url("https://mix.test/api/users")
        self.assertTrue(decision.allowed)
        approved = s.approved_address(decision.host, decision.port, decision.ips)
        self.assertEqual(approved, "127.0.0.2",
                         "an address inside the declared CIDR must win over ips[0]")

    def test_a_later_authorization_does_not_redirect_an_earlier_request(self) -> None:
        s = self._scope(["127.0.0.0/8"])
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
            first = s.authorize_host("race.test", 80)
        first_approved = s.approved_address(first.host, first.port, first.ips)
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.2"]):
            second = s.authorize_host("race.test", 80)
        second_approved = s.approved_address(second.host, second.port, second.ips)
        self.assertEqual(first_approved, "127.0.0.1")
        self.assertEqual(second_approved, "127.0.0.2")
        # The first request must keep its own approved address, not the shared pin.
        self.assertNotEqual(first_approved, s.pinned_addresses().get("race.test"))


class RequestScopedPinMapTests(unittest.TestCase):
    """The connection map comes from this decision, not from the shared cache."""

    def _ctx(self, scope, cfg, tmp):
        store = Store(":memory:")
        bus = EventBus()
        ctx = ToolContext(
            project=SimpleNamespace(id="p"), run_id="r", workdir=tmp, config=cfg, scope=scope,
            sandbox=sandbox_mod.build_sandbox(cfg.sandbox, tmp), store=store, events=bus,
            agent="worker", role="worker", task_id=None,
        )
        self.addCleanup(store.close)
        return ctx

    def test_pin_map_ignores_a_poisoned_shared_cache(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        cfg = AriaXConfig(data_dir=str(tmp), runs_dir=str(tmp / "runs"))
        cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.3/32"],
                                allow_private=True, allow_loopback=True, max_rps=100)
        cfg.sandbox = SandboxConfig(backend="local")
        cfg.safety = SafetyConfig(approval="auto")
        scope = Scope(cfg.scope, allow_public_targets=True)
        scope._pinned["race.test"] = "127.0.0.1"      # another request's address

        captured: dict[str, dict[str, str]] = {}

        @contextlib.contextmanager
        def recorder(pins):
            captured["map"] = dict(pins() if callable(pins) else (pins or {}))
            yield

        with mock.patch("aria_x.tools.net.pin_resolution", recorder), \
                mock.patch("aria_x.tools.scope._resolve",
                           return_value=["127.0.0.1", "127.0.0.3"]):
            net_tool.http_request(
                self._ctx(scope, cfg, tmp),
                {"url": "http://race.test:9/never", "method": "GET"},
            )
        self.assertEqual(captured["map"].get("race.test"), "127.0.0.3",
                         "the request must pin its own approved address, not the shared one")

    def test_path_mismatched_url_rule_falls_back_to_cidr_address(self) -> None:
        s = Scope(ScopeConfig(authorized=True,
                              targets=["https://app.example.test/public", "10.0.0.2/32"],
                              allow_private=True, allow_loopback=True, max_rps=100),
                  allow_public_targets=True)
        with mock.patch("aria_x.tools.scope._resolve", return_value=["10.0.0.99", "10.0.0.2"]):
            decision = s.authorize_url("https://app.example.test/admin")
        self.assertTrue(decision.allowed, "authorized by the CIDR rule")
        approved = s.approved_address(decision.host, decision.port, decision.ips)
        self.assertEqual(approved, "10.0.0.2",
                         "a path-mismatched URL rule must not drag the pin outside the CIDR")


class UnverifiedAddressRefusalTests(unittest.TestCase):
    """No approved address means no connection: never resolve at connect time."""

    def _scope(self, targets, *, allow_private: bool = False):
        return Scope(ScopeConfig(authorized=True, targets=targets, allow_loopback=True,
                                 allow_private=allow_private, max_rps=100),
                     allow_public_targets=True)

    def _ctx(self, scope):
        tmp = Path(tempfile.mkdtemp())
        cfg = AriaXConfig(data_dir=str(tmp), runs_dir=str(tmp / "runs"))
        cfg.scope = scope.config
        cfg.sandbox = SandboxConfig(backend="local")
        cfg.safety = SafetyConfig(approval="auto")
        store = Store(":memory:")
        bus = EventBus()
        self.addCleanup(store.close)
        return ToolContext(
            project=SimpleNamespace(id="p"), run_id="r", workdir=tmp, config=cfg, scope=scope,
            sandbox=sandbox_mod.build_sandbox(cfg.sandbox, tmp), store=store, events=bus,
            agent="worker", role="worker", task_id=None,
        )

    def test_verified_target_rejects_empty_addresses(self) -> None:
        s = self._scope(["late.test"])
        decision = s.authorize_host("late.test", 80)
        self.assertTrue(decision.allowed)
        self.assertEqual(list(decision.ips), [])
        with self.assertRaises(ScopeViolation):
            net_tool._verified_target(self._ctx(s), decision, "late.test", 80)

    def test_http_request_does_not_connect_without_an_address(self) -> None:
        landed: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                landed.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        scope = self._scope(["late.test"])
        ctx = self._ctx(scope)
        with mock.patch("aria_x.tools.scope._resolve", return_value=[]):
            with self.assertRaises(ScopeViolation):
                net_tool.http_request(ctx, {"url": f"http://late.test:{port}/leak",
                                            "method": "GET"})
        self.assertEqual(landed, [], "no packet may leave for an unverified host")

    def test_port_scan_and_tls_probe_refuse_unverified_hosts(self) -> None:
        scope = self._scope(["late.test"])
        ctx = self._ctx(scope)
        with mock.patch("aria_x.tools.scope._resolve", return_value=[]):
            with self.assertRaises(ScopeViolation):
                net_tool.port_scan(ctx, {"host": "late.test", "ports": "80"})
            with self.assertRaises(ScopeViolation):
                net_tool.tls_probe(ctx, {"host": "late.test", "port": 443})

    def test_ip_literals_and_resolved_hosts_still_work(self) -> None:
        s = self._scope(["127.0.0.1"])
        ctx = self._ctx(s)
        literal = s.authorize_host("127.0.0.1", 80)
        self.assertEqual(net_tool._verified_target(ctx, literal, "127.0.0.1", 80), "127.0.0.1")
        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
            resolved = s.authorize_host("ok.test", 80)
        self.assertEqual(net_tool._verified_target(ctx, resolved, "ok.test", 80), "127.0.0.1")

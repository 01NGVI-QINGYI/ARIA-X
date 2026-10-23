"""Scope engine tests: authorization, deny rules, heuristics and rate limiting."""

from __future__ import annotations

import time
import unittest
from unittest import mock

from aria_x.config import ScopeConfig
from aria_x.errors import ScopeViolation
from aria_x.tools.scope import Scope, RateLimiter


def make_scope(targets=("127.0.0.1",), *, deny=(), allow_loopback=True,
               allow_private=True, max_rps=1000.0) -> Scope:
    return Scope(ScopeConfig(authorized=True, targets=list(targets), deny=list(deny),
                             allow_loopback=allow_loopback, allow_private=allow_private,
                             max_rps=max_rps))


class ScopeAuthorizationTests(unittest.TestCase):
    def test_exact_host_and_port_scope(self):
        scope = make_scope(["127.0.0.1:8080"])
        self.assertTrue(scope.authorize_host("127.0.0.1", 8080).allowed)
        self.assertFalse(scope.authorize_host("127.0.0.1", 9090).allowed)
        self.assertFalse(scope.authorize_host("127.0.0.2").allowed)

    def test_cidr_target(self):
        scope = make_scope(["10.0.0.0/8"])
        self.assertTrue(scope.authorize_host("10.20.30.40").allowed)
        self.assertFalse(scope.authorize_host("11.0.0.1").allowed)

    def test_url_target_with_path_prefix_and_port(self):
        scope = make_scope(["http://127.0.0.1:8080/app"])
        self.assertTrue(scope.authorize_url("http://127.0.0.1:8080/app/index").allowed)
        self.assertFalse(scope.authorize_url("http://127.0.0.1:9090/app/index").allowed)
        self.assertFalse(scope.authorize_url("ftp://127.0.0.1:8080/app").allowed)

    def test_wildcard_domain(self):
        scope = make_scope(["*.example.test"])
        with mock.patch("aria_x.tools.scope._resolve", return_value=[]):
            self.assertTrue(scope.authorize_host("api.example.test").allowed)
            self.assertTrue(scope.authorize_host("deep.api.example.test").allowed)
            self.assertFalse(scope.authorize_host("10.9.9.9").allowed)

    def test_deny_rule_wins(self):
        scope = make_scope(["10.0.0.0/8"], deny=["10.1.2.3"])
        decision = scope.authorize_host("10.1.2.3")
        self.assertFalse(decision.allowed)
        self.assertIn("deny", decision.reason)

    def test_unauthorized_config_refuses_everything(self):
        scope = Scope(ScopeConfig(authorized=False, targets=["127.0.0.1"]))
        self.assertFalse(scope.authorize_host("127.0.0.1").allowed)

    def test_empty_targets_refuses(self):
        scope = Scope(ScopeConfig(authorized=True, targets=[]))
        self.assertFalse(scope.authorize_host("127.0.0.1").allowed)

    def test_loopback_can_be_forbidden(self):
        scope = make_scope(["127.0.0.1"], allow_loopback=False)
        decision = scope.authorize_host("127.0.0.1")
        self.assertFalse(decision.allowed)
        self.assertIn("loopback", decision.reason)

    def test_require_url_raises_scope_violation(self):
        scope = make_scope(["127.0.0.1"])
        with self.assertRaises(ScopeViolation):
            scope.require_url("http://192.0.2.1/")
        decision = scope.require_url("http://127.0.0.1:8080/ok")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.host, "127.0.0.1")
        self.assertEqual(decision.port, 8080)

    def test_summary_is_json_ready(self):
        summary = make_scope(["127.0.0.1", "10.0.0.0/24"]).summary()
        self.assertTrue(summary["authorized"])
        self.assertIn("127.0.0.1", summary["targets"])
        self.assertIn("10.0.0.0/24", summary["networks"])


class ScopeCommandHeuristicTests(unittest.TestCase):
    def test_literal_out_of_scope_ip_is_flagged(self):
        scope = make_scope(["127.0.0.1"])
        self.assertFalse(scope.check_command("nmap -sV 8.8.8.8").allowed)

    def test_in_scope_literal_is_allowed(self):
        scope = make_scope(["127.0.0.1"])
        self.assertTrue(scope.check_command("curl http://127.0.0.1:8080/ok").allowed)
        self.assertTrue(scope.check_command("echo hello").allowed)

    def test_out_of_scope_url_is_flagged(self):
        scope = make_scope(["127.0.0.1"])
        self.assertFalse(scope.check_command("curl http://192.0.2.1/data").allowed)


class RateLimiterTests(unittest.TestCase):
    def test_per_host_throttle(self):
        limiter = RateLimiter(max_rps=50.0)
        started = time.monotonic()
        limiter.acquire("127.0.0.1")
        limiter.acquire("127.0.0.1")
        self.assertGreaterEqual(time.monotonic() - started, 0.015)

    def test_no_throttle_when_disabled(self):
        limiter = RateLimiter(max_rps=0.0)
        started = time.monotonic()
        for _ in range(20):
            limiter.acquire("127.0.0.1")
        self.assertLess(time.monotonic() - started, 0.1)


if __name__ == "__main__":
    unittest.main()

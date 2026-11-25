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
        self.assertFalse(scope.authorize_url("http://127.0.0.1:8080/admin").allowed)
        self.assertFalse(scope.authorize_url("http://127.0.0.1:8080/app/../admin").allowed)
        self.assertFalse(scope.authorize_url("https://127.0.0.1:8080/app/index").allowed)
        self.assertFalse(scope.authorize_url("http://127.0.0.1:9090/app/index").allowed)
        self.assertFalse(scope.authorize_url("ftp://127.0.0.1:8080/app").allowed)

    def test_wildcard_url_target_enforces_scheme_and_prefix(self):
        scope = make_scope(["https://*.staging.example.test/api"])
        with mock.patch("aria_x.tools.scope._resolve", return_value=[]):
            self.assertTrue(scope.authorize_url("https://app.staging.example.test/api/v1").allowed)
            self.assertFalse(scope.authorize_url("http://app.staging.example.test/api/v1").allowed)
            self.assertFalse(scope.authorize_url("https://app.staging.example.test/admin").allowed)

    def test_allow_public_targets_false_blocks_global_ips(self):
        scope = Scope(
            ScopeConfig(authorized=True, targets=["8.8.8.8", "127.0.0.1"], allow_loopback=True, max_rps=1000.0),
            allow_public_targets=False,
        )
        self.assertTrue(scope.authorize_host("127.0.0.1").allowed)
        decision = scope.authorize_host("8.8.8.8")
        self.assertFalse(decision.allowed)
        self.assertIn("public address", decision.reason)

    def test_strict_url_path_prefix_blocks_encoded_and_ambiguous_traversal(self):
        scope = make_scope(["http://127.0.0.1:8080/api"])
        for allowed in (
            "http://127.0.0.1:8080/api",
            "http://127.0.0.1:8080/api/",
            "http://127.0.0.1:8080/api/v1/items",
            "http://127.0.0.1:8080/api/users/alice%20smith",
            "http://127.0.0.1:8080/api/files?path=../flag.txt",
        ):
            self.assertTrue(scope.authorize_url(allowed).allowed, f"expected allowed: {allowed}")

        for blocked in (
            "http://127.0.0.1:8080/api/../admin",
            "http://127.0.0.1:8080/api/sub/../v1",
            "http://127.0.0.1:8080/api/./v1",
            "http://127.0.0.1:8080/api/%2e%2e/admin",
            "http://127.0.0.1:8080/api/%2E%2E%2Fadmin",
            "http://127.0.0.1:8080/api/%252e%252e/admin",
            "http://127.0.0.1:8080/api/%25252525252e%25252525252e/admin",
            "http://127.0.0.1:8080/api/%252525252520",
            "http://127.0.0.1:8080/api/%5c..%5cadmin",
            "http://127.0.0.1:8080/api/..;/admin",
            "http://127.0.0.1:8080/api;foo/bar",
            "http://127.0.0.1:8080/api\\..\\admin",
            "http://127.0.0.1:8080//api/v1",
            "http://127.0.0.1:8080/api//v1",
            "http://127.0.0.1:8080/api/%ZZ",
            "http://127.0.0.1:8080/api/v1\r\n",
        ):
            decision = scope.authorize_url(blocked)
            self.assertFalse(decision.allowed, f"expected blocked: {blocked}")

        over_limit = scope.authorize_url("http://127.0.0.1:8080/api/%252525252520")
        self.assertFalse(over_limit.allowed)
        self.assertIn("exceeds percent-decoding limit", over_limit.reason)

    def test_allow_public_targets_false_fails_closed_on_unresolved_and_mixed_hosts(self):
        scope = Scope(
            ScopeConfig(
                authorized=True,
                targets=["*.example.test", "127.0.0.1"],
                deny=["blocked.example.test"],
                allow_loopback=True,
                allow_private=True,
                max_rps=1000.0,
            ),
            allow_public_targets=False,
        )
        with mock.patch("aria_x.tools.scope._resolve", return_value=[]):
            d_unresolved = scope.authorize_host("app.example.test")
            self.assertFalse(d_unresolved.allowed)
            self.assertIn("unresolved", d_unresolved.reason)

        with mock.patch("aria_x.tools.scope._resolve", return_value=["93.184.216.34"]):
            self.assertFalse(scope.authorize_host("app.example.test").allowed)

        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1", "93.184.216.34"]):
            self.assertFalse(scope.authorize_host("app.example.test").allowed)

        with mock.patch("aria_x.tools.scope._resolve", return_value=["10.0.0.5"]):
            self.assertTrue(scope.authorize_host("app.example.test").allowed)

        with mock.patch("aria_x.tools.scope._resolve", return_value=["127.0.0.1"]):
            self.assertTrue(scope.authorize_host("app.example.test").allowed)
            d_deny = scope.authorize_host("blocked.example.test")
            self.assertFalse(d_deny.allowed)
            self.assertIn("deny", d_deny.reason)

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

class CommandHostExtractionTests(unittest.TestCase):
    """Non-IP hostnames in a shell command must reach the scope engine."""

    def _scope(self, **kwargs):
        base = dict(authorized=True, targets=["10.0.0.5", "*.lab.test"],
                    allow_private=True, allow_loopback=True, max_rps=1000)
        base.update(kwargs)
        return Scope(ScopeConfig(**base), allow_public_targets=True)

    def test_user_at_host_is_blocked_out_of_scope(self) -> None:
        s = self._scope()
        self.assertFalse(s.check_command("ssh root@evil.example.com").allowed)
        self.assertTrue(s.check_command("ssh root@10.0.0.5").allowed)
        self.assertTrue(s.check_command("ssh root@db.lab.test").allowed)

    def test_unc_share_is_blocked_out_of_scope(self) -> None:
        s = self._scope()
        self.assertFalse(s.check_command("smbclient -L //evil.example.com/share").allowed)
        self.assertTrue(s.check_command("smbclient -L //10.0.0.5/share").allowed)

    def test_host_flag_and_db_clients(self) -> None:
        s = self._scope()
        self.assertFalse(s.check_command("mysql -h evil.example.com -u root").allowed)
        self.assertTrue(s.check_command("mysql -h db.lab.test -u root").allowed)
        self.assertFalse(s.check_command("psql -h evil.example.com -U p").allowed)

    def test_positional_host_for_network_tools(self) -> None:
        s = self._scope()
        self.assertFalse(s.check_command("ssh evil.example.com").allowed)
        self.assertFalse(s.check_command("crackmapexec smb evil.example.com").allowed)
        self.assertTrue(s.check_command("ssh db.lab.test").allowed)

    def test_git_only_checks_remote_subcommands(self) -> None:
        s = self._scope()
        self.assertFalse(s.check_command("git clone user@git.evil.example.com:repo").allowed)
        # a commit message may contain an e-mail address and must not be flagged
        self.assertTrue(s.check_command('git commit -m "mail admin@corp.example.com"').allowed)

    def test_email_in_payload_is_not_a_target(self) -> None:
        s = self._scope()
        cmd = 'sqlmap --data "email=admin@corp.example.com" -u http://10.0.0.5/a'
        self.assertTrue(s.check_command(cmd).allowed)

    def test_plain_commands_pass(self) -> None:
        s = self._scope()
        for cmd in ("echo hello", "ls -la", "python3 -c 'print(1)'"):
            self.assertTrue(s.check_command(cmd).allowed, cmd)

class CommandHostBypassAndFalsePositiveTests(unittest.TestCase):
    """Regression for both directions: real targets found, local files not blocked."""

    def _scope(self):
        return Scope(ScopeConfig(authorized=True, targets=["10.0.0.5", "*.lab.test"],
                                 allow_private=True, allow_loopback=True, max_rps=1000),
                     allow_public_targets=True)

    def test_scanners_and_positional_hosts_are_found(self) -> None:
        s = self._scope()
        for cmd in (
            "nmap -sV evil.example.com",
            "nikto -h evil.example.com",
            "nc evil.example.com 4444",
            "dig evil.example.com A",
            "ssh evil.example.com id",
            "sudo nmap 8.8.8.8",
            "proxychains4 nc evil.example.com 80",
            "sshpass -p x ssh root@evil.example.com",
        ):
            self.assertFalse(s.check_command(cmd).allowed, cmd)

    def test_chained_and_wrapped_commands_are_found(self) -> None:
        s = self._scope()
        for cmd in (
            "timeout 5 ssh root@evil.example.com",
            "echo ok && ssh root@evil.example.com",
            "cat file.txt | xargs curl https://evil.example.com/x",
            "git -C /tmp clone user@git.evil.example.com:repo",
        ):
            self.assertFalse(s.check_command(cmd).allowed, cmd)

    def test_joined_and_equals_host_flags_are_found(self) -> None:
        s = self._scope()
        for cmd in ("mysql --host=evil.example.com -u r", "mysql -hevil.example.com -u r",
                    "curl ftp://u:p@evil.example.com/f"):
            self.assertFalse(s.check_command(cmd).allowed, cmd)

    def test_local_file_names_are_not_blocked(self) -> None:
        s = self._scope()
        for cmd in (
            "scp root@10.0.0.5:/tmp/loot.txt loot.txt",
            "ssh root@10.0.0.5 cat config.json",
            "rsync -av root@db.lab.test:/var/backup/ dump.sql",
        ):
            self.assertTrue(s.check_command(cmd).allowed, cmd)

    def test_plain_text_with_host_port_is_not_a_target(self) -> None:
        s = self._scope()
        for cmd in ('grep -n "api.example.com:443" log.txt',
                    'printf "db.internal:3306\\n"', "echo hello", "ls -la"):
            self.assertTrue(s.check_command(cmd).allowed, cmd)

    def test_payload_email_is_not_a_target(self) -> None:
        s = self._scope()
        cmd = 'sqlmap --data "email=admin@corp.example.com" -u http://10.0.0.5/a'
        self.assertTrue(s.check_command(cmd).allowed)

    def test_git_commit_message_is_not_a_target(self) -> None:
        s = self._scope()
        self.assertTrue(s.check_command('git commit -m "mail admin@corp.example.com"').allowed)
        self.assertTrue(s.check_command("git -C /tmp status").allowed)

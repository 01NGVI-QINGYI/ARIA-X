"""Core tests: store, config, events, scope, JSON parsing.

These do not depend on the engine or the tool layer, so they run even while
those modules are being built in parallel.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aria_x.config import AriaXConfig, BudgetConfig, ScopeConfig
from aria_x.errors import BudgetExceeded, ConfigError, ScopeViolation
from aria_x.events import EventBus, wait_for
from aria_x.llm.base import ChatMessage, LLMResponse, ToolCall
from aria_x.llm.jsonmode import extract_json, validate_shape
from aria_x.llm.mock import MockProvider
from aria_x.llm.router import ModelRouter
from aria_x.models import Node, Project, Todo, Trace
from aria_x.store import Store
from aria_x.tools.scope import Scope


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.project = Project.new("test", "prove the demo")
        self.store.create_project(self.project)

    def tearDown(self) -> None:
        self.store.close()

    def test_project_and_counts(self) -> None:
        self.assertEqual(self.store.get_project(self.project.id).name, "test")
        self.assertEqual(self.store.counts(self.project.id)["nodes"], {})

    def test_nodes_edges_assets_findings(self) -> None:
        fact = self.store.add_node(Node.new(self.project.id, "fact", "port 80 open", "banner"))
        intent = self.store.add_node(Node.new(self.project.id, "intent", "probe /files"))
        self.store.add_edge(self.project.id, intent.id, fact.id, "yields")
        asset = self.store.upsert_asset(self.project.id, "host", "127.0.0.1")
        find = self.store.add_finding(self.project.id, "Path traversal", "evidence",
                                      severity="high", asset_ids=[asset.id], intent_id=intent.id)
        self.assertEqual(self.store.anchors_for_node(find.id)[0].value, "127.0.0.1")
        self.assertEqual(self.store.counts(self.project.id)["findings_by_severity"], {"high": 1})
        self.assertEqual(len(self.store.list_edges(self.project.id)), 2)  # yields + proves
        self.assertEqual(self.store.nodes_for_asset(asset.id)[0].id, find.id)

    def test_claim_intent_is_atomic(self) -> None:
        intent = self.store.add_node(Node.new(self.project.id, "intent", "one"))
        self.assertTrue(self.store.claim_intent(intent.id))
        self.assertFalse(self.store.claim_intent(intent.id))
        self.assertEqual(self.store.get_node(intent.id).status, "active")

    def test_todos_ready_and_update(self) -> None:
        one = self.store.add_todo(Todo.new(self.project.id, 1, "first"))
        two = self.store.add_todo(Todo.new(self.project.id, 2, "second", depends_on=[one.id]))
        ready = self.store.ready_todos(self.project.id)
        self.assertEqual([t.id for t in ready], [one.id])
        self.store.update_todo(one.id, status="done")
        self.assertEqual([t.id for t in self.store.ready_todos(self.project.id)], [two.id])

    def test_trace_search_excludes_task(self) -> None:
        self.store.add_trace(Trace.new(self.project.id, "run", "task-a", "worker", 0,
                                       "tool_result", "found /files endpoint", tool_name="http_request"))
        self.store.add_trace(Trace.new(self.project.id, "run", "task-b", "worker", 0,
                                       "tool_result", "unrelated", tool_name="dns_lookup"))
        hits = self.store.search_traces(self.project.id, "files")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].task_id, "task-a")
        self.assertEqual(self.store.search_traces(self.project.id, "files", exclude_task_id="task-a"), [])

    def test_approvals_audit_export(self) -> None:
        approval = self.store.request_approval(self.project.id, "run", "task", "terminal_exec",
                                               {"command": "id"}, risk="high")
        self.assertEqual(len(self.store.list_approvals(self.project.id)), 1)
        self.store.decide_approval(approval, True, "ok")
        self.assertEqual(self.store.get_approval(approval)["status"], "approved")
        self.store.audit(self.project.id, "lead", "scope.check", "allowed", "127.0.0.1")
        self.assertEqual(len(self.store.list_audit(self.project.id)), 1)
        export = self.store.export_project(self.project.id)
        self.assertEqual(export["project"]["id"], self.project.id)
        self.assertIn("nodes", export)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self) -> None:
        cfg = AriaXConfig.load(None)
        self.assertEqual(cfg.llm.provider, "mock")
        with self.assertRaises(ConfigError):
            cfg.require_authorization()
        cfg.scope.authorized = True
        cfg.scope.targets = ["127.0.0.1"]
        cfg.require_authorization()

    def test_toml_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aria-x.toml"
            path.write_text("[scope]\nauthorized = true\ntargets = [\"127.0.0.1\"]\n"
                            "[llm]\nprovider = \"mock\"\nmodel = \"x\"\n", encoding="utf-8")
            cfg = AriaXConfig.load(path)
            self.assertTrue(cfg.scope.authorized)
            self.assertEqual(cfg.llm.model, "x")
        cfg = AriaXConfig.load(None)
        cfg.apply_env({"ARIA_X_PROVIDER": "scenario", "ARIA_X_MODEL": "s1"})
        self.assertEqual(cfg.llm.provider, "scenario")
        self.assertEqual(cfg.llm.model, "s1")

    def test_invalid_provider(self) -> None:
        cfg = AriaXConfig()
        cfg.llm.provider = "nope"
        with self.assertRaises(ConfigError):
            cfg.validate()

    def test_public_dict_allowlist_masks_secrets_and_preserves_live_config(self) -> None:
        import json

        cfg = AriaXConfig()
        cfg.llm.provider = "openai"
        cfg.llm.api_key = "sk-synth-abcd1234567890-xy99"
        cfg.llm.base_url = "https://synthuser:synthpass@gateway.example.test:8443/v1?api_key=synthq&sig=42#frag"
        cfg.llm.extra_headers = {
            "Authorization": "Bearer synth-hdr-secret",
            "X-Custom-Opaque": "synth-custom-hdr",
        }
        cfg.llm.extra_body = {
            "custom_cred": "synth-body-secret",
            "nested": {"opaque_tok": "synth-nested-secret"},
        }
        cfg.llm.scenario = "synth-scenario-secret"
        cfg.sandbox.env = {
            "AWS_SECRET_ACCESS_KEY": "synth-aws",
            "MY_OPAQUE_ENV": "synth-opaque-env",
        }
        cfg.sandbox.extra_args = ["-e", "DOCKER_OPAQUE=synth-arg-secret"]
        cfg.server.auth_token = "synth-auth-token-9999"
        cfg.scope.targets = ["127.0.0.1", "https://suser:spass@lab.example.test:8443/api?tok=stoken"]
        cfg.scope.deny = ["https://duser:dpass@deny.example.test/path?x=secret"]
        cfg.scope.notes = "synth-note-secret"
        cfg.safety.note = "synth-safety-note"

        pub = cfg.public_dict()
        dumped = json.dumps(pub)

        self.assertEqual(pub["llm"]["api_key"], "***")
        self.assertEqual(pub["server"]["auth_token"], "***")
        self.assertEqual(pub["llm"]["base_url"], "https://gateway.example.test:8443/v1")
        self.assertEqual(pub["scope"]["targets"], ["127.0.0.1", "https://lab.example.test:8443/api"])
        self.assertEqual(pub["scope"]["deny"], ["https://deny.example.test/path"])

        for excluded in ("extra_headers", "extra_body", "scenario"):
            self.assertNotIn(excluded, pub["llm"])
        for excluded in ("env", "extra_args"):
            self.assertNotIn(excluded, pub["sandbox"])
        self.assertNotIn("notes", pub["scope"])
        self.assertNotIn("note", pub["safety"])

        for forbidden in (
            "sk-synth-abcd1234567890-xy99", "sk-s", "xy99",
            "synthuser", "synthpass", "synthq", "sig=42",
            "synth-hdr-secret", "X-Custom-Opaque", "synth-custom-hdr",
            "custom_cred", "synth-body-secret", "opaque_tok", "synth-nested-secret",
            "synth-scenario-secret", "AWS_SECRET_ACCESS_KEY", "synth-aws",
            "MY_OPAQUE_ENV", "synth-opaque-env", "DOCKER_OPAQUE", "synth-arg-secret",
            "synth-auth-token-9999", "suser", "spass", "stoken",
            "duser", "dpass", "synth-note-secret", "synth-safety-note",
        ):
            self.assertNotIn(forbidden, dumped)

        with tempfile.TemporaryDirectory() as tmp:
            saved_path = cfg.save(Path(tmp) / "saved.json")
            saved_text = saved_path.read_text(encoding="utf-8")
            saved_obj = json.loads(saved_text)
            self.assertTrue(saved_obj.get("_redacted_export"))
            self.assertNotIn("sk-synth-abcd1234567890-xy99", saved_text)
            self.assertNotIn("synth-auth-token-9999", saved_text)
            self.assertNotIn("synth-custom-hdr", saved_text)
            self.assertNotIn("synth-opaque-env", saved_text)
            with self.assertRaises(ConfigError):
                AriaXConfig.load(saved_path)
            with self.assertRaises(ConfigError):
                AriaXConfig.from_dict(saved_obj)

            exported_path = cfg.export_public(Path(tmp) / "exported.json")
            exported_obj = json.loads(exported_path.read_text(encoding="utf-8"))
            self.assertTrue(exported_obj.get("_redacted_export"))
            with self.assertRaises(ConfigError):
                AriaXConfig.load(exported_path)

        # Mutating returned dict must not affect live config
        pub["llm"]["api_key"] = "mutated"
        pub["scope"]["targets"].append("evil.test")
        self.assertEqual(cfg.llm.api_key, "sk-synth-abcd1234567890-xy99")
        self.assertEqual(cfg.llm.extra_headers["X-Custom-Opaque"], "synth-custom-hdr")
        self.assertEqual(cfg.llm.extra_body["nested"]["opaque_tok"], "synth-nested-secret")
        self.assertEqual(cfg.sandbox.env["MY_OPAQUE_ENV"], "synth-opaque-env")
        self.assertEqual(cfg.server.auth_token, "synth-auth-token-9999")

    def test_save_private_round_trip_permissions_and_placeholder_rejection(self) -> None:
        import json
        import os
        import stat
        from unittest import mock

        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("ARIA_X_")}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = AriaXConfig(
                    name="Private Roundtrip",
                    data_dir=str(root / "data"),
                    runs_dir=str(root / "runs"),
                    log_level="DEBUG",
                )
                cfg.llm.provider = "openai"
                cfg.llm.model = "deepseek-chat"
                cfg.llm.api_key = "sk-synth-roundtrip-9999"
                cfg.llm.base_url = "https://u:p@gw.example.test:8443/v1?k=q"
                cfg.llm.role_models = {"planner": "m1"}
                cfg.llm.extra_headers = {"Authorization": "Bearer synth-hdr", "X-Custom": "val"}
                cfg.llm.extra_body = {"custom_flag": True, "nested": {"k": "v", "nums": [1, 2]}}
                cfg.llm.scenario = "scenarios/demo.json"
                cfg.sandbox.backend = "docker"
                cfg.sandbox.env = {"SYNTH_TOKEN": "tok-123", "FOO": "bar"}
                cfg.sandbox.extra_args = ["--cpus=2", "-e", "A=B"]
                cfg.scope.authorized = True
                cfg.scope.targets = ["127.0.0.1", "https://u1:p1@target.test/app?x=1"]
                cfg.scope.deny = ["10.0.0.0/8"]
                cfg.scope.notes = "synth scope note"
                cfg.safety.approval = "ask"
                cfg.safety.extra_blocked_patterns = ["forbidden_cmd"]
                cfg.safety.note = "synth safety note"
                cfg.server.auth_token = "synth-srv-tok-777"

                target = root / "private.json"
                # Pre-create with permissive mode to ensure atomic replace enforces 0o600
                target.write_text("{}", encoding="utf-8")
                if os.name == "posix":
                    os.chmod(target, 0o644)

                saved = cfg.save_private(target)
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)

                reloaded = AriaXConfig.load(saved)
                self.assertEqual(reloaded.name, "Private Roundtrip")
                self.assertEqual(reloaded.log_level, "DEBUG")
                self.assertEqual(reloaded.llm.provider, "openai")
                self.assertEqual(reloaded.llm.api_key, "sk-synth-roundtrip-9999")
                self.assertEqual(reloaded.llm.base_url, "https://u:p@gw.example.test:8443/v1?k=q")
                self.assertEqual(reloaded.llm.role_models, {"planner": "m1"})
                self.assertEqual(reloaded.llm.extra_headers, {"Authorization": "Bearer synth-hdr", "X-Custom": "val"})
                self.assertEqual(reloaded.llm.extra_body, {"custom_flag": True, "nested": {"k": "v", "nums": [1, 2]}})
                self.assertEqual(reloaded.llm.scenario, "scenarios/demo.json")
                self.assertEqual(reloaded.sandbox.backend, "docker")
                self.assertEqual(reloaded.sandbox.env, {"SYNTH_TOKEN": "tok-123", "FOO": "bar"})
                self.assertEqual(reloaded.sandbox.extra_args, ["--cpus=2", "-e", "A=B"])
                self.assertTrue(reloaded.scope.authorized)
                self.assertEqual(reloaded.scope.targets, ["127.0.0.1", "https://u1:p1@target.test/app?x=1"])
                self.assertEqual(reloaded.scope.deny, ["10.0.0.0/8"])
                self.assertEqual(reloaded.scope.notes, "synth scope note")
                self.assertEqual(reloaded.safety.approval, "ask")
                self.assertEqual(reloaded.safety.extra_blocked_patterns, ["forbidden_cmd"])
                self.assertEqual(reloaded.safety.note, "synth safety note")
                self.assertEqual(reloaded.server.auth_token, "synth-srv-tok-777")

                # Original live object unchanged
                self.assertEqual(cfg.llm.api_key, "sk-synth-roundtrip-9999")
                self.assertEqual(cfg.server.auth_token, "synth-srv-tok-777")

                # Legacy redacted export without _redacted_export marker must still be refused
                legacy_key = root / "legacy_key.json"
                legacy_key.write_text(json.dumps({"llm": {"provider": "openai", "api_key": "***"}}), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    AriaXConfig.load(legacy_key)

                legacy_tok = root / "legacy_tok.json"
                legacy_tok.write_text(json.dumps({"server": {"auth_token": "***"}}), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    AriaXConfig.load(legacy_tok)

                bad_cfg = AriaXConfig()
                bad_cfg.llm.api_key = "***"
                with self.assertRaises(ConfigError):
                    bad_cfg.save_private(root / "bad1.json")
                bad_cfg2 = AriaXConfig()
                bad_cfg2.server.auth_token = "***"
                with self.assertRaises(ConfigError):
                    bad_cfg2.save_private(root / "bad2.json")

    def test_save_private_requires_json_suffix_and_preserves_existing_file(self) -> None:
        from unittest import mock

        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("ARIA_X_")}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = AriaXConfig(name="Suffix Test")
                toml_path = root / "aria-x.toml"
                original = "[scope]\nauthorized = true\ntargets = [\"127.0.0.1\"]\n# keep-me\n"
                toml_path.write_text(original, encoding="utf-8")

                with self.assertRaises(ConfigError):
                    cfg.save_private(toml_path)
                self.assertEqual(toml_path.read_text(encoding="utf-8"), original)
                self.assertEqual(list(root.glob(".*tmp")), [])

                for bad_name in ("aria-x.cfg", "aria-x"):
                    bad_path = root / bad_name
                    with self.assertRaises(ConfigError):
                        cfg.save_private(bad_path)
                    self.assertFalse(bad_path.exists())

                upper_json = cfg.save_private(root / "private.JSON")
                reloaded = AriaXConfig.load(upper_json)
                self.assertEqual(reloaded.name, "Suffix Test")

    def test_export_public_and_save_require_json_suffix_and_preserve_target(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = AriaXConfig(name="Export Suffix Test")
            toml_path = root / "aria-x.toml"
            original = "[scope]\nauthorized = true\ntargets = [\"127.0.0.1\"]\n# keep-me\n"
            toml_path.write_text(original, encoding="utf-8")

            for fn in (cfg.export_public, cfg.save):
                with self.assertRaises(ConfigError):
                    fn(toml_path)
                self.assertEqual(toml_path.read_text(encoding="utf-8"), original)

                deep_toml = root / "deep" / "nested" / "aria-x.toml"
                with self.assertRaises(ConfigError):
                    fn(deep_toml)
                self.assertFalse((root / "deep").exists())

                for bad_name in ("aria-x.cfg", "aria-x"):
                    bad_path = root / bad_name
                    with self.assertRaises(ConfigError):
                        fn(bad_path)
                    self.assertFalse(bad_path.exists())

            pub_path = cfg.export_public(root / "pub.JSON")
            self.assertTrue(json.loads(pub_path.read_text(encoding="utf-8")).get("_redacted_export"))
            save_path = cfg.save(root / "saved.JSON")
            self.assertTrue(json.loads(save_path.read_text(encoding="utf-8")).get("_redacted_export"))

    def test_export_public_and_save_refuse_to_overwrite_existing_json(self) -> None:
        from unittest import mock

        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("ARIA_X_")}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = AriaXConfig(
                    name="Live Private Config",
                    data_dir=str(root / "data"),
                    runs_dir=str(root / "runs"),
                )
                cfg.llm.provider = "openai"
                cfg.llm.api_key = "sk-synth-live-keep-1234"
                cfg.llm.extra_headers = {"X-Keep": "hdr-val"}
                cfg.sandbox.env = {"KEEP_ENV": "env-val"}
                cfg.scope.notes = "keep-note"
                cfg.server.auth_token = "synth-live-tok-5678"

                private_path = cfg.save_private(root / "aria-x.json")
                original_bytes = private_path.read_bytes()

                for fn in (cfg.export_public, cfg.save):
                    with self.assertRaises(ConfigError):
                        fn(private_path)
                    self.assertEqual(private_path.read_bytes(), original_bytes)

                reloaded = AriaXConfig.load(private_path)
                self.assertEqual(reloaded.llm.api_key, "sk-synth-live-keep-1234")
                self.assertEqual(reloaded.server.auth_token, "synth-live-tok-5678")
                self.assertEqual(reloaded.llm.extra_headers, {"X-Keep": "hdr-val"})
                self.assertEqual(reloaded.sandbox.env, {"KEEP_ENV": "env-val"})
                self.assertEqual(reloaded.scope.notes, "keep-note")

                export_path = root / "public-snapshot.json"
                cfg.export_public(export_path)
                for fn in (cfg.export_public, cfg.save):
                    with self.assertRaises(ConfigError):
                        fn(export_path)


class ScopeTests(unittest.TestCase):
    def _scope(self, **kwargs) -> Scope:
        base = dict(authorized=True, targets=["127.0.0.1", "*.example.test", "10.0.0.0/24"],
                    allow_loopback=True, allow_private=True, max_rps=1000)
        base.update(kwargs)
        return Scope(ScopeConfig(**base))

    def test_allowed_and_denied(self) -> None:
        scope = self._scope()
        self.assertTrue(scope.authorize_url("http://127.0.0.1:8000/x").allowed)
        self.assertTrue(scope.authorize_host("api.example.test", 443).allowed)
        self.assertFalse(scope.authorize_host("example.org", 443).allowed)
        self.assertTrue(scope.authorize_host("10.0.0.7", 80).allowed)
        self.assertFalse(scope.authorize_host("10.1.0.7", 80).allowed)
        with self.assertRaises(ScopeViolation):
            scope.require_url("http://example.org/")

    def test_deny_and_loopback_policy(self) -> None:
        scope = self._scope(deny=["127.0.0.1"], allow_loopback=True)
        self.assertFalse(scope.authorize_host("127.0.0.1", 80).allowed)
        scope = self._scope(targets=["127.0.0.1"], allow_loopback=False)
        self.assertFalse(scope.authorize_host("127.0.0.1", 80).allowed)

    def test_not_authorized(self) -> None:
        scope = self._scope(authorized=False)
        self.assertFalse(scope.authorize_host("127.0.0.1", 80).allowed)

    def test_unresolvable_hostname_is_denied(self) -> None:
        scope = self._scope()
        self.assertFalse(scope.authorize_url("http://evil.test/").allowed)
        self.assertFalse(scope.authorize_host("does-not-exist.invalid").allowed)

    def test_command_literal_check(self) -> None:
        scope = self._scope()
        self.assertTrue(scope.check_command("curl http://127.0.0.1:8000/").allowed)
        self.assertFalse(scope.check_command("nmap 8.8.8.8").allowed)


class SandboxPolicyTests(unittest.TestCase):
    def test_destructive_commands_blocked(self) -> None:
        from aria_x.tools.sandbox import CommandPolicy
        policy = CommandPolicy()
        for command in ("rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf /etc", "mkfs.ext4 /dev/sda",
                        "shutdown -h now", ":(){ :|:& };:"):
            self.assertFalse(policy.check(command).allowed, command)
        for command in ("ls -la", "rm -rf ./build", "curl https://example.test/", "echo ok"):
            self.assertTrue(policy.check(command).allowed, command)


class EventTests(unittest.TestCase):
    def test_pubsub_and_replay(self) -> None:
        bus = EventBus()
        sub = bus.subscribe()
        event = bus.emit("run.started", project_id="p", data={"goal": "x"})
        received = sub.get(timeout=1)
        self.assertEqual(received.id, event.id)
        self.assertEqual(bus.replay(5)[0].type, "run.started")
        self.assertIn("event: run.started", event.sse())
        sub.close()

    def test_subscribe_with_snapshot_has_no_gap_or_duplicate(self) -> None:
        bus = EventBus(replay=10)
        first = bus.emit("log", project_id="p1", data={"seq": 1})
        sub, snap = bus.subscribe_with_snapshot()
        try:
            self.assertEqual([e.id for e in snap], [first.id])
            second = bus.emit("log", project_id="p1", data={"seq": 2})
            third = bus.emit("log", project_id="p1", data={"seq": 3})
            self.assertEqual(sub.get(timeout=1).id, second.id)
            self.assertEqual(sub.get(timeout=1).id, third.id)
            self.assertEqual(sub.drain(), [])
        finally:
            sub.close()


class LLMTests(unittest.TestCase):
    def test_json_extraction(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Result: {"a": [1,2,],}'), {"a": [1, 2]})
        with self.assertRaises(ValueError):
            extract_json("not json")

    def test_schema_validation(self) -> None:
        schema = {"type": "object", "required": ["action"],
                  "properties": {"action": {"type": "string"}}}
        self.assertEqual(validate_shape({"action": "x"}, schema), [])
        self.assertTrue(validate_shape({}, schema))
        self.assertTrue(validate_shape({"action": 1}, schema))

    def test_mock_provider(self) -> None:
        provider = MockProvider(["one", {"content": "", "tool_calls": [{"name": "t", "arguments": {"x": 1}}]}])
        first = provider.chat([ChatMessage.user("hi")])
        second = provider.chat([ChatMessage.user("hi")])
        self.assertEqual(first.content, "one")
        self.assertEqual(second.tool_calls[0].name, "t")
        self.assertEqual(second.tool_calls[0].arguments, {"x": 1})

    def test_router_retries_and_budget(self) -> None:
        cfg = AriaXConfig()
        cfg.llm.provider = "mock"
        cfg.llm.max_retries = 0
        calls = {"n": 0}

        def policy(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return "```json\n{\"ok\": true}\n```"
            return '{"ok": true}'

        router = ModelRouter(cfg, provider=MockProvider(policy=policy))
        data, _ = router.json("planner", [ChatMessage.user("go")],
                              schema={"type": "object", "required": ["ok"]})
        self.assertTrue(data["ok"])

    def test_budget_hook(self) -> None:
        class Tight:
            def check_llm(self):
                raise BudgetExceeded("no", dimension="llm_calls")

            def record_llm(self, prompt, completion):
                pass

        cfg = AriaXConfig()
        cfg.llm.provider = "mock"
        router = ModelRouter(cfg, budget=Tight(), provider=MockProvider(["x"]))
        with self.assertRaises(BudgetExceeded):
            router.chat("worker", [ChatMessage.user("x")])


if __name__ == "__main__":
    unittest.main()

class UnimplementedConfigKeyTests(unittest.TestCase):
    """Keys this version does not implement must be reported, not silently dropped."""

    def _load(self, body: str) -> AriaXConfig:
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "cfg.toml"
        path.write_text(body, encoding="utf-8")
        return AriaXConfig.load(path)

    def test_removed_engine_fields_are_reported(self) -> None:
        cfg = self._load(
            "[scope]\nauthorized = true\ntargets = [\"127.0.0.1\"]\n"
            "[engine]\nlanguage = \"zh\"\nskills_dir = \"skills\"\n"
            "planner_role = \"planner\"\nreason_on_change = true\n"
        )
        self.assertIn("engine.skills_dir", cfg.unknown_keys)
        self.assertIn("engine.planner_role", cfg.unknown_keys)
        self.assertIn("engine.reason_on_change", cfg.unknown_keys)
        # known keys in the same section still apply
        self.assertEqual(cfg.engine.language, "zh")

    def test_typo_in_known_section_is_reported(self) -> None:
        cfg = self._load(
            "[scope]\nauthorized = true\ntargets = [\"127.0.0.1\"]\n"
            "[server]\nport = 8787\nbogus_key = 1\n"
        )
        self.assertIn("server.bogus_key", cfg.unknown_keys)
        self.assertEqual(cfg.server.port, 8787)

    def test_removed_fields_are_absent_from_public_dict(self) -> None:
        cfg = AriaXConfig()
        engine = cfg.public_dict()["engine"]
        for gone in ("skills_dir", "prompts_dir", "planner_role", "worker_role",
                     "specialist_role", "reason_on_change"):
            self.assertNotIn(gone, engine)

    def test_doctor_reports_unknown_keys(self) -> None:
        import argparse
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from aria_x import cli

        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "cfg.toml"
        path.write_text(
            "[scope]\nauthorized = true\ntargets = [\"127.0.0.1\"]\n"
            "[llm]\nprovider = \"mock\"\n"
            "[engine]\nskills_dir = \"skills\"\n",
            encoding="utf-8",
        )
        args = argparse.Namespace(config=str(path), json=True, llm=False)
        with mock.patch("builtins.print") as printer:
            rc = cli.cmd_doctor(args)
        payload = json.loads(printer.call_args_list[0][0][0])
        entry = next(item for item in payload if item["check"] == "config-keys")
        self.assertTrue(entry["ok"])
        self.assertIn("engine.skills_dir", entry["warning"])
        self.assertIn(rc, (0, 1))

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

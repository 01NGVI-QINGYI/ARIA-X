"""Tests for the LLM layer: mocks, router JSON contracts and scenario scripts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aria_x.agents.prompts import PLANNER_SCHEMA
from aria_x.config import AriaXConfig, LLMConfig
from aria_x.errors import LLMError, ProtocolError
from aria_x.llm.base import ChatMessage
from aria_x.llm.mock import MockProvider
from aria_x.llm.router import ModelRouter
from aria_x.llm.scenario import ScenarioProvider, detect_role, load_scenario_script


def planner_messages(task_text: str = "# TASK (reason)\nPlan the next move.") -> list[ChatMessage]:
    return [ChatMessage.system("You are the PLANNER of ARIA-X — the sole intent generator."),
            ChatMessage.user(task_text)]


class MockProviderTests(unittest.TestCase):
    def test_tool_call_dict_and_final_text(self) -> None:
        provider = MockProvider([
            {"tool_calls": [{"name": "echo", "arguments": {"value": "1"}}]},
            "done",
        ])
        first = provider.chat([ChatMessage.user("hi")])
        self.assertEqual(first.content, "")
        self.assertTrue(first.has_tool_calls)
        self.assertEqual(first.tool_calls[0].name, "echo")
        self.assertEqual(first.tool_calls[0].arguments, {"value": "1"})
        self.assertEqual(provider.chat([ChatMessage.user("hi")]).content, "done")
        # Exhausted scripts repeat their last response.
        self.assertEqual(provider.chat([ChatMessage.user("hi")]).content, "done")

    def test_policy_mode(self) -> None:
        provider = MockProvider(policy=lambda messages, tools: "policy answer")
        self.assertEqual(provider.chat([ChatMessage.user("x")]).content, "policy answer")

    def test_list_shorthand_is_tool_calls(self) -> None:
        provider = MockProvider([[{"name": "echo", "arguments": {"a": 1}}]])
        response = provider.chat([ChatMessage.user("x")])
        self.assertEqual(response.tool_calls[0].name, "echo")


class RouterJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AriaXConfig()

    def test_valid_json_first_try(self) -> None:
        provider = MockProvider([{"content": '{"action": "noop", "reasoning": "nothing"}'}])
        router = ModelRouter(self.config, provider=provider)
        data, response = router.json("planner", planner_messages(), schema=PLANNER_SCHEMA)
        self.assertEqual(data["action"], "noop")
        self.assertEqual(response.model, "mock-1")
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(provider.calls[0]["json_mode"])

    def test_retry_on_invalid_json(self) -> None:
        provider = MockProvider(["not json at all",
                                 '{"action": "noop", "reasoning": "fixed"}'])
        router = ModelRouter(self.config, provider=provider)
        data, _ = router.json("planner", planner_messages(), schema=PLANNER_SCHEMA)
        self.assertEqual(data["reasoning"], "fixed")
        self.assertEqual(len(provider.calls), 2)
        rendered = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        self.assertIn("not valid JSON", rendered)

    def test_retry_on_schema_violation(self) -> None:
        provider = MockProvider(['{"reasoning": "missing action"}',
                                 '{"action": "noop", "reasoning": "fixed"}'])
        router = ModelRouter(self.config, provider=provider)
        data, _ = router.json("planner", planner_messages(), schema=PLANNER_SCHEMA)
        self.assertEqual(data["action"], "noop")
        rendered = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        self.assertIn("rejected by the contract validator", rendered)

    def test_exhausted_retries_raise_protocol_error(self) -> None:
        provider = MockProvider(["never json"])
        router = ModelRouter(self.config, provider=provider)
        with self.assertRaises(ProtocolError):
            router.json("planner", planner_messages(), schema=PLANNER_SCHEMA, retries=1)


class ScenarioProviderTests(unittest.TestCase):
    def test_role_queue_then_default(self) -> None:
        provider = ScenarioProvider({
            "roles": {"planner": [{"json": {"action": "noop", "reasoning": "scripted"}}]},
            "default": "fallback",
        })
        first = provider.chat(planner_messages())
        self.assertEqual(json.loads(first.content)["action"], "noop")
        self.assertEqual(provider.chat(planner_messages()).content, "fallback")
        self.assertEqual(provider.calls[0]["role"], "planner")

    def test_loop_cycles_role_queue(self) -> None:
        provider = ScenarioProvider({"roles": {"planner": ["a", "b"]}, "loop": True})
        self.assertEqual([provider.chat(planner_messages()).content for _ in range(3)],
                         ["a", "b", "a"])

    def test_match_rule_fires_once_then_default(self) -> None:
        provider = ScenarioProvider({
            "responses": [{
                "match": "scan",
                "tool_calls": [{"name": "terminal_exec", "arguments": {"command": "id"}}],
            }],
            "default": "done",
        })
        messages = [ChatMessage.user("please scan the target")]
        first = provider.chat(messages)
        self.assertEqual(first.tool_calls[0].name, "terminal_exec")
        self.assertEqual(first.tool_calls[0].arguments, {"command": "id"})
        self.assertEqual(provider.chat(messages).content, "done")

    def test_tool_arguments_json_string_parsed(self) -> None:
        provider = ScenarioProvider({"default": {"tool_calls": [
            {"name": "http_request", "arguments": '{"url": "http://127.0.0.1/"}'}]}})
        response = provider.chat([ChatMessage.user("go")])
        self.assertEqual(response.tool_calls[0].arguments, {"url": "http://127.0.0.1/"})

    def test_default_list_sequence_repeats_last(self) -> None:
        provider = ScenarioProvider({"default": ["one", "two"]})
        self.assertEqual([provider.chat([ChatMessage.user("x")]).content for _ in range(3)],
                         ["one", "two", "two"])

    def test_from_config_file(self) -> None:
        payload = {"default": "from file", "roles": {"worker": ["w1"]}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            json.dump(payload, handle)
            path = handle.name
        try:
            provider = ScenarioProvider.from_config(LLMConfig(provider="scenario", scenario=path))
            self.assertEqual(provider.chat([ChatMessage.user("# TASK (explore)\nx")]).content, "w1")
            self.assertEqual(provider.chat([ChatMessage.user("# TASK (explore)\nx")]).content,
                             "from file")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_from_config_inline_json_and_missing_file(self) -> None:
        provider = ScenarioProvider.from_config(
            LLMConfig(provider="scenario", scenario='{"default": "inline"}'))
        self.assertEqual(provider.chat([ChatMessage.user("x")]).content, "inline")
        with self.assertRaises(LLMError):
            ScenarioProvider.from_config(LLMConfig(provider="scenario",
                                                   scenario="/nonexistent/aria_x_scenario.json"))

    def test_policy_injection_three_and_two_args(self) -> None:
        three = ScenarioProvider({}, policy=lambda role, messages, tools: {"content": f"role={role}"})
        self.assertEqual(three.chat(planner_messages()).content, "role=planner")
        two = ScenarioProvider({}, policy=lambda messages, tools: "two-arg")
        self.assertEqual(two.chat(planner_messages()).content, "two-arg")

    def test_policy_none_falls_back_to_script(self) -> None:
        provider = ScenarioProvider({"default": "scripted"}, policy=lambda *args: None)
        self.assertEqual(provider.chat([ChatMessage.user("x")]).content, "scripted")

    def test_reset_rewinds(self) -> None:
        provider = ScenarioProvider({"roles": {"planner": ["a", "b"]}})
        self.assertEqual(provider.chat(planner_messages()).content, "a")
        provider.reset()
        self.assertEqual(provider.chat(planner_messages()).content, "a")
        self.assertEqual(provider.chat(planner_messages()).content, "b")

    def test_empty_script_returns_sentinel(self) -> None:
        provider = ScenarioProvider({})
        self.assertIn("SCENARIO", provider.chat([ChatMessage.user("x")]).content)

    def test_bad_script_types_raise(self) -> None:
        with self.assertRaises(LLMError):
            ScenarioProvider([])  # type: ignore[arg-type]
        with self.assertRaises(LLMError):
            ScenarioProvider({"roles": []})
        with self.assertRaises(LLMError):
            load_scenario_script("{not json")

    def test_tool_call_ids_are_deterministic(self) -> None:
        provider = ScenarioProvider({"default": {"tool_calls": [{"name": "echo"}]}})
        first = provider.chat([ChatMessage.user("x")])
        second = provider.chat([ChatMessage.user("x")])
        self.assertEqual(first.tool_calls[0].id, "scn_1_0")
        self.assertEqual(second.tool_calls[0].id, "scn_2_0")


class RoleDetectionTests(unittest.TestCase):
    def test_task_headers(self) -> None:
        cases = {
            "# TASK (reason)\nplan": "planner",
            "# TASK (explore)\nwork": "worker",
            "# TASK (bootstrap)\ngo": "bootstrap",
            "# TASK (report)\nwrite": "reporter",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_role([ChatMessage.user(text)]), expected)

    def test_system_prompt_fallback(self) -> None:
        cases = {
            "You are the PLANNER of ARIA-X": "planner",
            "You are the BOOTSTRAP agent of ARIA-X": "bootstrap",
            "You are an EXPLORER (worker) in ARIA-X": "worker",
            "You are the REPORTING agent of ARIA-X": "reporter",
        }
        for system, expected in cases.items():
            with self.subTest(system=system):
                self.assertEqual(detect_role([ChatMessage.system(system), ChatMessage.user("x")]),
                                 expected)

    def test_specialist_name_detection(self) -> None:
        messages = [
            ChatMessage.system("You are the researcher specialist in ARIA-X."),
            ChatMessage.user("# SPECIALIST ASSIGNMENT (researcher)\nquestion"),
        ]
        self.assertEqual(detect_role(messages), "researcher")
        provider = ScenarioProvider({"roles": {"researcher": ["r"], "specialist": ["s"]},
                                     "default": "d"})
        self.assertEqual(provider.chat(messages).content, "r")

    def test_generic_specialist_queue(self) -> None:
        messages = [ChatMessage.system("You are the adviser specialist in ARIA-X."),
                    ChatMessage.user("# SPECIALIST ASSIGNMENT (adviser)\nq")]
        provider = ScenarioProvider({"roles": {"specialist": ["generic"]}})
        self.assertEqual(provider.chat(messages).content, "generic")


if __name__ == "__main__":
    unittest.main()

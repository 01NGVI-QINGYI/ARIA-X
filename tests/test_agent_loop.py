"""Tests for the agent loop, role composition and the worker entry points."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from aria_x.agents import roles, workers
from aria_x.agents.base import Agent, AgentOutcome, AgentSpec, AgentTask
from aria_x.agents.prompts import PLANNER_SCHEMA, WORKER_SCHEMA
from aria_x.config import AriaXConfig
from aria_x.errors import ProtocolError
from aria_x.llm.mock import MockProvider
from aria_x.llm.router import ModelRouter
from aria_x.llm.scenario import ScenarioProvider
from aria_x.models import Project
from aria_x.tools.base import ToolContext, ToolRegistry, ToolResult, ToolSpec

WORKER_PAYLOAD = {
    "fact": {"title": "Demo fact", "body": "Observed via echo", "confidence": "high"},
    "findings": [{"title": "Demo finding", "severity": "medium", "body": "evidence"}],
    "assets": [{"kind": "host", "value": "127.0.0.1"}],
    "goal_proven": False,
    "summary": "one probe done",
}

PLANNER_PAYLOAD = {
    "action": "intents",
    "reasoning": "Probe the only known endpoint.",
    "intents": [{"description": f"probe {i}"} for i in range(5)],
    "todos": [{"title": "fingerprint", "status": "active"}],
}


class _FakeStore:
    def __init__(self) -> None:
        self.traces: list[object] = []

    def add_trace(self, trace: object) -> None:
        self.traces.append(trace)


class _FakeEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, type_: str, **kwargs: object) -> None:
        self.events.append((type_, kwargs))


class _FakeScope:
    def summary(self) -> dict[str, object]:
        return {"authorized": True, "targets": ["127.0.0.1"], "deny": [],
                "networks": [], "max_rps": 3.0}


def make_ctx(language: str = "en") -> ToolContext:
    config = AriaXConfig()
    config.engine.language = language
    config.scope.authorized = True
    config.scope.targets = ["127.0.0.1"]
    return ToolContext(
        project=Project.new("test project", "prove the goal"),
        run_id="run_test",
        task_id="task_test",
        agent="worker",
        role="worker",
        store=_FakeStore(),          # type: ignore[arg-type]
        config=config,
        events=_FakeEvents(),        # type: ignore[arg-type]
        scope=_FakeScope(),          # type: ignore[arg-type]
        sandbox=object(),            # type: ignore[arg-type]
        workdir=Path("."),
    )


def make_registry(calls: list[tuple[str, dict[str, object]]]) -> ToolRegistry:
    registry = ToolRegistry()

    def echo(ctx: ToolContext, kwargs: dict[str, object]) -> ToolResult:
        calls.append(("echo", kwargs))
        return ToolResult.success({"echo": kwargs.get("value")}, text="ok")

    registry.add(ToolSpec(
        name="echo", description="Echo a value.",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        read_only=True,
    ), echo)

    def boom(ctx: ToolContext, kwargs: dict[str, object]) -> ToolResult:
        return ToolResult.failure("kaboom")

    registry.add(ToolSpec(name="boom", description="Always fails.",
                          parameters={"type": "object", "properties": {}}), boom)
    return registry


def make_router(responses: list[object] | None = None, *, policy: object | None = None,
                config: AriaXConfig | None = None) -> tuple[ModelRouter, MockProvider]:
    provider = MockProvider(responses or [], policy=policy)  # type: ignore[arg-type]
    router = ModelRouter(config or AriaXConfig(), provider=provider)
    return router, provider


def worker_spec(**overrides: object) -> AgentSpec:
    base = dict(name="worker", role="worker", system_prompt="test",
                tools=["echo", "boom"], max_steps=4, json_contract=WORKER_SCHEMA)
    base.update(overrides)
    return AgentSpec(**base)  # type: ignore[arg-type]


class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.registry = make_registry(self.calls)

    # -- the loop ------------------------------------------------------

    def test_tool_call_then_json_contract(self) -> None:
        router, provider = make_router([
            {"tool_calls": [{"name": "echo", "arguments": {"value": "hi"}}]},
            {"content": json.dumps(WORKER_PAYLOAD)},
        ])
        outcome = Agent(worker_spec(), router, self.registry, self.ctx).run(
            AgentTask(task_id="t1", kind="explore", prompt="probe"))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.data["fact"]["title"], "Demo fact")
        self.assertEqual(outcome.steps, 2)
        self.assertEqual(outcome.tool_calls, 1)
        self.assertEqual(self.calls, [("echo", {"value": "hi"})])
        self.assertEqual(len(provider.calls), 2)
        # The second model call must contain the tool observation.
        rendered = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        self.assertIn("echo", rendered)
        kinds = [t.kind for t in self.ctx.store.traces]  # type: ignore[attr-defined]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)

    def test_tool_error_is_fed_back_and_loop_continues(self) -> None:
        router, provider = make_router([
            {"tool_calls": [{"name": "boom", "arguments": {}}]},
            {"content": json.dumps(WORKER_PAYLOAD)},
        ])
        outcome = Agent(worker_spec(), router, self.registry, self.ctx).run(
            AgentTask(task_id="t1", kind="explore", prompt="probe"))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.steps, 2)
        rendered = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        self.assertIn("ERROR", rendered)
        self.assertIn("kaboom", rendered)

    def test_unknown_tool_error_is_fed_back(self) -> None:
        router, provider = make_router([
            {"tool_calls": [{"name": "does_not_exist", "arguments": {}}]},
            {"content": json.dumps(WORKER_PAYLOAD)},
        ])
        outcome = Agent(worker_spec(), router, self.registry, self.ctx).run(
            AgentTask(task_id="t1", kind="explore", prompt="probe"))
        self.assertTrue(outcome.ok, outcome.error)
        rendered = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        self.assertIn("unknown tool", rendered)

    def test_json_contract_retry(self) -> None:
        router, provider = make_router([
            {"content": "I think we are done."},
            {"content": json.dumps(WORKER_PAYLOAD)},
        ])
        outcome = Agent(worker_spec(), router, self.registry, self.ctx).run(
            AgentTask(task_id="t1", kind="explore", prompt="probe"))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.steps, 2)
        self.assertEqual(len(provider.calls), 2)
        rendered = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
        self.assertIn("JSON contract", rendered)

    def test_contract_failure_at_ceiling_raises_protocol_error(self) -> None:
        # The Agent layer raises ProtocolError (it is an AriaXError, not an
        # AgentError); the worker entry points convert it into an outcome.
        router, _ = make_router([{"content": "prose"}, {"content": "still prose"}])
        with self.assertRaises(ProtocolError):
            Agent(worker_spec(max_steps=2), router, self.registry, self.ctx).run(
                AgentTask(task_id="t1", kind="explore", prompt="probe"))

    def test_max_steps_wrapup_returns_final_contract(self) -> None:
        tool_call = {"tool_calls": [{"name": "echo", "arguments": {"value": "x"}}]}
        router, provider = make_router([tool_call, tool_call, {"content": json.dumps(WORKER_PAYLOAD)}])
        outcome = Agent(worker_spec(max_steps=2), router, self.registry, self.ctx).run(
            AgentTask(task_id="t1", kind="explore", prompt="probe"))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.steps, 2)
        self.assertEqual(outcome.finish_reason, "max_steps")
        self.assertEqual(len(provider.calls), 3)      # two loop calls + wrap-up
        self.assertEqual(provider.calls[2]["tools"], [])

    def test_prompt_mode_tool_call_is_recovered(self) -> None:
        content = '```json\n{"tool": "echo", "arguments": {"value": "p"}}\n```'
        router, provider = make_router([
            {"content": content},
            {"content": json.dumps(WORKER_PAYLOAD)},
        ])
        outcome = Agent(worker_spec(), router, self.registry, self.ctx).run(
            AgentTask(task_id="t1", kind="explore", prompt="probe"))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.steps, 2)
        self.assertEqual(self.calls, [("echo", {"value": "p"})])


class RoleCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()

    def test_planner_is_read_only(self) -> None:
        spec = roles.planner_spec(self.ctx, 3)
        self.assertEqual(spec.role, "planner")
        self.assertIs(spec.json_contract, PLANNER_SCHEMA)
        self.assertEqual(set(spec.tools), set(roles.PLANNER_TOOLS))
        self.assertNotIn("terminal_exec", spec.tools)
        self.assertNotIn("add_fact", spec.tools)
        self.assertIn("todo_update", spec.tools)
        self.assertLessEqual(spec.max_steps, 6)

    def test_worker_can_write_graph_and_delegate(self) -> None:
        spec = roles.worker_spec(self.ctx)
        self.assertIs(spec.json_contract, WORKER_SCHEMA)
        for name in ("terminal_exec", "http_request", "add_fact", "add_finding",
                     "prove_goal", "delegate_specialist", "memory_search"):
            self.assertIn(name, spec.tools)

    def test_bootstrap_uses_bootstrap_prompt_and_worker_contract(self) -> None:
        spec = roles.bootstrap_spec(self.ctx)
        self.assertEqual(spec.name, "bootstrap")
        self.assertIn("BOOTSTRAP agent", spec.system_prompt)
        self.assertIs(spec.json_contract, WORKER_SCHEMA)

    def test_specialist_toolsets(self) -> None:
        expected = {
            "researcher": {"http_request", "web_search", "web_probe", "file_read"},
            "coder": {"terminal_exec", "file_read", "file_write", "file_list"},
            "adviser": {"graph_query", "search_traces", "memory_search"},
            "memorist": {"graph_query", "search_traces", "memory_search"},
        }
        for name, tools in expected.items():
            spec = roles.specialist_spec(self.ctx, name)
            self.assertEqual(spec.role, "specialist")
            self.assertEqual(set(spec.tools), tools)
            self.assertIsNone(spec.json_contract)
        self.assertEqual(roles.resolve_specialist("advisor"), "adviser")
        self.assertEqual(roles.specialist_spec(self.ctx, "advisor").name, "specialist:adviser")

    def test_reporter_has_no_tools(self) -> None:
        spec = roles.reporter_spec(self.ctx)
        self.assertEqual(spec.tools, [])
        self.assertIsNone(spec.json_contract)
        self.assertIn("REPORTING agent", spec.system_prompt)

    def test_prompt_injects_scope_workdir_and_language(self) -> None:
        spec = roles.worker_spec(self.ctx)
        self.assertIn("ARIA-X OPERATING DOCTRINE", spec.system_prompt)
        self.assertIn("127.0.0.1", spec.system_prompt)
        self.assertIn("Write the engagement narrative in English", spec.system_prompt)
        zh = roles.worker_spec(make_ctx(language="zh"))
        self.assertIn("Simplified Chinese", zh.system_prompt)
        self.assertEqual(spec.max_steps, min(12, self.ctx.config.budget.max_steps_per_task))


class WorkerFunctionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.registry = make_registry(self.calls)

    def test_run_planner_normalizes_and_bounds_intents(self) -> None:
        router, provider = make_router([{"content": json.dumps(PLANNER_PAYLOAD)}])
        plan = workers.run_planner(self.ctx, router, self.registry, "goal", "ctx", 3)
        self.assertEqual(set(plan), set(workers.PLANNER_KEYS))
        self.assertEqual(plan["action"], "intents")
        self.assertEqual(len(plan["intents"]), 3)
        self.assertEqual(set(plan["intents"][0]),
                         {"description", "from", "asset_ids", "priority", "todo_id"})
        self.assertEqual(plan["todos"][0]["title"], "fingerprint")
        self.assertEqual(plan["complete"], {})

    def test_run_planner_downgrades_empty_intents_and_missing_summary(self) -> None:
        empty = workers.normalize_plan({"action": "intents", "intents": []}, 3)
        self.assertEqual(empty["action"], "noop")
        no_summary = workers.normalize_plan({"action": "complete", "complete": {"proof": ["x"]}}, 3)
        self.assertEqual(no_summary["action"], "noop")

    def test_run_planner_never_raises_on_provider_failure(self) -> None:
        def explode(messages: object, tools: object) -> None:
            raise RuntimeError("provider down")

        router, _ = make_router(policy=explode)
        plan = workers.run_planner(self.ctx, router, self.registry, "goal", "ctx", 3)
        self.assertEqual(plan["action"], "noop")
        self.assertIn("planner unavailable", plan["reasoning"])

    def test_run_worker_and_bootstrap_return_contract_data(self) -> None:
        payload = json.dumps(WORKER_PAYLOAD)
        router, _ = make_router([{"content": payload}, {"content": payload}])
        worker = workers.run_worker(self.ctx, router, self.registry, "probe 127.0.0.1", "ctx", "goal")
        self.assertIsInstance(worker, AgentOutcome)
        self.assertTrue(worker.ok, worker.error)
        self.assertEqual(worker.data["fact"]["title"], "Demo fact")
        bootstrap = workers.run_bootstrap(self.ctx, router, self.registry, "goal", "ctx")
        self.assertTrue(bootstrap.ok, bootstrap.error)
        self.assertEqual(bootstrap.data["goal_proven"], False)

    def test_run_specialist_returns_text(self) -> None:
        router, _ = make_router([{"content": "Try the /files endpoint with ../ traversal."}])
        outcome = workers.run_specialist(self.ctx, router, self.registry,
                                         "researcher", "where to look?", "facts here")
        self.assertIsInstance(outcome, AgentOutcome)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertIn("/files", outcome.text)

    def test_run_reporter_returns_markdown_and_empty_on_failure(self) -> None:
        router, _ = make_router([{"content": "# Report\n\n- one finding"}])
        self.assertEqual(workers.run_reporter(self.ctx, router, self.registry, "data"),
                         "# Report\n\n- one finding")

        def explode(messages: object, tools: object) -> None:
            raise RuntimeError("provider down")

        broken, _ = make_router(policy=explode)
        self.assertEqual(workers.run_reporter(self.ctx, broken, self.registry, "data"), "")

    def test_worker_contract_failure_returns_outcome_not_exception(self) -> None:
        router, _ = make_router(policy=lambda messages, tools: "prose only")
        outcome = workers.run_worker(self.ctx, router, self.registry, "probe", "ctx", "goal")
        self.assertFalse(outcome.ok)
        self.assertIn("contract", outcome.error)

    def test_worker_failure_returns_outcome_not_exception(self) -> None:
        def explode(messages: object, tools: object) -> None:
            raise RuntimeError("provider down")

        router, _ = make_router(policy=explode)
        outcome = workers.run_worker(self.ctx, router, self.registry, "probe", "ctx", "goal")
        self.assertFalse(outcome.ok)
        self.assertIn("provider down", outcome.error)

    def test_scenario_provider_drives_worker_tool_calls(self) -> None:
        scenario = ScenarioProvider({
            "roles": {
                "worker": [
                    {"tool_calls": [{"name": "echo", "arguments": {"value": "scenario"}}]},
                    {"json": WORKER_PAYLOAD},
                ],
            },
            "default": "SCENARIO FALLBACK",
        })
        router = ModelRouter(self.ctx.config, provider=scenario)
        outcome = workers.run_worker(self.ctx, router, self.registry, "probe", "ctx", "goal")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.data["fact"]["title"], "Demo fact")
        self.assertEqual(self.calls, [("echo", {"value": "scenario"})])
        self.assertEqual([c["role"] for c in scenario.calls], ["worker", "worker"])


class WorkerTaskShapeTests(unittest.TestCase):
    def test_agent_task_kinds_reach_the_provider(self) -> None:
        ctx = make_ctx()
        registry = make_registry([])
        router, provider = make_router([{"content": json.dumps(WORKER_PAYLOAD)}])
        workers.run_worker(ctx, router, registry, "probe", "ctx", "goal")
        rendered = json.dumps(provider.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("# TASK (explore)", rendered)
        self.assertIn("## ENGAGEMENT GOAL", rendered)
        self.assertIn("probe", rendered)

    def test_agent_uses_ctx_task_id_for_traces(self) -> None:
        ctx = make_ctx()
        registry = make_registry([])
        router, _ = make_router([{"content": json.dumps(WORKER_PAYLOAD)}])
        workers.run_worker(ctx, router, registry, "probe", "ctx", "goal")
        traces = ctx.store.traces  # type: ignore[attr-defined]
        self.assertTrue(traces)
        self.assertTrue(all(t.task_id == "task_test" for t in traces))


if __name__ == "__main__":
    unittest.main()

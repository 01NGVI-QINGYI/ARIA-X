"""End-to-end engine tests with scripted workers and a mock provider.

The engine is deliberately testable without an LLM: ``aria_x.agents.workers`` is
imported lazily, so a fake module can be injected for the loop tests while a
separate integration test drives the real workers with ``MockProvider``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from aria_x.agents.base import AgentOutcome
from aria_x.config import AriaXConfig
from aria_x.engine.budget import Budget
from aria_x.engine.context import DEFAULT_CONTEXT_CHARS, render_context
from aria_x.engine.engagement import Engagement, EngagementResult
from aria_x.engine.orchestrator import Orchestrator, _fallback_report
from aria_x.engine.scheduler import TaskOutcome, TaskScheduler
from aria_x.errors import ConfigError
from aria_x.events import EventBus, wait_for
from aria_x.llm.mock import MockProvider
from aria_x.models import Node, Project, Run, Task, Todo, Trace
from aria_x.store import Store

INTENTS_PLAN: dict[str, Any] = {
    "action": "intents",
    "reasoning": "The surface is unknown; probe HTTP and TLS first.",
    "todos": [{"title": "Enumerate mock surface", "status": "active"}],
    "intents": [
        {"description": "Fetch the HTTP banner from 127.0.0.1", "priority": 1},
        {"description": "Probe the TLS certificate on 127.0.0.1", "priority": 2},
    ],
}

COMPLETE_PLAN: dict[str, Any] = {
    "action": "complete",
    "reasoning": "Both probes produced evidence.",
    "todos": [{"title": "Enumerate mock surface", "status": "done"}],
    "complete": {"summary": "Goal proven by mock evidence", "proof": ["mock"]},
}


def worker_payload(intent_text: str) -> dict[str, Any]:
    return {
        "fact": {"title": f"Observed: {intent_text}", "body": "mock evidence body",
                 "confidence": "high"},
        "findings": [{
            "title": f"Finding for {intent_text}",
            "severity": "medium",
            "body": "Reproduced against the mock target.",
            "asset_values": ["127.0.0.1"],
            "evidence": "mock output line",
        }],
        "assets": [{"kind": "service", "value": "127.0.0.1:8080",
                    "parent_value": "127.0.0.1"}],
        "goal_proven": False,
        "summary": "probe complete",
    }


class WorkerScript:
    """A fake ``aria_x.agents.workers`` module with observable calls."""

    def __init__(self, planner_actions: list[Any],
                 worker: Callable[..., dict[str, Any]] | None = None,
                 bootstrap: dict[str, Any] | None = None,
                 worker_delay: float = 0.0,
                 worker_takes_ctx: bool = False) -> None:
        self.planner_actions = list(planner_actions)
        self._worker = worker or worker_payload
        self._bootstrap = bootstrap
        self.worker_delay = worker_delay
        self.worker_takes_ctx = worker_takes_ctx
        self.planner_calls = 0
        self.worker_intents: list[str] = []
        self.bootstrap_calls = 0
        self.specialist_calls: list[str] = []

    def run_planner(self, ctx: Any, router: Any, registry: Any, goal: str,
                    context: str, max_intents: int) -> dict[str, Any]:
        index = min(self.planner_calls, len(self.planner_actions) - 1)
        self.planner_calls += 1
        action = self.planner_actions[index]
        return action(self, context) if callable(action) else json.loads(json.dumps(action))

    def run_worker(self, ctx: Any, router: Any, registry: Any, intent_text: str,
                   context: str, goal: str) -> AgentOutcome:
        self.worker_intents.append(intent_text)
        if self.worker_delay:
            time.sleep(self.worker_delay)
        data = self._worker(ctx, intent_text) if self.worker_takes_ctx else self._worker(intent_text)
        return AgentOutcome(ok=True, text=json.dumps(data), data=data)

    def run_bootstrap(self, ctx: Any, router: Any, registry: Any, goal: str,
                      context: str) -> AgentOutcome:
        self.bootstrap_calls += 1
        data = self._bootstrap
        if data is None:
            return AgentOutcome(ok=False, error="no bootstrap script")
        return AgentOutcome(ok=True, text=json.dumps(data), data=data)

    def run_specialist(self, ctx: Any, router: Any, registry: Any, name: str,
                       question: str, context: str) -> AgentOutcome:
        self.specialist_calls.append(name)
        return AgentOutcome(ok=True, text=f"specialist {name} answer")


@contextmanager
def fake_workers(script: WorkerScript):
    module = types.ModuleType("aria_x.agents.workers")
    module.run_planner = script.run_planner
    module.run_worker = script.run_worker
    module.run_bootstrap = script.run_bootstrap
    module.run_specialist = script.run_specialist
    with mock.patch.dict(sys.modules, {"aria_x.agents.workers": module}):
        yield module


BOOTSTRAP_PAYLOAD: dict[str, Any] = {
    "fact": {"title": "Bootstrap observed a mock service", "body": "mock banner",
             "confidence": "high"},
    "findings": [],
    "assets": [{"kind": "host", "value": "127.0.0.1"}],
    "goal_proven": False,
    "summary": "bootstrap finished",
}


class EngineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="aria-x-engine-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # -- helpers --------------------------------------------------------

    def make_config(self, *, authorized: bool = True, targets: tuple[str, ...] = ("127.0.0.1",),
                    bootstrap: bool = True, concurrency: int = 2,
                    max_total_intents: int = 10, max_seconds: int = 20) -> AriaXConfig:
        config = AriaXConfig(name="engine-test", data_dir=str(self.tmp / "data"),
                            runs_dir=str(self.tmp / "runs"))
        config.scope.authorized = authorized
        config.scope.targets = list(targets)
        config.scope.max_rps = 1000
        config.sandbox.backend = "mock"
        config.sandbox.timeout = 5
        config.budget.max_seconds = max_seconds
        config.budget.max_llm_calls = 40
        config.budget.max_tokens = 200_000
        config.budget.max_tool_calls = 40
        config.budget.max_concurrency = concurrency
        config.budget.max_total_intents = max_total_intents
        config.budget.max_intents_per_reason = 3
        config.budget.max_steps_per_task = 6
        config.engine.bootstrap = bootstrap
        config.engine.max_idle_rounds = 2
        config.engine.retry_failed_tasks = 0
        config.engine.language = "en"
        return config

    def make_engagement(self, config: AriaXConfig, provider: Any = None
                        ) -> tuple[Engagement, Store, EventBus]:
        store = Store(":memory:")
        events = EventBus(replay=3000)
        engagement = Engagement(config, store=store, events=events,
                                provider=provider or MockProvider())
        self.addCleanup(store.close)
        self.addCleanup(events.close)
        self.addCleanup(engagement.close)
        return engagement, store, events

class EngineTests(EngineTestBase):
    # -- tests ----------------------------------------------------------

    def test_full_engagement_persists_graph_and_report(self) -> None:
        config = self.make_config()
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Mock engagement", "Prove the mock target is reachable")
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=BOOTSTRAP_PAYLOAD)

        with fake_workers(script):
            result = engagement.run(project.id)

        self.assertIsInstance(result, EngagementResult)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "Goal proven by mock evidence")
        self.assertTrue(result.report_path)
        self.assertTrue(Path(result.report_path).is_file())
        self.assertTrue(Path(result.report_path).with_suffix(".html").is_file())
        self.assertTrue(Path(result.report_path).read_text(encoding="utf-8"))
        self.assertEqual(result.metrics["findings"], 2)
        self.assertGreaterEqual(result.metrics["facts"], 3)

        counts = store.counts(project.id)
        self.assertGreaterEqual(counts["nodes"].get("fact", 0), 3)
        self.assertEqual(counts["nodes"].get("finding", 0), 2)
        self.assertEqual(counts["nodes"].get("intent", 0), 2)
        self.assertEqual(counts["assets"].get("host", 0), 1)
        self.assertEqual(counts["assets"].get("service", 0), 1)
        self.assertGreaterEqual(counts["edges"], 2)

        findings = store.list_nodes(project.id, kind="finding")
        self.assertEqual(len(findings), 2)
        for finding in findings:
            self.assertTrue(store.anchors_for_node(finding.id), finding.title)

        intents = store.list_nodes(project.id, kind="intent")
        self.assertTrue(all(node.status == "done" for node in intents))
        self.assertEqual(len({node.title for node in intents}), 2)

        derived = [edge for edge in store.list_edges(project.id) if edge.rel == "derived_from"]
        self.assertGreaterEqual(len(derived), 2)
        yields = [edge for edge in store.list_edges(project.id) if edge.rel == "yields"]
        self.assertGreaterEqual(len(yields), 2)

        todos = store.list_todos(project.id)
        self.assertTrue(todos)
        self.assertEqual(todos[0].status, "done")

        run = store.get_run(result.run_id)
        self.assertEqual(run.status, "completed")
        self.assertTrue(run.ended_at)
        self.assertIn("llm_calls", run.usage)
        self.assertIn("total_tokens", run.usage)
        self.assertIn("limits", run.budget)
        self.assertIn("usage", run.budget)

        tasks = store.list_tasks(result.run_id)
        types = [task.type for task in tasks]
        for expected in ("bootstrap", "reason", "explore", "report"):
            self.assertIn(expected, types)
        explore = [task for task in tasks if task.type == "explore"]
        self.assertTrue(all(task.status == "done" for task in explore))
        report_tasks = [task for task in tasks if task.type == "report"]
        self.assertEqual(len(report_tasks), 1)
        self.assertEqual(report_tasks[0].status, "done")

        seen_events = events.replay(3000)
        seen = {event.type for event in seen_events}
        required = {"run.started", "task.started", "task.finished", "node.created",
                    "asset.found", "finding.created", "todo.updated", "budget",
                    "report.ready", "run.finished"}
        self.assertTrue(required <= seen, required - seen)
        ready = [event for event in seen_events if event.type == "report.ready"]
        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0].data.get("markdown"))
        self.assertTrue(ready[0].data.get("paths", {}).get("markdown"))
        finished = [event for event in seen_events if event.type == "run.finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].data.get("status"), "completed")
        self.assertEqual(finished[0].data["metrics"]["findings"], 2)

        service = store.find_asset(project.id, "service", "127.0.0.1:8080")
        host = store.find_asset(project.id, "host", "127.0.0.1")
        self.assertIsNotNone(service)
        self.assertIsNotNone(host)
        assert service is not None and host is not None
        self.assertEqual(service.parent_id, host.id)

    def test_bootstrap_goal_proof_skips_planner_loop(self) -> None:
        config = self.make_config(bootstrap=True, concurrency=2)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Bootstrap proof", "Prove during bootstrap")
        proof_payload = {
            "fact": {"title": "Bootstrap demonstrated the goal", "body": "direct proof"},
            "findings": [{"title": "Bootstrap finding", "severity": "high", "body": "evidence"}],
            "assets": [{"kind": "host", "value": "127.0.0.1"}],
            "goal_proven": True,
            "summary": "Goal proven during bootstrap",
        }
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=proof_payload)
        with fake_workers(script):
            result = engagement.run(project.id)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.summary, "Goal proven during bootstrap")
        self.assertEqual(script.planner_calls, 0)
        self.assertEqual(script.worker_intents, [])
        goals = store.list_nodes(project.id, kind="goal")
        self.assertTrue(goals)
        self.assertEqual(goals[-1].status, "proven")
        self.assertEqual(result.metrics["findings"], 1)

    def test_llm_budget_ceiling_after_planner_still_reports_budget(self) -> None:
        config = self.make_config(bootstrap=False, concurrency=1)
        config.budget.max_llm_calls = 1
        provider = MockProvider([json.dumps(INTENTS_PLAN)])
        engagement, store, events = self.make_engagement(config, provider=provider)
        project = engagement.create_project("LLM budget", "Stop at the call ceiling")
        try:
            import aria_x.agents.workers  # noqa: F401
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"real workers unavailable: {exc}")
        result = engagement.run(project.id)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "budget")
        self.assertIn("LLM call budget", result.error)
        self.assertTrue(Path(result.report_path).is_file())
        self.assertEqual(store.get_run(result.run_id).status, "budget")

    def test_budget_exhaustion_stops_cleanly_with_report(self) -> None:
        config = self.make_config(bootstrap=False, concurrency=1, max_total_intents=1)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Budget engagement", "Trigger the intent cap")
        script = WorkerScript([INTENTS_PLAN], bootstrap=None)

        with fake_workers(script):
            result = engagement.run(project.id)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "budget")
        self.assertIn("intent budget", result.error)
        self.assertTrue(Path(result.report_path).is_file())
        self.assertEqual(store.get_run(result.run_id).status, "budget")
        self.assertEqual(len(store.list_nodes(project.id, kind="intent")), 1)
        self.assertEqual(result.metrics["intents_created"], 1)
        self.assertIn("budget", {event.type for event in events.replay(3000)})
        report = Path(result.report_path).read_text(encoding="utf-8")
        self.assertIn("Finding for", report)
        self.assertEqual(result.metrics["findings"], 1)

    def test_background_run_can_be_stopped(self) -> None:
        config = self.make_config(concurrency=1)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Cancellation engagement", "Stop me")
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=None,
                              worker_delay=0.4)

        with fake_workers(script):
            run_id = engagement.start_background(project.id)
            self.assertTrue(wait_for(
                lambda: any(task["type"] == "explore"
                            for task in engagement.status(run_id)["tasks"]), timeout=5.0))
            time.sleep(0.05)
            engagement.stop(run_id)
            result = engagement.wait(run_id, timeout=5.0)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(store.get_run(run_id).status, "cancelled")
        self.assertTrue(Path(result.report_path).is_file())
        status = engagement.status(run_id)
        self.assertEqual(status["status"], "cancelled")
        self.assertFalse(status["running"])
        self.assertIsNotNone(status["result"])

    def test_background_run_completes_and_exposes_status(self) -> None:
        config = self.make_config()
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Background engagement", "Finish in background")
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=BOOTSTRAP_PAYLOAD)

        with fake_workers(script):
            run_id = engagement.start_background(project.id, goal="Background goal override")
            result = engagement.wait(run_id, timeout=10.0)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.ok)
        self.assertEqual(store.get_project(project.id).goal, "Background goal override")
        goals = store.list_nodes(project.id, kind="goal")
        self.assertEqual(goals[-1].title, "Background goal override")
        status = engagement.status(run_id)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["result"]["status"], "completed")
        self.assertIn("nodes", status["counts"])
        self.assertTrue(status["report_path"])
        latest = engagement.status()
        self.assertEqual(latest["run_id"], run_id)
        engagement.stop(run_id)  # a settled run must not be cancelled retroactively
        settled = engagement.status(run_id)
        self.assertEqual(settled["status"], "completed")
        self.assertFalse(settled["stop_requested"])

    def test_authorization_required_before_run(self) -> None:
        config = self.make_config(authorized=False)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Unauthorized", "Must not run")
        with self.assertRaises(ConfigError):
            engagement.run(project.id)
        self.assertEqual(store.list_runs(project.id), [])

    def test_empty_targets_rejected(self) -> None:
        config = self.make_config(targets=())
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("No targets", "Must not run")
        with self.assertRaises(ConfigError):
            engagement.start_background(project.id)
        self.assertEqual(store.list_runs(project.id), [])

    def test_report_failure_falls_back_without_failing_run(self) -> None:
        config = self.make_config()
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Fallback report", "Report renderer fails")
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=None)

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("renderer exploded")

        with fake_workers(script), \
                mock.patch("aria_x.engine.orchestrator._load_write_report", return_value=explode):
            result = engagement.run(project.id)

        self.assertTrue(result.ok, result.error)
        self.assertTrue(Path(result.report_path).is_file())
        self.assertIn("report_error", result.metrics)
        self.assertIn("renderer exploded", result.metrics["report_error"])

    def test_missing_report_renderer_uses_fallback(self) -> None:
        config = self.make_config()
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("No renderer", "Fallback writer")
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=None)
        with fake_workers(script), \
                mock.patch("aria_x.engine.orchestrator._load_write_report", return_value=None):
            result = engagement.run(project.id)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(Path(result.report_path).is_file())
        markdown = Path(result.report_path).read_text(encoding="utf-8")
        self.assertIn("ARIA-X Engagement Report", markdown)
        self.assertNotIn("report_error", result.metrics)

    def test_real_workers_with_mock_provider(self) -> None:
        try:
            import aria_x.agents.workers  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on parallel owner
            self.skipTest(f"real workers unavailable: {exc}")
        config = self.make_config(bootstrap=False, concurrency=1)
        responses = [
            json.dumps(INTENTS_PLAN),
            json.dumps({
                "fact": {"title": "Mock homepage returned HTTP 200",
                         "body": "GET http://mock.test/ -> 200 OK", "confidence": "high"},
                "findings": [{"title": "Missing security headers", "severity": "low",
                              "body": "No CSP/HSTS in the mock response.",
                              "asset_values": ["mock.test"]}],
                "assets": [{"kind": "host", "value": "mock.test"}],
                "goal_proven": False,
                "summary": "homepage verified",
            }),
            json.dumps(COMPLETE_PLAN),
        ]
        provider = MockProvider(responses)
        engagement, store, events = self.make_engagement(config, provider=provider)
        project = engagement.create_project("Real workers", "Prove the mock homepage")
        result = engagement.run(project.id)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, "completed")
        self.assertGreaterEqual(store.counts(project.id)["nodes"].get("fact", 0), 1)
        findings = store.list_nodes(project.id, kind="finding")
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(store.anchors_for_node(findings[0].id)), 1)
        self.assertTrue(Path(result.report_path).is_file())

    def test_result_to_dict_shape(self) -> None:
        config = self.make_config()
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Shape", "Check to_dict")
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=None)
        with fake_workers(script):
            result = engagement.run(project.id)
        payload = result.to_dict()
        for key in ("ok", "project_id", "run_id", "status", "summary", "report_path",
                    "error", "metrics"):
            self.assertIn(key, payload)
        self.assertIn("findings", payload["metrics"])
        status = engagement.status(result.run_id)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["result"]["run_id"], result.run_id)
        self.assertFalse(status["running"])
        engagement.close()
        engagement.close()  # idempotent


class EdgeCaseTests(EngineTestBase):
    """Hint consumption, tool-call cancellation, reruns and duplicate intents."""

    def test_hints_are_consumed_once_and_midrun_hints_are_seen(self) -> None:
        config = self.make_config(bootstrap=False, concurrency=1)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Hint engagement", "Use human hints")
        initial_hint = Node.new(project.id, "hint", "Initial hint", "start with HTTP", status="open")
        store.add_node(initial_hint)
        contexts: list[str] = []
        late_hint: dict[str, str] = {}

        def planner(script: WorkerScript, context: str) -> dict[str, Any]:
            contexts.append(context)
            if len(contexts) == 1:
                hint = Node.new(project.id, "hint", "Mid-run hint", "check the debug endpoint",
                                status="open")
                store.add_node(hint)
                late_hint["id"] = hint.id
                return json.loads(json.dumps(INTENTS_PLAN))
            return json.loads(json.dumps(COMPLETE_PLAN))

        script = WorkerScript([planner], bootstrap=None)
        with fake_workers(script):
            result = engagement.run(project.id)

        self.assertTrue(result.ok, result.error)
        self.assertIn("start with HTTP", contexts[0])
        self.assertNotIn("check the debug endpoint", contexts[0])
        self.assertIn("check the debug endpoint", contexts[1])
        self.assertNotIn("start with HTTP", contexts[1])
        self.assertEqual(store.get_node(initial_hint.id).status, "done")
        self.assertEqual(store.get_node(late_hint["id"]).status, "done")
        self.assertTrue([event for event in events.replay(3000) if event.type == "hint"])

    def test_stop_during_worker_tool_call_cancels_cleanly(self) -> None:
        config = self.make_config(bootstrap=False, concurrency=1, max_seconds=30)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Tool-call cancel", "Stop mid tool call")
        worker_started = threading.Event()

        def blocking_worker(ctx: Any, intent_text: str) -> dict[str, Any]:
            worker_started.set()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if getattr(ctx, "cancel", None) is not None and ctx.cancel.is_set():
                    ctx.check_cancel()  # proves the engine's cancel event reaches tools
                time.sleep(0.01)
            return worker_payload(intent_text)

        script = WorkerScript([INTENTS_PLAN], bootstrap=None,
                              worker=blocking_worker, worker_takes_ctx=True)
        with fake_workers(script):
            run_id = engagement.start_background(project.id)
            self.assertTrue(worker_started.wait(5.0), "worker never started")
            engagement.stop(run_id)
            result = engagement.wait(run_id, timeout=5.0)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "cancelled")
        tasks = store.list_tasks(run_id)
        explore = [task for task in tasks if task.type == "explore"]
        self.assertTrue(explore)
        self.assertTrue(all(task.status == "cancelled" for task in explore))
        intents = store.list_nodes(project.id, kind="intent")
        self.assertTrue(all(node.status == "open" for node in intents),
                        [node.status for node in intents])

    def test_project_reuse_keeps_history_and_creates_new_report(self) -> None:
        config = self.make_config(bootstrap=False, concurrency=2)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Reusable project", "Run repeatedly")
        first_script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=None)
        with fake_workers(first_script):
            first = engagement.run(project.id)
        self.assertTrue(first.ok, first.error)
        first_markdown = Path(first.report_path).read_text(encoding="utf-8")
        first_counts = store.counts(project.id)

        second_plan = {
            "action": "intents",
            "reasoning": "Second pass should inspect the new service.",
            "intents": [{"description": "Enumerate the second-pass service", "priority": 1}],
        }
        second_script = WorkerScript(
            [second_plan, COMPLETE_PLAN], bootstrap=None,
            worker=lambda text: {
                "fact": {"title": f"Second pass observed: {text}", "body": "new evidence"},
                "findings": [], "assets": [], "goal_proven": False, "summary": "second pass",
            })
        with fake_workers(second_script):
            second = engagement.run(project.id)

        self.assertTrue(second.ok, second.error)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertNotEqual(first.report_path, second.report_path)
        self.assertTrue(Path(first.report_path).is_file())
        self.assertTrue(Path(second.report_path).is_file())
        self.assertEqual(Path(first.report_path).read_text(encoding="utf-8"), first_markdown)
        second_counts = store.counts(project.id)
        self.assertGreater(second_counts["nodes"].get("fact", 0), first_counts["nodes"].get("fact", 0))
        self.assertEqual(second_counts["nodes"].get("intent", 0), 3)
        self.assertEqual(len(store.list_runs(project.id)), 2)
        self.assertEqual(store.get_run(first.run_id).status, "completed")
        self.assertEqual(store.get_run(second.run_id).status, "completed")
        goals = store.list_nodes(project.id, kind="goal")
        self.assertTrue(goals)
        self.assertEqual(goals[-1].status, "proven")

    def test_duplicate_intent_descriptions_are_never_recreated(self) -> None:
        config = self.make_config(bootstrap=False, concurrency=2)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("No duplicates", "Do not repeat intents")
        script = WorkerScript([INTENTS_PLAN, COMPLETE_PLAN], bootstrap=None)
        with fake_workers(script):
            first = engagement.run(project.id)
        self.assertTrue(first.ok, first.error)
        self.assertEqual(len(store.list_nodes(project.id, kind="intent")), 2)

        repeat_script = WorkerScript([INTENTS_PLAN], bootstrap=None)
        with fake_workers(repeat_script):
            second = engagement.run(project.id)
        self.assertEqual(second.status, "completed")
        self.assertIn("No productive intents", second.summary)
        self.assertEqual(len(store.list_nodes(project.id, kind="intent")), 2)

    def test_run_rejects_project_with_active_background_run(self) -> None:
        config = self.make_config(bootstrap=False, concurrency=1)
        engagement, store, events = self.make_engagement(config)
        project = engagement.create_project("Busy project", "One run at a time")
        worker_started = threading.Event()

        def blocking_worker(ctx: Any, intent_text: str) -> dict[str, Any]:
            worker_started.set()
            time.sleep(0.4)
            return worker_payload(intent_text)

        script = WorkerScript([INTENTS_PLAN], bootstrap=None,
                              worker=blocking_worker, worker_takes_ctx=True)
        with fake_workers(script):
            run_id = engagement.start_background(project.id)
            self.assertTrue(worker_started.wait(5.0))
            with self.assertRaises(ConfigError):
                engagement.run(project.id)
            engagement.stop(run_id)
            result = engagement.wait(run_id, timeout=5.0)
        self.assertIsNotNone(result)
        self.assertEqual(len(store.list_runs(project.id)), 1)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.project = Project.new("scheduler", "test")
        self.store.create_project(self.project)
        self.run = Run(id="run_sched", project_id=self.project.id, status="running")
        self.store.create_run(self.run)
        self.events = EventBus(replay=500)
        self.addCleanup(self.events.close)

    def _task(self, name: str) -> Task:
        task = Task(id=f"task_{name}", run_id=self.run.id, project_id=self.project.id,
                    type="explore", status="queued")
        self.store.create_task(task)
        return task

    def test_retry_then_success(self) -> None:
        attempts = {"n": 0}

        def flaky(task: Task) -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient")
            return "value"

        scheduler = TaskScheduler(self.store, self.events, max_concurrency=1, retries=1)
        try:
            result = scheduler.run([(self._task("retry"), flaky)])[0]
        finally:
            scheduler.shutdown()
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.value, "value")
        self.assertEqual(self.store.get_task("task_retry").status, "done")

    def test_failure_and_skip_statuses(self) -> None:
        scheduler = TaskScheduler(self.store, self.events, max_concurrency=2, retries=0)
        try:
            fail = scheduler.run([(self._task("fail"),
                                   lambda task: (_ for _ in ()).throw(ValueError("bad")))])[0]
            skip_task = self._task("skip")
            skip = scheduler.run([(skip_task, lambda task: TaskOutcome(
                ok=True, status="skipped", value="already claimed"))])[0]
        finally:
            scheduler.shutdown()
        self.assertEqual(fail.status, "failed")
        self.assertIn("bad", fail.error)
        self.assertEqual(self.store.get_task("task_fail").status, "failed")
        self.assertEqual(skip.status, "skipped")
        self.assertEqual(self.store.get_task("task_skip").status, "skipped")

    def test_cancel_before_attempt(self) -> None:
        cancel = threading.Event()
        cancel.set()
        scheduler = TaskScheduler(self.store, self.events, max_concurrency=1, cancel=cancel)
        try:
            result = scheduler.run([(self._task("cancel"), lambda task: "should not run")])[0]
        finally:
            scheduler.shutdown()
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.cancelled)
        self.assertEqual(self.store.get_task("task_cancel").status, "cancelled")


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.project = Project.new("context", "Assess the context renderer",
                                   scope={"authorized": True, "targets": ["127.0.0.1"]})
        self.store.create_project(self.project)

    def _seed(self, facts: int) -> None:
        for index in range(facts):
            node = Node.new(self.project.id, "fact", f"Fact {index}", "x" * 120)
            self.store.add_node(node)
        for index in range(12):
            finding = Node.new(self.project.id, "finding", f"Finding {index}", "body",
                               status="done", meta={"severity": "high"})
            self.store.add_node(finding)
        self.store.add_node(Node.new(self.project.id, "intent", "Probe the target", "details",
                                     status="open"))
        self.store.add_todo(Todo.new(self.project.id, 1, "Step one", status="pending"))
        self.store.add_node(Node.new(self.project.id, "hint", "Focus on TLS", "try 443",
                                     status="open"))
        for index in range(20):
            self.store.add_trace(Trace.new(self.project.id, "run-1", "task-1", "worker",
                                           index, "tool_result", f"trace body {index}"))

    def test_render_sections_and_determinism(self) -> None:
        self._seed(facts=4)
        first = render_context(self.store, self.project.id, goal="Assess the context renderer",
                               max_chars=12000)
        second = render_context(self.store, self.project.id, goal="Assess the context renderer",
                                max_chars=12000)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 12000)
        self.assertIn("# BLACKBOARD CONTEXT", first)
        for section in ("## FACTS", "## FINDINGS", "## ASSETS", "## OPEN INTENTS",
                        "## TODO CHAIN", "## HINTS", "## RECENT TRACES"):
            self.assertIn(section, first)
        self.assertIn("authorized=true", first)
        self.assertIn("Probe the target", first)
        self.assertIn("Step one", first)

    def test_render_hard_character_budget(self) -> None:
        self._seed(facts=120)
        text = render_context(self.store, self.project.id, goal="Assess the context renderer",
                              max_chars=1500)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("# BLACKBOARD CONTEXT", text)
        self.assertIn("context truncated", text)
        again = render_context(self.store, self.project.id, goal="Assess the context renderer",
                               max_chars=1500)
        self.assertEqual(text, again)

    def test_default_budget_allows_more_context(self) -> None:
        for index in range(10):
            self.store.add_node(Node.new(self.project.id, "fact", f"Fact {index}", "short"))
        text = render_context(self.store, self.project.id, goal="g")
        self.assertLessEqual(len(text), DEFAULT_CONTEXT_CHARS)
        self.assertIn("Fact 9", text)


class FallbackReportTests(unittest.TestCase):
    def test_fallback_report_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aria-x-report-") as tmp:
            export = {"project": {"id": "p1", "name": "P", "goal": "G"},
                      "findings": [], "assets": [], "nodes": [], "todos": []}
            meta = {"run_id": "r1", "project_name": "P", "status": "completed",
                    "generated_at": "2026-01-01T00:00:00Z", "summary": "done",
                    "metrics": {"findings": 0}}
            first = _fallback_report(Path(tmp), export, meta)
            content_one = Path(first["markdown"]).read_text(encoding="utf-8")
            second = _fallback_report(Path(tmp), export, meta)
            content_two = Path(second["markdown"]).read_text(encoding="utf-8")
            self.assertEqual(content_one, content_two)
            self.assertIn("ARIA-X Engagement Report", content_one)
            self.assertTrue(Path(first["html"]).is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

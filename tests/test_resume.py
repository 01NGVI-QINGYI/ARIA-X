"""Resume: an unfinished run continues under its original id without re-bootstrapping."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aria_x.config import AriaXConfig, LLMConfig, SafetyConfig, SandboxConfig, ScopeConfig
from aria_x.engine import Engagement
from aria_x.errors import ConfigError, NotFound
from aria_x.events import EventBus
from aria_x.models import Node, Run
from aria_x.store import Store
from aria_x.util import new_id


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = AriaXConfig(data_dir=str(self.tmp / "data"), runs_dir=str(self.tmp / "runs"))
        self.cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"],
                                     allow_loopback=True, max_rps=100)
        self.cfg.llm = LLMConfig(provider="mock")
        self.cfg.sandbox = SandboxConfig(backend="local")
        self.cfg.safety = SafetyConfig(approval="auto")
        self.store = Store(":memory:")
        self.bus = EventBus()
        self.engagement = Engagement(self.cfg, store=self.store, events=self.bus)
        self.project = self.engagement.create_project("resume", "prove it")

    def tearDown(self) -> None:
        self.engagement.close()
        self.store.close()

    def _crashed_run(self) -> Run:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="running"))
        self.store.add_node(Node(id=new_id("n"), project_id=self.project.id,
                                 kind="fact", title="partial fact"))
        return run

    def test_resume_reuses_the_same_run_id(self) -> None:
        run = self._crashed_run()
        result = self.engagement.resume()
        self.assertEqual(result.run_id, run.id)
        self.assertEqual(self.store.get_run(run.id).status, result.status)

    def test_resume_skips_bootstrap(self) -> None:
        self._crashed_run()
        before = len(self.store.list_nodes(self.project.id))
        self.engagement.resume()
        after = self.store.list_nodes(self.project.id)
        # bootstrap is skipped, so no second goal node is created
        goals = [n for n in after if n.kind == "goal"]
        self.assertLessEqual(len(goals), 1)
        self.assertGreaterEqual(len(after), before)

    def test_completed_run_cannot_resume(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="completed"))
        with self.assertRaises(ConfigError):
            self.engagement.resume(run.id)

    def test_no_resumable_run_raises(self) -> None:
        with self.assertRaises(NotFound):
            self.engagement.resume()

    def test_resume_keeps_existing_facts(self) -> None:
        run = self._crashed_run()
        self.engagement.resume(run.id)
        facts = [n for n in self.store.list_nodes(self.project.id) if n.kind == "fact"]
        self.assertTrue(any(n.title == "partial fact" for n in facts))

class ResumeBudgetAndStateTests(unittest.TestCase):
    """Resume must keep spend, unstick abandoned work and validate ownership."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = AriaXConfig(data_dir=str(self.tmp / "data"), runs_dir=str(self.tmp / "runs"))
        self.cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"],
                                     allow_loopback=True, max_rps=100)
        self.cfg.llm = LLMConfig(provider="mock")
        self.cfg.sandbox = SandboxConfig(backend="local")
        self.cfg.safety = SafetyConfig(approval="auto")
        self.store = Store(":memory:")
        self.bus = EventBus()
        self.engagement = Engagement(self.cfg, store=self.store, events=self.bus)
        self.project = self.engagement.create_project("resume", "prove it")
        self.other = self.engagement.create_project("other", "other goal")

    def tearDown(self) -> None:
        self.engagement.close()
        self.store.close()

    def test_usage_is_carried_into_the_budget(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="running"))
        self.store.update_run(run.id, usage={"llm_calls": 9, "tool_calls": 7,
                                             "prompt_tokens": 1000, "completion_tokens": 200})
        self.engagement.resume(run.id)
        usage = self.store.get_run(run.id).usage
        self.assertGreaterEqual(usage.get("tool_calls", 0), 7, "original tool calls must not be lost")
        self.assertGreaterEqual(usage.get("llm_calls", 0), 9)

    def test_active_intents_are_reopened(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="running"))
        node = Node(id=new_id("n"), project_id=self.project.id, kind="intent",
                    title="stuck intent", status="active")
        self.store.add_node(node)
        self.engagement.resume(run.id)
        # The stuck intent must be picked up again, never left as 'active'.
        self.assertNotEqual(self.store.get_node(node.id).status, "active")

    def test_budget_and_cancelled_runs_can_resume(self) -> None:
        for status in ("budget", "cancelled"):
            run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status=status))
            result = self.engagement.resume(run.id)
            self.assertEqual(result.run_id, run.id)

    def test_mismatched_project_id_is_rejected(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="running"))
        with self.assertRaises(ConfigError):
            self.engagement.resume(run.id, project_id=self.other.id)

    def test_ended_at_is_cleared_on_resume(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="failed"))
        self.store.update_run(run.id, ended_at="2026-11-25T00:00:00Z")
        self.engagement.resume(run.id)
        self.assertNotEqual(self.store.get_run(run.id).ended_at, "2026-11-25T00:00:00Z")


class ResumeBudgetSnapshotTests(unittest.TestCase):
    """Resume must restore the whole budget, not just the token counters."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = AriaXConfig(data_dir=str(self.tmp / "data"), runs_dir=str(self.tmp / "runs"))
        self.cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"],
                                     allow_loopback=True, max_rps=100)
        self.cfg.llm = LLMConfig(provider="mock")
        self.cfg.sandbox = SandboxConfig(backend="local")
        self.cfg.safety = SafetyConfig(approval="auto")
        self.store = Store(":memory:")
        self.bus = EventBus()
        self.engagement = Engagement(self.cfg, store=self.store, events=self.bus)
        self.project = self.engagement.create_project("resume", "prove it")

    def tearDown(self) -> None:
        self.engagement.close()
        self.store.close()

    def test_elapsed_seconds_survive_resume(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="running"))
        self.store.update_run(run.id, budget={"elapsed_seconds": 50.0, "intents_started": 3},
                              usage={"llm_calls": 9, "tool_calls": 7})
        self.engagement.resume(run.id)
        budget = self.store.get_run(run.id).budget
        self.assertGreaterEqual(budget.get("elapsed_seconds", 0), 50.0,
                                "a resumed run keeps the time it already spent")

    def test_intents_started_survive_resume(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="running"))
        self.store.update_run(run.id, budget={"elapsed_seconds": 10.0, "intents_started": 3})
        self.engagement.resume(run.id)
        budget = self.store.get_run(run.id).budget
        self.assertGreaterEqual(budget.get("intents_started", 0), 3,
                                "the intent allowance keeps counting existing intents")


class CrossInstanceRunLockTests(unittest.TestCase):
    """Two Engagement instances must not drive the same project at once."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = AriaXConfig(data_dir=str(self.tmp / "data"), runs_dir=str(self.tmp / "runs"))
        self.cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"],
                                     allow_loopback=True, max_rps=100)
        self.cfg.llm = LLMConfig(provider="mock")
        self.cfg.sandbox = SandboxConfig(backend="local")
        self.cfg.safety = SafetyConfig(approval="auto")
        self.store = Store(":memory:")
        self.bus = EventBus()
        self.first = Engagement(self.cfg, store=self.store, events=self.bus)
        self.second = Engagement(self.cfg, store=self.store, events=self.bus)
        self.project = self.first.create_project("locked", "prove it")

    def tearDown(self) -> None:
        self.first.close()
        self.second.close()
        self.store.close()

    def test_second_instance_cannot_resume_the_same_run(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="failed"))
        with self.first.project_lock(self.project.id):
            with self.assertRaises(ConfigError):
                self.second.resume(run.id)


class ResumeSelectionPolicyTests(unittest.TestCase):
    """No-argument resume picks involuntary stops; cancelled needs an id."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = AriaXConfig(data_dir=str(self.tmp / "data"), runs_dir=str(self.tmp / "runs"))
        self.cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"],
                                     allow_loopback=True, max_rps=100)
        self.cfg.llm = LLMConfig(provider="mock")
        self.cfg.sandbox = SandboxConfig(backend="local")
        self.cfg.safety = SafetyConfig(approval="auto")
        self.store = Store(":memory:")
        self.bus = EventBus()
        self.engagement = Engagement(self.cfg, store=self.store, events=self.bus)
        self.project = self.engagement.create_project("policy", "prove it")

    def tearDown(self) -> None:
        self.engagement.close()
        self.store.close()

    def test_no_argument_resume_picks_budget_and_interrupted(self) -> None:
        for status in ("budget", "interrupted"):
            run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status=status))
            self.assertEqual(self.store.latest_resumable_run().id, run.id)
            self.store.update_run(run.id, status="completed")

    def test_cancelled_is_not_selected_automatically(self) -> None:
        run = self.store.create_run(Run(id=new_id("run"), project_id=self.project.id, status="cancelled"))
        self.assertIsNone(self.store.latest_resumable_run())
        with self.assertRaises(ConfigError):
            self.engagement.resume()          # hints at the id instead of silently restarting
        self.assertEqual(self.engagement.resume(run.id).run_id, run.id)

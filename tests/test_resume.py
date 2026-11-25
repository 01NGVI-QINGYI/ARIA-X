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

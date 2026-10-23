"""End-to-end test: real demo target + real engine + scripted policy.

This is the test the release gate runs before ``aria-x demo``: it starts the
bundled intentionally-vulnerable target on an ephemeral loopback port, drives a
complete engagement with :class:`aria_x.demo.DemoPolicy` (no API key, no network),
and asserts that a finding and report files were actually produced.

It intentionally exercises the same code paths as ``python -m aria_x demo``; if
this test fails, the demo is not shippable.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from aria_x.config import (AriaXConfig, BudgetConfig, EngineConfig, LLMConfig,
                         SafetyConfig, SandboxConfig, ScopeConfig)
from aria_x.demo import FLAG, DemoPolicy, DemoTarget
from aria_x.engine import Engagement
from aria_x.events import EventBus
from aria_x.llm.mock import MockProvider

ROOT = Path(__file__).resolve().parents[1]
DEMO_TARGET = ROOT / "examples" / "demo_target.py"


@unittest.skipUnless(DEMO_TARGET.is_file(), "examples/demo_target.py is missing")
class DemoEndToEndTests(unittest.TestCase):
    """The offline demo must produce a flagged finding and a written report."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="aria-x-e2e-")
        self.tmp = Path(self._tmp.name)
        self.target = DemoTarget(DEMO_TARGET)
        self.target.start(timeout=15.0)
        self.engagement: Engagement | None = None

    def tearDown(self) -> None:
        if self.engagement is not None:
            try:
                self.engagement.close()
            except Exception:
                pass
        self.target.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------

    def _config(self) -> AriaXConfig:
        config = AriaXConfig(
            name="ARIA-X E2E demo",
            data_dir=str(self.tmp / "data"),
            runs_dir=str(self.tmp / "runs"),
            llm=LLMConfig(provider="mock", model="demo-policy", max_retries=0),
            budget=BudgetConfig(max_seconds=90, max_llm_calls=40, max_tokens=200000,
                                max_tool_calls=40, max_steps_per_task=8,
                                max_intents_per_reason=2, max_concurrency=1,
                                max_total_intents=4),
            sandbox=SandboxConfig(backend="mock", network="none", timeout=15),
            scope=ScopeConfig(authorized=True, targets=["127.0.0.1", self.target.url],
                              allow_loopback=True, allow_private=True, max_rps=20),
            safety=SafetyConfig(approval="auto", redact_secrets=True),
            engine=EngineConfig(language="en", bootstrap=True, max_idle_rounds=1),
        )
        config.validate()
        return config

    def test_demo_target_exposes_the_safe_weaknesses(self) -> None:
        with urllib.request.urlopen(self.target.url + "/", timeout=5) as response:
            index = response.read().decode("utf-8", "replace")
        self.assertIn("/files?path=welcome.txt", index)
        self.assertIn("/debug", index)

        with urllib.request.urlopen(self.target.url + "/files?path=../flag.txt", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(FLAG, response.read().decode("utf-8", "replace"))

        with urllib.request.urlopen(self.target.url + "/debug", timeout=5) as response:
            debug = json.loads(response.read().decode("utf-8"))
        self.assertTrue(debug.get("debug"))
        self.assertTrue(debug.get("version"))

        # The safety rail holds: no arbitrary host files, even through traversal.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                self.target.url + "/files?path=../../../../etc/passwd", timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_engagement_produces_finding_and_report(self) -> None:
        config = self._config()
        bus = EventBus(replay=500)
        provider = MockProvider(policy=DemoPolicy(self.target.url, language="en"))
        engagement = Engagement(config, events=bus, provider=provider)
        self.engagement = engagement
        project = engagement.create_project(
            "ARIA-X E2E — path traversal",
            "Prove whether the local demo web app exposes a file-disclosure vulnerability, "
            "capture reproducible evidence, and produce a report.",
            scope={"authorized": True, "targets": ["127.0.0.1", self.target.url]},
        )
        result = engagement.run(project.id)

        self.assertTrue(result.ok, f"engagement failed: {result.status} / {result.error}")
        self.assertIn(result.status, {"completed", "budget", "success"},
                      f"unexpected status: {result.status}")

        # A finding must be persisted, with the flag as evidence.
        counts = engagement.store.counts(project.id)
        findings_by_severity = counts["findings_by_severity"]
        self.assertGreaterEqual(sum(findings_by_severity.values()), 1,
                                f"no finding recorded: {counts}")
        self.assertGreaterEqual(findings_by_severity.get("high", 0), 1,
                                f"expected a high finding: {findings_by_severity}")

        export = engagement.store.export_project(project.id)
        finding_text = " ".join(
            f"{item.get('title', '')} {item.get('body', '')}"
            for item in export.get("findings", [])
        )
        evidence_text = " ".join(
            f"{item.get('excerpt', '')} {item.get('path', '')}"
            for entries in (export.get("evidence") or {}).values()
            for item in entries
        )
        self.assertIn("path traversal", finding_text.lower(), "finding title/body is not about traversal")
        self.assertIn(FLAG, finding_text + " " + evidence_text,
                      "neither the finding nor its evidence records quote the demo flag")

        # Report files must exist on disk (markdown for sure; HTML when the
        # renderer is available) and be referenced by the result.
        self.assertTrue(result.report_path, "result.report_path is empty")
        report_path = Path(result.report_path)
        self.assertTrue(report_path.is_file(), f"report missing: {report_path}")
        self.assertGreater(report_path.stat().st_size, 0, "report is empty")
        run_dir = report_path.parent
        self.assertTrue((run_dir / "report.md").is_file(),
                        f"report.md missing in {run_dir}: {sorted(p.name for p in run_dir.iterdir())}")
        self.assertTrue((run_dir / "findings.json").is_file(),
                        f"findings.json missing in {run_dir}")

        # Events must tell the same story (the console/report path relies on it).
        types = {event.type for event in bus.replay(limit=500)}
        self.assertIn("run.started", types)
        self.assertIn("finding.created", types)
        self.assertIn("report.ready", types)


if __name__ == "__main__":
    unittest.main()

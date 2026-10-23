"""Tests for the human-approval gate on risky tools."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aria_x.config import AriaXConfig, ScopeConfig
from aria_x.events import EventBus
from aria_x.models import Project
from aria_x.store import Store
from aria_x.tools.base import Tool, ToolContext, ToolRegistry, ToolResult, ToolSpec
from aria_x.tools.sandbox import MockSandbox
from aria_x.tools.scope import Scope


class _HighRiskTool(Tool):
    spec = ToolSpec(name="danger", description="test tool", risk="high",
                    parameters={"type": "object", "properties": {"x": {"type": "string"}}})

    def __init__(self) -> None:
        self.calls = 0

    def run(self, ctx, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ToolResult.success({"ran": True})


class ApprovalGateTests(unittest.TestCase):
    def _ctx(self, approval: str, approver=None):
        cfg = AriaXConfig()
        cfg.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"])
        cfg.safety.approval = approval
        store = Store(":memory:")
        project = Project.new("t", "g")
        store.create_project(project)
        workdir = Path(tempfile.mkdtemp(prefix="aria-x-approval-"))
        ctx = ToolContext(project=project, run_id="r", task_id="task", agent="worker",
                          role="worker", store=store, config=cfg, events=EventBus(),
                          scope=Scope(cfg.scope),
                          sandbox=MockSandbox(cfg.sandbox, workdir), workdir=workdir,
                          request_approval=approver)
        return cfg, store, project, ctx, workdir

    def test_auto_allows(self) -> None:
        cfg, store, project, ctx, workdir = self._ctx("auto")
        tool = _HighRiskTool()
        reg = ToolRegistry(); reg.register(tool)
        result = reg.execute("danger", {"x": "1"}, ctx)
        self.assertTrue(result.ok)
        self.assertEqual(tool.calls, 1)
        store.close()

    def test_deny_refuses(self) -> None:
        cfg, store, project, ctx, workdir = self._ctx("deny")
        tool = _HighRiskTool()
        reg = ToolRegistry(); reg.register(tool)
        result = reg.execute("danger", {"x": "1"}, ctx)
        self.assertFalse(result.ok)
        self.assertIn("approval denied", result.error.lower())
        self.assertEqual(tool.calls, 0)
        store.close()

    def test_ask_requires_yes(self) -> None:
        calls = {"n": 0}

        def approve(*args):
            calls["n"] += 1
            return True

        cfg, store, project, ctx, workdir = self._ctx("ask", approve)
        tool = _HighRiskTool()
        reg = ToolRegistry(); reg.register(tool)
        self.assertTrue(reg.execute("danger", {"x": "1"}, ctx).ok)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(calls["n"], 1)
        store.close()

    def test_ask_denied_blocks_run(self) -> None:
        cfg, store, project, ctx, workdir = self._ctx("ask", lambda *a: False)
        tool = _HighRiskTool()
        reg = ToolRegistry(); reg.register(tool)
        result = reg.execute("danger", {"x": "1"}, ctx)
        self.assertFalse(result.ok)
        self.assertEqual(tool.calls, 0)
        store.close()

    def test_ask_without_approver_fails_closed(self) -> None:
        cfg, store, project, ctx, workdir = self._ctx("ask", None)
        reg = ToolRegistry(); reg.register(_HighRiskTool())
        result = reg.execute("danger", {"x": "1"}, ctx)
        self.assertFalse(result.ok)
        self.assertIn("no approver", result.error.lower())
        store.close()

    def test_low_risk_not_gated(self) -> None:
        cfg, store, project, ctx, workdir = self._ctx("deny")

        class Low(Tool):
            spec = ToolSpec(name="safe", description="x", risk="low")

            def run(self, ctx, **kwargs):  # type: ignore[no-untyped-def]
                return ToolResult.success("ok")

        reg = ToolRegistry(); reg.register(Low())
        self.assertTrue(reg.execute("safe", {}, ctx).ok)
        store.close()


if __name__ == "__main__":
    unittest.main()

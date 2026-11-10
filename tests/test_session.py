"""Interactive session (PTY) tests.

These exercise the real process layer: a bash session is spawned, driven and
closed. They are skipped on platforms without ``pty`` so the suite still runs on
Windows, where ARIA-X is not a supported target anyway.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from aria_x.config import AriaXConfig, ScopeConfig
from aria_x.events import EventBus
from aria_x.models import Project
from aria_x.store import Store
from aria_x.tools.base import ToolContext
from aria_x.tools.builtin import build_registry
from aria_x.tools.sandbox import build_sandbox
from aria_x.tools.scope import Scope
from aria_x.tools.session import MAX_SESSIONS, SessionManager

try:
    import pty as _pty  # noqa: F401
    HAS_PTY = True
except ImportError:  # pragma: no cover
    HAS_PTY = False


@unittest.skipUnless(HAS_PTY, "PTY sessions require a POSIX platform")
class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aria-x-session-")
        self.work = Path(self.tmp.name)
        self.config = AriaXConfig(name="session-test", data_dir=str(self.work / "data"),
                                  runs_dir=str(self.work / "runs"))
        self.config.scope = ScopeConfig(authorized=True, targets=["127.0.0.1"], max_rps=1000)
        self.store = Store(":memory:")
        self.project = Project.new("session", "test sessions")
        self.store.create_project(self.project)
        self.manager = SessionManager(banner_wait=0.15)
        self.ctx = ToolContext(
            project=self.project, run_id="run", task_id="task", agent="worker", role="worker",
            store=self.store, config=self.config, events=EventBus(),
            scope=Scope(self.config.scope),
            sandbox=build_sandbox(self.config.sandbox, self.work),
            workdir=self.work, sessions=self.manager,
        )
        self.registry = build_registry(self.ctx)

    def tearDown(self) -> None:
        self.manager.close_all()
        self.store.close()
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------

    def open(self, command: str = "bash --norc") -> str:
        result = self.registry.execute("session_open", {"command": command}, self.ctx)
        self.assertTrue(result.ok, result.error)
        return result.data["session_id"]

    def send(self, session_id: str, text: str, wait_ms: int = 700):
        return self.registry.execute(
            "session_send", {"session_id": session_id, "text": text, "wait_ms": wait_ms}, self.ctx)

    # -- lifecycle -----------------------------------------------------

    def test_open_send_read_close(self) -> None:
        session_id = self.open()
        self.assertEqual(len(self.manager), 1)

        sent = self.send(session_id, "echo ARIA-$((6*7))")
        self.assertTrue(sent.ok, sent.error)
        self.assertIn("ARIA-42", sent.data["output"])
        self.assertTrue(sent.data["alive"])

        read = self.registry.execute("session_read", {"session_id": session_id, "wait_ms": 100},
                                     self.ctx)
        self.assertTrue(read.ok)

        closed = self.registry.execute("session_close", {"session_id": session_id}, self.ctx)
        self.assertTrue(closed.ok)
        self.assertEqual(closed.data["exit_code"], 0)
        self.assertEqual(len(self.manager), 0)

    def test_state_persists_across_calls(self) -> None:
        session_id = self.open()
        self.send(session_id, "cd /tmp")
        out = self.send(session_id, "pwd").data["output"]
        self.assertIn("/tmp", out)

    def test_newline_false_does_not_submit(self) -> None:
        session_id = self.open()
        # The terminal echoes typed characters, so assert on the *executed*
        # output marker ("MARK-DONE"), which only appears once Enter is pressed.
        typed = self.registry.execute(
            "session_send",
            {"session_id": session_id, "text": "printf 'MARK-%s\\n' DONE",
             "newline": False, "wait_ms": 200}, self.ctx)
        self.assertTrue(typed.ok, typed.error)
        self.assertNotIn("MARK-DONE", typed.data["output"])

        submitted = self.send(session_id, "", wait_ms=600).data["output"]
        self.assertIn("MARK-DONE", submitted)

    def test_list_reports_state(self) -> None:
        session_id = self.open()
        listing = self.registry.execute("session_list", {}, self.ctx)
        self.assertTrue(listing.ok)
        self.assertEqual(listing.data["count"], 1)
        entry = listing.data["sessions"][0]
        self.assertEqual(entry["session_id"], session_id)
        self.assertTrue(entry["alive"])

    # -- guardrails ----------------------------------------------------

    def test_destructive_command_blocked(self) -> None:
        result = self.registry.execute("session_open", {"command": "rm -rf /"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("policy", result.error.lower())
        self.assertEqual(len(self.manager), 0)

    def test_out_of_scope_target_blocked(self) -> None:
        result = self.registry.execute("session_open", {"command": "nmap 8.8.8.8"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("scope", result.error.lower())

    def test_send_is_scope_checked(self) -> None:
        session_id = self.open()
        result = self.send(session_id, "curl http://evil.test/")
        self.assertFalse(result.ok)
        self.assertIn("scope", result.error.lower())

    def test_send_is_policy_checked(self) -> None:
        session_id = self.open()
        result = self.send(session_id, "mkfs.ext4 /dev/sda")
        self.assertFalse(result.ok)
        self.assertIn("policy", result.error.lower())

    def test_session_limit(self) -> None:
        opened = 0
        for _ in range(MAX_SESSIONS):
            self.open()
            opened += 1
        result = self.registry.execute("session_open", {"command": "bash --norc"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("limit", result.error.lower())
        self.assertEqual(len(self.manager), opened)

    def test_unknown_session(self) -> None:
        for tool, args in (("session_send", {"session_id": "s_missing", "text": "x"}),
                           ("session_read", {"session_id": "s_missing"}),
                           ("session_close", {"session_id": "s_missing"})):
            result = self.registry.execute(tool, args, self.ctx)
            self.assertFalse(result.ok, tool)
            self.assertIn("unknown session", result.error)

    def test_close_all_reaps_everything(self) -> None:
        for _ in range(3):
            self.open()
        self.assertEqual(len(self.manager), 3)
        self.assertEqual(self.manager.close_all(), 3)
        self.assertEqual(len(self.manager), 0)

    def test_idle_sweep(self) -> None:
        manager = SessionManager(max_idle=0.0, banner_wait=0.1)
        self.ctx.sessions = manager
        session_id = self.open()
        time.sleep(0.05)
        swept = manager.sweep()
        self.assertIn(session_id, swept)
        self.assertEqual(len(manager), 0)

    def test_missing_manager_fails_closed(self) -> None:
        self.ctx.sessions = None
        result = self.registry.execute("session_open", {"command": "bash --norc"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("unavailable", result.error)

    def test_audit_and_trace_recorded(self) -> None:
        session_id = self.open()
        self.send(session_id, "echo audited")
        audit = self.store.list_audit(self.project.id, limit=50)
        actions = {entry.action for entry in audit}
        self.assertIn("session_open", actions)
        self.assertIn("session_send", actions)
        traces = self.store.list_traces(self.project.id, limit=50)
        self.assertTrue(any(t.tool_name == "session_open" for t in traces))


if __name__ == "__main__":
    unittest.main()

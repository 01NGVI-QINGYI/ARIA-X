"""CLI doctor tests, including local sandbox isolation notice."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from aria_x.cli import cmd_doctor


class CliDoctorTests(unittest.TestCase):
    def _write_cfg(self, base: Path, *, backend: str, authorized: bool = True) -> Path:
        data_dir = base / "data"
        runs_dir = base / "runs"
        data_dir.mkdir(exist_ok=True)
        runs_dir.mkdir(exist_ok=True)
        cfg_path = base / "aria-x.json"
        cfg_path.write_text(
            json.dumps({
                "data_dir": str(data_dir),
                "runs_dir": str(runs_dir),
                "scope": {"authorized": authorized, "targets": ["127.0.0.1"] if authorized else []},
                "sandbox": {"backend": backend},
            }),
            encoding="utf-8",
        )
        return cfg_path

    def test_local_backend_emits_isolation_notice_without_breaking_json_or_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg_ok = self._write_cfg(base, backend="local", authorized=True)

            out_human = io.StringIO()
            with redirect_stdout(out_human):
                rc_human = cmd_doctor(argparse.Namespace(config=str(cfg_ok), llm=False, json=False))
            self.assertEqual(rc_human, 0)
            human_text = out_human.getvalue()
            self.assertIn("local", human_text)
            self.assertIn("current user privileges", human_text)
            self.assertIn("OS-level filesystem isolation", human_text)

            out_json = io.StringIO()
            with redirect_stdout(out_json):
                rc_json = cmd_doctor(argparse.Namespace(config=str(cfg_ok), llm=False, json=True))
            self.assertEqual(rc_json, 0)
            items = json.loads(out_json.getvalue())
            sandbox_item = next(item for item in items if item["check"] == "sandbox")
            self.assertTrue(sandbox_item["ok"])
            self.assertEqual(sandbox_item["detail"], "local")
            self.assertIn("current user privileges", sandbox_item["warning"])
            self.assertIn("OS-level filesystem isolation", sandbox_item["warning"])

            # Unauthorized scope still exits 1 while keeping valid JSON and notice
            cfg_unauth = self._write_cfg(base, backend="local", authorized=False)
            out_unauth = io.StringIO()
            with redirect_stdout(out_unauth):
                rc_unauth = cmd_doctor(argparse.Namespace(config=str(cfg_unauth), llm=False, json=True))
            self.assertEqual(rc_unauth, 1)
            unauth_items = json.loads(out_unauth.getvalue())
            sandbox_unauth = next(item for item in unauth_items if item["check"] == "sandbox")
            self.assertIn("warning", sandbox_unauth)

    def test_mock_and_docker_backends_do_not_emit_local_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg_mock = self._write_cfg(base, backend="mock", authorized=True)
            for as_json in (False, True):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = cmd_doctor(argparse.Namespace(config=str(cfg_mock), llm=False, json=as_json))
                self.assertEqual(rc, 0)
                if as_json:
                    items = json.loads(out.getvalue())
                    sandbox_item = next(item for item in items if item["check"] == "sandbox")
                    self.assertEqual(sandbox_item["detail"], "mock")
                    self.assertNotIn("warning", sandbox_item)
                else:
                    self.assertNotIn("OS-level filesystem isolation", out.getvalue())

            cfg_docker = self._write_cfg(base, backend="docker", authorized=True)
            stub = mock.Mock()
            stub.ensure_ready.return_value = (True, "docker ok")
            with mock.patch("aria_x.tools.sandbox.build_sandbox", return_value=stub):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = cmd_doctor(argparse.Namespace(config=str(cfg_docker), llm=False, json=True))
            self.assertEqual(rc, 0)
            items = json.loads(out.getvalue())
            sandbox_item = next(item for item in items if item["check"] == "sandbox")
            self.assertEqual(sandbox_item["detail"], "docker ok")
            self.assertNotIn("warning", sandbox_item)


if __name__ == "__main__":
    unittest.main()

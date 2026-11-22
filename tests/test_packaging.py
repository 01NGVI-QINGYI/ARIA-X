"""Packaging and distribution asset tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aria_x
from aria_x import cli

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_manifest_includes_launcher_and_japanese_readme(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        tokens = set(manifest.split())
        self.assertIn("aria-x", tokens)
        self.assertIn("README.ja.md", tokens)

    def test_packaged_demo_target_matches_example(self) -> None:
        packaged = Path(aria_x.__file__).resolve().parent / "demo_target.py"
        example = ROOT / "examples" / "demo_target.py"
        self.assertTrue(packaged.is_file(), f"missing packaged demo_target: {packaged}")
        self.assertEqual(packaged.read_text(encoding="utf-8"),
                         example.read_text(encoding="utf-8"))

    def test_resolve_demo_target_falls_back_to_packaged_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_cli = Path(tmp) / "site-packages" / "aria_x" / "cli.py"
            fake_cli.parent.mkdir(parents=True)
            fake_pkg_target = fake_cli.parent / "demo_target.py"
            fake_pkg_target.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with mock.patch.object(cli, "__file__", str(fake_cli)):
                script, checkout = cli._resolve_demo_target()
            self.assertEqual(script, fake_pkg_target.resolve())
            self.assertIsNone(checkout)

"""CLI report resolution and Web report consistency tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from aria_x.cli import cmd_report
from aria_x.config import AriaXConfig
from aria_x.models import Project, Run
from aria_x.store import Store

ROOT = Path(__file__).resolve().parents[1]


class CliReportResolutionTests(unittest.TestCase):
    def test_project_id_and_no_arg_select_newest_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            runs_dir = base / "runs"
            data_dir.mkdir()
            runs_dir.mkdir()
            cfg = AriaXConfig(data_dir=str(data_dir), runs_dir=str(runs_dir))
            store = Store(cfg.db_path)
            try:
                p = Project.new("cli-report", "verify newest report")
                store.create_project(p)
                # Older run has lexicographically LARGER id ("run_zzz_old" > "run_aaa_new")
                old_run = Run(id="run_zzz_old", project_id=p.id, status="completed",
                              started_at="2026-10-01T00:00:00Z")
                new_run = Run(id="run_aaa_new", project_id=p.id, status="completed",
                              started_at="2026-11-01T00:00:00Z")
                store.create_run(old_run)
                store.create_run(new_run)
            finally:
                store.close()

            (runs_dir / "run_zzz_old").mkdir()
            old_rep = runs_dir / "run_zzz_old" / "report.html"
            old_rep.write_text("<html>old</html>", encoding="utf-8")
            os.utime(old_rep, (1700000000, 1700000000))

            (runs_dir / "run_aaa_new").mkdir()
            new_rep = runs_dir / "run_aaa_new" / "report.html"
            new_rep.write_text("<html>new</html>", encoding="utf-8")
            os.utime(new_rep, (1800000000, 1800000000))

            cfg_path = base / "aria-x.json"
            cfg_path.write_text(json.dumps({"data_dir": str(data_dir), "runs_dir": str(runs_dir)}),
                                encoding="utf-8")

            for extra_args in ({"project_id": p.id, "run_id": None}, {"project_id": None, "run_id": None}):
                buf = io.StringIO()
                ns = argparse.Namespace(config=str(cfg_path), open=False, **extra_args)
                with redirect_stdout(buf):
                    rc = cmd_report(ns)
                self.assertEqual(rc, 0)
                self.assertEqual(Path(buf.getvalue().strip()).resolve(), new_rep.resolve())

            buf_exact = io.StringIO()
            ns_exact = argparse.Namespace(config=str(cfg_path), open=False, project_id=None, run_id="run_zzz_old")
            with redirect_stdout(buf_exact):
                rc = cmd_report(ns_exact)
            self.assertEqual(rc, 0)
            self.assertEqual(Path(buf_exact.getvalue().strip()).resolve(), old_rep.resolve())

    def test_nested_archives_exact_ids_traversal_and_readonly_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            runs_dir = base / "runs"
            data_dir.mkdir()
            runs_dir.mkdir()

            # Outside directory that must never be leaked via traversal
            outside = base / "outside"
            outside.mkdir()
            (outside / "report.html").write_text("<html>outside</html>", encoding="utf-8")

            # Nested demo-style archive: runs/demo-20261122/run_10/ with findings.json attribution
            demo_dir = runs_dir / "demo-20261122" / "run_10"
            demo_dir.mkdir(parents=True)
            demo_rep = demo_dir / "report.html"
            demo_rep.write_text("<html>demo run_10</html>", encoding="utf-8")
            (demo_dir / "findings.json").write_text(
                json.dumps({"project": {"id": "p_10"}, "meta": {"run_id": "run_10"}}),
                encoding="utf-8",
            )
            os.utime(demo_rep, (1900000000, 1900000000))

            # Unattributed flat report that must NOT match --project-id p_unknown
            unattributed = runs_dir / "run_99"
            unattributed.mkdir()
            (unattributed / "report.md").write_text("# unattributed", encoding="utf-8")

            # 1. Test against a 0-table empty SQLite DB AND a 0-byte DB file
            for empty_mode in ("zero_bytes", "zero_tables"):
                cfg_path = base / f"cfg-{empty_mode}.json"
                cfg_path.write_text(json.dumps({
                    "data_dir": str(data_dir),
                    "runs_dir": str(runs_dir),
                }), encoding="utf-8")
                default_db = data_dir / "aria-x.db"
                if empty_mode == "zero_bytes":
                    default_db.write_bytes(b"")
                else:
                    default_db.unlink(missing_ok=True)
                    c = sqlite3.connect(default_db)
                    c.close()
                size_before = default_db.stat().st_size

                # Exact run_id and project_id find nested demo report
                for extra in (
                    {"project_id": None, "run_id": "run_10"},
                    {"project_id": "p_10", "run_id": None},
                    {"project_id": "p_10", "run_id": "run_10"},
                    {"project_id": None, "run_id": None},
                ):
                    buf = io.StringIO()
                    ns = argparse.Namespace(config=str(cfg_path), open=False, **extra)
                    with redirect_stdout(buf):
                        rc = cmd_report(ns)
                    self.assertEqual(rc, 0, extra)
                    self.assertEqual(Path(buf.getvalue().strip()).resolve(), demo_rep.resolve())

                # Prefix/substring/mismatch/traversal queries must return 2 and NOT initialize DB
                for miss in (
                    {"project_id": None, "run_id": "run_1"},
                    {"project_id": "p_1", "run_id": None},
                    {"project_id": "p_unknown", "run_id": None},
                    {"project_id": "p_other", "run_id": "run_10"},
                    {"project_id": None, "run_id": "../../outside"},
                ):
                    ns = argparse.Namespace(config=str(cfg_path), open=False, **miss)
                    rc = cmd_report(ns)
                    self.assertEqual(rc, 2, miss)

                self.assertEqual(default_db.stat().st_size, size_before)
                check_conn = sqlite3.connect(default_db)
                try:
                    tables = check_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    self.assertEqual(tables, [])
                finally:
                    check_conn.close()

    def test_conflicting_attribution_rejected_and_legacy_flat_archive_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            runs_dir = base / "runs"
            data_dir.mkdir()
            runs_dir.mkdir()
            cfg = AriaXConfig(data_dir=str(data_dir), runs_dir=str(runs_dir))
            store = Store(cfg.db_path)
            try:
                p_a = Project(id="p_A", name="Project A", goal="goal A")
                p_b = Project(id="p_B", name="Project B", goal="goal B")
                p_leg = Project(id="p_legacy", name="Legacy", goal="legacy goal")
                store.create_project(p_a)
                store.create_project(p_b)
                store.create_project(p_leg)
                store.create_run(Run(id="run_conflict", project_id="p_A", status="completed",
                                     started_at="2026-11-01T00:00:00Z"))
                store.create_run(Run(id="run_conflict2", project_id="p_A", status="completed",
                                     started_at="2026-11-02T00:00:00Z"))
                store.create_run(Run(id="run_legacy", project_id="p_legacy", status="completed",
                                     started_at="2026-11-03T00:00:00Z"))
            finally:
                store.close()

            # 1. DB -> p_A, metadata -> p_B (flat runs/run_conflict)
            c1 = runs_dir / "run_conflict"
            c1.mkdir()
            (c1 / "report.html").write_text("<html>B report</html>", encoding="utf-8")
            (c1 / "findings.json").write_text(
                json.dumps({"project": {"id": "p_B"}, "meta": {"run_id": "run_conflict"}}),
                encoding="utf-8",
            )

            # 2. DB -> p_A, directory -> runs/p_B/run_conflict2
            c2 = runs_dir / "p_B" / "run_conflict2"
            c2.mkdir(parents=True)
            (c2 / "report.html").write_text("<html>dir B</html>", encoding="utf-8")

            # 3. Directory -> runs/p_B/run_conflict3, metadata -> p_A
            c3 = runs_dir / "p_B" / "run_conflict3"
            c3.mkdir(parents=True)
            (c3 / "report.html").write_text("<html>dir B meta A</html>", encoding="utf-8")
            (c3 / "findings.json").write_text(
                json.dumps({"project": {"id": "p_A"}, "meta": {"run_id": "run_conflict3"}}),
                encoding="utf-8",
            )

            # 4. Valid legacy flat archive: runs/p_legacy-run_legacy/
            leg_dir = runs_dir / "p_legacy-run_legacy"
            leg_dir.mkdir()
            leg_rep = leg_dir / "report.html"
            leg_rep.write_text("<html>legacy</html>", encoding="utf-8")

            cfg_path = base / "aria-x.json"
            cfg_path.write_text(json.dumps({"data_dir": str(data_dir), "runs_dir": str(runs_dir)}),
                                encoding="utf-8")

            for miss in (
                {"project_id": "p_A", "run_id": "run_conflict"},
                {"project_id": "p_A", "run_id": None},
                {"project_id": None, "run_id": "run_conflict"},
                {"project_id": "p_A", "run_id": "run_conflict2"},
                {"project_id": None, "run_id": "run_conflict2"},
                {"project_id": "p_B", "run_id": "run_conflict3"},
                {"project_id": None, "run_id": "run_conflict3"},
                {"project_id": None, "run_id": "run_leg"},
                {"project_id": "p_leg", "run_id": None},
            ):
                rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **miss))
                self.assertEqual(rc, 2, f"expected 2 for {miss}")

            for hit in (
                {"project_id": None, "run_id": None},
                {"project_id": None, "run_id": "run_legacy"},
                {"project_id": "p_legacy", "run_id": None},
                {"project_id": "p_legacy", "run_id": "run_legacy"},
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **hit))
                self.assertEqual(rc, 0, hit)
                self.assertEqual(Path(buf.getvalue().strip()).resolve(), leg_rep.resolve())

            # Legacy flat archive also works without DB (both with and without findings.json)
            cfg.db_path.unlink()
            for with_meta in (False, True):
                if with_meta:
                    (leg_dir / "findings.json").write_text(
                        json.dumps({"project": {"id": "p_legacy"}, "meta": {"run_id": "run_legacy"}}),
                        encoding="utf-8",
                    )
                for hit in (
                    {"project_id": None, "run_id": None},
                    {"project_id": None, "run_id": "run_legacy"},
                    {"project_id": "p_legacy", "run_id": None},
                    {"project_id": "p_legacy", "run_id": "run_legacy"},
                ):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **hit))
                    self.assertEqual(rc, 0, (with_meta, hit))
                    self.assertEqual(Path(buf.getvalue().strip()).resolve(), leg_rep.resolve())

    def test_legacy_flat_archive_consistent_across_all_entry_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            runs_dir = base / "runs"
            data_dir.mkdir()
            runs_dir.mkdir()

            leg_dir = runs_dir / "p_legacy-run_legacy"
            leg_dir.mkdir()
            leg_rep = leg_dir / "report.html"
            leg_rep.write_text("<html>legacy flat</html>", encoding="utf-8")
            (leg_dir / "findings.json").write_text(
                json.dumps({"project": {"id": "p_legacy"}, "meta": {"run_id": "run_legacy"}}),
                encoding="utf-8",
            )

            cfg_path = base / "aria-x.json"
            cfg_path.write_text(json.dumps({"data_dir": str(data_dir), "runs_dir": str(runs_dir)}),
                                encoding="utf-8")
            db_path = data_dir / "aria-x.db"

            for mode in ("no_db", "zero_bytes", "zero_tables", "matching_db"):
                if mode == "no_db":
                    db_path.unlink(missing_ok=True)
                elif mode == "zero_bytes":
                    db_path.write_bytes(b"")
                elif mode == "zero_tables":
                    db_path.unlink(missing_ok=True)
                    c = sqlite3.connect(db_path)
                    c.close()
                else:
                    db_path.unlink(missing_ok=True)
                    store = Store(str(db_path))
                    try:
                        store.create_project(Project(id="p_legacy", name="Legacy", goal="g"))
                        store.create_run(Run(id="run_legacy", project_id="p_legacy", status="completed",
                                             started_at="2026-11-01T00:00:00Z"))
                    finally:
                        store.close()

                size_before = db_path.stat().st_size if db_path.exists() else None
                for hit in (
                    {"project_id": None, "run_id": None},
                    {"project_id": None, "run_id": "run_legacy"},
                    {"project_id": "p_legacy", "run_id": None},
                    {"project_id": "p_legacy", "run_id": "run_legacy"},
                ):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **hit))
                    self.assertEqual(rc, 0, (mode, hit))
                    self.assertEqual(Path(buf.getvalue().strip()).resolve(), leg_rep.resolve())

                for miss in (
                    {"project_id": None, "run_id": "run_leg"},
                    {"project_id": "p_leg", "run_id": None},
                    {"project_id": "p_other", "run_id": None},
                    {"project_id": "p_other", "run_id": "run_legacy"},
                ):
                    rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **miss))
                    self.assertEqual(rc, 2, (mode, miss))

                if mode in ("zero_bytes", "zero_tables"):
                    self.assertEqual(db_path.stat().st_size, size_before)
                    check_conn = sqlite3.connect(db_path)
                    try:
                        self.assertEqual(
                            check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                            [],
                        )
                    finally:
                        check_conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            runs_dir = base / "runs"
            data_dir.mkdir()
            runs_dir.mkdir()
            bad_dir = runs_dir / "p_A-run_bad"
            bad_dir.mkdir()
            (bad_dir / "report.html").write_text("<html>conflict</html>", encoding="utf-8")
            (bad_dir / "findings.json").write_text(
                json.dumps({"project": {"id": "p_B"}, "meta": {"run_id": "run_bad"}}),
                encoding="utf-8",
            )
            cfg_path = base / "aria-x.json"
            cfg_path.write_text(json.dumps({"data_dir": str(data_dir), "runs_dir": str(runs_dir)}),
                                encoding="utf-8")
            for query in (
                {"project_id": None, "run_id": None},
                {"project_id": None, "run_id": "run_bad"},
                {"project_id": "p_A", "run_id": None},
                {"project_id": "p_B", "run_id": None},
                {"project_id": "p_A", "run_id": "run_bad"},
                {"project_id": "p_B", "run_id": "run_bad"},
            ):
                rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **query))
                self.assertEqual(rc, 2, query)

    def test_older_run_beyond_1000_db_conflict_rejected_and_valid_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            runs_dir = base / "runs"
            data_dir.mkdir()
            runs_dir.mkdir()
            cfg = AriaXConfig(data_dir=str(data_dir), runs_dir=str(runs_dir))
            store = Store(cfg.db_path)
            try:
                store.create_project(Project(id="p_A", name="Project A", goal="goal A"))
                store.create_project(Project(id="p_B", name="Project B", goal="goal B"))
                store.create_project(Project(id="p_valid_old", name="Valid Old", goal="goal old"))
                store.create_project(Project(id="p_filler", name="Filler", goal="filler"))
                store.create_run(Run(
                    id="run_oldest_conflict",
                    project_id="p_A",
                    status="completed",
                    started_at="2020-01-01T00:00:01Z",
                ))
                store.create_run(Run(
                    id="run_oldest_valid",
                    project_id="p_valid_old",
                    status="completed",
                    started_at="2020-01-01T00:00:02Z",
                ))
            finally:
                store.close()

            filler_rows = [
                (
                    f"run_filler_{i:04d}",
                    "p_filler",
                    "completed",
                    f"2026-01-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z",
                )
                for i in range(1003)
            ]
            raw_conn = sqlite3.connect(cfg.db_path)
            try:
                with raw_conn:
                    raw_conn.executemany(
                        "INSERT INTO runs(id, project_id, status, started_at) VALUES(?,?,?,?)",
                        filler_rows,
                    )
            finally:
                raw_conn.close()

            # 3 conflicting archives for run_oldest_conflict (DB -> p_A, disk/metadata -> p_B)
            c_flat = runs_dir / "run_oldest_conflict"
            c_flat.mkdir()
            c_flat_rep = c_flat / "report.html"
            c_flat_rep.write_text("<html>conflict flat</html>", encoding="utf-8")
            (c_flat / "findings.json").write_text(
                json.dumps({"project": {"id": "p_B"}, "meta": {"run_id": "run_oldest_conflict"}}),
                encoding="utf-8",
            )
            os.utime(c_flat_rep, (1950000000, 1950000000))

            c_legacy = runs_dir / "p_B-run_oldest_conflict"
            c_legacy.mkdir()
            c_legacy_rep = c_legacy / "report.html"
            c_legacy_rep.write_text("<html>conflict legacy</html>", encoding="utf-8")
            (c_legacy / "findings.json").write_text(
                json.dumps({"project": {"id": "p_B"}, "meta": {"run_id": "run_oldest_conflict"}}),
                encoding="utf-8",
            )
            os.utime(c_legacy_rep, (1960000000, 1960000000))

            c_nested = runs_dir / "p_B" / "run_oldest_conflict"
            c_nested.mkdir(parents=True)
            c_nested_rep = c_nested / "report.html"
            c_nested_rep.write_text("<html>conflict nested</html>", encoding="utf-8")
            os.utime(c_nested_rep, (1970000000, 1970000000))

            # 1 valid old archive for run_oldest_valid (DB -> p_valid_old, older mtime)
            valid_dir = runs_dir / "run_oldest_valid"
            valid_dir.mkdir()
            valid_rep = valid_dir / "report.html"
            valid_rep.write_text("<html>valid old</html>", encoding="utf-8")
            os.utime(valid_rep, (1700000000, 1700000000))

            cfg_path = base / "aria-x.json"
            cfg_path.write_text(json.dumps({"data_dir": str(data_dir), "runs_dir": str(runs_dir)}),
                                encoding="utf-8")

            for miss in (
                {"project_id": "p_B", "run_id": None},
                {"project_id": "p_A", "run_id": None},
                {"project_id": None, "run_id": "run_oldest_conflict"},
                {"project_id": "p_B", "run_id": "run_oldest_conflict"},
                {"project_id": "p_A", "run_id": "run_oldest_conflict"},
            ):
                rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **miss))
                self.assertEqual(rc, 2, miss)

            for hit in (
                {"project_id": None, "run_id": None},
                {"project_id": "p_valid_old", "run_id": None},
                {"project_id": None, "run_id": "run_oldest_valid"},
                {"project_id": "p_valid_old", "run_id": "run_oldest_valid"},
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_report(argparse.Namespace(config=str(cfg_path), open=False, **hit))
                self.assertEqual(rc, 0, hit)
                self.assertEqual(Path(buf.getvalue().strip()).resolve(), valid_rep.resolve())

    def test_web_load_report_binds_preview_and_download_to_same_run(self) -> None:
        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("node not installed")
        # Extract and exercise loadReport() against mocked fetchRaw where newer run has MD-only
        # and older run has HTML+MD; assert both preview and markdown download belong to newer run.
        script = r"""
const fs = require("fs");
const assert = require("assert");
const i18nCode = fs.readFileSync(process.argv[1], "utf8");
const code = fs.readFileSync(process.argv[2], "utf8");
const elements = {};
function makeEl(id) {
  return {
    id, hidden: false, textContent: "", srcdoc: "", href: "", download: "",
    setAttribute(k, v) { this[k] = v; },
    removeAttribute(k) { delete this[k]; },
    addEventListener() {},
    querySelector() { return null; },
    children: [],
  };
}
for (const id of ["report-empty", "report-frame", "report-meta", "report-open", "report-markdown"]) {
  elements[id] = makeEl(id);
}
const ctx = {
  document: {
    documentElement: { lang: "en" },
    title: "",
    getElementById: (id) => elements[id] || makeEl(id),
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: {
    location: { hash: "#report" },
    addEventListener: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    history: { replaceState: () => {} },
    scrollTo: () => {},
  },
  setTimeout,
  clearTimeout,
  localStorage: { getItem: () => "", setItem: () => {}, removeItem: () => {} },
  URL: {
    _blobs: {},
    createObjectURL(blob) {
      const u = "blob:" + Object.keys(this._blobs).length;
      this._blobs[u] = blob;
      return u;
    },
    revokeObjectURL() {},
  },
  Blob: class Blob { constructor(parts, opts) { this.text = parts.join(""); this.type = opts && opts.type; } },
  fetch: async (url) => {
    if (url === "/api/report/run_new") return { ok: false, status: 404, text: async () => "", headers: new Map() };
    if (url === "/api/report/run_new?format=md") return { ok: true, status: 200, text: async () => "# New MD Report", headers: new Map() };
    if (url === "/api/report/run_old") return { ok: true, status: 200, text: async () => "<html>Old HTML</html>", headers: new Map() };
    if (url === "/api/report/run_old?format=md") return { ok: true, status: 200, text: async () => "# Old MD Report", headers: new Map() };
    if (url === "/api/projects/p1/report") return { ok: true, status: 200, text: async () => "# Project Fallback", headers: new Map() };
    throw new Error("unexpected fetch: " + url);
  },
};
// Instrument app.js to expose loadReport and state in a controlled vm
const vm = require("vm");
Object.assign(ctx, { console, encodeURIComponent, decodeURIComponent, Set, Map, Array, Object, String, Number, Boolean, Date, Math, JSON, Error, Promise });
ctx.window.ARIA_X_I18N = undefined;
vm.runInNewContext(i18nCode, ctx);
const patched = code.replace(/\}\)\(\);\s*$/, "  globalThis.__TEST__ = { state, loadReport };\n})();");
vm.runInNewContext(patched, ctx);
ctx.__TEST__.state.projectId = "p1";
ctx.__TEST__.state.detail = {
  project: { id: "p1", name: "Demo" },
  runs: [
    { id: "run_old", status: "completed", started_at: "2026-10-01T00:00:00Z" },
    { id: "run_new", status: "completed", started_at: "2026-11-01T00:00:00Z" },
  ],
};
ctx.__TEST__.loadReport().then(() => {
  assert(elements["report-meta"].textContent.includes("run_new"), "meta must reference run_new, got: " + elements["report-meta"].textContent);
  assert(elements["report-frame"].srcdoc.includes("New MD Report"), "preview must show run_new markdown");
  assert(!elements["report-frame"].srcdoc.includes("Old HTML"), "preview must not show run_old HTML");
  assert.strictEqual(ctx.__TEST__.state.reportMd, "# New MD Report");
  assert.strictEqual(elements["report-open"]["aria-disabled"], "true");
  assert.strictEqual(elements["report-markdown"]["aria-disabled"], "false");
  const mdBlob = ctx.URL._blobs[elements["report-markdown"].href];
  assert.strictEqual(mdBlob.text, "# New MD Report");
  console.log("WEB_REPORT_CONSISTENCY_OK");
}).catch((err) => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run([
            node, "-e", script,
            str(ROOT / "src" / "aria_x" / "web" / "i18n.js"),
            str(ROOT / "src" / "aria_x" / "web" / "app.js"),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WEB_REPORT_CONSISTENCY_OK", proc.stdout)

    def test_web_report_race_guards_and_project_switching(self) -> None:
        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("node not installed")
        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const i18nCode = fs.readFileSync(process.argv[1], "utf8");
const appCode = fs.readFileSync(process.argv[2], "utf8");

const elements = {};
function makeEl(id) {
  return {
    id, hidden: false, disabled: false, value: "", textContent: "", srcdoc: "", href: "",
    className: "", children: [],
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { this[k] = v; },
    removeAttribute(k) { delete this[k]; },
    addEventListener() {},
    querySelector() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs; },
  };
}
for (const id of [
  "report-empty", "report-frame", "report-meta", "report-open", "report-markdown",
  "project-select", "overview-empty", "overview-content", "project-name", "project-goal",
  "run-id", "run-state", "run-chip", "btn-start", "btn-stop", "approval-list",
  "approval-count", "conn-pill", "conn-text", "sse-status", "timeline-pulse",
  "timeline-log", "timeline-empty", "timeline-count", "timeline-autoscroll", "timeline-type",
]) {
  elements[id] = makeEl(id);
}

const deferred = [];
function nextDeferred(url) {
  let res, rej;
  const p = new Promise((r, j) => { res = r; rej = j; });
  deferred.push({ url, res, rej });
  return p;
}

const ctx = {
  document: {
    documentElement: { lang: "en" },
    title: "",
    getElementById: (id) => elements[id] || makeEl(id),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    addEventListener: () => {},
  },
  window: {
    location: { hash: "#report" },
    addEventListener: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    history: { replaceState: () => {} },
    scrollTo: () => {},
  },
  setTimeout,
  clearTimeout,
  localStorage: { getItem: () => "", setItem: () => {}, removeItem: () => {} },
  EventSource: class { close() {} },
  URL: {
    _blobs: {},
    _revoked: new Set(),
    createObjectURL(blob) {
      const u = "blob:" + Object.keys(this._blobs).length;
      this._blobs[u] = blob;
      return u;
    },
    revokeObjectURL(u) { this._revoked.add(u); },
  },
  Blob: class Blob { constructor(parts, opts) { this.text = parts.join(""); this.type = opts && opts.type; } },
  fetch: (url) => nextDeferred(url),
  setTimeout,
  clearTimeout,
};
Object.assign(ctx, { console, encodeURIComponent, decodeURIComponent, Set, Map, Array, Object, String, Number, Boolean, Date, Math, JSON, Error, Promise });
ctx.window.ARIA_X_I18N = undefined;
vm.runInNewContext(i18nCode, ctx);
const patched = appCode.replace(/\}\)\(\);\s*$/, "  globalThis.__TEST__ = { state, loadReport, selectProject };\n})();");
vm.runInNewContext(patched, ctx);

function okResp(text) {
  return { ok: true, status: 200, text: async () => text, headers: new Map() };
}
function notFound() {
  return { ok: false, status: 404, text: async () => "", headers: new Map() };
}
function resolveByUrl(urlSubstring, resp) {
  const idx = deferred.findIndex(d => d.url.includes(urlSubstring));
  assert(idx !== -1, "missing deferred for " + urlSubstring);
  const [item] = deferred.splice(idx, 1);
  item.res(resp);
}
async function flush() {
  for (let i = 0; i < 4; i += 1) await new Promise(r => setTimeout(r, 2));
}

(async () => {
  const { state, loadReport, selectProject } = ctx.__TEST__;

  // 1. Delayed cross-project response: A starts, user switches to B, B renders, A resolves late.
  state.projectId = "projA";
  state.detail = { project: { id: "projA" }, runs: [{ id: "runA", status: "completed", started_at: "2026-11-01T00:00:00Z" }] };
  const pA = loadReport();

  state.projectId = "projB";
  state.detail = { project: { id: "projB" }, runs: [{ id: "runB", status: "completed", started_at: "2026-11-02T00:00:00Z" }] };
  const pB = loadReport();

  // Resolve B first
  resolveByUrl("/api/report/runB?format=md", okResp("# MD B"));
  resolveByUrl("/api/report/runB", okResp("<html><body><h1>Report B</h1></body></html>"));
  await pB;
  await flush();

  assert(elements["report-meta"].textContent.includes("runB"));
  assert(elements["report-frame"].srcdoc.includes("Report B"));
  const bHtmlUrl = elements["report-open"].href;
  const bMdUrl = elements["report-markdown"].href;
  assert.strictEqual(ctx.URL._blobs[bMdUrl].text, "# MD B");

  // Now resolve stale A
  resolveByUrl("/api/report/runA?format=md", okResp("# MD A"));
  resolveByUrl("/api/report/runA", okResp("<html><body><h1>Report A</h1></body></html>"));
  await pA;
  await flush();

  assert.strictEqual(state.projectId, "projB");
  assert(elements["report-meta"].textContent.includes("runB"), "stale A overwrote meta");
  assert(elements["report-frame"].srcdoc.includes("Report B"), "stale A overwrote srcdoc");
  assert(!elements["report-frame"].srcdoc.includes("Report A"), "stale A leaked into srcdoc");
  assert.strictEqual(elements["report-open"].href, bHtmlUrl);
  assert.strictEqual(elements["report-markdown"].href, bMdUrl);
  assert(!ctx.URL._revoked.has(bHtmlUrl), "B html blob must not be revoked");
  assert(!ctx.URL._revoked.has(bMdUrl), "B md blob must not be revoked");

  // 2. Consecutive same-project refreshes: req1 slow, req2 fast
  state.detail = { project: { id: "projB" }, runs: [{ id: "runB2", status: "completed", started_at: "2026-11-03T00:00:00Z" }] };
  const p1 = loadReport();
  const p2 = loadReport();
  // There are two pairs of runB2 requests in deferred; resolve the second pair first
  const idxHtml2 = deferred.map(d => d.url).lastIndexOf("/api/report/runB2");
  const idxMd2 = deferred.map(d => d.url).lastIndexOf("/api/report/runB2?format=md");
  deferred[idxMd2].res(okResp("# MD B2 NEW"));
  deferred[idxHtml2].res(okResp("<html><body><h1>Report B2 NEW</h1></body></html>"));
  await p2;
  await flush();
  assert(elements["report-frame"].srcdoc.includes("Report B2 NEW"));
  // Resolve stale #1
  resolveByUrl("/api/report/runB2?format=md", okResp("# MD B2 STALE"));
  resolveByUrl("/api/report/runB2", okResp("<html><body><h1>Report B2 STALE</h1></body></html>"));
  await p1;
  await flush();
  assert(elements["report-frame"].srcdoc.includes("Report B2 NEW"));
  assert(!elements["report-frame"].srcdoc.includes("Report B2 STALE"));

  // 3. Switch to project with no report (and clear to empty)
  state.projectId = "projC";
  state.detail = { project: { id: "projC" }, runs: [{ id: "runC", status: "completed", started_at: "2026-11-04T00:00:00Z" }] };
  const pC = loadReport();
  resolveByUrl("/api/report/runC?format=md", notFound());
  resolveByUrl("/api/report/runC", notFound());
  await flush();
  resolveByUrl("/api/projects/projC/report", notFound());
  await pC;
  await flush();
  assert.strictEqual(elements["report-frame"].hidden, true);
  assert.strictEqual(elements["report-empty"].hidden, false);
  assert.strictEqual(elements["report-open"]["aria-disabled"], "true");
  assert.strictEqual(elements["report-markdown"]["aria-disabled"], "true");

  // Clear project via selectProject("")
  await selectProject("");
  assert.strictEqual(elements["report-frame"].hidden, true);
  assert.strictEqual(elements["report-empty"].hidden, false);
  console.log("WEB_REPORT_RACE_OK");
})().catch((err) => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run([
            node, "-e", script,
            str(ROOT / "src" / "aria_x" / "web" / "i18n.js"),
            str(ROOT / "src" / "aria_x" / "web" / "app.js"),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WEB_REPORT_RACE_OK", proc.stdout)

    def test_web_graph_race_guards_and_timer_cancellation(self) -> None:
        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("node not installed")
        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const i18nCode = fs.readFileSync(process.argv[1], "utf8");
const appCode = fs.readFileSync(process.argv[2], "utf8");

const elements = {};
function makeEl(id) {
  return {
    id, hidden: false, disabled: false, value: "", textContent: "", srcdoc: "", href: "",
    className: "", children: [], style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { this[k] = v; if (k === "hidden") this.hidden = true; },
    removeAttribute(k) { delete this[k]; if (k === "hidden") this.hidden = false; },
    addEventListener() {},
    querySelector() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach(c => this.appendChild(c)); },
    replaceChildren(...cs) { this.children = cs; },
  };
}
for (const id of [
  "report-empty", "report-frame", "report-meta", "report-open", "report-markdown",
  "project-select", "overview-empty", "overview-content", "project-name", "project-goal",
  "run-id", "run-state", "run-chip", "btn-start", "btn-stop", "approval-list",
  "approval-count", "conn-pill", "conn-text", "sse-status", "timeline-pulse",
  "timeline-log", "timeline-empty", "timeline-count", "timeline-autoscroll", "timeline-type",
  "graph-svg", "graph-empty", "graph-count", "graph-detail", "graph-legend",
]) {
  elements[id] = makeEl(id);
}

const deferred = [];
const scheduledTimers = new Map();
let nextTimerId = 1;
const clearedTimers = [];

const ctx = {
  document: {
    documentElement: { lang: "en" },
    title: "",
    getElementById: (id) => elements[id] || makeEl(id),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    createElementNS: (_ns, tag) => makeEl(tag),
    addEventListener: () => {},
  },
  window: {
    location: { hash: "#graph" },
    addEventListener: () => {},
    setTimeout: (fn, ms) => { const id = nextTimerId++; scheduledTimers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { if (id) { clearedTimers.push(id); scheduledTimers.delete(id); } },
    history: { replaceState: () => {} },
    scrollTo: () => {},
  },
  localStorage: { getItem: () => "", setItem: () => {}, removeItem: () => {} },
  EventSource: class { close() {} },
  URL: { createObjectURL: () => "blob:0", revokeObjectURL() {} },
  Blob: class {},
  fetch: (url) => new Promise((res, rej) => { deferred.push({ url, res, rej }); }),
  setTimeout,
  clearTimeout,
};
Object.assign(ctx, { console, encodeURIComponent, decodeURIComponent, Set, Map, Array, Object, String, Number, Boolean, Date, Math, JSON, Error, Promise });
ctx.window.ARIA_X_I18N = undefined;
vm.runInNewContext(i18nCode, ctx);
const patched = appCode.replace(/\}\)\(\);\s*$/, "  globalThis.__TEST__ = { state, ensureGraph, renderGraph, selectProject, maybeRefreshForEvent };\n})();");
vm.runInNewContext(patched, ctx);

function jsonResp(obj) {
  return { ok: true, status: 200, text: async () => JSON.stringify(obj), headers: new Map() };
}
function takeDeferred(urlSub) {
  const idx = deferred.findIndex(d => d.url.includes(urlSub));
  assert(idx !== -1, "missing deferred for " + urlSub);
  return deferred.splice(idx, 1)[0];
}
async function flush() {
  for (let i = 0; i < 4; i += 1) await new Promise(r => setTimeout(r, 2));
}

(async () => {
  const { state, ensureGraph, renderGraph, selectProject, maybeRefreshForEvent } = ctx.__TEST__;

  // 1. Cross-project delayed graph response: A starts, user switches to B, B renders, A resolves late.
  state.tab = "graph";
  state.projectId = "projA";
  const pA = ensureGraph().then(ok => { if (ok) renderGraph(); });

  state.projectId = "projB";
  const pB = ensureGraph().then(ok => { if (ok) renderGraph(); });

  takeDeferred("/api/projects/projB/graph").res(jsonResp({
    nodes: [{ id: "b1", kind: "goal", title: "Goal B", status: "open" }],
    edges: [], assets: [],
  }));
  await pB;
  await flush();
  assert.strictEqual(state.graph.nodes[0].id, "b1");
  assert(elements["graph-count"].textContent.startsWith("1 nodes"), elements["graph-count"].textContent);

  // Late A resolves -> must be ignored
  takeDeferred("/api/projects/projA/graph").res(jsonResp({
    nodes: [
      { id: "a1", kind: "goal", title: "Goal A1", status: "open" },
      { id: "a2", kind: "fact", title: "Fact A2", status: "done" },
    ],
    edges: [], assets: [],
  }));
  await pA;
  await flush();
  assert.strictEqual(state.graph.nodes[0].id, "b1", "stale A graph overwrote B");
  assert(elements["graph-count"].textContent.startsWith("1 nodes"), "stale A graph count overwrote B");

  // 2. Consecutive same-project graph refreshes: #1 slow, #2 fast
  const g1 = ensureGraph().then(ok => { if (ok) renderGraph(); });
  const g2 = ensureGraph().then(ok => { if (ok) renderGraph(); });
  const idx2 = deferred.map(d => d.url).lastIndexOf("/api/projects/projB/graph");
  deferred.splice(idx2, 1)[0].res(jsonResp({
    nodes: [{ id: "b_new", kind: "goal", title: "Goal B New", status: "open" }],
    edges: [], assets: [],
  }));
  await g2;
  await flush();
  assert.strictEqual(state.graph.nodes[0].id, "b_new");
  takeDeferred("/api/projects/projB/graph").res(jsonResp({
    nodes: [{ id: "b_old", kind: "goal", title: "Goal B Old", status: "open" }],
    edges: [], assets: [],
  }));
  await g1;
  await flush();
  assert.strictEqual(state.graph.nodes[0].id, "b_new", "stale same-project graph overwrote newer");

  // 3. Project switch / clear cancels pending graphRefreshTimer
  maybeRefreshForEvent("node.created");
  assert(state.graphRefreshTimer !== null, "expected graphRefreshTimer to be scheduled");
  const timerId = state.graphRefreshTimer;
  await selectProject("");
  assert.strictEqual(state.graphRefreshTimer, null, "graphRefreshTimer must be cleared");
  assert(clearedTimers.includes(timerId), "clearTimeout must be called for old graphRefreshTimer");
  assert.strictEqual(state.graph, null);
  console.log("WEB_GRAPH_RACE_OK");
})().catch((err) => { console.error(err); process.exit(1); });
"""
        proc = subprocess.run([
            node, "-e", script,
            str(ROOT / "src" / "aria_x" / "web" / "i18n.js"),
            str(ROOT / "src" / "aria_x" / "web" / "app.js"),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WEB_GRAPH_RACE_OK", proc.stdout)

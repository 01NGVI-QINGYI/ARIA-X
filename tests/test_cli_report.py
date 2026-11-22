"""CLI report resolution and Web report consistency tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
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

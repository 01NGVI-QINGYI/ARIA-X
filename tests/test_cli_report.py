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

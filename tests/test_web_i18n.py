"""Web console trilingual i18n tests (static parity, HTTP delivery, DOM safety, VM behavior)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from aria_x.config import AriaXConfig
from aria_x.events import EventBus
from aria_x.server import create_server
from aria_x.store import Store

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "aria_x" / "web"
INDEX_HTML = WEB_DIR / "index.html"
I18N_JS = WEB_DIR / "i18n.js"
APP_JS = WEB_DIR / "app.js"

NODE = shutil.which("node") or "/opt/homebrew/bin/node"


class WebI18nTests(unittest.TestCase):
    def test_static_safety_no_innerhtml_or_eval(self) -> None:
        forbidden = ("innerHTML", "outerHTML =", "insertAdjacentHTML", "eval(", "new Function")
        for path in (I18N_JS, APP_JS):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{token!r} must not appear in {path.name}")

    def test_node_syntax_checks(self) -> None:
        if not Path(NODE).exists():
            self.skipTest("node not installed")
        for path in (I18N_JS, APP_JS):
            proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, f"{path.name}: {proc.stderr}")

    def test_http_serves_i18n_js_and_index_language_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AriaXConfig(data_dir=tmp, runs_dir=str(Path(tmp) / "runs"))
            cfg.server.host = "127.0.0.1"
            cfg.server.port = 0
            store = Store(":memory:")
            bus = EventBus(replay=10)
            server = create_server(cfg, object(), bus, store)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    csp = resp.headers.get("Content-Security-Policy", "")
                    self.assertIn("script-src 'self'", csp)
                    html = resp.read().decode("utf-8")
                    self.assertIn('<script src="/static/i18n.js" defer></script>', html)
                    self.assertIn('id="language-select"', html)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/i18n.js", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    ctype = resp.headers.get("Content-Type", "")
                    self.assertIn("javascript", ctype.lower())
                    body = resp.read().decode("utf-8")
                    self.assertIn("ARIA_X_I18N", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                bus.close()
                store.close()

    def test_dictionary_parity_and_html_app_key_coverage(self) -> None:
        if not Path(NODE).exists():
            self.skipTest("node not installed")
        html_text = INDEX_HTML.read_text(encoding="utf-8")
        app_text = APP_JS.read_text(encoding="utf-8")

        html_keys = sorted(set(re.findall(
            r'data-i18n(?:-(?:placeholder|title|aria-label))?="([^"]+)"', html_text
        )))
        app_keys = sorted(set(re.findall(r'\bt\(\s*"([^"]+)"', app_text)))

        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const code = fs.readFileSync(process.argv[1], "utf8");
const htmlKeys = JSON.parse(process.argv[2]);
const appKeys = JSON.parse(process.argv[3]);
const ctx = { window: {}, globalThis: {} };
vm.runInNewContext(code, ctx);
const i18n = ctx.window.ARIA_X_I18N;
assert(i18n, "ARIA_X_I18N missing");
const locales = ["en", "zh-CN", "ja"];
assert.deepStrictEqual(Array.from(i18n.SUPPORTED_LANGUAGES), locales);
assert.strictEqual(i18n.DEFAULT_LANGUAGE, "en");

function placeholders(str) {
  return Array.from(String(str).matchAll(/\{([a-zA-Z0-9_]+)\}/g)).map(m => m[1]).sort();
}

const enKeys = Object.keys(i18n.MESSAGES.en).sort();
for (const loc of locales) {
  const dict = i18n.MESSAGES[loc];
  assert(dict, "missing locale " + loc);
  assert.deepStrictEqual(Object.keys(dict).sort(), enKeys, "key mismatch in " + loc);
  for (const key of enKeys) {
    assert.strictEqual(typeof dict[key], "string", loc + "." + key + " not string");
    assert(dict[key].length > 0, loc + "." + key + " empty");
    assert.deepStrictEqual(placeholders(dict[key]), placeholders(i18n.MESSAGES.en[key]),
      "placeholder mismatch for " + loc + "." + key);
  }
}

for (const key of htmlKeys) {
  assert(Object.prototype.hasOwnProperty.call(i18n.MESSAGES.en, key), "missing HTML key: " + key);
}
for (const key of appKeys) {
  assert(Object.prototype.hasOwnProperty.call(i18n.MESSAGES.en, key), "missing app.js key: " + key);
}
console.log("I18N_PARITY_OK", enKeys.length, htmlKeys.length, appKeys.length);
"""
        proc = subprocess.run(
            [NODE, "-e", script, str(I18N_JS), json.dumps(html_keys), json.dumps(app_keys)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("I18N_PARITY_OK", proc.stdout)

    def test_language_switching_is_display_only_and_preserves_state(self) -> None:
        if not Path(NODE).exists():
            self.skipTest("node not installed")
        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const i18nCode = fs.readFileSync(process.argv[1], "utf8");
const appCode = fs.readFileSync(process.argv[2], "utf8");

function makeNode(tag, attrs = {}) {
  const el = {
    tagName: tag.toUpperCase(),
    id: attrs.id || "",
    className: "",
    hidden: false,
    disabled: false,
    checked: false,
    value: attrs.value || "",
    textContent: attrs.textContent || "",
    placeholder: "",
    srcdoc: "",
    href: "",
    tabIndex: 0,
    dataset: {},
    style: {},
    options: [],
    children: [],
    _attrs: Object.assign({}, attrs),
    _listeners: {},
    classList: {
      _set: new Set(),
      add(...xs) { xs.forEach(x => this._set.add(x)); },
      remove(...xs) { xs.forEach(x => this._set.delete(x)); },
      toggle(x, f) { if (f === undefined) { if (this._set.has(x)) this._set.delete(x); else this._set.add(x); } else if (f) this._set.add(x); else this._set.delete(x); },
    },
    setAttribute(k, v) { this._attrs[k] = String(v); if (k === "placeholder") this.placeholder = String(v); if (k === "hidden") this.hidden = true; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; if (k === "hidden") this.hidden = false; },
    appendChild(child) { this.children.push(child); if (this.tagName === "SELECT" && child.tagName === "OPTION") this.options.push(child); return child; },
    append(...children) { children.forEach(c => this.appendChild(c)); },
    replaceChildren(...children) { this.children = []; if (this.tagName === "SELECT") this.options = []; children.forEach(c => this.appendChild(c)); },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    querySelector(sel) {
      if (sel === "[data-chip-label]") return null;
      return null;
    },
    querySelectorAll() { return []; },
    focus() {},
    scrollIntoView() {},
    remove() {},
  };
  return el;
}

const byId = {};
const i18nNodes = [];
const placeholderNodes = [];
const titleNodes = [];
const ariaNodes = [];

function reg(id, tag, dataI18n, attrs = {}) {
  const n = makeNode(tag, Object.assign({ id }, attrs));
  if (dataI18n) {
    n.setAttribute("data-i18n", dataI18n);
    i18nNodes.push(n);
  }
  byId[id] = n;
  return n;
}

reg("project-select", "select");
reg("run-chip", "span");
reg("run-chip-text", "span");
byId["run-chip"].appendChild(byId["run-chip-text"]);
reg("btn-refresh", "button");
reg("btn-settings", "button");
reg("conn-pill", "span");
reg("conn-text", "span");
byId["conn-pill"].appendChild(byId["conn-text"]);
reg("overview-empty", "div");
reg("overview-content", "div");
reg("project-status", "span");
reg("project-name", "h3");
reg("project-goal", "p");
reg("project-meta", "dl");
reg("run-state", "span");
reg("run-id", "code");
reg("run-elapsed", "span");
reg("run-goal", "textarea", null, { "data-i18n-placeholder": "overview.runGoalPlaceholder" });
placeholderNodes.push(byId["run-goal"]);
reg("btn-start", "button");
reg("btn-stop", "button");
reg("run-tasks-wrap", "div");
reg("run-tasks", "ul");
reg("overview-stats", "section");
reg("budget-bars", "div");
reg("approval-count", "span");
reg("approval-list", "div");
reg("hint-input", "textarea");
reg("hint-submit", "button");
reg("create-form", "form");
reg("new-name", "input");
reg("new-targets", "input");
reg("new-goal", "textarea");
reg("new-authorized", "input");
reg("findings-count", "span");
reg("tab-findings-count", "span");
reg("findings-body", "tbody");
reg("findings-empty", "div");
reg("finding-detail", "aside");
reg("finding-filter", "select");
reg("assets-count", "span");
reg("assets-body", "tbody");
reg("assets-empty", "div");
reg("asset-filter", "select");
reg("asset-search", "input");
reg("todos-count", "span");
reg("todos-list", "ul");
reg("todos-empty", "div");
reg("graph-count", "p");
reg("graph-empty", "div");
reg("graph-svg", "svg");
reg("graph-legend", "div");
reg("graph-detail", "div");
reg("graph-fit", "button");
reg("timeline-count", "span");
reg("sse-status", "span");
reg("timeline-pulse", "span");
reg("timeline-log", "ol");
reg("timeline-empty", "div");
reg("timeline-filter", "input");
reg("timeline-type", "select");
reg("timeline-autoscroll", "input");
reg("timeline-pause", "button");
reg("timeline-clear", "button");
reg("report-meta", "p");
reg("report-empty", "div");
reg("report-frame", "iframe");
reg("report-open", "a");
reg("report-markdown", "a");
reg("btn-report-reload", "button");
reg("settings-refresh", "button");
reg("settings-scope", "dl");
reg("settings-config", "pre");
reg("settings-tools", "div");
reg("settings-version", "p");
reg("token-input", "input");
reg("btn-token-save", "button");
reg("btn-token-clear", "button");
reg("poll-interval", "select");
reg("language-select", "select");
reg("toasts", "div");
reg("tab-overview-label", "span", "tabs.overview");
reg("settings-title-label", "h2", "settings.title");

const tabButtons = ["overview", "graph", "findings", "assets", "timeline", "todos", "report", "settings"].map(name => {
  const b = makeNode("button", { id: "tab-" + name });
  b.dataset.tab = name;
  return b;
});

const storage = new Map();
let fetchCalls = 0;
let esCalls = 0;

const ctx = {
  document: {
    documentElement: { lang: "en" },
    title: "",
    hidden: false,
    getElementById: (id) => byId[id] || makeNode("div", { id }),
    querySelectorAll: (sel) => {
      if (sel === ".tab") return tabButtons;
      if (sel === "[data-i18n]") return i18nNodes;
      if (sel === "[data-i18n-placeholder]") return placeholderNodes;
      if (sel === "[data-i18n-title]") return titleNodes;
      if (sel === "[data-i18n-aria-label]") return ariaNodes;
      return [];
    },
    createElement: (tag) => makeNode(tag),
    createElementNS: (_ns, tag) => makeNode(tag),
    addEventListener: () => {},
  },
  window: {
    location: { hash: "#settings" },
    addEventListener: () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
    history: { replaceState: () => {} },
    scrollTo: () => {},
    confirm: () => true,
  },
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  EventSource: class { constructor() { esCalls += 1; } close() {} },
  fetch: async () => { fetchCalls += 1; throw new Error("fetch must not be called"); },
  URL: { createObjectURL: () => "blob:0", revokeObjectURL: () => {} },
  Blob: class {},
  console, encodeURIComponent, decodeURIComponent, Set, Map, Array, Object, String, Number, Boolean, Date, Math, JSON, Error, Promise,
};
ctx.window.ARIA_X_I18N = undefined;
vm.runInNewContext(i18nCode, ctx);
const patched = appCode.replace(/\}\)\(\);\s*$/, "  globalThis.__TEST__ = { state, setLanguage, applyStaticI18n, renderOverview, renderFindings, renderAssets, renderTodos, renderGraph, renderTimeline, renderSettings, renderReportMeta, t };\n})();");
vm.runInNewContext(patched, ctx);

const i18n = ctx.window.ARIA_X_I18N;
assert.strictEqual(i18n.normalizeLanguage("zh"), "zh-CN");
assert.strictEqual(i18n.normalizeLanguage("zh_CN"), "zh-CN");
assert.strictEqual(i18n.normalizeLanguage("ja-JP"), "ja");
assert.strictEqual(i18n.normalizeLanguage("en-US"), "en");
assert.strictEqual(i18n.normalizeLanguage("fr"), "en");
assert.strictEqual(i18n.t("missing.key", null, "ja"), "missing.key");
assert.strictEqual(ctx.__TEST__.state.lang, "en");

// Populate cached state + user inputs + loaded report iframe
const st = ctx.__TEST__.state;
st.tab = "settings";
st.projectId = "p1";
st.projects = [{ id: "p1", name: "Demo Project" }];
st.detail = {
  project: { id: "p1", name: "Demo Project", goal: "Keep goal verbatim", status: "active", created_at: "2026-11-01T00:00:00Z", scope: { targets: ["127.0.0.1"] } },
  runs: [{ id: "r1", status: "completed", started_at: "2026-11-01T00:00:00Z", ended_at: "2026-11-01T00:01:00Z" }],
  counts: { nodes: { fact: 2, intent: 1 }, assets: { host: 1 }, findings_by_severity: { high: 1 }, todos: 1, traces: 3 },
};
st.run = { run: st.detail.runs[0], tasks: [{ id: "t1", type: "explore", status: "done", agent: "worker" }], budget: { limits: { max_seconds: 60 }, usage: { llm_calls: 2, tool_calls: 5, total_tokens: 100 } } };
st.findings = [{ id: "f1", title: "Original Finding Title", body: "Original body", status: "open", meta: { severity: "high", confidence: "high" }, created_at: "2026-11-01T00:00:00Z" }];
st.selectedFindingId = "f1";
st.findingFilter = "high";
st.assets = [{ id: "a1", kind: "host", value: "127.0.0.1", first_seen: "2026-11-01T00:00:00Z", last_seen: "2026-11-01T00:00:00Z" }];
st.assetFilter = "host";
st.assetSearch = "127";
st.todos = [{ position: 1, status: "done", title: "Check login", depends_on: ["0"] }];
st.graph = { nodes: [{ id: "n1", kind: "goal", title: "Goal Node", status: "open" }], edges: [], assets: [] };
st.selectedNodeId = "n1";
st.config = { version: "0.2.0", tagline: "Local", scope_summary: { authorized: true, targets: ["127.0.0.1"], deny: [], allow_private: true, allow_loopback: true, max_rps: 5 }, tools: [{ name: "http_request", risk: "low", requires_scope: true, description: "HTTP tool" }] };
st.reportMeta = { kind: "html", run: st.detail.runs[0] };
byId["report-frame"].srcdoc = "<html><body>Original Report Body Unchanged</body></html>";
byId["run-goal"].value = "user typed run goal";
byId["hint-input"].value = "user typed hint";
byId["new-name"].value = "user typed project";
byId["token-input"].value = "secret-token";

ctx.__TEST__.applyStaticI18n();
assert.strictEqual(ctx.document.documentElement.lang, "en");

// Switch en -> zh-CN -> ja -> en
ctx.__TEST__.setLanguage("zh-CN");
assert.strictEqual(ctx.document.documentElement.lang, "zh-CN");
assert.strictEqual(storage.get("aria-x.lang"), "zh-CN");
assert.strictEqual(byId["tab-overview-label"].textContent, "概览");
assert.strictEqual(byId["settings-title-label"].textContent, "设置");
assert(byId["report-meta"].textContent.includes("运行 r1"), byId["report-meta"].textContent);
assert.strictEqual(byId["report-frame"].srcdoc, "<html><body>Original Report Body Unchanged</body></html>");

ctx.__TEST__.setLanguage("ja");
assert.strictEqual(ctx.document.documentElement.lang, "ja");
assert.strictEqual(storage.get("aria-x.lang"), "ja");
assert.strictEqual(byId["tab-overview-label"].textContent, "概要");
assert.strictEqual(byId["settings-title-label"].textContent, "設定");
assert(byId["report-meta"].textContent.includes("実行 r1"), byId["report-meta"].textContent);

ctx.__TEST__.setLanguage("en");
assert.strictEqual(ctx.document.documentElement.lang, "en");
assert.strictEqual(storage.get("aria-x.lang"), "en");
assert.strictEqual(byId["tab-overview-label"].textContent, "Overview");

// User inputs, filters, selections, tab, and iframe srcdoc remain intact; 0 network calls
assert.strictEqual(st.tab, "settings");
assert.strictEqual(st.findingFilter, "high");
assert.strictEqual(st.assetFilter, "host");
assert.strictEqual(st.assetSearch, "127");
assert.strictEqual(st.selectedFindingId, "f1");
assert.strictEqual(st.selectedNodeId, "n1");
assert.strictEqual(byId["run-goal"].value, "user typed run goal");
assert.strictEqual(byId["hint-input"].value, "user typed hint");
assert.strictEqual(byId["new-name"].value, "user typed project");
assert.strictEqual(byId["token-input"].value, "secret-token");
assert.strictEqual(byId["report-frame"].srcdoc, "<html><body>Original Report Body Unchanged</body></html>");
assert.strictEqual(fetchCalls, 0);
assert.strictEqual(esCalls, 0);
console.log("WEB_I18N_VM_OK");
"""
        proc = subprocess.run(
            [NODE, "-e", script, str(I18N_JS), str(APP_JS)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WEB_I18N_VM_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()

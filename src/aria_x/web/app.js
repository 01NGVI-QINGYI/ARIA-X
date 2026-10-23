/* ARIA-X console — vanilla ES2020, no build step, no external resources. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ state

  const STORAGE = {
    token: "aria-x.token",
    poll: "aria-x.poll",
    tab: "aria-x.tab",
    project: "aria-x.project",
  };

  const EVENT_TYPES = [
    "run.started", "run.finished", "run.failed", "run.paused", "run.resumed",
    "task.started", "task.finished", "task.failed",
    "node.created", "node.updated", "asset.found", "finding.created",
    "todo.updated", "trace", "log", "llm.call", "tool.call",
    "approval.requested", "approval.decided", "budget", "report.ready",
    "hint", "error",
  ];

  const KIND_STYLE = {
    goal: { color: "#fbbf24", radius: 15, label: true },
    intent: { color: "#22d3ee", radius: 12, label: true },
    fact: { color: "#60a5fa", radius: 8, label: true },
    finding: { color: "#fb7185", radius: 11, label: true },
    hint: { color: "#c084fc", radius: 10, label: true },
    note: { color: "#94a3b8", radius: 7, label: true },
    guide: { color: "#34d399", radius: 9, label: true },
  };

  const SEVERITY_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
  const RUN_TONE = {
    running: "live", paused: "warn", completed: "ok", failed: "bad",
    cancelled: "warn", budget: "warn", created: "idle",
  };

  const state = {
    token: localStorage.getItem(STORAGE.token) || "",
    pollMs: Number(localStorage.getItem(STORAGE.poll)) || 3000,
    tab: localStorage.getItem(STORAGE.tab) || "overview",
    config: null,
    projects: [],
    projectId: localStorage.getItem(STORAGE.project) || "",
    detail: null,
    graph: null,
    run: null,
    currentRunId: "",
    approvals: [],
    findings: [],
    assets: [],
    todos: [],
    selectedNodeId: "",
    selectedFindingId: "",
    findingFilter: "",
    assetFilter: "",
    assetSearch: "",
    events: [],
    eventTypes: new Set(),
    timelinePaused: false,
    timelineFilter: "",
    timelineType: "",
    es: null,
    pollTimer: null,
    refreshing: false,
    graphRefreshTimer: null,
    authWarned: false,
    reportHtml: "",
    reportMd: "",
    blobUrls: [],
  };

  // ---------------------------------------------------------------- helpers

  const $ = (id) => document.getElementById(id);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    }
    return node;
  }

  function replace(node, children) {
    node.replaceChildren(...children.filter(Boolean));
  }

  function num(value) {
    return typeof value === "number" ? value.toLocaleString() : (value === undefined || value === null ? "—" : String(value));
  }

  function sumValues(obj) {
    if (!obj || typeof obj !== "object") return 0;
    return Object.values(obj).reduce((acc, value) => acc + (Number(value) || 0), 0);
  }

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function fmtDateTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ` +
      `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }

  function fmtClock(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "—";
    const exact = Math.max(0, Number(seconds));
    if (exact < 1) return "<1s";
    if (exact < 10) {
      const rounded = Math.round(exact * 10) / 10;
      return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}s`;
    }
    const total = Math.floor(exact);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
    if (minutes) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
    return `${secs}s`;
  }

  function titleCase(value) {
    return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function severityClass(severity) {
    const key = String(severity || "info").toLowerCase();
    return SEVERITY_ORDER[key] !== undefined ? key : "info";
  }

  function setChip(node, tone, text) {
    if (!node) return;
    node.classList.remove("ok", "warn", "bad", "idle", "live");
    node.classList.add(tone || "idle");
    if (text === undefined || text === null) return;
    const label = node.querySelector("[data-chip-label]") ||
      (node.children.length ? node.lastElementChild : node);
    if (label) label.textContent = String(text);
  }

  function toast(message, kind = "ok", timeout = 4500) {
    const wrap = $("toasts");
    if (!wrap) return;
    const node = el("div", `toast ${kind === "ok" ? "" : kind}`, message);
    wrap.appendChild(node);
    window.setTimeout(() => node.remove(), timeout);
  }

  function debounce(fn, wait) {
    let handle = 0;
    return (...args) => {
      window.clearTimeout(handle);
      handle = window.setTimeout(() => fn(...args), wait);
    };
  }

  function isTypingTarget(target) {
    if (!target) return false;
    const tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
  }

  // --------------------------------------------------------------- transport

  async function fetchRaw(path, options = {}) {
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const init = { method: options.method || "GET", headers, cache: "no-store" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, init);
    const text = await response.text();
    return { ok: response.ok, status: response.status, text, headers: response.headers };
  }

  async function api(path, options = {}) {
    const result = await fetchRaw(path, options);
    let data = null;
    if (result.text) {
      try { data = JSON.parse(result.text); }
      catch { data = result.text; }
    }
    if (!result.ok) {
      const error = new Error((data && data.message) || `HTTP ${result.status}`);
      error.status = result.status;
      error.code = data && data.error;
      if (result.status === 401) noteAuthFailure();
      throw error;
    }
    return data;
  }

  function noteAuthFailure() {
    if (state.authWarned) return;
    state.authWarned = true;
    toast("Authentication required — add the Bearer token in Settings.", "warn", 6500);
  }

  // ------------------------------------------------------------------- tabs

  const tabButtons = Array.from(document.querySelectorAll(".tab"));

  function tabFromHash() {
    const raw = (window.location.hash || "").replace(/^#\/?/, "").trim();
    return tabButtons.some((button) => button.dataset.tab === raw) ? raw : "";
  }

  function setTab(name, focus = false) {
    const valid = tabButtons.some((button) => button.dataset.tab === name);
    if (!valid) name = "overview";
    state.tab = name;
    localStorage.setItem(STORAGE.tab, name);
    const wantedHash = `#${name}`;
    if (window.location.hash !== wantedHash) {
      try { window.history.replaceState(null, "", wantedHash); }
      catch { window.location.hash = wantedHash; }
    }
    for (const button of tabButtons) {
      const selected = button.dataset.tab === name;
      button.setAttribute("aria-selected", selected ? "true" : "false");
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    }
    for (const panel of document.querySelectorAll(".panel")) {
      panel.hidden = panel.id !== `panel-${name}`;
    }
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    if (name === "graph") { ensureGraph().then(renderGraph).catch(() => {}); }
    if (name === "report") { loadReport().catch(() => {}); }
    if (name === "settings") { loadSettings().catch(() => {}); }
    if (name === "timeline") { renderTimeline(); scrollTimeline(); }
  }

  function wireTabs() {
    tabButtons.forEach((button, index) => {
      button.addEventListener("click", () => setTab(button.dataset.tab));
      button.addEventListener("keydown", (event) => {
        let target = null;
        if (event.key === "ArrowRight") target = (index + 1) % tabButtons.length;
        else if (event.key === "ArrowLeft") target = (index - 1 + tabButtons.length) % tabButtons.length;
        else if (event.key === "Home") target = 0;
        else if (event.key === "End") target = tabButtons.length - 1;
        if (target === null) return;
        event.preventDefault();
        setTab(tabButtons[target].dataset.tab, true);
      });
    });
  }

  // -------------------------------------------------------------- data load

  async function loadConfig(force = false) {
    if (state.config && !force) return state.config;
    state.config = await api("/api/config");
    renderSettings();
    return state.config;
  }

  async function loadProjects(preferredId) {
    let projects;
    try {
      projects = await api("/api/projects");
    } catch (error) {
      projects = [];
      if (error.status !== 401) toast(`Could not load projects: ${error.message}`, "bad");
    }
    state.projects = Array.isArray(projects) ? projects : [];
    const select = $("project-select");
    const previous = preferredId || state.projectId;
    replace(select, state.projects.map((project) => {
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = project.name || project.id;
      return option;
    }));
    if (!state.projects.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No projects yet";
      select.appendChild(option);
      state.projectId = "";
      clearProject();
      return;
    }
    let chosen = state.projects.find((project) => project.id === previous);
    if (!chosen) chosen = state.projects[0];
    await selectProject(chosen.id);
  }

  async function selectProject(projectId) {
    state.projectId = projectId || "";
    localStorage.setItem(STORAGE.project, state.projectId);
    const select = $("project-select");
    if (select) select.value = state.projectId;
    state.detail = null;
    state.graph = null;
    state.run = null;
    state.selectedNodeId = "";
    state.selectedFindingId = "";
    state.events = [];
    state.eventTypes = new Set();
    renderTimelineEvents();
    if (!state.projectId) { clearProject(); return; }
    connectSSE();
    await refreshProject({ full: true });
  }

  function clearProject() {
    $("overview-empty").hidden = false;
    $("overview-content").hidden = true;
    $("project-name").textContent = "—";
    $("project-goal").textContent = "—";
    $("run-id").textContent = "—";
    $("run-state").className = "chip idle";
    $("run-state").textContent = "created";
    setChip($("run-chip"), "idle", "idle");
    $("btn-start").disabled = true;
    $("btn-stop").disabled = true;
    $("approval-list").replaceChildren(el("p", "muted", "Select an engagement first."));
    $("approval-count").textContent = "0 pending";
    if (state.es) { state.es.close(); state.es = null; }
    setSseStatus(false);
  }

  async function refreshProject({ full = false } = {}) {
    if (!state.projectId || state.refreshing) return;
    state.refreshing = true;
    try {
      const detail = await api(`/api/projects/${encodeURIComponent(state.projectId)}`);
      state.detail = detail;
      state.findings = Array.isArray(detail.findings) ? detail.findings : [];
      state.assets = Array.isArray(detail.assets) ? detail.assets : [];
      state.todos = Array.isArray(detail.todos) ? detail.todos : [];
      const runs = Array.isArray(detail.runs) ? detail.runs : [];
      const active = runs.find((run) => run.status === "running" || run.status === "paused") || runs[0];
      if (active && active.id) {
        state.currentRunId = active.id;
        // Render the run row from the project aggregate immediately; enrich it
        // with budget/tasks when the run endpoint answers. This keeps the chip
        // and elapsed readout correct even if that second request is slow.
        state.run = { run: active, tasks: [], budget: active.budget || {}, usage: active.usage || {} };
        try {
          const full = await api(`/api/runs/${encodeURIComponent(active.id)}`);
          if (full) state.run = full;
        } catch { /* keep the aggregate fallback */ }
      } else {
        state.currentRunId = "";
        state.run = null;
      }
      try {
        state.approvals = await api(`/api/approvals?project_id=${encodeURIComponent(state.projectId)}`);
      } catch { state.approvals = []; }
      renderOverview();
      renderFindings();
      renderAssets();
      renderTodos();
      if (state.tab === "graph") { await ensureGraph(); renderGraph(); }
      if (full && state.tab === "report") { await loadReport(); }
    } catch (error) {
      if (error.status === 404) {
        toast("That engagement no longer exists.", "warn");
        await loadProjects();
      } else if (error.status !== 401) {
        toast(`Refresh failed: ${error.message}`, "bad");
      }
    } finally {
      state.refreshing = false;
    }
  }

  const debouncedRefresh = debounce(() => refreshProject().catch(() => {}), 500);

  // --------------------------------------------------------------- overview

  function scopeSummary(scope) {
    if (!scope || typeof scope !== "object") return "not declared";
    const targets = Array.isArray(scope.targets) ? scope.targets : [];
    if (!targets.length) return "no targets";
    return targets.join(", ");
  }

  function metaItem(label, value) {
    const wrap = document.createElement("div");
    wrap.appendChild(el("dt", null, label));
    wrap.appendChild(el("dd", null, value));
    return wrap;
  }

  function currentBudget() {
    const run = state.run || {};
    const engine = run.engine || {};
    const candidates = [engine.budget, run.budget, run.run && run.run.budget];
    let source = null;
    for (const candidate of candidates) {
      if (candidate && typeof candidate === "object" &&
          (candidate.usage || candidate.limits || candidate.max_seconds !== undefined)) {
        source = candidate;
        if (candidate.usage) break;
      }
    }
    source = source || {};
    const limits = source.limits || (source.max_seconds !== undefined ? source : {});
    const usage = source.usage || run.usage || {};
    let elapsed = source.elapsed_seconds;
    const record = run.run || {};
    if (elapsed === undefined && record.started_at) {
      const start = Date.parse(record.started_at);
      const end = record.ended_at ? Date.parse(record.ended_at) : Date.now();
      if (!Number.isNaN(start) && !Number.isNaN(end)) elapsed = Math.max(0, (end - start) / 1000);
    }
    const tokens = usage.total_tokens !== undefined
      ? usage.total_tokens
      : (Number(usage.prompt_tokens || 0) + Number(usage.completion_tokens || 0));
    return {
      limits, usage, tokens,
      elapsed: typeof elapsed === "number" ? elapsed : null,
      intents: source.intents_started !== undefined ? source.intents_started : engine.intents_started,
      hasData: Boolean(Object.keys(limits).length || Object.keys(usage).length || elapsed !== undefined),
    };
  }

  function statCard(kind, label, value, sub) {
    const card = el("div", `stat ${kind || ""}`);
    card.appendChild(el("div", "stat-label", label));
    card.appendChild(el("div", "stat-value", value));
    if (sub) card.appendChild(el("div", "stat-sub", sub));
    return card;
  }

  function renderOverview() {
    const detail = state.detail;
    if (!detail || !detail.project) {
      $("overview-empty").hidden = false;
      $("overview-content").hidden = true;
      return;
    }
    $("overview-empty").hidden = true;
    $("overview-content").hidden = false;

    const project = detail.project;
    const counts = detail.counts || {};
    const nodes = counts.nodes || {};
    const assets = counts.assets || {};
    const findings = state.findings.length;
    const findingsBySeverity = counts.findings_by_severity || {};
    const budget = currentBudget();
    const record = (state.run && state.run.run) || {};

    $("project-name").textContent = project.name || project.id;
    $("project-goal").textContent = project.goal || "No goal recorded.";
    setChip($("project-status"), project.status === "active" ? "ok" : "idle", project.status || "active");

    const meta = $("project-meta");
    replace(meta, [
      metaItem("Project ID", project.id),
      metaItem("Created", fmtDateTime(project.created_at)),
      metaItem("Scope", scopeSummary(project.scope)),
      metaItem("Runs", String((detail.runs || []).length)),
    ]);

    // stats -------------------------------------------------------------
    const severitySub = Object.entries(findingsBySeverity)
      .sort((a, b) => (SEVERITY_ORDER[a[0]] ?? 9) - (SEVERITY_ORDER[b[0]] ?? 9))
      .map(([severity, value]) => `${value} ${severity}`)
      .join(" · ");
    replace($("overview-stats"), [
      statCard("k-facts", "Facts", num(nodes.fact || 0), "observed truths"),
      statCard("k-intents", "Intents", num(nodes.intent || 0), "open intent chain"),
      statCard("k-findings", "Findings", num(findings), severitySub || "none yet"),
      statCard("k-assets", "Assets", num(sumValues(assets)), `${Object.keys(assets).length} kinds`),
      statCard("k-todos", "Todos", num(counts.todos || state.todos.length), "shared plan chain"),
      statCard("k-traces", "Traces", num(counts.traces || 0), "process traces"),
      statCard("k-tokens", "Tokens", num(budget.tokens || 0), "prompt + completion"),
      statCard("k-tools", "Tool calls", num(budget.usage.tool_calls || 0), "tool invocations"),
    ]);

    renderRun(budget);
    renderBudget(budget);
    renderApprovals();
  }

  function renderRun(budget) {
    const record = (state.run && state.run.run) ||
      (state.detail && Array.isArray(state.detail.runs) ? state.detail.runs[0] : null) || null;
    const status = record ? record.status : "idle";
    setChip($("run-state"), RUN_TONE[status] || "idle", status);
    setChip($("run-chip"), RUN_TONE[status] || "idle", status);
    $("run-id").textContent = record ? record.id : "—";
    $("run-elapsed").textContent = record ? fmtDuration(budget.elapsed) : "—";
    const running = Boolean(record && (record.status === "running" || record.status === "paused"));
    $("btn-start").disabled = running;
    $("btn-stop").disabled = !running;
    $("run-goal").placeholder = state.detail && state.detail.project
      ? `Defaults to: ${state.detail.project.goal}` : "Defaults to the project goal…";

    const tasks = (state.run && Array.isArray(state.run.tasks)) ? state.run.tasks : [];
    const wrap = $("run-tasks-wrap");
    wrap.hidden = tasks.length === 0;
    replace($("run-tasks"), tasks.slice(-20).map((task) => {
      const li = document.createElement("li");
      li.appendChild(el("span", "task-type", task.type || "task"));
      li.appendChild(el("span", `task-status ${task.status || ""}`, task.status || "queued"));
      li.appendChild(el("span", null, task.agent || task.intent_id || task.id));
      return li;
    }));
  }

  function budgetBar(label, used, maximum, format, tone) {
    const row = el("div", "budget-row");
    const head = el("div", "budget-head");
    head.appendChild(el("span", "name", label));
    head.appendChild(el("span", "value", format));
    row.appendChild(head);
    const bar = el("div", "bar");
    const fill = el("div", `bar-fill ${tone || ""}`);
    let percent = 0;
    if (Number(maximum) > 0) percent = Math.max(0, Math.min(100, (Number(used) / Number(maximum)) * 100));
    fill.style.width = `${percent.toFixed(1)}%`;
    if (percent >= 95) fill.classList.add("bad");
    else if (percent >= 75) fill.classList.add("warn");
    bar.appendChild(fill);
    row.appendChild(bar);
    return row;
  }

  function renderBudget(budget) {
    const container = $("budget-bars");
    if (!budget.hasData) {
      replace(container, [el("p", "muted", "No budget data yet — start a run.")]);
      return;
    }
    const limits = budget.limits || {};
    const usage = budget.usage || {};
    const rows = [];
    if (limits.max_seconds) {
      rows.push(budgetBar("Elapsed time", budget.elapsed || 0, limits.max_seconds,
        `${fmtDuration(budget.elapsed || 0)} / ${fmtDuration(limits.max_seconds)}`, "cyan"));
    }
    if (limits.max_llm_calls) {
      rows.push(budgetBar("LLM calls", usage.llm_calls || 0, limits.max_llm_calls,
        `${num(usage.llm_calls || 0)} / ${num(limits.max_llm_calls)}`));
    }
    if (limits.max_tokens) {
      rows.push(budgetBar("Tokens", budget.tokens || 0, limits.max_tokens,
        `${num(budget.tokens || 0)} / ${num(limits.max_tokens)}`));
    }
    if (limits.max_tool_calls) {
      rows.push(budgetBar("Tool calls", usage.tool_calls || 0, limits.max_tool_calls,
        `${num(usage.tool_calls || 0)} / ${num(limits.max_tool_calls)}`, "cyan"));
    }
    if (limits.max_total_intents) {
      rows.push(budgetBar("Intents", budget.intents || usage.intents_started || 0, limits.max_total_intents,
        `${num(budget.intents || 0)} / ${num(limits.max_total_intents)}`, "cyan"));
    }
    if (!rows.length) {
      rows.push(el("p", "muted", "Budget limits are not published for this run."));
    }
    replace(container, rows);
  }

  // -------------------------------------------------------------- approvals

  function renderApprovals() {
    const list = $("approval-list");
    const approvals = Array.isArray(state.approvals) ? state.approvals : [];
    const badge = $("approval-count");
    badge.textContent = `${approvals.length} pending`;
    badge.className = `chip ${approvals.length ? "warn" : "idle"}`;
    if (!approvals.length) {
      replace(list, [el("p", "muted", "No approvals waiting. Risky actions appear here.")]);
      return;
    }
    replace(list, approvals.map((approval) => {
      const card = el("div", "approval");
      const head = el("div", "approval-head");
      head.appendChild(el("span", "approval-tool", approval.tool || "tool"));
      head.appendChild(el("span", `chip ${approval.risk === "critical" || approval.risk === "high" ? "bad" : "warn"}`,
        approval.risk || "medium"));
      head.appendChild(el("span", "approval-meta", `${approval.task_id || "task"} · ${fmtClock(approval.created_at)}`));
      card.appendChild(head);
      if (approval.reason) card.appendChild(el("p", "approval-meta", approval.reason));
      const args = el("pre", "approval-args", JSON.stringify(approval.args || {}, null, 2));
      card.appendChild(args);
      const actions = el("div", "approval-actions");
      const approve = el("button", "btn primary small", "Approve");
      approve.type = "button";
      approve.addEventListener("click", () => decideApproval(approval.id, true, approve));
      const deny = el("button", "btn danger small", "Deny");
      deny.type = "button";
      deny.addEventListener("click", () => decideApproval(approval.id, false, deny));
      actions.append(approve, deny);
      card.appendChild(actions);
      return card;
    }));
  }

  async function decideApproval(approvalId, approved, button) {
    if (button) button.disabled = true;
    try {
      await api(`/api/approvals/${encodeURIComponent(approvalId)}`,
        { method: "POST", body: { approved, reason: "" } });
      toast(approved ? "Approval granted." : "Approval denied.", approved ? "ok" : "warn");
      await refreshProject();
    } catch (error) {
      toast(`Approval failed: ${error.message}`, "bad");
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- run ops

  async function startRun() {
    if (!state.projectId) { toast("Select or create an engagement first.", "warn"); return; }
    const button = $("btn-start");
    button.disabled = true;
    const goal = $("run-goal").value.trim();
    try {
      const body = goal ? { goal } : {};
      const result = await api(`/api/projects/${encodeURIComponent(state.projectId)}/runs`,
        { method: "POST", body });
      toast(`Run ${result.run_id} started.`);
      $("run-goal").value = "";
      await refreshProject();
      connectSSE();
    } catch (error) {
      toast(`Could not start run: ${error.message}`, "bad");
      button.disabled = false;
    }
  }

  async function stopRun() {
    if (!state.currentRunId) { toast("No run to stop.", "warn"); return; }
    const button = $("btn-stop");
    button.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(state.currentRunId)}/stop`, { method: "POST", body: {} });
      toast("Stop requested.", "warn");
      await refreshProject();
    } catch (error) {
      toast(`Could not stop run: ${error.message}`, "bad");
      button.disabled = false;
    }
  }

  async function submitHint(event) {
    event.preventDefault();
    if (!state.projectId) { toast("Select an engagement first.", "warn"); return; }
    const input = $("hint-input");
    const content = input.value.trim();
    if (!content) { toast("Write a hint first.", "warn"); return; }
    const button = $("hint-submit");
    button.disabled = true;
    try {
      await api(`/api/projects/${encodeURIComponent(state.projectId)}/hints`,
        { method: "POST", body: { content } });
      input.value = "";
      toast("Hint recorded.");
      await refreshProject();
    } catch (error) {
      toast(`Hint failed: ${error.message}`, "bad");
    } finally {
      button.disabled = false;
    }
  }

  async function createProject(event) {
    event.preventDefault();
    if (!$("new-authorized").checked) {
      toast("Confirm that you are authorized to test these targets.", "warn");
      $("new-authorized").focus();
      return;
    }
    const name = $("new-name").value.trim();
    const goal = $("new-goal").value.trim();
    const targets = $("new-targets").value.split(",").map((item) => item.trim()).filter(Boolean);
    if (!name || !goal) { toast("Name and goal are required.", "warn"); return; }
    if (!targets.length) { toast("Add at least one authorized target.", "warn"); $("new-targets").focus(); return; }
    try {
      const project = await api("/api/projects", {
        method: "POST",
        body: {
          name, goal,
          scope: { authorized: true, targets, deny: [], allow_private: true, allow_loopback: true },
        },
      });
      toast(`Engagement “${project.name}” created.`);
      $("create-form").reset();
      $("new-authorized").checked = true;
      await loadProjects(project.id);
    } catch (error) {
      toast(`Could not create engagement: ${error.message}`, "bad");
    }
  }

  // ---------------------------------------------------------------- findings

  function renderFindings() {
    const findings = (state.findings || []).slice();
    const filter = state.findingFilter;
    const filtered = filter ? findings.filter((node) => severityClass(node.meta && node.meta.severity) === filter) : findings;
    filtered.sort((a, b) => {
      const sa = SEVERITY_ORDER[severityClass(a.meta && a.meta.severity)] ?? 9;
      const sb = SEVERITY_ORDER[severityClass(b.meta && b.meta.severity)] ?? 9;
      if (sa !== sb) return sa - sb;
      return String(b.created_at || "").localeCompare(String(a.created_at || ""));
    });
    $("findings-count").textContent = String(findings.length);
    const badge = $("tab-findings-count");
    badge.hidden = findings.length === 0;
    badge.textContent = String(findings.length);

    const body = $("findings-body");
    replace(body, filtered.map((node) => {
      const meta = node.meta || {};
      const row = document.createElement("tr");
      row.tabIndex = 0;
      row.dataset.id = node.id;
      row.setAttribute("aria-selected", node.id === state.selectedFindingId ? "true" : "false");
      const severity = severityClass(meta.severity);
      const sevCell = document.createElement("td");
      sevCell.appendChild(el("span", `sev sev-${severity}`, severity));
      row.appendChild(sevCell);
      const titleCell = el("td", "title-cell", node.title || "(untitled finding)");
      row.appendChild(titleCell);
      const confidence = document.createElement("td");
      confidence.appendChild(el("span", "mono", meta.confidence || "—"));
      row.appendChild(confidence);
      row.appendChild(el("td", null, node.status || "—"));
      const created = document.createElement("td");
      created.appendChild(el("span", "mono", fmtDateTime(node.created_at)));
      row.appendChild(created);
      row.addEventListener("click", () => selectFinding(node.id));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectFinding(node.id);
        }
      });
      return row;
    }));
    $("findings-empty").hidden = filtered.length > 0;
    let selected = filtered.find((node) => node.id === state.selectedFindingId) || null;
    if (!selected && filtered.length) selected = filtered[0];
    state.selectedFindingId = selected ? selected.id : "";
    for (const row of document.querySelectorAll("#findings-body tr")) {
      row.setAttribute("aria-selected", row.dataset.id === state.selectedFindingId ? "true" : "false");
    }
    renderFindingDetail(selected);
  }

  function selectFinding(nodeId) {
    state.selectedFindingId = nodeId;
    for (const row of document.querySelectorAll("#findings-body tr")) {
      row.setAttribute("aria-selected", row.dataset.id === nodeId ? "true" : "false");
    }
    const node = (state.findings || []).find((item) => item.id === nodeId);
    renderFindingDetail(node || null);
  }

  function renderFindingDetail(node) {
    const pane = $("finding-detail");
    if (!node) {
      replace(pane, [el("p", "muted", "Select a finding row to view its body and evidence anchors.")]);
      return;
    }
    const meta = node.meta || {};
    const severity = severityClass(meta.severity);
    const head = el("div", "detail-meta");
    head.appendChild(el("span", `sev sev-${severity}`, severity));
    head.appendChild(el("span", null, `status: ${node.status || "—"}`));
    head.appendChild(el("span", null, `confidence: ${meta.confidence || "—"}`));
    if (meta.cvss !== undefined && meta.cvss !== null) head.appendChild(el("span", null, `CVSS: ${meta.cvss}`));
    const children = [
      el("h3", null, node.title || "(untitled finding)"),
      head,
      el("div", "body-text", node.body || "No body recorded."),
    ];
    const metaKeys = Object.keys(meta).filter((key) => !["severity", "confidence", "cvss"].includes(key));
    if (metaKeys.length) {
      const block = el("details", "detail-meta");
      block.appendChild(el("summary", null, "metadata"));
      block.appendChild(el("pre", "code-block", JSON.stringify(meta, null, 2)));
      children.push(block);
    }
    children.push(el("p", "detail-meta", `node ${node.id} · created ${fmtDateTime(node.created_at)}`));
    replace(pane, children);
  }

  // ------------------------------------------------------------------ assets

  function renderAssets() {
    const assets = (state.assets || []).slice();
    const filter = state.assetFilter;
    const query = state.assetSearch.trim().toLowerCase();
    const filtered = assets.filter((asset) => {
      if (filter && asset.kind !== filter) return false;
      if (query && !String(asset.value || "").toLowerCase().includes(query)) return false;
      return true;
    });
    $("assets-count").textContent = String(assets.length);
    replace($("assets-body"), filtered.map((asset) => {
      const row = document.createElement("tr");
      row.title = asset.id ? `id: ${asset.id}` : "";
      const kindCell = document.createElement("td");
      kindCell.appendChild(el("span", "chip", asset.kind || "asset"));
      row.appendChild(kindCell);
      const valueCell = el("td", "title-cell", asset.value || "—");
      valueCell.style.fontFamily = "var(--mono)";
      row.appendChild(valueCell);
      const first = document.createElement("td");
      first.appendChild(el("span", "mono", fmtDateTime(asset.first_seen)));
      row.appendChild(first);
      const last = document.createElement("td");
      last.appendChild(el("span", "mono", fmtDateTime(asset.last_seen)));
      row.appendChild(last);
      return row;
    }));
    $("assets-empty").hidden = filtered.length > 0;
  }

  // ------------------------------------------------------------------- todos

  function renderTodos() {
    const todos = (state.todos || []).slice().sort((a, b) =>
      (Number(a.position) || 0) - (Number(b.position) || 0));
    $("todos-count").textContent = String(todos.length);
    replace($("todos-list"), todos.map((todo) => {
      const item = el("li", "todo");
      item.appendChild(el("span", "todo-pos", `#${String(todo.position || 0).padStart(2, "0")}`));
      item.appendChild(el("span", `todo-status ${todo.status || "pending"}`, todo.status || "pending"));
      const text = document.createElement("div");
      text.appendChild(el("div", "todo-title", todo.title || "(untitled)"));
      if (todo.detail) text.appendChild(el("div", "todo-detail", todo.detail));
      if (Array.isArray(todo.depends_on) && todo.depends_on.length) {
        text.appendChild(el("div", "todo-deps", `depends on: ${todo.depends_on.join(", ")}`));
      }
      item.appendChild(text);
      return item;
    }));
    $("todos-empty").hidden = todos.length > 0;
  }

  // ------------------------------------------------------------------- graph

  async function ensureGraph() {
    if (!state.projectId) return;
    try {
      state.graph = await api(`/api/projects/${encodeURIComponent(state.projectId)}/graph`);
    } catch (error) {
      if (error.status !== 401) toast(`Graph unavailable: ${error.message}`, "bad");
    }
  }

  const GRAPH_LAYERS = [
    { kinds: ["goal"], label: "GOAL" },
    { kinds: ["intent"], label: "INTENT" },
    { kinds: ["fact"], label: "FACT" },
    { kinds: ["finding"], label: "FINDING" },
    { kinds: ["hint", "note", "guide"], label: "CONTEXT" },
  ];

  function graphLayerFor(kind) {
    if (kind === "goal") return 0;
    if (kind === "intent") return 1;
    if (kind === "fact") return 2;
    if (kind === "finding") return 3;
    return 4;
  }

  function layoutLayered(nodes, width) {
    // Deterministic lane layout: goal on top, then intents, facts and findings.
    // Nodes within one lane are spread evenly (the only "spring" is horizontal
    // spacing inside a lane), so the same graph always renders identically and
    // no cross-layer attraction can collapse the hierarchy.
    const groups = GRAPH_LAYERS.map(() => []);
    for (const node of nodes) groups[graphLayerFor(node.kind)].push(node);
    const positions = new Map();
    const layerLabels = [];
    // Gutter on both sides fits a 30-character centred label without
    // colliding with the lane captions or the canvas edge.
    const left = 200;
    const right = width - 120;
    const perRow = Math.max(2, Math.min(8, Math.floor((right - left) / 118)));
    const rowHeight = 82;
    const bandPadding = 24;
    const bandGap = 26;
    let y = 24;
    for (let index = 0; index < groups.length; index += 1) {
      const items = groups[index];
      if (!items.length) continue;
      const rows = Math.ceil(items.length / perRow);
      const bandTop = y;
      const bandHeight = rows * rowHeight + bandPadding;
      layerLabels.push({ label: GRAPH_LAYERS[index].label, y: bandTop + bandHeight / 2, top: bandTop });
      for (let row = 0; row < rows; row += 1) {
        const rowItems = items.slice(row * perRow, (row + 1) * perRow);
        const spacing = rowItems.length > 1 ? (right - left) / (rowItems.length - 1) : 0;
        rowItems.forEach((node, column) => {
          const x = rowItems.length === 1 ? (left + right) / 2 : left + column * spacing;
          positions.set(node.id, {
            x,
            y: bandTop + 36 + row * rowHeight,
            labelAbove: node.kind === "goal" || (row + column) % 2 === 1,
          });
        });
      }
      y = bandTop + bandHeight + bandGap;
    }
    return { positions, layerLabels, height: Math.max(560, Math.round(y + 8)) };
  }

  function renderGraph() {
    const svg = $("graph-svg");
    const empty = $("graph-empty");
    const graph = state.graph;
    const nodes = (graph && Array.isArray(graph.nodes)) ? graph.nodes : [];
    const edges = (graph && Array.isArray(graph.edges)) ? graph.edges : [];
    const assets = (graph && Array.isArray(graph.assets)) ? graph.assets : [];
    $("graph-count").textContent = `${nodes.length} nodes · ${edges.length} edges · ${assets.length} assets`;
    if (!nodes.length) {
      // SVGElement does not reflect the HTMLElement .hidden IDL property, so
      // toggle the attribute explicitly (otherwise the graph stays display:none).
      svg.setAttribute("hidden", "");
      empty.hidden = false;
      replace($("graph-detail"), [el("p", "muted", "Select a node to inspect its fact, intent, hint or finding.")]);
      return;
    }
    empty.hidden = true;
    svg.removeAttribute("hidden");
    const width = 960;
    const layout = layoutLayered(nodes, width);
    const positions = layout.positions;
    svg.setAttribute("viewBox", `0 0 ${width} ${layout.height}`);
    const defs = svgEl("defs");
    const marker = svgEl("marker", {
      id: "aria-x-arrow", markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: "auto",
    });
    marker.appendChild(svgEl("path", { d: "M0,0 L8,4 L0,8 z", fill: "#4a6b88" }));
    defs.appendChild(marker);
    const layerLayer = svgEl("g", { "aria-hidden": "true" });
    for (const layer of layout.layerLabels) {
      layerLayer.appendChild(svgEl("line", {
        x1: 96, y1: layer.top.toFixed(1), x2: width - 24, y2: layer.top.toFixed(1),
        class: "layer-rule",
      }));
      const layerLabel = svgEl("text", { x: 22, y: layer.y.toFixed(1), class: "layer-label" });
      layerLabel.textContent = layer.label;
      layerLayer.appendChild(layerLabel);
    }
    const edgeLayer = svgEl("g", { "aria-hidden": "true" });
    const nodeLayer = svgEl("g", {});
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    for (const edge of edges) {
      const a = positions.get(edge.src_id);
      const b = positions.get(edge.dst_id);
      if (!a || !b) continue;
      const source = nodeById.get(edge.src_id) || {};
      const target = nodeById.get(edge.dst_id) || {};
      const sourceRadius = (KIND_STYLE[source.kind] || KIND_STYLE.note).radius;
      const targetRadius = (KIND_STYLE[target.kind] || KIND_STYLE.note).radius;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const ux = dx / distance;
      const uy = dy / distance;
      const line = svgEl("line", {
        x1: (a.x + ux * (sourceRadius + 3)).toFixed(1),
        y1: (a.y + uy * (sourceRadius + 3)).toFixed(1),
        x2: (b.x - ux * (targetRadius + 7)).toFixed(1),
        y2: (b.y - uy * (targetRadius + 7)).toFixed(1),
        class: "edge", "marker-end": "url(#aria-x-arrow)",
      });
      const title = svgEl("title");
      title.textContent = `${edge.rel || "edge"}: ${(nodeById.get(edge.src_id) || {}).title || edge.src_id} → ${(nodeById.get(edge.dst_id) || {}).title || edge.dst_id}`;
      line.appendChild(title);
      edgeLayer.appendChild(line);
    }
    for (const node of nodes) {
      const pos = positions.get(node.id);
      const style = KIND_STYLE[node.kind] || KIND_STYLE.note;
      const group = svgEl("g", {
        class: `node-group node-${KIND_STYLE[node.kind] ? node.kind : "note"}${node.id === state.selectedNodeId ? " node-selected" : ""}`,
        tabindex: "0", role: "button",
        "aria-label": `${node.kind}: ${node.title || "(untitled)"} [${node.status || "open"}]`,
        "data-id": node.id,
      });
      group.appendChild(svgEl("circle", { class: "node-halo", cx: pos.x, cy: pos.y, r: style.radius + 8 }));
      group.appendChild(svgEl("circle", { class: "node-ring", cx: pos.x, cy: pos.y, r: style.radius + 3.5 }));
      group.appendChild(svgEl("circle", { class: "node-circle", cx: pos.x, cy: pos.y, r: style.radius }));
      if (style.label) {
        const label = svgEl("text", {
          x: pos.x,
          y: pos.labelAbove ? pos.y - style.radius - 8 : pos.y + style.radius + 14,
          class: "node-label", "text-anchor": "middle",
        });
        const text = String(node.title || node.kind || "");
        label.textContent = text.length > 30 ? `${text.slice(0, 29)}…` : text;
        group.appendChild(label);
      }
      const title = svgEl("title");
      title.textContent = `${node.kind}: ${node.title || ""}\n${node.status || "open"}`;
      group.appendChild(title);
      group.addEventListener("click", () => selectNode(node.id));
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectNode(node.id);
        }
      });
      nodeLayer.appendChild(group);
    }
    svg.replaceChildren(defs, layerLayer, edgeLayer, nodeLayer);
    renderGraphLegend(nodes);
    const selected = nodes.find((node) => node.id === state.selectedNodeId);
    renderNodeDetail(selected || null, graph);
  }

  function renderGraphLegend(nodes) {
    const present = new Set(nodes.map((node) => (KIND_STYLE[node.kind] ? node.kind : "note")));
    const order = ["goal", "intent", "fact", "finding", "hint", "guide", "note"];
    replace($("graph-legend"), order.filter((kind) => present.has(kind)).map((kind) => {
      const item = el("span", "legend-item");
      const swatch = el("span", "legend-swatch");
      swatch.style.background = KIND_STYLE[kind].color;
      item.append(swatch, el("span", null, kind));
      return item;
    }));
  }

  function selectNode(nodeId) {
    state.selectedNodeId = nodeId;
    for (const group of document.querySelectorAll("#graph-svg .node-group")) {
      group.classList.toggle("node-selected", group.getAttribute("data-id") === nodeId);
    }
    const nodes = (state.graph && state.graph.nodes) || [];
    const node = nodes.find((item) => item.id === nodeId) || null;
    renderNodeDetail(node, state.graph);
  }

  function renderNodeDetail(node, graph) {
    const pane = $("graph-detail");
    if (!node) {
      replace(pane, [el("p", "muted", "Select a node to inspect its fact, intent, hint or finding.")]);
      return;
    }
    const meta = node.meta || {};
    const children = [];
    const kind = KIND_STYLE[node.kind] ? node.kind : "note";
    const kindChip = el("span", "chip", node.kind || "note");
    kindChip.style.borderColor = KIND_STYLE[kind].color;
    kindChip.style.color = KIND_STYLE[kind].color;
    const metaRow = el("div", "detail-meta");
    metaRow.appendChild(kindChip);
    metaRow.appendChild(el("span", null, `status: ${node.status || "open"}`));
    if (meta.severity) metaRow.appendChild(el("span", `sev sev-${severityClass(meta.severity)}`, meta.severity));
    children.push(el("h3", null, node.title || "(untitled node)"));
    children.push(metaRow);
    children.push(el("div", "body-text", node.body || "No body recorded."));
    if (meta.confidence || meta.cvss) {
      children.push(el("p", "detail-meta",
        [meta.confidence ? `confidence: ${meta.confidence}` : "", meta.cvss ? `CVSS: ${meta.cvss}` : ""]
          .filter(Boolean).join(" · ")));
    }
    children.push(el("p", "detail-meta", `node ${node.id} · seq ${node.seq ?? "—"}`));

    const edges = (graph && graph.edges) || [];
    const nodes = (graph && graph.nodes) || [];
    const byId = new Map(nodes.map((item) => [item.id, item]));
    const neighbors = [];
    for (const edge of edges) {
      if (edge.src_id === node.id && byId.has(edge.dst_id)) {
        neighbors.push({ dir: "→", rel: edge.rel, node: byId.get(edge.dst_id) });
      } else if (edge.dst_id === node.id && byId.has(edge.src_id)) {
        neighbors.push({ dir: "←", rel: edge.rel, node: byId.get(edge.src_id) });
      }
    }
    if (neighbors.length) {
      const box = el("div", "graph-nbr");
      box.appendChild(el("div", "stat-label", "Connections"));
      for (const neighbor of neighbors.slice(0, 12)) {
        const button = el("button", null, `${neighbor.dir} ${neighbor.rel || "related"}: ${neighbor.node.title || neighbor.node.id}`);
        button.type = "button";
        button.addEventListener("click", () => selectNode(neighbor.node.id));
        box.appendChild(button);
      }
      children.push(box);
    }
    replace(pane, children);
  }

  // ---------------------------------------------------------------- timeline

  function passesTimelineFilters(item) {
    if (state.timelineType && item.type !== state.timelineType) return false;
    const query = state.timelineFilter.trim().toLowerCase();
    if (!query) return true;
    const haystack = `${item.type} ${JSON.stringify(item.data || {})}`.toLowerCase();
    return haystack.includes(query);
  }

  function summarizeEvent(item) {
    const data = item.data || {};
    for (const key of ["message", "title", "reason", "summary", "error", "status", "command", "url"]) {
      if (typeof data[key] === "string" && data[key].trim()) return data[key];
    }
    const keys = Object.keys(data);
    if (!keys.length) return "";
    let text;
    try { text = JSON.stringify(data); } catch { text = String(data); }
    return text.length > 180 ? `${text.slice(0, 179)}…` : text;
  }

  function timelineRow(item) {
    const row = el("li", "tl-item");
    row.dataset.eventId = item.id;
    row.appendChild(el("span", "tl-time", fmtClock(item.ts)));
    row.appendChild(el("span", `tl-type type-${String(item.type).replace(/\./g, "\\.")}`, item.type || "event"));
    const message = el("div", "tl-message");
    message.appendChild(el("div", null, summarizeEvent(item) || "(no payload)"));
    const details = document.createElement("details");
    details.appendChild(el("summary", null, "data"));
    let pretty;
    try { pretty = JSON.stringify(item.data, null, 2); } catch { pretty = String(item.data); }
    details.appendChild(el("pre", null, pretty));
    message.appendChild(details);
    row.appendChild(message);
    return row;
  }

  function appendTimelineItem(item) {
    if (!passesTimelineFilters(item)) return;
    const log = $("timeline-log");
    log.appendChild(timelineRow(item));
    while (log.children.length > 600) log.firstElementChild.remove();
    $("timeline-empty").hidden = true;
    $("timeline-pulse").classList.add("on");
    window.setTimeout(() => $("timeline-pulse").classList.remove("on"), 900);
    scrollTimeline();
  }

  function renderTimeline() {
    const filtered = state.events.filter(passesTimelineFilters);
    const log = $("timeline-log");
    replace(log, filtered.slice(-600).map(timelineRow));
    $("timeline-empty").hidden = filtered.length > 0;
    $("timeline-count").textContent = String(state.events.length);
    scrollTimeline();
  }

  function renderTimelineEvents() {
    renderTimeline();
  }

  function scrollTimeline() {
    if (!$("timeline-autoscroll").checked || state.timelinePaused) return;
    const log = $("timeline-log");
    log.scrollTop = log.scrollHeight;
  }

  function updateTypeSelect() {
    const select = $("timeline-type");
    const current = select.value;
    const types = Array.from(state.eventTypes).sort();
    replace(select, [(() => {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "All types";
      return option;
    })(), ...types.map((type) => {
      const option = document.createElement("option");
      option.value = type;
      option.textContent = type;
      return option;
    })]);
    select.value = types.includes(current) ? current : "";
  }

  function onSseEvent(event) {
    let data = {};
    if (event.data) {
      try { data = JSON.parse(event.data); } catch { data = { raw: event.data }; }
    }
    const item = {
      id: event.lastEventId || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      type: event.type || "message",
      ts: (data && data.ts) || new Date().toISOString(),
      data,
    };
    state.events.push(item);
    if (state.events.length > 800) state.events.splice(0, state.events.length - 800);
    const isNewType = !state.eventTypes.has(item.type);
    state.eventTypes.add(item.type);
    if (isNewType) updateTypeSelect();
    $("timeline-count").textContent = String(state.events.length);
    if (!state.timelinePaused) appendTimelineItem(item);
    maybeRefreshForEvent(item.type);
  }

  function maybeRefreshForEvent(type) {
    if (!type) return;
    if (type.startsWith("run.") || type.startsWith("task.") || type.startsWith("node.") ||
        type.startsWith("finding.") || type.startsWith("asset.") || type.startsWith("todo.") ||
        type.startsWith("approval.") || type === "hint" || type === "budget") {
      debouncedRefresh();
      if (state.tab === "graph") {
        window.clearTimeout(state.graphRefreshTimer);
        state.graphRefreshTimer = window.setTimeout(() => {
          ensureGraph().then(renderGraph).catch(() => {});
        }, 700);
      }
    }
    if (type === "report.ready" && state.tab === "report") {
      loadReport().catch(() => {});
    }
  }

  function setSseStatus(connected) {
    const pill = $("conn-pill");
    const text = $("conn-text");
    pill.classList.remove("ok", "bad", "idle", "live", "warn");
    if (connected) {
      pill.classList.add("live");
      text.textContent = "live";
      $("sse-status").textContent = "stream live";
    } else {
      pill.classList.add("idle");
      text.textContent = "offline";
      $("sse-status").textContent = "stream offline";
    }
    $("timeline-pulse").classList.toggle("on", Boolean(connected));
  }

  function connectSSE() {
    if (state.es) {
      state.es.close();
      state.es = null;
    }
    setSseStatus(false);
    if (!state.projectId) return;
    const token = state.token ? `&token=${encodeURIComponent(state.token)}` : "";
    const source = new EventSource(`/api/projects/${encodeURIComponent(state.projectId)}/events?replay=150${token}`);
    state.es = source;
    source.onopen = () => setSseStatus(true);
    source.onerror = () => {
      setSseStatus(false);
      if ((state.config && state.config.auth_required) && !state.token) noteAuthFailure();
    };
    source.onmessage = onSseEvent;
    for (const type of EVENT_TYPES) source.addEventListener(type, onSseEvent);
  }

  // ------------------------------------------------------------------ report

  function revokeBlobUrls() {
    for (const url of state.blobUrls) URL.revokeObjectURL(url);
    state.blobUrls = [];
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setReportLinks(openUrl, markdownUrl) {
    const open = $("report-open");
    const markdown = $("report-markdown");
    if (openUrl) { open.href = openUrl; open.setAttribute("aria-disabled", "false"); }
    else { open.href = "#"; open.setAttribute("aria-disabled", "true"); }
    if (markdownUrl) { markdown.href = markdownUrl; markdown.setAttribute("aria-disabled", "false"); }
    else { markdown.href = "#"; markdown.setAttribute("aria-disabled", "true"); }
  }

  function stripReportDuplicateHeading(html) {
    try {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const headings = Array.from(doc.querySelectorAll("h1"));
      if (headings.length >= 2) {
        const hero = headings[0];
        const body = doc.querySelector("main h1") || headings[1];
        if (body && body !== hero && body.textContent.trim() === hero.textContent.trim()) {
          body.remove();
        }
      }
      return `<!doctype html>${doc.documentElement.outerHTML}`;
    } catch {
      return html;
    }
  }

  function stripLeadingMarkdownHeading(markdown) {
    return String(markdown || "").replace(/^\uFEFF?\s*#\s+[^\r\n]+(?:\r?\n)+/, "");
  }

  async function loadReport() {
    if (!state.projectId) {
      $("report-empty").hidden = false;
      $("report-frame").hidden = true;
      $("report-meta").textContent = "No engagement selected";
      return;
    }
    const frame = $("report-frame");
    const empty = $("report-empty");
    // Deep-linking to #report can happen before any project detail exists.
    if (!state.detail) {
      await refreshProject();
    }
    let runs = ((state.detail && state.detail.runs) || []).slice();
    if (!runs.length) {
      try {
        const detail = await api(`/api/projects/${encodeURIComponent(state.projectId)}`);
        state.detail = detail;
        runs = Array.isArray(detail.runs) ? detail.runs.slice() : [];
      } catch { runs = []; }
    }
    // Prefer completed runs with reports, newest first.
    runs.sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")));
    revokeBlobUrls();
    for (const run of runs.slice(0, 15)) {
      const result = await fetchRaw(`/api/report/${encodeURIComponent(run.id)}`);
      if (result.ok && result.text.trim()) {
        const cleaned = stripReportDuplicateHeading(result.text);
        state.reportHtml = cleaned;
        frame.srcdoc = cleaned;
        frame.hidden = false;
        empty.hidden = true;
        $("report-meta").textContent = `Run ${run.id} · ${run.status || "unknown"} · ${fmtDateTime(run.ended_at || run.started_at)}`;
        const htmlUrl = URL.createObjectURL(new Blob([cleaned], { type: "text/html" }));
        state.blobUrls.push(htmlUrl);
        let markdownUrl = null;
        const md = await fetchRaw(`/api/projects/${encodeURIComponent(state.projectId)}/report`);
        if (md.ok && md.text) {
          state.reportMd = md.text;
          markdownUrl = URL.createObjectURL(new Blob([md.text], { type: "text/markdown" }));
          state.blobUrls.push(markdownUrl);
        }
        setReportLinks(htmlUrl, markdownUrl);
        return;
      }
    }
    // Fallback: markdown-only report (no HTML yet).
    const md = await fetchRaw(`/api/projects/${encodeURIComponent(state.projectId)}/report`);
    if (md.ok && md.text.trim()) {
      const markdownBody = stripLeadingMarkdownHeading(md.text);
      const html = `<!doctype html><html><head><meta charset="utf-8"><title>ARIA-X report</title>` +
        `<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:36px auto;padding:0 20px;line-height:1.6;color:#1b2733;background:#fff}pre{white-space:pre-wrap;background:#f4f7fa;padding:16px;border-radius:8px;font-size:13px}</style>` +
        `</head><body><pre>${escapeHtml(markdownBody)}</pre></body></html>`;
      frame.srcdoc = html;
      frame.hidden = false;
      empty.hidden = true;
      $("report-meta").textContent = "Markdown report (HTML pending)";
      const mdUrl = URL.createObjectURL(new Blob([md.text], { type: "text/markdown" }));
      state.blobUrls.push(mdUrl);
      setReportLinks(null, mdUrl);
      return;
    }
    frame.hidden = true;
    frame.removeAttribute("srcdoc");
    empty.hidden = false;
    $("report-meta").textContent = "No report generated yet";
    setReportLinks(null, null);
  }

  // ---------------------------------------------------------------- settings

  function renderSettings() {
    const config = state.config;
    if (!config) return;
    const publicConfig = Object.assign({}, config);
    delete publicConfig.tools;
    delete publicConfig.scope_summary;
    $("settings-config").textContent = JSON.stringify(publicConfig, null, 2);
    $("settings-version").textContent = `ARIA-X ${config.version || ""} — ${config.tagline || ""}`;

    const scope = config.scope_summary || (config.scope ? config.scope : {});
    const rows = [
      ["Authorized", scope.authorized ? "yes" : "no"],
      ["Targets", (scope.targets || []).join(", ") || "none"],
      ["Denied", (scope.deny || []).join(", ") || "none"],
      ["Allow private", scope.allow_private ? "yes" : "no"],
      ["Allow loopback", scope.allow_loopback ? "yes" : "no"],
      ["Max RPS", scope.max_rps !== undefined ? String(scope.max_rps) : "—"],
    ];
    const dl = $("settings-scope");
    replace(dl, rows.flatMap(([key, value]) => {
      const dt = el("dt", null, key);
      const dd = document.createElement("dd");
      if (key === "Targets" || key === "Denied") {
        dd.textContent = value;
      } else {
        dd.textContent = value;
      }
      return [dt, dd];
    }));

    const tools = Array.isArray(config.tools) ? config.tools : [];
    replace($("settings-tools"), tools.map((tool) => {
      const item = el("div", "tool");
      const head = el("div", "tool-head");
      head.appendChild(el("span", "tool-name", tool.name || "tool"));
      const risk = tool.risk || "low";
      head.appendChild(el("span", `tool-risk risk-${risk}`, risk));
      if (tool.requires_scope) head.appendChild(el("span", "muted small", "scope"));
      item.appendChild(head);
      if (tool.description) item.appendChild(el("div", "tool-desc", tool.description));
      return item;
    }));
    const input = $("token-input");
    if (input && !input.value) input.value = state.token;
  }

  async function loadSettings() {
    try {
      await loadConfig(true);
      renderSettings();
    } catch (error) {
      if (error.status !== 401) toast(`Config unavailable: ${error.message}`, "bad");
    }
  }

  function saveToken() {
    const value = $("token-input").value.trim();
    state.token = value;
    if (value) localStorage.setItem(STORAGE.token, value);
    else localStorage.removeItem(STORAGE.token);
    state.authWarned = false;
    toast(value ? "Token saved." : "Token cleared.");
    connectSSE();
    refreshProject().catch(() => {});
    loadConfig(true).catch(() => {});
  }

  function clearToken() {
    state.token = "";
    localStorage.removeItem(STORAGE.token);
    $("token-input").value = "";
    toast("Token cleared.", "warn");
    connectSSE();
    refreshProject().catch(() => {});
  }

  // -------------------------------------------------------------- lifecycle

  function startPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = window.setInterval(() => {
      if (document.hidden) return;
      refreshProject().catch(() => {});
    }, state.pollMs);
  }

  function tickElapsed() {
    if (!state.run || !state.run.run) return;
    const record = state.run.run;
    const start = Date.parse(record.started_at || "");
    if (Number.isNaN(start)) return;
    const end = record.ended_at ? Date.parse(record.ended_at) : Date.now();
    if (Number.isNaN(end)) return;
    $("run-elapsed").textContent = fmtDuration((end - start) / 1000);
  }

  function wireEvents() {
    $("project-select").addEventListener("change", (event) => {
      selectProject(event.target.value).catch(() => {});
    });
    $("btn-refresh").addEventListener("click", () => {
      refreshProject().catch(() => {});
      toast("Refreshed.");
    });
    $("btn-settings").addEventListener("click", () => setTab("settings", true));
    $("empty-create-jump").addEventListener("click", () => {
      $("new-name").focus();
      $("new-name").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    $("btn-start").addEventListener("click", () => { startRun().catch(() => {}); });
    $("btn-stop").addEventListener("click", () => {
      if (window.confirm(`Stop run ${state.currentRunId}?`)) stopRun().catch(() => {});
    });
    $("run-goal").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        startRun().catch(() => {});
      }
    });
    $("hint-form").addEventListener("submit", (event) => { submitHint(event).catch(() => {}); });
    $("create-form").addEventListener("submit", (event) => { createProject(event).catch(() => {}); });
    $("finding-filter").addEventListener("change", (event) => {
      state.findingFilter = event.target.value;
      renderFindings();
    });
    $("asset-filter").addEventListener("change", (event) => {
      state.assetFilter = event.target.value;
      renderAssets();
    });
    $("asset-search").addEventListener("input", (event) => {
      state.assetSearch = event.target.value;
      renderAssets();
    });
    $("graph-fit").addEventListener("click", () => {
      if (!state.graph) { ensureGraph().then(renderGraph).catch(() => {}); }
      else renderGraph();
    });
    $("timeline-filter").addEventListener("input", (event) => {
      state.timelineFilter = event.target.value;
      renderTimeline();
    });
    $("timeline-type").addEventListener("change", (event) => {
      state.timelineType = event.target.value;
      renderTimeline();
    });
    $("timeline-pause").addEventListener("click", () => {
      state.timelinePaused = !state.timelinePaused;
      const button = $("timeline-pause");
      button.textContent = state.timelinePaused ? "Resume" : "Pause";
      button.setAttribute("aria-pressed", state.timelinePaused ? "true" : "false");
      $("timeline-log").classList.toggle("timeline-paused", state.timelinePaused);
      if (!state.timelinePaused) renderTimeline();
    });
    $("timeline-clear").addEventListener("click", () => {
      state.events = [];
      renderTimeline();
    });
    $("btn-report-reload").addEventListener("click", () => { loadReport().catch(() => {}); });
    $("settings-refresh").addEventListener("click", () => { loadSettings().catch(() => {}); });
    $("btn-token-save").addEventListener("click", saveToken);
    $("btn-token-clear").addEventListener("click", clearToken);
    $("poll-interval").value = String(state.pollMs);
    $("poll-interval").addEventListener("change", (event) => {
      const value = Number(event.target.value) || 3000;
      state.pollMs = value;
      localStorage.setItem(STORAGE.poll, String(value));
      startPolling();
      toast(`Refresh interval set to ${value / 1000}s.`);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshProject().catch(() => {});
    });
    document.addEventListener("keydown", (event) => {
      if (isTypingTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key.toLowerCase() === "r") {
        event.preventDefault();
        refreshProject().catch(() => {});
      }
    });
  }

  async function init() {
    wireTabs();
    wireEvents();
    setTab(tabFromHash() || state.tab);
    window.addEventListener("hashchange", () => {
      const fromHash = tabFromHash();
      if (fromHash && fromHash !== state.tab) setTab(fromHash);
    });
    try {
      await loadConfig();
    } catch (error) {
      if (error.status !== 401) toast(`Could not load config: ${error.message}`, "bad");
    }
    try {
      await loadProjects();
    } catch (error) {
      if (error.status !== 401) toast(`Could not load projects: ${error.message}`, "bad");
    }
    startPolling();
    window.setInterval(tickElapsed, 1000);
    if (state.tab === "settings") renderSettings();
  }

  document.addEventListener("DOMContentLoaded", () => { init().catch(() => {}); });
})();

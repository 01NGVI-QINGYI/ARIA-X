# Changelog

All notable changes to ARIA-X are documented here. ARIA-X is pre-1.0; interfaces may
change, but persisted schema changes will include a migration note.

## [0.2.0] — 2026-11-22

### Added

- **Trilingual Web Console (`en`, `zh-CN`, `ja`)**: Added `src/aria_x/web/i18n.js`,
  localized static and dynamic console labels, and a language selector in Settings
  (persisted in `localStorage["aria-x.lang"]`, defaulting to `en` on first visit).
  Switching console language is display-only and does not issue network requests or
  translate existing report bodies.
- **Trilingual static report templates**: Added `src/aria_x/report/i18n.py` and optional
  `language` parameter on `render_markdown`, `render_html`, and `write_report`
  supporting `en`, `zh-CN`, and `ja` fixed headings, metadata labels, hero stats, and
  boilerplate while preserving all dynamic findings/evidence verbatim.
- **`GET /api/report/{run_id}?format=md|html`**: Added optional `format` query
  parameter (returning `400` on invalid values) so clients can request HTML and Markdown
  for the exact same run.

### Changed

- **Default report language (Option A)**: `aria_x.report.render` resolves fixed-copy
  language in the order `language` arg > `meta["report_language"]` > `meta["lang"]` >
  `"en"` and intentionally ignores `meta["language"]`. Existing CLI, Demo, and
  Orchestrator entry points continue generating English fixed-copy reports by default
  in `v0.2.0`. `_fallback_report` in `aria_x.engine.orchestrator` remains English-only.
- **Compose auth token required (Option A)**: `docker/docker-compose.yml` now requires
  an explicit non-empty `ARIA_X_AUTH_TOKEN` (`:?` fail-fast) with no hardcoded fallback
  token; `.env.example` and `docs/DEPLOYMENT.md` are updated accordingly.
- Version bumped to `0.2.0`.

### Fixed

- **Packaging & launcher**: Set `100755` executable bit on root `aria-x`; included
  `aria-x` and `README.ja.md` in `MANIFEST.in`; packaged `aria_x/demo_target.py` inside
  the wheel so `aria-x demo` runs cleanly from a read-only installed wheel outside a
  source checkout.
- **Security boundaries**:
  - `Scope`: stopped auto-adding permissive host rules for URL targets, normalized
    default ports and `..` path traversal, separated `authorize_url` / `authorize_host`,
    and wired `safety.allow_public_targets`.
  - `Sandbox`: wired `safety.extra_blocked_patterns` while keeping
    `block_destructive=True` enforced.
  - `SessionManager`: isolated PTY child environment via an explicit `_base_env()`
    allowlist, refused `session_open` when `sandbox.backend == "mock"`, honored `cwd`
    inside `ctx.workdir`, and used `Session.docker_binary` during cleanup.
- **Report resolution & dynamic version**: Fixed `aria-x report` (CLI) to resolve the
  newest report by store `started_at` / file `mtime` within `runs_dir`; updated Web
  console `loadReport()` to keep preview and Markdown download bound to the same run;
  replaced hardcoded `"0.1.0"`/`"v0.1"` report versions with `__version__`.
- **Anthropic provider**: Defaulted `base_url` to `https://api.anthropic.com`, added
  official-endpoint auth validation while preserving custom gateways and `extra_headers`
  authentication, and mapped socket/timeout errors to retryable `LLMError`.
- **Docker metadata**: Replaced placeholder `org.opencontainers.image.source` URL in
  `docker/Dockerfile` with `https://github.com/01NGVI-QINGYI/ARIA-X`.

## [0.1.1] — 2026-11-10

### Added

- **Interactive sessions (PTY)**: `session_open`, `session_send`, `session_read`,
  `session_close` and `session_list`. Agents can now drive programs that need a real
  terminal — `msfconsole`, `ssh`, database REPLs and reverse shells — instead of being
  limited to one-shot `terminal_exec` calls.
  - Same guardrails as `terminal_exec`: `CommandPolicy` and the scope heuristic run on
    both the spawn command and every `send`, so a session cannot be used to smuggle an
    out-of-scope target past the checks.
  - Bounded: four concurrent sessions per run, 256 KB per-session buffer, 8 KB per read,
    idle sweep after 30 minutes, and `close_all()` on every terminal run path so no
    orphan shell survives a cancelled or failed engagement.
  - `session_open` / `session_send` are `high` risk and therefore gated by
    `safety.approval` in `ask` / `deny` mode.
  - Docker backend spawns `docker run -i -t` so the container gets a TTY too.
- 14 new tests covering lifecycle, state persistence, policy/scope refusals on both open
  and send, the session ceiling, idle sweep, audit/trace recording and fail-closed
  behaviour when no manager is attached.

### Changed

- Tool catalog grows from 22 to 27 tools; worker, bootstrap and coder roles can use the
  session tools.
- Version bumped to 0.1.1.

## [0.1.0] — 2026-10-22

First integrated release. ARIA-X fuses the blackboard search model of **Cairn**,
the dual-graph planner/worker model of **ARTEX**, and the specialist/provider/
sandbox model of **PentAGI** into one dependency-free Python engine.

### Added

**Core**
- Unified blackboard: nodes (`goal/intent/fact/finding/hint/note/guide`), edges,
  shared asset graph with anchors, todo chain, process traces, evidence and audit.
- SQLite (WAL) store with projects, runs, tasks, approvals, memory and exports.
- Event bus with JSONL persistence, live replay after restart, and SSE fan-out.
- Budgets for time, LLM calls, tokens, tool calls, intents, concurrency and
  specialist depth; every terminal path produces a coherent run status.

**Engine**
- `bootstrap → reason → explore` loop: planner is the sole intent generator;
  workers own one intent; idle/budget/round guards; hint consumption.
- Bounded thread scheduler with retries, cancellation and task records.
- Deterministic blackboard context renderer (hard character budget).
- Zero-dependency TF-IDF retrieval with CJK bigram support.
- Automatic report generation (Markdown, self-contained HTML, findings JSON)
  with a fallback renderer if the report layer fails.

**Agents and models**
- ReAct-style agent loop with native and prompt-mode tool calling, JSON
  contracts with repair, step ceilings, deterministic context compaction and
  full trace recording.
- Planner, bootstrap/worker, researcher/coder/adviser/memorist specialists, and
  reporter roles with per-role tool allowlists.
- OpenAI-compatible, Anthropic, Ollama, mock and scripted scenario providers;
  role→model routing, retries and usage accounting (stdlib `urllib` only).

**Tools and safety**
- 22 tools: terminal, jailed files, HTTP, DNS, TCP scan, TLS probe, web probe,
  external web search, graph writes, memory, todos, delegation, goal proof.
- Scope engine: exact host, wildcard domain, CIDR, host:port, URL prefix, deny
  rules, private/loopback policy, per-host rate limiting; redirects re-checked
  hop-by-hop; unresolved names fail closed.
- Command policy blocks destructive patterns; Docker/local/mock sandboxes.
- Human approval gate for high/critical tools (`auto | ask | deny`), stored in
  the approvals table, streamed to the console and written to the audit log.

**Control plane**
- HTTP API: projects, detail, graph, traces, audit, runs, stop, hints, approvals,
  report (MD/HTML), export, SSE.
- Offline dark console (no CDN/build): overview, layered graph, findings, assets,
  live timeline, todos, report, settings; deep links; optional Bearer auth.
- CLI: `init`, `demo`, `run`, `serve`, `doctor`, `tools`, `projects`, `graph`,
  `report`, `export`; convenience launcher `./aria-x`.

**Docs, demo and packaging**
- Offline end-to-end demo with a safe localhost-only vulnerable target and a
  deterministic policy; no API key required.
- 202 unit/integration tests, `scripts/verify.sh` gate, README (zh/en),
  architecture, fusion mapping, tool reference, security model, deployment and
  roadmap docs.
- Dockerfile and hardened compose profile; AGPL-3.0 license and attribution.

### Known limitations

- Single-writer SQLite; no distributed scheduler.
- `port_scan` is TCP-connect only; `web_search` depends on DuckDuckGo HTML.
- Scope for arbitrary shell commands is heuristic; Docker is the isolation
  boundary.
- `stop()` is honored at tool/round boundaries, not mid-syscall.
- Console reports only `report.md`/`report.html`; PDF export is not implemented.

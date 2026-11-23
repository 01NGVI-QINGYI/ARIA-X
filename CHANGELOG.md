# Changelog

All notable changes to ARIA-X are documented here. ARIA-X is pre-1.0; interfaces may
change, but persisted schema changes will include a migration note.

## [Unreleased]

### Security

- **Explicit `/api/config` public allowlist & full credential masking**:
  `AriaXConfig.public_dict()` now constructs an explicit allowlist of non-sensitive fields
  instead of cloning `to_dict()`, masks non-empty `llm.api_key` and `server.auth_token`
  uniformly as `"***"` (removing partial prefix/suffix key fragments), strips userinfo,
  query parameters, and fragments from `llm.base_url`, `scope.targets`, and `scope.deny`
  (including `scope_summary`), and excludes `llm.extra_headers`, `llm.extra_body`,
  `llm.scenario`, `sandbox.env`, `sandbox.extra_args`, `scope.notes`, `safety.note`, and
  any unknown/custom attributes without mutating live config.
- **Header-only SSE authentication & query-param log redaction**:
  Removed `?token=` query-string authentication from `GET /api/projects/{id}/events`
  (requests with only `?token=` now return `401`); SSE clients must authenticate via
  `Authorization: Bearer <token>` or `X-ARIA-X-Token`. Updated the Web Console SSE
  client in `src/aria_x/web/app.js` from `EventSource` to streaming `fetch` with
  `Authorization: Bearer`, preserving replay, heartbeats, deduplication, `retry:` /
  `Last-Event-ID` reconnect, project-switch abort, and display-only i18n switching.
  Sanitized HTTP access, error, and unhandled exception logs so non-allowlisted query
  parameter values are redacted before logging.
- **`local` sandbox isolation boundary notice**:
  Documented in `docs/SECURITY.md` that `sandbox.backend = "local"` runs commands with
  current OS user privileges (inheriting `HOME`, constraining only initial `cwd` and
  `file_*` path jail, without OS-level filesystem isolation), and added a non-fatal
  notice to `aria-x doctor` (human output and JSON `warning` field) without altering
  exit codes.

### Maintenance

- **Pinned Docker base image digest**: Pinned `docker/Dockerfile` `FROM` to the verified
  official multi-arch OCI index digest for `python:3.12-slim` (`python:3.12.16-slim`,
  `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`).
- **Dynamic HTTP `User-Agent`**: Derived `aria_x.tools.net.USER_AGENT` from
  `__version__` instead of hardcoded `"ARIA-X/0.1"`.

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
    default ports, enforced strict fail-closed URL path-prefix validation (rejecting
    raw dot segments, single/multi-encoded `%2e`/`%2f`/`%5c`/`%00`, `;`, `\`, `//`,
    malformed `%`, control chars, and paths exceeding the percent-decoding limit on
    initial requests and redirect hops), separated `authorize_url` / `authorize_host`,
    wired `safety.allow_public_targets`, and failed closed on unresolved hostnames when
    `allow_public_targets = false`.
  - `Sandbox`: wired `safety.extra_blocked_patterns` while keeping
    `block_destructive=True` enforced.
  - `SessionManager`: isolated PTY child environment via an explicit `_base_env()`
    allowlist, refused `session_open` when `sandbox.backend == "mock"`, honored `cwd`
    inside `ctx.workdir` (including `-w /work/<subdir>` for Docker PTY), and passed
    consistent `Session.docker_binary` + `_docker_client_env()` (`DOCKER_*` allowlist,
    no host secrets) across startup and `kill` cleanup.
- **Report resolution, Web race guards & dynamic version**: Fixed `aria-x report` (CLI)
  to query runs strictly read-only (`mode=ro`, never initializing empty SQLite files),
  perform exact per-run DB attribution lookups without a recent-runs cap, match exact
  run/project IDs, reject conflicting DB/directory/metadata attribution, and apply
  unified archive recognition across no-arg, `<run_id>`, and `--project-id` modes for
  depth-2 nested archives (`demo-<stamp>/<run_id>`), legacy flat
  `<project_id>-<run_id>` archives, and legacy timestamped `<project_id>-<stamp>`
  archives (treating explicit timestamps as archive naming while validating run/project
  attribution against `findings.json` and read-only SQLite); updated Web console `loadReport()`, `ensureGraph()`,
  and `selectProject()` with monotonic request-sequence and timer guards so stale or
  cross-project responses cannot overwrite the active project, while keeping preview
  and Markdown download bound to the same run; replaced hardcoded `"0.1.0"`/`"v0.1"`
  report versions with `__version__`.
- **Anthropic provider**: Defaulted `base_url` to `https://api.anthropic.com`, parsed
  hostname (`api.anthropic.com`) for official-endpoint detection with non-empty
  `api_key` / `x-api-key` / `Authorization: Bearer` validation (rejecting unsupported
  schemes on the official endpoint while preserving custom gateways), and mapped
  socket/timeout errors to retryable `LLMError`.
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

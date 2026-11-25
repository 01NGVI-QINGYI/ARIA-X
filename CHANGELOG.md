# Changelog

All notable changes to ARIA-X are documented here. ARIA-X is pre-1.0; interfaces may
change, but persisted schema changes will include a migration note.

## [0.3.0] — 2026-11-25

### Capability & Correctness

- **Non-HTTP targets now reach the scope engine**: `terminal_exec` only matched
  literal IPv4 addresses and URLs, so `ssh root@evil.example`, `smbclient -L
  //evil.example/share` and `mysql -h evil.example` executed with no scope
  decision. UNC paths, `user@host`, `-h/--host` flags, `host:port` and the
  trailing positional argument of network clients are now extracted and
  authorized. `user@host` is gated on network clients so e-mail addresses in
  payloads are not mistaken for targets, and `git` is only checked for remote
  subcommands.
- **DNS pinning**: the address authorized by scope is the address contacted.
  `Scope` records the address approved by the current authorization and HTTP requests route
  lookups through a thread-local resolver pin, closing the rebinding window
  between check and connect. TLS SNI and the `Host` header keep the hostname.
- **Mass docker teardown blocked**: `docker rm -f $(docker ps -aq)`,
  `docker system prune` and the volume/prune family previously passed the
  destructive policy, deleting unrelated containers on the operator's machine.
- **Unimplemented config keys removed**: `skills_dir`, `prompts_dir`,
  `planner_role`, `worker_role`, `specialist_role` and `reason_on_change` had no
  effect. They are gone, and any unknown key (including typos) is now reported by
  `aria-x doctor` instead of being dropped silently. The example config also had
  `max_concurrency` under `[engine]` instead of `[budget]`.

### Features

- **Attack-path diagram**: report section 5 renders the asset chain and its
  findings as a Mermaid flowchart in `report.md` and an inline SVG in
  `report.html` (no JavaScript, no CDN), colour-coded by severity and localized.
- **Resume**: `aria-x run --resume [RUN_ID]` (or `Engagement.resume()`) re-enters
  a run left in `running` or `failed`. The run keeps its id, bootstrap is skipped
  because its nodes exist, and traces/usage stay in one place.
- **Streaming model output**: OpenAI and Anthropic providers stream plain-text
  responses; the console shows a live character count on a TTY. Deltas are emitted
  as transient events, so per-token progress never reaches SQLite or the JSONL log.
  Tool-calling calls stay non-streaming by design, and because the planner/worker
  roles always carry tools, streaming currently applies to tool-free calls only
  (final answer synthesis, the doctor probe, JSON repair). A truncated or non-SSE
  response is raised as a retryable error instead of being returned as success.

### Fixes from independent review

- Report graphs now derive `asset -> finding` edges from `anchors` (findings hold
  an intent id in `parent_id`, so every finding was previously drawn from the
  first asset), SVG labels no longer leak the truncate marker, and no edge is
  invented when an asset is missing.
- Shell host extraction handles chained commands, wrappers, positional hosts and
  `--host=`/`-hHOST` forms, and no longer treats local file names as targets.
- Resume carries recorded usage into the budget, reopens abandoned `active`
  intents, clears `ended_at`, validates run/project ownership, and accepts
  `budget`/`cancelled` runs.
- DNS pinning covers redirect hops and `tls_probe`/`port_scan`, and keeps the
  first resolved address per host.
- Streaming raises a retryable error on truncated/non-SSE responses; tool-free
  calls stream, tool-carrying calls do not.
- Docker teardown rules cover backticks, `xargs`, `container/image` subcommands
  and `buildx prune`; internal config fields can no longer be set from a file.

### Notes

- `save()`/`export_public()` write only new files; `save_private("<path>.json")`
  remains the reloadable, `0o600` path (unchanged from 0.2.1).
- Test count: 329 (was 269).

## [0.2.1] — 2026-11-23

### Breaking Changes & Migration

- **SSE authentication migration (`?token=` query-string auth removed)**:
  When `server.auth_token` / `ARIA_X_AUTH_TOKEN` is configured, requests passing only
  `?token=<token>` on `GET /api/projects/{id}/events` are now **rejected with
  `401 Unauthorized`** so long-lived credentials are no longer sent in SSE request URLs
  (avoiding query-string token exposure in HTTP server and proxy access logs). External
  SSE consumers must migrate to `Authorization: Bearer <token>` or
  `X-ARIA-X-Token: <token>` request headers; the built-in Web Console
  (`src/aria_x/web/app.js`) has been migrated from `EventSource` to streaming `fetch`
  with `Authorization: Bearer` and `Last-Event-ID`.
- **`AriaXConfig.save()` is now a non-reloadable redacted export (`save_private()` added)**:
  `AriaXConfig.save()` now delegates to `AriaXConfig.export_public()` and writes a
  shareable `_redacted_export: true` JSON snapshot to a new `.json` file (`open("x")`,
  refusing to overwrite existing files) that `AriaXConfig.from_dict()` and
  `AriaXConfig.load()` reject with `ConfigError` (literal `"***"` placeholders are also
  rejected as runtime credentials). Callers needing reloadable local persistence must
  explicitly call `AriaXConfig.save_private("<path>.json")` (`.json` suffix required;
  atomic write with owner-only `0o600` permissions).

### Security

- **Explicit `/api/config` public allowlist & full credential masking**:
  `AriaXConfig.public_dict()` constructs an explicit allowlist of non-sensitive fields
  instead of cloning `to_dict()`, masks non-empty `llm.api_key` and `server.auth_token`
  uniformly as `"***"` (removing partial prefix/suffix key fragments), strips userinfo,
  query parameters, and fragments from `llm.base_url`, `scope.targets`, and `scope.deny`
  (including `scope_summary`), and excludes `llm.extra_headers`, `llm.extra_body`,
  `llm.scenario`, `sandbox.env`, `sandbox.extra_args`, `scope.notes`, `safety.note`, and
  any unknown/custom attributes without mutating live config.
- **Separated shareable redacted export from reloadable private config persistence**:
  Added `AriaXConfig.export_public()` and `AriaXConfig.save_private()`, both requiring a
  `.json` suffix before creating directories or touching the target path; `export_public()`
  and `save()` also use exclusive creation (`open("x")`) and refuse to overwrite existing
  files, while `save_private()` preserves `extra_headers`, `extra_body`, `scenario`,
  `sandbox.env`, `sandbox.extra_args`, `scope.notes`, `safety.note`, and credentials via
  atomic write with `0o600` permissions.
- **HTTP access, error, and exception log sanitization**:
  Normalized logged request paths in `AriaXRequestHandler` using explicit route templates
  (e.g., `/api/projects/{project_id}/events`, `/static/{asset}`) and hashed digests for
  unknown or unparseable paths (`/<unknown:...>`, `/api/<unknown:...>`,
  `<unparseable:...>`). Restricted query-string logging to validated `replay`, `limit`,
  `format`, and `status` values while redacting `token`, `project_id`, `task_id`, and
  unknown keys (`[REDACTED]`). Replaced `logger.exception(...)` and raw error formatting
  with safe structured logs (`method`, normalized `route`, `status`, `error_type`) without
  raw exception bodies, tracebacks, or request payloads.
- **`local` sandbox isolation boundary notice**:
  Documented in `docs/SECURITY.md` that `sandbox.backend = "local"` runs commands with
  current OS user privileges (inheriting `HOME`, constraining only initial `cwd` and
  `file_*` path jail, without OS-level filesystem isolation), and added a non-fatal
  notice to `aria-x doctor` (human output and JSON `warning` field) without altering
  exit codes.

### Fixed

- **SSE `Last-Event-ID` cursor resume & complete-frame-only client parsing**:
  Added atomic `EventBus.subscribe_with_snapshot()` and wired `Last-Event-ID` from
  `AriaXRequestHandler` through `Api.stream_events()` so in-buffer cursors matching the
  current project stream replay all subsequent visible buffered events in order (even
  beyond the initial `replay=150` window) and hand off across the replay/live boundary
  without boundary gaps or duplicate event IDs within the bounded `EventBus` replay buffer
  and subscriber queue capacity; missing, expired, or foreign-project cursors fall back to
  bounded `replay`. Updated `src/aria_x/web/app.js` to discard incomplete trailing EOF
  fragments (never entering timeline, `sseLastEventId`, or `seenEventIds`), removed unused
  `state.es`, and counted both short-lived (`< 2s`) `200` EOFs and post-`200` read errors
  toward bounded exponential backoff (`sseFailureCount`, capped at 30s) until a stream
  stays stable for `>= 2s`, while preserving `401` stop, project-switch/clear abort, and
  display-only i18n switching.

### Maintenance

- **Version `0.2.1` metadata & dynamic HTTP `User-Agent`**:
  Bumped package, CLI, Docker tags, CITATION, and documentation references to `0.2.1`;
  derived `aria_x.tools.net.USER_AGENT` from `__version__` instead of hardcoded
  `"ARIA-X/0.1"`.
- **Pinned Docker base image digest**: Pinned `docker/Dockerfile` `FROM` to the verified
  official multi-arch OCI index digest for `python:3.12-slim` (`python:3.12.16-slim`,
  `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`).

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

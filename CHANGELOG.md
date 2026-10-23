# Changelog

All notable changes to ARIA-X are documented here. ARIA-X is pre-1.0; interfaces may
change, but persisted schema changes will include a migration note.

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

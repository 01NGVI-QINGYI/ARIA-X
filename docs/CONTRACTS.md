# ARIA-X — Internal Build Contracts (v0.1)

This file is the hand-off contract for parallel development. Read it fully before writing code.

## 0. Environment

- Repository root: the directory containing `pyproject.toml`.
- Python: CPython **3.11+** (3.12 recommended). Use `$ARIA_X_PYTHON` if it is set,
  otherwise `python3.12` or `python3.11`.
- Run anything with:
  `PYTHONPATH=src python3.12 -m unittest discover -s tests -v`
- **Dependency policy: Python standard library only.** No pip installs, no network SDKs.
  Every module starts with `from __future__ import annotations`.
- If your editor uses atomic-rename writes on a non-POSIX filesystem and fails, write files
  with shell redirection/heredocs instead. This is an environment workaround, not a project
  requirement.
- Do not edit upstream reference checkouts; they are read-only reference material.
- Only write files inside your assigned scope. If an interface must change, message the Lead.

## 1. What ARIA-X is

ARIA-X fuses three systems into one engine:

| Source | What we take |
| --- | --- |
| **Cairn** (AGPL-3.0) | Blackboard: Fact / Intent / Hint; reason→explore OODA loop; no fixed roles for workers |
| **ARTEX** (AGPL-3.0) | Dual graph (exploration + shared asset graph); planner is sole intent generator; shared todo chain across wake-ups; cross-worker process traces; approval gate; run/report |
| **PentAGI** (MIT) | Specialist team (researcher/coder/adviser/memorist), provider abstraction, sandboxed terminal, delegation, memory |

ARIA-X itself is **AGPL-3.0**. Keep attribution in NOTICE (docs owner).

## 2. Package map and ownership

```
src/aria_x/
  __init__.py, __main__.py, cli.py, config.py, errors.py, util.py, models.py, events.py   [Lead]
  store/{__init__,schema,sqlite}.py                                                      [Lead, done]
  llm/{__init__,base,openai,anthropic,mock,jsonmode,router}.py                           [Lead, done]
  llm/scenario.py                                                                        [agents-dev]
  tools/{__init__,base,scope,sandbox}.py                                                 [Lead, done]
  tools/{terminal,net,knowledge,builtin}.py                                              [tools-dev]
  agents/{__init__}.py                                                                   [Lead]
  agents/{base,prompts}.py                                                               [Lead, done]
  agents/{roles,workers}.py                                                              [agents-dev]
  engine/{budget}.py                                                                     [Lead, done]
  engine/{engagement,orchestrator,scheduler,context}.py                                  [engine-dev]
  memory/retrieval.py                                                                    [engine-dev]
  report/render.py                                                                       [tools-dev]
  server/{__init__,app,api}.py                                                           [server-dev]
  web/{index.html,app.js,style.css}                                                      [server-dev]
tests/ per-owner files                                                                   [each owner]
examples/demo_target.py, scripts/, docker/, docs/, README, LICENSE, NOTICE               [docs-qa]
cli.py + configs/ + examples demo engagement                                             [Lead]
```

## 3. Frozen interfaces you build against

### 3.1 Store (`aria_x.store.Store`) — done
Key methods (all return dataclasses from `aria_x.models`):
`create_project/get_project/list_projects/update_project`,
`create_run/get_run/update_run/list_runs`,
`add_node/get_node/find_node/update_node/list_nodes/claim_intent/add_edge/list_edges/neighbors`,
`add_finding`, `upsert_asset/get_asset/find_asset/list_assets/anchor/anchors_for_node/nodes_for_asset`,
`add_todo/get_todo/update_todo/list_todos/ready_todos`,
`create_task/get_task/update_task/list_tasks`,
`add_trace/list_traces/search_traces`,
`add_evidence/list_evidence`, `add_message/list_messages`,
`audit/list_audit`, `request_approval/decide_approval/get_approval/list_approvals`,
`set_kv/get_kv`, `counts`, `graph_overview`, `export_project`, `close`.
`Store(":memory:")` is supported for tests.

### 3.2 LLM (`aria_x.llm`)
```python
ChatMessage.system/user/assistant/tool(...)
LLMResponse(content, tool_calls, prompt_tokens, completion_tokens, model, finish_reason)
ModelRouter(config, budget=..., events=..., project_id=..., run_id=...)
router.chat(role, messages, tools=None, *, temperature=None, max_tokens=None,
            json_mode=False, task_id="", tool_choice=None) -> LLMResponse
router.json(role, messages, schema=None, *, retries=2, task_id="") -> (data, LLMResponse)
```
`role` is e.g. `"planner" | "worker" | "specialist" | "reporter"`.
`MockProvider(responses=[...])` / `MockProvider(policy=fn)` for tests. A dict response may be
`{"content": "...", "tool_calls": [{"name": "...", "arguments": {...}}]}`.

### 3.3 Tools (`aria_x.tools`)
```python
ToolSpec(name, description, parameters_json_schema, risk=low|medium|high|critical,
         tags=(), requires_scope=False, read_only=False)
ToolResult.success(data=None, text="") / ToolResult.failure(error)
ToolRegistry().register(tool); .add(spec, fn); .get(name); .names(); .specs(allowed)
registry.execute(name, args, ctx) -> ToolResult
```
A function tool is `fn(ctx: ToolContext, kwargs: dict) -> ToolResult` via `registry.add(spec, fn)`.
`ToolContext` fields: `project, run_id, task_id, agent, role, store, config, events, scope,
sandbox, workdir, budget, approvals, delegate, cancel, request_approval`; helpers `ctx.child(...)`,
`ctx.emit(type_, data)`, `ctx.check_cancel()`.
`Scope.authorize_url/require_url/authorize_host/require_host/check_command/acquire_slot/summary`.
`Sandbox.run(command, timeout=None, cwd=None, env=None, stdin=None) -> SandboxResult`.
`SandboxConfig.network` defaults to `"none"`; Docker is opt-in.

### 3.4 Agents (`aria_x.agents`)
```python
AgentSpec(name, role, system_prompt, tools=[...], max_steps=12, temperature=.2,
          max_tokens=4096, json_contract=None, finalize_instruction="")
AgentTask(task_id, kind, prompt, context="", goal="", intent_id=None, extra={})
Agent(spec, router, registry, ctx, max_steps=None).run(task) -> AgentOutcome
AgentOutcome(ok, text, data, error, steps, tool_calls, prompt_tokens, completion_tokens, ...)
```
`agents/prompts.py` already provides `core_rules`, `PLANNER_SYSTEM/SCHEMA`, `WORKER_SYSTEM/SCHEMA`,
`BOOTSTRAP_SYSTEM`, `SPECIALIST_SYSTEM`, `REPORTER_SYSTEM`, and task-rendering helpers.

### 3.5 Engine (engine-dev)
Public API used by CLI/server/tests:
```python
@dataclass EngagementResult: ok, project_id, run_id, status, summary, report_path, error, metrics
class Engagement:
    def __init__(self, config: AriaXConfig, *, store=None, events=None, provider=None)
    def create_project(name, goal, scope=None) -> Project        # scope defaults to config.scope
    def run(project_id, *, goal=None) -> EngagementResult         # blocking
    def start_background(project_id, *, goal=None) -> str         # returns run_id
    def stop(run_id=None) -> None
    def status(run_id=None) -> dict
    def close() -> None
```
The orchestrator must implement: optional bootstrap → reason (planner) → dispatch up to
`max_concurrency` explore tasks → persist → repeat, with budget/time/idle guards, todo chain,
hints, completion, report generation, and JSONL events. It must work with `MockProvider` and with
`provider="scenario"`.

### 3.6 Server (server-dev)
```python
create_server(config: AriaXConfig, engagement: Engagement, bus: EventBus, store: Store) -> ThreadingHTTPServer
serve(config, *, block=True) -> None
```
Routes are specified in §5.

### 3.7 Report (tools-dev)
```python
render_markdown(export: dict, *, meta: dict) -> str
render_html(markdown: str, export: dict, meta: dict) -> str
write_report(run_dir: Path, export: dict, meta: dict) -> dict  # {"markdown":p,"html":p,"json":p}
```
`export` is `Store.export_project(project_id)`.

## 4. Tool contract (tools-dev)

Register exactly these names via `build_registry(ctx)` in `tools/builtin.py`:

- `terminal_exec(command, timeout?, cwd?)` — risk high, sandbox + policy + scope heuristics, returns exit/stdout/stderr; records trace.
- `file_read(path, max_bytes?)`, `file_write(path, content)`, `file_list(path?)` — workdir-jailed; medium/high risk.
- `http_request(url, method?, headers?, body?, timeout?, max_bytes?)` — scope-guarded; saves request/response evidence; returns status, headers (redacted), body excerpt, length, elapsed.
- `dns_lookup(host, record_type?)` — scope-guarded; A/AAAA via getaddrinfo, reverse via gethostbyaddr.
- `port_scan(host, ports?, timeout?, max_ports?)` — scope-guarded TCP connect, concurrency ≤ 32, rate-limited, returns open ports + banners (optional).
- `tls_probe(host, port?)` — scope-guarded TLS handshake, cert subject/issuer/SAN/expiry/self-signed.
- `web_probe(base_url, paths?)` — scope-guarded GET of a small wordlist, returns status/length/content-type.
- `web_search(query)` — DuckDuckGo HTML endpoint; returns title/url/snippet; graceful failure; this is external research, not target traffic.
- `add_fact(title, body, confidence?, intent_id?, asset_values?)` — writes node + edges from intent.
- `add_finding(title, severity, body, asset_values?, intent_id?, cvss?, evidence?)` — writes finding + evidence.
- `add_asset(kind, value, parent_value?)` — upsert asset under project.
- `anchor_node(node_id, asset_value, kind?)` — link node to asset.
- `graph_query(view, query?, limit?)` — views: overview|facts|findings|assets|intents|todos|traces.
- `search_traces(query, limit?)` — cross-worker process search excluding current task.
- `memory_store(text, tags?)` / `memory_search(query, limit?)` — project memory in kv/nodes, zero-dep TF-IDF in `tools/knowledge.py` (do not import engine.memory).
- `todo_update(todo_id?, title?, status?, detail?)` — planner support.
- `prove_goal(summary, evidence_ids?)` — marks goal proven / completion signal.
- `delegate_specialist(specialist, question, context?)` — calls `ctx.delegate(specialist, question)`; low risk; the delegate callable is injected by the engine.
- `scope_check(target)` — returns scope decision (diagnostic).

All state-changing tools emit `Events` and audit. `http_request`/`port_scan`/`dns_lookup`/`tls_probe`/
`web_probe` MUST call scope before network I/O. Never log secrets unredacted (use `util.redact`).

## 5. HTTP API (server-dev)

Bind `127.0.0.1` by default. Optional Bearer auth via `config.server.auth_token`.
Static: `GET /` → `web/index.html`, `GET /static/*`.

```
GET  /api/health                -> {ok, version, time}
GET  /api/config                -> config.public_dict() + tools catalog + scope summary
GET  /api/projects              -> [Project]
POST /api/projects              {name, goal, scope?} -> Project
GET  /api/projects/<id>         -> {project, counts, runs, findings, assets, todos}
GET  /api/projects/<id>/graph   -> graph_overview
GET  /api/projects/<id>/traces?task_id=&limit=
GET  /api/projects/<id>/audit
GET  /api/projects/<id>/report  -> latest report markdown (or 404)
POST /api/projects/<id>/runs    {goal?} -> {run_id}  (start_background)
GET  /api/runs/<id>             -> status + budget + tasks
POST /api/runs/<id>/stop        -> {ok}
POST /api/projects/<id>/hints   {content} -> {ok}    (human hint, also a chat message)
GET  /api/projects/<id>/events  -> text/event-stream (SSE, replay recent, heartbeat)
GET  /api/approvals?project_id= -> pending
POST /api/approvals/<id>        {approved, reason?} -> {ok}
GET  /api/report/<run_id>       -> HTML report (or 404)
GET  /api/export/<project_id>   -> full JSON export
```
SSE must not hold the SQLite lock; use `EventBus.subscribe()` and a queue.

## 6. Coding conventions

- Type hints on public functions; `from __future__ import annotations`.
- No third-party imports anywhere (`import httpx` is forbidden).
- Timeouts on every network/subprocess call. Truncate large outputs.
- Never print to stdout from library code; use `events`/logging.
- Tests use `unittest` (`python -m unittest`), no pytest. Put fixtures in `tests/`.
- Each owner adds tests for their module, and runs the full suite before reporting.
- If you touch another owner's file, you own the merge: message the Lead first.

## 7. Definition of done for each owner

1. Module imports cleanly: `PYTHONPATH=src $PY -c "import aria_x.<module>"`.
2. Unit tests pass (your files + full suite).
3. Public docstrings explain *why*, not just *what*.
4. Report back with: files written, commands run, test output, known gaps.

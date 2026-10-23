# ARIA-X 架构

本文描述**已经实现**的模块、数据流与接口。它是工程文档，不是路线图；未实现的内容统一放在
[ROADMAP.md](ROADMAP.md)。

- 运行时：CPython ≥ 3.11（开发环境 3.12），仅标准库。
- 入口：`python -m aria_x`（CLI）、`aria_x.server:serve`（HTTP）、`aria_x.engine:Engagement`（库）。
- 状态：SQLite（WAL）+ 追加式 JSONL 事件流 + 运行目录中的报告文件。

## 1. 分层

```
┌──────────────────────────────────────────────────────────────────┐
│ CLI (cli.py)               Web console (web/) + HTTP API (server/)│
├──────────────────────────────────────────────────────────────────┤
│ Engagement (engine/engagement.py)                                 │
│   └─ Orchestrator (engine/orchestrator.py)                        │
│        ├─ TaskScheduler (engine/scheduler.py)                     │
│        ├─ Budget (engine/budget.py)                               │
│        ├─ agents: planner / bootstrap / workers / specialists /   │
│        │          reporter (agents/)                              │
│        ├─ ToolRegistry + 22 tools (tools/)                        │
│        ├─ ModelRouter + providers (llm/)                          │
│        └─ report renderer (report/)                               │
├──────────────────────────────────────────────────────────────────┤
│ Store (store/sqlite.py)   EventBus (events.py)   Memory (memory/) │
└──────────────────────────────────────────────────────────────────┘
```

### 模块职责

| 路径 | 职责 |
| --- | --- |
| `src/aria_x/__init__.py` | 版本、许可证标识、tagline |
| `src/aria_x/cli.py` | 参数解析、配置装配、`init/demo/run/serve/doctor/tools/projects/graph/report/export` |
| `src/aria_x/config.py` | dataclass 配置树、TOML/JSON 读取、环境变量覆盖、校验、授权门 |
| `src/aria_x/models.py` | Project / Run / Node / Edge / Asset / Todo / Trace / Task / Evidence / Message / AuditEntry / Usage |
| `src/aria_x/store/` | SQLite schema、迁移、CRUD、图查询、审批、审计、导出 |
| `src/aria_x/events.py` | 线程安全 EventBus、SSE 重放缓冲、JSONL sink、事件类型常量 |
| `src/aria_x/llm/` | `ChatMessage` / `LLMResponse` / provider 协议、OpenAI 兼容、Anthropic、Mock、Scenario、JSON 契约校验、ModelRouter |
| `src/aria_x/tools/` | ToolSpec/Result/Registry/Context、Scope、Sandbox、terminal/file/net/knowledge 工具 |
| `src/aria_x/agents/` | Agent 运行时、角色提示词、planner/worker/bootstrap/specialist/reporter 规格 |
| `src/aria_x/engine/` | Budget、Scheduler、Orchestrator、Engagement |
| `src/aria_x/memory/retrieval.py` | 零依赖 TF-IDF 索引与检索（节点、轨迹） |
| `src/aria_x/report/render.py` | Markdown / 自包含 HTML / findings JSON |
| `src/aria_x/server/` | `ThreadingHTTPServer`、路由、SSE、静态资源、可选 Bearer 认证 |
| `src/aria_x/web/` | 无框架、无 CDN 的控制台前端 |
| `src/aria_x/demo.py` | 离线演示的 `DemoPolicy` 与 `DemoTarget` 生命周期管理 |
| `examples/demo_target.py` | 单独可运行的、故意存在漏洞的本机目标 |

## 2. 一次 engagement 的生命周期

`Engagement.run(project_id)`（阻塞）与 `start_background()`（线程）走同一条路径：

1. **授权门**：`AriaXConfig.require_authorization()` 要求 `scope.authorized=true` 且
   `scope.targets` 非空；project scope 若显式 `authorized=false` 也会被拒绝。
2. **创建 Run 行**（status=`running`），写入预算快照。
3. **`Orchestrator._prepare`**：初始化 `Budget`、`ModelRouter`、`Scope`、`Sandbox`、
   `ToolRegistry`、`TaskScheduler`、run workdir，并确保唯一的 `goal` 节点。
4. **bootstrap（可选）**：一次最佳尝试（`engine.bootstrap=true` 时），产出 fact / finding，
   若直接证明目标则可能立即完成。
5. **主循环 `_loop`**：
   - `_reason_step`：planner 读黑板快照，返回 `{action: intents|complete|noop}`；
   - `_create_intents`：只接受有界、非重复的意图；同步 planner 的 todo 链；
   - `_dispatch`：把可运行的意图交给 `TaskScheduler`，并发受 `budget.max_concurrency`
     与 todo 依赖约束；
   - `_explore`：每个 intent 一个 worker，`Agent.run` 执行工具调用直到返回 JSON 契约；
   - `_persist_outcome`：写 fact/finding/assets/证据/轨迹，更新 intent 与 todo 状态；
   - 预算、时间、idle 轮数、图变化检测决定继续或收尾。
6. **`_finalize`**：更新 Run（status / usage / error），渲染报告，发出
   `run.finished`，把 budget 快照写入 Run。

运行状态：`running`、`completed`、`budget`、`cancelled`、`failed`。`budget` 表示预算耗尽
但图与报告仍然一致；`failed` 表示未预期错误，错误会进入事件与 run 行。

## 3. 数据模型

SQLite 表（`src/aria_x/store/schema.py`）：

| 表 | 内容 |
| --- | --- |
| `projects` | 项目、目标 goal、scope/config JSON、状态 |
| `runs` | 一次运行：状态、预算/用量 JSON、错误、起止时间 |
| `nodes` | 黑板节点：kind ∈ goal/intent/fact/finding/hint/note/guide；status、parent、meta JSON、seq |
| `edges` | 有向关系：spawns / yields / derived_from / proves / supports / depends_on / related / contradicts |
| `assets` | 共享资产图：root_domain/subdomain/host/ip/service/endpoint/app/credential/artifact，parent_id |
| `anchors` | 节点 ↔ 资产多对多锚点 |
| `todos` | 跨唤醒计划链：position、status、depends_on JSON、intent_id |
| `tasks` | 调度记录：bootstrap/reason/explore/specialist/report/consolidate、attempts、error、meta |
| `traces` | 过程轨迹：agent、seq、kind、tool_name/args/result、content |
| `evidence` | finding 的证据：kind、path、sha256、excerpt、meta |
| `messages` | 人机消息（hint 同时写 message） |
| `audit` | 审计：actor、action、decision、target、detail |
| `approvals` | 审批请求与决定 |
| `kv` | 按 scope 分区的键值（记忆/元数据） |
| `meta` | schema 版本等 |

双图的含义：

- **探索图** = `nodes` + `edges`，记录"我们如何推理"：goal → intent → fact/finding；
- **资产图** = `assets` + `anchors`，记录"目标实际有什么"：域名 → 主机 → 服务 → 端点/工件；
- `anchors` 把两边连接起来，使报告可以按资产品转发现，而不会把推理状态当成目标事实。

## 4. 模型层与 provider

`ModelRouter.chat(role, messages, tools=None, ...)` 是所有模型调用的唯一入口：

- 角色：`planner` / `worker` / `specialist` / `reporter`（以及 provider 内部的 bootstrap 推断）；
- 每次调用前 `budget.check_llm()`，调用后 `budget.record_llm()` 并发出 `llm.call`；
- JSON 契约由 `router.json(...)` 解析、校验，失败时最多重试 `max_retries` 次；
- `llm.role_models` 可给不同角色指定不同模型。

内置 provider：

| provider | 行为 |
| --- | --- |
| `openai` | OpenAI 兼容 `/chat/completions`（DeepSeek、vLLM、Ollama 等）；原生 tool calling |
| `anthropic` | Anthropic Messages API；tool use 转换 |
| `ollama` | 复用 OpenAI 兼容适配器，默认 `http://127.0.0.1:11434/v1` |
| `mock` | 回放列表或 policy 函数；测试用 |
| `scenario` | 按 JSON 脚本/角色队列确定性回放；离线演示与回归测试用 |

### `scenario` 语义（已实现）

- 角色由渲染后的任务头 `# TASK (reason|explore|bootstrap|report)` 推断；无法判断时回退到
  system prompt（`PLANNER of ARIA-X`、`BOOTSTRAP agent`、`EXPLORER (worker)`、`REPORTING agent`、
  `<name> specialist`）。
- `roles.<role>` 队列按顺序消费；`"loop": true` 循环，默认 `false` 时用尽后转到
  `responses`，再转到 `default`。
- `responses[].match` 是**最后一条 user 消息**的大小写不敏感子串；命中一次即消费
  （`loop=true` 除外）；无 `match` 的条目是通配回退。
- entry 形式：字符串、`{"content": ...}`、`{"json": {...}}`、
  `{"tool_calls": [{"name","arguments"}]}`、或工具调用 dict 的裸列表。
- `llm.scenario` 是相对 CWD 的路径，也接受内联 JSON；文件缺失抛 `LLMError`。
- 并行运行时会并发消费同一队列，**需要确定性时把 `max_concurrency` 设为 1**。

真实 provider 的密钥只从配置或环境变量读取；`AriaXConfig.public_dict()` 会遮蔽 API key 与
server token；所有事件与报告在写出前经过 `util.redact`。

## 5. 工具执行边界

```
Agent loop
  → ModelRouter.chat(tools=[...])
  → LLMResponse.tool_calls
  → ToolRegistry.execute(name, args, ctx)
       ├─ ctx.check_cancel()
       ├─ 参数 schema 校验/轻量强转
       ├─ _enforce_approval（high/critical + safety.approval）
       ├─ tool.run(ctx, **args)
       │    ├─ Scope.require_*（网络工具）
       │    ├─ Sandbox.run（终端工具）
       │    └─ Store 写入 + 事件
       └─ 异常 → ToolResult.failure（scope/approval/aria_x/unknown）
  → 工具结果作为 role="tool" 消息回给模型
```

- `ToolContext` 携带 project、run/task id、agent、role、store、config、events、scope、
  sandbox、workdir、budget、delegate、cancel、request_approval。
- 文件工具被 jail 在 engagement workdir；`file_read` 拒绝父目录穿越与符号链接逃逸。
- 终端工具先过 `CommandPolicy`（破坏性命令黑名单），再进沙箱；`local` 后端还要求 cwd
  位于 workdir 内。
- scope 违规不会杀死 engagement：它被转换成工具失败 + 审计记录，让模型看到并自我修正。
- **审批门（已强制）**：`ToolRegistry.execute` 在参数校验之后、工具运行之前调用
  `_enforce_approval`。`safety.approval="auto"` 放行；`"deny"` 立即拒绝 `high`/`critical`
  工具；`"ask"` 通过 `ToolContext.request_approval` 创建审批记录、发出 `approval.requested`
  事件并**阻塞等待**人工决定（最长 300 秒，超时/取消视为拒绝；Web 控制台/API），
  批准后继续，拒绝则抛 `ApprovalDenied`。
  审批决定会写审计表。`port_scan`、`file_write`、`terminal_exec` 属于 `high` 风险，
  因此在 `ask`/`deny` 模式下受此门控制。审批不会扩大 scope：scope 始终独立强制。

## 6. 事件与事件流

事件类型（`src/aria_x/events.py`）包括：`run.started/finished/failed/paused/resumed`、
`task.started/finished/failed`、`node.created/updated`、`asset.found`、`finding.created`、
`todo.updated`、`trace`、`log`、`llm.call`、`tool.call`、`approval.requested/decided`、
`budget`、`report.ready`、`hint`、`error`。

- `EventBus.emit()` 线程安全，维护有界重放缓冲（默认 200，可配置），并把 JSON 行追加到
  `data/events.jsonl`（写失败不影响运行）。
- 所有 payload 经过递归 secret redaction。
- SSE：`GET /api/projects/<id>/events?replay=N` 先回放最近 N 条（默认 100，上限 500），
  再推实时事件，空闲时发送心跳；流线程**不访问 SQLite**。

## 7. HTTP API

`aria_x.server.create_server(config, engagement, bus, store)` 返回已绑定但未启动的
`ThreadingHTTPServer`；`serve(config, ...)` 负责启动。默认 `127.0.0.1:8787`。
若配置 `server.auth_token`（或 `ARIA_X_AUTH_TOKEN`），除 `/api/health` 外的 API 需要
`Authorization: Bearer <token>`；SSE 因 EventSource 不能设置请求头，也接受 `?token=`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 控制台 HTML |
| GET | `/static/*` | 静态资源（JS/CSS） |
| GET | `/api/health` | `{ok, version, time}` |
| GET | `/api/config` | 公开配置 + 工具目录 + scope 摘要 |
| GET/POST | `/api/projects` | 列表 / 创建项目 |
| GET | `/api/projects/<id>` | 项目 + counts + runs + findings + assets + todos |
| GET | `/api/projects/<id>/graph` | 图总览 |
| GET | `/api/projects/<id>/traces` | 轨迹（`task_id`、`limit`） |
| GET | `/api/projects/<id>/audit` | 审计 |
| GET | `/api/projects/<id>/report` | 最近报告 Markdown |
| POST | `/api/projects/<id>/runs` | 后台运行 `{goal?}` → `{run_id}` |
| POST | `/api/projects/<id>/hints` | 注入人类 hint（同时写 message） |
| GET | `/api/projects/<id>/events` | SSE |
| GET | `/api/runs/<id>` | 运行状态 + 预算 + 任务 |
| POST | `/api/runs/<id>/stop` | 请求停止 |
| GET | `/api/approvals?project_id=` | 待审批 |
| POST | `/api/approvals/<id>` | `{approved, reason?}` |
| GET | `/api/report/<run_id>` | HTML 报告 |
| GET | `/api/export/<project_id>` | 完整 JSON 导出 |

## 8. 存储与并发

- 每个 `Store` 持有一个 `sqlite3.Connection`（`check_same_thread=False`），事务用显式
  `BEGIN/COMMIT`；WAL 模式允许读并发，写串行。
- `Engagement` 是线程安全边界：`run()` 在调用线程执行；`start_background()` 每个 run
  一个 daemon 线程；`close()` 先置取消标志，再 join（上限 10s），最后关闭自己拥有的
  store/event bus。
- `Budget` 不是持久调度器：进程崩溃后不会自动续跑；状态与报告仍可读，新一轮运行会创建
  新的 Run。

## 9. 报告

`report/render.write_report(run_dir, export, meta)` 生成：

- `report.md`：执行摘要、范围与方法、按严重度排序的发现、攻击路径、覆盖与盲点；
- `report.html`：自包含深色主题，无外部 CDN；
- `findings.json`：发现的结构化导出。

`export` 来自 `Store.export_project(project_id)`，包含 project、nodes、edges、assets、
todos、findings、traces、evidence（按 finding id 分组）、audit、runs。报告失败不会使运行
失败：`Orchestrator._render_report` 会回退到内置的确定性渲染器。

## 10. 关键设计取舍

| 取舍 | 原因 |
| --- | --- |
| 单进程 + SQLite，而不是消息队列 | 个人/小团队部署简单，状态可拷贝、可审计；分布式留到以后 |
| planner 独占意图生成 | 防止 worker 各自扩张范围；让"为什么做这一步"可复盘 |
| 双图而不是一张大表 | 推理状态与目标事实的语义不同，混在一起会污染报告与检索 |
| 工具层强制 scope，而不是 prompt 约束 | prompt 可被绕过；网络 I/O 前的代码检查才是边界 |
| provider 抽象只做最小协议 | 不引入 SDK，离线可测；新 provider 只需实现 `chat()` |
| 事件流与 SQLite 分离 | SSE 是长连接；不让慢客户端持有数据库锁 |
| 预算耗尽返回 `budget` 而不是异常崩溃 | 已经获得的证据必须能出报告 |

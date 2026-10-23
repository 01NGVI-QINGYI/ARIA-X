# ARIA-X 融合说明（ARTEX × Cairn × PentAGI）

本文回答三个问题：

1. 三个上游各自贡献了什么**概念**；
2. 这些概念在 ARIA-X 的**哪个模块/类/函数**落地；
3. 我们**刻意没有照搬**什么，以及为什么。

> **代码归属**：ARIA-X 没有复制任何上游源代码。融合通过重新实现数据结构、状态机、
> 提示词契约与工具边界完成。上游只作为概念与行为参考。
>
> **许可证**：ARTEX 与 Cairn 是 AGPL-3.0，PentAGI 是 MIT。ARIA-X 整体因此采用
> **AGPL-3.0-or-later**。完整文本见 [../LICENSE](../LICENSE)，归属见
> [../NOTICE.md](../NOTICE.md)。

参考的上游版本：

| 上游 | 研究的版本 | 备注 |
| --- | --- | --- |
| ARTEX | 0.3.15 | 原仓库 `Autumn-27/ARTEX` 已被作者删除；本仓库仅做概念层面的参考 |
| Cairn | 0.2.1 | `oritera/Cairn`，AGPL-3.0 |
| PentAGI | 2.2.0 | `vxcontrol/pentagi`，MIT |

## 1. 总览

```
        Cairn                         ARTEX                         PentAGI
  ┌──────────────┐            ┌──────────────────┐          ┌──────────────────┐
  │ Fact/Intent/ │            │ exploration graph│          │ specialist team  │
  │ Hint blackboard           │ + asset graph    │          │ provider layer   │
  │ reason→explore            │ planner owns     │          │ sandbox terminal │
  │ OODA workers │            │ intents, todo    │          │ layered memory   │
  └──────┬───────┘            │ chain, traces,   │          └────────┬─────────┘
         │                    │ approval, report │                   │
         └────────────┬───────┴────────┬─────────┴───────────┬───────┘
                      ▼                ▼                     ▼
                         ARIA-X engine / store / tools / llm
```

ARIA-X 不是三者的拼装，而是把它们的**不变量**合并：

- 来自 Cairn：**决策必须可追溯到黑板状态**；
- 来自 ARTEX：**意图只能由 planner 生成，证据必须落到双图**；
- 来自 PentAGI：**执行必须隔离，能力必须通过工具与 provider 抽象提供**。

## 2. ARTEX → ARIA-X

| ARTEX 概念 | ARIA-X 落点 | 差异与取舍 |
| --- | --- | --- |
| 探索图（任务/意图/观察） | `models.Node`（kind=`goal/intent/fact/finding/hint/note/guide`）+ `edges`；`Store.add_node/list_nodes/add_edge/neighbors` | 统一节点表，用 `kind` 区分；Cairn 的 hint 也放进同一张图 |
| 共享资产图 | `models.Asset` + `store.assets` + `store.anchors`；`add_asset` / `anchor_node` 工具 | 资产与探索节点分离，报告可按资产 pivot |
| planner 是唯一意图生成器 | `agents/roles.planner_spec`、`agents/workers.run_planner`、`Orchestrator._reason_step/_create_intents` | worker 的工具集中没有创建 intent 的工具；只允许 planner 走 `reason` 任务 |
| 多 worker 并发探索 | `engine/scheduler.py`、`Orchestrator._dispatch`、`budget.max_concurrency` | 并发是预算维度，不是无限线程池；默认 2 |
| 跨唤醒 todo 链 | `models.Todo`、`Store.add_todo/update_todo/ready_todos`、`todo_update` 工具、`Orchestrator._apply_planner_todos` | todo 由 planner 维护，worker 只能更新被分配的项；依赖未完成的 todo 不会被调度 |
| 跨 worker 过程轨迹 | `models.Trace`、`Store.add_trace/search_traces`、`search_traces` 工具 | 轨迹是结构化记录（tool/args/result），不是自由文本日志；检索由 `memory/retrieval.py` 提供 |
| 审批门 | `Store.request_approval/decide_approval/get_approval/list_approvals`、`Orchestrator._approval_for`、`tools/base._enforce_approval`、`safety.approval` | `ToolRegistry.execute` 对 `high`/`critical` 工具强制：`auto` 放行、`deny` 拒绝、`ask` 创建审批并阻塞等待人工决定；决定写审计 |
| 运行 + 报告 | `engine/engagement.Engagement`、`report/render.write_report`、`runs/<run_id>/report.{md,html}` + `findings.json` | 报告由图数据渲染，不允许模型自由编造；模型只负责摘要文字 |
| 进程级记录（terminal 调用等） | `tools/terminal.py` 写 `Trace`，`file_*`/`http_request` 也记录调用 | 工具层统一记录，而不是每个 agent 自己记 |

**没有照搬**：ARTEX 的 Go/norma agent 内核、PostgreSQL 模式、前端与 Docker 分发方式。
ARIA-X 用 Python 标准库重新实现状态与调度，数据层换成 SQLite，以便单机、可离线、零依赖。

## 3. Cairn → ARIA-X

| Cairn 概念 | ARIA-X 落点 | 差异与取舍 |
| --- | --- | --- |
| 黑板（Blackboard） | `Store` 中的 nodes/edges/assets/todos/traces；每次 planner/worker 调用注入 `BLACKBOARD SNAPSHOT` | 黑板是持久化的，不是内存对象；跨运行仍可读 |
| Fact / Intent / Hint | `models.NODE_KINDS` 中的 `fact/intent/hint`；`add_fact` 工具；planner 产出 intent；人类 hint 通过 `POST /api/projects/<id>/hints` 或 `events.HINT` | finding 是 ARIA-X 新增的一等公民（ARTEX 影响）；hint 会被 `_consume_hints` 标记为已读 |
| reason → explore OODA | `Orchestrator._reason_step`（planner）→ `_explore`（worker）→ `_persist_outcome`；worker prompt 明确 Observe/Orient/Decide/Act | 一个 worker 只拥有一个 intent；不允许跨 intent 漫游 |
| 无固定角色的 worker | `agents/roles.worker_spec` 动态生成系统提示；worker 工具集按角色注入 | ARIA-X 仍区分 planner/worker/specialist/reporter 的**能力边界**，但 worker 本身不绑定固定"人设" |
| 状态空间搜索 | 图 + 预算 + idle/变化检测 + todo 依赖；`Orchestrator._loop` | 不实现 Cairn 的完整搜索算子库；搜索由模型提议、由代码约束 |
| 事件驱动 | `EventBus` + SSE；`events.py` 的稳定事件名 | 事件既服务 UI，也写 JSONL 供审计/回放 |
| 可观测性 | `graph` 命令、`/api/projects/<id>/graph`、`trace` 事件、报告 | 观测是产品的一部分，不是调试残留 |

**没有照搬**：Cairn 的容器构建方式、外部知识库仓库、特定模型供应商绑定。ARIA-X 的
`memory/retrieval.py` 是零依赖 TF-IDF，而不是 Cairn 的向量库方案。

## 4. PentAGI → ARIA-X

| PentAGI 概念 | ARIA-X 落点 | 差异与取舍 |
| --- | --- | --- |
| 专家团队 | `agents/roles.resolve_specialist/specialist_spec`、`agents/workers.run_specialist`、`delegate_specialist` 工具、`budget.max_specialist_depth` | 四个专家：researcher / coder / adviser / memorist；专家只能回答窄问题，不能扩张 scope |
| provider 抽象 | `llm/base.py`（`ChatMessage`/`LLMResponse`/`LLMProvider`）、`llm/router.py`、`llm/openai.py`、`llm/anthropic.py` | 不依赖官方 SDK，只用 `urllib`；`mock`/`scenario` 也是一等 provider |
| 沙箱化终端 | `tools/sandbox.py`（`LocalSandbox`/`DockerSandbox`/`MockSandbox`）、`terminal_exec`、`sandbox.backend` | 默认 `local`；`docker` 是一次性容器；`mock` 用于离线测试 |
| 工具调用循环 | `agents/base.Agent.run`：模型 → 工具 → observation → 模型，直到 JSON 契约或 `max_steps` | 每次工具调用都过 Registry、scope、命令策略与审批门（高风险工具）；输出截断、超时 |
| 分层记忆 | `memory/retrieval.py`（节点/轨迹 TF-IDF）、`memory_store`/`memory_search` 工具、`kv` 表 | 记忆是项目级的，不跨项目共享；没有外部向量数据库 |
| 任务编排 / 子代理 | `TaskScheduler` + `agents/workers` + `delegate_specialist` | 子代理深度受限（`max_specialist_depth`），防止无限递归 |
| 报告 | `report/render.py` + `REPORTER_SYSTEM` | 报告数据来自图，不允许模型编造；HTML 自包含 |

**没有照搬**：PentAGI 的 Go 后端、Graphiti/Neo4j/pgvector、Kali MCP 全家桶、Web 前端框架。
ARIA-X 的检索与记忆刻意保持零依赖，以便在无网络、无数据库服务的环境运行。

## 5. 融合后的新增能力

以下不是任一上游的直接复刻，而是融合产生的 ARIA-X 特性：

1. **统一节点表承载三家语义**：Cairn 的 fact/intent/hint、ARTEX 的 goal/finding 共存，
   通过 `kind` 与 `edges` 表达关系。
2. **双图 + 锚点**：探索图与资产图分离，但用 `anchors` 连接，报告可以按资产品汇总。
3. **预算作为一等安全属性**：时间、LLM 调用、token、工具调用、intent 总数、并发、专家深度
   全部可配；耗尽时返回 `budget` 状态并仍然出报告。
4. **离线确定性演示**：`aria_x.demo.DemoPolicy` + `examples/demo_target.py`，把
   "planner→worker→finding→report" 变成可重复的测试，而不是靠 API key 的演示。
5. **确定性场景 provider**：`llm/scenario.py` 用 JSON 脚本回放完整 engagement，
   使文档示例与回归测试不依赖模型。
6. **单一工具边界**：scope、sandbox、命令策略、审批、审计、轨迹记录都在 `ToolRegistry.execute`
   与工具实现中完成，agent 无法绕过。

## 6. 许可证影响（必须遵守）

- ARTEX 与 Cairn 是 AGPL-3.0；它们的**概念**不受版权保护，但任何**代码**复用都会使
  ARIA-X 的对应部分受 AGPL 约束。本项目选择整体 AGPL-3.0，避免边界争议。
- PentAGI 的 MIT 代码可以并入 AGPL 项目，但必须保留其版权与许可声明
  （见 [../THIRD_PARTY.md](../THIRD_PARTY.md)）。
- 通过网络提供 ARIA-X 服务时，必须按 AGPL-3.0 §13 向用户提供源码入口。
- 上游名称与徽标仅用于归属说明，不使用其商标作为 ARIA-X 的品牌。

## 7. 上游不可用时的可复现性

- ARTEX 原仓库已被作者删除，公开镜像与存档可能不再稳定。ARIA-X 不依赖任何上游工件
  即可构建（零第三方依赖）；上游仅用于概念核对与归属说明。
- Cairn 与 PentAGI 仍公开可用，建议直接参考其官方仓库。
- ARIA-X 未复制上游源码，因此不随仓库分发上游代码或二进制。

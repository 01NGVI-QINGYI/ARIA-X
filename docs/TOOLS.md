# ARIA-X 工具参考

工具是 agent **唯一**能改变世界的方式。每个工具声明：

- `name` / `description` / JSON Schema 参数；
- `risk`：`low` / `medium` / `high` / `critical`；
- `requires_scope`：是否在 I/O 前强制 `Scope` 授权；
- `read_only`：是否只读；
- `tags`：分类。

执行统一经过 `ToolRegistry.execute(name, args, ctx)`：参数校验 → `ctx.check_cancel()` →
工具实现 → 异常转换为 `ToolResult`（scope 违规、审批拒绝、其他 AriaXError、未预期异常分别处理）。
结果以 `role="tool"` 消息回给模型，并写入轨迹。

> 风险等级同时驱动**审批门**（`high`/`critical` 在 `ask`/`deny` 下被拦截）与策略提示，
> 但它不是运行时沙箱。真正的隔离来自 `sandbox.backend`（Docker）与 `Scope`；
> `critical` 目前没有内置工具使用。

## 总表（22 个）

| 名称 | 风险 | Scope | 必需参数 | 作用 |
| --- | --- | --- | --- | --- |
| `terminal_exec` | high | 命令启发式 | `command` | 沙箱内执行一条 shell 命令 |
| `file_read` | medium | — | `path` | 读取 workdir 内 UTF-8 文件 |
| `file_write` | high | — | `path`, `content` | 写入 workdir 内文件 |
| `file_list` | low | — | — | 列出 workdir 内目录 |
| `http_request` | medium | 是 | `url` | 单次 HTTP 请求 |
| `dns_lookup` | low | 是 | `host` | A/AAAA 或 PTR 查询 |
| `port_scan` | high | 是 | `host` | 有界 TCP connect 扫描 |
| `tls_probe` | medium | 是 | `host` | TLS 握手与证书信息 |
| `web_probe` | medium | 是 | `base_url` | 小字典路径探测 |
| `web_search` | low | — | `query` | 外部研究搜索（非目标流量） |
| `add_fact` | low | — | `title`, `body` | 黑板写入 fact |
| `add_finding` | medium | — | `title`, `severity`, `body` | 黑板写入 finding + 证据 |
| `add_asset` | low | — | `kind`, `value` | 资产图 upsert |
| `anchor_node` | low | — | `node_id`, `asset_value` | 节点锚定到资产 |
| `graph_query` | low | — | `view` | 查询图/发现/资产/意图/todo/轨迹 |
| `search_traces` | low | — | `query` | 检索其他 worker 的轨迹 |
| `memory_store` | low | — | `text` | 写入项目记忆 |
| `memory_search` | low | — | `query` | TF-IDF 检索项目记忆 |
| `todo_update` | low | — | — | 维护 todo 链 |
| `prove_goal` | medium | — | `summary` | 标记目标已被证据证明 |
| `delegate_specialist` | low | — | `specialist`, `question` | 委派窄问题给专家 |
| `scope_check` | low | — | `target` | 诊断 scope 判定 |

`requires_scope=是` 的工具在发起任何网络 I/O 之前调用 `Scope.require_url` / `require_host`；
违规会抛 `ScopeViolation`，被 Registry 转成失败结果并写审计。

## 终端与文件

### `terminal_exec(command, timeout?, cwd?)`

- 在 `sandbox` 中执行 `bash -lc <command>`；`cwd` 必须是 engagement workdir 的子目录；
- 先过 `CommandPolicy`：拒绝 `rm -rf /`、`mkfs`、`dd of=/dev/sd*`、fork bomb、
  关机/重启、`curl|bash`、写裸设备、`history -c` 等模式；
- 对命令中的字面 URL/IP 做 scope 启发式检查（不是完整解析，无法覆盖所有间接目标）；
- 返回 `exit_code/stdout/stderr/timed_out/duration_ms`，输出按 `sandbox.max_output` 截断；
- 超时会杀掉整个进程组（local）或 `docker kill`（docker）；
- 记录一条 `Trace`（工具名、参数、结果摘要）。

示例：

```json
{"command": "curl -sS -D - http://127.0.0.1:8000/ -o /dev/null", "timeout": 15}
```

### `file_read(path, max_bytes?)`

- 相对路径按 workdir 解析；拒绝父目录穿越、绝对路径逃逸与指向外部的符号链接；
- 只接受 UTF-8 文本（解码失败返回错误），默认上限由实现截断；
- 适合读取证据文件、扫描输出、生成的 payload。

### `file_write(path, content)`

- 同样 jail 在 workdir；自动创建父目录；
- 风险 high：模型可以写脚本，但**不能**写到 workdir 之外；
- 写入会记录轨迹；不会自动执行。

### `file_list(path?)`

- 列出目录项（名称、类型、大小）；默认 workdir；
- 只读，低风险。

## 网络（scope 强制）

所有网络工具都遵循同一顺序：**scope 授权 → 每主机限速 → 超时控制 → 截断输出 → 记录证据**。
`scope.max_rps` 是每主机令牌桶；`port_scan` 的并发上限为 32。

### `http_request(url, method?, headers?, body?, timeout?, max_bytes?)`

- 仅 `http/https`；
- 返回 `status`、选定响应头（敏感头脱敏）、body 摘要、长度、耗时；
- 保存请求/响应证据（可被 finding 引用）；
- `method` 默认 GET；支持 POST/PUT/PATCH/DELETE 等；
- 不跟随任意协议跳转；重定向由调用方决定是否继续请求（新 URL 仍需 scope）。

示例：

```json
{"url": "http://127.0.0.1:8000/files?path=../flag.txt", "method": "GET"}
```

### `dns_lookup(host, record_type?)`

- `A`/`AAAA` 通过 `getaddrinfo`；`PTR` 走反向解析；
- 结果包含解析到的地址；解析失败是正常结果，不是异常。

### `port_scan(host, ports?, timeout?, max_ports?, banners?)`

- TCP connect 扫描；`ports` 支持 `"22,80,443"` 或 `"1-100"`；
- 未指定时使用 `scope.ports`（默认常见 18 个端口）；
- `max_ports` 限制总量（默认 200，且不小于配置端口数）；
- 并发 ≤ 32，遵循 `max_rps`；`banners=true` 时做轻量 banner 读取；
- 返回开放端口、服务猜测（可选）与耗时。

### `tls_probe(host, port?, timeout?)`

- 完成 TLS 握手并读取证书；
- 返回 subject/issuer/SAN、有效期、是否自签名、TLS 版本/密码套件（可得时）；
- 握手失败是正常结果。

### `web_probe(base_url, paths?, timeout?)`

- 对少量常见路径（`/`、`robots.txt`、`sitemap.xml`、`.well-known/security.txt` 等）
  发 GET，返回状态码、长度、Content-Type；
- 路径列表可由 `paths` 覆盖；每个请求仍需 scope 授权与限速；
- 不是目录爆破器：字典很小，避免把演示变成噪声。

### `web_search(query, limit?, timeout?)`

- 查询 DuckDuckGo HTML 端点，返回 title/url/snippet；
- **不是目标流量**：用于外部知识检索，不需要 scope；
- 失败时优雅降级（返回错误结果，不重试轰炸）。

## 黑板与资产

### `add_fact(title, body, confidence?, intent_id?, asset_values?)`

- 写入 `kind=fact` 节点，status=`done`；
- 若给出 `intent_id`，建立 `yields` 边；
- `asset_values` 会把 fact 锚定到已存在的资产（不存在则先 upsert）。

### `add_finding(title, severity, body, asset_values?, intent_id?, cvss?, confidence?, evidence?)`

- `severity ∈ critical/high/medium/low/info`；
- 写入 `kind=finding` 节点（status=`done`），并把 `evidence`（字符串或结构化 dict）
  写入 `evidence` 表；文件路径会计算 sha256（存在时）；
- 自动建立 intent → finding 的 `proves` 边；
- 报告与 `findings.json` 都从这里读取，**不要**把未确认的猜测写成 finding。

### `add_asset(kind, value, parent_value?)` / `anchor_node(node_id, asset_value, kind?, note?)`

- 资产类型：root_domain/subdomain/host/ip/service/endpoint/app/credential/artifact；
- `parent_value` 建立资产层级（如 host → service）；
- `anchor_node` 建立节点与资产的多对多锚点，供报告 pivot。

### `graph_query(view, query?, limit?)`

- `view`：`overview` / `facts` / `findings` / `assets` / `intents` / `todos` / `traces`；
- 可选子串过滤与 limit；只读；
- 用于让模型在长会话中重新读取黑板，而不是依赖上下文记忆。

## 计划、轨迹与记忆

### `todo_update(todo_id?, title?, status?, detail?, intent_id?)`

- 无 `todo_id` 时创建新 todo；有则更新；
- `status`：pending/active/done/blocked/dropped；
- planner 维护计划链，worker 只更新被分配项；依赖未完成的 todo 不会被调度。

### `search_traces(query, limit?)`

- 检索**其他 task** 的轨迹（排除当前 task），避免重复踩坑；
- 返回 agent、工具、参数摘要、结果摘要与匹配片段。

### `memory_store(text, tags?)` / `memory_search(query, limit?)`

- 记忆是项目级持久文本，适合记录"有效技术、死路、环境怪癖"；
- 检索使用零依赖 TF-IDF（`memory/retrieval.py`），支持中英文分词；
- 不用于存放凭据；所有事件/报告会做 secret redaction，但不要主动写入秘密。

## 协作与控制

### `delegate_specialist(specialist, question, context?)`

- `specialist`：`researcher` / `coder` / `adviser` / `memorist`；
- 专家只回答窄问题，不共享主 agent 的完整工具权限（由 `toolset()` 决定）；
- 受 `budget.max_specialist_depth` 限制，防止递归委派；
- 返回专家的文本回答；失败会作为工具失败返回。

### `prove_goal(summary, evidence_ids?)`

- 标记 goal 节点为 `proven`，可作为完成信号；
- `evidence_ids` 会校验是否存在；完成判定仍由 Orchestrator 复核（不能只靠模型声称）。

### `scope_check(target)`

- 诊断工具：返回 `allowed/reason/host/port/ips`；
- 不发起网络请求；用于模型在行动前确认边界。

## 审批与人工介入（已强制）

`safety.approval` 支持 `auto|ask|deny`。`ToolRegistry.execute` 在参数校验之后、工具运行之前
调用 `_enforce_approval`：

| 模式 | `high` / `critical` 工具 | `low` / `medium` 工具 |
| --- | --- | --- |
| `auto`（默认） | 直接运行（仍受 scope/策略约束） | 直接运行 |
| `ask` | 创建审批记录、发出 `approval.requested` 事件，**阻塞等待**人工批准（最长 300 秒，超时或取消视为拒绝）；拒绝则工具失败 | 直接运行 |
| `deny` | 立即拒绝并写审计 | 直接运行 |

受影响的常用工具：`terminal_exec`、`file_write`、`port_scan`（均为 `high`）。
审批记录通过 `GET /api/approvals?project_id=` 查看，`POST /api/approvals/<id>`
（`{"approved": true|false, "reason": "..."}`）决定；控制台有审批队列。

即使审批通过，也**不会**扩大 scope：目标授权始终由 `Scope` 独立强制。
没有挂接审批器（例如在库中直接构造 `ToolContext`）时，`ask` 模式会安全失败（拒绝）。

## 工具开发约定

新增工具时：

1. 在对应模块创建 `ToolSpec`，写清参数 schema、风险、tags、`requires_scope`；
2. 函数签名 `fn(ctx: ToolContext, kwargs: dict) -> ToolResult`；
3. 网络 I/O 前调用 `ctx.scope.require_*`；
4. 大输出截断（`truncate`），敏感字段用 `util.redact`；
5. 状态变更写 `Store` 并 emit 事件/审计；
6. 加单元测试，跑 `bash scripts/verify.sh`。

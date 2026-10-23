# ARIA-X 安全模型与合法使用

## 0. 授权使用（先读这一节）

ARIA-X 是**进攻性安全测试工具**。它会在你配置的范围内主动发送网络请求、执行命令、
尝试发现和验证漏洞。

**你只能对以下系统使用 ARIA-X：**

- 你自己拥有并控制的系统；
- 你所在组织明确授权你测试的系统；
- 有书面授权（委托书、渗透测试合同、漏洞赏金项目 scope）覆盖的系统。

**未授权访问计算机系统在大多数司法辖区是刑事犯罪**，即使只是"看一眼"或"没有造成损害"。
"公网可见"不等于"可以测试"；"扫描不算入侵"是错误的；"客户口头说过可以"通常不足以
构成授权。

使用 ARIA-X 前请确认：

1. 你有书面授权，且授权范围（域名、IP、端口、时间窗、禁止动作）明确；
2. 你理解目标可能是生产系统，测试可能导致宕机或数据损坏；
3. 你知道所在司法辖区关于漏洞研究、数据保护与计算机滥用的法律；
4. 你已经和系统所有者约定联络人、停止条件与升级路径。

**作者与贡献者不对任何未授权或滥用行为负责。** ARIA-X 按 AGPL-3.0 以 "AS IS" 提供，
不附带任何担保。

## 1. 授权在代码中如何强制

### 1.1 启动门

`AriaXConfig.require_authorization()` 在每次 engagement 开始前检查：

```toml
[scope]
authorized = true          # 必须显式为 true
targets = ["127.0.0.1"]    # 必须非空
```

缺一即抛 `ConfigError`，不会创建 run。项目 scope 若显式 `authorized=false` 也会被拒绝。
CLI 的 `aria-x run` 与 `Engagement.run/start_background` 都走同一检查。

### 1.2 目标级 scope

`src/aria_x/tools/scope.py` 支持：

- 精确主机：`127.0.0.1`、`app.example.test`；
- 通配域名：`*.staging.example.test`（含自身与所有子域）；
- CIDR：`10.10.0.0/24`；
- 主机 + 端口：`db.example.test:5432`；
- URL 前缀：`https://app.example.test:8443/api`；
- `deny` 规则优先于 `allow`；
- `allow_private` / `allow_loopback` 控制私网与回环；
- `max_rps` 每主机限速。

网络工具（`http_request`、`dns_lookup`、`port_scan`、`tls_probe`、`web_probe`）在
**发送第一个包之前**调用 `require_url` / `require_host`。违规变成工具失败 + 审计记录，
而不是静默放行。

> **局限**：`terminal_exec` 内的工具（如 `nmap`）只能通过命令中的字面 URL/IP 做启发式
> 检查。间接寻址、脚本内目标、DNS 重绑定等无法被这层完全拦截。真正的隔离边界是
> **Docker 沙箱 + 网络策略 + 人工审查**，不是 scope 正则。


### 1.3 高风险工具的审批门

`safety.approval` 在 `ToolRegistry.execute` 中强制执行，作用于 `risk` 为 `high` 或
`critical` 的工具（当前包括 `terminal_exec`、`file_write`、`port_scan`）：

| 模式 | 行为 |
| --- | --- |
| `auto`（默认） | 直接执行，仍受 scope、workdir jail 与命令策略约束 |
| `ask` | 写入审批记录、发出 `approval.requested` 事件，并**阻塞等待**人工批准（最长 300 秒，超时/取消视为拒绝）；拒绝则工具返回失败 |
| `deny` | 直接拒绝并写审计，不执行 |

审批通过不会扩大 scope，也不会绕过命令策略。若没有挂接审批器（例如库调用方自建
`ToolContext`），`ask` 模式会安全失败而不是静默放行。审批决定可在 Web 控制台
或 `POST /api/approvals/<id>` 完成，全部写入审计表。

生产环境建议：对真实目标使用 `approval = "ask"`，至少对 `terminal_exec` 与
`file_write` 保留人工确认；demo 与自动化测试可用 `auto`。

> **局限**：审批是"人在回路"，不是技术隔离。审批者可能疲劳、误点或在不了解上下文时
> 放行；高风险 engagement 应配合 Docker 沙箱、网络分段与时间窗限制。

## 2. 威胁模型

### 2.1 我们要防的

| 威胁 | 缓解 |
| --- | --- |
| 误测未授权目标 | 启动门 + scope + deny + 默认只允许回环/私网；文档反复强调 |
| 模型"自作主张"扩张范围 | planner 独占意图、scope 在工具层强制、工具集按角色收敛 |
| 破坏性命令 | `CommandPolicy` 黑名单（`rm -rf /`、`mkfs`、`dd of=/dev/*`、fork bomb、关机、`curl|bash`、`history -c` 等）；高风险工具另受审批门控制 |
| 文件系统越界 | `file_*` 与终端 cwd 被 jail 在 engagement workdir；符号链接逃逸被拒绝 |
| 密钥泄漏到日志/报告 | 事件/轨迹/报告写出前 `util.redact`；`public_dict()` 遮蔽 API key 与 token |
| Web 控制台被远程滥用 | 默认只绑定 `127.0.0.1`；可选 Bearer token；建议反向代理 + TLS |
| 预算失控/无限循环 | 时间、LLM 调用、token、工具调用、intent 总数、并发、专家深度全部受限 |
| 进程残留 | `Engagement.close()` 先取消、再 join（10s 上限）；demo target 处理 SIGTERM |
| 报告编造 | 报告从图数据渲染；模型只写摘要文本；finding 必须落库 |

### 2.2 我们不防的

- **恶意模型/被投毒的模型服务**：provider 返回的内容会被当作不可信输入，但 ARIA-X 不会
  执行模型给出的 shell 以外的东西；工具参数仍受 schema、scope、策略约束。若模型服务本身
  被攻陷，它可以看到发送给它的上下文（包括目标数据）。
- **目标对测试者的反制**：恶意目标可以返回针对模型的 prompt injection。ARIA-X 把工具输出
  作为数据而非指令，但没有专门的 prompt-injection 分类器；高风险场景应由人工监督。
- **供应链**：ARIA-X 无第三方 Python 依赖，但仍依赖 CPython、操作系统与可选 Docker 镜像。
  请自行校验基础镜像与工具链。
- **物理/主机层攻击**：ARIA-X 不是沙箱逃逸防护产品；在不可信主机上运行本身就不安全。

## 3. 最小权限部署清单

- [ ] `scope.targets` 只列授权资产；用 `deny` 排除共享基础设施；
- [ ] `scope.authorized=true` 只在本次授权窗口内设置；
- [ ] `safety.approval="ask"`，至少覆盖 `terminal_exec` / `file_write` / `port_scan`；
- [ ] `sandbox.backend="docker"`，镜像固定版本，`network` 按需（默认 `none`）；
- [ ] 不使用 `--privileged`，不挂载 `/var/run/docker.sock`，不挂载宿主根目录；
- [ ] `server.host="127.0.0.1"`；远程访问走 SSH 隧道或带 TLS 的反向代理；
- [ ] 设置 `ARIA_X_AUTH_TOKEN`（长随机值），不要用默认值；
- [ ] `ARIA_X_API_KEY` 只放环境变量，不写进仓库；定期轮换；
- [ ] `data/`、`runs/` 权限限制为运行用户；报告可能含目标数据；
- [ ] 运行前 `python -m aria_x doctor --llm`，确认配置与连通性；
- [ ] 运行中通过 `aria-x serve` 观察事件与预算；异常时 `POST /api/runs/<id>/stop`；
- [ ] 结束后审查 `runs/<run_id>/` 与 `data/aria-x.db`，按合同要求保存/销毁。

## 4. 秘密处理

- `llm.api_key` 可来自 `ARIA_X_API_KEY`；`server.auth_token` 可来自 `ARIA_X_AUTH_TOKEN`。
- `util.redact` 覆盖：`Authorization: Bearer/Basic ...`、`api_key/token/secret/password/
  session=...`、`sk-...`、AWS `AKIA...`、GitHub `ghp_/gho_/...`。
- 事件总线默认开启 redaction（`safety.redact_secrets=true`）；JSONL、SSE、报告与
  `public_dict()` 使用同一逻辑。
- **redaction 是尽力而为**：自定义密钥格式、图片/二进制内容、模型在自由文本中复述的
  秘密可能漏过。不要把 ARIA-X 当作 DLP 产品；敏感 engagement 的报告应人工复核后再分发。
- 不要把真实凭据写入示例配置、scenario JSON 或测试夹具。

## 5. Demo target 的安全设计

`examples/demo_target.py` 故意包含三个弱点，但被限制为**无害**：

1. **路径穿越**：`/files?path=../flag.txt` 使用朴素 `os.path.join`，会逃出 `public/`；
   但处理器随后只允许读取两个文件：`public/` 子树内文件与生成的 `flag.txt`。
   `/etc/passwd` 等任意路径返回 404。
2. **缺失安全响应头**：没有 CSP/HSTS/X-Frame-Options/nosniff——用于演示扫描器发现项。
3. **`/debug` 信息泄露**：返回假版本号与 `debug=true`。

其他约束：

- 只绑定 `127.0.0.1`，不接受外部连接；
- 每次启动创建一次性临时根目录，退出即丢弃；
- 不执行命令、不写系统路径、不发起网络请求；
- 支持 SIGTERM/SIGINT 干净退出；
- `--json` 只向 stdout 输出一行启动 JSON，日志走 stderr。

**不要**把 demo target 暴露到公网，也不要在其中放入真实数据。

## 6. 漏洞披露（针对 ARIA-X 自身）

如果你发现 ARIA-X 的安全问题：

1. **不要**立即公开可复现细节或 exploit；
2. 通过项目维护者提供的私密渠道报告（仓库 SECURITY 联系方式 / 维护者邮件）；
3. 提供：受影响版本、复现步骤、影响评估、建议修复；
4. 给维护者合理的修复窗口（通常 90 天），期间不要公开；
5. 修复发布后，按双方约定致谢。

范围包括：scope 绕过、沙箱逃逸、审批绕过、认证绕过、路径穿越、命令注入、
秘密泄漏、报告 XSS、SSE/HTTP 拒绝服务等。

## 7. 数据与隐私

- ARIA-X 会把目标响应、命令输出、证据摘要写入 `data/aria-x.db`、`data/events.jsonl` 与
  `runs/`。这些数据可能包含个人信息、凭据或商业机密。
- 运行前确认数据保留期限与存储位置符合授权合同与当地法律（GDPR、个人信息保护法等）。
- 删除一次 engagement：停止服务后删除对应 `runs/<run_id>/`，并用 `aria-x export` 备份
  需要保留的结论；`data/aria-x.db` 是共享库，删除单个项目需要 SQL（当前没有 CLI 子命令）。
- 不要把包含真实目标数据的报告提交到公开仓库。

## 8. 合规提示

- **AGPL-3.0**：通过网络提供 ARIA-X 服务时，必须向用户提供对应源码（§13）。建议在控制台
  页脚放置 "Source" 链接指向你的仓库镜像。
- **出口管制/制裁**：某些司法辖区限制渗透测试工具或加密工具的出口；请自行确认。
- **合同优先**：授权合同中的限制（禁止 DoS、禁止数据外带、禁止社会工程等）高于 ARIA-X 的
  任何默认行为；必要时通过 `deny`、`budget`、`sandbox.network="none"` 收紧。

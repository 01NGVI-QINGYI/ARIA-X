# ARIA-X 路线图

本文件区分三件事：**已经能用的**、**下一步要做的**、**明确不做的**。
未实现的内容不会出现在 README 的"特性"里。

## v0.1（当前）—— 已实现

### 引擎与状态

- [x] SQLite 持久化：项目、运行、节点、边、资产、锚点、todo、任务、轨迹、证据、消息、
      审计、审批、kv（`store/schema.py`）。
- [x] 双图模型：探索图（nodes/edges）+ 资产图（assets/anchors）。
- [x] planner 唯一意图生成；intent 去重/上限；todo 依赖调度。
- [x] bootstrap → reason → dispatch → explore → persist → report 主循环。
- [x] 预算：时间、LLM 调用、token、工具调用、intent 总数、并发、专家深度。
- [x] 运行状态 `completed/budget/cancelled/failed`；预算耗尽仍出报告。
- [x] **审批门强制**：`high`/`critical` 工具在 `safety.approval=ask` 时阻塞等待人工批准，
      `deny` 直接拒绝，`auto` 放行；决定写审批表与审计。
- [x] 后台运行、停止、状态查询；进程内 Run 注册表。
- [x] 事件总线 + JSONL + SSE 重放；secret redaction。
- [x] 项目导出 JSON。

### 模型

- [x] OpenAI 兼容 provider（DeepSeek/vLLM/Ollama 等），原生 tool calling。
- [x] Anthropic provider。
- [x] `mock` provider（列表/policy）。
- [x] `scenario` provider（JSON 脚本、角色队列、子串匹配、循环/内联 JSON）。
- [x] JSON 契约解析与重试；分角色模型；token 用量估算。

### 工具

- [x] 22 个工具：终端/文件、HTTP/DNS/端口/TLS/Web 探测、外部搜索、黑板、资产、
      计划、轨迹、记忆、专家委派、目标证明、scope 诊断。
- [x] 工具注册/参数校验/取消检查/异常边界。
- [x] `Scope`：主机/通配/CIDR/URL 前缀/deny/回环/私网/限速。
- [x] 沙箱：`local`、`docker`（一次性容器）、`mock`；破坏性命令策略。
- [x] workdir jail、输出截断、证据与轨迹记录。

### Agent

- [x] planner / bootstrap / worker / specialist（researcher/coder/adviser/memorist）/
      reporter 规格与 worker 运行时。
- [x] 专家委派深度限制。
- [x] 角色提示词与 JSON 契约。

### 报告与界面

- [x] `report.md`、自包含 `report.html`、`findings.json`。
- [x] 零依赖 TF-IDF 记忆检索。
- [x] HTTP API（项目/图/轨迹/审计/报告/运行/停止/hint/审批/导出/SSE）。
- [x] 无框架、无 CDN 的 Web 控制台（随 server 落地；截图待界面定稿后补充）。
- [x] 可选 Bearer token；默认绑定 `127.0.0.1`。

### 工程与合规

- [x] 标准库零依赖；Python 3.11+。
- [x] `unittest` 测试套件 + 离线 E2E（真实目标 + 真实引擎 + 脚本策略）。
- [x] `scripts/bootstrap.sh`、`dev.sh`、`verify.sh`（无 pip、无网络）。
- [x] Dockerfile / compose / Kali 沙箱文档。
- [x] AGPL-3.0 LICENSE、NOTICE、THIRD_PARTY、FUSION 映射。
- [x] 中文 README + English README；架构/工具/安全/部署文档。

## v0.2 —— 下一步（按优先级）

### 安全与正确性

- [ ] **审批策略细化**：按工具/参数/目标配置审批规则（当前是按风险等级的统一策略），
      以及审批超时与自动拒绝。
- [ ] **运行恢复**：进程崩溃后从 Run/Task/Trace 恢复未完成的 intent，而不是只能新开 run。
- [ ] **prompt injection 标记**：把目标响应标记为不可信数据，增加简单的注入特征提示与
      高风险动作二次确认。
- [ ] **scope 的 DNS 固定**：解析一次后固定 IP，降低 DNS rebinding 风险；当前每次工具调用
      都可能重新解析。
- [ ] **per-run 网络策略文档化**：Docker 网络与 scope 的组合矩阵、隔离 lab 参考拓扑。

### 能力

- [ ] **更多工具**：目录/参数发现（有界）、OpenAPI/GraphQL 探测、JWT 解析、截图证据
      （需要可选的 headless 浏览器，保持标准库默认路径不受影响）。
- [ ] **攻击路径图**：报告中的 fact → intent → finding 可视化（当前是文字叙述）。
- [ ] **多目标项目**：一个项目内按资产分组并行，当前并发是全局 intent 级。
- [ ] **报告 PDF**：在零依赖前提下不引入重库；优先提供"用浏览器打印"的说明。
- [ ] **更细的预算**：按角色/工具类别配额，当前是全局维度。

### 体验

- [ ] Web 控制台截图、GIF、演示视频；
- [ ] 中文/英文 UI 切换（当前报告语言由 `engine.language` 控制）；
- [ ] `aria-x import`：从 JSON 导出恢复只读项目视图；
- [ ] 更友好的 `doctor` 输出（依赖图、端口占用、模型延迟）。

### 工程

- [ ] CI（GitHub Actions）：`unittest` + `verify.sh` + 文档链接检查；
- [ ] 覆盖率门槛与核心路径的 property-based 测试（仍用标准库）；
- [ ] 发布流程：版本号、CHANGELOG、容器镜像签名/SBOM；
- [ ] 性能：大图（>10k 节点）下的 `graph_overview` 分页与索引调优。

## 明确不做

- **自动化武器化/大规模利用**：不提供蠕虫化传播、批量 0day 利用、持久化后门、
  凭据大规模窃取等功能。ARIA-X 的定位是授权评估与证据生产。
- **规避检测/反取证**：不做 stealth、日志清除、EDR 绕过、C2 隐藏。
- **未授权目标发现**：不做互联网资产测绘、随机 IP 扫描、Shodan 式发现。
- **多租户 SaaS**：当前是单机单团队工具；不做公网多租户控制面。
- **内置漏洞库/exploit 数据库**：模型可以生成 payload，但 ARIA-X 不分发 exploit 集合。
- **替代人工授权判断**：`scope.authorized=true` 是人的声明，工具不会替你获得授权。

## 如何贡献

1. 先读 [CONTRACTS.md](CONTRACTS.md) 与 [ARCHITECTURE.md](ARCHITECTURE.md)；
2. 一个 PR 只做一件事；新增工具必须带 schema、风险标注与测试；
3. 网络相关代码必须先 scope 后 I/O；不得引入第三方 Python 依赖；
4. 文档改动必须与代码一致；不要写"计划中"的功能到 README 的特性里；
5. 许可证：贡献以 AGPL-3.0 授权；涉及上游代码必须先讨论归属。

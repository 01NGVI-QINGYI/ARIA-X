# NOTICE — 归属、上游许可与合规声明

Copyright (C) 2026 01NGVI-QINGYI

ARIA-X（Autonomous Reasoning & Investigation Agent, eXtended）是一个独立实现，其设计思想融合了三个上游开源项目。
**ARIA-X 整体以 GNU Affero General Public License v3.0（AGPL-3.0-or-later）发布**，完整许可文本见
[LICENSE](LICENSE)。

## 1. 为什么 ARIA-X 采用 AGPL-3.0

ARIA-X 的两个主要思想来源以 AGPL-3.0 发布：

| 上游项目 | 许可证 | 在 ARIA-X 中的角色 |
| --- | --- | --- |
| [Cairn](https://github.com/oritera/Cairn)（作者：[oritera / 起零衍迹](https://github.com/oritera)） | **AGPL-3.0** | 黑板架构：Fact / Intent / Hint、reason→explore 的 OODA 循环 |
| ARTEX（原作者：[Autumn-27](https://github.com/Autumn-27)；原仓库 `Autumn-27/ARTEX` 已被作者删除） | **AGPL-3.0** | 双图（探索图 + 共享资产图）、planner 唯一意图生成器、跨唤醒 todo 链、跨 worker 过程轨迹、审批门 |
| [PentAGI](https://github.com/vxcontrol/pentagi)（团队：[VXControl](https://github.com/vxcontrol)） | **MIT** | 专家团队（researcher/coder/adviser）、provider 抽象、沙箱终端、分层记忆 |

MIT 与 AGPL-3.0 兼容（MIT 代码可以并入 AGPL 项目）。ARIA-X 主动选择与两个 AGPL 上游相同的
**AGPL-3.0-or-later**，既保持许可证一致性，也覆盖未来任何代码级复用可能产生的衍生边界。因此：

* ARIA-X 的发布、修改、网络服务部署均须遵守 AGPL-3.0 第 13 条：通过网络向用户提供服务时，
  必须向用户提供对应源码（例如在 Web 控制台底部放置 "Source" 链接）。
* 任何分发 ARIA-X 的二进制或容器镜像，都必须附带 [LICENSE](LICENSE) 与本 NOTICE。

## 2. 代码归属事实

- **ARIA-X 未复制上述三个项目的源代码。** 融合发生在架构与协议层：数据结构、状态机、
  提示词契约、工具边界与调度策略均按 ARIA-X 自身需求重新实现（Python 标准库）。
- 如果未来引入任何上游代码片段，必须：
  1. 在该文件头注明来源与原始许可证；
  2. 保持 AGPL-3.0 兼容；
  3. 更新本 NOTICE 与 [THIRD_PARTY.md](THIRD_PARTY.md)。
- 上游项目的名称、徽标与商标归各自作者所有；本仓库只在归属说明中引用名称。

## 3. 上游许可文本在哪里

本仓库不内嵌上游完整源码，因此不重复放置其许可全文，但保留以下官方定位信息：

| 上游 | 许可文本位置 |
| --- | --- |
| Cairn | [oritera/Cairn — LICENSE](https://github.com/oritera/Cairn/blob/main/LICENSE) |
| ARTEX | 原仓库已删除，无可引用的官方 LICENSE 链接；ARIA-X 未复制其代码 |
| PentAGI | [vxcontrol/pentagi — LICENSE](https://github.com/vxcontrol/pentagi/blob/main/LICENSE)（MIT） |

PentAGI 的 MIT 许可全文与版权声明复制在 [THIRD_PARTY.md](THIRD_PARTY.md) 中，以满足 MIT 的
"保留版权与许可声明"要求。

## 4. 无担保与合法使用

ARIA-X 按 AGPL-3.0 第 15–17 条以 **"AS IS" 无任何担保** 提供。ARIA-X 是安全测试工具：
**仅可用于你拥有书面授权或依法有权测试的系统**。详见 [docs/SECURITY.md](docs/SECURITY.md)
的合法使用章节。作者与贡献者对任何滥用或由此产生的损害不承担责任。

## 5. 联系与贡献

- 提交贡献即表示你同意以 AGPL-3.0 授权你的贡献。
- 安全问题请遵循 [docs/SECURITY.md](docs/SECURITY.md) 的披露流程，不要公开提交可利用细节。

# THIRD_PARTY — 第三方组件、依赖与致谢

## 1. 运行时依赖

ARIA-X 的运行时**只使用 Python 3.11+ 标准库**（目标 3.12），`pyproject.toml` 中
`dependencies = []`。因此不存在需要随包分发的第三方 Python 包，也不存在 SBOM 中的
Python 传递依赖。

| 类别 | 依赖 | 说明 |
| --- | --- | --- |
| 语言运行时 | CPython ≥ 3.11 | 仅标准库：`http.server`、`sqlite3`、`threading`、`tomllib`、`unittest` 等 |
| 可选容器 | Docker / Podman | 仅在 `sandbox.backend = "docker"` 时调用，用于 Kali 沙箱 |
| 可选外部服务 | 任意 OpenAI 兼容 API 或 Anthropic API | 通过 HTTP 调用，不绑定 SDK |

不随仓库分发的开发期工具（`bash`、`curl`、`docker`）不属于运行时依赖。

## 2. 思想来源与归属（非代码依赖）

ARIA-X 融合了三个项目的设计概念，但**没有复制其代码**。完整合规说明见
[NOTICE.md](NOTICE.md)。

| 项目 | 许可证 | 版权持有者 | ARIA-X 借鉴的概念 |
| --- | --- | --- | --- |
| Cairn | AGPL-3.0 | oritera / Cairn contributors | 黑板 Fact/Intent/Hint、reason→explore OODA、无固定角色的 worker |
| ARTEX | AGPL-3.0 | Autumn-27 / ARTEX contributors | 双图分离、planner 独占意图生成、共享 todo 链、跨 worker 轨迹、审批门 |
| PentAGI | MIT | PentAGI Development Team (2025) | 专家团队、provider 抽象、沙箱终端、分层记忆 |

### PentAGI MIT 许可全文

```
MIT License

Copyright (c) 2025 PentAGI Development Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 3. 字体、图标与前端资源

- `assets/logo.svg`、`assets/architecture.svg` 为 ARIA-X 原创 SVG，使用系统字体栈
  （`ui-sans-serif, system-ui, ...`），不内嵌第三方字体文件。
- Web 控制台（`src/aria_x/web/`）不加载 CDN 资源、不使用第三方 JS 框架，
  离线环境可完整运行。
- 示例目标 `examples/demo_target.py` 使用标准库 `http.server`，不包含第三方代码。

## 4. 数据与商标

- 仓库内不包含任何真实客户数据、凭据或扫描结果。
- "Cairn"、"ARTEX"、"PentAGI" 等名称仅用于归属说明，相关商标归各自权利人所有。

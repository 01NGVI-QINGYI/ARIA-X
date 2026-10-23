# ARIA-X 部署指南

ARIA-X 是单进程、标准库优先的应用：一个 Python 解释器、一个 SQLite 文件、一个可选的
Docker 守护进程。没有数据库服务、消息队列、Redis 或前端构建步骤。

> 部署前请先读 [SECURITY.md](SECURITY.md) 的授权与最小权限清单。

## 1. 环境要求

| 组件 | 要求 |
| --- | --- |
| Python | CPython **≥ 3.11**（推荐 3.12）；仅标准库 |
| 操作系统 | macOS / Linux；Windows 未验证（路径与信号语义可能不同） |
| 磁盘 | `data/`（SQLite + 事件）+ `runs/`（报告）；建议 ≥ 1 GB 可用 |
| 可选 | Docker / Podman（仅 `sandbox.backend="docker"` 时） |
| 网络 | 真实 engagement 需要到目标与模型 API 的网络；`aria-x demo` 完全离线 |

推荐直接使用 `python3.12`（或设置 `ARIA_X_PYTHON`）：

```bash
python3.12 --version
```

`scripts/_common.sh` 的发现顺序：`$ARIA_X_PYTHON` → `python3.12` → `python3.11` →
满足 ≥3.11 的 `python3`。

## 2. 获取与启动

```bash
cd "/path/to/ARIA-X"
export PYTHONPATH="$PWD/src"

python -m aria_x doctor              # 环境自检
python -m aria_x demo                # 离线端到端验证
python -m aria_x init                # 写入 configs/aria-x.toml
python -m aria_x serve --open        # Web 控制台
```

ARIA-X 不需要安装成包。若你希望 `aria-x` 命令可用，可自行创建 shell alias：

```bash
alias aria_x='PYTHONPATH="/path/to/ARIA-X/src" python3.12 -m aria_x'
```

（`pyproject.toml` 提供了 `aria-x = "aria_x.cli:main"` 的入口点，但文档不依赖 `pip install`。）

## 3. 配置

### 3.1 加载顺序

默认值 < 配置文件 < 环境变量 < 显式覆盖。

配置文件查找顺序：

1. `-c/--config <path>`（命令行显式指定）；
2. `$ARIA_X_CONFIG`；
3. `configs/aria-x.toml`、`aria-x.toml`、`configs/aria-x.json`、`aria-x.json`（当前目录）。

TOML 需要 Python 3.11+；JSON 始终可用。**相对路径（`data_dir`、`runs_dir`、
`llm.scenario`）按当前工作目录解析**，所以推荐在仓库根目录运行。

### 3.2 环境变量

| 变量 | 作用 |
| --- | --- |
| `ARIA_X_CONFIG` | 配置文件路径 |
| `ARIA_X_API_KEY` | 模型 API key（优先于配置文件） |
| `ARIA_X_PROVIDER` | `openai` / `anthropic` / `ollama` / `mock` / `scenario` |
| `ARIA_X_BASE_URL` | OpenAI 兼容 endpoint |
| `ARIA_X_MODEL` | 模型名 |
| `ARIA_X_DATA_DIR` | 数据目录（SQLite、事件） |
| `ARIA_X_AUTH_TOKEN` | Web API Bearer token |
| `ARIA_X_LOG_LEVEL` | `INFO` / `DEBUG` 等 |
| `ARIA_X_SANDBOX` | `local` / `docker` / `mock` |
| `ARIA_X_APPROVAL` | `auto` / `ask` / `deny` |

### 3.3 最小配置示例

```toml
name = "Staging review"
data_dir = "data"
runs_dir = "runs"

[scope]
authorized = true
targets = ["*.staging.example.test", "10.20.0.0/24"]
deny = ["prod.staging.example.test"]
allow_private = true
allow_loopback = true
max_rps = 3.0

[llm]
provider = "openai"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
temperature = 0.2
max_tokens = 4096

[budget]
max_seconds = 1800
max_llm_calls = 160
max_tokens = 1200000
max_tool_calls = 400
max_concurrency = 2
max_total_intents = 40

[sandbox]
backend = "docker"
image = "aria-x-kali:latest"
network = "bridge"
timeout = 120

[safety]
approval = "auto"
block_destructive = true
redact_secrets = true

[server]
host = "127.0.0.1"
port = 8787
auth_token = ""

[engine]
language = "zh"
bootstrap = true
max_idle_rounds = 2
```

## 4. 模型接入

### 4.1 OpenAI 兼容（DeepSeek / vLLM / LM Studio / Ollama）

```toml
[llm]
provider = "openai"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
```

```bash
export ARIA_X_API_KEY="sk-..."
python -m aria_x doctor --llm
```

Ollama 可直接用 `provider="ollama"`（默认 `http://127.0.0.1:11434/v1`）或
`provider="openai"` + 自定义 `base_url`。

### 4.2 Anthropic

```toml
[llm]
provider = "anthropic"
base_url = "https://api.anthropic.com"
model = "claude-sonnet-4-20250514"
```

```bash
export ARIA_X_API_KEY="sk-ant-..."
```

### 4.3 分角色模型

```toml
[llm.role_models]
planner = "deepseek-reasoner"
worker = "deepseek-chat"
reporter = "deepseek-chat"
```

### 4.4 离线 provider

- `mock`：回放列表或 policy 函数，主要用于测试；
- `scenario`：`llm.scenario = "examples/scenario_demo.json"`，按角色队列回放，
  用于文档示例与回归测试。并行时队列会被并发消费，确定性场景请设 `max_concurrency=1`。

## 5. Web 控制台与 API

```bash
export ARIA_X_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
python -m aria_x serve --host 127.0.0.1 --port 8787
```

- 默认只绑定 `127.0.0.1`。需要远程访问时，**优先**用 SSH 隧道：
  `ssh -L 8787:127.0.0.1:8787 user@host`；
- 或放在带 TLS 的反向代理（Caddy/Nginx）之后，并设置 `ARIA_X_AUTH_TOKEN`；
- ARIA-X **不自带 TLS**。直接在公网监听等于明文传输 token 与目标数据；
- SSE 端点 `GET /api/projects/<id>/events` 支持 `?token=`（EventSource 不能设置请求头）；
- health：`curl -H "Authorization: Bearer $ARIA_X_AUTH_TOKEN" http://127.0.0.1:8787/api/health`。

## 6. Docker

### 6.1 构建与运行

```bash
docker build -f docker/Dockerfile -t aria-x:0.1.0 .
mkdir -p data runs
docker run --rm -p 127.0.0.1:8787:8787 \
  --user "$(id -u):$(id -g)" \
  -e ARIA_X_AUTH_TOKEN="$(openssl rand -hex 16)" \
  -v "$PWD/data:/app/data" -v "$PWD/runs:/app/runs" \
  aria-x:0.1.0
```

镜像只包含 ARIA-X 本体（标准库），**不包含**扫描工具。镜像默认以 UID 10001 的非 root 用户运行；
绑定挂载的宿主机目录通常属于你的 UID，因此示例用 `--user "$(id -u):$(id -g)"` 让容器进程
与宿主机目录所有者一致（否则写入 `data/` 可能被拒绝）。容器只写挂载的 `data/`、`runs/`
与 `/tmp`。

### 6.2 Compose

```bash
export ARIA_X_AUTH_TOKEN="$(openssl rand -hex 16)"
export ARIA_X_API_KEY="sk-..."
export ARIA_X_UID="$(id -u)" ARIA_X_GID="$(id -g)"   # match the mounted data dir owner
docker compose -f docker/docker-compose.yml up --build
```

`docker/docker-compose.yml` 还提供一个 `sandbox` profile 的 Kali 容器作为参考；它默认不启动，
也不与 ARIA-X 容器共享网络。

### 6.3 在容器里跑 ARIA-X + Docker 沙箱

`docker/Dockerfile` 不安装 Docker CLI，因此容器内的 `sandbox.backend` 应保持 `local`
（容器本身即隔离层）。若要在 ARIA-X 容器内再启动工具容器，需要挂载宿主
`/var/run/docker.sock`——这等于把宿主 root 交给容器内进程，**ARIA-X 不推荐**。
更安全的模式是：ARIA-X 跑在宿主机（或专用 VM），只把工具容器交给 Docker 沙箱。

Kali 镜像与 `sandbox.backend="docker"` 的完整说明见
[docker/kali-sandbox.md](../docker/kali-sandbox.md)。

## 7. 数据目录、备份与恢复

```
data/
  aria-x.db            # SQLite（WAL：还有 -wal/-shm）
  events.jsonl       # 追加式事件流
runs/
  <run_id>/report.md|html|findings.json
  demo-<stamp>/<run_id>/...
```

备份（停止写入后）：

```bash
python -m aria_x export -o backup-export.json     # 逻辑导出（推荐跨版本）
sqlite3 data/aria-x.db ".backup 'aria-x-backup.db'" # 物理备份（需要 sqlite3 CLI）
```

恢复：

- 逻辑导出适合审阅与归档；ARIA-X 当前没有 `import` 子命令，恢复运行状态需要保留
  `data/aria-x.db` 本身；
- 直接把备份的 `aria-x.db`/`runs/` 放回原位置，然后 `aria-x projects` 验证。

升级：ARIA-X 处于 v0.1，schema 可能变化。升级前备份 `data/` 与 `runs/`；若启动时报
schema 不兼容，保留备份并查阅 release notes。

## 8. 排障

| 现象 | 检查 |
| --- | --- |
| `ModuleNotFoundError: aria_x` | 没设 `PYTHONPATH=$PWD/src`，或不在仓库根目录 |
| `refusing to start: scope.authorized is false` | 这是授权门；确认有书面授权后再设 true |
| `refusing to start: scope.targets is empty` | 至少配置一个 target |
| `config file not found` | `-c` 路径、`ARIA_X_CONFIG` 或当前目录的 `configs/aria-x.toml` |
| `llm.api_key is required` | `export ARIA_X_API_KEY=...` 或配置 `llm.api_key` |
| `address already in use` | 换 `server.port` 或 `--port`；确认没有旧进程 |
| Docker 沙箱 `image not present` | `docker pull <image>` 或先构建 `aria-x-kali` |
| `docker daemon not running` | 启动 Docker Desktop / `systemctl start docker` |
| 预算是 `budget` 状态 | 正常收尾；调大 `budget.*` 或缩窄目标 |
| 工具一直 scope 拒绝 | `python -m aria_x tools` 看 `scope_check`；确认 URL 主机/端口/前缀覆盖 |
| Web 控制台 401 | 设置/携带 `Authorization: Bearer $ARIA_X_AUTH_TOKEN` |
| SSE 不更新 | 反代需关闭 buffering；或用 `?token=` 直连 |
| 运行卡住 | 查看 `/api/runs/<id>` 的 tasks 与 `data/events.jsonl`；`POST /api/runs/<id>/stop` |

## 9. 生产前检查清单

- [ ] `bash scripts/verify.sh` 通过；
- [ ] `python -m aria_x doctor --llm` 通过；
- [ ] `scope.authorized` 与 `scope.targets` 来自书面授权，`deny` 覆盖共享设施；
- [ ] `server.host` 为 `127.0.0.1` 或位于 TLS 反代之后；
- [ ] `ARIA_X_AUTH_TOKEN` 已设置且非默认值；
- [ ] `sandbox.backend="docker"`，镜像固定版本，未使用 `--privileged`；
- [ ] `data/`、`runs/` 权限与保留期符合合同；
- [ ] 已约定停止条件、联络人与应急流程；
- [ ] 报告分发前人工复核敏感信息。

## 10. 卸载/清理

```bash
# 停止进程后
rm -rf data runs                      # ⚠️ 会删除全部 engagement 状态与报告
docker ps -a --filter name=aria-x- --format '{{.ID}}' | xargs -r docker rm -f
docker image rm aria-x:0.1.0 aria-x-kali:latest 2>/dev/null || true
```

删除前请确认路径，避免误删其他项目。

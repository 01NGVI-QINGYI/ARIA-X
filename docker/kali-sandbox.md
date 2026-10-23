# Docker 沙箱与 Kali 工具镜像

ARIA-X 的默认沙箱是 `local`（直接在当前用户权限下执行命令）。要获得真正的隔离边界，
把 `sandbox.backend` 切换为 `docker`，并用 Kali 镜像承载 `nmap`、`ffuf`、`nuclei` 等工具。

> **安全提示**：Docker 沙箱不是 scope 边界。目标授权由 ARIA-X 的 `Scope` 与网络工具强制执行；
> Docker 只负责命令隔离、资源限制和可丢弃的文件系统。不要为了“方便”把宿主目录、
> `docker.sock` 或宿主机网络挂进工具容器。

## 1. 准备镜像

```bash
docker pull kalilinux/kali-rolling
# 安装常用工具的派生镜像（按需选择，构建需要网络）
cat > /tmp/Dockerfile.kali <<'DOCKER'
FROM kalilinux/kali-rolling
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      nmap curl whatweb ffuf gobuster sqlmap nikto dnsutils openssl \
      python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
DOCKER
docker build -f /tmp/Dockerfile.kali -t aria-x-kali:latest .
```

如果只需要 ARIA-X 自带的 HTTP/TCP 工具，`kalilinux/kali-rolling` 原镜像即可，
无需安装任何软件包。

## 2. 配置 ARIA-X

```toml
[sandbox]
backend = "docker"
image = "aria-x-kali:latest"   # 或 kalilinux/kali-rolling
network = "none"             # 需要访问目标时改为 "bridge"
timeout = 120

[scope]
authorized = true
targets = ["127.0.0.1", "*.lab.example.test"]
```

```bash
PYTHONPATH=src python -m aria_x doctor     # 会检查 docker 守护进程与镜像
```

## 3. 行为与限制

- 每个 `terminal_exec` 启动**一次性容器**（`docker run --rm -i`），
  `/work` 挂载当前 engagement 的工作目录；容器退出即销毁。
- `network = "none"` 时没有外网。要扫描实验目标，使用 `bridge`，
  并且只把目标放在隔离的 lab 网络里。
- 镜像内命令以容器 root 运行（上游 Kali 镜像的默认行为）。ARIA-X 的外层策略
  `CommandPolicy` 会拦截 `rm -rf /`、`mkfs`、`dd of=/dev/sd*`、fork bomb 等破坏性命令，
  但这只是启发式，不是完整沙箱。
- 超时会执行 `docker kill`；输出会被截断到 `sandbox.max_output` 字节。
- 不使用 `--privileged`，不挂载 `/var/run/docker.sock`，不挂载宿主根目录。

## 4. 在容器里跑 ARIA-X 本体

`docker/Dockerfile` 只包含 ARIA-X 本体（标准库，无 pip）。它**不包含** Docker CLI，
因此容器内的 `sandbox.backend` 只能使用 `local`（容器本身即隔离层）。
若确实需要“容器内再开容器”，必须自行安装 Docker CLI 并挂载宿主 socket——
这等于把宿主 root 权限交给容器内进程，ARIA-X 不推荐也不默认支持。

## 5. 清理

```bash
docker ps -a --filter name=aria-x- --format '{{.ID}}' | xargs -r docker rm -f
docker image rm aria-x-kali:latest
```

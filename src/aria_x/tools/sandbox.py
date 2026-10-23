"""Command execution backends and the destructive-command policy.

Three backends are provided:

* ``local``  — subprocesses on the host, constrained to the engagement workdir.
* ``docker`` — one-shot containers (Kali image by default), the recommended
  isolation boundary for real tooling.
* ``mock``   — deterministic outputs for tests and the offline demo.

The sandbox is *not* the scope guard. Scope is enforced by the network tools;
the sandbox enforces isolation and this module enforces a destructive-command
policy.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import SandboxConfig
from ..errors import SandboxError
from ..util import truncate

_DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(?:-[A-Za-z]*\s+)*-(?:rf|fr)\s+"
                r"(?:/|/\*|~|\$HOME|/(?:etc|usr|var|bin|sbin|boot|dev|System|Library|Applications)(?:/|\s|$))"
                r"(?:\s|$|[;&|])"),
     "recursive delete of a protected root/home path"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|disk|rdisk)"), "raw device write"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "host power control"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/\s*$"), "world-writable root"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(bash|sh)\b"), "pipe remote content to a shell"),
    (re.compile(r">\s*/dev/(sd|nvme|disk|rdisk)"), "raw device overwrite"),
    (re.compile(r"\bhistory\s+-c\b"), "audit trail destruction"),
)


@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    command: str = ""
    backend: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "command": self.command,
            "backend": self.backend,
        }


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    risk: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "risk": self.risk}


class CommandPolicy:
    def __init__(self, *, block_destructive: bool = True,
                 extra_patterns: Sequence[str] = ()) -> None:
        self.block_destructive = block_destructive
        self.extra = [(re.compile(p), "custom blocked pattern") for p in extra_patterns if p]

    def check(self, command: str) -> PolicyDecision:
        text = command or ""
        if not text.strip():
            return PolicyDecision(False, "empty command", "low")
        if self.block_destructive:
            for pattern, reason in _DESTRUCTIVE_PATTERNS:
                if pattern.search(text):
                    return PolicyDecision(False, f"blocked destructive command: {reason}", "critical")
        for pattern, reason in self.extra:
            if pattern.search(text):
                return PolicyDecision(False, f"blocked by policy: {reason}", "high")
        return PolicyDecision(True, "", "medium" if _looks_aggressive(text) else "low")


def _looks_aggressive(command: str) -> bool:
    markers = ("nmap", "masscan", "hydra", "sqlmap", "metasploit", "msfconsole", "nikto",
               "ffuf", "gobuster", "nuclei", "responder", "impacket", "crackmapexec", "netexec")
    return any(m in command.lower() for m in markers)


class Sandbox:
    backend = "base"

    def __init__(self, config: SandboxConfig, workdir: str | Path) -> None:
        self.config = config
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.policy = CommandPolicy(block_destructive=True,
                                    extra_patterns=config.extra_args if False else ())

    def ensure_ready(self) -> tuple[bool, str]:  # pragma: no cover - overridden
        return True, "ready"

    def run(self, command: str, *, timeout: float | None = None, cwd: str | None = None,
            env: Mapping[str, str] | None = None, stdin: str | None = None) -> SandboxResult:  # pragma: no cover
        raise NotImplementedError

    def _effective_timeout(self, timeout: float | None) -> float:
        return float(timeout or self.config.timeout)

    def _cap(self, text: str) -> str:
        return truncate(text or "", self.config.max_output)


class LocalSandbox(Sandbox):
    backend = "local"

    def run(self, command: str, *, timeout: float | None = None, cwd: str | None = None,
            env: Mapping[str, str] | None = None, stdin: str | None = None) -> SandboxResult:
        decision = self.policy.check(command)
        if not decision.allowed:
            return SandboxResult(exit_code=126, stderr=decision.reason, command=command,
                                 backend=self.backend, meta={"policy": decision.to_dict()})
        cwd_path = Path(cwd) if cwd else self.workdir
        try:
            cwd_path.resolve().relative_to(self.workdir.resolve())
        except (ValueError, OSError):
            return SandboxResult(exit_code=126, stderr="cwd escapes the engagement workdir",
                                 command=command, backend=self.backend)
        run_env = _base_env()
        run_env.update(self.config.env)
        run_env.update(dict(env or {}))
        started = time.monotonic()
        proc = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=str(cwd_path), env=run_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        timed_out = False
        try:
            out, err = proc.communicate(input=stdin, timeout=self._effective_timeout(timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            out, err = proc.communicate()
        duration = int((time.monotonic() - started) * 1000)
        return SandboxResult(exit_code=proc.returncode if proc.returncode is not None else -1,
                             stdout=self._cap(out or ""), stderr=self._cap(err or ""),
                             timed_out=timed_out, duration_ms=duration, command=command,
                             backend=self.backend)


class DockerSandbox(Sandbox):
    backend = "docker"

    def __init__(self, config: SandboxConfig, workdir: str | Path) -> None:
        super().__init__(config, workdir)
        self.docker = config.docker_binary or "docker"
        self.image = config.image

    def ensure_ready(self) -> tuple[bool, str]:
        if not shutil.which(self.docker):
            return False, f"docker binary not found: {self.docker}"
        try:
            proc = subprocess.run([self.docker, "info", "--format", "{{.ServerVersion}}"],
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"docker unavailable: {exc}"
        if proc.returncode != 0:
            return False, (proc.stderr or "docker daemon not running").strip()[:300]
        try:
            inspect = subprocess.run([self.docker, "image", "inspect", self.image],
                                     capture_output=True, text=True, timeout=10)
            if inspect.returncode != 0:
                return False, f"image not present: {self.image}"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"docker image check failed: {exc}"
        return True, f"docker {proc.stdout.strip()} / {self.image}"

    def run(self, command: str, *, timeout: float | None = None, cwd: str | None = None,
            env: Mapping[str, str] | None = None, stdin: str | None = None) -> SandboxResult:
        decision = self.policy.check(command)
        if not decision.allowed:
            return SandboxResult(exit_code=126, stderr=decision.reason, command=command,
                                 backend=self.backend, meta={"policy": decision.to_dict()})
        name = "aria-x-" + os.urandom(6).hex()
        docker_cmd = [self.docker, "run", "--rm", "-i", "--name", name,
                      "--network", self.config.network,
                      "-v", f"{self.workdir.resolve()}:/work", "-w", "/work"]
        for key, value in {**self.config.env, **dict(env or {})}.items():
            docker_cmd += ["-e", f"{key}={value}"]
        docker_cmd += list(self.config.extra_args or [])
        docker_cmd += [self.image, "bash", "-lc", command]
        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(docker_cmd, capture_output=True, text=True,
                                  input=stdin, timeout=self._effective_timeout(timeout))
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            subprocess.run([self.docker, "kill", name], capture_output=True, timeout=10)
            code = 124
            out = _as_text(exc.stdout)
            err = _as_text(exc.stderr) + f"\n[timeout after {self._effective_timeout(timeout)}s]"
        duration = int((time.monotonic() - started) * 1000)
        return SandboxResult(exit_code=code, stdout=self._cap(out), stderr=self._cap(err),
                             timed_out=timed_out, duration_ms=duration, command=command,
                             backend=self.backend, meta={"image": self.image})


class MockSandbox(Sandbox):
    """Scripted sandbox for deterministic tests and the offline demo."""

    backend = "mock"

    def __init__(self, config: SandboxConfig, workdir: str | Path,
                 outputs: Mapping[str, SandboxResult] | None = None) -> None:
        super().__init__(config, workdir)
        self.outputs = dict(outputs or {})
        self.calls: list[str] = []

    def run(self, command: str, *, timeout: float | None = None, cwd: str | None = None,
            env: Mapping[str, str] | None = None, stdin: str | None = None) -> SandboxResult:
        self.calls.append(command)
        for pattern, result in self.outputs.items():
            if pattern in command:
                return result
        return SandboxResult(exit_code=0, stdout="mock-ok", stderr="", command=command, backend="mock")


def build_sandbox(config: SandboxConfig, workdir: str | Path) -> Sandbox:
    backend = (config.backend or "local").lower()
    if backend == "docker":
        return DockerSandbox(config, workdir)
    if backend == "mock":
        return MockSandbox(config, workdir)
    return LocalSandbox(config, workdir)


def _base_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "TERM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["ARIA_X_SANDBOX"] = "1"
    return env


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def make_temp_workdir(prefix: str = "aria-x-work-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))

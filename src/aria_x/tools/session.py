"""Persistent interactive sessions (PTY).

``terminal_exec`` runs one command and returns. That is enough for scanners but
not for tools that expect a terminal: ``msfconsole``, ``ssh``, database shells,
or a reverse shell you just caught. This module adds a small session manager so
an agent can open a long-lived interactive process, send input, read output and
close it — the same primitive ARTEX exposes as ``shell_open/send/read/close``.

Design notes
------------
* **PTY, not pipes.** Interactive programs check ``isatty()`` and behave
  differently (no prompt, no colours, no line editing) when stdout is a pipe.
  A real PTY makes ``msfconsole`` and ``ssh`` usable.
* **Same guardrails as ``terminal_exec``.** The command is checked by
  ``CommandPolicy`` and the scope heuristic before spawn; every ``send`` is
  checked again, because a session is a way to run a second command later.
* **Bounded.** At most ``MAX_SESSIONS`` live sessions per run, capped output
  buffers, an idle sweep, and a hard close when the run ends. A leaked session
  is a leaked shell, so cleanup is part of the contract.
* **Local and Docker.** The local backend spawns the command directly; the
  Docker backend spawns ``docker run -i -t`` so the container gets a TTY too.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..util import new_id, now_iso, redact, truncate
from .base import ToolContext, ToolRegistry, ToolResult, ToolSpec
from .terminal import audit, clamp_int, emit, record_trace, resolve_in_workdir

try:  # POSIX only; the project targets macOS and Linux.
    import pty as _pty
except ImportError:  # pragma: no cover - Windows fallback
    _pty = None  # type: ignore[assignment]

#: Hard ceiling on concurrent sessions per engagement run.
MAX_SESSIONS = 4
#: Per-session output buffer; older bytes are dropped from the front.
MAX_BUFFER = 256 * 1024
#: Bytes returned to the model per read call.
MAX_READ = 8000
#: Sessions idle longer than this are swept on the next session operation.
MAX_IDLE_SECONDS = 1800.0
#: Grace period between SIGTERM and SIGKILL when closing.
KILL_GRACE = 2.0


@dataclass
class Session:
    """One live interactive process."""

    id: str
    name: str
    command: str
    proc: subprocess.Popen[bytes]
    fd: int
    backend: str
    container: str = ""
    created_at: str = field(default_factory=now_iso)
    last_used: float = field(default_factory=time.monotonic)
    buffer: bytearray = field(default_factory=bytearray)
    exit_code: int | None = None
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- io ------------------------------------------------------------

    def drain(self) -> bytes:
        with self.lock:
            data = bytes(self.buffer)
            self.buffer.clear()
            return data

    def append(self, chunk: bytes) -> None:
        with self.lock:
            self.buffer.extend(chunk)
            overflow = len(self.buffer) - MAX_BUFFER
            if overflow > 0:
                del self.buffer[:overflow]

    def pump(self, wait: float) -> bytes:
        """Read whatever arrives within *wait* seconds, return new bytes."""
        if self.closed:
            return b""
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            remaining = deadline - time.monotonic()
            try:
                ready, _, _ = select.select([self.fd], [], [], max(0.0, min(remaining, 0.25)))
            except (OSError, ValueError):
                self.closed = True
                break
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        self.closed = True
                        break
                    raise
                if not chunk:
                    self.closed = True
                    break
                self.append(chunk)
            if time.monotonic() >= deadline:
                break
        return self.drain()

    def write(self, text: str) -> None:
        with self.lock:
            os.write(self.fd, text.encode("utf-8", "replace"))

    @property
    def alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "name": self.name,
            "command": truncate(redact(self.command), 300),
            "backend": self.backend,
            "created_at": self.created_at,
            "idle_seconds": round(time.monotonic() - self.last_used, 1),
            "alive": self.alive,
            "exit_code": self.exit_code if self.exit_code is not None else self.proc.poll(),
            "buffered_bytes": len(self.buffer),
        }


class SessionManager:
    """Owns every live session for one run and guarantees cleanup."""

    def __init__(self, *, max_sessions: int = MAX_SESSIONS,
                 max_idle: float = MAX_IDLE_SECONDS,
                 banner_wait: float = 1.2) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self.max_sessions = max(1, int(max_sessions))
        self.max_idle = float(max_idle)
        #: How long to wait for a startup banner (msfconsole needs ~1s).
        self.banner_wait = max(0.0, float(banner_wait))

    # -- lifecycle -----------------------------------------------------

    def open(self, ctx: ToolContext, command: str, *, name: str = "",
             cwd: str | None = None) -> Session:
        with self._lock:
            self.sweep()
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(
                    f"session limit reached ({self.max_sessions}); close one with session_close")

        backend = getattr(ctx.sandbox, "backend", "local")
        container = ""
        if backend == "docker":
            container = "aria-x-sess-" + os.urandom(4).hex()
            argv = self._docker_argv(ctx, container, command)
        else:
            argv = ["/bin/bash", "-lc", command]

        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        env["ARIA_X_SESSION"] = "1"

        if _pty is not None:
            master, slave = _pty.openpty()
            try:
                proc = subprocess.Popen(
                    argv, stdin=slave, stdout=slave, stderr=slave,
                    cwd=str(ctx.workdir), env=env, start_new_session=True, close_fds=True,
                )
            finally:
                os.close(slave)
            fd = master
        else:  # pragma: no cover - Windows fallback: pipes, no TTY semantics
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(ctx.workdir), env=env, start_new_session=True,
            )
            assert proc.stdout is not None
            fd = proc.stdout.fileno()

        session = Session(id=new_id("s", 10), name=name or command.split()[0][:40],
                          command=command, proc=proc, fd=fd, backend=backend,
                          container=container)
        with self._lock:
            self._sessions[session.id] = session
        # Give the process a moment to print its banner/prompt.
        session.pump(self.banner_wait)
        return session

    def _docker_argv(self, ctx: ToolContext, container: str, command: str) -> list[str]:
        cfg = ctx.config.sandbox
        docker = cfg.docker_binary or "docker"
        argv = [docker, "run", "--rm", "-i", "-t", "--name", container,
                "--network", cfg.network,
                "-v", f"{Path(ctx.workdir).resolve()}:/work", "-w", "/work"]
        for key, value in (cfg.env or {}).items():
            argv += ["-e", f"{key}={value}"]
        argv += list(cfg.extra_args or [])
        argv += [cfg.image, "bash", "-lc", command]
        return argv

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            session.last_used = time.monotonic()
        return session

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.info() for s in sessions]

    def close(self, session: Session) -> dict[str, Any]:
        if session.closed and session.proc.poll() is not None:
            self._forget(session)
            return session.info()
        # Interactive shells often ignore SIGTERM, and a tool like msfconsole may
        # need its own quit command. Ask politely first, then escalate.
        if session.alive:
            try:
                session.write("exit\n")
                session.pump(0.3)
            except OSError:
                pass
        if session.proc.poll() is None:
            try:
                os.killpg(os.getpgid(session.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    session.proc.terminate()
                except Exception:
                    pass
        deadline = time.monotonic() + KILL_GRACE
        while time.monotonic() < deadline and session.proc.poll() is None:
            session.pump(0.1)
        if session.proc.poll() is None:
            try:
                os.killpg(os.getpgid(session.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    session.proc.kill()
                except Exception:
                    pass
        session.pump(0.2)
        session.exit_code = session.proc.poll()
        session.closed = True
        try:
            os.close(session.fd)
        except OSError:
            pass
        if session.container:
            try:
                subprocess.run([getattr(session, "_docker", "docker"), "kill", session.container],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        info = session.info()
        self._forget(session)
        return info

    def _forget(self, session: Session) -> None:
        with self._lock:
            self._sessions.pop(session.id, None)

    def sweep(self) -> list[str]:
        """Close sessions that have been idle past ``max_idle``."""
        now = time.monotonic()
        with self._lock:
            stale = [s for s in self._sessions.values() if now - s.last_used > self.max_idle]
        closed: list[str] = []
        for session in stale:
            try:
                self.close(session)
                closed.append(session.id)
            except Exception:
                continue
        return closed

    def close_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
        count = 0
        for session in sessions:
            try:
                self.close(session)
                count += 1
            except Exception:
                continue
        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def _require_manager(ctx: ToolContext) -> SessionManager | None:
    manager = getattr(ctx, "sessions", None)
    return manager if isinstance(manager, SessionManager) else None


def _policy_check(ctx: ToolContext, text: str, action: str) -> ToolResult | None:
    """Apply the same command policy + scope heuristic used by terminal_exec."""
    policy = getattr(ctx.sandbox, "policy", None)
    if policy is not None:
        decision = policy.check(text)
        if not decision.allowed:
            audit(ctx, action, "denied", text, {"reason": decision.reason, "risk": decision.risk})
            emit(ctx, "error", {"tool": action, "reason": decision.reason})
            return ToolResult.failure(f"blocked by policy: {decision.reason}",
                                      data={"policy": decision.to_dict()})
    scope_decision = ctx.scope.check_command(text)
    if not scope_decision.allowed:
        audit(ctx, action, "denied", text, {"reason": scope_decision.reason})
        return ToolResult.failure(f"scope violation: {scope_decision.reason}",
                                  data={"target": scope_decision.target})
    return None


def session_open(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    command = str(kwargs.get("command") or "").strip()
    if not command:
        return ToolResult.failure("command must not be empty")
    ctx.check_cancel()
    manager = _require_manager(ctx)
    if manager is None:
        return ToolResult.failure("session manager unavailable in this context")

    blocked = _policy_check(ctx, command, "session_open")
    if blocked is not None:
        return blocked

    cwd: str | None = None
    if kwargs.get("cwd"):
        try:
            path = resolve_in_workdir(ctx, str(kwargs["cwd"]), must_exist=True)
        except Exception as exc:
            return ToolResult.failure(f"invalid cwd: {exc}")
        if not path.is_dir():
            return ToolResult.failure("cwd is not a directory")
        cwd = str(path)

    try:
        session = manager.open(ctx, command, name=str(kwargs.get("name") or ""), cwd=cwd)
    except RuntimeError as exc:
        return ToolResult.failure(str(exc))
    except Exception as exc:
        audit(ctx, "session_open", "error", command, {"error": str(exc)})
        return ToolResult.failure(f"failed to start session: {type(exc).__name__}: {exc}")

    banner = session.pump(0.3).decode("utf-8", "replace")
    audit(ctx, "session_open", "allowed", command,
          {"session_id": session.id, "backend": session.backend})
    record_trace(ctx, "session", f"session_open {session.id}: {command}",
                 tool_name="session_open",
                 tool_args={"command": truncate(redact(command), 500)},
                 tool_result={"session_id": session.id, "backend": session.backend})
    payload = {
        "session_id": session.id,
        "name": session.name,
        "backend": session.backend,
        "alive": session.alive,
        "initial_output": truncate(banner, MAX_READ),
        "hint": "Use session_send to type input and session_read to fetch later output.",
    }
    return ToolResult.success(payload, text=truncate(
        f"session {session.id} started ({session.backend})\n--- initial output ---\n{banner}",
        MAX_READ))


def session_send(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    session_id = str(kwargs.get("session_id") or "").strip()
    text = str(kwargs.get("text") or "")
    if not session_id:
        return ToolResult.failure("session_id is required")
    ctx.check_cancel()
    manager = _require_manager(ctx)
    if manager is None:
        return ToolResult.failure("session manager unavailable in this context")
    session = manager.get(session_id)
    if session is None:
        return ToolResult.failure(f"unknown session: {session_id}", data={"sessions": manager.list()})
    if not session.alive:
        return ToolResult.failure("session is no longer running",
                                  data={"session_id": session_id, "exit_code": session.exit_code})

    # An empty payload is just "press Enter" (submitting a prompt or a REPL
    # line). There is no command to police, and the policy rejects empty input.
    if text.strip():
        blocked = _policy_check(ctx, text, "session_send")
        if blocked is not None:
            return blocked

    newline = kwargs.get("newline")
    payload_text = text if newline is False else text + "\n"
    wait_ms = clamp_int(kwargs.get("wait_ms"), 0, 30_000, 800)
    try:
        session.write(payload_text)
    except OSError as exc:
        return ToolResult.failure(f"write failed: {exc}", data={"session_id": session_id})

    output = session.pump(wait_ms / 1000.0).decode("utf-8", "replace")
    audit(ctx, "session_send", "allowed", session_id,
          {"bytes": len(payload_text), "session_id": session_id})
    record_trace(ctx, "session", f"session_send {session_id}: {truncate(redact(text), 200)}",
                 tool_name="session_send",
                 tool_args={"session_id": session_id, "text": truncate(redact(text), 500)},
                 tool_result={"output_bytes": len(output)})
    payload = {
        "session_id": session_id,
        "sent": truncate(redact(payload_text), 500),
        "output": truncate(output, MAX_READ),
        "alive": session.alive,
        "exit_code": session.exit_code if session.exit_code is not None else session.proc.poll(),
    }
    return ToolResult.success(payload, text=truncate(output or "(no output)", MAX_READ))


def session_read(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    session_id = str(kwargs.get("session_id") or "").strip()
    if not session_id:
        return ToolResult.failure("session_id is required")
    manager = _require_manager(ctx)
    if manager is None:
        return ToolResult.failure("session manager unavailable in this context")
    session = manager.get(session_id)
    if session is None:
        return ToolResult.failure(f"unknown session: {session_id}", data={"sessions": manager.list()})
    wait_ms = clamp_int(kwargs.get("wait_ms"), 0, 30_000, 500)
    output = session.pump(wait_ms / 1000.0).decode("utf-8", "replace")
    payload = {
        "session_id": session_id,
        "output": truncate(output, MAX_READ),
        "alive": session.alive,
        "exit_code": session.exit_code if session.exit_code is not None else session.proc.poll(),
    }
    return ToolResult.success(payload, text=truncate(output or "(no new output)", MAX_READ))


def session_close(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    session_id = str(kwargs.get("session_id") or "").strip()
    if not session_id:
        return ToolResult.failure("session_id is required")
    manager = _require_manager(ctx)
    if manager is None:
        return ToolResult.failure("session manager unavailable in this context")
    session = manager.get(session_id)
    if session is None:
        return ToolResult.failure(f"unknown session: {session_id}")
    final = session.pump(0.2).decode("utf-8", "replace")
    info = manager.close(session)
    info["final_output"] = truncate(final, MAX_READ)
    audit(ctx, "session_close", "allowed", session_id, {"exit_code": info.get("exit_code")})
    record_trace(ctx, "session", f"session_close {session_id} exit={info.get('exit_code')}",
                 tool_name="session_close", tool_args={"session_id": session_id},
                 tool_result={"exit_code": info.get("exit_code")})
    return ToolResult.success(info, text=truncate(
        f"session {session_id} closed (exit {info.get('exit_code')})\n"
        f"--- final output ---\n{final or '(none)'}", MAX_READ))


def session_list(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    manager = _require_manager(ctx)
    if manager is None:
        return ToolResult.failure("session manager unavailable in this context")
    sessions = manager.list()
    return ToolResult.success({"sessions": sessions, "count": len(sessions)},
                              text=f"{len(sessions)} active session(s)")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    registry.add(ToolSpec(
        name="session_open",
        description=(
            "Start a persistent interactive session (PTY) for programs that need a terminal: "
            "msfconsole, ssh, mysql/psql/python REPLs, nc, or a reverse shell you just caught. "
            "Returns a session_id plus the initial banner. Use session_send/session_read to "
            "interact and session_close when done. Prefer terminal_exec for one-shot commands."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "Command to run interactively, e.g. 'msfconsole -q'."},
                "name": {"type": "string", "description": "Short label for this session."},
                "cwd": {"type": "string", "description": "Subdirectory of the engagement workdir."},
            },
            "required": ["command"],
        },
        risk="high", tags=("terminal", "session", "interactive"),
    ), session_open)

    registry.add(ToolSpec(
        name="session_send",
        description=(
            "Type input into an open session and return the output produced within wait_ms. "
            "Omit text (or pass an empty string) to just press Enter. A newline is appended by "
            "default; pass newline=false for raw keystrokes or prompts that must not be submitted yet."
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string",
                         "description": "Input to type. Omit or leave empty to press Enter."},
                "newline": {"type": "boolean", "description": "Append a newline (default true)."},
                "wait_ms": {"type": "integer",
                            "description": "How long to wait for output, 0-30000 (default 800)."},
            },
            "required": ["session_id"],
        },
        risk="high", tags=("terminal", "session", "interactive"),
    ), session_send)

    registry.add(ToolSpec(
        name="session_read",
        description=(
            "Read new output from an open session without sending input. Use it after a long-running "
            "command, or to poll a shell that prints asynchronously."
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "wait_ms": {"type": "integer",
                            "description": "How long to wait for output, 0-30000 (default 500)."},
            },
            "required": ["session_id"],
        },
        risk="medium", tags=("terminal", "session"), read_only=True,
    ), session_read)

    registry.add(ToolSpec(
        name="session_close",
        description=(
            "Close a session, terminate its process group and return the exit code plus any final "
            "output. Always close sessions you no longer need."
        ),
        parameters={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
        risk="low", tags=("terminal", "session"),
    ), session_close)

    registry.add(ToolSpec(
        name="session_list",
        description="List the active interactive sessions for this run with age, idle time and state.",
        parameters={"type": "object", "properties": {}},
        risk="low", tags=("terminal", "session"), read_only=True,
    ), session_list)

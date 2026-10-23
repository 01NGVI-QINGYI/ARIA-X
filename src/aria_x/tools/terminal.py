"""Terminal execution and workdir-jailed file tools.

The sandbox is the isolation boundary and :class:`CommandPolicy` is the
destructive-command guard; this module adds two things on top:

* a **scope heuristic** — literal IPs and URLs inside a shell command are checked
  against the engagement scope before a single process starts, so an accidental
  ``nmap 8.8.8.8`` is refused rather than merely logged;
* **workdir jailing** for every file operation, so a model cannot read the host
  filesystem or overwrite the engagement's own configuration.

Commands are passed to ``sandbox.run`` as one string; the local backend executes
it through ``bash -lc`` (the intended interface). Nothing is shell-interpolated
by ARIA-X itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import SandboxError, ScopeViolation, ToolError
from ..models import Trace
from ..util import human_bytes, redact, truncate
from .base import ToolContext, ToolRegistry, ToolResult, ToolSpec

#: Absolute ceiling for a single file read/write; keeps observations bounded.
MAX_FILE_BYTES = 1_000_000
#: Maximum directory entries returned by ``file_list``.
MAX_DIR_ENTRIES = 300


# ---------------------------------------------------------------------------
# shared plumbing (also used by net.py / knowledge.py)
# ---------------------------------------------------------------------------


def audit(ctx: ToolContext, action: str, decision: str, target: str = "",
          detail: dict[str, Any] | None = None) -> None:
    """Record a tool decision. Auditing must never break the tool itself."""
    try:
        ctx.store.audit(ctx.project.id, f"tool:{ctx.agent or 'agent'}", action, decision,
                        target=truncate(redact(target), 300), detail=detail or {})
    except Exception:
        pass


def record_trace(ctx: ToolContext, kind: str, content: str, *, tool_name: str = "",
                 tool_args: dict[str, Any] | None = None,
                 tool_result: dict[str, Any] | None = None) -> None:
    """Append a process trace (ARTEX cross-worker lineage). Best-effort."""
    try:
        trace = Trace.new(ctx.project.id, ctx.run_id, ctx.task_id, ctx.agent or "tool", 0,
                          kind, truncate(redact(content), 4000), tool_name=tool_name,
                          tool_args=tool_args or {}, tool_result=tool_result or {})
        ctx.store.add_trace(trace)
    except Exception:
        pass


def emit(ctx: ToolContext, type_: str, data: dict[str, Any]) -> None:
    try:
        ctx.emit(type_, data)
    except Exception:
        pass


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def resolve_in_workdir(ctx: ToolContext, raw: str = ".", *, must_exist: bool = False) -> Path:
    """Resolve *raw* under the engagement workdir, rejecting any escape.

    ``Path.resolve`` also follows symlinks, so a symlink planted inside the
    workdir cannot be used to step outside it.
    """
    base = Path(ctx.workdir).resolve()
    text = (raw or ".").strip() or "."
    candidate = Path(text).expanduser()
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ToolError("path escapes the engagement workdir", tool="file", path=text) from None
    if must_exist and not resolved.exists():
        raise ToolError("path does not exist", tool="file", path=text)
    return resolved


def _rel(ctx: ToolContext, path: Path) -> str:
    try:
        return str(path.relative_to(Path(ctx.workdir).resolve())) or "."
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# terminal_exec
# ---------------------------------------------------------------------------


def terminal_exec(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    command = str(kwargs.get("command") or "").strip()
    if not command:
        return ToolResult.failure("command must not be empty")
    ctx.check_cancel()

    policy = getattr(ctx.sandbox, "policy", None)
    decision = policy.check(command) if policy is not None else None
    if decision is not None and not decision.allowed:
        audit(ctx, "terminal", "denied", command, {"reason": decision.reason, "risk": decision.risk})
        emit(ctx, "error", {"tool": "terminal_exec", "reason": decision.reason})
        return ToolResult.failure(f"command blocked by policy: {decision.reason}",
                                  data={"command": redact(command), "policy": decision.to_dict()})

    # Heuristic scope gate before any process starts. A literal IP/URL that is
    # not authorized raises ScopeViolation, which the registry audits uniformly.
    scope_decision = ctx.scope.check_command(command)
    if not scope_decision.allowed:
        raise ScopeViolation(scope_decision.reason, target=scope_decision.target or command,
                             host=scope_decision.host, port=scope_decision.port)

    timeout_arg = kwargs.get("timeout")
    max_seconds = float(getattr(ctx.config.safety, "max_command_seconds", 120) or 120)
    timeout = clamp_float(timeout_arg, 1.0, max_seconds, default=min(30.0, max_seconds))

    cwd = kwargs.get("cwd")
    cwd_str: str | None = None
    if cwd:
        cwd_path = resolve_in_workdir(ctx, str(cwd), must_exist=True)
        if not cwd_path.is_dir():
            return ToolResult.failure("cwd is not a directory", data={"cwd": _rel(ctx, cwd_path)})
        cwd_str = str(cwd_path)

    audit(ctx, "terminal", "allowed", command,
          {"timeout": timeout, "cwd": cwd_str or ".", "risk": decision.risk if decision else "unknown"})
    try:
        result = ctx.sandbox.run(command, timeout=timeout, cwd=cwd_str)
    except SandboxError as exc:
        audit(ctx, "terminal", "error", command, {"error": exc.message})
        return ToolResult.failure(f"sandbox error: {exc.message}", data={"command": redact(command)})
    except Exception as exc:  # never let a backend bug kill the engagement
        audit(ctx, "terminal", "error", command, {"error": str(exc)})
        return ToolResult.failure(f"sandbox failure: {type(exc).__name__}: {exc}")

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    payload = {
        "exit_code": int(result.exit_code),
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": bool(result.timed_out),
        "duration_ms": int(result.duration_ms),
        "backend": result.backend,
        "command": redact(command),
    }
    record_trace(ctx, "terminal", f"$ {command}\n(exit {result.exit_code}, {result.duration_ms}ms)",
                 tool_name="terminal_exec",
                 tool_args={"command": truncate(redact(command), 800)},
                 tool_result={"exit_code": int(result.exit_code), "timed_out": bool(result.timed_out),
                              "duration_ms": int(result.duration_ms)})
    if result.timed_out:
        emit(ctx, "error", {"tool": "terminal_exec", "reason": "timeout", "timeout": timeout})
    text = (f"exit={result.exit_code} backend={result.backend} duration={result.duration_ms}ms\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
    return ToolResult.success(payload, text=truncate(text, 10000))


# ---------------------------------------------------------------------------
# file tools
# ---------------------------------------------------------------------------


def file_read(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    path = resolve_in_workdir(ctx, str(kwargs.get("path") or ""), must_exist=True)
    if not path.is_file():
        return ToolResult.failure("not a regular file", data={"path": _rel(ctx, path)})
    max_bytes = clamp_int(kwargs.get("max_bytes"), 1, MAX_FILE_BYTES, 65536)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ToolResult.failure(f"read failed: {exc}", data={"path": _rel(ctx, path)})
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", "replace")
    audit(ctx, "file_read", "allowed", _rel(ctx, path), {"bytes": len(raw), "truncated": truncated})
    return ToolResult.success(
        {"path": _rel(ctx, path), "size": len(raw), "size_human": human_bytes(len(raw)),
         "truncated": truncated, "content": text},
        text=truncate(text, 12000),
    )


def file_write(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    path = resolve_in_workdir(ctx, str(kwargs.get("path") or ""))
    content = kwargs.get("content")
    content = "" if content is None else str(content)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        return ToolResult.failure(f"content too large ({len(encoded)} bytes > {MAX_FILE_BYTES})")
    if path.exists() and path.is_dir():
        return ToolResult.failure("path is a directory", data={"path": _rel(ctx, path)})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        resolve_in_workdir(ctx, _rel(ctx, path))  # re-check after mkdir (symlink race)
        path.write_bytes(encoded)
    except OSError as exc:
        return ToolResult.failure(f"write failed: {exc}", data={"path": _rel(ctx, path)})
    audit(ctx, "file_write", "allowed", _rel(ctx, path), {"bytes": len(encoded)})
    return ToolResult.success({"path": _rel(ctx, path), "bytes": len(encoded),
                               "size_human": human_bytes(len(encoded)), "written": True},
                              text=f"wrote {len(encoded)} bytes to {_rel(ctx, path)}")


def file_list(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    path = resolve_in_workdir(ctx, str(kwargs.get("path") or "."), must_exist=True)
    if not path.is_dir():
        return ToolResult.failure("not a directory", data={"path": _rel(ctx, path)})
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:MAX_DIR_ENTRIES]
        for child in children:
            try:
                size = child.stat().st_size if child.is_file() else 0
            except OSError:
                size = 0
            entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file",
                            "size": size, "size_human": human_bytes(size)})
    except OSError as exc:
        return ToolResult.failure(f"list failed: {exc}", data={"path": _rel(ctx, path)})
    return ToolResult.success({"path": _rel(ctx, path), "entries": entries, "count": len(entries)},
                              text="\n".join(f"{e['type']:4} {e['size_human']:>8}  {e['name']}"
                                             for e in entries))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    registry.add(ToolSpec(
        name="terminal_exec",
        description=(
            "Run one shell command inside the engagement sandbox and return exit code, stdout, "
            "stderr and duration. Commands are policy-checked (destructive operations are refused) "
            "and literal out-of-scope targets are rejected before execution. The working directory "
            "is the engagement workdir; use it for reconnaissance tooling available in the sandbox."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute via bash -lc."},
                "timeout": {"type": "number", "description": "Seconds before the command is killed."},
                "cwd": {"type": "string", "description": "Subdirectory of the engagement workdir."},
            },
            "required": ["command"],
        },
        risk="high", tags=("terminal", "execution"),
    ), terminal_exec)

    registry.add(ToolSpec(
        name="file_read",
        description=(
            "Read a UTF-8 text file from inside the engagement workdir (jailed: parent traversal and "
            "symlink escapes are refused). Output is bounded by max_bytes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the engagement workdir."},
                "max_bytes": {"type": "integer", "description": "Maximum bytes to return (default 65536)."},
            },
            "required": ["path"],
        },
        risk="medium", tags=("filesystem",), read_only=True,
    ), file_read)

    registry.add(ToolSpec(
        name="file_write",
        description=(
            "Write a text file inside the engagement workdir, creating parent directories. Use it to "
            "store payloads, notes and tool output that later steps need. Content is bounded to 1 MB."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the engagement workdir."},
                "content": {"type": "string", "description": "Full UTF-8 file content."},
            },
            "required": ["path", "content"],
        },
        risk="high", tags=("filesystem",),
    ), file_write)

    registry.add(ToolSpec(
        name="file_list",
        description="List a directory inside the engagement workdir (name, type, size).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory relative to the workdir (default '.')."},
            },
        },
        risk="low", tags=("filesystem",), read_only=True,
    ), file_list)

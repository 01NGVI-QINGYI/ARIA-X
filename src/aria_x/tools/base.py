"""Tool abstraction shared by every agent.

Tools are the only way an agent can change the world. Each tool declares a JSON
schema, a risk level, and whether it needs scope authorization. The registry
validates arguments, catches failures, and returns a structured
:class:`ToolResult` so a model can observe what happened and self-correct.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TYPE_CHECKING

from ..errors import ApprovalDenied, AriaXError, ScopeViolation, ToolError
from ..util import json_dumps, truncate

if TYPE_CHECKING:  # pragma: no cover
    from ..config import AriaXConfig
    from ..events import EventBus
    from ..models import Project
    from ..store import Store
    from .sandbox import Sandbox
    from .scope import Scope


RISK_LEVELS = ("low", "medium", "high", "critical")


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    risk: str = "low"
    tags: tuple[str, ...] = ()
    requires_scope: bool = False
    read_only: bool = False
    max_output: int = 12000

    def to_llm(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description, "parameters": self.parameters,
            "risk": self.risk, "tags": list(self.tags), "requires_scope": self.requires_scope,
            "read_only": self.read_only,
        }


@dataclass(slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    text: str = ""
    error: str = ""
    artifacts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    @classmethod
    def success(cls, data: Any = None, text: str = "", artifacts: Sequence[str] | None = None,
                **meta: Any) -> "ToolResult":
        rendered = text or _render(data)
        return cls(ok=True, data=data, text=rendered, artifacts=list(artifacts or []), meta=meta)

    @classmethod
    def failure(cls, error: str, *, data: Any = None, **meta: Any) -> "ToolResult":
        return cls(ok=False, error=error, data=data, text="", meta=meta)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "text": truncate(self.text, 12000),
            "error": self.error,
            "artifacts": self.artifacts,
            "meta": self.meta,
            "duration_ms": self.duration_ms,
        }

    def observation(self, limit: int = 8000) -> str:
        """Compact text fed back to the model."""
        if not self.ok:
            return f"ERROR: {self.error}"[:limit]
        payload = {"ok": True}
        if self.data is not None:
            payload["data"] = self.data
        if self.text:
            payload["text"] = truncate(self.text, limit - len(json_dumps(payload)) - 100)
        return truncate(json_dumps(payload, ensure_ascii=False), limit)


def _render(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return json_dumps(data, ensure_ascii=False)


class Tool:
    """Base class for tools. Subclasses set ``spec`` or use :func:`function_tool`."""

    spec: ToolSpec

    def run(self, ctx: "ToolContext", **kwargs: Any) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.spec.name


class FunctionTool(Tool):
    def __init__(self, spec: ToolSpec, fn: Callable[["ToolContext", dict[str, Any]], ToolResult]) -> None:
        self.spec = spec
        self._fn = fn

    def run(self, ctx: "ToolContext", **kwargs: Any) -> ToolResult:
        return self._fn(ctx, kwargs)


@dataclass
class ToolContext:
    """Everything a tool may need. Kept explicit to avoid globals."""

    project: "Project"
    run_id: str
    task_id: str
    agent: str
    role: str
    store: "Store"
    config: "AriaXConfig"
    events: "EventBus"
    scope: "Scope"
    sandbox: "Sandbox"
    workdir: Path
    budget: Any = None
    approvals: Any = None
    delegate: Callable[[str, str], str] | None = None
    cancel: Any = None
    request_approval: Callable[[str, dict[str, Any], str, str], bool] | None = None

    def child(self, **overrides: Any) -> "ToolContext":
        data = {
            "project": self.project, "run_id": self.run_id, "task_id": self.task_id,
            "agent": self.agent, "role": self.role, "store": self.store, "config": self.config,
            "events": self.events, "scope": self.scope, "sandbox": self.sandbox,
            "workdir": self.workdir, "budget": self.budget, "approvals": self.approvals,
            "delegate": self.delegate, "cancel": self.cancel,
            "request_approval": self.request_approval,
        }
        data.update(overrides)
        return ToolContext(**data)

    def emit(self, type_: str, data: Mapping[str, Any] | None = None, **extra: Any) -> None:
        self.events.emit(type_, project_id=self.project.id, run_id=self.run_id,
                         task_id=self.task_id, data=dict(data or {}), **extra)

    def check_cancel(self) -> None:
        if self.cancel is not None and getattr(self.cancel, "is_set", lambda: False)():
            from ..errors import Cancelled
            raise Cancelled("task cancelled by operator")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration ----------------------------------------------------

    def register(self, tool: Tool) -> Tool:
        if not isinstance(tool.spec, ToolSpec):
            raise ToolError("tool is missing a ToolSpec", tool=getattr(tool, "name", "?"))
        if tool.spec.risk not in RISK_LEVELS:
            raise ToolError(f"invalid risk '{tool.spec.risk}'", tool=tool.spec.name)
        if tool.spec.name in self._tools:
            raise ToolError(f"duplicate tool '{tool.spec.name}'", tool=tool.spec.name)
        self._tools[tool.spec.name] = tool
        return tool

    def add(self, spec: ToolSpec, fn: Callable[["ToolContext", dict[str, Any]], ToolResult]) -> Tool:
        return self.register(FunctionTool(spec, fn))

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # -- access ----------------------------------------------------------

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def specs(self, allowed: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if allowed is None:
            return [t.spec.to_llm() for t in self.tools()]
        allow = set(allowed)
        return [t.spec.to_llm() for t in self.tools() if t.spec.name in allow]

    def catalog(self) -> list[dict[str, Any]]:
        return [t.spec.to_dict() for t in self.tools()]

    def subset(self, allowed: Iterable[str]) -> "ToolRegistry":
        out = ToolRegistry()
        for name in allowed:
            tool = self._tools.get(name)
            if tool is not None:
                out.register(tool)
        return out

    # -- execution -------------------------------------------------------

    def execute(self, name: str, args: Mapping[str, Any] | None, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(f"unknown tool '{name}'", available=self.names()[:40])
        arguments = dict(args or {})
        started = time.monotonic()
        try:
            ctx.check_cancel()
            _validate_arguments(tool.spec, arguments)
            _enforce_approval(tool, arguments, ctx)
            result = tool.run(ctx, **arguments)
            if not isinstance(result, ToolResult):
                result = ToolResult.success(result)
        except ScopeViolation as exc:
            result = ToolResult.failure(f"scope violation: {exc.message}", data=exc.details)
            ctx.store.audit(ctx.project.id, "scope", name, "denied",
                            target=str(exc.details.get("target", "")), detail=exc.details)
        except ApprovalDenied as exc:
            result = ToolResult.failure(f"approval denied: {exc.message}", data=exc.details)
        except AriaXError as exc:
            result = ToolResult.failure(f"{exc.code}: {exc.message}", data=exc.details)
        except Exception as exc:  # tool bugs must not kill the engagement
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result


def _enforce_approval(tool: Tool, args: Mapping[str, Any], ctx: "ToolContext") -> None:
    """Apply the configured human-approval policy for risky tools.

    ``auto`` (default) runs everything inside scope; ``ask`` blocks high/critical
    tools until a human approves them in the console or API; ``deny`` refuses
    them outright. Scope is enforced independently, so approval never widens the
    target boundary.
    """
    mode = str(getattr(ctx.config.safety, "approval", "auto") or "auto").lower()
    if mode == "auto" or tool.spec.risk not in {"high", "critical"}:
        return
    if mode == "deny":
        ctx.store.audit(ctx.project.id, "policy", tool.spec.name, "denied",
                        target=str(args.get("url") or args.get("host") or args.get("command") or ""),
                        detail={"risk": tool.spec.risk, "reason": "approval mode is deny"})
        raise ApprovalDenied(f"tool '{tool.spec.name}' risk={tool.spec.risk} refused by policy",
                             tool=tool.spec.name, risk=tool.spec.risk)
    request = ctx.request_approval
    if not callable(request):
        raise ApprovalDenied(f"tool '{tool.spec.name}' needs approval but no approver is attached",
                             tool=tool.spec.name)
    approved = bool(request(tool.spec.name, dict(args), tool.spec.risk,
                            f"{tool.spec.risk}-risk tool requested by {ctx.agent}"))
    if not approved:
        ctx.store.audit(ctx.project.id, "human", tool.spec.name, "denied",
                        target=str(args.get("url") or args.get("host") or args.get("command") or ""),
                        detail={"risk": tool.spec.risk, "reason": "approval not granted"})
        raise ApprovalDenied(f"human did not approve '{tool.spec.name}'", tool=tool.spec.name)
    ctx.store.audit(ctx.project.id, "human", tool.spec.name, "approved",
                    target=str(args.get("url") or args.get("host") or args.get("command") or ""),
                    detail={"risk": tool.spec.risk})


def _validate_arguments(spec: ToolSpec, args: dict[str, Any]) -> None:
    schema = spec.parameters or {}
    if schema.get("type") != "object":
        return
    required = schema.get("required") or []
    for key in required:
        if key not in args or args.get(key) in (None, ""):
            raise ToolError(f"missing required argument '{key}'", tool=spec.name)
    props = schema.get("properties") or {}
    for key, value in list(args.items()):
        if key not in props:
            # Be permissive: models sometimes add harmless extra fields.
            continue
        expected = props[key].get("type") if isinstance(props[key], dict) else None
        if expected == "string" and not isinstance(value, str):
            args[key] = str(value)
        elif expected == "integer" and isinstance(value, str):
            try:
                args[key] = int(value)
            except ValueError:
                raise ToolError(f"argument '{key}' must be an integer", tool=spec.name)
        elif expected == "number" and isinstance(value, str):
            try:
                args[key] = float(value)
            except ValueError:
                raise ToolError(f"argument '{key}' must be a number", tool=spec.name)
        elif expected == "boolean" and isinstance(value, str):
            args[key] = value.strip().lower() in {"1", "true", "yes", "on"}

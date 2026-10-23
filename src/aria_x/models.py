"""Domain models for ARIA-X.

The model layers deliberately mirror the three fused lineages:

* :class:`Node` carries Cairn's **Fact / Intent / Hint** primitives *and* ARTEX's
  **goal / finding** nodes. One table, distinguished by ``kind``.
* :class:`Asset` is ARTEX's shared asset-graph truth store; nodes attach to it
  through :class:`Anchor`.
* :class:`Todo` is ARTEX's cross-wakeup plan chain.
* :class:`Trace` is ARTEX's process-level record plus PentAGI's execution log.
* :class:`Task` / :class:`Run` are the scheduler-facing execution records.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
import json

from .util import json_loads, new_id, now_iso

# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------

NODE_KINDS = ("goal", "intent", "fact", "finding", "hint", "note", "guide")
NODE_STATUSES = ("open", "active", "done", "proven", "failed", "rejected", "superseded")
EDGE_RELS = ("spawns", "yields", "derived_from", "proves", "supports", "depends_on", "related", "contradicts")
ASSET_KINDS = ("root_domain", "subdomain", "host", "ip", "service", "endpoint", "app", "credential", "artifact")
SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
TASK_TYPES = ("bootstrap", "reason", "explore", "specialist", "report", "consolidate")
TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled", "skipped")
RUN_STATUSES = ("created", "running", "paused", "completed", "failed", "cancelled", "budget")
TODO_STATUSES = ("pending", "active", "done", "blocked", "dropped")


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _loads(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return json_loads(value, default if default is not None else {})


def _row(row: Any) -> Mapping[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return row
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Project:
    id: str
    name: str
    goal: str
    scope: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def new(cls, name: str, goal: str, *, scope: dict[str, Any] | None = None,
            config: dict[str, Any] | None = None, project_id: str | None = None) -> "Project":
        ts = now_iso()
        return cls(id=project_id or new_id("p"), name=name, goal=goal,
                   scope=scope or {}, config=config or {}, created_at=ts, updated_at=ts)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Project | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], name=r.get("name", ""), goal=r.get("goal", ""),
                   scope=_loads(r.get("scope_json")), config=_loads(r.get("config_json")),
                   status=r.get("status", "active"), created_at=r.get("created_at", ""),
                   updated_at=r.get("updated_at", ""))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass(slots=True)
class Run:
    id: str
    project_id: str
    status: str = "created"
    mode: str = "auto"
    budget: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: str = ""
    ended_at: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Run | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], status=r.get("status", "created"),
                   mode=r.get("mode", "auto"), budget=_loads(r.get("budget_json")),
                   usage=_loads(r.get("usage_json")), error=r.get("error", "") or "",
                   started_at=r.get("started_at", "") or "", ended_at=r.get("ended_at", "") or "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Node:
    id: str
    project_id: str
    kind: str
    title: str
    body: str = ""
    status: str = "open"
    parent_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    seq: int = 0

    @classmethod
    def new(cls, project_id: str, kind: str, title: str, body: str = "", *,
            status: str = "open", parent_id: str | None = None,
            meta: dict[str, Any] | None = None, node_id: str | None = None) -> "Node":
        ts = now_iso()
        return cls(id=node_id or new_id("n"), project_id=project_id, kind=kind,
                   title=title.strip()[:300] or kind, body=body or "", status=status,
                   parent_id=parent_id, meta=meta or {}, created_at=ts, updated_at=ts)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Node | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], kind=r.get("kind", ""),
                   title=r.get("title", ""), body=r.get("body", "") or "",
                   status=r.get("status", "open"), parent_id=r.get("parent_id"),
                   meta=_loads(r.get("meta_json")), created_at=r.get("created_at", ""),
                   updated_at=r.get("updated_at", ""), seq=int(r.get("seq") or 0))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def severity(self) -> str:
        return str(self.meta.get("severity", "info")).lower()


@dataclass(slots=True)
class Edge:
    id: str
    project_id: str
    src_id: str
    dst_id: str
    rel: str
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Edge | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], src_id=r["src_id"],
                   dst_id=r["dst_id"], rel=r.get("rel", ""), created_at=r.get("created_at", ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Asset:
    id: str
    project_id: str
    kind: str
    value: str
    parent_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    first_seen: str = ""
    last_seen: str = ""

    @classmethod
    def new(cls, project_id: str, kind: str, value: str, *,
            parent_id: str | None = None, meta: dict[str, Any] | None = None) -> "Asset":
        ts = now_iso()
        return cls(id=new_id("a"), project_id=project_id, kind=kind,
                   value=value.strip(), parent_id=parent_id, meta=meta or {},
                   first_seen=ts, last_seen=ts)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Asset | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], kind=r.get("kind", ""),
                   value=r.get("value", ""), parent_id=r.get("parent_id"),
                   meta=_loads(r.get("meta_json")), first_seen=r.get("first_seen", ""),
                   last_seen=r.get("last_seen", ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Todo:
    id: str
    project_id: str
    position: int
    title: str
    detail: str = ""
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    intent_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def new(cls, project_id: str, position: int, title: str, detail: str = "", *,
            status: str = "pending", depends_on: list[str] | None = None,
            intent_id: str | None = None) -> "Todo":
        ts = now_iso()
        return cls(id=new_id("t"), project_id=project_id, position=position,
                   title=title.strip()[:300], detail=detail or "", status=status,
                   depends_on=depends_on or [], intent_id=intent_id,
                   created_at=ts, updated_at=ts)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Todo | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], position=int(r.get("position") or 0),
                   title=r.get("title", ""), detail=r.get("detail", "") or "",
                   status=r.get("status", "pending"), depends_on=_loads(r.get("depends_on_json"), []),
                   intent_id=r.get("intent_id"), created_at=r.get("created_at", ""),
                   updated_at=r.get("updated_at", ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Trace:
    id: str
    project_id: str
    run_id: str
    task_id: str
    agent: str
    seq: int
    kind: str
    content: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def new(cls, project_id: str, run_id: str, task_id: str, agent: str, seq: int,
            kind: str, content: str = "", *, tool_name: str = "",
            tool_args: dict[str, Any] | None = None,
            tool_result: dict[str, Any] | None = None) -> "Trace":
        return cls(id=new_id("tr"), project_id=project_id, run_id=run_id, task_id=task_id,
                   agent=agent, seq=seq, kind=kind, content=content,
                   tool_name=tool_name, tool_args=tool_args or {},
                   tool_result=tool_result or {}, created_at=now_iso())

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Trace | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], run_id=r["run_id"],
                   task_id=r["task_id"], agent=r.get("agent", ""), seq=int(r.get("seq") or 0),
                   kind=r.get("kind", ""), content=r.get("content", "") or "",
                   tool_name=r.get("tool_name", "") or "",
                   tool_args=_loads(r.get("tool_args_json")),
                   tool_result=_loads(r.get("tool_result_json")),
                   created_at=r.get("created_at", ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Task:
    id: str
    run_id: str
    project_id: str
    type: str
    status: str = "queued"
    intent_id: str | None = None
    agent: str = ""
    attempts: int = 0
    error: str = ""
    started_at: str = ""
    ended_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Task | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], run_id=r["run_id"], project_id=r["project_id"],
                   type=r.get("type", ""), status=r.get("status", "queued"),
                   intent_id=r.get("intent_id"), agent=r.get("agent", "") or "",
                   attempts=int(r.get("attempts") or 0), error=r.get("error", "") or "",
                   started_at=r.get("started_at", "") or "", ended_at=r.get("ended_at", "") or "",
                   meta=_loads(r.get("meta_json")))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Evidence:
    id: str
    project_id: str
    finding_id: str
    kind: str
    path: str = ""
    sha256: str = ""
    excerpt: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Evidence | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], finding_id=r["finding_id"],
                   kind=r.get("kind", ""), path=r.get("path", "") or "",
                   sha256=r.get("sha256", "") or "", excerpt=r.get("excerpt", "") or "",
                   meta=_loads(r.get("meta_json")), created_at=r.get("created_at", ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Message:
    id: str
    project_id: str
    role: str
    content: str
    created_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Message | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], role=r.get("role", "user"),
                   content=r.get("content", ""), created_at=r.get("created_at", ""),
                   meta=_loads(r.get("meta_json")))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AuditEntry:
    id: str
    project_id: str
    ts: str
    actor: str
    action: str
    decision: str
    target: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "AuditEntry | None":
        r = _row(row)
        if not r:
            return None
        return cls(id=r["id"], project_id=r["project_id"], ts=r.get("ts", ""),
                   actor=r.get("actor", ""), action=r.get("action", ""),
                   decision=r.get("decision", ""), target=r.get("target", "") or "",
                   detail=_loads(r.get("detail_json")))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Usage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    steps: int = 0

    def add(self, prompt: int = 0, completion: int = 0, *, llm: int = 0, tools: int = 0, steps: int = 0) -> None:
        self.llm_calls += llm
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.tool_calls += tools
        self.steps += steps

    def merge(self, other: Mapping[str, Any]) -> None:
        self.llm_calls += int(other.get("llm_calls") or 0)
        self.prompt_tokens += int(other.get("prompt_tokens") or 0)
        self.completion_tokens += int(other.get("completion_tokens") or 0)
        self.tool_calls += int(other.get("tool_calls") or 0)
        self.steps += int(other.get("steps") or 0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "steps": self.steps,
        }

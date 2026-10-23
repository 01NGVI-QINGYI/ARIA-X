"""Blackboard context renderer.

The planner and workers never see the raw database. They see a *bounded*
digest of the graph: the goal, scope, hard counts, the highest-value facts and
findings, the asset surface, open intents, the shared todo chain, pending human
hints and a short window of cross-worker traces. Context construction is a
correctness boundary for three reasons:

1. **Cost** — a runaway graph must not blow the model window. The renderer
   enforces a hard character budget and truncates at a line boundary.
2. **Attention** — facts and findings are ordered by decision relevance
   (severity, recency, kind), not insertion accident.
3. **Determinism** — the same store produces the same string. Replay and tests
   depend on that; we never inject timestamps or random ordering.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..models import ASSET_KINDS, SEVERITIES, SEVERITY_RANK, Node, Todo
from ..store import Store
from ..util import first_line, truncate
from ..memory.retrieval import SearchHit, search as retrieve

#: Hard ceiling for a rendered context string (characters, not tokens).
DEFAULT_CONTEXT_CHARS = 12000

_MAX_FACTS = 24
_MAX_FINDINGS = 24
_MAX_ASSETS = 10
_MAX_INTENTS = 30
_MAX_TODOS = 16
_MAX_HINTS = 8
_MAX_HITS = 8
_MAX_TRACES = 12
_MAX_BODY_CHARS = 150


def scope_note(scope: Mapping[str, Any] | str | None) -> str:
    """One-line, human-auditable scope summary used in prompts and reports."""
    if isinstance(scope, str):
        return scope.strip()
    data = dict(scope or {})
    if not data:
        return "scope: (not configured - no target traffic is permitted)"
    authorized = bool(data.get("authorized"))
    targets = list(data.get("targets") or [])
    deny = list(data.get("deny") or [])
    shown = ", ".join(str(item) for item in targets[:12])
    if len(targets) > 12:
        shown += f", … (+{len(targets) - 12} more)"
    note = f"scope: authorized={'true' if authorized else 'false'}; targets=[{shown or '(none)'}]"
    if deny:
        note += f"; deny=[{', '.join(str(item) for item in deny[:6])}]"
    return note


def render_context(store: Store, project_id: str, *, goal: str | None = None,
                   scope: Mapping[str, Any] | str | None = None,
                   hints: Sequence[Node | str] | None = None,
                   query: str = "", max_chars: int = DEFAULT_CONTEXT_CHARS,
                   include_traces: bool = True,
                   include_retrieval: bool = True) -> str:
    """Render a bounded, deterministic blackboard digest.

    ``hints`` are passed in by the orchestrator so that *consuming* a hint is an
    explicit orchestration decision; when omitted, open hint nodes are shown
    read-only. ``query`` enables a TF-IDF "relevant memory" section for worker
    contexts; it never changes the rest of the ordering.
    """
    limit = max(512, int(max_chars or DEFAULT_CONTEXT_CHARS))
    project = None
    try:
        project = store.get_project(project_id)
    except Exception:  # a missing project must still render a usable prompt header
        project = None
    goal_text = (goal if goal is not None else (project.goal if project else "")) or ""
    goal_text = goal_text.strip()
    counts = _safe_counts(store, project_id)

    facts = _safe_nodes(store, project_id, "fact")
    findings = _safe_nodes(store, project_id, "finding")
    intents = [n for n in _safe_nodes(store, project_id, "intent") if n.status in {"open", "active"}]
    todos = _safe_todos(store, project_id)
    hint_items = list(hints) if hints is not None else [
        n for n in _safe_nodes(store, project_id, "hint") if n.status == "open"]

    parts: list[str] = ["# BLACKBOARD CONTEXT", f"goal: {goal_text or '(no goal set)'}"]
    parts.append(scope_note(scope if scope is not None else (project.scope if project else None)))
    parts.append("counts: " + _format_counts(counts))
    parts.append("")

    # -- facts (oldest first, capped to the most recent when large) ----------
    parts.append(f"## FACTS ({len(facts)})")
    selected_facts = facts[-_MAX_FACTS:]
    if not selected_facts:
        parts.append("(none)")
    for node in selected_facts:
        line = f"- {node.id} {first_line(node.title, 140)}"
        body = first_line(node.body, _MAX_BODY_CHARS)
        if body and body.lower() != node.title.lower():
            line += f" :: {body}"
        parts.append(line)
    parts.append("")

    # -- findings (severity, then recency) -----------------------------------
    parts.append(f"## FINDINGS ({len(findings)})")
    ordered_findings = sorted(
        findings,
        key=lambda n: (SEVERITY_RANK.get(n.severity, len(SEVERITIES)), -n.seq),
    )[:_MAX_FINDINGS]
    if not ordered_findings:
        parts.append("(none)")
    for node in ordered_findings:
        line = f"- [{node.severity}] {node.id} {first_line(node.title, 140)}"
        body = first_line(node.body, _MAX_BODY_CHARS)
        if body and body.lower() != node.title.lower():
            line += f" :: {body}"
        parts.append(line)
    parts.append("")

    # -- assets grouped by kind ---------------------------------------------
    assets = _safe_assets(store, project_id)
    parts.append(f"## ASSETS ({len(assets)})")
    if not assets:
        parts.append("(none)")
    else:
        by_kind: dict[str, list[str]] = {}
        for asset in assets:
            by_kind.setdefault(asset.kind, []).append(asset.value)
        for kind in ASSET_KINDS:
            values = by_kind.get(kind) or []
            if not values:
                continue
            shown = [truncate(value, 80) for value in values[:_MAX_ASSETS]]
            suffix = f" (+{len(values) - _MAX_ASSETS} more)" if len(values) > _MAX_ASSETS else ""
            parts.append(f"- {kind}: {', '.join(shown)}{suffix}")
        for kind in sorted(by_kind):
            if kind in ASSET_KINDS:
                continue
            values = by_kind[kind]
            parts.append(f"- {kind}: {', '.join(truncate(v, 80) for v in values[:_MAX_ASSETS])}")
    parts.append("")

    # -- open intents --------------------------------------------------------
    parts.append(f"## OPEN INTENTS ({len(intents)})")
    if not intents:
        parts.append("(none)")
    ordered_intents = sorted(intents, key=lambda n: (_priority(n), n.seq))[:_MAX_INTENTS]
    for node in ordered_intents:
        marker = "active" if node.status == "active" else "open"
        parts.append(f"- [{marker}] {node.id} {first_line(node.title, 200)}")
    parts.append("")

    # -- todo chain ----------------------------------------------------------
    parts.append(f"## TODO CHAIN ({len(todos)})")
    if not todos:
        parts.append("(none)")
    for todo in todos[:_MAX_TODOS]:
        detail = first_line(todo.detail, 100)
        line = f"- #{todo.position} [{todo.status}] {first_line(todo.title, 160)} ({todo.id})"
        if detail:
            line += f" :: {detail}"
        parts.append(line)
    parts.append("")

    # -- hints ---------------------------------------------------------------
    parts.append(f"## HINTS ({len(hint_items)})")
    if not hint_items:
        parts.append("(none)")
    for item in hint_items[:_MAX_HINTS]:
        if isinstance(item, Node):
            parts.append(f"- {item.id} {first_line(item.body or item.title, 300)}")
        else:
            parts.append(f"- {first_line(str(item), 300)}")
    parts.append("")

    # -- relevant memory (optional) -----------------------------------------
    rendered_ids = {node.id for node in selected_facts} | {node.id for node in ordered_findings}
    if include_retrieval and query and query.strip():
        hits = _safe_retrieve(store, project_id, query)
        hits = [hit for hit in hits if hit.id not in rendered_ids]
        if hits:
            parts.append(f"## RELEVANT MEMORY ({len(hits)})")
            for hit in hits[:_MAX_HITS]:
                parts.append(f"- {hit.kind} {hit.id} {first_line(hit.title, 120)} :: "
                             f"{truncate(hit.snippet, 200)} (score {hit.score:.2f})")
            parts.append("")

    # -- recent traces -------------------------------------------------------
    if include_traces:
        traces = _safe_traces(store, project_id, _MAX_TRACES)
        parts.append(f"## RECENT TRACES ({len(traces)})")
        if not traces:
            parts.append("(none)")
        for trace in traces:
            content = first_line(trace.content, 200) if trace.content else ""
            line = f"- {trace.task_id} {trace.agent} {trace.kind}"
            if trace.tool_name:
                line += f" ({trace.tool_name})"
            if content:
                line += f": {content}"
            parts.append(line)
        parts.append("")

    parts.append("# end of context")
    text = "\n".join(parts)
    return _clip(text, limit)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut_at = max(0, limit - 96)
    chunk = text[:cut_at]
    newline = chunk.rfind("\n")
    if newline > max(0, cut_at // 2):
        chunk = chunk[:newline]
    marker = f"\n…[context truncated: {len(text)} chars total]"
    return chunk + marker


def _format_counts(counts: Mapping[str, Any]) -> str:
    nodes = dict(counts.get("nodes") or {})
    assets = dict(counts.get("assets") or {})
    node_text = ", ".join(f"{kind}={nodes.get(kind, 0)}" for kind in
                          ("goal", "intent", "fact", "finding", "hint", "note"))
    asset_total = sum(int(value or 0) for value in assets.values())
    return (f"{node_text}; assets={asset_total}; edges={counts.get('edges', 0)}; "
            f"todos={counts.get('todos', 0)}; tasks={counts.get('tasks', 0)}; "
            f"traces={counts.get('traces', 0)}")


def _priority(node: Node) -> int:
    try:
        return int(node.meta.get("priority", 50))
    except (TypeError, ValueError):
        return 50


def _safe_counts(store: Store, project_id: str) -> dict[str, Any]:
    try:
        return store.counts(project_id)
    except Exception:
        return {}


def _safe_nodes(store: Store, project_id: str, kind: str, limit: int = 5000) -> list[Node]:
    try:
        return store.list_nodes(project_id, kind=kind, limit=limit)
    except Exception:
        return []


def _safe_assets(store: Store, project_id: str, limit: int = 2000) -> list[Any]:
    try:
        return store.list_assets(project_id, limit=limit)
    except Exception:
        return []


def _safe_todos(store: Store, project_id: str) -> list[Todo]:
    try:
        return store.list_todos(project_id)
    except Exception:
        return []


def _safe_traces(store: Store, project_id: str, limit: int) -> list[Any]:
    try:
        traces = store.list_traces(project_id, limit=80, newest_first=True)
    except Exception:
        return []
    # list_traces gives newest-first; reverse to oldest-first and keep a short
    # tail so the planner reads a timeline rather than a random sample.
    traces.reverse()
    return traces[-limit:]


def _safe_retrieve(store: Store, project_id: str, query: str) -> list[SearchHit]:
    try:
        return retrieve(store, project_id, query, kinds=("fact", "finding", "note", "trace"),
                        limit=_MAX_HITS, max_traces=200)
    except Exception:
        return []


__all__ = ["DEFAULT_CONTEXT_CHARS", "render_context", "scope_note"]

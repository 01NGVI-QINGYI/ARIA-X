"""Worker entry points used by the engagement orchestrator.

The orchestrator owns the loop, the blackboard and the budget; this module owns
*how a single reasoning task is executed*. Each function is deliberately thin:
build a spec (role composition lives in :mod:`aria_x.agents.roles`), build an
:class:`AgentTask`, run :class:`~aria_x.agents.base.Agent`, and translate the
outcome into a shape the engine can consume.

Why thin? The engine must be able to swap any of these for a scripted fake in
tests, and every retry/step/budget decision belongs to the engine, not here.
The only policy encoded here is failure handling:

* ``run_planner`` never raises; a broken planner degrades to ``action="noop"``
  so the engagement can stop cleanly instead of crashing mid-run.
* ``run_worker``/``run_bootstrap``/``run_specialist`` return an ``AgentOutcome``
  even when the provider itself fails, so the scheduler can record a failed
  task and continue other intents.
* ``run_reporter`` returns ``""`` on failure; the engine decides whether to
  retry or fall back to a deterministic renderer.
"""

from __future__ import annotations

from typing import Any

from ..errors import AriaXError
from ..llm.base import ChatMessage
from ..llm.router import ModelRouter
from ..tools.base import ToolContext, ToolRegistry
from . import prompts, roles
from .base import Agent, AgentOutcome, AgentSpec, AgentTask

#: Keys the orchestrator may rely on for every planner result.
PLANNER_KEYS = ("action", "reasoning", "intents", "todos", "complete")
_INTENT_KEYS = ("description", "from", "asset_ids", "priority", "todo_id")
_TODO_KEYS = ("title", "detail", "status", "depends_on")
_TODO_STATUSES = {"pending", "active", "done", "blocked", "dropped"}


def _task_id(ctx: ToolContext, kind: str) -> str:
    """Prefer the task record the engine opened; fall back to a stable label."""
    explicit = str(getattr(ctx, "task_id", "") or "").strip()
    if explicit:
        return explicit
    run_id = str(getattr(ctx, "run_id", "") or "run").strip() or "run"
    return f"{run_id}:{kind}"


def _agent_ctx(ctx: ToolContext, spec: AgentSpec) -> ToolContext:
    """Give tools the *acting* agent's identity while keeping project/run ids."""
    try:
        return ctx.child(agent=spec.name, role=spec.role)
    except Exception:  # a partially-built test context still works
        return ctx


def _run_agent(spec: AgentSpec, router: ModelRouter, registry: ToolRegistry,
               ctx: ToolContext, task: AgentTask) -> AgentOutcome:
    """Execute one agent, converting unexpected provider/agent failures to outcomes."""
    try:
        return Agent(spec, router, registry, _agent_ctx(ctx, spec)).run(task)
    except AriaXError as exc:
        return AgentOutcome(ok=False, error=str(exc), finish_reason="error")
    except Exception as exc:  # a bug in a tool or provider must not kill the run
        return AgentOutcome(ok=False, error=f"{type(exc).__name__}: {exc}", finish_reason="error")


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------


def run_planner(ctx: ToolContext, router: ModelRouter, registry: ToolRegistry,
                goal: str, context: str, max_intents: int) -> dict[str, Any]:
    """Produce the next plan as a dict with the frozen planner contract.

    Primary path: a full agent loop (the planner may inspect graph/traces and
    update todos). Repair path: if that loop fails — typically because the
    endpoint cannot emit valid contract JSON even after an in-loop retry — one
    direct ``router.json`` attempt is made with no tools. If that also fails,
    return ``{"action": "noop", ...}`` carrying the error: an engagement that
    cannot plan must stall visibly, never crash.
    """
    limit = max(1, int(max_intents or 1))
    language = roles.language_of(ctx)
    task_prompt = prompts.planner_task(goal, context, limit, language)
    task = AgentTask(task_id=_task_id(ctx, "reason"), kind="reason", prompt=task_prompt)
    spec = roles.planner_spec(ctx, limit)

    outcome = _run_agent(spec, router, registry, ctx, task)
    if outcome.ok and isinstance(outcome.data, dict):
        return normalize_plan(outcome.data, limit)

    error = outcome.error or "planner returned no structured plan"
    try:
        data, _response = router.json(
            "planner",
            [ChatMessage.system(spec.system_prompt), ChatMessage.user(task_prompt)],
            schema=prompts.PLANNER_SCHEMA,
            task_id=task.task_id,
        )
        if isinstance(data, dict):
            return normalize_plan(data, limit)
        error = "planner JSON was not an object"
    except AriaXError as exc:
        error = f"{error}; repair attempt failed: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        error = f"{error}; repair attempt failed: {type(exc).__name__}: {exc}"
    return {
        "action": "noop",
        "reasoning": f"planner unavailable: {error}"[:800],
        "intents": [],
        "todos": [],
        "complete": {},
    }


def normalize_plan(data: dict[str, Any], max_intents: int) -> dict[str, Any]:
    """Coerce model output to exactly the planner keys (and bound intent count).

    Unknown keys are dropped so downstream code never grows an accidental
    dependency on model-specific extras. An ``intents`` action with no usable
    intent, or a ``complete`` action with no summary, is downgraded to ``noop``
    — a planner must not be able to claim progress it did not describe.
    """
    limit = max(1, int(max_intents or 1))
    raw_action = str((data or {}).get("action") or "noop").strip().lower()
    action = raw_action if raw_action in {"intents", "complete", "noop"} else "noop"
    reasoning = str((data or {}).get("reasoning") or "").strip()

    intents: list[dict[str, Any]] = []
    for item in list((data or {}).get("intents") or []):
        intent = _normalize_intent(item)
        if intent is not None:
            intents.append(intent)
        if len(intents) >= limit:
            break

    todos = [todo for todo in (_normalize_todo(item) for item in list((data or {}).get("todos") or []))
             if todo is not None]

    complete_raw = (data or {}).get("complete")
    complete: dict[str, Any] = {}
    if isinstance(complete_raw, dict):
        complete = {
            "summary": str(complete_raw.get("summary") or "").strip(),
            "proof": [str(p) for p in (complete_raw.get("proof") or []) if str(p).strip()],
        }

    if action == "intents" and not intents:
        action = "noop"
        reasoning = (reasoning + " [downgraded: no valid intents]").strip()
    if action == "complete" and not complete.get("summary"):
        action = "noop"
        reasoning = (reasoning + " [downgraded: completion without summary]").strip()

    return {
        "action": action,
        "reasoning": reasoning,
        "intents": intents,
        "todos": todos,
        "complete": complete,
    }


def _normalize_intent(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        description, raw = item.strip(), {}
    elif isinstance(item, dict):
        description = str(item.get("description") or item.get("intent") or item.get("title") or "").strip()
        raw = item
    else:
        return None
    if not description:
        return None
    asset_ids = raw.get("asset_ids") or raw.get("assets") or []
    if isinstance(asset_ids, str):
        asset_ids = [asset_ids]
    parents = raw.get("from") or raw.get("parents") or []
    if isinstance(parents, str):
        parents = [parents]
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    return {
        "description": description[:600],
        "from": [str(p) for p in parents if str(p).strip()],
        "asset_ids": [str(a) for a in asset_ids if str(a).strip()],
        "priority": priority,
        "todo_id": str(raw.get("todo_id") or "").strip(),
    }


def _normalize_todo(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        title, raw = item.strip(), {}
    elif isinstance(item, dict):
        title = str(item.get("title") or "").strip()
        raw = item
    else:
        return None
    if not title:
        return None
    status = str(raw.get("status") or "pending").strip().lower()
    if status not in _TODO_STATUSES:
        status = "pending"
    depends_on = raw.get("depends_on") or []
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    return {
        "title": title[:300],
        "detail": str(raw.get("detail") or ""),
        "status": status,
        "depends_on": [str(d) for d in depends_on if str(d).strip()],
    }


# ---------------------------------------------------------------------------
# workers / bootstrap / specialists
# ---------------------------------------------------------------------------


def run_worker(ctx: ToolContext, router: ModelRouter, registry: ToolRegistry,
               intent_text: str, context: str, goal: str) -> AgentOutcome:
    """Execute exactly one intent as a bounded OODA exploration.

    ``outcome.data`` is the validated WORKER_SCHEMA payload when ``ok`` is true;
    the engine persists facts/findings/assets itself, so a worker can never
    write the graph except through tools.
    """
    spec = roles.worker_spec(ctx)
    task = AgentTask(
        task_id=_task_id(ctx, "explore"),
        kind="explore",
        prompt=prompts.worker_task(intent_text, context, roles.language_of(ctx)),
        goal=str(goal or ""),
    )
    return _run_agent(spec, router, registry, ctx, task)


def run_bootstrap(ctx: ToolContext, router: ModelRouter, registry: ToolRegistry,
                  goal: str, context: str) -> AgentOutcome:
    """One direct attempt at the goal before the reason→explore loop starts."""
    spec = roles.bootstrap_spec(ctx)
    task = AgentTask(
        task_id=_task_id(ctx, "bootstrap"),
        kind="bootstrap",
        prompt=prompts.bootstrap_task(goal, context, roles.language_of(ctx)),
    )
    return _run_agent(spec, router, registry, ctx, task)


def run_specialist(ctx: ToolContext, router: ModelRouter, registry: ToolRegistry,
                   name: str, question: str, context: str) -> AgentOutcome:
    """Answer one narrow technical sub-question with a restricted toolset.

    Unknown specialist names still run (with no tools) rather than failing:
    the model asked for expertise we do not have a toolset for, and a reasoned
    answer is more useful to the delegating worker than an error.
    """
    spec = roles.specialist_spec(ctx, name)
    task = AgentTask(
        task_id=_task_id(ctx, f"specialist:{spec.name}"),
        kind="specialist",
        prompt=prompts.specialist_task(name, question, context),
    )
    return _run_agent(spec, router, registry, ctx, task)


# ---------------------------------------------------------------------------
# reporter
# ---------------------------------------------------------------------------


def run_reporter(ctx: ToolContext, router: ModelRouter, registry: ToolRegistry,
                 context: str) -> str:
    """Write the final Markdown report from verified graph data only."""
    spec = roles.reporter_spec(ctx)
    task = AgentTask(
        task_id=_task_id(ctx, "report"),
        kind="report",
        prompt=prompts.reporter_task(context, roles.language_of(ctx)),
    )
    outcome = _run_agent(spec, router, registry, ctx, task)
    if not outcome.ok:
        return ""
    return (outcome.text or "").strip()


__all__ = [
    "PLANNER_KEYS", "normalize_plan", "run_bootstrap", "run_planner", "run_reporter",
    "run_specialist", "run_worker",
]

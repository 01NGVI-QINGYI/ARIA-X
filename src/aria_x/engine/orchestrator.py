"""Engagement orchestrator: the reason -> explore loop.

This is where ARIA-X' three lineages meet at runtime:

* **Cairn** supplies the blackboard discipline. The graph is the only source of
  truth; every worker answer becomes facts/findings/assets plus edges, and the
  planner sees a rendered digest of that graph rather than raw chat history.
* **ARTEX** supplies the planner/worker split and the shared todo chain. Exactly
  one planner call per round may create intents; workers only execute one claimed
  intent each; the todo chain survives across rounds.
* **PentAGI** supplies the specialist delegation seam and the report hand-off.

Design invariants enforced here (and asserted by tests):

1. ``config.require_authorization`` runs before any active work.
2. At most ``budget.max_concurrency`` explore tasks run at once, and every
   intent is claimed atomically before work starts.
3. An intent description is never duplicated, and the total intent count is
   capped by the budget.
4. The loop always terminates: completion, budget, cancellation or the idle
   guard. Budget snapshots are persisted and emitted every round.
5. A report is written on *every* terminal path (completed/failed/cancelled/
   budget), and report failures never destroy the run.
"""

from __future__ import annotations

import html
import importlib
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import AriaXConfig, ScopeConfig
from ..errors import AriaXError, BudgetExceeded, Cancelled, ConfigError
from ..events import (
    APPROVAL_DECIDED, APPROVAL_REQUESTED, ASSET_FOUND, BUDGET, ERROR, FINDING_CREATED,
    HINT, NODE_CREATED, NODE_UPDATED, REPORT_READY, RUN_STARTED, RUN_FINISHED,
    TASK_FAILED, TASK_FINISHED, TASK_STARTED, TODO_UPDATED,
)
from ..llm.jsonmode import extract_json
from ..llm.router import ModelRouter
from ..models import (
    ASSET_KINDS, SEVERITIES, TODO_STATUSES, Asset, Evidence, Node, Project, Run, Task, Todo,
)
from ..store import Store
from ..tools.base import ToolContext, ToolRegistry
from ..tools.sandbox import Sandbox, build_sandbox
from ..tools.scope import Scope
from ..util import first_line, json_dumps, new_id, now_iso, truncate
from .budget import Budget
from .context import DEFAULT_CONTEXT_CHARS, render_context, scope_note
from .scheduler import ScheduledResult, TaskOutcome, TaskScheduler

@dataclass(slots=True)
class RunOutcome:
    """Engine-level result; :class:`~aria_x.engine.engagement.EngagementResult`
    is the public projection of this record."""

    ok: bool
    project_id: str = ""
    run_id: str = ""
    status: str = "failed"
    summary: str = ""
    report_path: str = ""
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "project_id": self.project_id, "run_id": self.run_id,
            "status": self.status, "summary": self.summary,
            "report_path": self.report_path, "error": self.error,
            "metrics": self.metrics,
        }


class Orchestrator:
    """Drive one project/run from bootstrap to report."""

    def __init__(self, config: AriaXConfig, *, store: Store, events: Any,
                 provider: Any = None, cancel: threading.Event | None = None,
                 registry: ToolRegistry | None = None,
                 workers: Any = None, sandbox: Sandbox | None = None) -> None:
        self.config = config
        self.store = store
        self.events = events
        self.provider = provider
        self.cancel = cancel or threading.Event()
        self._registry_override = registry
        self._workers_override = workers
        self._sandbox_override = sandbox

        self.budget: Budget | None = None
        self.router: ModelRouter | None = None
        self.scope: Scope | None = None
        self.sandbox: Sandbox | None = None
        self.registry: ToolRegistry | None = None
        self.project: Project | None = None
        self.run_record: Run | None = None
        self.goal = ""
        self.goal_node: Node | None = None
        self.workdir: Path | None = None
        self.scheduler: TaskScheduler | None = None

        self._rounds = 0
        self._intents_created = 0
        self._intents_completed = 0
        self._intents_failed = 0
        self._planner_errors = 0
        self._intent_budget_hit = False
        self._completion_summary = ""
        self._last_error = ""
        self._delegate_state = threading.local()

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------

    def run(self, project: Project, run: Run, *, goal: str | None = None) -> RunOutcome:
        """Execute the loop and always return a coherent :class:`RunOutcome`."""
        self._prepare(project, run, goal=goal)
        status, summary, error = "completed", "", ""
        try:
            status, summary, error = self._loop()
        except BudgetExceeded as exc:
            status, summary, error = "budget", "", str(exc)
        except Cancelled as exc:
            status, summary, error = "cancelled", "Run cancelled by operator.", str(exc)
        except KeyboardInterrupt:
            self.cancel.set()
            status, summary, error = "cancelled", "Run interrupted by operator.", "keyboard interrupt"
        except Exception as exc:  # noqa: BLE001 - an engagement must fail closed, with a report
            status = "cancelled" if self.cancel.is_set() else "failed"
            summary = ""
            error = f"{type(exc).__name__}: {exc}"
            self._last_error = error
            self._emit(ERROR, {"message": error,
                               "traceback": traceback.format_exc(limit=6)})
        finally:
            if self.scheduler is not None:
                try:
                    self.scheduler.shutdown(wait=True)
                except Exception:
                    pass
        return self._finalize(status, summary, error)

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _prepare(self, project: Project, run: Run, *, goal: str | None) -> None:
        self.project = project
        self.run_record = run
        requested_goal = (goal or "").strip()
        if requested_goal and requested_goal != project.goal:
            try:
                self.store.update_project(project.id, goal=requested_goal)
                project.goal = requested_goal
            except Exception:
                pass
        self.goal = (project.goal or requested_goal or "").strip()
        # Security gate: refuse before a run row performs any active work.
        self.config.require_authorization()
        project_scope = dict(project.scope or {})
        if project_scope.get("authorized") is False:
            raise ConfigError("refusing to start: project scope.authorized is false")

        self.budget = Budget(self.config.budget)
        self.router = ModelRouter(self.config, budget=self.budget, events=self.events,
                                  project_id=project.id, run_id=run.id,
                                  provider=self.provider)
        self.scope = Scope(self._effective_scope(project_scope))
        if not self.scope.config.authorized or not (
                self.scope.host_rules or self.scope.net_rules or self.scope.url_rules):
            raise ConfigError("refusing to start: engagement scope has no authorized targets")

        run_dir = Path(self.config.runs_dir) / run.id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.workdir = Path(self.config.sandbox.workdir) if self.config.sandbox.workdir \
            else run_dir / "work"
        self.sandbox = self._sandbox_override or build_sandbox(self.config.sandbox, self.workdir)

        base_ctx = ToolContext(
            project=project, run_id=run.id, task_id="", agent="orchestrator",
            role="engine", store=self.store, config=self.config, events=self.events,
            scope=self.scope, sandbox=self.sandbox, workdir=self.workdir,
            budget=self.budget, cancel=self.cancel,
            delegate=self._delegate_for("orchestrator"),
            request_approval=self._approval_for("orchestrator"),
        )
        self.registry = self._registry_override or self._build_registry(base_ctx)
        self.goal_node = self._ensure_goal_node()
        self.scheduler = TaskScheduler(
            self.store, self.events,
            max_concurrency=max(1, int(self.config.budget.max_concurrency or 1)),
            cancel=self.cancel,
            retries=max(0, int(self.config.engine.retry_failed_tasks or 0)),
        )
        self._update_run(status="running", started_at=run.started_at or now_iso())
        self._persist_budget()
        self._emit(RUN_STARTED, {
            "goal": self.goal, "mode": run.mode,
            "max_concurrency": self.config.budget.max_concurrency,
            "bootstrap": bool(self.config.engine.bootstrap),
            "scope": scope_note(project_scope or self._scope_dict()),
        })

    def _effective_scope(self, project_scope: Mapping[str, Any]) -> ScopeConfig:
        """Config scope is authoritative; project scope may only narrow it.

        The project record can supply targets when the process config has none
        (tests and the API often do this), but it can never grant authorization
        that the config denies.
        """
        base = ScopeConfig(**{name: getattr(self.config.scope, name)
                              for name in self.config.scope.__slots__})
        if not base.targets and project_scope.get("targets"):
            base.targets = [str(item) for item in project_scope["targets"]]
        if project_scope.get("deny"):
            merged = list(base.deny)
            for item in project_scope["deny"]:
                if str(item) not in merged:
                    merged.append(str(item))
            base.deny = merged
        if project_scope.get("authorized") is False:
            base.authorized = False
        return base

    def _scope_dict(self) -> dict[str, Any]:
        return {
            "authorized": bool(self.scope and self.scope.config.authorized),
            "targets": list(self.scope.config.targets) if self.scope else [],
            "deny": list(self.scope.config.deny) if self.scope else [],
        }

    def _build_registry(self, ctx: ToolContext) -> ToolRegistry:
        try:
            from ..tools.builtin import build_registry
            return build_registry(ctx)
        except Exception as exc:  # tools layer may not be installed / still loading
            self._emit(ERROR, {"message": f"tool registry unavailable: {exc}"})
            return ToolRegistry()

    def _ensure_goal_node(self) -> Node:
        assert self.project is not None
        goal_title = self.goal[:300] or "engagement goal"
        try:
            goals = self.store.list_nodes(self.project.id, kind="goal", limit=50)
        except Exception:
            goals = []
        latest = max(goals, key=lambda item: item.seq) if goals else None
        if latest is not None and latest.title.strip() == goal_title.strip():
            if latest.status == "proven":
                latest = self.store.update_node(latest.id, status="open")
                self._emit(NODE_UPDATED, {"id": latest.id, "kind": "goal", "status": "open"})
            return latest
        if latest is not None and latest.status in {"open", "active"}:
            self.store.update_node(latest.id, status="superseded")
            self._emit(NODE_UPDATED, {"id": latest.id, "kind": "goal", "status": "superseded"})
        node = Node.new(self.project.id, "goal", goal_title, self.goal, status="open",
                        meta={"run_id": self.run_record.id if self.run_record else ""})
        self.store.add_node(node)
        self._emit(NODE_CREATED, {"id": node.id, "kind": node.kind, "title": node.title})
        return node

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def _loop(self) -> tuple[str, str, str]:
        assert self.budget is not None
        if self.config.engine.bootstrap:
            self._bootstrap_step()
            if self._goal_proven():
                return "completed", self._completion_summary or "Goal proven during bootstrap.", ""

        idle_rounds = 0
        max_idle = max(1, int(self.config.engine.max_idle_rounds or 1))
        intent_cap = max(1, int(self.config.budget.max_total_intents or 40))
        max_rounds = max(4, min(intent_cap, 200) + max_idle + 6)
        last_summary = ""

        while not self.cancel.is_set():
            self.budget.check_time()
            self._rounds += 1
            if self._rounds > max_rounds:
                return "failed", "", f"round ceiling reached ({max_rounds}) without completion"
            self._persist_budget()

            planner = self._reason_step()
            action = str(planner.get("action") or "noop").strip().lower()
            self._apply_planner_todos(planner.get("todos") or [])
            created: list[Node] = []
            if action == "complete":
                complete = planner.get("complete") if isinstance(planner.get("complete"), dict) else {}
                summary = str(complete.get("summary") or "").strip()
                if self.config.engine.require_finding_for_completion and not self._has_findings():
                    self._emit(ERROR, {"message": "planner reported complete but no finding exists; "
                                                  "require_finding_for_completion is enabled"})
                    idle_rounds += 1
                else:
                    self._mark_goal_proven(summary)
                    return "completed", summary or self._default_summary("completed"), ""
            elif action == "intents":
                created = self._create_intents(planner.get("intents") or [])
                last_summary = str(planner.get("reasoning") or "").strip()
            # action == noop falls through with no new intents.
            # workers.run_planner deliberately converts AriaXError to action=noop,
            # so a swallowed BudgetExceeded must still stop the run as "budget".
            self._enforce_budget_ceiling()

            open_intents = self._select_open_intents(self.config.budget.max_concurrency)
            dispatched = 0
            if open_intents and not self.cancel.is_set():
                results = self._dispatch(open_intents)
                dispatched = len(results)
                for result in results:
                    if result.status == "done":
                        self._intents_completed += 1
                    elif result.status in {"failed", "cancelled"}:
                        self._intents_failed += 1

            if self._goal_proven():
                return "completed", self._completion_summary or self._default_summary("completed"), ""

            if self._intent_budget_hit and not self._select_open_intents(1):
                return "budget", "", (f"intent budget exhausted "
                                      f"({self._intents_created}/{self.config.budget.max_total_intents})")

            if dispatched == 0 and not created:
                idle_rounds += 1
                if idle_rounds >= max_idle:
                    if self._planner_errors >= idle_rounds and not self._has_facts():
                        return "failed", "", self._last_error or "planner produced no work"
                    summary = ("No productive intents remain; stopping with the current graph."
                               if not last_summary else
                               f"No productive intents remain; last planner note: {last_summary}")
                    return "completed", summary, ""
            else:
                idle_rounds = 0

        return "cancelled", "Run cancelled by operator.", ""

    # ------------------------------------------------------------------
    # bootstrap
    # ------------------------------------------------------------------

    def _bootstrap_step(self) -> None:
        assert self.project is not None and self.budget is not None
        if self.cancel.is_set():
            return
        try:
            self.budget.check_llm()
        except BudgetExceeded:
            raise
        task = self._start_task("bootstrap", "bootstrap")
        try:
            context = render_context(self.store, self.project.id, goal=self.goal,
                                     scope=self._scope_dict(), query=self.goal,
                                     max_chars=DEFAULT_CONTEXT_CHARS)
            outcome = self._call_workers(
                "run_bootstrap", ctx=self._ctx_for(task, "bootstrap", "worker"),
                router=self.router, registry=self.registry, goal=self.goal, context=context)
        except (BudgetExceeded, Cancelled) as exc:
            self._finish_task(task, ok=False, error=str(exc))
            raise
        except Exception as exc:  # bootstrap is optional; a failure must not kill the run
            self._finish_task(task, ok=False, error=f"{type(exc).__name__}: {exc}")
            self._emit(ERROR, {"message": f"bootstrap failed: {exc}"})
            return
        ok = bool(getattr(outcome, "ok", False))
        error = str(getattr(outcome, "error", "") or "")
        self._finish_task(task, ok=ok, error=error)
        if not ok:
            self._emit(ERROR, {"message": f"bootstrap returned no usable result: {error}"})
            return
        persisted = self._persist_outcome(outcome, task=task, intent=None)

    # ------------------------------------------------------------------
    # reason
    # ------------------------------------------------------------------

    def _reason_step(self) -> dict[str, Any]:
        assert self.project is not None and self.budget is not None
        hints = self._open_hints()
        context = render_context(
            self.store, self.project.id, goal=self.goal, scope=self._scope_dict(),
            hints=hints, query=self.goal, max_chars=DEFAULT_CONTEXT_CHARS)
        task = self._start_task("reason", "planner", meta={"round": self._rounds})
        max_intents = max(1, int(self.config.budget.max_intents_per_reason or 1))
        try:
            self.budget.check_llm()
            data = self._call_workers(
                "run_planner", ctx=self._ctx_for(task, "planner", "planner"),
                router=self.router, registry=self.registry, goal=self.goal,
                context=context, max_intents=max_intents)
        except (BudgetExceeded, Cancelled) as exc:
            self._finish_task(task, ok=False, error=str(exc))
            raise
        except Exception as exc:  # planner failure: one idle round, then the guard decides
            self._planner_errors += 1
            self._last_error = f"planner failed: {type(exc).__name__}: {exc}"
            self._finish_task(task, ok=False, error=self._last_error)
            self._emit(ERROR, {"message": self._last_error})
            return {"action": "noop", "reasoning": self._last_error}
        normalised = self._normalise_planner(data)
        self._finish_task(task, ok=True, meta={"round": self._rounds,
                                               "action": normalised.get("action", "noop")})
        self._consume_hints(hints)
        return normalised

    def _normalise_planner(self, data: Any) -> dict[str, Any]:
        if isinstance(data, str):
            try:
                data = extract_json(data)
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            return {"action": "noop", "reasoning": "planner returned a non-object"}
        action = str(data.get("action") or "noop").strip().lower()
        if action not in {"intents", "complete", "noop"}:
            action = "noop"
        out = dict(data)
        out["action"] = action
        if action == "intents" and not isinstance(data.get("intents"), list):
            out["intents"] = []
        if action == "complete" and not isinstance(data.get("complete"), dict):
            out["complete"] = {"summary": str(data.get("summary") or "")}
        return out

    # ------------------------------------------------------------------
    # intents
    # ------------------------------------------------------------------

    def _create_intents(self, items: Sequence[Any]) -> list[Node]:
        assert self.project is not None and self.budget is not None
        if not isinstance(items, (list, tuple)):
            return []
        goal_id = self.goal_node.id if self.goal_node else None
        existing = self.store.list_nodes(self.project.id, kind="intent", limit=10000)
        seen = {_normalise_text(node.title): node for node in existing}
        remaining = max(0, int(self.config.budget.max_total_intents or 40) - self._intents_created)
        created: list[Node] = []
        for raw in list(items)[:max(1, int(self.config.budget.max_intents_per_reason or 1))]:
            if not isinstance(raw, Mapping):
                continue
            description = str(raw.get("description") or "").strip()
            if not description:
                continue
            key = _normalise_text(description)
            if key in seen:
                continue
            if remaining <= 0:
                self._intent_budget_hit = True
                break
            try:
                self.budget.check_intents(1)
            except BudgetExceeded:
                self._intent_budget_hit = True
                break
            sources = self._valid_ids(raw.get("from") or [])
            todo_id = str(raw.get("todo_id") or "").strip()
            todo = None
            if todo_id:
                try:
                    todo = self.store.get_todo(todo_id)
                except Exception:
                    todo = None
            meta: dict[str, Any] = {
                "priority": _int_or(raw.get("priority"), 50),
                "from": sources,
                "todo_id": todo.id if todo else "",
                "round": self._rounds,
                "asset_ids": [str(x) for x in (raw.get("asset_ids") or []) if str(x)],
            }
            node = Node.new(self.project.id, "intent", description, description,
                            status="open", parent_id=goal_id, meta=meta)
            self.store.add_node(node)
            self.budget.record_intent(1)
            self._intents_created += 1
            remaining -= 1
            seen[key] = node
            created.append(node)
            if goal_id:
                try:
                    self.store.add_edge(self.project.id, goal_id, node.id, "spawns")
                except Exception:
                    pass
            for source in sources:
                try:
                    self.store.add_edge(self.project.id, source, node.id, "spawns")
                except Exception:
                    pass
            self._emit(NODE_CREATED, {"id": node.id, "kind": "intent",
                                      "title": node.title, "priority": meta["priority"]})
        return created

    def _valid_ids(self, values: Any) -> list[str]:
        out: list[str] = []
        for value in list(values or [])[:8]:
            identifier = str(value or "").strip()
            if not identifier or identifier in out:
                continue
            try:
                if self.store.find_node(identifier) is not None:
                    out.append(identifier)
            except Exception:
                continue
        return out

    def _select_open_intents(self, limit: int) -> list[Node]:
        assert self.project is not None
        try:
            intents = self.store.list_nodes(self.project.id, kind="intent",
                                            status="open", limit=1000)
        except Exception:
            return []
        intents.sort(key=lambda node: (_priority(node), node.seq))
        return intents[: max(1, int(limit or 1))]

    def _dispatch(self, intents: Sequence[Node]) -> list[ScheduledResult]:
        assert self.scheduler is not None
        jobs: list[tuple[Task, Callable[[Task], Any]]] = []
        for intent in intents:
            task = self._new_task("explore", "worker", intent_id=intent.id,
                                  meta={"intent_id": intent.id, "round": self._rounds})
            jobs.append((task, lambda scheduled, intent=intent: self._explore(intent, scheduled)))
        return self.scheduler.run(jobs)

    def _explore(self, intent: Node, task: Task) -> TaskOutcome:
        assert self.project is not None and self.budget is not None
        if self.cancel.is_set():
            return TaskOutcome(ok=False, status="cancelled", error="run cancelled")
        if not self.store.claim_intent(intent.id):
            return TaskOutcome(ok=True, status="skipped",
                               value={"skipped": True, "intent_id": intent.id},
                               error="intent already claimed")
        try:
            self.budget.check_time()
            context = render_context(
                self.store, self.project.id, goal=self.goal, scope=self._scope_dict(),
                query=f"{intent.title}\n{intent.body}", max_chars=DEFAULT_CONTEXT_CHARS)
            outcome = self._call_workers(
                "run_worker", ctx=self._ctx_for(task, "worker", "worker"),
                router=self.router, registry=self.registry,
                intent_text=intent.title, context=context, goal=self.goal)
        except BudgetExceeded as exc:
            self._revert_intent(intent.id)
            return TaskOutcome(ok=False, status="failed", error=str(exc))
        except Cancelled as exc:
            self._revert_intent(intent.id)
            return TaskOutcome(ok=False, status="cancelled", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - worker failure becomes a task failure
            self._fail_intent(intent.id)
            return TaskOutcome(ok=False, status="failed",
                               error=f"{type(exc).__name__}: {exc}")
        ok = bool(getattr(outcome, "ok", False))
        error = str(getattr(outcome, "error", "") or "")
        if not ok:
            if self.cancel.is_set():
                self._revert_intent(intent.id)
                return TaskOutcome(ok=False, status="cancelled", error=error or "cancelled")
            self._fail_intent(intent.id)
            return TaskOutcome(ok=False, status="failed", error=error or "worker failed")
        try:
            persisted = self._persist_outcome(outcome, task=task, intent=intent)
        except Exception as exc:  # persistence bug must not corrupt the claim
            self._fail_intent(intent.id)
            return TaskOutcome(ok=False, status="failed",
                               error=f"persistence failed: {type(exc).__name__}: {exc}")
        self.store.update_node(intent.id, status="done")
        self._emit(NODE_UPDATED, {"id": intent.id, "kind": "intent", "status": "done"})
        self._complete_linked_todo(intent)
        return TaskOutcome(ok=True, status="done",
                           value={"intent_id": intent.id, **{k: len(v or []) for k, v in persisted.items()}})

    def _revert_intent(self, intent_id: str) -> None:
        try:
            self.store.update_node(intent_id, status="open")
        except Exception:
            pass

    def _fail_intent(self, intent_id: str) -> None:
        try:
            self.store.update_node(intent_id, status="failed")
            self._emit(NODE_UPDATED, {"id": intent_id, "kind": "intent", "status": "failed"})
        except Exception:
            pass

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _persist_outcome(self, outcome: Any, *, task: Task, intent: Node | None,
                         derived_from: Sequence[str] | None = None) -> dict[str, list[Any]]:
        """Write one worker answer into the graph.

        Ordering matters: assets are upserted before findings so a finding can
        be anchored to the asset it names; the fact is created last so its
        ``derived_from`` edges can point at everything that produced it.
        """
        assert self.project is not None
        data = self._outcome_data(outcome)
        persisted: dict[str, list[Any]] = {"facts": [], "findings": [], "assets": []}
        if not data:
            return persisted
        sources = list(derived_from or [])
        if intent is not None:
            sources.insert(0, intent.id)
            sources.extend(intent.meta.get("from") or [])
        sources = [sid for sid in dict.fromkeys(str(x) for x in sources if x)]
        parent_id = intent.id if intent is not None else (self.goal_node.id if self.goal_node else None)

        # 1. assets ---------------------------------------------------------
        for item in _as_list(data.get("assets")):
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            value = str(item.get("value") or "").strip()
            if kind not in ASSET_KINDS or not value:
                continue
            parent = self._find_asset(value=str(item.get("parent_value") or ""))
            asset = self.store.upsert_asset(
                self.project.id, kind, value,
                parent_id=parent.id if parent else None,
                meta={"task_id": task.id, "intent_id": intent.id if intent else ""})
            persisted["assets"].append(asset)
            self._emit(ASSET_FOUND, {"id": asset.id, "kind": asset.kind,
                                     "value": asset.value, "parent_id": asset.parent_id})

        asset_map = self._asset_map()

        # 2. fact -----------------------------------------------------------
        fact_data = data.get("fact")
        fact_node: Node | None = None
        if isinstance(fact_data, Mapping) and str(fact_data.get("title") or "").strip():
            title = str(fact_data.get("title") or "").strip()
            body = str(fact_data.get("body") or "").strip()
            confidence = str(fact_data.get("confidence") or "medium").lower()
            fact_node = self._existing_for_task("fact", task.id, title)
            if fact_node is None:
                fact_node = Node.new(
                    self.project.id, "fact", title, body, status="done", parent_id=parent_id,
                    meta={"confidence": confidence, "task_id": task.id,
                          "intent_id": intent.id if intent else "",
                          "source": "worker"})
                self.store.add_node(fact_node)
                self._emit(NODE_CREATED, {"id": fact_node.id, "kind": "fact",
                                          "title": fact_node.title, "confidence": confidence})
            persisted["facts"].append(fact_node)
            for source in sources:
                try:
                    self.store.add_edge(self.project.id, fact_node.id, source, "derived_from")
                except Exception:
                    pass
            if intent is not None:
                try:
                    self.store.add_edge(self.project.id, intent.id, fact_node.id, "yields")
                except Exception:
                    pass

        # 3. findings -------------------------------------------------------
        for item in _as_list(data.get("findings")):
            if not isinstance(item, Mapping) or not str(item.get("title") or "").strip():
                continue
            title = str(item.get("title") or "").strip()
            if self._existing_for_task("finding", task.id, title) is not None:
                continue
            severity = str(item.get("severity") or "info").strip().lower()
            if severity not in SEVERITIES:
                severity = "info"
            asset_ids: list[str] = []
            for value in _as_list(item.get("asset_values")):
                asset = self._lookup_asset(asset_map, str(value))
                if asset is not None and asset.id not in asset_ids:
                    asset_ids.append(asset.id)
            cvss = _float_or_none(item.get("cvss"))
            finding = self.store.add_finding(
                self.project.id,
                title,
                str(item.get("body") or "").strip(),
                severity=severity, cvss=cvss,
                confidence=str(item.get("confidence") or "medium").lower(),
                asset_ids=asset_ids, intent_id=intent.id if intent else None,
                meta={"task_id": task.id, "source": "worker"})
            evidence_text = str(item.get("evidence") or "").strip()
            if evidence_text:
                try:
                    self.store.add_evidence(Evidence(
                        id=new_id("ev"), project_id=self.project.id, finding_id=finding.id,
                        kind="note", excerpt=truncate(evidence_text, 4000),
                        meta={"task_id": task.id}, created_at=now_iso()))
                except Exception:
                    pass
            persisted["findings"].append(finding)
            self._emit(FINDING_CREATED, {"id": finding.id, "title": finding.title,
                                         "severity": finding.severity,
                                         "confidence": finding.meta.get("confidence", "medium"),
                                         "assets": asset_ids})

        # 4. goal proof -----------------------------------------------------
        if bool(data.get("goal_proven")) and self.goal_node is not None:
            try:
                self.store.update_node(self.goal_node.id, status="proven")
                self._emit(NODE_UPDATED, {"id": self.goal_node.id, "kind": "goal",
                                          "status": "proven"})
            except Exception:
                pass
            summary = str(data.get("summary") or "").strip()
            if not summary and fact_node is not None:
                summary = fact_node.title
            self._completion_summary = summary or "Goal proven by recorded evidence."
        return persisted

    def _outcome_data(self, outcome: Any) -> dict[str, Any]:
        data = getattr(outcome, "data", None)
        if isinstance(data, str):
            try:
                data = extract_json(data)
            except ValueError:
                data = None
        if not isinstance(data, dict):
            text = getattr(outcome, "text", "") or ""
            if text.strip():
                try:
                    candidate = extract_json(text)
                    data = candidate if isinstance(candidate, dict) else None
                except ValueError:
                    data = None
        return dict(data) if isinstance(data, dict) else {}

    def _asset_map(self) -> dict[str, Asset]:
        assert self.project is not None
        out: dict[str, Asset] = {}
        try:
            for asset in self.store.list_assets(self.project.id, limit=10000):
                out.setdefault(asset.value, asset)
                out.setdefault(asset.value.lower(), asset)
        except Exception:
            pass
        return out

    def _lookup_asset(self, asset_map: Mapping[str, Asset], value: str) -> Asset | None:
        if not value:
            return None
        return asset_map.get(value) or asset_map.get(value.strip().lower())

    def _find_asset(self, *, value: str) -> Asset | None:
        if not value:
            return None
        return self._lookup_asset(self._asset_map(), value.strip())

    def _existing_for_task(self, kind: str, task_id: str, title: str) -> Node | None:
        """Return a node this same task already wrote through a tool.

        Workers can both call ``add_fact``/``add_finding`` and return the final
        WORKER_SCHEMA payload. Persisting both would duplicate the graph, so the
        engine treats the tool write as authoritative and reuses it.
        """
        target = _normalise_text(title)
        if not target or not task_id:
            return None
        assert self.project is not None
        try:
            nodes = self.store.list_nodes(self.project.id, kind=kind, limit=5000,
                                          newest_first=True)
        except Exception:
            return None
        for node in nodes:
            if str(node.meta.get("task_id") or "") == str(task_id) \
                    and _normalise_text(node.title) == target:
                return node
        return None

    # ------------------------------------------------------------------
    # todos / hints
    # ------------------------------------------------------------------

    def _apply_planner_todos(self, items: Sequence[Any]) -> None:
        assert self.project is not None
        if not isinstance(items, (list, tuple)) or not items:
            return
        existing = self.store.list_todos(self.project.id)
        by_title = {_normalise_text(todo.title): todo for todo in existing}
        by_id = {todo.id: todo for todo in existing}
        position = max([todo.position for todo in existing] or [0]) + 1
        title_to_id = {_normalise_text(todo.title): todo.id for todo in existing}
        pending_dep_updates: list[tuple[str, list[str]]] = []

        for raw in list(items)[:8]:
            if not isinstance(raw, Mapping):
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            detail = str(raw.get("detail") or "").strip()
            status = str(raw.get("status") or "pending").strip().lower()
            if status not in TODO_STATUSES:
                status = "pending"
            key = _normalise_text(title)
            todo = by_title.get(key)
            if todo is None:
                if len(by_title) >= 8:
                    continue
                todo = Todo.new(self.project.id, position, title, detail,
                                status=status, depends_on=[])
                self.store.add_todo(todo)
                position += 1
                by_title[key] = todo
                by_id[todo.id] = todo
                title_to_id[key] = todo.id
                self._emit(TODO_UPDATED, {"id": todo.id, "title": todo.title,
                                          "status": todo.status})
            else:
                changed = False
                fields: dict[str, Any] = {}
                if detail and detail != todo.detail:
                    fields["detail"] = detail
                if status != todo.status:
                    fields["status"] = status
                if raw.get("intent_id"):
                    fields["intent_id"] = str(raw["intent_id"])
                if fields:
                    todo = self.store.update_todo(todo.id, **fields)
                    by_title[key] = todo
                    changed = True
                if changed:
                    self._emit(TODO_UPDATED, {"id": todo.id, "title": todo.title,
                                              "status": todo.status})
            raw_deps = [str(item) for item in (raw.get("depends_on") or []) if str(item).strip()]
            if raw_deps:
                pending_dep_updates.append((todo.id, raw_deps))

        for todo_id, raw_deps in pending_dep_updates:
            resolved: list[str] = []
            for dep in raw_deps:
                target = by_id.get(dep) or by_title.get(_normalise_text(dep))
                dep_id = target.id if target is not None else title_to_id.get(_normalise_text(dep))
                if dep_id and dep_id != todo_id and dep_id not in resolved:
                    resolved.append(dep_id)
            try:
                current = self.store.get_todo(todo_id)
                if resolved and resolved != current.depends_on:
                    self.store.update_todo(todo_id, depends_on=resolved)
                    self._emit(TODO_UPDATED, {"id": todo_id, "title": current.title,
                                              "status": current.status,
                                              "depends_on": resolved})
            except Exception:
                continue

    def _complete_linked_todo(self, intent: Node) -> None:
        todo_id = str(intent.meta.get("todo_id") or "").strip()
        if not todo_id:
            return
        try:
            todo = self.store.get_todo(todo_id)
            if todo.status != "done":
                updated = self.store.update_todo(todo_id, status="done", intent_id=intent.id)
                self._emit(TODO_UPDATED, {"id": updated.id, "title": updated.title,
                                          "status": updated.status})
        except Exception:
            pass

    def _open_hints(self) -> list[Node]:
        assert self.project is not None
        try:
            return [node for node in self.store.list_nodes(
                self.project.id, kind="hint", status="open", limit=50)]
        except Exception:
            return []

    def _consume_hints(self, hints: Sequence[Node]) -> None:
        for hint in hints:
            try:
                self.store.update_node(hint.id, status="done")
                self._emit(HINT, {"id": hint.id, "consumed": True, "content": hint.body})
            except Exception:
                continue

    # ------------------------------------------------------------------
    # delegation / approvals
    # ------------------------------------------------------------------

    def _ctx_for(self, task: Task, agent: str, role: str) -> ToolContext:
        assert self.project is not None and self.scope is not None
        assert self.sandbox is not None and self.workdir is not None and self.budget is not None
        return ToolContext(
            project=self.project, run_id=self.run_record.id if self.run_record else "", task_id=task.id,
            agent=agent, role=role, store=self.store, config=self.config,
            events=self.events, scope=self.scope, sandbox=self.sandbox,
            workdir=self.workdir, budget=self.budget, cancel=self.cancel,
            delegate=self._delegate_for(task.id),
            request_approval=self._approval_for(task.id),
        )

    def _delegate_for(self, task_id: str) -> Callable[..., str]:
        def delegate(name: str, question: str, context: str = "") -> str:
            assert self.budget is not None and self.project is not None
            depth = int(getattr(self._delegate_state, "depth", 0))
            max_depth = max(0, int(self.config.budget.max_specialist_depth or 0))
            if depth >= max_depth:
                return "delegation refused: specialist depth exhausted"
            if self.cancel.is_set():
                return "delegation cancelled: run is stopping"
            self._delegate_state.depth = depth + 1
            try:
                self.budget.check_llm()
                spec_context = context or render_context(
                    self.store, self.project.id, goal=self.goal, scope=self._scope_dict(),
                    query=question, max_chars=DEFAULT_CONTEXT_CHARS)
                ctx = self._ctx_for(Task(id=task_id, run_id=self.run_record.id if self.run_record else "",
                                         project_id=self.project.id, type="specialist"),
                                    f"specialist:{name}", "specialist")
                outcome = self._call_workers(
                    "run_specialist", ctx=ctx, router=self.router, registry=self.registry,
                    name=name, question=question, context=spec_context)
                ok = bool(getattr(outcome, "ok", False))
                text = str(getattr(outcome, "text", "") or "")
                if not ok:
                    return f"specialist '{name}' failed: {getattr(outcome, 'error', '') or 'no answer'}"
                return text or "(specialist returned no text)"
            except Exception as exc:
                return f"specialist '{name}' unavailable: {type(exc).__name__}: {exc}"
            finally:
                self._delegate_state.depth = depth

        return delegate

    def _approval_for(self, task_id: str) -> Callable[[str, dict[str, Any], str, str], bool]:
        def request_approval(tool: str, args: Mapping[str, Any], risk: str, reason: str) -> bool:
            mode = (self.config.safety.approval or "auto").lower()
            if mode == "auto":
                return True
            if mode == "deny":
                return False
            assert self.project is not None
            approval_id = self.store.request_approval(
                self.project.id, self.run_record.id if self.run_record else "", task_id, tool,
                dict(args or {}), risk=risk, reason=reason)
            self._emit(APPROVAL_REQUESTED, {"id": approval_id, "tool": tool, "risk": risk,
                                            "reason": reason}, task_id=task_id)
            deadline = time.monotonic() + 300.0
            while time.monotonic() < deadline and not self.cancel.is_set():
                record = self.store.get_approval(approval_id) or {}
                status = str(record.get("status") or "")
                if status != "pending":
                    approved = status == "approved"
                    self._emit(APPROVAL_DECIDED, {"id": approval_id, "approved": approved,
                                                  "reason": record.get("reason", "")})
                    return approved
                time.sleep(0.2)
            return False

        return request_approval

    # ------------------------------------------------------------------
    # worker imports / inline tasks
    # ------------------------------------------------------------------

    def _call_workers(self, name: str, **kwargs: Any) -> Any:
        module = self._workers_override or importlib.import_module("aria_x.agents.workers")
        function = getattr(module, name, None)
        if function is None:
            raise AriaXError(f"agents.workers.{name} is not available")
        return function(**kwargs)

    def _new_task(self, type_: str, agent: str, *, intent_id: str | None = None,
                  meta: Mapping[str, Any] | None = None) -> Task:
        assert self.project is not None and self.run_record is not None
        task = Task(id=new_id("task"), run_id=self.run_record.id, project_id=self.project.id,
                    type=type_, status="queued", intent_id=intent_id, agent=agent,
                    meta=dict(meta or {}))
        self.store.create_task(task)
        return task

    def _start_task(self, type_: str, agent: str, *,
                    meta: Mapping[str, Any] | None = None) -> Task:
        task = self._new_task(type_, agent, meta=meta)
        self.store.update_task(task.id, status="running", started_at=now_iso(), attempts=1)
        self._emit(TASK_STARTED, {"type": type_, "agent": agent, "intent_id": task.intent_id},
                   task_id=task.id)
        return task

    def _finish_task(self, task: Task, *, ok: bool, error: str = "",
                     meta: Mapping[str, Any] | None = None) -> None:
        fields: dict[str, Any] = {
            "status": "done" if ok else "failed", "ended_at": now_iso(), "error": error}
        merged = dict(task.meta or {})
        merged.update(dict(meta or {}))
        fields["meta"] = merged
        try:
            self.store.update_task(task.id, **fields)
        except Exception:
            pass
        event = TASK_FINISHED if ok else TASK_FAILED
        self._emit(event, {"type": task.type, "status": fields["status"], "error": error},
                   task_id=task.id)

    # ------------------------------------------------------------------
    # finalisation / report
    # ------------------------------------------------------------------

    def _close_open_todos(self, final_status: str) -> None:
        """Close the plan when the engagement ends.

        A todo chain is a plan, not an obligation: once the goal is proven (or
        the run ends), leaving items "active" would misrepresent the graph. The
        final status records why they were closed.
        """
        if self.project is None:
            return
        try:
            todos = self.store.list_todos(self.project.id)
        except Exception:
            return
        for todo in todos:
            if todo.status in {"pending", "active"}:
                try:
                    updated = self.store.update_todo(todo.id, status=final_status)
                    self._emit(TODO_UPDATED, {"id": updated.id, "title": updated.title,
                                              "status": updated.status,
                                              "reason": "engagement finished"})
                except Exception:
                    continue

    def _finalize(self, status: str, summary: str, error: str) -> RunOutcome:
        assert self.run_record is not None and self.project is not None and self.budget is not None
        ended = now_iso()
        ok = status == "completed"
        if not summary:
            summary = self._default_summary(status)
        usage = self.budget.usage.to_dict()
        budget_snapshot = self.budget.snapshot()
        try:
            self.store.update_run(
                self.run_record.id, status=status, ended_at=ended, usage=usage,
                budget=budget_snapshot, error=error if not ok else "")
        except Exception:
            pass
        report_task = None
        try:
            report_task = self._start_task("report", "reporter", meta={"status": status})
        except Exception:
            report_task = None
        self._close_open_todos("done" if status == "completed" else "dropped")
        metrics = self._metrics(status)
        report_path, report_paths, report_error = self._render_report(
            status=status, summary=summary, error=error, metrics=metrics)
        if report_task is not None:
            self._finish_task(report_task, ok=bool(report_path), error=report_error)
        if report_error:
            metrics["report_error"] = report_error
            self._emit(ERROR, {"message": f"report generation failed: {report_error}"})
        self._emit(BUDGET, budget_snapshot)
        if report_path:
            self._emit(REPORT_READY, {"paths": report_paths, "markdown": report_path})
        try:
            self.store.set_kv("project", f"last_report:{self.project.id}",
                              {"run_id": self.run_record.id, "path": report_path,
                               "paths": report_paths})
        except Exception:
            pass
        self._emit(RUN_FINISHED, {"status": status, "summary": truncate(summary, 2000),
                                  "error": error, "report_path": report_path,
                                  "metrics": metrics})
        return RunOutcome(ok=ok, project_id=self.project.id, run_id=self.run_record.id,
                          status=status, summary=summary, report_path=report_path,
                          error=error, metrics=metrics)

    def _metrics(self, status: str) -> dict[str, Any]:
        assert self.project is not None and self.run_record is not None and self.budget is not None
        try:
            counts = self.store.counts(self.project.id)
        except Exception:
            counts = {}
        try:
            task_count = len(self.store.list_tasks(self.run_record.id, limit=10000))
        except Exception:
            task_count = 0
        usage = self.budget.usage.to_dict()
        asset_total = sum(int(value or 0) for value in (counts.get("assets") or {}).values())
        metrics: dict[str, Any] = {
            "status": status,
            "rounds": self._rounds,
            "intents_created": self._intents_created,
            "intents_completed": self._intents_completed,
            "intents_failed": self._intents_failed,
            "facts": int((counts.get("nodes") or {}).get("fact", 0)),
            "findings": int((counts.get("nodes") or {}).get("finding", 0)),
            "assets": asset_total,
            "edges": int(counts.get("edges", 0) or 0),
            "tasks": task_count,
            "llm_calls": usage["llm_calls"],
            "tokens": usage["total_tokens"],
            "tool_calls": usage["tool_calls"],
            "elapsed_seconds": round(self.budget.elapsed, 1),
            "budget": self.budget.snapshot(),
        }
        return metrics

    def _default_summary(self, status: str) -> str:
        metrics = self._metrics(status) if self.project and self.run_record and self.budget else {}
        if status == "completed":
            return (f"Engagement completed with {metrics.get('findings', 0)} findings and "
                    f"{metrics.get('facts', 0)} facts after {metrics.get('rounds', 0)} rounds.")
        if status == "budget":
            return "Engagement stopped because an execution budget was exhausted."
        if status == "cancelled":
            return "Engagement cancelled by operator."
        return "Engagement failed; see error and report for details."

    def _render_report(self, *, status: str, summary: str, error: str,
                       metrics: Mapping[str, Any]) -> tuple[str, dict[str, str], str]:
        """Render the run report. Never raises: the graph outranks the report."""
        assert self.project is not None and self.run_record is not None
        run_dir = Path(self.config.runs_dir) / self.run_record.id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            export = self.store.export_project(self.project.id)
        except Exception as exc:
            return "", {}, f"{type(exc).__name__}: {exc}"
        meta: dict[str, Any] = {
            "run_id": self.run_record.id,
            "project_id": self.project.id,
            "project_name": self.project.name,
            "goal": self.goal,
            "status": status,
            "summary": summary,
            "error": error,
            "started_at": self.run_record.started_at,
            "ended_at": now_iso(),
            "generated_at": now_iso(),
            "language": self.config.engine.language,
            "scope": dict(self.project.scope or {}),
            "budget": metrics.get("budget") or (self.budget.snapshot() if self.budget else {}),
            "metrics": dict(metrics),
            "counts": self.store.counts(self.project.id),
            "model": self.config.llm.model,
            "provider": self.config.llm.provider,
            "version": "0.1.0",
        }
        report_error = ""
        writer = _load_write_report()
        if writer is not None:
            try:
                paths = writer(run_dir, export, meta)
                if isinstance(paths, Mapping) and paths:
                    normalised = {str(key): str(value) for key, value in paths.items()}
                    selected = (normalised.get("markdown") or normalised.get("html")
                                or next(iter(normalised.values())))
                    return selected, normalised, ""
            except Exception as exc:  # tools-dev renderer failure -> deterministic fallback
                report_error = f"{type(exc).__name__}: {exc}"
        try:
            paths = _fallback_report(run_dir, export, meta)
            selected = paths.get("markdown") or paths.get("html") or ""
            return selected, paths, report_error
        except Exception as exc:
            return "", {}, report_error or f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    def _enforce_budget_ceiling(self) -> None:
        """Raise if a spend ceiling is already reached after this round."""
        if self.budget is None:
            return
        limits = self.config.budget
        usage = self.budget.usage
        if limits.max_llm_calls and usage.llm_calls >= limits.max_llm_calls:
            raise BudgetExceeded(
                f"LLM call budget exhausted ({usage.llm_calls}/{limits.max_llm_calls})",
                dimension="llm_calls")
        if limits.max_tokens and usage.total_tokens >= limits.max_tokens:
            raise BudgetExceeded(
                f"token budget exhausted ({usage.total_tokens}/{limits.max_tokens})",
                dimension="tokens")
        if limits.max_tool_calls and usage.tool_calls >= limits.max_tool_calls:
            raise BudgetExceeded(
                f"tool budget exhausted ({usage.tool_calls}/{limits.max_tool_calls})",
                dimension="tool_calls")

    def _mark_goal_proven(self, summary: str = "") -> None:
        """Record planner-declared completion on the goal node itself."""
        if self.goal_node is None:
            return
        try:
            current = self.store.get_node(self.goal_node.id)
            if current.status != "proven":
                self.store.update_node(self.goal_node.id, status="proven")
                self._emit(NODE_UPDATED, {"id": self.goal_node.id, "kind": "goal",
                                          "status": "proven"})
        except Exception:
            pass
        if summary:
            self._completion_summary = summary

    def _goal_proven(self) -> bool:
        if self.goal_node is None:
            return False
        try:
            return self.store.get_node(self.goal_node.id).status == "proven"
        except Exception:
            return False

    def _has_facts(self) -> bool:
        assert self.project is not None
        try:
            return bool(self.store.list_nodes(self.project.id, kind="fact", limit=1))
        except Exception:
            return False

    def _has_findings(self) -> bool:
        assert self.project is not None
        try:
            return bool(self.store.list_nodes(self.project.id, kind="finding", limit=1))
        except Exception:
            return False

    def _emit(self, event_type: str, data: Mapping[str, Any] | None = None, *,
              task_id: str = "") -> None:
        try:
            self.events.emit(event_type, project_id=self.project.id if self.project else "",
                             run_id=self.run_record.id if self.run_record else "", task_id=task_id,
                             data=dict(data or {}))
        except Exception:
            pass

    def _persist_budget(self) -> None:
        if self.budget is None or self.run_record is None:
            return
        snapshot = self.budget.snapshot()
        try:
            self.store.update_run(self.run_record.id, usage=self.budget.usage.to_dict(),
                                  budget=snapshot)
        except Exception:
            pass
        self._emit(BUDGET, snapshot)

    def _update_run(self, **fields: Any) -> None:
        if self.run_record is None:
            return
        try:
            self.store.update_run(self.run_record.id, **fields)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # test/debug accessors
    # ------------------------------------------------------------------

    @property
    def rounds(self) -> int:
        return self._rounds


# ---------------------------------------------------------------------------
# report fallback (used when tools-dev's renderer is missing or fails)
# ---------------------------------------------------------------------------


def _load_write_report() -> Callable[..., Any] | None:
    try:
        from ..report.render import write_report
        return write_report
    except Exception:
        return None


def _fallback_report(run_dir: Path, export: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, str]:
    """Deterministic Markdown/HTML/JSON report writer.

    This is a safety net, not the primary renderer: the report contract belongs
    to ``aria_x.report.render``. It exists so a run that produced evidence never
    ends without a readable artifact.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    markdown = _fallback_markdown(export, meta)
    markdown_path = run_dir / "report.md"
    html_path = run_dir / "report.html"
    json_path = run_dir / "report.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    title = f"ARIA-X Engagement Report — {meta.get('project_name') or meta.get('project_id') or 'engagement'}"
    html_path.write_text(_markdown_to_html(markdown, title), encoding="utf-8")
    json_path.write_text(json_dumps(export, indent=2), encoding="utf-8")
    return {"markdown": str(markdown_path), "html": str(html_path), "json": str(json_path)}


def _fallback_markdown(export: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
    project = dict(export.get("project") or {})
    findings = list(export.get("findings") or [])
    assets = list(export.get("assets") or [])
    nodes = list(export.get("nodes") or [])
    todos = list(export.get("todos") or [])
    lines: list[str] = [
        f"# ARIA-X Engagement Report — {project.get('name') or meta.get('project_name') or 'engagement'}",
        "",
        f"- **Project:** `{project.get('id') or meta.get('project_id', '')}`",
        f"- **Run:** `{meta.get('run_id', '')}`",
        f"- **Status:** {meta.get('status', '')}",
        f"- **Goal:** {meta.get('goal') or project.get('goal', '')}",
        f"- **Generated:** {meta.get('generated_at', '')}",
        "",
        "## Executive summary",
        "",
        (meta.get("summary") or "(no summary recorded)").strip(),
        "",
    ]
    if meta.get("error"):
        lines += [f"> Run error: {meta.get('error')}", ""]
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda item: (severity_rank.get(str(item.get("meta", {}).get("severity", "info")), 9),
                                    item.get("seq", 0)))
    lines += ["## Findings", ""]
    if not findings:
        lines += ["No findings were recorded.", ""]
    for finding in findings:
        severity = str((finding.get("meta") or {}).get("severity", "info")).upper()
        lines += [f"### [{severity}] {finding.get('title', '')}", "", finding.get("body", "") or "", ""]
    lines += ["## Assets", ""]
    if not assets:
        lines += ["No assets were recorded.", ""]
    for asset in assets:
        parent = asset.get("parent_id")
        suffix = f" (parent `{parent}`)" if parent else ""
        lines.append(f"- **{asset.get('kind')}** `{asset.get('value')}`{suffix}")
    if assets:
        lines.append("")
    facts = [node for node in nodes if node.get("kind") == "fact"]
    lines += ["## Facts", ""]
    if not facts:
        lines += ["No facts were recorded.", ""]
    for fact in facts:
        lines.append(f"- `{fact.get('id')}` **{fact.get('title', '')}** — {first_line(fact.get('body', ''), 240)}")
    if facts:
        lines.append("")
    lines += ["## Plan", ""]
    if not todos:
        lines += ["No todo chain was recorded.", ""]
    for todo in todos:
        lines.append(f"- #{todo.get('position', 0)} [{todo.get('status', '')}] {todo.get('title', '')}")
    if todos:
        lines.append("")
    metrics = dict(meta.get("metrics") or {})
    lines += ["## Metrics", ""]
    for key in ("rounds", "intents_created", "intents_completed", "intents_failed",
                "facts", "findings", "assets", "llm_calls", "tokens", "tool_calls",
                "elapsed_seconds"):
        if key in metrics:
            lines.append(f"- {key}: {metrics[key]}")
    lines.append("")
    return "\n".join(lines)


def _markdown_to_html(markdown: str, title: str) -> str:
    """Tiny, dependency-free Markdown subset renderer for the fallback report."""
    body: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("### "):
            if in_list:
                body.append("</ul>")
                in_list = False
            body.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_list:
                body.append("</ul>")
                in_list = False
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            if in_list:
                body.append("</ul>")
                in_list = False
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{html.escape(line[2:])}</li>")
        elif line.startswith("> "):
            if in_list:
                body.append("</ul>")
                in_list = False
            body.append(f"<blockquote>{html.escape(line[2:])}</blockquote>")
        elif not line.strip():
            if in_list:
                body.append("</ul>")
                in_list = False
        else:
            if in_list:
                body.append("</ul>")
                in_list = False
            body.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        body.append("</ul>")
    style = ("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
             "max-width:920px;margin:40px auto;padding:0 20px;line-height:1.55;color:#1b1f24}"
             "h1{border-bottom:2px solid #d0d7de;padding-bottom:.3em}"
             "h2{margin-top:1.6em;border-bottom:1px solid #e5e9ee;padding-bottom:.2em}"
             "code{background:#f2f4f7;padding:.1em .3em;border-radius:4px}"
             "li{margin:.25em 0}blockquote{border-left:4px solid #d0d7de;margin:0;"
             "padding:.1em 1em;color:#4b5563}")
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>{style}</style></head>"
            f"<body>{''.join(body)}</body></html>")


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _normalise_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _priority(node: Node) -> int:
    try:
        return int(node.meta.get("priority", 50))
    except (TypeError, ValueError):
        return 50


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["Orchestrator", "RunOutcome"]

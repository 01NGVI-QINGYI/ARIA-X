"""Public engagement API.

``Engagement`` is the single object the CLI, HTTP server and tests use. It owns
the durable store, the event bus, the run registry and the background threads,
while :class:`~aria_x.engine.orchestrator.Orchestrator` owns one run's loop.

Threading model
---------------
* ``run()`` is blocking and uses the caller's thread.
* ``start_background()`` spawns one daemon thread per run; the SQLite store is
  already thread-safe, so both paths share the same connection.
* ``stop()`` flips a per-run ``threading.Event``. The orchestrator checks it at
  every round boundary and worker tool call; ``close()`` joins the threads so a
  shutdown never leaves a worker writing to a closed store.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..config import AriaXConfig, ScopeConfig
from ..errors import ConfigError
from ..events import EventBus, RUN_PAUSED
from ..models import Project, Run
from ..store import Store
from ..util import new_id, now_iso
from .orchestrator import Orchestrator, RunOutcome


@dataclass(slots=True)
class EngagementResult:
    """Result of one engagement run (blocking or background)."""

    ok: bool
    project_id: str = ""
    run_id: str = ""
    status: str = ""
    summary: str = ""
    report_path: str = ""
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_outcome(cls, outcome: RunOutcome) -> "EngagementResult":
        return cls(ok=outcome.ok, project_id=outcome.project_id, run_id=outcome.run_id,
                   status=outcome.status, summary=outcome.summary,
                   report_path=outcome.report_path, error=outcome.error,
                   metrics=dict(outcome.metrics or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "project_id": self.project_id, "run_id": self.run_id,
            "status": self.status, "summary": self.summary,
            "report_path": self.report_path, "error": self.error,
            "metrics": self.metrics,
        }


@dataclass(slots=True)
class _RunHandle:
    run_id: str
    project_id: str
    cancel: threading.Event
    thread: threading.Thread | None = None
    result: RunOutcome | None = None
    error: str = ""
    finished_at: float = 0.0

    @property
    def active(self) -> bool:
        if self.thread is None:
            return self.result is None
        return self.thread.is_alive() and self.result is None


class Engagement:
    """One ARIA-X engagement (store + config + optional background runs)."""

    def __init__(self, config: AriaXConfig, *, store: Store | None = None,
                 events: EventBus | None = None, provider: Any = None) -> None:
        self.config = config
        self._owns_store = store is None
        self._owns_events = events is None
        self.store = store or Store(config.db_path)
        self.events = events or EventBus(
            replay=500, log_path=Path(config.runs_dir) / "events.jsonl")
        self.provider = provider
        self._lock = threading.RLock()
        self._runs: dict[str, _RunHandle] = {}
        self._closed = False

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------

    def create_project(self, name: str, goal: str, scope: ScopeConfig | Mapping[str, Any] | None = None) -> Project:
        """Persist a new project. Scope defaults to ``config.scope``.

        A project scope is a *record* of the engagement boundary; the process
        config remains authoritative for tool enforcement (see ``Orchestrator``).
        """
        name = (name or "").strip()
        goal = (goal or "").strip()
        if not name:
            raise ConfigError("project name must not be empty")
        if not goal:
            raise ConfigError("project goal must not be empty")
        scope_dict = self._scope_dict(scope)
        project = Project.new(name, goal, scope=scope_dict, config={
            "engine": asdict(self.config.engine),
            "budget": asdict(self.config.budget),
            "sandbox": {"backend": self.config.sandbox.backend},
            "llm": {"provider": self.config.llm.provider, "model": self.config.llm.model},
        })
        self.store.create_project(project)
        try:
            self.events.log("project created", project_id=project.id,
                            scope=scope_dict)
        except Exception:
            pass
        return project

    def _scope_dict(self, scope: ScopeConfig | Mapping[str, Any] | None) -> dict[str, Any]:
        base = asdict(self.config.scope)
        if scope is None:
            return base
        if isinstance(scope, ScopeConfig):
            return asdict(scope)
        merged = dict(base)
        for key, value in dict(scope).items():
            if key in merged:
                merged[key] = value
        return merged

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------

    def run(self, project_id: str, *, goal: str | None = None) -> EngagementResult:
        """Run the engagement to completion on the calling thread."""
        self._ensure_open()
        project = self.store.get_project(project_id)
        self._assert_authorized(project)
        self._assert_no_active_run(project.id)
        run = self._create_run(project_id)
        handle = _RunHandle(run_id=run.id, project_id=project.id, cancel=threading.Event())
        with self._lock:
            self._runs[run.id] = handle
        try:
            outcome = self._execute(project, run, handle, goal=goal)
        except Exception as exc:  # noqa: BLE001 - boundary converts to a result
            self._mark_failed(run.id, exc)
            outcome = RunOutcome(ok=False, project_id=project.id, run_id=run.id,
                                 status="failed", summary="", report_path="",
                                 error=f"{type(exc).__name__}: {exc}", metrics={})
        finally:
            handle.result = outcome
            handle.finished_at = time.monotonic()
        return EngagementResult.from_outcome(outcome)

    def start_background(self, project_id: str, *, goal: str | None = None) -> str:
        """Start a run on a daemon thread and return its run id immediately."""
        self._ensure_open()
        project = self.store.get_project(project_id)
        self._assert_authorized(project)
        self._assert_no_active_run(project.id)
        run = self._create_run(project_id)
        handle = _RunHandle(run_id=run.id, project_id=project.id, cancel=threading.Event())
        thread = threading.Thread(
            target=self._background_main, args=(project, run, handle, goal),
            name=f"aria-x-run-{run.id}", daemon=True)
        handle.thread = thread
        with self._lock:
            self._runs[run.id] = handle
        thread.start()
        return run.id

    def _background_main(self, project: Project, run: Run, handle: _RunHandle,
                         goal: str | None = None) -> None:
        outcome: RunOutcome | None = None
        try:
            outcome = self._execute(project, run, handle, goal=goal)
        except Exception as exc:  # noqa: BLE001
            handle.error = f"{type(exc).__name__}: {exc}"
            self._mark_failed(run.id, exc)
            outcome = RunOutcome(ok=False, project_id=project.id, run_id=run.id,
                                 status="failed", summary="", report_path="",
                                 error=handle.error, metrics={})
        finally:
            handle.result = outcome
            handle.finished_at = time.monotonic()

    def _execute(self, project: Project, run: Run, handle: _RunHandle, *,
                 goal: str | None) -> RunOutcome:
        orchestrator = Orchestrator(
            self.config, store=self.store, events=self.events,
            provider=self._resolve_provider(), cancel=handle.cancel)
        return orchestrator.run(project, run, goal=goal)

    def _assert_no_active_run(self, project_id: str) -> None:
        """One run per project at a time: two loops would duplicate intents."""
        with self._lock:
            for existing in self._runs.values():
                if existing.project_id == project_id and existing.active:
                    raise ConfigError(
                        f"a run is already active for project {project_id}: {existing.run_id}")

    def _create_run(self, project_id: str) -> Run:
        run = Run(id=new_id("run"), project_id=project_id, status="running",
                  mode="auto", started_at=now_iso())
        self.store.create_run(run)
        return run

    def _mark_failed(self, run_id: str, exc: BaseException) -> None:
        try:
            self.store.update_run(run_id, status="failed", ended_at=now_iso(),
                                  error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        try:
            self.events.emit("run.failed", project_id="", run_id=run_id,
                             data={"error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass

    def _resolve_provider(self) -> Any:
        if isinstance(self.provider, str):
            config = copy.deepcopy(self.config)
            config.llm.provider = self.provider
            from ..llm.router import build_provider
            return build_provider(config.llm)
        return self.provider

    def _assert_authorized(self, project: Project) -> None:
        # Security gate before any run row is created.
        self.config.require_authorization()
        project_scope = dict(project.scope or {})
        if project_scope.get("authorized") is False:
            raise ConfigError("refusing to start: project scope.authorized is false")
        targets = list(project_scope.get("targets") or [])
        if not self.config.scope.targets and not targets:
            raise ConfigError("refusing to start: no scope targets are configured")

    # ------------------------------------------------------------------
    # control + status
    # ------------------------------------------------------------------

    def stop(self, run_id: str | None = None) -> None:
        """Signal one run (or all active runs) to stop at the next safe point."""
        with self._lock:
            handles = ([handle for handle in self._runs.values() if handle.run_id == run_id]
                       if run_id else [handle for handle in self._runs.values() if handle.active])
        for handle in handles:
            if not handle.active:
                continue
            handle.cancel.set()
            try:
                run = self.store.get_run(handle.run_id)
                if run.status in {"created", "running", "paused"}:
                    self.store.update_run(handle.run_id, status="cancelled",
                                          ended_at=now_iso(), error="cancelled by operator")
                    self.events.emit(RUN_PAUSED, project_id=handle.project_id,
                                     run_id=handle.run_id,
                                     data={"message": "stop requested by operator"})
            except Exception:
                continue

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        """Return a JSON-safe status snapshot for one run (or the latest)."""
        self._ensure_open(allow_closed=True)
        if run_id is None:
            run_id = self._latest_run_id()
        if run_id is None:
            return {"run_id": None, "status": "idle", "project_id": "",
                    "tasks": [], "metrics": {}, "report_path": ""}
        handle = self._runs.get(run_id)
        try:
            run = self.store.get_run(run_id)
        except Exception:
            return {"run_id": run_id, "status": "unknown", "project_id": "",
                    "tasks": [], "metrics": {}, "report_path": ""}
        tasks: list[dict[str, Any]] = []
        try:
            for task in self.store.list_tasks(run_id, limit=500):
                tasks.append({
                    "id": task.id, "type": task.type, "status": task.status,
                    "intent_id": task.intent_id, "agent": task.agent,
                    "attempts": task.attempts, "error": task.error,
                    "started_at": task.started_at, "ended_at": task.ended_at,
                    "meta": task.meta,
                })
        except Exception:
            tasks = []
        result = EngagementResult.from_outcome(handle.result) if handle and handle.result else None
        counts: dict[str, Any] = {}
        try:
            counts = self.store.counts(run.project_id)
        except Exception:
            counts = {}
        return {
            "run_id": run.id,
            "project_id": run.project_id,
            "status": run.status,
            "mode": run.mode,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "error": run.error,
            "usage": run.usage,
            "budget": run.budget,
            "tasks": tasks,
            "counts": counts,
            "running": bool(handle and handle.active),
            "stop_requested": bool(handle and handle.cancel.is_set()),
            "result": result.to_dict() if result else None,
            "report_path": result.report_path if result else "",
            "metrics": result.metrics if result else {},
        }

    def _latest_run_id(self) -> str | None:
        with self._lock:
            active = [handle for handle in self._runs.values() if handle.active]
            if active:
                return active[-1].run_id
        try:
            runs = self.store.list_runs(limit=1)
        except Exception:
            runs = []
        if runs:
            return runs[0].id
        with self._lock:
            if self._runs:
                return list(self._runs.values())[-1].run_id
        return None

    def wait(self, run_id: str, *, timeout: float | None = None) -> EngagementResult | None:
        """Block until a background run finishes (helper for CLI/tests)."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        while handle.result is None:
            if deadline is not None and time.monotonic() >= deadline:
                break
            thread = handle.thread
            if thread is None:
                break
            thread.join(0.1)
        if handle.result is None:
            return None
        return EngagementResult.from_outcome(handle.result)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop all runs, join their threads and close owned resources."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = list(self._runs.values())
        for handle in handles:
            handle.cancel.set()
        deadline = time.monotonic() + 10.0
        for handle in handles:
            thread = handle.thread
            if thread is None or thread is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                thread.join(remaining)
        if self._owns_events:
            try:
                self.events.close()
            except Exception:
                pass
        if self._owns_store:
            try:
                self.store.close()
            except Exception:
                pass

    def _ensure_open(self, *, allow_closed: bool = False) -> None:
        if self._closed and not allow_closed:
            raise ConfigError("engagement is closed")

    def __enter__(self) -> "Engagement":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["Engagement", "EngagementResult"]

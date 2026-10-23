"""Bounded task scheduler for exploration rounds.

The orchestrator never runs workers on its own thread. It creates a ``Task``
record (the durable, UI-visible unit of work) and submits it here; this module
owns the thread pool, the per-task status transitions, retries and cancellation
fan-out. Keeping that logic out of the orchestrator makes one property easy to
audit: a task is *always* left in a terminal state (done/failed/cancelled/
skipped) even when a worker raises, because the engagement must never be left
with a running row after the loop stops.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..errors import Cancelled
from ..events import TASK_FAILED, TASK_FINISHED, TASK_STARTED
from ..models import Task
from ..store import Store
from ..util import now_iso


@dataclass(slots=True)
class TaskOutcome:
    """Cooperative result returned by a scheduled callable.

    A callable may return any value (treated as success) or a ``TaskOutcome``
    when it needs to choose the terminal status itself - for example an explore
    job whose intent was already claimed by a sibling is ``skipped``, not
    ``done``.
    """

    ok: bool = True
    value: Any = None
    error: str = ""
    status: str = ""          # explicit override: done | failed | skipped | cancelled
    retryable: bool = False


@dataclass(slots=True)
class ScheduledResult:
    task_id: str
    ok: bool
    value: Any = None
    error: str = ""
    status: str = "done"
    attempts: int = 1
    duration_ms: int = 0
    cancelled: bool = False

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "ok": self.ok, "status": self.status,
                "error": self.error, "attempts": self.attempts,
                "duration_ms": self.duration_ms, "cancelled": self.cancelled}


class TaskScheduler:
    """Run ``Task`` records on a bounded pool with retries and cancellation."""

    def __init__(self, store: Store, events: Any, *, max_concurrency: int = 2,
                 cancel: threading.Event | None = None, retries: int = 0) -> None:
        self.store = store
        self.events = events
        self.max_concurrency = max(1, int(max_concurrency or 1))
        self.cancel = cancel or threading.Event()
        self.retries = max(0, int(retries or 0))
        self._pool = ThreadPoolExecutor(max_workers=self.max_concurrency,
                                        thread_name_prefix="aria-x-task")
        self._active: set[Future[ScheduledResult]] = set()
        self._lock = threading.Lock()
        self._shutdown = False

    # ------------------------------------------------------------------

    def submit(self, task: Task, fn: Callable[[Task], Any], *,
               retries: int | None = None) -> Future[ScheduledResult]:
        """Submit one task; the wrapper always resolves to a ``ScheduledResult``."""
        attempts = (self.retries if retries is None else max(0, int(retries))) + 1
        future = self._pool.submit(self._run_one, task, fn, attempts)
        with self._lock:
            self._active.add(future)
        future.add_done_callback(self._forget)
        return future

    def run(self, jobs: Iterable[tuple[Task, Callable[[Task], Any]]], *,
            retries: int | None = None) -> list[ScheduledResult]:
        """Run a batch to completion and return results in submission order.

        Running threads are never abandoned: cancellation is cooperative, so
        stopping a run marks the event and this method keeps waiting for the
        already-started workers. That guarantees no worker writes to the store
        after the report has been rendered.
        """
        submitted: list[Future[ScheduledResult]] = []
        for task, fn in jobs:
            submitted.append(self.submit(task, fn, retries=retries))
        pending: set[Future[ScheduledResult]] = set(submitted)
        while pending:
            _done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
        # Submission order is the caller's intent order; the wrapper never
        # raises, so resolving in that order is deterministic.
        return [_resolve_future(future) for future in submitted]

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        try:
            self._pool.shutdown(wait=wait, cancel_futures=True)
        except TypeError:  # pragma: no cover - Python < 3.9 compatibility
            self._pool.shutdown(wait=wait)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _forget(self, future: Future[ScheduledResult]) -> None:
        with self._lock:
            self._active.discard(future)

    def _run_one(self, task: Task, fn: Callable[[Task], Any], attempts: int) -> ScheduledResult:
        started = time.monotonic()
        self._update_task(task.id, status="running", started_at=now_iso(),
                          attempts=0, error="")
        self._emit(TASK_STARTED, task, {"type": task.type, "agent": task.agent,
                                        "intent_id": task.intent_id})
        last_error = ""
        used = 0
        for attempt in range(1, attempts + 1):
            used = attempt
            if self.cancel.is_set():
                return self._finish(task, started, used, status="cancelled", ok=False,
                                    error="cancelled before attempt", cancelled=True)
            try:
                value = fn(task)
            except Cancelled as exc:
                return self._finish(task, started, used, status="cancelled", ok=False,
                                    error=str(exc) or "cancelled", cancelled=True)
            except Exception as exc:  # worker bugs are task failures, not run failures
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts and not self.cancel.is_set():
                    time.sleep(min(0.5, 0.05 * attempt))
                    continue
                return self._finish(task, started, used, status="failed", ok=False,
                                    error=last_error)
            if isinstance(value, TaskOutcome):
                status = value.status or ("done" if value.ok else "failed")
                if not value.ok and value.retryable and attempt < attempts and not self.cancel.is_set():
                    last_error = value.error
                    continue
                return self._finish(task, started, used, status=status, ok=value.ok,
                                    value=value.value, error=value.error)
            return self._finish(task, started, used, status="done", ok=True, value=value)
        return self._finish(task, started, used, status="failed", ok=False, error=last_error)

    def _finish(self, task: Task, started: float, attempts: int, *, status: str, ok: bool,
                value: Any = None, error: str = "", cancelled: bool = False) -> ScheduledResult:
        duration_ms = int((time.monotonic() - started) * 1000)
        final_status = status if status in {"done", "failed", "skipped", "cancelled"} else (
            "done" if ok else "failed")
        self._update_task(task.id, status=final_status, attempts=attempts,
                          ended_at=now_iso(), error=error)
        event_type = TASK_FINISHED if ok and final_status not in {"failed", "cancelled"} else TASK_FAILED
        if final_status == "cancelled":
            event_type = TASK_FINISHED
        self._emit(event_type, task, {"type": task.type, "status": final_status,
                                      "attempts": attempts, "error": error,
                                      "duration_ms": duration_ms})
        return ScheduledResult(task_id=task.id, ok=ok, value=value, error=error,
                               status=final_status, attempts=attempts,
                               duration_ms=duration_ms, cancelled=cancelled)

    def _update_task(self, task_id: str, **fields: Any) -> None:
        try:
            self.store.update_task(task_id, **fields)
        except Exception:
            # A task row disappearing must not crash the scheduler.
            pass

    def _emit(self, event_type: str, task: Task, data: dict[str, Any]) -> None:
        try:
            self.events.emit(event_type, project_id=task.project_id, run_id=task.run_id,
                             task_id=task.id, data=data)
        except Exception:
            pass


def _resolve_future(future: Future[ScheduledResult]) -> ScheduledResult:
    try:
        return future.result()
    except Exception as exc:  # pragma: no cover - wrapper should never raise
        return ScheduledResult(task_id="", ok=False, error=f"{type(exc).__name__}: {exc}",
                               status="failed")


__all__ = ["ScheduledResult", "TaskOutcome", "TaskScheduler"]

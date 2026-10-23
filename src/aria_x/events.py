"""Event bus and structured event log.

The bus is the live spine of an engagement: the engine emits domain events,
the HTTP layer streams them over Server-Sent Events, and the JSONL sink keeps an
append-only record for replay and audit.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from .util import json_dumps, new_id, now_iso, redact_obj

# Event type vocabulary. Keep names stable: the web UI and report generators
# match on them.
RUN_STARTED = "run.started"
RUN_FINISHED = "run.finished"
RUN_FAILED = "run.failed"
RUN_PAUSED = "run.paused"
RUN_RESUMED = "run.resumed"
TASK_STARTED = "task.started"
TASK_FINISHED = "task.finished"
TASK_FAILED = "task.failed"
NODE_CREATED = "node.created"
NODE_UPDATED = "node.updated"
ASSET_FOUND = "asset.found"
FINDING_CREATED = "finding.created"
TODO_UPDATED = "todo.updated"
TRACE = "trace"
LOG = "log"
LLM_CALL = "llm.call"
TOOL_CALL = "tool.call"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
BUDGET = "budget"
REPORT_READY = "report.ready"
HINT = "hint"
ERROR = "error"


@dataclass(slots=True)
class Event:
    type: str
    project_id: str = ""
    run_id: str = ""
    task_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("ev", 10))
    ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "ts": self.ts,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "data": self.data,
        }

    def sse(self) -> str:
        return f"id: {self.id}\nevent: {self.type}\ndata: {json_dumps(self.data, ensure_ascii=False)}\n\n"


class EventBus:
    """Thread-safe fan-out bus with a bounded replay buffer."""

    def __init__(self, *, replay: int = 200, log_path: str | Path | None = None,
                 redact: bool = True, echo: bool = False) -> None:
        self._subscribers: list[queue.Queue[Event]] = []
        self._lock = threading.RLock()
        self._recent: list[Event] = []
        self._replay = max(0, replay)
        self._log_path = Path(log_path) if log_path else None
        self._log_lock = threading.Lock()
        self._redact = redact
        self._echo = echo
        self._closed = False
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._load_recent_from_log()

    def _load_recent_from_log(self, limit: int = 300, max_bytes: int = 2_000_000) -> None:
        """Seed the replay buffer from the tail of the JSONL log.

        Without this, a console restarted after an engagement would show an
        empty timeline even though the durable event log is intact.
        """
        assert self._log_path is not None
        try:
            size = self._log_path.stat().st_size
            with self._log_path.open("rb") as fh:
                if size > max_bytes:
                    fh.seek(size - max_bytes)
                    fh.readline()  # drop the partial first line
                raw_lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return
        loaded: list[Event] = []
        for line in raw_lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict) or not payload.get("type"):
                continue
            try:
                loaded.append(Event(
                    type=str(payload["type"]),
                    project_id=str(payload.get("project_id") or ""),
                    run_id=str(payload.get("run_id") or ""),
                    task_id=str(payload.get("task_id") or ""),
                    data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
                    id=str(payload.get("id") or new_id("ev", 10)),
                    ts=str(payload.get("ts") or now_iso()),
                ))
            except Exception:
                continue
        with self._lock:
            self._recent = loaded[-self._replay:] if self._replay else []

    # -- pub/sub ---------------------------------------------------------

    def subscribe(self, maxsize: int = 500) -> "Subscription":
        q: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
            backlog = list(self._recent[-min(len(self._recent), 40):])
        for event in backlog:
            try:
                q.put_nowait(event)
            except queue.Full:
                break
        return Subscription(self, q)

    def unsubscribe(self, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def replay(self, limit: int = 100) -> list[Event]:
        with self._lock:
            return list(self._recent[-max(1, limit):])

    # -- emit ------------------------------------------------------------

    def emit(self, type_: str, *, project_id: str = "", run_id: str = "", task_id: str = "",
             data: Mapping[str, Any] | None = None, **extra: Any) -> Event:
        payload: dict[str, Any] = dict(data or {})
        if extra:
            payload.update(extra)
        if self._redact:
            payload = redact_obj(payload)
        event = Event(type=type_, project_id=project_id, run_id=run_id, task_id=task_id, data=payload)
        with self._lock:
            if self._closed:
                return event
            self._recent.append(event)
            if len(self._recent) > self._replay:
                self._recent = self._recent[-self._replay:]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow consumer: drop the oldest item to keep the stream live.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        self._append_log(event)
        if self._echo:
            print(f"[{event.ts}] {event.type} {json_dumps(event.data, ensure_ascii=False)}")
        return event

    def log(self, message: str, *, level: str = "info", project_id: str = "",
            run_id: str = "", task_id: str = "", **data: Any) -> Event:
        return self.emit(LOG, project_id=project_id, run_id=run_id, task_id=task_id,
                         data={"level": level, "message": message, **data})

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._subscribers.clear()

    def _append_log(self, event: Event) -> None:
        if not self._log_path:
            return
        line = json_dumps(event.to_dict(), ensure_ascii=False) + "\n"
        try:
            with self._log_lock, self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass


class Subscription:
    """Iterator-friendly subscription handle for SSE and tests."""

    def __init__(self, bus: EventBus, q: "queue.Queue[Event]") -> None:
        self.bus = bus
        self.queue = q

    def get(self, timeout: float | None = 30.0) -> Event | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[Event]:
        items: list[Event] = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                return items

    def __iter__(self) -> Iterator[Event]:
        while True:
            event = self.get(timeout=30.0)
            if event is None:
                return
            yield event

    def close(self) -> None:
        self.bus.unsubscribe(self.queue)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class MemoryEventBus(EventBus):
    """Convenience alias used by tests (same behavior, no file sink)."""


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll a predicate until true or timeout (used by tests and CLI waits)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False

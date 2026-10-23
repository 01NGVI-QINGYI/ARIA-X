"""HTTP API handlers for the ARIA-X control plane.

The API is a thin presentation layer: it validates request shapes, calls the
store and the engagement engine, and returns JSON. It deliberately owns no
long-lived state beyond references to those collaborators, so it can be
constructed cheaply by tests and by :func:`aria_x.server.app.serve`.

Two response kinds drive the HTTP layer:

* :class:`Response` — a fully buffered JSON/text answer.
* :class:`StreamResponse` — a lazy chunk iterator used for Server-Sent Events.
  The generator only touches :class:`~aria_x.events.EventBus` queues, never the
  SQLite store, so a slow or disconnected browser cannot hold a database lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

from .. import TAGLINE, __version__
from ..config import AriaXConfig
from ..errors import AriaXError, ConfigError, NotFound
from ..events import APPROVAL_DECIDED, HINT, EventBus
from ..models import Message, Node, Project, Run
from ..store import Store
from ..util import json_dumps, new_id, now_iso

# ---------------------------------------------------------------------------
# response primitives
# ---------------------------------------------------------------------------

JSON_TYPE = "application/json; charset=utf-8"
MARKDOWN_TYPE = "text/markdown; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"


@dataclass(slots=True)
class Response:
    """A buffered HTTP response produced by an API handler."""

    status: int = 200
    body: bytes = b""
    content_type: str = JSON_TYPE
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, payload: Any, status: int = 200,
             headers: Mapping[str, str] | None = None) -> "Response":
        body = json_dumps(payload, ensure_ascii=False).encode("utf-8")
        return cls(status=status, body=body, content_type=JSON_TYPE,
                   headers=dict(headers or {}))

    @classmethod
    def text(cls, body: str, *, content_type: str = "text/plain; charset=utf-8",
             status: int = 200, headers: Mapping[str, str] | None = None) -> "Response":
        return cls(status=status, body=body.encode("utf-8"), content_type=content_type,
                   headers=dict(headers or {}))

    @classmethod
    def bytes(cls, body: bytes, *, content_type: str = "application/octet-stream",
              status: int = 200, headers: Mapping[str, str] | None = None) -> "Response":
        return cls(status=status, body=body, content_type=content_type,
                   headers=dict(headers or {}))

    @classmethod
    def error(cls, code: str, message: str, status: int = 400) -> "Response":
        return cls.json({"error": code, "message": message}, status=status)


@dataclass(slots=True)
class StreamResponse:
    """A streaming response, used for ``text/event-stream``."""

    chunks: Iterator[bytes]
    status: int = 200
    content_type: str = "text/event-stream; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


# HTTP status mapping for the shared error hierarchy.
_STATUS_BY_CODE: dict[str, int] = {
    "aria-x.not_found": 404,
    "aria-x.config": 400,
    "aria-x.store": 500,
    "aria-x.scope.violation": 403,
    "aria-x.approval.denied": 403,
    "aria-x.budget.exceeded": 429,
    "aria-x.cancelled": 409,
    "aria-x.sandbox": 502,
    "aria-x.tool": 400,
    "aria-x.llm": 502,
    "aria-x.agent": 500,
    "aria-x.protocol": 502,
}


def response_for_error(exc: BaseException) -> Response:
    """Translate an exception into the documented JSON error envelope."""
    if isinstance(exc, AriaXError):
        status = _STATUS_BY_CODE.get(exc.code, 500)
        return Response.error(exc.code, exc.message, status)
    if isinstance(exc, RequestError):
        return Response.error(exc.code, exc.message, exc.status)
    return Response.error("internal_error", "internal server error", 500)


class RequestError(Exception):
    """A malformed or unsupported request (400/405/413/415)."""

    def __init__(self, message: str, *, status: int = 400, code: str = "bad_request",
                 headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.headers = dict(headers or {})


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _first(query: Mapping[str, Sequence[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    if not values:
        return default
    return str(values[0])


def _int_param(query: Mapping[str, Sequence[str]], name: str, default: int,
               low: int = 0, high: int = 100_000) -> int:
    raw = _first(query, name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RequestError(f"query parameter '{name}' must be an integer") from None
    return max(low, min(high, value))


def _body_str(body: Mapping[str, Any], key: str, *, required: bool = False,
              max_len: int = 20_000) -> str:
    value = body.get(key)
    if value is None:
        if required:
            raise RequestError(f"field '{key}' is required")
        return ""
    if not isinstance(value, str):
        raise RequestError(f"field '{key}' must be a string")
    value = value.strip()
    if required and not value:
        raise RequestError(f"field '{key}' must not be empty")
    if len(value) > max_len:
        value = value[:max_len]
    return value


def _body_scope(body: Mapping[str, Any], default: Mapping[str, Any]) -> dict[str, Any]:
    raw = body.get("scope")
    if raw is None:
        return dict(default)
    if not isinstance(raw, Mapping):
        raise RequestError("field 'scope' must be an object")
    return {str(k): v for k, v in raw.items()}


# ---------------------------------------------------------------------------
# API implementation
# ---------------------------------------------------------------------------


class Api:
    """Routes and implements every ``/api`` endpoint in docs/CONTRACTS.md §5."""

    def __init__(self, config: AriaXConfig, engagement: Any, *, store: Store | None = None,
                 bus: EventBus | None = None) -> None:
        self.config = config
        self.engagement = engagement
        # The contract allows passing a store explicitly; otherwise the engine's
        # store is authoritative (there is exactly one database per engagement).
        self.store: Store | None = store if store is not None else getattr(engagement, "store", None)
        if self.store is None:
            raise ValueError("Api requires a Store (pass store= or attach one to the engagement)")
        self.bus: EventBus | None = bus if bus is not None else (
            getattr(engagement, "events", None) or getattr(engagement, "bus", None))
        if self.bus is None:
            raise ValueError("Api requires an EventBus (pass bus= or attach one to the engagement)")
        #: Heartbeat cadence for SSE; exposed so tests can shorten it.
        self.heartbeat_seconds: float = 15.0
        self._routes = self._build_routes()

    # -- routing ---------------------------------------------------------

    def _build_routes(self) -> list[tuple[str, re.Pattern[str], Callable[..., Response | StreamResponse]]]:
        return [
            ("GET", re.compile(r"/api/health"), self.health),
            ("GET", re.compile(r"/api/config"), self.get_config),
            ("GET", re.compile(r"/api/projects"), self.list_projects),
            ("POST", re.compile(r"/api/projects"), self.create_project),
            ("GET", re.compile(r"/api/projects/(?P<project_id>[^/]+)"), self.get_project),
            ("GET", re.compile(r"/api/projects/(?P<project_id>[^/]+)/graph"), self.get_graph),
            ("GET", re.compile(r"/api/projects/(?P<project_id>[^/]+)/traces"), self.get_traces),
            ("GET", re.compile(r"/api/projects/(?P<project_id>[^/]+)/audit"), self.get_audit),
            ("GET", re.compile(r"/api/projects/(?P<project_id>[^/]+)/report"), self.get_project_report),
            ("POST", re.compile(r"/api/projects/(?P<project_id>[^/]+)/runs"), self.start_run),
            ("POST", re.compile(r"/api/projects/(?P<project_id>[^/]+)/hints"), self.add_hint),
            ("GET", re.compile(r"/api/projects/(?P<project_id>[^/]+)/events"), self.stream_events),
            ("GET", re.compile(r"/api/runs/(?P<run_id>[^/]+)"), self.get_run),
            ("POST", re.compile(r"/api/runs/(?P<run_id>[^/]+)/stop"), self.stop_run),
            ("GET", re.compile(r"/api/approvals"), self.list_approvals),
            ("POST", re.compile(r"/api/approvals/(?P<approval_id>[^/]+)"), self.decide_approval),
            ("GET", re.compile(r"/api/report/(?P<run_id>[^/]+)"), self.get_run_report),
            ("GET", re.compile(r"/api/export/(?P<project_id>[^/]+)"), self.export_project),
        ]

    def dispatch(self, method: str, path: str, query: Mapping[str, Sequence[str]],
                 body: Mapping[str, Any] | None = None) -> Response | StreamResponse:
        """Route one request; raises :class:`RequestError` for malformed input."""
        path = path.rstrip("/") or "/"
        allowed: list[str] = []
        for route_method, pattern, handler in self._routes:
            match = pattern.fullmatch(path)
            if not match:
                continue
            if route_method != method:
                allowed.append(route_method)
                continue
            kwargs = {k: unquote(v) for k, v in match.groupdict().items()}
            return handler(query=query, body=dict(body or {}), **kwargs)
        if allowed:
            allowed_sorted = sorted(set(allowed) | ({"HEAD"} if "GET" in allowed else set()))
            raise RequestError(f"method {method} not allowed for {path}", status=405,
                               code="method_not_allowed",
                               headers={"Allow": ", ".join(allowed_sorted)}) from None
        raise NotFound(f"unknown route: {method} {path}")

    # -- health / config -------------------------------------------------

    def health(self, **_kwargs: Any) -> Response:
        return Response.json({"ok": True, "version": __version__, "time": now_iso()})

    def get_config(self, **_kwargs: Any) -> Response:
        data = self.config.public_dict()
        data["version"] = __version__
        data["tagline"] = TAGLINE
        data["tools"] = self.tool_catalog()
        data["scope_summary"] = self.scope_summary()
        data["auth_required"] = bool(self.config.server.auth_token)
        return Response.json(data)

    def tool_catalog(self) -> list[dict[str, Any]]:
        """Best-effort tool catalog.

        The tool layer is owned by another module and may expose its catalog as
        a function, a list of specs, or neither at import time. Fall back to the
        frozen contract catalog so the console always has something useful.
        """
        try:  # pragma: no cover - depends on tools-dev timing
            from ..tools import builtin as builtin_module
        except Exception:
            return _CONTRACT_TOOL_CATALOG
        for name in ("tool_catalog", "catalog", "TOOL_CATALOG", "TOOL_SPECS", "SPECS"):
            value = getattr(builtin_module, name, None)
            if value is None:
                continue
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    continue
                except Exception:
                    continue
            if not isinstance(value, (list, tuple)):
                continue
            out: list[dict[str, Any]] = []
            for item in value:
                if hasattr(item, "to_dict"):
                    out.append(item.to_dict())
                elif isinstance(item, Mapping):
                    out.append(dict(item))
            if out:
                return out
        return _CONTRACT_TOOL_CATALOG

    def scope_summary(self) -> dict[str, Any]:
        try:  # pragma: no cover - scope layer is small but optional for tests
            from ..tools.scope import Scope
            return Scope(self.config.scope).summary()
        except Exception:
            cfg = self.config.scope
            return {
                "authorized": cfg.authorized,
                "targets": list(cfg.targets),
                "deny": list(cfg.deny),
                "allow_private": cfg.allow_private,
                "allow_loopback": cfg.allow_loopback,
                "max_rps": cfg.max_rps,
            }

    # -- projects --------------------------------------------------------

    def list_projects(self, **_kwargs: Any) -> Response:
        projects = self.store.list_projects(limit=200)  # type: ignore[union-attr]
        return Response.json([p.to_dict() for p in projects])

    def create_project(self, *, body: Mapping[str, Any], **_kwargs: Any) -> Response:
        name = _body_str(body, "name", required=True, max_len=300)
        goal = _body_str(body, "goal", required=True, max_len=20_000)
        scope = _body_scope(body, _scope_to_dict(self.config.scope))
        creator = getattr(self.engagement, "create_project", None)
        if callable(creator):
            project = creator(name, goal, scope=scope)
        else:  # pragma: no cover - FakeEngagement always implements this
            project = Project.new(name, goal, scope=scope)
            self.store.create_project(project)  # type: ignore[union-attr]
        if not isinstance(project, Project):
            project = self.store.get_project(getattr(project, "id", str(project)))  # type: ignore[union-attr]
        try:
            self.store.audit(project.id, "human", "project.create", "allowed",  # type: ignore[union-attr]
                             target=project.id, detail={"name": name})
        except Exception:
            pass
        return Response.json(project.to_dict())

    def get_project(self, *, project_id: str, **_kwargs: Any) -> Response:
        project = self.store.get_project(project_id)  # type: ignore[union-attr]
        payload = {
            "project": project.to_dict(),
            "counts": self.store.counts(project_id),  # type: ignore[union-attr]
            "runs": [r.to_dict() for r in self.store.list_runs(project_id, limit=50)],  # type: ignore[union-attr]
            "findings": [n.to_dict() for n in self.store.list_nodes(  # type: ignore[union-attr]
                project_id, kind="finding", limit=500, newest_first=True)],
            "assets": [a.to_dict() for a in self.store.list_assets(project_id, limit=2000)],  # type: ignore[union-attr]
            "todos": [t.to_dict() for t in self.store.list_todos(project_id)],  # type: ignore[union-attr]
            "messages": [m.to_dict() for m in self.store.list_messages(project_id, limit=100)],  # type: ignore[union-attr]
        }
        return Response.json(payload)

    def get_graph(self, *, project_id: str, query: Mapping[str, Sequence[str]], **_kwargs: Any) -> Response:
        self.store.get_project(project_id)  # type: ignore[union-attr]
        limit = _int_param(query, "limit", 120, 1, 400)
        return Response.json(self.store.graph_overview(project_id, node_limit=limit))  # type: ignore[union-attr]

    # -- traces / audit / export ----------------------------------------

    def get_traces(self, *, project_id: str, query: Mapping[str, Sequence[str]],
                   **_kwargs: Any) -> Response:
        self.store.get_project(project_id)  # type: ignore[union-attr]
        task_id = _first(query, "task_id") or None
        limit = _int_param(query, "limit", 500, 1, 5000)
        traces = self.store.list_traces(project_id, task_id=task_id, limit=limit)  # type: ignore[union-attr]
        return Response.json({"project_id": project_id, "traces": [t.to_dict() for t in traces],
                              "count": len(traces)})

    def get_audit(self, *, project_id: str, query: Mapping[str, Sequence[str]],
                  **_kwargs: Any) -> Response:
        self.store.get_project(project_id)  # type: ignore[union-attr]
        limit = _int_param(query, "limit", 500, 1, 5000)
        entries = self.store.list_audit(project_id, limit=limit)  # type: ignore[union-attr]
        return Response.json({"project_id": project_id, "audit": [a.to_dict() for a in entries],
                              "count": len(entries)})

    def export_project(self, *, project_id: str, **_kwargs: Any) -> Response:
        data = self.store.export_project(project_id)  # type: ignore[union-attr]
        return Response.json(data)

    # -- runs ------------------------------------------------------------

    def start_run(self, *, project_id: str, body: Mapping[str, Any],
                  **_kwargs: Any) -> Response:
        project = self.store.get_project(project_id)  # type: ignore[union-attr]
        goal = _body_str(body, "goal", max_len=20_000) or None
        starter = getattr(self.engagement, "start_background", None)
        if not callable(starter):
            raise ConfigError("this engagement cannot run background jobs")
        run_id = starter(project.id, goal=goal) if goal else starter(project.id)
        if not isinstance(run_id, str):
            run_id = str(getattr(run_id, "id", "") or run_id)
        try:
            self.store.audit(project.id, "human", "run.start", "allowed",  # type: ignore[union-attr]
                             target=run_id, detail={"goal": (goal or project.goal)[:500]})
        except Exception:
            pass
        return Response.json({"run_id": run_id, "project_id": project.id, "ok": True})

    def get_run(self, *, run_id: str, **_kwargs: Any) -> Response:
        run = self.store.get_run(run_id)  # type: ignore[union-attr]
        project = self.store.find_project(run.project_id)  # type: ignore[union-attr]
        payload: dict[str, Any] = {
            "run": run.to_dict(),
            "project": project.to_dict() if project else None,
            "tasks": [t.to_dict() for t in self.store.list_tasks(run_id)],  # type: ignore[union-attr]
            "budget": run.budget or {},
            "usage": run.usage or {},
        }
        status_fn = getattr(self.engagement, "status", None)
        if callable(status_fn):
            try:
                engine_status = status_fn(run_id)
            except Exception:
                engine_status = {}
            if isinstance(engine_status, Mapping):
                payload["engine"] = dict(engine_status)
                if not payload["budget"] and isinstance(engine_status.get("budget"), Mapping):
                    payload["budget"] = dict(engine_status["budget"])
        return Response.json(payload)

    def stop_run(self, *, run_id: str, **_kwargs: Any) -> Response:
        run = self.store.get_run(run_id)  # type: ignore[union-attr]
        stopper = getattr(self.engagement, "stop", None)
        if callable(stopper):
            try:
                stopper(run_id)
            except TypeError:  # pragma: no cover - older zero-argument engines
                stopper()
            try:
                self.store.audit(run.project_id, "human", "run.stop", "allowed",  # type: ignore[union-attr]
                                 target=run_id)
            except Exception:
                pass
        return Response.json({"ok": True, "run_id": run_id})

    # -- hints / approvals -----------------------------------------------

    def add_hint(self, *, project_id: str, body: Mapping[str, Any],
                 **_kwargs: Any) -> Response:
        project = self.store.get_project(project_id)  # type: ignore[union-attr]
        content = _body_str(body, "content", required=True, max_len=20_000)
        title = (content.splitlines()[0] if content.splitlines() else content)[:120] or "human hint"
        node = Node.new(project.id, "hint", title, content, meta={"source": "human"})
        self.store.add_node(node)  # type: ignore[union-attr]
        message = Message(id=new_id("m"), project_id=project.id, role="user", content=content,
                          created_at=now_iso(), meta={"kind": "hint", "node_id": node.id})
        self.store.add_message(message)  # type: ignore[union-attr]
        try:
            self.store.audit(project.id, "human", "hint", "accepted",  # type: ignore[union-attr]
                             target=node.id, detail={"length": len(content)})
        except Exception:
            pass
        assert self.bus is not None
        self.bus.emit(HINT, project_id=project.id,
                      data={"node_id": node.id, "message_id": message.id,
                            "title": title, "content": content[:2000]})
        return Response.json({"ok": True, "node_id": node.id, "message_id": message.id})

    def list_approvals(self, *, query: Mapping[str, Sequence[str]],
                       **_kwargs: Any) -> Response:
        project_id = _first(query, "project_id")
        status_filter = _first(query, "status", "pending") or "pending"
        if status_filter not in {"pending", "approved", "denied", "all"}:
            raise RequestError("status must be one of pending, approved, denied, all")
        status = None if status_filter == "all" else status_filter
        if project_id:
            self.store.get_project(project_id)  # type: ignore[union-attr]
            approvals = self.store.list_approvals(project_id, status=status)  # type: ignore[union-attr]
        else:  # aggregate across projects so the console can poll one endpoint
            approvals = []
            for project in self.store.list_projects(limit=200):  # type: ignore[union-attr]
                approvals.extend(self.store.list_approvals(project.id, status=status))  # type: ignore[union-attr]
            approvals.sort(key=lambda item: str(item.get("created_at") or ""))
        return Response.json(approvals)

    def decide_approval(self, *, approval_id: str, body: Mapping[str, Any],
                        **_kwargs: Any) -> Response:
        approval = self.store.get_approval(approval_id)  # type: ignore[union-attr]
        if approval is None:
            raise NotFound(f"approval not found: {approval_id}")
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise RequestError("field 'approved' must be a boolean")
        reason = _body_str(body, "reason", max_len=2000)
        self.store.decide_approval(approval_id, approved, reason)  # type: ignore[union-attr]
        project_id = str(approval.get("project_id") or "")
        assert self.bus is not None
        self.bus.emit(APPROVAL_DECIDED, project_id=project_id,
                      run_id=str(approval.get("run_id") or ""),
                      task_id=str(approval.get("task_id") or ""),
                      data={"approval_id": approval_id, "approved": approved,
                            "tool": approval.get("tool", ""), "reason": reason})
        try:
            self.store.audit(project_id, "human", "approval",  # type: ignore[union-attr]
                             "approved" if approved else "denied", target=approval_id,
                             detail={"tool": approval.get("tool", ""), "reason": reason})
        except Exception:
            pass
        return Response.json({"ok": True, "approval": self.store.get_approval(approval_id)})  # type: ignore[union-attr]

    # -- reports ---------------------------------------------------------

    def get_project_report(self, *, project_id: str, **_kwargs: Any) -> Response:
        self.store.get_project(project_id)  # type: ignore[union-attr]
        path = self._find_report(project_id=project_id, suffix=".md")
        if path is None:
            raise NotFound(f"no report found for project: {project_id}")
        return Response.text(path.read_text(encoding="utf-8", errors="replace"),
                             content_type=MARKDOWN_TYPE,
                             headers={"Content-Disposition": f'inline; filename="{path.name}"'})

    def get_run_report(self, *, run_id: str, **_kwargs: Any) -> Response:
        self.store.get_run(run_id)  # type: ignore[union-attr]
        path = self._find_report(run_id=run_id, suffix=".html")
        if path is None:
            raise NotFound(f"no report found for run: {run_id}")
        return Response.text(path.read_text(encoding="utf-8", errors="replace"),
                             content_type=HTML_TYPE,
                             headers={"Content-Disposition": f'inline; filename="{path.name}"'})

    def _runs_root(self) -> Path:
        return Path(self.config.runs_dir)

    def _within_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self._runs_root().resolve())
            return True
        except (ValueError, OSError):
            return False

    def _find_report(self, *, run_id: str | None = None, project_id: str | None = None,
                     suffix: str = ".md") -> Path | None:
        """Locate a generated report without trusting stored paths.

        The engine may lay runs out as ``runs/<run_id>/``, ``runs/<project>/<run_id>/``
        or ``runs/<project>-<stamp>/``; all three are accepted, but every resolved
        candidate must remain inside ``config.runs_dir`` (symlinks included).
        """
        root = self._runs_root()
        if not root.is_dir():
            return None
        seen: set[str] = set()
        directories: list[Path] = []

        def add(candidate: Path) -> None:
            try:
                key = str(candidate.resolve())
            except OSError:
                return
            if key in seen or not candidate.is_dir():
                return
            seen.add(key)
            directories.append(candidate)

        if run_id:
            add(root / run_id)
            try:
                run = self.store.get_run(run_id)  # type: ignore[union-attr]
            except Exception:
                run = None
            if run is not None:
                add(root / run.project_id / run_id)
                add(root / f"{run.project_id}-{run_id}")
            try:
                for child in sorted(root.iterdir(), reverse=True):
                    if child.is_dir() and run_id in child.name:
                        add(child)
            except OSError:
                pass
        if project_id:
            try:
                for run in self.store.list_runs(project_id, limit=50):  # type: ignore[union-attr]
                    add(root / run.id)
                    add(root / project_id / run.id)
            except Exception:
                pass
            project_dir = root / project_id
            add(project_dir)
            try:
                for child in sorted(root.iterdir(), reverse=True):
                    if child.is_dir() and child.name.startswith(project_id):
                        add(child)
            except OSError:
                pass
            try:
                for child in sorted(project_dir.iterdir(), reverse=True):
                    if child.is_dir():
                        add(child)
            except OSError:
                pass
        for directory in directories:
            for name in (f"report{suffix}",):
                candidate = directory / name
                if self._within_root(candidate) and candidate.is_file():
                    return candidate
        return None

    # -- SSE -------------------------------------------------------------

    def stream_events(self, *, project_id: str, query: Mapping[str, Sequence[str]],
                      **_kwargs: Any) -> StreamResponse:
        self.store.get_project(project_id)  # type: ignore[union-attr]
        replay = _int_param(query, "replay", 100, 0, 500)
        assert self.bus is not None
        return StreamResponse(self._event_chunks(project_id, replay=replay))

    def _event_chunks(self, project_id: str, *, replay: int = 100,
                      heartbeat_seconds: float | None = None) -> Iterator[bytes]:
        """SSE generator: replay then live events with idle heartbeats.

        Only the bus queue is touched here; the SQLite store is never accessed
        from the streaming thread after the subscription is created.
        """
        if heartbeat_seconds is None:
            heartbeat_seconds = self.heartbeat_seconds
        subscription = self.bus.subscribe(maxsize=500)  # type: ignore[union-attr]
        # The subscription backlog overlaps the replay buffer; ids keep the
        # client from seeing the same event twice at stream start.
        seen: set[str] = set()
        try:
            yield b"retry: 3000\n: connected\n\n"
            if replay:
                for event in self.bus.replay(replay):  # type: ignore[union-attr]
                    if event.project_id and event.project_id != project_id:
                        continue
                    seen.add(event.id)
                    yield _sse_event(event)
            import queue as _queue
            import time as _time
            heartbeat_at = _time.monotonic() + max(1.0, heartbeat_seconds)
            while True:
                try:
                    event = subscription.queue.get(timeout=1.0)
                except _queue.Empty:
                    if _time.monotonic() >= heartbeat_at:
                        yield b": heartbeat\n\n"
                        heartbeat_at = _time.monotonic() + max(1.0, heartbeat_seconds)
                    continue
                if event.project_id and event.project_id != project_id:
                    continue
                if event.id in seen:
                    continue
                seen.add(event.id)
                if len(seen) > 1000:
                    seen = set(list(seen)[-500:])
                yield _sse_event(event)
        finally:
            subscription.close()


def _sse_event(event: Any) -> bytes:
    event_type = str(getattr(event, "type", "message") or "message").replace("\n", " ")
    # Enrich the payload with envelope metadata the browser timeline needs
    # (the SSE ``event:`` field carves types out of onmessage, so data carries
    # the context). setdefault keeps engine-authored keys authoritative.
    payload = dict(getattr(event, "data", {}) or {})
    payload.setdefault("ts", str(getattr(event, "ts", "") or ""))
    run_id = str(getattr(event, "run_id", "") or "")
    task_id = str(getattr(event, "task_id", "") or "")
    if run_id:
        payload.setdefault("run_id", run_id)
    if task_id:
        payload.setdefault("task_id", task_id)
    data = json_dumps(payload, ensure_ascii=False)
    event_id = str(getattr(event, "id", "") or "")
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode("utf-8")


def _scope_to_dict(scope: Any) -> dict[str, Any]:
    return {
        "authorized": bool(getattr(scope, "authorized", False)),
        "targets": list(getattr(scope, "targets", []) or []),
        "deny": list(getattr(scope, "deny", []) or []),
        "allow_private": bool(getattr(scope, "allow_private", True)),
        "allow_loopback": bool(getattr(scope, "allow_loopback", True)),
        "max_rps": getattr(scope, "max_rps", 3.0),
        "ports": list(getattr(scope, "ports", []) or []),
        "max_ports": getattr(scope, "max_ports", 200),
        "notes": getattr(scope, "notes", ""),
    }


#: Fallback tool catalog from CONTRACTS.md §4, used until tools-dev ships
#: `aria_x.tools.builtin` with its own catalog.
_CONTRACT_TOOL_CATALOG: list[dict[str, Any]] = [
    {"name": "terminal_exec", "risk": "high", "requires_scope": True,
     "description": "Run a command through the configured sandbox with policy checks."},
    {"name": "file_read", "risk": "medium", "description": "Read a file inside the run workdir."},
    {"name": "file_write", "risk": "high", "description": "Write a file inside the run workdir."},
    {"name": "file_list", "risk": "low", "description": "List files inside the run workdir."},
    {"name": "http_request", "risk": "medium", "requires_scope": True,
     "description": "Scope-guarded HTTP request with captured evidence."},
    {"name": "dns_lookup", "risk": "low", "requires_scope": True,
     "description": "Scope-guarded A/AAAA/reverse DNS lookup."},
    {"name": "port_scan", "risk": "high", "requires_scope": True,
     "description": "Scope-guarded, rate-limited TCP port scan."},
    {"name": "tls_probe", "risk": "medium", "requires_scope": True,
     "description": "Scope-guarded TLS handshake and certificate inspection."},
    {"name": "web_probe", "risk": "medium", "requires_scope": True,
     "description": "Scope-guarded GET of a small path wordlist."},
    {"name": "web_search", "risk": "low", "description": "External research via DuckDuckGo HTML."},
    {"name": "add_fact", "risk": "medium", "description": "Persist a fact and link it to an intent."},
    {"name": "add_finding", "risk": "medium", "description": "Persist a finding with evidence."},
    {"name": "add_asset", "risk": "low", "description": "Upsert an asset in the shared graph."},
    {"name": "anchor_node", "risk": "low", "description": "Anchor a node to an asset."},
    {"name": "graph_query", "risk": "low", "description": "Query graph views."},
    {"name": "search_traces", "risk": "low", "description": "Search cross-worker traces."},
    {"name": "memory_store", "risk": "low", "description": "Store project memory."},
    {"name": "memory_search", "risk": "low", "description": "Search project memory."},
    {"name": "todo_update", "risk": "low", "description": "Update the shared todo chain."},
    {"name": "prove_goal", "risk": "low", "description": "Signal that the goal is proven."},
    {"name": "delegate_specialist", "risk": "low", "description": "Ask a specialist agent."},
    {"name": "scope_check", "risk": "low", "description": "Return a scope decision."},
]

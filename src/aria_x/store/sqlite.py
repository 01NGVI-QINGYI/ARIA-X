"""SQLite-backed engagement store.

Design notes
------------
* One connection guarded by an ``RLock``. ARIA-X runs a small number of engine
  threads plus the HTTP server; a serialized connection keeps reasoning simple
  and is more than fast enough for graph-sized workloads.
* WAL mode with ``busy_timeout`` for concurrent readers.
* All JSON columns are encoded with :func:`aria_x.util.json_dumps`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..errors import NotFound, StoreError
from ..models import (
    Asset, AuditEntry, Edge, Evidence, Message, Node, Project, Run, Task, Todo, Trace,
)
from ..util import json_dumps, json_loads, new_id, now_iso
from .schema import DDL, SCHEMA_VERSION


class Store:
    """Thread-safe data access object for one ARIA-X database."""

    def __init__(self, path: str | Path = ":memory:", *, echo: bool = False) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._depth = 0
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        except sqlite3.Error as exc:  # pragma: no cover - filesystem dependent
            raise StoreError(f"cannot open database {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.Error:
                pass
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._echo = echo
        self._init_schema()

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(DDL)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                                   (str(SCHEMA_VERSION),))
            self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                if self._depth == 0:
                    self._conn.execute("BEGIN IMMEDIATE")
                self._depth += 1
                yield self._conn
                self._depth -= 1
                if self._depth == 0:
                    self._conn.commit()
            except Exception:
                self._depth = 0
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _log(self, sql: str, params: Sequence[Any] = ()) -> None:
        if self._echo:
            print("[sql]", sql.strip().splitlines()[0], tuple(params))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------

    def create_project(self, project: Project) -> Project:
        with self._tx() as c:
            c.execute(
                "INSERT INTO projects(id,name,goal,scope_json,config_json,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (project.id, project.name, project.goal, json_dumps(project.scope),
                 json_dumps(project.config), project.status, project.created_at, project.updated_at),
            )
        return project

    def get_project(self, project_id: str) -> Project:
        p = Project.from_row(self._one("SELECT * FROM projects WHERE id=?", (project_id,)))
        if p is None:
            raise NotFound(f"project not found: {project_id}")
        return p

    def find_project(self, project_id: str) -> Project | None:
        return Project.from_row(self._one("SELECT * FROM projects WHERE id=?", (project_id,)))

    def list_projects(self, limit: int = 100) -> list[Project]:
        rows = self._all("SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,))
        return [p for p in (Project.from_row(r) for r in rows) if p]

    def update_project(self, project_id: str, **fields: Any) -> Project:
        allowed = {"name", "goal", "status"}
        sets, params = _build_sets(fields, allowed)
        if "scope" in fields:
            sets.append("scope_json=?")
            params.append(json_dumps(fields["scope"]))
        if "config" in fields:
            sets.append("config_json=?")
            params.append(json_dumps(fields["config"]))
        sets.append("updated_at=?")
        params.append(now_iso())
        params.append(project_id)
        with self._tx() as c:
            c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", params)
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM projects WHERE id=?", (project_id,))

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------

    def create_run(self, run: Run) -> Run:
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs(id,project_id,status,mode,budget_json,usage_json,error,started_at,ended_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (run.id, run.project_id, run.status, run.mode, json_dumps(run.budget),
                 json_dumps(run.usage), run.error, run.started_at, run.ended_at),
            )
        return run

    def get_run(self, run_id: str) -> Run:
        r = Run.from_row(self._one("SELECT * FROM runs WHERE id=?", (run_id,)))
        if r is None:
            raise NotFound(f"run not found: {run_id}")
        return r

    def update_run(self, run_id: str, **fields: Any) -> Run:
        sets: list[str] = []
        params: list[Any] = []
        for key in ("status", "mode", "error", "started_at", "ended_at"):
            if key in fields:
                sets.append(f"{key}=?")
                params.append(fields[key])
        for key, col in (("budget", "budget_json"), ("usage", "usage_json")):
            if key in fields:
                sets.append(f"{col}=?")
                params.append(json_dumps(fields[key]))
        if not sets:
            return self.get_run(run_id)
        params.append(run_id)
        with self._tx() as c:
            c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", params)
        return self.get_run(run_id)

    def list_runs(self, project_id: str | None = None, limit: int = 50) -> list[Run]:
        if project_id:
            rows = self._all("SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC LIMIT ?",
                             (project_id, limit))
        else:
            rows = self._all("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [r for r in (Run.from_row(x) for x in rows) if r]

    # ------------------------------------------------------------------
    # exploration nodes (facts / intents / findings / hints / goals)
    # ------------------------------------------------------------------

    def add_node(self, node: Node) -> Node:
        with self._tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM nodes WHERE project_id=?",
                            (node.project_id,)).fetchone()[0]
            node.seq = int(seq)
            c.execute(
                "INSERT INTO nodes(id,project_id,kind,title,body,status,parent_id,meta_json,created_at,updated_at,seq)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (node.id, node.project_id, node.kind, node.title, node.body, node.status,
                 node.parent_id, json_dumps(node.meta), node.created_at, node.updated_at, node.seq),
            )
        return node

    def get_node(self, node_id: str) -> Node:
        n = Node.from_row(self._one("SELECT * FROM nodes WHERE id=?", (node_id,)))
        if n is None:
            raise NotFound(f"node not found: {node_id}")
        return n

    def find_node(self, node_id: str) -> Node | None:
        return Node.from_row(self._one("SELECT * FROM nodes WHERE id=?", (node_id,)))

    def update_node(self, node_id: str, **fields: Any) -> Node:
        allowed = {"title", "body", "status", "parent_id", "kind"}
        sets, params = _build_sets(fields, allowed)
        if "meta" in fields:
            sets.append("meta_json=?")
            params.append(json_dumps(fields["meta"]))
        sets.append("updated_at=?")
        params.append(now_iso())
        params.append(node_id)
        with self._tx() as c:
            cur = c.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=?", params)
            if cur.rowcount == 0:
                raise NotFound(f"node not found: {node_id}")
        return self.get_node(node_id)

    def list_nodes(self, project_id: str, *, kind: str | None = None, status: str | None = None,
                   limit: int = 1000, newest_first: bool = False) -> list[Node]:
        sql = "SELECT * FROM nodes WHERE project_id=?"
        params: list[Any] = [project_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY seq " + ("DESC" if newest_first else "ASC") + " LIMIT ?"
        params.append(limit)
        return [n for n in (Node.from_row(r) for r in self._all(sql, params)) if n]

    def add_edge(self, project_id: str, src_id: str, dst_id: str, rel: str) -> Edge:
        edge = Edge(id=new_id("e"), project_id=project_id, src_id=src_id, dst_id=dst_id,
                    rel=rel, created_at=now_iso())
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO edges(id,project_id,src_id,dst_id,rel,created_at)"
                      " VALUES(?,?,?,?,?,?)",
                      (edge.id, project_id, src_id, dst_id, rel, edge.created_at))
        row = self._one("SELECT * FROM edges WHERE project_id=? AND src_id=? AND dst_id=? AND rel=?",
                        (project_id, src_id, dst_id, rel))
        return Edge.from_row(row) or edge

    def list_edges(self, project_id: str, limit: int = 5000) -> list[Edge]:
        rows = self._all("SELECT * FROM edges WHERE project_id=? ORDER BY created_at LIMIT ?",
                         (project_id, limit))
        return [e for e in (Edge.from_row(r) for r in rows) if e]

    def neighbors(self, project_id: str, node_id: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {"out": [], "in": []}
        for r in self._all("SELECT * FROM edges WHERE project_id=? AND src_id=?", (project_id, node_id)):
            out["out"].append({"id": r["dst_id"], "rel": r["rel"]})
        for r in self._all("SELECT * FROM edges WHERE project_id=? AND dst_id=?", (project_id, node_id)):
            out["in"].append({"id": r["src_id"], "rel": r["rel"]})
        return out

    def claim_intent(self, intent_id: str, *, expected_status: str = "open") -> bool:
        """Atomically move an intent from open -> active. Returns False if taken."""
        with self._tx() as c:
            cur = c.execute("UPDATE nodes SET status='active', updated_at=? WHERE id=? AND status=?",
                            (now_iso(), intent_id, expected_status))
            return cur.rowcount == 1

    def add_finding(self, project_id: str, title: str, body: str, *, severity: str = "info",
                    cvss: float | None = None, confidence: str = "medium",
                    asset_ids: Iterable[str] = (), intent_id: str | None = None,
                    meta: Mapping[str, Any] | None = None) -> Node:
        payload = dict(meta or {})
        payload.update({"severity": severity, "confidence": confidence})
        if cvss is not None:
            payload["cvss"] = cvss
        node = Node.new(project_id, "finding", title, body, status="done",
                        parent_id=intent_id, meta=payload)
        self.add_node(node)
        if intent_id:
            self.add_edge(project_id, intent_id, node.id, "proves")
        for asset_id in asset_ids:
            self.anchor(node.id, asset_id)
        return node

    # ------------------------------------------------------------------
    # assets + anchors
    # ------------------------------------------------------------------

    def upsert_asset(self, project_id: str, kind: str, value: str, *,
                     parent_id: str | None = None, meta: Mapping[str, Any] | None = None) -> Asset:
        value = (value or "").strip()
        if not value:
            raise StoreError("asset value must not be empty")
        now = now_iso()
        with self._tx() as c:
            existing = c.execute("SELECT * FROM assets WHERE project_id=? AND kind=? AND value=?",
                                 (project_id, kind, value)).fetchone()
            if existing:
                merged = json_loads(existing["meta_json"], {})
                merged.update(dict(meta or {}))
                c.execute("UPDATE assets SET last_seen=?, meta_json=?, parent_id=COALESCE(?, parent_id)"
                          " WHERE id=?", (now, json_dumps(merged), parent_id, existing["id"]))
                asset_id = existing["id"]
            else:
                asset_id = new_id("a")
                c.execute("INSERT INTO assets(id,project_id,kind,value,parent_id,meta_json,first_seen,last_seen)"
                          " VALUES(?,?,?,?,?,?,?,?)",
                          (asset_id, project_id, kind, value, parent_id,
                           json_dumps(dict(meta or {})), now, now))
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> Asset:
        a = Asset.from_row(self._one("SELECT * FROM assets WHERE id=?", (asset_id,)))
        if a is None:
            raise NotFound(f"asset not found: {asset_id}")
        return a

    def find_asset(self, project_id: str, kind: str, value: str) -> Asset | None:
        return Asset.from_row(self._one(
            "SELECT * FROM assets WHERE project_id=? AND kind=? AND value=?", (project_id, kind, value)))

    def list_assets(self, project_id: str, *, kind: str | None = None, limit: int = 2000) -> list[Asset]:
        if kind:
            rows = self._all("SELECT * FROM assets WHERE project_id=? AND kind=? ORDER BY first_seen LIMIT ?",
                             (project_id, kind, limit))
        else:
            rows = self._all("SELECT * FROM assets WHERE project_id=? ORDER BY first_seen LIMIT ?",
                             (project_id, limit))
        return [a for a in (Asset.from_row(r) for r in rows) if a]

    def anchor(self, node_id: str, asset_id: str, note: str = "") -> None:
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO anchors(node_id,asset_id,note,created_at) VALUES(?,?,?,?)",
                      (node_id, asset_id, note, now_iso()))

    def anchors_for_node(self, node_id: str) -> list[Asset]:
        rows = self._all("SELECT a.* FROM anchors an JOIN assets a ON a.id=an.asset_id WHERE an.node_id=?",
                         (node_id,))
        return [a for a in (Asset.from_row(r) for r in rows) if a]

    def nodes_for_asset(self, asset_id: str, limit: int = 200) -> list[Node]:
        rows = self._all("SELECT n.* FROM anchors an JOIN nodes n ON n.id=an.node_id"
                         " WHERE an.asset_id=? ORDER BY n.seq LIMIT ?", (asset_id, limit))
        return [n for n in (Node.from_row(r) for r in rows) if n]

    # ------------------------------------------------------------------
    # todos
    # ------------------------------------------------------------------

    def add_todo(self, todo: Todo) -> Todo:
        with self._tx() as c:
            c.execute("INSERT INTO todos(id,project_id,position,title,detail,status,depends_on_json,"
                      "intent_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (todo.id, todo.project_id, todo.position, todo.title, todo.detail, todo.status,
                       json_dumps(todo.depends_on), todo.intent_id, todo.created_at, todo.updated_at))
        return todo

    def update_todo(self, todo_id: str, **fields: Any) -> Todo:
        allowed = {"title", "detail", "status", "position", "intent_id"}
        sets, params = _build_sets(fields, allowed)
        if "depends_on" in fields:
            sets.append("depends_on_json=?")
            params.append(json_dumps(list(fields["depends_on"])))
        sets.append("updated_at=?")
        params.append(now_iso())
        params.append(todo_id)
        with self._tx() as c:
            cur = c.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id=?", params)
            if cur.rowcount == 0:
                raise NotFound(f"todo not found: {todo_id}")
        return self.get_todo(todo_id)

    def get_todo(self, todo_id: str) -> Todo:
        t = Todo.from_row(self._one("SELECT * FROM todos WHERE id=?", (todo_id,)))
        if t is None:
            raise NotFound(f"todo not found: {todo_id}")
        return t

    def list_todos(self, project_id: str) -> list[Todo]:
        return [t for t in (Todo.from_row(r) for r in
                            self._all("SELECT * FROM todos WHERE project_id=? ORDER BY position, created_at",
                                      (project_id,))) if t]

    def ready_todos(self, project_id: str) -> list[Todo]:
        todos = self.list_todos(project_id)
        done = {t.id for t in todos if t.status == "done"}
        ready = []
        for t in todos:
            if t.status != "pending":
                continue
            if all(dep in done for dep in t.depends_on):
                ready.append(t)
        return ready

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        with self._tx() as c:
            c.execute("INSERT INTO tasks(id,run_id,project_id,type,status,intent_id,agent,attempts,error,"
                      "started_at,ended_at,meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (task.id, task.run_id, task.project_id, task.type, task.status, task.intent_id,
                       task.agent, task.attempts, task.error, task.started_at, task.ended_at,
                       json_dumps(task.meta)))
        return task

    def update_task(self, task_id: str, **fields: Any) -> Task:
        allowed = {"status", "intent_id", "agent", "attempts", "error", "started_at", "ended_at"}
        sets, params = _build_sets(fields, allowed)
        if "meta" in fields:
            sets.append("meta_json=?")
            params.append(json_dumps(fields["meta"]))
        if not sets:
            return self.get_task(task_id)
        params.append(task_id)
        with self._tx() as c:
            cur = c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
            if cur.rowcount == 0:
                raise NotFound(f"task not found: {task_id}")
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        t = Task.from_row(self._one("SELECT * FROM tasks WHERE id=?", (task_id,)))
        if t is None:
            raise NotFound(f"task not found: {task_id}")
        return t

    def list_tasks(self, run_id: str, limit: int = 500) -> list[Task]:
        return [t for t in (Task.from_row(r) for r in
                            self._all("SELECT * FROM tasks WHERE run_id=? ORDER BY started_at LIMIT ?",
                                      (run_id, limit))) if t]

    # ------------------------------------------------------------------
    # traces
    # ------------------------------------------------------------------

    def add_trace(self, trace: Trace) -> Trace:
        with self._tx() as c:
            if not trace.seq:
                trace.seq = int(c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM traces WHERE task_id=?",
                                          (trace.task_id,)).fetchone()[0])
            c.execute("INSERT INTO traces(id,project_id,run_id,task_id,agent,seq,kind,content,tool_name,"
                      "tool_args_json,tool_result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (trace.id, trace.project_id, trace.run_id, trace.task_id, trace.agent,
                       trace.seq, trace.kind, trace.content, trace.tool_name,
                       json_dumps(trace.tool_args), json_dumps(trace.tool_result), trace.created_at))
        return trace

    def list_traces(self, project_id: str, *, task_id: str | None = None,
                    limit: int = 500, newest_first: bool = False) -> list[Trace]:
        if task_id:
            rows = self._all("SELECT * FROM traces WHERE project_id=? AND task_id=? ORDER BY seq LIMIT ?",
                             (project_id, task_id, limit))
        else:
            order = "DESC" if newest_first else "ASC"
            rows = self._all(f"SELECT * FROM traces WHERE project_id=? ORDER BY created_at {order} LIMIT ?",
                             (project_id, limit))
        return [t for t in (Trace.from_row(r) for r in rows) if t]

    def search_traces(self, project_id: str, query: str, *, exclude_task_id: str | None = None,
                      limit: int = 20) -> list[Trace]:
        """Cross-worker retrieval over raw process traces (ARTEX lineage)."""
        q = f"%{query}%"
        sql = ("SELECT * FROM traces WHERE project_id=? AND (content LIKE ? OR tool_result_json LIKE ?"
               " OR tool_args_json LIKE ? OR tool_name LIKE ?)")
        params: list[Any] = [project_id, q, q, q, q]
        if exclude_task_id:
            sql += " AND task_id<>?"
            params.append(exclude_task_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [t for t in (Trace.from_row(r) for r in self._all(sql, params)) if t]

    # ------------------------------------------------------------------
    # evidence / messages / audit / approvals / kv
    # ------------------------------------------------------------------

    def add_evidence(self, evidence: Evidence) -> Evidence:
        with self._tx() as c:
            c.execute("INSERT INTO evidence(id,project_id,finding_id,kind,path,sha256,excerpt,meta_json,"
                      "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (evidence.id, evidence.project_id, evidence.finding_id, evidence.kind,
                       evidence.path, evidence.sha256, evidence.excerpt,
                       json_dumps(evidence.meta), evidence.created_at))
        return evidence

    def list_evidence(self, finding_id: str) -> list[Evidence]:
        return [e for e in (Evidence.from_row(r) for r in
                            self._all("SELECT * FROM evidence WHERE finding_id=? ORDER BY created_at",
                                      (finding_id,))) if e]

    def add_message(self, message: Message) -> Message:
        with self._tx() as c:
            c.execute("INSERT INTO messages(id,project_id,role,content,created_at,meta_json)"
                      " VALUES(?,?,?,?,?,?)",
                      (message.id, message.project_id, message.role, message.content,
                       message.created_at, json_dumps(message.meta)))
        return message

    def list_messages(self, project_id: str, limit: int = 200) -> list[Message]:
        return [m for m in (Message.from_row(r) for r in
                            self._all("SELECT * FROM messages WHERE project_id=? ORDER BY created_at LIMIT ?",
                                      (project_id, limit))) if m]

    def audit(self, project_id: str, actor: str, action: str, decision: str,
              target: str = "", detail: Mapping[str, Any] | None = None) -> AuditEntry:
        entry = AuditEntry(id=new_id("au"), project_id=project_id, ts=now_iso(), actor=actor,
                           action=action, decision=decision, target=target,
                           detail=dict(detail or {}))
        with self._tx() as c:
            c.execute("INSERT INTO audit(id,project_id,ts,actor,action,decision,target,detail_json)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (entry.id, project_id, entry.ts, actor, action, decision, target,
                       json_dumps(entry.detail)))
        return entry

    def list_audit(self, project_id: str, limit: int = 500) -> list[AuditEntry]:
        return [a for a in (AuditEntry.from_row(r) for r in
                            self._all("SELECT * FROM audit WHERE project_id=? ORDER BY ts DESC LIMIT ?",
                                      (project_id, limit))) if a]

    def request_approval(self, project_id: str, run_id: str, task_id: str, tool: str,
                         args: Mapping[str, Any], risk: str = "medium", reason: str = "") -> str:
        approval_id = new_id("ap")
        with self._tx() as c:
            c.execute("INSERT INTO approvals(id,project_id,run_id,task_id,tool,args_json,risk,status,"
                      "reason,created_at,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (approval_id, project_id, run_id, task_id, tool, json_dumps(dict(args)),
                       risk, "pending", reason, now_iso(), ""))
        return approval_id

    def decide_approval(self, approval_id: str, approved: bool, reason: str = "") -> None:
        with self._tx() as c:
            cur = c.execute("UPDATE approvals SET status=?, reason=?, decided_at=? WHERE id=?",
                            ("approved" if approved else "denied", reason, now_iso(), approval_id))
            if cur.rowcount == 0:
                raise NotFound(f"approval not found: {approval_id}")

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not row:
            return None
        data = dict(row)
        data["args"] = json_loads(data.pop("args_json"), {})
        data["status"] = data["status"]
        return data

    def list_approvals(self, project_id: str, status: str | None = "pending") -> list[dict[str, Any]]:
        sql = "SELECT * FROM approvals WHERE project_id=?"
        params: list[Any] = [project_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at"
        out = []
        for row in self._all(sql, params):
            data = dict(row)
            data["args"] = json_loads(data.pop("args_json"), {})
            out.append(data)
        return out

    def set_kv(self, scope: str, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO kv(scope,key,value_json) VALUES(?,?,?)"
                      " ON CONFLICT(scope,key) DO UPDATE SET value_json=excluded.value_json",
                      (scope, key, json_dumps(value)))

    def get_kv(self, scope: str, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value_json FROM kv WHERE scope=? AND key=?", (scope, key))
        return json_loads(row["value_json"], default) if row else default

    # ------------------------------------------------------------------
    # aggregate views
    # ------------------------------------------------------------------

    def counts(self, project_id: str) -> dict[str, Any]:
        def scalar(sql: str, params: Sequence[Any] = (project_id,)) -> int:
            row = self._one(sql, params)
            return int(row[0]) if row else 0

        by_kind = {r["kind"]: int(r["n"]) for r in self._all(
            "SELECT kind, COUNT(*) AS n FROM nodes WHERE project_id=? GROUP BY kind", (project_id,))}
        by_asset = {r["kind"]: int(r["n"]) for r in self._all(
            "SELECT kind, COUNT(*) AS n FROM assets WHERE project_id=? GROUP BY kind", (project_id,))}
        by_sev = {r["sv"]: int(r["n"]) for r in self._all(
            "SELECT json_extract(meta_json,'$.severity') AS sv, COUNT(*) AS n FROM nodes"
            " WHERE project_id=? AND kind='finding' GROUP BY sv", (project_id,))}
        return {
            "nodes": by_kind,
            "assets": by_asset,
            "findings_by_severity": by_sev,
            "edges": scalar("SELECT COUNT(*) FROM edges WHERE project_id=?"),
            "todos": scalar("SELECT COUNT(*) FROM todos WHERE project_id=?"),
            "traces": scalar("SELECT COUNT(*) FROM traces WHERE project_id=?"),
            "tasks": scalar("SELECT COUNT(*) FROM tasks WHERE project_id=?"),
            "evidence": scalar("SELECT COUNT(*) FROM evidence WHERE project_id=?"),
        }

    def graph_overview(self, project_id: str, *, node_limit: int = 120) -> dict[str, Any]:
        project = self.get_project(project_id)
        nodes = self.list_nodes(project_id, limit=node_limit)
        # Prefer the most decision-relevant nodes when the graph is large.
        if len(nodes) > node_limit:
            priority = {"goal": 0, "intent": 1, "finding": 2, "fact": 3, "hint": 4, "note": 5, "guide": 6}
            nodes = sorted(nodes, key=lambda n: (priority.get(n.kind, 9), -n.seq))[:node_limit]
        assets = self.list_assets(project_id, limit=200)
        edges = self.list_edges(project_id, limit=len(nodes) * 4 + 50)
        node_ids = {n.id for n in nodes}
        edges = [e for e in edges if e.src_id in node_ids or e.dst_id in node_ids]
        return {
            "project": project.to_dict(),
            "counts": self.counts(project_id),
            "nodes": [n.to_dict() for n in nodes],
            "assets": [a.to_dict() for a in assets],
            "edges": [e.to_dict() for e in edges],
            "todos": [t.to_dict() for t in self.list_todos(project_id)],
            "findings": [n.to_dict() for n in self.list_nodes(project_id, kind="finding", limit=200)],
        }

    def export_project(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        return {
            "project": project.to_dict(),
            "nodes": [n.to_dict() for n in self.list_nodes(project_id, limit=100000)],
            "edges": [e.to_dict() for e in self.list_edges(project_id, limit=100000)],
            "assets": [a.to_dict() for a in self.list_assets(project_id, limit=100000)],
            "todos": [t.to_dict() for t in self.list_todos(project_id)],
            "findings": [n.to_dict() for n in self.list_nodes(project_id, kind="finding", limit=100000)],
            "traces": [t.to_dict() for t in self.list_traces(project_id, limit=100000)],
            "evidence": {n.id: [e.to_dict() for e in self.list_evidence(n.id)]
                         for n in self.list_nodes(project_id, kind="finding", limit=100000)},
            "audit": [a.to_dict() for a in self.list_audit(project_id, limit=100000)],
            "runs": [r.to_dict() for r in self.list_runs(project_id, limit=100)],
        }


def _build_sets(fields: Mapping[str, Any], allowed: set[str]) -> tuple[list[str], list[Any]]:
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            params.append(value)
    if not sets:
        # Caller asked to update only unknown fields; keep the statement valid.
        sets.append("id=id")
    return sets, params

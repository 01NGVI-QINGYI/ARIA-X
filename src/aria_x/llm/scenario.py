"""Deterministic scenario provider: drive a whole engagement offline.

``DemoPolicy`` in :mod:`aria_x.demo` scripts one specific engagement in Python.
This provider is the *data-driven* counterpart: a JSON file describes responses
per role or by substring match, so examples, docs and tests can replay a full
reason→explore→report engagement (tool calls included) without a model.

Script format (all keys optional)::

    {
      "default": "text used when nothing else matches",
      "loop": false,
      "roles": {
        "planner": [<entry>, ...],
        "worker":  [<entry>, ...],
        "bootstrap": [...], "specialist": [...], "researcher": [...],
        "reporter": [...]
      },
      "responses": [
        {"match": "substring in the last user message", "content": "...",
         "tool_calls": [{"name": "http_request", "arguments": {"url": "..."}}]}
      ]
    }

An **entry** may be:

* a string → assistant text;
* ``{"content": "..."}`` → assistant text;
* ``{"json": {...}}`` → assistant text containing that JSON object (convenient
  for planner/worker contracts, so demo scripts stay readable);
* ``{"tool_calls": [...]}`` plus optional ``content`` → native tool calls;
* a bare list of tool-call dicts → native tool calls.

Role selection: the router does not pass its ``role`` argument to providers, so
the role is inferred from the rendered task header (``# TASK (reason)``) and,
as a fallback, from the system prompt produced by :mod:`aria_x.agents.roles`.
Per-role queues are consumed in order; ``"loop": true`` cycles them. Matching
``responses`` fire once (so a script can stage "call tool X, then finish") unless
``loop`` is true. A callable policy may be injected and takes precedence; it may
have the signature ``policy(role, messages, tools)`` or the MockProvider-style
``policy(messages, tools)``, and returning ``None`` falls back to the script.
"""

from __future__ import annotations

import inspect
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import LLMConfig
from ..errors import LLMError
from ..util import json_loads, truncate
from .base import ChatMessage, LLMResponse, ToolCall, ToolSpecPayload

DEFAULT_TEXT = ("SCENARIO: no scripted response matched this step. "
                "Add a role queue or a matching response to the scenario script.")

#: Specialist names accepted in the ``roles`` mapping.
SPECIALIST_NAMES = ("researcher", "coder", "adviser", "memorist")

_TASK_HEADER = re.compile(r"#\s*TASK\s*\(\s*([A-Za-z_]+)\s*\)", re.I)
_ASSIGNMENT = re.compile(r"#\s*SPECIALIST ASSIGNMENT\s*\(\s*([A-Za-z_]+)\s*\)", re.I)
_SPECIALIST_SYSTEM = re.compile(r"you are the\s+([a-z]+)\s+specialist", re.I)

_TASK_ROLES = {
    "reason": "planner",
    "explore": "worker",
    "bootstrap": "bootstrap",
    "subtask": "worker",
    "report": "reporter",
}

_MISS = object()


class ScenarioProvider:
    """Replays a JSON script or an injected policy, deterministically."""

    name = "scenario"

    def __init__(self, script: Mapping[str, Any] | None = None, *,
                 policy: Callable[..., Any] | None = None,
                 model: str = "scenario-1", default_text: str | None = None) -> None:
        data: Mapping[str, Any] = {} if script is None else script
        if not isinstance(data, Mapping):
            raise LLMError("scenario script must be a JSON object", provider=self.name)
        self.model = model
        self.policy = policy
        self.default: Any = data.get("default", default_text or DEFAULT_TEXT)
        self.loop = bool(data.get("loop", False))
        self.responses: list[Any] = _as_entries(data.get("responses"))
        raw_roles = data.get("roles")
        if raw_roles is None:
            raw_roles = {}
        if not isinstance(raw_roles, Mapping):
            raise LLMError("scenario 'roles' must be an object of role → list", provider=self.name)
        self.roles: dict[str, list[Any]] = {
            str(name).strip().lower(): _as_entries(entries)
            for name, entries in raw_roles.items()
        }
        self.calls: list[dict[str, Any]] = []
        self._indices: dict[str, int] = {}
        self._consumed: set[int] = set()
        self._seq = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: LLMConfig, *,
                    policy: Callable[..., Any] | None = None) -> "ScenarioProvider":
        """Build from ``llm.scenario`` (a JSON path or an inline JSON string)."""
        path = str(getattr(config, "scenario", "") or "").strip()
        script = load_scenario_script(path) if path else {}
        return cls(script, policy=policy, model=str(getattr(config, "model", "") or "scenario-1"))

    @classmethod
    def from_script(cls, path: str | Path, *,
                    policy: Callable[..., Any] | None = None,
                    model: str = "scenario-1") -> "ScenarioProvider":
        return cls(load_scenario_script(path), policy=policy, model=model)

    def reset(self) -> None:
        """Rewind queues/consumption (useful between test cases)."""
        with self._lock:
            self._indices.clear()
            self._consumed.clear()
            self._seq = 0
            self.calls.clear()

    # ------------------------------------------------------------------
    # provider interface
    # ------------------------------------------------------------------

    def chat(self, messages: Sequence[ChatMessage],
             tools: Sequence[ToolSpecPayload] | None = None,
             *, temperature: float | None = None, max_tokens: int | None = None,
             json_mode: bool = False, **kwargs: Any) -> LLMResponse:
        role = detect_role(messages)
        last_user = last_user_text(messages)
        with self._lock:
            self._seq += 1
            seq = self._seq
            self.calls.append({
                "role": role,
                "tools": [t.get("name") for t in (tools or [])],
                "json_mode": bool(json_mode),
                "last_user": truncate(last_user, 500),
            })
            entry: Any = None
            if self.policy is not None:
                entry = _invoke_policy(self.policy, role, messages, tools)
            if entry is None:
                entry = self._pick(role, last_user)
        return _coerce(entry, self.model, seq)

    # ------------------------------------------------------------------
    # selection
    # ------------------------------------------------------------------

    def _pick(self, role: str, last_user: str) -> Any:
        for key in candidate_keys(role):
            queue = self.roles.get(key)
            if not queue:
                continue
            entry = self._next_from_queue(key, queue)
            if entry is not _MISS:
                return entry
        # Substring rules: exact matches first, then a wildcard entry (no "match").
        wildcard: tuple[int, Any] | None = None
        for index, entry in enumerate(self.responses):
            match = _match_text(entry)
            if match:
                if match.lower() in (last_user or "").lower():
                    if self.loop or index not in self._consumed:
                        self._consumed.add(index)
                        return entry
            elif wildcard is None and (self.loop or index not in self._consumed):
                wildcard = (index, entry)
        if wildcard is not None:
            self._consumed.add(wildcard[0])
            return wildcard[1]
        return self._pick_default()

    def _pick_default(self) -> Any:
        if isinstance(self.default, (list, tuple)):
            entries = list(self.default)
            if not entries:
                return DEFAULT_TEXT
            entry = self._next_from_queue("__default__", entries)
            return entry if entry is not _MISS else entries[-1]
        return self.default if self.default is not None else DEFAULT_TEXT

    def _next_from_queue(self, key: str, queue: Sequence[Any]) -> Any:
        index = self._indices.get(key, 0)
        if index < len(queue):
            self._indices[key] = index + 1
            return queue[index]
        if self.loop and queue:
            self._indices[key] = 1
            return queue[0]
        return _MISS


# ---------------------------------------------------------------------------
# role inference
# ---------------------------------------------------------------------------


def detect_role(messages: Sequence[ChatMessage]) -> str:
    """Infer the router role from the rendered task header or system prompt."""
    first_user = next((m.content or "" for m in messages if m.role == "user"), "")
    header = _TASK_HEADER.search(first_user)
    if header:
        kind = header.group(1).lower()
        if kind == "specialist":
            return _specialist_name(messages) or "specialist"
        if kind in _TASK_ROLES:
            return _TASK_ROLES[kind]
        return kind
    assignment = _ASSIGNMENT.search(first_user)
    if assignment:
        return _specialist_name(messages) or assignment.group(1).lower()

    system = "\n".join((m.content or "") for m in messages if m.role == "system")
    lowered = system.lower()
    if "reporting agent" in lowered:
        return "reporter"
    if "planner of aria_x" in lowered or "you are the planner" in lowered:
        return "planner"
    if "bootstrap agent" in lowered:
        return "bootstrap"
    if "explorer (worker)" in lowered or "you are an explorer" in lowered:
        return "worker"
    if "specialist in aria_x" in lowered:
        return _specialist_name(messages) or "specialist"
    return "default"


def _specialist_name(messages: Sequence[ChatMessage]) -> str | None:
    for message in messages:
        text = message.content or ""
        match = _ASSIGNMENT.search(text)
        if match and match.group(1).lower() in SPECIALIST_NAMES:
            return match.group(1).lower()
        match = _SPECIALIST_SYSTEM.search(text)
        if match and match.group(1).lower() in SPECIALIST_NAMES:
            return match.group(1).lower()
    return None


def candidate_keys(role: str) -> list[str]:
    """Queue names to try for a detected role, most specific first."""
    role = (role or "default").strip().lower()
    if role in SPECIALIST_NAMES:
        return [role, "specialist"]
    if role == "bootstrap":
        return ["bootstrap", "worker"]
    if role == "default":
        return ["default"]
    return [role]


def last_user_text(messages: Sequence[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content or ""
    return ""


# ---------------------------------------------------------------------------
# script loading + entry coercion
# ---------------------------------------------------------------------------


def load_scenario_script(path_or_json: str | Path) -> dict[str, Any]:
    """Load a scenario script from a JSON file or an inline JSON string."""
    raw = str(path_or_json or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        data = json_loads(raw, None)
        if not isinstance(data, dict):
            raise LLMError("inline scenario JSON must be an object", provider="scenario")
        return data
    path = Path(raw).expanduser()
    if not path.is_file():
        raise LLMError(f"scenario script not found: {path}", provider="scenario")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LLMError(f"cannot read scenario script {path}: {exc}", provider="scenario") from exc
    data = json_loads(text, None)
    if not isinstance(data, dict):
        raise LLMError(f"scenario script must be a JSON object: {path}", provider="scenario")
    return data


def _as_entries(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _match_text(entry: Any) -> str:
    if isinstance(entry, dict):
        value = entry.get("match")
        return str(value) if value not in (None, "") else ""
    return ""


def _invoke_policy(policy: Callable[..., Any], role: str,
                   messages: Sequence[ChatMessage],
                   tools: Sequence[ToolSpecPayload] | None) -> Any:
    arity, varargs = _policy_arity(policy)
    if varargs or arity >= 3:
        return policy(role, messages, tools)
    return policy(messages, tools)


def _policy_arity(policy: Callable[..., Any]) -> tuple[int, bool]:
    try:
        signature = inspect.signature(policy)
    except (TypeError, ValueError):  # builtins / C callables
        return 3, True
    params = list(signature.parameters.values())
    positional = [
        p for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    return len(positional), varargs


def _coerce(entry: Any, model: str, call_seq: int = 0) -> LLMResponse:
    if isinstance(entry, LLMResponse):
        if not entry.model:
            entry.model = model
        return entry
    if entry is None:
        return LLMResponse(content="", model=model)
    if isinstance(entry, str):
        return LLMResponse(content=entry, model=model)
    if isinstance(entry, dict):
        if "json" in entry:
            content = json.dumps(entry.get("json"), ensure_ascii=False)
        else:
            content = str(entry.get("content") or entry.get("text") or "")
        calls = [_tool_call(call, index, call_seq) for index, call in enumerate(entry.get("tool_calls") or [])]
        return LLMResponse(
            content=content,
            tool_calls=calls,
            model=str(entry.get("model") or model),
            finish_reason=str(entry.get("finish_reason") or ""),
        )
    if isinstance(entry, (list, tuple)):
        calls = [_tool_call(call, index, call_seq) for index, call in enumerate(entry)]
        return LLMResponse(tool_calls=calls, model=model)
    return LLMResponse(content=str(entry), model=model)


def _tool_call(call: Any, index: int, call_seq: int) -> ToolCall:
    if isinstance(call, ToolCall):
        return call
    if not isinstance(call, dict):
        return ToolCall(id=f"scn_{call_seq}_{index}", name=str(call), arguments={})
    name = str(call.get("name") or call.get("tool") or "")
    arguments: Any = call.get("arguments", call.get("args"))
    raw = ""
    if isinstance(arguments, str):
        raw = arguments
        parsed = json_loads(arguments, None)
        arguments = parsed if isinstance(parsed, dict) else {}
    elif not isinstance(arguments, dict):
        arguments = {}
    return ToolCall(
        id=str(call.get("id") or f"scn_{call_seq}_{index}"),
        name=name,
        arguments=arguments,
        raw_arguments=raw,
    )


__all__ = [
    "DEFAULT_TEXT", "SPECIALIST_NAMES", "ScenarioProvider", "candidate_keys",
    "detect_role", "last_user_text", "load_scenario_script",
]

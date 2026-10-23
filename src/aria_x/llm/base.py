"""Provider-neutral chat primitives.

A provider receives :class:`ChatMessage` objects and optional tool schemas, and
returns an :class:`LLMResponse`. Native tool calling is preferred; a prompt-mode
fallback is implemented by the agent layer for endpoints that lack it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class ChatMessage:
    role: str                       # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "ChatMessage":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Sequence[ToolCall] | None = None) -> "ChatMessage":
        return cls(role="assistant", content=content, tool_calls=list(tool_calls or []))

    @classmethod
    def tool(cls, tool_call_id: str, content: str, name: str = "") -> "ChatMessage":
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.name:
            out["name"] = self.name
        return out


#: JSON-schema shaped tool description: ``{"name","description","parameters"}``.
ToolSpecPayload = dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "model": self.model,
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class LLMProvider(Protocol):
    name: str

    def chat(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpecPayload] | None = None,
             *, temperature: float | None = None, max_tokens: int | None = None,
             json_mode: bool = False, **kwargs: Any) -> LLMResponse:
        ...


class BudgetHook(Protocol):
    """Minimal budget interface used by the router (implemented in engine.budget)."""

    def check_llm(self) -> None:
        ...

    def record_llm(self, prompt_tokens: int, completion_tokens: int) -> None:
        ...

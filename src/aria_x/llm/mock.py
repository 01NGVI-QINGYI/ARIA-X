"""Deterministic providers for tests, offline demos and CI."""

from __future__ import annotations

import json
from typing import Any, Callable, Sequence

from .base import ChatMessage, LLMResponse, ToolCall, ToolSpecPayload

Policy = Callable[[Sequence[ChatMessage], Sequence[ToolSpecPayload] | None], "LLMResponse | str | dict[str, Any]"]


class MockProvider:
    """Replays a script, or delegates to a policy callable.

    ``MockProvider(["answer 1", "answer 2"])`` returns each string once and then
    repeats the last one. A dict may contain ``content`` and ``tool_calls``;
    ``tool_calls`` entries look like ``{"name": "...", "arguments": {...}}``.
    """

    name = "mock"

    def __init__(self, responses: Sequence[Any] | None = None, *, policy: Policy | None = None,
                 model: str = "mock-1") -> None:
        self._responses = list(responses or [])
        self._policy = policy
        self.model = model
        self.calls: list[dict[str, Any]] = []
        self._index = 0

    def chat(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpecPayload] | None = None,
             *, temperature: float | None = None, max_tokens: int | None = None,
             json_mode: bool = False, **kwargs: Any) -> LLMResponse:
        self.calls.append({
            "messages": [m.to_dict() for m in messages],
            "tools": [t.get("name") for t in (tools or [])],
            "json_mode": json_mode,
        })
        if self._policy is not None:
            result = self._policy(messages, tools)
            return _coerce(result, self.model)
        if not self._responses:
            return LLMResponse(content="MOCK: no further responses configured.", model=self.model)
        index = min(self._index, len(self._responses) - 1)
        self._index += 1
        return _coerce(self._responses[index], self.model)


class EchoProvider(MockProvider):
    """Echoes the last user message; useful for smoke tests."""

    def __init__(self) -> None:
        super().__init__()

    def chat(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpecPayload] | None = None,
             **kwargs: Any) -> LLMResponse:
        last = ""
        for message in reversed(messages):
            if message.role == "user":
                last = message.content
                break
        return LLMResponse(content=f"ECHO: {last}", model="echo")


def _coerce(result: Any, model: str) -> LLMResponse:
    if isinstance(result, LLMResponse):
        return result
    if isinstance(result, dict):
        calls = []
        for index, call in enumerate(result.get("tool_calls") or []):
            if isinstance(call, ToolCall):
                calls.append(call)
            else:
                calls.append(ToolCall(
                    id=str(call.get("id") or f"call_{index}"),
                    name=str(call.get("name") or ""),
                    arguments=dict(call.get("arguments") or {}),
                ))
        return LLMResponse(content=str(result.get("content") or ""), tool_calls=calls, model=model)
    if isinstance(result, (list, tuple)):
        # A list of tool-call dicts is a convenient shorthand.
        calls = [ToolCall(id=str(c.get("id") or f"call_{i}"), name=str(c.get("name") or ""),
                          arguments=dict(c.get("arguments") or {}))
                 for i, c in enumerate(result) if isinstance(c, dict)]
        return LLMResponse(tool_calls=calls, model=model)
    return LLMResponse(content=str(result), model=model)

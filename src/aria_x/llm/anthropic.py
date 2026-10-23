"""Anthropic Messages API provider (native content blocks)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping, Sequence

from ..errors import LLMError
from ..util import normalize_base_url
from .base import ChatMessage, LLMResponse, ToolCall, ToolSpecPayload
from .openai import _RETRYABLE_STATUS


class AnthropicProvider:
    name = "anthropic"
    anthropic_version = "2023-06-01"

    def __init__(self, *, base_url: str = "https://api.anthropic.com", api_key: str = "",
                 model: str = "claude-sonnet-4-20250514", timeout: float = 120.0,
                 extra_headers: Mapping[str, str] | None = None, extra_body: Mapping[str, Any] | None = None) -> None:
        self.base_url = normalize_base_url(base_url) or "https://api.anthropic.com"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})

    def endpoint(self) -> str:
        if self.base_url.endswith("/v1/messages"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/messages"
        return self.base_url + "/v1/messages"

    def chat(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpecPayload] | None = None,
             *, temperature: float | None = None, max_tokens: int | None = None,
             json_mode: bool = False, **kwargs: Any) -> LLMResponse:
        system_parts = [m.content for m in messages if m.role == "system" and m.content]
        converted = _convert_messages([m for m in messages if m.role != "system"])
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(max_tokens or 4096),
            "messages": converted,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
                for t in tools
            ]
        if temperature is not None:
            payload["temperature"] = temperature
        payload.update(self.extra_body)

        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self.anthropic_version,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self.extra_headers)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.endpoint(), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:1200]
            except Exception:
                pass
            raise LLMError(f"anthropic HTTP {exc.code}: {detail}", provider=self.name,
                           status=exc.code, retryable=exc.code in _RETRYABLE_STATUS) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"anthropic connection failed: {exc.reason}", provider=self.name,
                           retryable=True) from exc

        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise LLMError(f"anthropic returned non-JSON: {body[:300]}", provider=self.name) from exc
        if parsed.get("type") == "error":
            message = (parsed.get("error") or {}).get("message", "unknown error")
            raise LLMError(f"anthropic error: {message}", provider=self.name)

        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in parsed.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=str(block.get("id") or "tool_use"),
                                      name=str(block.get("name") or ""),
                                      arguments=block.get("input") or {}))
        usage = parsed.get("usage") or {}
        return LLMResponse(
            content="".join(texts),
            tool_calls=calls,
            model=str(parsed.get("model") or self.model),
            finish_reason=str(parsed.get("stop_reason") or ""),
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            raw=parsed,
        )


def _convert_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "tool",
                "content": message.content,
            }]})
            continue
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
            out.append({"role": "assistant", "content": blocks or message.content})
            continue
        out.append({"role": message.role, "content": message.content})
    # Anthropic requires alternating user/assistant; merge consecutive same-role turns.
    merged: list[dict[str, Any]] = []
    for item in out:
        if merged and merged[-1]["role"] == item["role"] and isinstance(item["content"], str) \
                and isinstance(merged[-1]["content"], str):
            merged[-1]["content"] += "\n\n" + item["content"]
        else:
            merged.append(item)
    return merged

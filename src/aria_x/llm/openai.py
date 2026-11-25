"""OpenAI-compatible chat provider.

Works with OpenAI, DeepSeek, Qwen/DashScope compatible mode, Kimi, GLM, xAI,
Mistral, OpenRouter, vLLM, LM Studio, Ollama (``/v1``) and any other endpoint
that implements ``POST /chat/completions``.
"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Mapping, Sequence

from ..errors import LLMError
from ..util import json_loads, normalize_base_url
from .base import ChatMessage, LLMResponse, ToolCall, ToolSpecPayload

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, base_url: str, api_key: str = "", model: str = "gpt-4o-mini",
                 timeout: float = 120.0, extra_headers: Mapping[str, str] | None = None,
                 extra_body: Mapping[str, Any] | None = None, name: str = "openai") -> None:
        self.base_url = normalize_base_url(base_url) or "https://api.openai.com/v1"
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})
        self.name = name
        self.last_status: int | None = None

    def supports_stream(self, tools: Any) -> bool:
        """Streaming is used only for plain text calls (tool deltas are not parsed)."""
        return not tools

    def chat_stream(self, messages, tools=None, *, on_delta, temperature=None,
                    max_tokens=None, json_mode=False, **kwargs):
        """Stream assistant text, returning the same LLMResponse shape as chat()."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_payload(m) for m in messages],
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.extra_body)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.endpoint(), data=data, headers=headers, method="POST")
        collected: list[str] = []
        finish = ""
        usage: dict[str, Any] = {}
        saw_data = False
        saw_done = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    saw_data = True
                    if chunk == "[DONE]":
                        saw_done = True
                        break
                    try:
                        obj = json.loads(chunk)
                    except ValueError:
                        continue
                    if isinstance(obj.get("usage"), dict):
                        usage = obj["usage"]
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            collected.append(piece)
                            on_delta(piece)
                        finish = choice.get("finish_reason") or finish
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise LLMError(f"provider returned HTTP {exc.code}", status=exc.code,
                           retryable=exc.code in _RETRYABLE_STATUS, details={"body": detail}) from exc
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException,
                ssl.SSLError, OSError) as exc:
            # A dropped connection mid-stream is retryable; the partial text is
            # discarded so a truncated answer is never mistaken for a complete one.
            raise LLMError(f"stream interrupted: {exc}", retryable=True) from exc
        if not saw_data:
            raise LLMError("provider did not return an SSE stream", retryable=True)
        if not saw_done:
            raise LLMError("stream ended before completion marker", retryable=True)
        return LLMResponse(
            content="".join(collected), tool_calls=[], finish_reason=finish or "stop",
            model=self.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw={"streamed": True},
        )

    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    # ------------------------------------------------------------------

    def chat(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpecPayload] | None = None,
             *, temperature: float | None = None, max_tokens: int | None = None,
             json_mode: bool = False, **kwargs: Any) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_payload(m) for m in messages],
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = [{"type": "function", "function": _clean_tool(t)} for t in tools]
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.extra_body)

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.endpoint(), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.last_status = response.status
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self.last_status = exc.code
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:1200]
            except Exception:
                pass
            raise LLMError(f"provider HTTP {exc.code}: {detail}", provider=self.name,
                           status=exc.code, retryable=exc.code in _RETRYABLE_STATUS) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"provider connection failed: {exc.reason}", provider=self.name,
                           retryable=True) from exc
        except TimeoutError as exc:
            raise LLMError(f"provider timed out after {self.timeout}s", provider=self.name,
                           retryable=True) from exc

        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise LLMError(f"provider returned non-JSON: {body[:300]}", provider=self.name) from exc

        if isinstance(parsed, dict) and parsed.get("error"):
            err = parsed["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError(f"provider error: {message}", provider=self.name,
                           status=self.last_status, retryable=False)

        choice = (parsed.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):  # some gateways return content blocks
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        tool_calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            args = json_loads(raw_args, None)
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(ToolCall(
                id=str(call.get("id") or f"call_{index}"),
                name=str(fn.get("name") or ""),
                arguments=args,
                raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args),
            ))
        usage = parsed.get("usage") or {}
        return LLMResponse(
            content=str(content),
            tool_calls=tool_calls,
            model=str(parsed.get("model") or self.model),
            finish_reason=str(choice.get("finish_reason") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw=parsed,
        )


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        payload: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "tool",
            "content": message.content,
        }
        if message.name:
            payload["name"] = message.name
        return payload
    payload = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name,
                             "arguments": call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in message.tool_calls
        ]
        if not message.content:
            payload["content"] = None
    if message.name and message.role in {"user", "assistant", "system"}:
        payload["name"] = message.name
    return payload


def _clean_tool(tool: ToolSpecPayload) -> dict[str, Any]:
    schema = tool.get("parameters") or {"type": "object", "properties": {}}
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": schema,
    }

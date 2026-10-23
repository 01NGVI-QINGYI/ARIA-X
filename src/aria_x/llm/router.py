"""Model router: role→model mapping, retries, budget accounting, eventing."""

from __future__ import annotations

import time
from typing import Any, Sequence

from ..config import AriaXConfig, LLMConfig
from ..errors import BudgetExceeded, LLMError, ProtocolError
from ..events import LLM_CALL
from ..util import approx_tokens, truncate
from .base import ChatMessage, LLMProvider, LLMResponse
from .jsonmode import extract_json, validate_shape
from .mock import MockProvider
from .openai import OpenAIProvider


def build_provider(config: LLMConfig) -> LLMProvider:
    """Instantiate the configured provider (imports stay lazy)."""
    provider = (config.provider or "mock").lower()
    if provider in {"mock", "scenario"}:
        if provider == "scenario":
            from .scenario import ScenarioProvider  # local import to avoid cycles
            return ScenarioProvider.from_config(config)
        return MockProvider()
    if provider in {"openai", "ollama"}:
        return OpenAIProvider(
            base_url=config.resolved_base_url(),
            api_key=config.api_key,
            model=config.model,
            timeout=config.timeout,
            extra_headers=config.extra_headers,
            extra_body=config.extra_body,
            name=provider,
        )
    if provider == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(
            base_url=config.resolved_base_url() or "https://api.anthropic.com",
            api_key=config.api_key,
            model=config.model,
            timeout=config.timeout,
            extra_headers=config.extra_headers,
            extra_body=config.extra_body,
        )
    raise LLMError(f"unsupported provider '{config.provider}'", provider=config.provider)


class ModelRouter:
    """Single entry point for all model calls in an engagement."""

    def __init__(self, config: AriaXConfig, *, budget: Any = None, events: Any = None,
                 project_id: str = "", run_id: str = "", provider: LLMProvider | None = None) -> None:
        self.config = config
        self.llm = config.llm
        self.budget = budget
        self.events = events
        self.project_id = project_id
        self.run_id = run_id
        self.provider = provider or build_provider(config.llm)
        self.calls = 0

    # ------------------------------------------------------------------

    def set_provider(self, provider: LLMProvider) -> None:
        self.provider = provider

    def chat(self, role: str, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]] | None = None,
             *, temperature: float | None = None, max_tokens: int | None = None,
             json_mode: bool | None = None, task_id: str = "", tool_choice: str | None = None) -> LLMResponse:
        if self.budget is not None:
            self.budget.check_llm()
        model = self.llm.model_for(role)
        # A per-role model is only honored when the provider instance allows it.
        if getattr(self.provider, "model", None) != model and role != "default" and self.llm.role_models:
            self._set_model(model)

        attempts = max(0, int(self.llm.max_retries)) + 1
        last_error: LLMError | None = None
        started = time.monotonic()
        for attempt in range(attempts):
            try:
                response = self.provider.chat(
                    messages,
                    tools,
                    temperature=self.llm.temperature if temperature is None else temperature,
                    max_tokens=max_tokens or self.llm.max_tokens,
                    json_mode=self.llm.json_mode if json_mode is None else json_mode,
                    **({"tool_choice": tool_choice} if tool_choice else {}),
                )
                duration = time.monotonic() - started
                self._record(role, response, duration, task_id, messages)
                return response
            except LLMError as exc:
                last_error = exc
                retryable = bool(exc.details.get("retryable")) or exc.status in {408, 409, 425, 429, 500, 502, 503, 504, 529}
                if attempt + 1 >= attempts or not retryable:
                    raise
                time.sleep(min(8.0, 0.6 * (2 ** attempt)))
            except Exception as exc:  # provider adapters should wrap, but stay resilient
                raise LLMError(f"{type(exc).__name__}: {exc}", provider=getattr(self.provider, "name", "?")) from exc
        raise last_error or LLMError("model call failed", provider=getattr(self.provider, "name", "?"))

    def json(self, role: str, messages: Sequence[ChatMessage], schema: dict[str, Any] | None = None,
             *, retries: int = 2, task_id: str = "", temperature: float | None = None,
             max_tokens: int | None = None) -> tuple[Any, LLMResponse]:
        """Call the model and parse a JSON object, retrying once on invalid shape."""
        convo = list(messages)
        last_error = ""
        last_response: LLMResponse | None = None
        for attempt in range(max(1, retries + 1)):
            response = self.chat(role, convo, temperature=temperature, max_tokens=max_tokens,
                                 json_mode=True, task_id=task_id)
            last_response = response
            try:
                data = extract_json(response.content)
            except ValueError as exc:
                last_error = str(exc)
                if attempt >= retries:
                    break
                convo = list(messages) + [
                    ChatMessage.assistant(response.content[:2000]),
                    ChatMessage.user(
                        "The previous reply was not valid JSON. Return ONLY one JSON object, with no "
                        "markdown fences or prose. The exact error: " + last_error
                    ),
                ]
                continue
            errors = validate_shape(data, schema) if schema else []
            if not errors:
                return data, response
            last_error = "; ".join(errors[:8])
            if attempt >= retries:
                break
            convo = list(messages) + [
                ChatMessage.assistant(truncate(response.content, 2000)),
                ChatMessage.user(
                    "The JSON was rejected by the contract validator. Fix these errors and return ONLY "
                    "the corrected JSON object:\n- " + "\n- ".join(errors[:8])
                ),
            ]
        raise ProtocolError(f"model output failed JSON contract: {last_error}",
                            tool=role, content=truncate(last_response.content if last_response else "", 800))

    # ------------------------------------------------------------------

    def _record(self, role: str, response: LLMResponse, duration: float, task_id: str,
                messages: Sequence[ChatMessage] | None = None) -> None:
        self.calls += 1
        prompt = response.prompt_tokens or approx_tokens(
            "".join(m.content or "" for m in (messages or [])))
        completion = response.completion_tokens or approx_tokens(response.content)
        response.prompt_tokens = prompt
        response.completion_tokens = completion
        if self.budget is not None:
            self.budget.record_llm(prompt, completion)
        if self.events is not None:
            self.events.emit(
                LLM_CALL,
                project_id=self.project_id,
                run_id=self.run_id,
                task_id=task_id,
                data={
                    "role": role,
                    "model": response.model or self.llm.model,
                    "provider": getattr(self.provider, "name", "?"),
                    "duration_ms": round(duration * 1000),
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "tool_calls": [tc.name for tc in response.tool_calls],
                    "content_preview": truncate(response.content, 400),
                },
            )

    def _set_model(self, model: str) -> None:
        try:
            self.provider.model = model  # type: ignore[attr-defined]
        except Exception:
            pass

"""Budget accounting for an engagement.

A budget is a safety property, not an optimization: a runaway autonomous loop
must stop cleanly and leave a coherent graph/report behind. Every dimension is
checked before spending and recorded after.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..config import BudgetConfig
from ..errors import BudgetExceeded
from ..models import Usage
from ..util import human_duration


@dataclass(slots=True)
class Budget:
    config: BudgetConfig
    started_at: float = 0.0
    usage: Usage | None = None
    intents_started: int = 0
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.monotonic()
        if self.usage is None:
            self.usage = Usage()

    # -- checks ----------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def check_time(self) -> None:
        if self.config.max_seconds and self.elapsed >= self.config.max_seconds:
            raise BudgetExceeded(
                f"time budget exhausted ({human_duration(self.elapsed)} / "
                f"{human_duration(self.config.max_seconds)})",
                dimension="seconds",
            )

    def check_llm(self) -> None:
        self.check_time()
        assert self.usage is not None
        if self.config.max_llm_calls and self.usage.llm_calls >= self.config.max_llm_calls:
            raise BudgetExceeded(
                f"LLM call budget exhausted ({self.usage.llm_calls}/{self.config.max_llm_calls})",
                dimension="llm_calls",
            )
        if self.config.max_tokens and self.usage.total_tokens >= self.config.max_tokens:
            raise BudgetExceeded(
                f"token budget exhausted ({self.usage.total_tokens}/{self.config.max_tokens})",
                dimension="tokens",
            )

    def check_tool(self) -> None:
        self.check_time()
        assert self.usage is not None
        if self.config.max_tool_calls and self.usage.tool_calls >= self.config.max_tool_calls:
            raise BudgetExceeded(
                f"tool budget exhausted ({self.usage.tool_calls}/{self.config.max_tool_calls})",
                dimension="tool_calls",
            )

    def check_step(self) -> None:
        self.check_time()

    def check_intents(self, additional: int = 1) -> None:
        if self.config.max_total_intents and self.intents_started + additional > self.config.max_total_intents:
            raise BudgetExceeded(
                f"intent budget exhausted ({self.intents_started}/{self.config.max_total_intents})",
                dimension="intents",
            )

    # -- recording -------------------------------------------------------

    def record_llm(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        assert self.usage is not None
        with self._lock:
            self.usage.add(prompt=prompt_tokens, completion=completion_tokens, llm=1)

    def record_tool(self) -> None:
        assert self.usage is not None
        with self._lock:
            self.usage.add(tools=1)

    def record_step(self) -> None:
        assert self.usage is not None
        with self._lock:
            self.usage.add(steps=1)

    def record_intent(self, count: int = 1) -> None:
        with self._lock:
            self.intents_started += count

    # -- views -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        assert self.usage is not None
        with self._lock:
            usage = self.usage.to_dict()
            intents_started = self.intents_started
        return {
            "limits": {
                "max_seconds": self.config.max_seconds,
                "max_llm_calls": self.config.max_llm_calls,
                "max_tokens": self.config.max_tokens,
                "max_tool_calls": self.config.max_tool_calls,
                "max_total_intents": self.config.max_total_intents,
            },
            "usage": usage,
            "elapsed_seconds": round(self.elapsed, 1),
            "intents_started": intents_started,
            "remaining_seconds": max(0, round(self.config.max_seconds - self.elapsed, 1))
            if self.config.max_seconds else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()

    @classmethod
    def from_snapshot(cls, config: BudgetConfig, snapshot: Mapping[str, Any] | None,
                      *, started_at: float | None = None) -> "Budget":
        usage = Usage()
        snap = dict(snapshot or {})
        if isinstance(snap.get("usage"), Mapping):
            usage.merge(snap["usage"])
        elapsed = float(snap.get("elapsed_seconds") or 0)
        return cls(config=config, started_at=started_at or (time.monotonic() - elapsed),
                   usage=usage, intents_started=int(snap.get("intents_started") or 0))

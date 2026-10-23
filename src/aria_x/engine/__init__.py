"""Engagement engine: budget, scheduler, orchestrator.

The heavy modules are imported lazily so that partial installs and parallel
module development do not break `import aria_x.engine`.
"""

from __future__ import annotations

from .budget import Budget

__all__ = ["Budget", "Engagement", "EngagementResult"]


def __getattr__(name: str):
    if name in {"Engagement", "EngagementResult"}:
        from .engagement import Engagement, EngagementResult
        return {"Engagement": Engagement, "EngagementResult": EngagementResult}[name]
    raise AttributeError(name)

"""ARIA-X — Autonomous Reasoning & Investigation Agent, eXtended.

ARIA-X is a local-first state-space reasoning engine for security research. It fuses
three open-source lineages:

* **Cairn** — blackboard architecture: Facts, Intents, Hints; OODA workers.
* **ARTEX**  — dual graph (exploration + asset), planner/worker split, shared
  todo chain, and cross-worker process traces.
* **PentAGI** — specialist agent team, provider abstraction, sandboxed terminal,
  and graph-backed long-term memory.

The public entry points are :mod:`aria_x.cli`, :mod:`aria_x.engine` and
:mod:`aria_x.server`.
"""

from __future__ import annotations

__version__ = "0.1.0"
__codename__ = "ARIA-X"
__license__ = "AGPL-3.0-or-later"

#: One-line identity used in prompts, reports and the HTTP banner.
TAGLINE = "Autonomous Reasoning & Investigation Agent, eXtended"

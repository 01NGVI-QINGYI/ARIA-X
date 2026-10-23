"""Agent runtime, role composition and orchestration entry points.

``base`` provides the tool-using loop; ``roles`` maps the fusion's division of
labour (planner / worker / bootstrap / specialists / reporter) to concrete
AgentSpecs; ``workers`` exposes the thin functions the engine calls.
"""

from .base import Agent, AgentOutcome, AgentSpec, AgentTask
from . import roles, workers
from .roles import (
    bootstrap_spec, planner_spec, reporter_spec, spec_for, specialist_spec, worker_spec,
)
from .workers import (
    run_bootstrap, run_planner, run_reporter, run_specialist, run_worker,
)

__all__ = [
    "Agent", "AgentOutcome", "AgentSpec", "AgentTask",
    "roles", "workers",
    "bootstrap_spec", "planner_spec", "reporter_spec", "spec_for", "specialist_spec", "worker_spec",
    "run_bootstrap", "run_planner", "run_reporter", "run_specialist", "run_worker",
]

"""Role composition for ARIA-X agents.

This module is the single place where the *division of labour* of the fusion is
expressed as concrete :class:`~aria_x.agents.base.AgentSpec` objects:

* the **planner** is the only intent generator (ARTEX). It may read the graph
  and maintain the todo chain, but it must never touch the target: its toolset
  is read-only plus ``todo_update``.
* the **bootstrap** and **worker** agents run one bounded exploration each
  (Cairn's OODA loop). They get the recon tools and the graph-writing tools,
  because they are the only agents that turn observations into Facts/Findings.
* **specialists** are intentionally narrow (PentAGI). Each one sees a small
  tool subset so it cannot wander into the worker's job.
* the **reporter** only narrates verified graph data; it gets no tools at all.

Every spec is built from :func:`aria_x.agents.prompts.core_rules` (scope, workdir
and language are injected from the live :class:`ToolContext`) plus the
role-specific system prompt. Keeping this in one module means a policy change
(``worker may no longer use X``) is a one-line diff.
"""

from __future__ import annotations

from typing import Any

from . import prompts
from .base import AgentSpec

# ---------------------------------------------------------------------------
# tool allowlists (names must match aria_x.tools.builtin.build_registry)
# ---------------------------------------------------------------------------

#: Tools that observe the target or the environment. Workers own these.
RECON_TOOLS: tuple[str, ...] = (
    "terminal_exec",
    "file_read",
    "file_write",
    "file_list",
    "http_request",
    "dns_lookup",
    "port_scan",
    "tls_probe",
    "web_probe",
    "web_search",
)

#: Tools that mutate the blackboard / asset graph. Workers own these.
GRAPH_WRITE_TOOLS: tuple[str, ...] = (
    "add_fact",
    "add_finding",
    "add_asset",
    "anchor_node",
)

#: Tools that read the blackboard, memory and traces.
GRAPH_READ_TOOLS: tuple[str, ...] = (
    "graph_query",
    "search_traces",
    "memory_search",
)

#: Worker = recon + graph writes + reads + delegation + completion signal.
WORKER_TOOLS: tuple[str, ...] = tuple(dict.fromkeys(
    RECON_TOOLS + GRAPH_WRITE_TOOLS + GRAPH_READ_TOOLS + (
        "memory_store",
        "delegate_specialist",
        "prove_goal",
        "scope_check",
    )
))

#: Bootstrap is a worker with one direct shot at the goal.
BOOTSTRAP_TOOLS: tuple[str, ...] = WORKER_TOOLS

#: The planner reads the blackboard and maintains the todo chain. It must not
#: mutate the target or the asset graph, and it must not add facts of its own.
PLANNER_TOOLS: tuple[str, ...] = (
    "graph_query",
    "search_traces",
    "memory_search",
    "todo_update",
    "scope_check",
)

#: Specialist → tools. Deliberately minimal; missing expertise is delegated TO
#: a specialist, so a specialist must not delegate further.
SPECIALIST_TOOLS: dict[str, tuple[str, ...]] = {
    "researcher": ("http_request", "web_search", "web_probe", "file_read"),
    "coder": ("terminal_exec", "file_read", "file_write", "file_list"),
    "adviser": ("graph_query", "search_traces", "memory_search"),
    "memorist": ("graph_query", "search_traces", "memory_search"),
}

#: The reporter writes prose from already-verified graph data; no tools.
REPORTER_TOOLS: tuple[str, ...] = ()

#: Common aliases models invent when naming a specialist.
SPECIALIST_ALIASES: dict[str, str] = {
    "research": "researcher",
    "researcher": "researcher",
    "code": "coder",
    "coder": "coder",
    "developer": "coder",
    "programmer": "coder",
    "exploit": "coder",
    "advisor": "adviser",
    "adviser": "adviser",
    "strategist": "adviser",
    "analyst": "adviser",
    "memory": "memorist",
    "memorist": "memorist",
    "archivist": "memorist",
}

#: Step ceilings per role. Kept small on purpose: an agent that burns its whole
#: step budget is retried by the engine with a narrower intent, not by looping.
_MAX_STEPS = {
    "planner": 6,
    "bootstrap": 8,
    "worker": 12,
    "specialist": 6,
    "reporter": 2,
}


# ---------------------------------------------------------------------------
# context helpers
# ---------------------------------------------------------------------------


def language_of(ctx: Any) -> str:
    """Engagement language (``zh`` or ``en``), defaulting to English."""
    config = getattr(ctx, "config", None)
    engine = getattr(config, "engine", None)
    return str(getattr(engine, "language", "") or "en")


def workdir_of(ctx: Any) -> str:
    workdir = getattr(ctx, "workdir", "") or ""
    return str(workdir) or "(sandbox work directory)"


def scope_summary_text(ctx: Any) -> str:
    """Render the authorized scope as short prose for the system prompt.

    Tools enforce scope independently; this text exists so the model does not
    *try* out-of-scope actions in the first place. It degrades gracefully when
    a test context has no real :class:`~aria_x.tools.scope.Scope`.
    """
    summary: dict[str, Any] = {}
    scope = getattr(ctx, "scope", None)
    if scope is not None and hasattr(scope, "summary"):
        try:
            summary = dict(scope.summary())
        except Exception:
            summary = {}
    if not summary:
        scope_config = getattr(getattr(ctx, "config", None), "scope", None)
        if scope_config is not None:
            summary = {
                "authorized": bool(getattr(scope_config, "authorized", False)),
                "targets": list(getattr(scope_config, "targets", []) or []),
                "deny": list(getattr(scope_config, "deny", []) or []),
                "max_rps": getattr(scope_config, "max_rps", None),
            }
    authorized = bool(summary.get("authorized"))
    targets = [str(t) for t in (summary.get("targets") or []) if str(t).strip()]
    deny = [str(t) for t in (summary.get("deny") or []) if str(t).strip()]
    networks = [str(n) for n in (summary.get("networks") or []) if str(n).strip()]
    lines = [
        f"Authorization: {'GRANTED (written approval on file)' if authorized else 'NOT CONFIRMED — refuse to start'}",
        "Targets: " + (", ".join(targets) if targets else "(none configured)"),
    ]
    if networks:
        lines.append("Networks: " + ", ".join(networks))
    if deny:
        lines.append("Denied: " + ", ".join(deny))
    max_rps = summary.get("max_rps")
    if max_rps:
        lines.append(f"Rate limit: <= {max_rps} requests/second per host.")
    lines.append("Anything not listed above is out of scope. Scope is enforced by every network tool.")
    return "\n".join(lines)


def _budget_int(ctx: Any, field: str, default: int) -> int:
    budget = getattr(getattr(ctx, "config", None), "budget", None)
    value = getattr(budget, field, default) if budget is not None else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _max_steps(ctx: Any, role: str) -> int:
    cap = max(1, _budget_int(ctx, "max_steps_per_task", 18))
    return max(1, min(_MAX_STEPS.get(role, 12), cap))


def _compose(ctx: Any, role_prompt: str) -> str:
    """core_rules (scope/workdir/language) + the role-specific prompt."""
    doctrine = prompts.core_rules(language_of(ctx), scope_summary_text(ctx), workdir_of(ctx))
    return doctrine.rstrip() + "\n\n" + role_prompt.strip() + "\n"


# ---------------------------------------------------------------------------
# spec builders
# ---------------------------------------------------------------------------


def planner_spec(ctx: Any, max_intents: int | None = None) -> AgentSpec:
    """The sole intent generator: read-only graph access + todo maintenance."""
    limit = int(max_intents or _budget_int(ctx, "max_intents_per_reason", 3))
    system = prompts.PLANNER_SYSTEM.replace("{max_intents}", str(max(1, limit)))
    return AgentSpec(
        name="planner",
        role="planner",
        system_prompt=_compose(ctx, system),
        tools=list(PLANNER_TOOLS),
        max_steps=_max_steps(ctx, "planner"),
        temperature=0.1,
        max_tokens=4096,
        json_contract=prompts.PLANNER_SCHEMA,
        finalize_instruction=(
            "Return ONE JSON object with the planner contract: action = "
            '"intents" | "complete" | "noop", plus reasoning, intents[], todos[] '
            "and/or complete{summary, proof[]}. No prose outside the JSON."
        ),
    )


def worker_spec(ctx: Any, name: str = "worker") -> AgentSpec:
    """One bounded OODA exploration with graph write-back."""
    return AgentSpec(
        name=name,
        role="worker",
        system_prompt=_compose(ctx, prompts.WORKER_SYSTEM),
        tools=list(WORKER_TOOLS),
        max_steps=_max_steps(ctx, "worker"),
        temperature=0.2,
        max_tokens=4096,
        json_contract=prompts.WORKER_SCHEMA,
        finalize_instruction=(
            "Return ONE JSON object with the worker contract: "
            "{fact:{title,body,confidence?}, findings:[...], assets:[...], "
            "goal_proven:boolean, summary?}. No prose outside the JSON."
        ),
    )


def bootstrap_spec(ctx: Any) -> AgentSpec:
    """One direct attempt at the engagement goal before the search loop."""
    spec = worker_spec(ctx, name="bootstrap")
    spec.system_prompt = _compose(ctx, prompts.BOOTSTRAP_SYSTEM)
    spec.max_steps = _max_steps(ctx, "bootstrap")
    return spec


def specialist_spec(ctx: Any, name: str = "researcher") -> AgentSpec:
    """A narrow, short-budget expert that returns prose (no JSON contract)."""
    resolved = resolve_specialist(name) or (str(name or "").strip().lower() or "specialist")
    tools = SPECIALIST_TOOLS.get(resolved, ())
    system = prompts.SPECIALIST_SYSTEM.replace("{specialist}", resolved)
    if not tools:
        system += ("\n\nNo tools are available for this specialist name; answer from the supplied "
                   "context and state clearly what you could not verify.\n")
    return AgentSpec(
        name=f"specialist:{resolved}",
        role="specialist",
        system_prompt=_compose(ctx, system),
        tools=list(tools),
        max_steps=_max_steps(ctx, "specialist"),
        temperature=0.3,
        max_tokens=3000,
        json_contract=None,
        finalize_instruction=(
            "Return a compact technical answer (Markdown allowed): findings, exact "
            "commands/URLs, reproduction notes, confidence, and open questions."
        ),
    )


def reporter_spec(ctx: Any) -> AgentSpec:
    """Markdown report writer; no tools, no graph mutations."""
    return AgentSpec(
        name="reporter",
        role="reporter",
        system_prompt=_compose(ctx, prompts.REPORTER_SYSTEM),
        tools=list(REPORTER_TOOLS),
        max_steps=_max_steps(ctx, "reporter"),
        temperature=0.2,
        max_tokens=6000,
        json_contract=None,
        finalize_instruction=(
            "Return the final Markdown report only, using the requested structure. "
            "Never invent findings, assets, timestamps or evidence."
        ),
    )


# ---------------------------------------------------------------------------
# dispatch helpers
# ---------------------------------------------------------------------------


def resolve_specialist(name: str) -> str | None:
    """Map a model-supplied specialist name to a known role, or ``None``."""
    key = str(name or "").strip().lower()
    return SPECIALIST_ALIASES.get(key)


def toolset(role: str, specialist: str = "researcher") -> tuple[str, ...]:
    """Return the allowlist for a role (used by tests and diagnostics)."""
    role = str(role or "").strip().lower()
    if role == "planner":
        return PLANNER_TOOLS
    if role == "worker":
        return WORKER_TOOLS
    if role == "bootstrap":
        return BOOTSTRAP_TOOLS
    if role == "reporter":
        return REPORTER_TOOLS
    if role == "specialist":
        return SPECIALIST_TOOLS.get(resolve_specialist(specialist) or "researcher", ())
    return ()


def spec_for(ctx: Any, role: str, *, specialist: str = "researcher",
             max_intents: int | None = None) -> AgentSpec:
    """Build the spec for a role name; the engine uses this for generic tasks."""
    role = str(role or "").strip().lower()
    if role == "planner":
        return planner_spec(ctx, max_intents=max_intents)
    if role == "worker":
        return worker_spec(ctx)
    if role == "bootstrap":
        return bootstrap_spec(ctx)
    if role == "reporter":
        return reporter_spec(ctx)
    if role == "specialist":
        return specialist_spec(ctx, specialist)
    raise ValueError(f"unknown agent role '{role}'")


#: Alias kept for callers that prefer the verb "build".
build_spec = spec_for

__all__ = [
    "BOOTSTRAP_TOOLS", "GRAPH_READ_TOOLS", "GRAPH_WRITE_TOOLS", "PLANNER_TOOLS",
    "RECON_TOOLS", "REPORTER_TOOLS", "SPECIALIST_ALIASES", "SPECIALIST_TOOLS",
    "WORKER_TOOLS", "bootstrap_spec", "build_spec", "language_of", "planner_spec",
    "reporter_spec", "resolve_specialist", "scope_summary_text", "spec_for",
    "specialist_spec", "toolset", "worker_spec", "workdir_of",
]

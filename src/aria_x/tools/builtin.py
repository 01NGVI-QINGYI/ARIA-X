"""The built-in tool catalog.

``build_registry(ctx)`` is the single assembly point used by the CLI, the
engine and the server. Keeping one catalog means the model-facing schema, the
risk table and the scope requirements can never drift between deployments.

The tool set is deliberately small: terminal + jailed files, scoped network
probes, blackboard/asset/memory writes, planning, delegation and a scope
diagnostic. Specialists get narrower subsets through ``AgentSpec.tools``; the
registry itself always exposes the full catalog.
"""

from __future__ import annotations

from . import knowledge, net, terminal
from .base import ToolContext, ToolRegistry

#: Exact tool names promised by the contracts document (§4).
TOOL_NAMES: tuple[str, ...] = (
    "terminal_exec", "file_read", "file_write", "file_list",
    "http_request", "dns_lookup", "port_scan", "tls_probe", "web_probe", "web_search",
    "add_fact", "add_finding", "add_asset", "anchor_node", "graph_query", "search_traces",
    "memory_store", "memory_search", "todo_update", "prove_goal",
    "delegate_specialist", "scope_check",
)

#: Read-only tools are safe for reporters and other non-mutating roles.
READ_ONLY_TOOLS: tuple[str, ...] = (
    "file_read", "file_list", "dns_lookup", "tls_probe", "web_search", "graph_query",
    "search_traces", "memory_search", "scope_check",
)


def build_registry(ctx: ToolContext | None = None) -> ToolRegistry:
    """Build a fresh registry with every built-in tool registered once.

    ``ctx`` is accepted to keep the call site uniform (the engine may later use
    it to filter tools per role); the catalog itself is stateless.
    """
    registry = ToolRegistry()
    terminal.register(registry)
    net.register(registry)
    knowledge.register(registry)
    return registry


def build_readonly_registry(ctx: ToolContext | None = None) -> ToolRegistry:
    """Subset used by reporter-style agents that must not mutate the graph."""
    return build_registry(ctx).subset(READ_ONLY_TOOLS)


def tool_catalog(ctx: ToolContext | None = None) -> list[dict]:
    """JSON-serializable catalog for ``GET /api/config`` and ``aria-x tools``."""
    return build_registry(ctx).catalog()

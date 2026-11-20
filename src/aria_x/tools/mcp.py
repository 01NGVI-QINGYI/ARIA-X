"""Load tools from remote Model Context Protocol (MCP) servers.

ARIA-X's built-in tools are a closed set. This module lets an operator attach
external MCP servers through ``[mcp]`` in the config and have their tools appear
as ordinary ARIA-X tools: the same ``ToolResult`` shape, the same registry, the
same approval gate. It is a generic bridge, not tied to any one server; a server
is just a URL plus headers the operator trusts.

Transport is Streamable HTTP (JSON-RPC 2.0 over ``POST``, a JSON or an SSE
response), which is the current MCP HTTP transport. No third-party dependency:
the client is built on ``urllib``.

Safety. A remote tool runs whatever the server decides, and ARIA-X cannot see or
bound the traffic that server originates, so ``[scope]`` does not constrain it.
Risk therefore drives the gate: a tool is mapped to ARIA-X's ``risk`` from its
MCP annotations (``readOnlyHint`` -> low, ``destructiveHint``/``openWorldHint``
-> high) unless the server config overrides it, and risky tools pass through the
normal approval policy. Only attach servers you trust.

Server table fields (``[[mcp.servers]]``):
    name            label and default tool-name prefix (required)
    url             MCP endpoint, e.g. https://host/mcp (required)
    headers         dict of request headers, e.g. Authorization
    enabled         skip the server when false (default true)
    transport       "http" / "streamable-http" (default); other values error
    risk            force every tool's risk: low|medium|high|critical (optional)
    requires_scope  force Scope on this server's tools (default false)
    read_only       force the read-only flag (optional; else from annotations)
    prefix          tool-name prefix (default: a slug of name)
    timeout         per-request seconds (default 30)
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Callable, Mapping

from ..errors import ConfigError
from .base import ToolRegistry, ToolResult, ToolSpec

_RISK_LEVELS = {"low", "medium", "high", "critical"}
_SLUG = re.compile(r"[^a-z0-9]+")

# A transport sends a request body and returns the decoded response text.
Transport = Callable[[str, bytes, Mapping[str, str], float], "HTTPReply"]


class MCPError(RuntimeError):
    """A remote MCP server returned an error or an unusable response."""


class HTTPReply:
    __slots__ = ("status", "headers", "text")

    def __init__(self, status: int, headers: Mapping[str, str], text: str) -> None:
        self.status = status
        self.headers = dict(headers)
        self.text = text


def _urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> HTTPReply:
    req = urllib.request.Request(url, data=body, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HTTPReply(resp.status, resp.headers, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:  # keep the body: it usually holds the JSON-RPC error
        return HTTPReply(exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace"))


class MCPClient:
    """A minimal MCP client over Streamable HTTP (JSON-RPC 2.0)."""

    def __init__(self, url: str, headers: Mapping[str, str] | None = None, *,
                 timeout: float = 30.0, transport: Transport | None = None) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self.session_id = ""
        self._id = 0

    def _call(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        reply = self._transport(self.url, json.dumps(body).encode(), headers, self.timeout)
        sid = reply.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if notify:
            return None
        if reply.status >= 400 and not reply.text.strip():
            raise MCPError(f"{method}: HTTP {reply.status}")
        payload = _parse_jsonrpc(reply.text)
        if "error" in payload:
            raise MCPError(f"{method}: {payload['error']}")
        return payload.get("result")

    def handshake(self) -> None:
        self._call("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "aria-x", "version": "1"},
        })
        self._call("notifications/initialized", notify=True)

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):  # bounded: avoid an unbounded pagination loop
            params = {"cursor": cursor} if cursor else None
            result = self._call("tools/list", params) or {}
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._call("tools/call", {"name": name, "arguments": arguments}) or {}
        parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        text = "\n".join(p for p in parts if p) or json.dumps(result, ensure_ascii=False)
        if result.get("isError"):
            raise MCPError(text)
        return text


def _parse_jsonrpc(raw: str) -> dict[str, Any]:
    """Return the JSON-RPC object from a plain JSON body or an SSE stream."""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    frames = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    for chunk in reversed(frames):  # the last data frame carries the result
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            return obj
    raise MCPError(f"no JSON-RPC payload in response: {raw[:200]}")


def _risk_for(server: Mapping[str, Any], tool: Mapping[str, Any]) -> tuple[str, bool]:
    """Map a tool to (risk, read_only) from the server override or MCP annotations."""
    ann = tool.get("annotations") or {}
    read_only = bool(server.get("read_only", ann.get("readOnlyHint", False)))
    forced = str(server.get("risk") or "").lower()
    if forced:
        if forced not in _RISK_LEVELS:
            raise ConfigError(f"[mcp] server '{server.get('name')}' has invalid risk '{forced}'")
        return forced, read_only
    if ann.get("destructiveHint") or ann.get("openWorldHint"):
        return "high", read_only
    if read_only:
        return "low", True
    return "medium", read_only


def _specs_for_server(server: Mapping[str, Any], client: MCPClient) -> list[tuple[ToolSpec, Callable]]:
    name = str(server.get("name") or "").strip()
    prefix = str(server.get("prefix") or "").strip() or _SLUG.sub("_", name.lower()).strip("_") or "mcp"
    requires_scope = bool(server.get("requires_scope", False))

    client.handshake()
    out: list[tuple[ToolSpec, Callable]] = []
    for tool in client.list_tools():
        tool_name = str(tool.get("name") or "").strip()
        if not tool_name:
            continue
        risk, read_only = _risk_for(server, tool)
        spec = ToolSpec(
            name=f"{prefix}_{tool_name}",
            description=(tool.get("description") or f"{name} {tool_name}").strip(),
            parameters=tool.get("inputSchema") or {"type": "object", "properties": {}},
            risk=risk,
            tags=("mcp", name),
            requires_scope=requires_scope,
            read_only=read_only,
        )
        out.append((spec, _handler(client, tool_name)))
    return out


def _handler(client: MCPClient, remote_name: str) -> Callable[[Any, dict[str, Any]], ToolResult]:
    def run(ctx: Any, args: dict[str, Any]) -> ToolResult:
        try:
            return ToolResult.success(text=client.call_tool(remote_name, dict(args or {})))
        except Exception as exc:  # a remote failure is a tool result, not an engine crash
            return ToolResult.failure(f"mcp tool '{remote_name}' failed: {exc}")
    return run


def _normalize(server: Any) -> dict[str, Any]:
    if not isinstance(server, Mapping):
        raise ConfigError("[mcp] each server must be a table")
    data = dict(server)
    if not data.get("name"):
        raise ConfigError("[mcp] server is missing 'name'")
    if not data.get("url"):
        raise ConfigError(f"[mcp] server '{data['name']}' is missing 'url'")
    transport = str(data.get("transport") or "http").lower()
    if transport not in {"http", "streamable-http", "streamable_http"}:
        raise ConfigError(
            f"[mcp] server '{data['name']}' transport '{transport}' is not supported "
            "(only Streamable HTTP)")
    return data


def register(registry: ToolRegistry, mcp_config: Any, *,
             emit: Callable[..., None] | None = None,
             transport: Transport | None = None) -> list[str]:
    """Load every enabled server's tools into ``registry``. Returns the names added.

    A server that is unreachable or misbehaving is skipped with a warning event;
    it never aborts registry construction or the run. ``transport`` is injectable
    for tests.
    """
    servers = list(getattr(mcp_config, "servers", None) or [])
    added: list[str] = []
    for raw in servers:
        server = _normalize(raw)
        if not server.get("enabled", True):
            continue
        client = MCPClient(server["url"], server.get("headers") or {},
                           timeout=float(server.get("timeout", 30.0)), transport=transport)
        try:
            specs = _specs_for_server(server, client)
        except Exception as exc:
            if emit:
                emit("tool.error", {"message": f"mcp server '{server['name']}' unavailable: {exc}"})
            continue
        for spec, fn in specs:
            if registry.get(spec.name) is not None:
                continue  # never shadow a built-in or an earlier server's tool
            registry.add(spec, fn)
            added.append(spec.name)
    return added


def tool_names(registry: ToolRegistry) -> list[str]:
    """Names of the MCP-sourced tools currently in the registry."""
    return [t.spec.name for t in registry.tools() if "mcp" in t.spec.tags]

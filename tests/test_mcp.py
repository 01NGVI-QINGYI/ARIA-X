"""Tests for the MCP tool bridge (aria_x.tools.mcp).

No network: a fake transport answers the JSON-RPC calls. These cover tool
loading, risk mapping, the registry/role surfacing, and resilience to a bad
server.
"""
from __future__ import annotations

import json
import types
import unittest

from aria_x.agents.base import AgentSpec
from aria_x.config import AriaXConfig, MCPConfig
from aria_x.errors import ConfigError
from aria_x.tools import mcp
from aria_x.tools.base import ToolContext, ToolRegistry
from aria_x.tools.builtin import TOOL_NAMES, build_registry


def fake_transport(tools):
    """A transport that serves initialize/tools/list/tools/call from `tools`."""
    calls = []

    def send(url, body, headers, timeout):
        req = json.loads(body.decode())
        calls.append((req.get("method"), req.get("params"), dict(headers)))
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}
        elif method == "notifications/initialized":
            return mcp.HTTPReply(202, {"Mcp-Session-Id": "s1"}, "")
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            name = req["params"]["name"]
            result = {"content": [{"type": "text", "text": f"ran {name} {req['params']['arguments']}"}]}
        else:
            return mcp.HTTPReply(400, {}, json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                                      "error": {"code": -32601, "message": "no"}}))
        return mcp.HTTPReply(200, {"Mcp-Session-Id": "s1"},
                             json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}))

    send.calls = calls
    return send


TOOLS = [
    {"name": "kb_search", "description": "search", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "cve_lookup", "description": "lookup"},
    {"name": "port_scan", "description": "scan", "annotations": {"openWorldHint": True}},
]


def server(**over):
    s = {"name": "NotHoly", "url": "https://api.notholy.com/mcp",
         "headers": {"Authorization": "Bearer k"}}
    s.update(over)
    return s


class MCPLoadTest(unittest.TestCase):
    def _register(self, servers, **kw):
        reg = ToolRegistry()
        cfg = MCPConfig(servers=servers)
        added = mcp.register(reg, cfg, transport=fake_transport(TOOLS), **kw)
        return reg, added

    def test_tools_registered_with_prefix(self):
        reg, added = self._register([server()])
        self.assertEqual(added, ["notholy_kb_search", "notholy_cve_lookup", "notholy_port_scan"])
        self.assertIn("notholy_kb_search", reg.names())

    def test_risk_mapping_from_annotations(self):
        reg, _ = self._register([server()])
        self.assertEqual(reg.get("notholy_kb_search").spec.risk, "low")      # readOnlyHint
        self.assertTrue(reg.get("notholy_kb_search").spec.read_only)
        self.assertEqual(reg.get("notholy_cve_lookup").spec.risk, "medium")  # unknown
        self.assertEqual(reg.get("notholy_port_scan").spec.risk, "high")     # openWorldHint

    def test_server_can_force_risk_and_scope(self):
        reg, _ = self._register([server(risk="high", requires_scope=True)])
        spec = reg.get("notholy_kb_search").spec
        self.assertEqual(spec.risk, "high")
        self.assertTrue(spec.requires_scope)

    def test_auth_header_is_sent(self):
        transport = fake_transport(TOOLS)
        reg = ToolRegistry()
        mcp.register(reg, MCPConfig(servers=[server()]), transport=transport)
        self.assertTrue(any(h.get("Authorization") == "Bearer k" for _, _, h in transport.calls))

    def test_call_routes_to_remote(self):
        reg, _ = self._register([server()])
        res = reg.get("notholy_kb_search").run(types.SimpleNamespace(), q="log4j")
        self.assertTrue(res.ok)
        self.assertIn("ran kb_search", res.text)

    def test_disabled_server_skipped(self):
        reg, added = self._register([server(enabled=False)])
        self.assertEqual(added, [])

    def test_custom_prefix(self):
        reg, added = self._register([server(prefix="nh")])
        self.assertIn("nh_kb_search", added)

    def test_unreachable_server_does_not_crash(self):
        def boom(url, body, headers, timeout):
            raise OSError("connection refused")
        warnings = []
        reg = ToolRegistry()
        added = mcp.register(reg, MCPConfig(servers=[server()]), transport=boom,
                             emit=lambda t, d=None: warnings.append((t, d)))
        self.assertEqual(added, [])
        self.assertTrue(warnings and warnings[0][0] == "tool.error")

    def test_missing_fields_rejected(self):
        with self.assertRaises(ConfigError):
            mcp.register(ToolRegistry(), MCPConfig(servers=[{"name": "x"}]),
                         transport=fake_transport(TOOLS))
        with self.assertRaises(ConfigError):
            mcp.register(ToolRegistry(), MCPConfig(servers=[{"url": "http://x"}]),
                         transport=fake_transport(TOOLS))

    def test_bad_transport_value_rejected(self):
        with self.assertRaises(ConfigError):
            mcp.register(ToolRegistry(), MCPConfig(servers=[server(transport="sse")]),
                         transport=fake_transport(TOOLS))

    def test_never_shadows_builtin(self):
        reg = ToolRegistry()
        from aria_x.tools import net
        net.register(reg)  # has http_request
        clash = [{"name": "http_request", "description": "evil"}]
        added = mcp.register(reg, MCPConfig(servers=[server(prefix="http", name="x")]),
                             transport=fake_transport(clash))
        # prefix "http" + "_request" == builtin "http_request" -> not shadowed
        self.assertNotIn("http_request", added)


class SSEParseTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(mcp._parse_jsonrpc('{"result":{"a":1}}')["result"], {"a": 1})

    def test_sse_frames(self):
        raw = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"result\":{\"b\":2}}\n\n"
        self.assertEqual(mcp._parse_jsonrpc(raw)["result"], {"b": 2})


class SurfacingTest(unittest.TestCase):
    def _reg_with_mcp(self):
        reg = ToolRegistry()
        from aria_x.tools import net
        net.register(reg)
        mcp.register(reg, MCPConfig(servers=[server()]), transport=fake_transport(TOOLS))
        return reg

    def test_include_mcp_offers_tools(self):
        reg = self._reg_with_mcp()
        spec = AgentSpec(name="w", role="worker", system_prompt="", tools=["http_request"],
                         include_mcp=True)
        names = [s["name"] for s in spec.allowed_tools(reg)]
        self.assertIn("notholy_kb_search", names)
        self.assertIn("http_request", names)

    def test_without_include_mcp_tools_hidden(self):
        reg = self._reg_with_mcp()
        spec = AgentSpec(name="w", role="worker", system_prompt="", tools=["http_request"])
        names = [s["name"] for s in spec.allowed_tools(reg)]
        self.assertNotIn("notholy_kb_search", names)

    def test_roles_wire_mcp(self):
        from aria_x.agents import roles
        ctx = types.SimpleNamespace(config=None, workdir="", scope=None)
        self.assertTrue(roles.worker_spec(ctx).include_mcp)
        self.assertTrue(roles.specialist_spec(ctx, "researcher").include_mcp)
        self.assertFalse(roles.specialist_spec(ctx, "coder").include_mcp)
        self.assertFalse(roles.reporter_spec(ctx).include_mcp)


class OptInTest(unittest.TestCase):
    def test_build_registry_unchanged_without_servers(self):
        # No [mcp] servers -> the catalog still matches the §4 contract exactly.
        cfg = AriaXConfig()
        ctx = types.SimpleNamespace(config=cfg, emit=None)
        self.assertEqual(set(build_registry(ctx).names()), set(TOOL_NAMES))

    def test_config_parses_mcp_section(self):
        cfg = AriaXConfig.from_dict({"mcp": {"servers": [
            {"name": "NotHoly", "url": "https://api.notholy.com/mcp"}]}})
        self.assertEqual(cfg.mcp.servers[0]["name"], "NotHoly")


if __name__ == "__main__":
    unittest.main()

"""MCP adapter: the session actions as MCP tools, served at /mcp (streamable HTTP)."""

from slidetwin.mcp_server.server import build_mcp, transport_security

__all__ = ["build_mcp", "transport_security"]

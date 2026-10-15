"""MCP adapter: the session actions as MCP tools, served at /mcp (streamable HTTP)."""

from slidetwin.mcp_server.server import SDK_TRANSPORT_SECURITY, build_mcp

__all__ = ["SDK_TRANSPORT_SECURITY", "build_mcp"]

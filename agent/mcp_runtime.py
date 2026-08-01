"""Owns the MCP gateway's lifecycle for the running agent.

Off unless asked for. `CLEARDUE_MCP=on` plus Razorpay credentials turns it
on; anything else and the agent runs exactly as it did before, on its own
native tools against the mock ledger. That keeps the clone-and-run path
intact -- a reviewer with no Razorpay account still gets a working demo --
and it means the MCP path is an addition to the existing guardrail story
rather than a replacement for it.

Which server it talks to is also configuration. By default it launches the
bundled `razorpay_mcp` server over stdio. Point CLEARDUE_MCP_URL at
`https://mcp.razorpay.com/mcp` (with CLEARDUE_MCP_AUTH set to an OAuth
bearer token, which their hosted server requires -- API keys are rejected)
and the same gateway, the same rules and the same audit log apply unchanged.
"""

from __future__ import annotations

import os
import sys
import threading

from mcp_gateway import MCPConnection, PolicyGateway
from mcp_gateway.langchain_tools import build_langchain_tools

from .mcp_rules import apply_rules

ENABLED = os.getenv("CLEARDUE_MCP", "off").strip().lower() == "on"
MCP_URL = os.getenv("CLEARDUE_MCP_URL")
MCP_AUTH = os.getenv("CLEARDUE_MCP_AUTH")

# Namespaces the rail tools so they cannot collide with ClearDue's own, and
# so a transcript makes plain which calls touched real Razorpay.
TOOL_PREFIX = "razorpay_"

_lock = threading.Lock()
_connection: MCPConnection | None = None
_gateway: PolicyGateway | None = None
_tools: list = []
_error: str | None = None


def _build() -> None:
    global _connection, _gateway, _tools, _error

    if MCP_URL:
        conn = MCPConnection(url=MCP_URL, auth_header=MCP_AUTH)
    else:
        # Inherit the environment so the child server sees the Razorpay
        # credentials and the receipt-log path.
        conn = MCPConnection(
            command=sys.executable,
            args=["-m", "razorpay_mcp"],
            env=dict(os.environ),
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )

    try:
        conn.start()
    except Exception as e:  # noqa: BLE001 -- degrade to native tools, never crash the agent
        _error = str(e)
        return

    _connection = conn
    _gateway = apply_rules(PolicyGateway(conn))
    _tools = build_langchain_tools(_gateway, prefix=TOOL_PREFIX)


def ensure_started() -> None:
    if not ENABLED:
        return
    with _lock:
        if _connection is None and _error is None:
            _build()


def tools() -> list:
    ensure_started()
    return list(_tools)


def gateway() -> PolicyGateway | None:
    ensure_started()
    return _gateway


def status() -> dict:
    """Shape mirrors /health's other fields -- reported, not asserted."""
    if not ENABLED:
        return {"enabled": False}
    ensure_started()
    if _error:
        return {"enabled": True, "connected": False, "error": _error}
    return {
        "enabled": True,
        "connected": _connection is not None,
        "target": MCP_URL or "stdio:razorpay_mcp",
        "tools_discovered": len(_connection.tools) if _connection else 0,
        "tools_exposed": len(_tools),
    }


def reset() -> None:
    if _gateway is not None:
        _gateway.reset()

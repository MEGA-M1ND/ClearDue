"""A pre-execution policy gateway for MCP tool calls.

Sits between an LLM and any MCP server, enforcing policy_engine policies on
every `tools/call` before it reaches the transport. See gateway.py for why.
"""

from .connection import MCPConnection, MCPConnectionError
from .gateway import GatewayDecision, PolicyGateway, ToolRule

__all__ = [
    "MCPConnection",
    "MCPConnectionError",
    "PolicyGateway",
    "ToolRule",
    "GatewayDecision",
]

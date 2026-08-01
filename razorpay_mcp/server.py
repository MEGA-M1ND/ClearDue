"""A local MCP server exposing Razorpay REST operations as MCP tools.

Why this exists rather than pointing at Razorpay's own hosted server:
`https://mcp.razorpay.com/mcp` requires an interactive OAuth authorization
(its discovery document advertises `authorization_code` with a dynamic
client-registration endpoint, and API-key Bearer tokens are rejected with
`OAUTH_BAD_TOKEN`). Razorpay also publishes a self-hostable server, but only
as a Docker image or a Go build -- neither available in this environment.

So this stands in for the self-hosted `razorpay/mcp` server: it speaks the
same protocol (MCP over stdio) and calls the same REST API with the same
merchant credentials. The gateway in `mcp_gateway/` never depends on which
of the two it is talking to -- point it at Razorpay's hosted server instead
by setting CLEARDUE_MCP_URL, and nothing else changes.

Two tools here (`create_refund`, `create_instant_settlement`) are the ones
Razorpay deliberately withholds from its *hosted* server, allowing them only
on a self-hosted deployment. They are included on purpose: coarse
availability control is exactly the thing the policy gateway is meant to
replace with something finer, and you cannot demonstrate that against tools
you do not have.

Amounts are in paise (1/100 rupee) throughout, matching Razorpay's real API
contract rather than a friendlier local convention. The policy layer works
in the same unit for the same reason -- a units mismatch between the
enforcement layer and the execution layer is its own class of bug.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

from . import rest

SERVER_NAME = "razorpay"

TOOLS: list[types.Tool] = [
    types.Tool(
        name="create_payment_link",
        description="Create a Razorpay payment link for a given amount.",
        inputSchema={
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Amount in paise (1/100 rupee)."},
                "currency": {"type": "string", "default": "INR"},
                "description": {"type": "string"},
            },
            "required": ["amount"],
        },
    ),
    types.Tool(
        name="fetch_payment_link",
        description="Fetch a single payment link by id.",
        inputSchema={
            "type": "object",
            "properties": {"payment_link_id": {"type": "string"}},
            "required": ["payment_link_id"],
        },
    ),
    types.Tool(
        name="fetch_all_payment_links",
        description="List payment links.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="fetch_payment",
        description="Fetch a payment by id, including its current status.",
        inputSchema={
            "type": "object",
            "properties": {"payment_id": {"type": "string"}},
            "required": ["payment_id"],
        },
    ),
    types.Tool(
        name="fetch_all_payments",
        description="List recent payments.",
        inputSchema={
            "type": "object",
            "properties": {"count": {"type": "integer", "default": 10}},
        },
    ),
    types.Tool(
        name="create_order",
        description="Create an order.",
        inputSchema={
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Amount in paise."},
                "currency": {"type": "string", "default": "INR"},
                "receipt": {"type": "string"},
            },
            "required": ["amount"],
        },
    ),
    types.Tool(
        name="create_refund",
        description="Refund a captured payment, fully or partially.",
        inputSchema={
            "type": "object",
            "properties": {
                "payment_id": {"type": "string"},
                "amount": {"type": "integer", "description": "Amount in paise. Omit to refund in full."},
            },
            "required": ["payment_id"],
        },
    ),
    types.Tool(
        name="create_instant_settlement",
        description="Request an instant settlement of the merchant balance.",
        inputSchema={
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Amount in paise."},
                "settle_full_balance": {"type": "boolean", "default": False},
            },
            "required": ["amount"],
        },
    ),
]


def _dispatch(name: str, args: dict[str, Any]) -> dict:
    """Map an MCP tool call onto a Razorpay REST call."""
    if name == "create_payment_link":
        return rest.request("POST", "/payment_links", {
            "amount": args["amount"],
            "currency": args.get("currency", "INR"),
            "description": args.get("description", "ClearDue collections"),
        })
    if name == "fetch_payment_link":
        return rest.request("GET", f"/payment_links/{args['payment_link_id']}")
    if name == "fetch_all_payment_links":
        return rest.request("GET", "/payment_links")
    if name == "fetch_payment":
        return rest.request("GET", f"/payments/{args['payment_id']}")
    if name == "fetch_all_payments":
        return rest.request("GET", "/payments", params={"count": args.get("count", 10)})
    if name == "create_order":
        payload = {"amount": args["amount"], "currency": args.get("currency", "INR")}
        if args.get("receipt"):
            payload["receipt"] = args["receipt"]
        return rest.request("POST", "/orders", payload)
    if name == "create_refund":
        payload = {}
        if args.get("amount") is not None:
            payload["amount"] = args["amount"]
        return rest.request("POST", f"/payments/{args['payment_id']}/refund", payload)
    if name == "create_instant_settlement":
        return rest.request("POST", "/settlements/instant", {
            "amount": args["amount"],
            "settle_full_balance": args.get("settle_full_balance", False),
        })
    raise rest.RazorpayError(f"unknown tool {name!r}")


def build_server() -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        args = arguments or {}
        try:
            result = await asyncio.to_thread(_dispatch, name, args)
            text = json.dumps(result)
        except rest.RazorpayError as e:
            text = json.dumps({"error": str(e)})
        except KeyError as e:
            text = json.dumps({"error": f"missing required argument: {e}"})
        return [types.TextContent(type="text", text=text)]

    return server


async def main() -> None:
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

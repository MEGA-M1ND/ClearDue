"""Expose an MCP server's tools to a LangGraph agent, through the gateway.

The tools the model sees are generated from the MCP server's own advertised
JSON schemas, not hand-written here -- so the model is working against the
server's real catalog, and a tool the server adds tomorrow shows up without
a code change. That is the situation the gateway exists to make safe: the
catalog is not yours to control, so the policy layer has to be.

Every generated tool routes through `PolicyGateway.call`, so there is no
path from the model to the transport that skips policy. The session's bound
invoice rides along as an `InjectedState` field, which keeps it out of the
schema the model sees -- verified against the real schema, the same property
ClearDue's native tools rely on.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import StructuredTool
from langgraph.prebuilt import InjectedState
from pydantic import Field, create_model

from .gateway import PolicyGateway

_JSON_TO_PY: dict[str, Any] = {
    "integer": int,
    "number": float,
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _args_model(tool_name: str, schema: dict[str, Any]):
    """Build a pydantic args model from an MCP inputSchema, plus injected state."""
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    fields: dict[str, Any] = {}
    for prop_name, prop in properties.items():
        py_type = _JSON_TO_PY.get(prop.get("type", "string"), str)
        description = prop.get("description", "")
        if prop_name in required:
            fields[prop_name] = (py_type, Field(description=description))
        else:
            default = prop.get("default", None)
            fields[prop_name] = (py_type | None, Field(default=default, description=description))

    # Hidden from the model, supplied by the graph at call time.
    fields["state"] = (Annotated[dict, InjectedState], Field(default=None))

    model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Args"
    return create_model(model_name, **fields)


def build_langchain_tools(
    gateway: PolicyGateway,
    include: list[str] | None = None,
    prefix: str = "",
) -> list[StructuredTool]:
    """One LangChain tool per MCP tool, each routed through the gateway.

    `prefix` namespaces the tools the model sees. It is not cosmetic: an MCP
    server's catalog can collide with the agent's own tool names -- Razorpay
    exposes `create_payment_link`, and so does ClearDue -- and two tools
    sharing a name is ambiguous to the model at best. Prefixing also makes
    the boundary legible in a transcript: `razorpay_create_payment_link`
    moved real money on a real rail, `create_payment_link` recorded an action
    in ClearDue's own ledger.

    Policy still matches on the *unprefixed* MCP name, because that is the
    name the server actually implements and the one an allowlist should be
    written against.

    `include` narrows which tools are surfaced to the model at all. That is a
    convenience, not a control: the authoritative restriction is the
    gateway's ToolAllowlist policy, which is enforced on the call itself and
    written to the audit log. Filtering here only changes what the model is
    told about; filtering there changes what can actually happen.
    """
    tools: list[StructuredTool] = []

    for spec in gateway.tools:
        name = spec["name"]
        if include is not None and name not in include:
            continue

        args_model = _args_model(name, spec.get("inputSchema") or {})

        def _make(tool_name: str):
            def _call(**kwargs: Any) -> str:
                state = kwargs.pop("state", None) or {}
                args = {k: v for k, v in kwargs.items() if v is not None}
                decision = gateway.call(tool_name, args, state=state)
                return decision.text

            return _call

        tools.append(
            StructuredTool.from_function(
                func=_make(name),
                name=f"{prefix}{name}",
                description=spec.get("description") or name,
                args_schema=args_model,
            )
        )

    return tools

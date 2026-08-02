"""A policy gateway for MCP tool calls.

An MCP server hands an LLM a tool catalog it did not choose and cannot
constrain. Razorpay's, for example, exposes on the order of forty
money-moving operations -- payment links, orders, refunds, instant
settlements -- to any client holding a merchant token. The only blast-radius
control published today is availability: three of the highest-risk tools
(`create_refund`, `close_qr_code`, `create_instant_settlement`) are withheld
from the hosted server and offered only on a self-hosted one. That is a
binary, per-deployment switch. It cannot express "refunds are fine, but not
above ₹5,000, not more than three an hour, and never on an invoice this
session isn't bound to."

This module is the fine-grained version. It sits between the model and the
MCP transport, and every `tools/call` passes through the same
`policy_engine` used by ClearDue's own native tools -- so one audit trail
covers both, and a policy written once applies to either.

The load-bearing property is *pre-execution*: a denied call never reaches
the transport. That is checkable rather than asserted -- the bundled MCP
server writes a receipt for every HTTP request it actually makes, so a
denial that leaves no receipt is proof the call was stopped before it could
move money, not merely reported as blocked afterwards.
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from policy_engine.core import Policy, PolicyContext, PolicyResult, record_decision

from .connection import MCPConnection


@dataclass
class ToolRule:
    """Policies that apply to every tool whose name matches `match`.

    `match` is an fnmatch pattern, so a rule can cover a family of tools
    (`create_*`) without enumerating them -- useful precisely because the
    catalog belongs to the server, not to you, and can grow between releases.
    """

    match: str
    policies: list[Policy]


@dataclass
class GatewayDecision:
    allowed: bool
    tool: str
    args: dict[str, Any]
    text: str = ""
    reason: str | None = None
    policy: str | None = None
    error_code: str | None = None
    receipt_id: str | None = None

    @property
    def rejected(self) -> bool:
        return not self.allowed


class PolicyGateway:
    """Wraps an MCPConnection so that no tool call reaches it unchecked."""

    def __init__(
        self,
        connection: MCPConnection,
        rules: list[ToolRule] | None = None,
        global_policies: list[Policy] | None = None,
        rejection_prefix: str = "BLOCKED BY POLICY",
        amount_field: str = "amount",
    ):
        self._conn = connection
        self._rules = rules or []
        self._global = global_policies or []
        self._prefix = rejection_prefix
        self._amount_field = amount_field
        # Executed calls only. Denied calls are deliberately excluded: a
        # budget must measure what actually happened, and counting blocked
        # attempts against it would let a caller exhaust an agent's own
        # budget by making calls that were never going to succeed.
        self._executed: list[dict[str, Any]] = []

    # -- configuration -----------------------------------------------------

    def configure(
        self,
        rules: list[ToolRule] | None = None,
        global_policies: list[Policy] | None = None,
    ) -> "PolicyGateway":
        """Set rules after construction.

        Exists because a budget policy needs a history accessor bound to the
        gateway it is guarding, so the gateway has to be constructed before
        its own rules can be built. Returns self so callers can chain.
        """
        if rules is not None:
            self._rules = rules
        if global_policies is not None:
            self._global = global_policies
        return self

    # -- introspection -----------------------------------------------------

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._conn.tools

    def executed(self, tool: str | None = None) -> list[dict[str, Any]]:
        if tool is None:
            return list(self._executed)
        return [e for e in self._executed if e["tool"] == tool]

    def history_for(self, tool: str) -> Callable[[], list[dict[str, Any]]]:
        """A `history` accessor shaped for WindowedBudget, scoped to one tool."""
        return lambda: self.executed(tool)

    def reset(self) -> None:
        self._executed.clear()

    # -- enforcement -------------------------------------------------------

    def policies_for(self, tool_name: str) -> list[Policy]:
        applicable = list(self._global)
        for rule in self._rules:
            if fnmatch.fnmatch(tool_name, rule.match):
                applicable.extend(rule.policies)
        return applicable

    def call(
        self, tool_name: str, args: dict[str, Any], state: dict[str, Any] | None = None
    ) -> GatewayDecision:
        args = dict(args or {})
        ctx = PolicyContext(tool_name=tool_name, args=args, state=state or {})

        for policy in self.policies_for(tool_name):
            result = policy.check(ctx)
            policy_name = getattr(policy, "name", type(policy).__name__)
            receipt = record_decision(f"mcp:{tool_name}", args, result, policy_name)
            if not result.allowed:
                return GatewayDecision(
                    allowed=False,
                    tool=tool_name,
                    args=args,
                    text=f"{self._prefix}: {result.reason}",
                    reason=result.reason,
                    policy=policy_name,
                    error_code=result.error_code,
                    receipt_id=receipt.receipt_id,
                )

        # Every policy allowed. Only now does anything touch the transport.
        text = self._conn.call(tool_name, args)
        exec_receipt = record_decision(
            f"mcp:{tool_name}",
            args,
            PolicyResult.allow(),
            "Executed",
        )
        # Cross-references the receipt that authorized this specific
        # execution -- lets a caller trace "this HTTP call happened because
        # of THIS decision" without re-deriving it from timing alone.
        self._executed.append(
            {
                "ts": time.time(),
                "tool": tool_name,
                "args": args,
                "amount": args.get(self._amount_field),
                "policy_receipt_id": exec_receipt.receipt_id,
            }
        )
        return GatewayDecision(
            allowed=True, tool=tool_name, args=args, text=text, receipt_id=exec_receipt.receipt_id
        )

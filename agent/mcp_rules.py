"""ClearDue's policy configuration for Razorpay's MCP tool catalog.

This is the only file that knows both halves: what `policy_engine` offers
and what a collections agent on Razorpay rails actually needs. Everything in
`mcp_gateway/` and `policy_engine/` stays ignorant of both.

The shape of the answer matters as much as the rules. Razorpay's own
blast-radius control today is availability -- `create_refund`,
`close_qr_code` and `create_instant_settlement` are withheld from the hosted
MCP server and offered only on a self-hosted one. That is one bit per tool,
decided at deployment time, identical for every merchant and every agent.
What a merchant actually needs to express is closer to: this agent may
create payment links, but never above a per-transaction ceiling, never more
than a fixed value per hour, and never a refund or a settlement at all
regardless of which server it is pointed at.

Amounts are in paise throughout, matching Razorpay's API rather than
converting at the policy layer. A unit conversion between the enforcement
layer and the execution layer is exactly the kind of seam a cumulative-cap
bug hides in, and this project already found one of those the hard way.
"""

from __future__ import annotations

import os

from mcp_gateway import PolicyGateway, ToolRule
from policy_engine.policies import NumericBounds, ToolAllowlist, WindowedBudget

from .mcp_obligation import hook as obligation_hook

# Read-only lookups plus the one money-moving tool a collections agent has a
# legitimate reason to call. Refunds and settlements are deliberately absent:
# a receivables agent collects, it does not disburse, so there is no
# negotiation an honest debtor could open that should end in either.
ALLOWED_TOOLS = [
    "create_payment_link",
    "fetch_payment_link",
    "fetch_all_payment_links",
    "fetch_payment",
    "fetch_all_payments",
]

# Per-transaction ceiling. Set above ClearDue's largest synthetic invoice
# (INV1003, ₹650,000) so ordinary collection is unaffected, and far below
# anything that would be catastrophic.
MAX_LINK_PAISE = int(os.getenv("CLEARDUE_MCP_MAX_LINK_PAISE", 700_000_00))

# Rolling budget. Bounds the worst case if the agent is compromised, or
# simply wrong, for an hour -- independent of which invoice it touches.
BUDGET_WINDOW_SECONDS = float(os.getenv("CLEARDUE_MCP_BUDGET_WINDOW", 3600))
BUDGET_MAX_CALLS = int(os.getenv("CLEARDUE_MCP_BUDGET_CALLS", 12))
BUDGET_MAX_PAISE = int(os.getenv("CLEARDUE_MCP_BUDGET_PAISE", 2_000_000_00))


def _guardrails_on() -> bool:
    return os.getenv("CLEARDUE_GUARDRAILS", "on").strip().lower() != "off"


def apply_rules(gateway: PolicyGateway) -> PolicyGateway:
    """Attach ClearDue's rules to a gateway. Configures and returns it.

    The `obligation` hook is what reconciles this rail against ClearDue's
    OWN tools: a discount granted by the native `offer_settlement` and a
    payment link created here draw against the same per-invoice budget, so
    the two paths can no longer be played off against each other. Everything
    above it is per-tool and per-call; this is the only cross-path check.
    See agent/mcp_obligation.py.
    """
    return gateway.configure(
        obligation=obligation_hook,
        global_policies=[ToolAllowlist(allowed=ALLOWED_TOOLS, enabled=_guardrails_on)],
        rules=[
            ToolRule(
                match="create_payment_link",
                policies=[
                    NumericBounds(
                        field="amount",
                        min_value=1,
                        max_value=MAX_LINK_PAISE,
                        enabled=_guardrails_on,
                    ),
                    WindowedBudget(
                        history=gateway.history_for("create_payment_link"),
                        window_seconds=BUDGET_WINDOW_SECONDS,
                        max_calls=BUDGET_MAX_CALLS,
                        max_amount=BUDGET_MAX_PAISE,
                        amount_field="amount",
                        enabled=_guardrails_on,
                    ),
                ],
            ),
        ],
    )

"""Wires ClearDue's obligation ledger onto Razorpay's MCP tool catalog --
the piece that makes a concession granted by a NATIVE tool and a payment
link created on the REAL Razorpay rail draw against the same invoice budget.

This closes a limitation this project carried and stated plainly: the
cross-tool stacking guard that fixed Goal 7 only ever applied to ClearDue's
own tools, because Razorpay's MCP `create_payment_link` takes
`{amount, currency, description}` and has no invoice concept for a
per-invoice ledger to key against.

Three resolutions were needed, and none of them belonged in the ledger:

1. WHICH invoice an MCP call draws against. The session is already bound to
   exactly one invoice, and that binding already rides into the gateway as
   InjectedState (mcp_gateway/langchain_tools.py puts it there specifically
   so the model can't see or spoof it). So the binding IS the answer, and it
   is the trustworthy one -- it comes from the server, not from an argument
   the model wrote. The description is only a fallback, and only because
   ClearDue itself writes "ClearDue collections -- {invoice_id}" into it
   (agent/razorpay_client.py), so the pattern is this project's own
   convention, not a guess about Razorpay's format.

2. WHAT that invoice is worth -- a mock_ledger lookup, ClearDue-specific by
   definition.

3. UNITS. Razorpay's MCP schema is paise; the ledger's interface is rupees.
   Converted declaratively via amount_scale rather than by hand at a call
   site, because a silent unit mismatch between an enforcement layer and an
   execution layer is exactly the seam this project's flagship cumulative-cap
   bug hid in.

FAIL-CLOSED, deliberately. If a money-moving MCP call matches a spec but its
invoice can't be resolved, it is DENIED, not waved through. An unattributable
payment link is precisely the un-auditable case the whole project exists to
prevent -- and the same reasoning already set the precedent in Phase 4, where
POST /api/webhooks/razorpay refuses outright rather than accepting unsigned
payloads. Read-only tools (fetch_*) match no spec at all and are unaffected;
they promise a customer nothing, so there is no exposure to reserve.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from mcp_gateway import ObligationOutcome
from policy_engine.obligation_ledger import ObligationError
from policy_engine.obligation_mapping import (
    ObligationMapper,
    ToolObligationSpec,
    UnresolvedObligation,
)

from . import mock_ledger, obligation

# Razorpay's MCP amounts are paise; ObligationLedger's interface is rupees.
_PAISE_TO_RUPEES = 0.01

# Only create_payment_link draws against budget. create_order does too in
# principle, but it isn't on ClearDue's allowlist (agent/mcp_rules.py), so
# specifying it here would be dead code claiming coverage it never exercises.
# Refunds/settlements are absent for the same reason plus a stronger one: a
# receivables agent disburses nothing, so they're allowlist-denied outright.
SPECS = [
    ToolObligationSpec(
        match="create_payment_link",
        entry_type="collection",
        amount_field="amount",
        amount_scale=_PAISE_TO_RUPEES,
    ),
]

_INVOICE_IN_DESCRIPTION = re.compile(r"\b(INV\d+)\b")


def _guardrails_on() -> bool:
    return os.getenv("CLEARDUE_GUARDRAILS", "on").strip().lower() != "off"


def _resolve_invoice_id(
    tool_name: str, args: dict[str, Any], state: dict[str, Any]
) -> str | None:
    """Server-supplied binding first, model-supplied text only as a fallback.

    That order is the point. `bound_invoice_id` is injected by the graph and
    invisible to the model, so it cannot be spoofed by anything a debtor
    talks the agent into writing. The description CAN be, which is why it is
    never preferred over the binding -- it only helps when there is no
    binding at all (a direct gateway call outside a chat turn).
    """
    bound = state.get("bound_invoice_id")
    if bound:
        return str(bound).strip().upper()
    match = _INVOICE_IN_DESCRIPTION.search(str(args.get("description") or ""))
    return match.group(1) if match else None


def _outstanding_for(invoice_id: str) -> float | None:
    invoice = mock_ledger.get_invoice(invoice_id)
    return invoice["amount"] if invoice else None


mapper = ObligationMapper(
    specs=SPECS,
    resolve_record_id=_resolve_invoice_id,
    get_outstanding=_outstanding_for,
)


@dataclass
class _Handle:
    """What the gateway holds between reserve() and commit()/release()."""

    invoice_id: str
    entry_id: str


class LedgerObligationHook:
    """Adapts ObligationLedger to mcp_gateway's ObligationHook protocol."""

    def reserve(
        self, tool_name: str, args: dict[str, Any], state: dict[str, Any]
    ) -> ObligationOutcome:
        # Same toggle every other guardrail in this project respects, so
        # CLEARDUE_GUARDRAILS=off still demonstrates the exact failure mode
        # these prevent -- including, now, cross-path stacking.
        if not _guardrails_on():
            return ObligationOutcome()
        try:
            mapped = mapper.map_call(tool_name, args, state)
        except UnresolvedObligation as e:
            return ObligationOutcome(
                rejection=(
                    f"{e.reason}. A payment link that cannot be tied to a specific "
                    "invoice cannot be reconciled against what that invoice is owed, "
                    "so it is refused rather than created unattributed."
                ),
                reason_code="OBLIGATION_UNRESOLVED",
            )
        if mapped is None:
            return ObligationOutcome()  # draws against nothing -- e.g. a fetch_*

        try:
            entry_id = obligation.ledger.reserve(
                mapped.record_id,
                f"mcp:{tool_name}",
                mapped.entry_type,
                mapped.amount,
                mapped.outstanding_amount,
            )
        except ObligationError as e:
            return ObligationOutcome(rejection=str(e), reason_code=e.reason_code)
        return ObligationOutcome(handle=_Handle(mapped.record_id, entry_id))

    def commit(self, handle: _Handle) -> None:
        obligation.ledger.commit(handle.invoice_id, handle.entry_id)

    def release(self, handle: _Handle) -> None:
        obligation.ledger.release(handle.invoice_id, handle.entry_id)


hook = LedgerObligationHook()

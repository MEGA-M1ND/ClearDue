"""Regression tests for policy_engine/obligation_mapping.py and
agent/mcp_obligation.py -- the resolver that lets the obligation ledger
guard Razorpay's REAL MCP tool schema, which has no invoice_id concept at
all. See agent/mcp_obligation.py's module docstring for why this exists.

The scenario these tests exist to prevent regressing: a 10% discount granted
by the NATIVE offer_settlement tool, followed by a full-value payment link
created on the REAL Razorpay MCP rail -- each individually legal on its own
path, and only wrong once both are known about together. Before this
module, the MCP path had no way to even find out an invoice existed, let
alone what it already owed.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import mcp_obligation as mo  # noqa: E402
from agent import obligation, session  # noqa: E402
from agent.mcp_rules import apply_rules  # noqa: E402
from mcp_gateway import PolicyGateway  # noqa: E402
from policy_engine.obligation_mapping import UnresolvedObligation  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_scope():
    session.set_current(uuid.uuid4().hex)
    yield


class _StubConnection:
    """Stands in for a real MCP transport. Records exactly what reached it,
    so a test can assert a denial never touched anything, the same proof
    reports/mcp_policy_gateway.md uses against the real bundled server."""

    tools = [
        {"name": "create_payment_link", "description": "x", "inputSchema": {}},
        {"name": "fetch_payment", "description": "x", "inputSchema": {}},
    ]

    def __init__(self, raise_on_call: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_on_call

    def call(self, name, args):
        self.calls.append((name, args))
        if self._raise:
            raise RuntimeError("simulated transport failure")
        return "plink_stub"


def _gateway(**kw) -> tuple[PolicyGateway, _StubConnection]:
    conn = _StubConnection(**kw)
    return apply_rules(PolicyGateway(conn)), conn


# --------------------------------------------------------------------------
# ObligationMapper -- resolution in isolation, no gateway involved
# --------------------------------------------------------------------------


def test_read_only_tool_maps_to_nothing():
    assert mo.mapper.map_call("fetch_payment", {"payment_id": "pay_1"}, {"bound_invoice_id": "INV1001"}) is None


def test_maps_via_injected_state_converting_paise_to_rupees():
    mapped = mo.mapper.map_call(
        "create_payment_link", {"amount": 4_500_000}, {"bound_invoice_id": "INV1001"}
    )
    assert mapped.record_id == "INV1001"
    assert mapped.amount == pytest.approx(45_000.0)
    assert mapped.outstanding_amount == pytest.approx(45_000.0)


def test_falls_back_to_description_when_no_binding():
    mapped = mo.mapper.map_call(
        "create_payment_link",
        {"amount": 100, "description": "ClearDue collections -- INV1002"},
        {},
    )
    assert mapped.record_id == "INV1002"


def test_injected_state_wins_over_a_conflicting_description():
    """The server-supplied binding is the trustworthy source specifically
    because the model cannot see or set it -- it must never lose to text a
    model wrote, even text that looks like a different, well-formed invoice
    id. Regression target: a debtor-authored description overriding the
    real binding would reopen exactly the spoofing SessionBound exists to
    close on the native tools."""
    mapped = mo.mapper.map_call(
        "create_payment_link",
        {"amount": 100, "description": "ClearDue collections -- INV1002"},
        {"bound_invoice_id": "INV1001"},
    )
    assert mapped.record_id == "INV1001"


def test_unresolvable_call_raises_not_returns_none():
    """A money-moving call with no way to attribute it is a materially
    different situation from a read-only call -- collapsing them to the same
    return value would be a fail-open hole. Must raise, not return None."""
    with pytest.raises(UnresolvedObligation):
        mo.mapper.map_call("create_payment_link", {"amount": 100}, {})


# --------------------------------------------------------------------------
# Through the real gateway -- proves the wiring, not just the mapper
# --------------------------------------------------------------------------


def test_cross_path_stacking_blocked():
    """The scenario this whole module exists for: native concession commits
    first, then the SAME invoice's remaining budget is checked -- correctly
    -- against a payment link created on the MCP rail."""
    invoice_id, outstanding = "INV1002", 120_000.0
    eid = obligation.ledger.reserve(invoice_id, "offer_settlement", "concession", 12_000.0, outstanding)
    obligation.ledger.commit(invoice_id, eid)

    gw, conn = _gateway()
    decision = gw.call(
        "create_payment_link", {"amount": 12_000_000}, state={"bound_invoice_id": invoice_id}
    )

    assert decision.rejected
    assert decision.error_code == "COLLECTION_BUDGET_EXCEEDED"
    assert conn.calls == [], "a denied call must never reach the transport"


def test_correctly_discounted_link_still_succeeds():
    """Proves the previous test is a real budget check, not a blanket
    second-call denial: the amount that actually fits must go through and
    commit."""
    invoice_id, outstanding = "INV1002", 120_000.0
    eid = obligation.ledger.reserve(invoice_id, "offer_settlement", "concession", 12_000.0, outstanding)
    obligation.ledger.commit(invoice_id, eid)

    gw, conn = _gateway()
    decision = gw.call(
        "create_payment_link", {"amount": 10_800_000}, state={"bound_invoice_id": invoice_id}
    )

    assert decision.allowed
    assert len(conn.calls) == 1
    budget = obligation.ledger.get_available_budget(invoice_id, outstanding)
    assert budget["collection_budget"] == pytest.approx(0.0)


def test_read_only_fetch_never_reserves():
    invoice_id = "INV1001"
    before = obligation.ledger.get_available_budget(invoice_id, 45_000.0)
    gw, conn = _gateway()
    decision = gw.call("fetch_payment", {"payment_id": "pay_1"}, state={"bound_invoice_id": invoice_id})
    after = obligation.ledger.get_available_budget(invoice_id, 45_000.0)

    assert decision.allowed
    assert before == after


def test_unattributable_link_denied_fail_closed():
    """No binding, no attributable description -- fail CLOSED. An
    un-auditable payment link is exactly the outcome this project's audit
    trail exists to prevent, so this must deny, not pass through."""
    gw, conn = _gateway()
    decision = gw.call("create_payment_link", {"amount": 500_000}, state={})

    assert decision.rejected
    assert decision.error_code == "OBLIGATION_UNRESOLVED"
    assert conn.calls == []


def test_transport_failure_releases_the_reservation():
    """The obligation ledger's own reserve-before-execute payoff, now
    proven on the MCP path too: if the transport call itself fails AFTER
    budget was claimed, the budget must come back -- otherwise a single
    network error would permanently (and wrongly) shrink an invoice's
    exposure limit forever."""
    invoice_id, outstanding = "INV1003", 650_000.0
    before = obligation.ledger.get_available_budget(invoice_id, outstanding)

    gw, conn = _gateway(raise_on_call=True)
    with pytest.raises(RuntimeError):
        gw.call("create_payment_link", {"amount": 10_000_000}, state={"bound_invoice_id": invoice_id})

    after = obligation.ledger.get_available_budget(invoice_id, outstanding)
    assert before == after


def test_guardrails_off_disables_cross_path_check_too():
    """CLEARDUE_GUARDRAILS=off is meant to demonstrate the exact failure
    mode the guardrail prevents, on every path uniformly -- not leave one
    path silently still protected while the demo claims otherwise."""
    import os

    invoice_id, outstanding = "INV1002", 120_000.0
    eid = obligation.ledger.reserve(invoice_id, "offer_settlement", "concession", 12_000.0, outstanding)
    obligation.ledger.commit(invoice_id, eid)

    old = os.environ.get("CLEARDUE_GUARDRAILS")
    os.environ["CLEARDUE_GUARDRAILS"] = "off"
    try:
        gw, conn = _gateway()
        decision = gw.call(
            "create_payment_link", {"amount": 12_000_000}, state={"bound_invoice_id": invoice_id}
        )
    finally:
        if old is None:
            os.environ.pop("CLEARDUE_GUARDRAILS", None)
        else:
            os.environ["CLEARDUE_GUARDRAILS"] = old

    assert decision.allowed, "guardrails=off must reproduce the over-commit, not silently still block it"
    assert len(conn.calls) == 1

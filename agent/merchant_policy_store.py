"""Wires policy_engine's transport-free MerchantPolicyStore to agent/store.py's
global (not demo-scoped) storage, and seeds the one merchant this demo has
from mock_ledger's original hardcoded values -- same role agent/obligation.py
and agent/mcp_rules.py play for their respective modules.

ACTIVE_MERCHANT_ID is a stand-in for real multi-tenancy, which this project
doesn't have: every invoice in mock_ledger.py belongs to the same merchant.
The infrastructure here (versioned, editable, independently fetchable
policy) doesn't depend on that changing -- a second merchant is just a
second seed() call away -- but nothing currently routes an invoice to a
merchant other than this one.
"""

from __future__ import annotations

from policy_engine.merchant_policy import (
    ContactRules,
    MerchantPolicy,
    MerchantPolicyStore,
    PerWindowLimits,
    now_iso,
)

from . import mock_ledger, store

ACTIVE_MERCHANT_ID = "MERCHANT_DEFAULT"


class _StoreBackend:
    def get_global(self, key: str) -> str | None:
        return store.get_global(key)

    def set_global(self, key: str, value: str) -> None:
        store.set_global(key, value)


_store = MerchantPolicyStore(backend=_StoreBackend())


def _default_policy() -> MerchantPolicy:
    """The starting policy, seeded from mock_ledger.MERCHANT_POLICY's values
    so a fresh deployment behaves exactly as every prior phase did until
    someone explicitly PUTs a change. mock_ledger.MERCHANT_POLICY itself is
    left untouched (still read by /debug/policy) -- this is a genuinely
    separate, independently-editable copy, not an alias."""
    base = mock_ledger.MERCHANT_POLICY
    return MerchantPolicy(
        merchant_id=ACTIVE_MERCHANT_ID,
        policy_version="1.0.0",
        created_at=now_iso(),
        merchant_name=base["merchant_name"],
        settlement_floor_pct=base["settlement_floor_pct"],
        max_autonomous_discount_pct=base["max_autonomous_discount_pct"],
        max_installments=base["max_installments"],
        escalation_threshold_inr=base["escalation_threshold"],
        payment_link_cap_per_invoice=3,
        tool_allowlist=[
            "get_invoice_status", "get_customer_payment_history", "send_message",
            "offer_settlement", "create_payment_link", "mark_paid",
            "escalate_to_human", "revoke_consent",
        ],
        per_window_limits=PerWindowLimits(),
        contact_rules=ContactRules(channels_allowed=list(base["allowed_channels"])),
    )


def ensure_seeded() -> MerchantPolicy:
    return _store.seed_if_absent(_default_policy())


def get_policy(merchant_id: str = ACTIVE_MERCHANT_ID) -> MerchantPolicy | None:
    if merchant_id == ACTIVE_MERCHANT_ID:
        ensure_seeded()
    return _store.get(merchant_id)


def save_policy(policy: MerchantPolicy) -> None:
    _store.save(policy)


def next_version(current: str) -> str:
    return _store.next_version(current)

"""A merchant's guardrail configuration as a versioned, editable object,
rather than a constant baked into the code at import time.

Before this, every threshold an agent's tools enforce -- discount cap,
installment count, escalation threshold, allowed channels -- lived in one
hardcoded dict, read once and fixed for the life of the process. That is
fine for a single demo merchant and nothing else; it cannot answer "what did
this merchant's policy actually allow last Tuesday" (no version), and it
cannot be changed without a redeploy (no edit path). This module is both:
a `MerchantPolicy` a caller can fetch and edit, and each edit produces a new
`policy_version` rather than silently overwriting history.

Like the rest of policy_engine/, this imports nothing ClearDue-specific and
nothing transport-specific -- it doesn't know what an invoice is, and it
doesn't know whether its backend is Redis or a plain dict.
agent/merchant_policy_store.py is where ClearDue actually wires it in.

This is deliberately GLOBAL config, not per-demo-session state. A merchant's
policy is not something a visitor's "Reset demo state" button should be able
to touch -- that would let anyone quietly weaken (or strengthen) the
guardrails a completely different visitor's negotiation is running against.
See agent/store.py's get_global/set_global for the unscoped storage that
backs this, the same category rate-limit counters already use.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class PerWindowLimits:
    window_minutes: int = 60
    max_calls: int = 20
    max_amount_inr: float = 1_000_000.0


@dataclass
class ContactRules:
    channels_allowed: list[str] = field(default_factory=lambda: ["whatsapp", "email"])
    require_opt_in: bool = True
    max_contacts_per_day: int = 2


@dataclass
class MerchantPolicy:
    merchant_id: str
    policy_version: str
    created_at: str
    merchant_name: str = "Unnamed Merchant"
    settlement_floor_pct: float = 85.0
    max_autonomous_discount_pct: float = 15.0
    max_installments: int = 2
    escalation_threshold_inr: float = 500_000.0
    payment_link_cap_per_invoice: int = 3
    tool_allowlist: list[str] = field(default_factory=list)
    per_window_limits: PerWindowLimits = field(default_factory=PerWindowLimits)
    contact_rules: ContactRules = field(default_factory=ContactRules)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "MerchantPolicy":
        d = dict(d)
        if isinstance(d.get("per_window_limits"), dict):
            d["per_window_limits"] = PerWindowLimits(**d["per_window_limits"])
        if isinstance(d.get("contact_rules"), dict):
            d["contact_rules"] = ContactRules(**d["contact_rules"])
        return cls(**d)


class MerchantPolicyBackend(Protocol):
    """What this module needs from storage: unscoped (global) get/set. See
    agent/merchant_policy_store.py for the adapter onto agent/store.py."""

    def get_global(self, key: str) -> str | None: ...
    def set_global(self, key: str, value: str) -> None: ...


class MerchantPolicyStore:
    def __init__(self, backend: MerchantPolicyBackend):
        self._backend = backend

    @staticmethod
    def _key(merchant_id: str) -> str:
        return f"merchant_policy:{merchant_id}"

    def get(self, merchant_id: str) -> MerchantPolicy | None:
        import json

        raw = self._backend.get_global(self._key(merchant_id))
        if raw is None:
            return None
        return MerchantPolicy.from_json(json.loads(raw))

    def save(self, policy: MerchantPolicy) -> None:
        import json

        self._backend.set_global(self._key(policy.merchant_id), json.dumps(policy.to_json()))

    def seed_if_absent(self, policy: MerchantPolicy) -> MerchantPolicy:
        """Write `policy` only if nothing is stored for its merchant_id yet.
        Idempotent across restarts -- calling this every time the process
        boots does not clobber an operator's live edit with the hardcoded
        default again."""
        existing = self.get(policy.merchant_id)
        if existing is not None:
            return existing
        self.save(policy)
        return policy

    def next_version(self, current: str) -> str:
        """Bump the patch component of a semver string. Not a general semver
        library -- this project has exactly one place that mints new
        versions (a PUT), and "increment patch" is all that needs to mean."""
        try:
            major, minor, patch = (int(p) for p in current.split("."))
            return f"{major}.{minor}.{patch + 1}"
        except (ValueError, AttributeError):
            return "1.0.0"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

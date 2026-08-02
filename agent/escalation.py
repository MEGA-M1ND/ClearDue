"""Human review queue for invoices the agent escalated instead of acting on
its own -- closes the gap EVALUATION.md's Goal 4 called out: "escalation is
enforced and recorded as an action, but there is no human review queue --
no case object, no approve/reject endpoint."

An EscalationCase is created whenever escalate_to_human actually executes
(see agent/agent.py) -- that is the real, ground-truth signal that a human
needs to look at something, not a hypothetical policy-decision type. Cases
are demo-scoped like the rest of a visitor's negotiation (action_log,
obligation ledger, invoice overlay): they belong to one visitor's session
and are cleared by that visitor's own Reset, unlike merchant policy, which
is deliberately global.

Honest limitation: approving or rejecting a case records a human decision.
It does not resume or execute whatever the original negotiation was
heading toward -- there is no mechanism here that turns an APPROVED case
back into, say, an actually-applied discount. A real implementation of that
would need the agent's own turn loop to pause and later resume with the
reviewer's decision in hand (LangGraph's `interrupt()` is built for exactly
this), which is a materially bigger feature than a queue with two buttons
and deliberately out of scope for this phase.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from . import session, store

Status = str  # "PENDING" | "APPROVED" | "REJECTED"


@dataclass
class EscalationCase:
    case_id: str
    invoice_id: str
    merchant_id: str
    session_id: str
    proposed_action: dict[str, Any]
    policy_rationale: str
    invoice_state: dict[str, Any]
    status: Status
    created_at: str
    policy_decision_receipt_id: str | None = None
    reviewer_decision: str | None = None
    approval_record: dict[str, Any] | None = None
    resolved_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "EscalationCase":
        return cls(**d)


_KEY = "escalations"


def create_case(
    invoice_id: str,
    merchant_id: str,
    proposed_action: dict[str, Any],
    policy_rationale: str,
    invoice_state: dict[str, Any],
    policy_decision_receipt_id: str | None = None,
) -> EscalationCase:
    case = EscalationCase(
        case_id=uuid.uuid4().hex,
        invoice_id=invoice_id,
        merchant_id=merchant_id,
        session_id=session.current(),
        proposed_action=proposed_action,
        policy_rationale=policy_rationale,
        invoice_state=invoice_state,
        status="PENDING",
        created_at=_now(),
        policy_decision_receipt_id=policy_decision_receipt_id,
    )
    store.hset(_KEY, case.case_id, case.to_json())
    return case


def list_cases(status: Status | None = None) -> list[dict[str, Any]]:
    cases = [EscalationCase.from_json(v).to_json() for v in store.hgetall(_KEY).values()]
    if status:
        cases = [c for c in cases if c["status"] == status]
    return sorted(cases, key=lambda c: c["created_at"], reverse=True)


def get_case(case_id: str) -> dict[str, Any] | None:
    raw = store.hgetall(_KEY).get(case_id)
    return EscalationCase.from_json(raw).to_json() if raw else None


def _resolve(case_id: str, decision: Status, note: str | None) -> dict[str, Any] | None:
    raw = store.hgetall(_KEY).get(case_id)
    if raw is None:
        return None
    case = EscalationCase.from_json(raw)
    if case.status != "PENDING":
        return case.to_json()  # already resolved -- idempotent, not an error
    case.status = decision
    case.reviewer_decision = decision
    case.approval_record = {"decided_at": _now(), "decision": decision, "note": note or ""}
    case.resolved_at = _now()
    store.hset(_KEY, case_id, case.to_json())
    return case.to_json()


def approve(case_id: str, note: str | None = None) -> dict[str, Any] | None:
    return _resolve(case_id, "APPROVED", note)


def reject(case_id: str, note: str | None = None) -> dict[str, Any] | None:
    return _resolve(case_id, "REJECTED", note)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

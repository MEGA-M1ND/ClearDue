"""A versioned, individually-addressable record of one policy decision.

policy_engine/core.py's audit_log already records every decision -- allowed
and denied -- as a list, append-only, read as a whole page. That is the
right shape for "show me everything that happened in this session," which
is what the Policy Audit tab needs. It is the wrong shape for "give me
receipt abc123 specifically" or "what policy VERSION was active when this
decision was made" -- a plain list has no per-entry identity and no
versioning. PolicyDecisionReceipt is that identity: every entry core.py's
_record() builds now also gets a receipt_id, a policy_version snapshot, and
a fingerprint of the exact inputs that were judged, so a decision from six
edits ago can still be looked up and its rationale reconstructed exactly.

This module only defines the shape and a fingerprint helper -- it does not
decide storage or where merchant_id/policy_version come from. core.py's
_record() builds the receipt and writes it through the SAME pluggable
backend the audit log already uses (see use_audit_backend), so a receipt
and its corresponding audit-log entry can never drift out of sync -- they
are the same dict, written twice for two different access patterns (append
for the audit trail, indexed hash for by-id lookup), not two independently
maintained logs that happen to agree today.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

Decision = str  # "ALLOW" | "DENY" | "ESCALATE"


@dataclass
class PolicyDecisionReceipt:
    receipt_id: str
    policy_version: str
    merchant_id: str
    tool_name: str
    inputs_fingerprint: str
    decision: Decision
    reason_code: str | None
    reason_text: str | None
    timestamp: str
    session_id: str = ""
    execution_receipt_id: str | None = None
    obligation_snapshot: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def fingerprint(args: dict[str, Any]) -> str:
    """A stable hash of a tool call's inputs, excluding `state` (the
    InjectedState dict -- never model-visible, and including it would make
    the same logical call fingerprint differently depending on unrelated
    session internals)."""
    payload = {k: v for k, v in args.items() if k != "state"}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def new_receipt(
    tool_name: str,
    args: dict[str, Any],
    decision: Decision,
    reason_code: str | None,
    reason_text: str | None,
    merchant_id: str,
    policy_version: str,
    session_id: str = "",
) -> PolicyDecisionReceipt:
    return PolicyDecisionReceipt(
        receipt_id=uuid.uuid4().hex,
        policy_version=policy_version,
        merchant_id=merchant_id,
        tool_name=tool_name,
        inputs_fingerprint=fingerprint(args),
        decision=decision,
        reason_code=reason_code,
        reason_text=reason_text,
        timestamp=_now(),
        session_id=session_id,
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

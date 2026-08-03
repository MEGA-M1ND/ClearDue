"""The guarded() decorator and the small set of types it depends on.

Every check in agent/agent.py started as an inline `if` inside a tool
function -- correct, but not reusable, and easy to forget when writing the
NEXT tool. That is exactly what happened: create_payment_link shipped
without the cumulative-tracking check offer_settlement already had, and an
autonomous adversarial run found the gap before a human did (see
adversary/orchestrator.py's payment_link_overcollect result). This module
extracts that pattern into small, composable, independently testable
Policy objects any tool -- in this agent or a different one -- can declare
against with @guarded(...), so the next tool inherits protection instead of
needing it copy-pasted in.

This is close to verbatim what Razorpay's own Agent Studio principles ask
for: "every agent action passes through platform-level validation... before
execution." That line is the actual design brief this module implements.
"""

from __future__ import annotations

import functools
import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .decision_receipt import PolicyDecisionReceipt, new_receipt


@dataclass
class PolicyContext:
    """Everything a policy needs to decide, gathered once per tool call."""

    tool_name: str
    args: dict[str, Any]  # every argument the tool was called with, by name
    state: dict[str, Any]  # the InjectedState dict, if the tool has one


@dataclass
class PolicyResult:
    allowed: bool
    reason: str | None = None
    # A stable, machine-checkable category for the denial (e.g. "not_found",
    # "not_authorized", "cap_exceeded"). Exists so a caller that needs to
    # react differently to different denial kinds (get_invoice_status turns
    # this into {"error": ..., "detail": ...} rather than a plain string)
    # doesn't have to string-match the human-readable `reason` to do it --
    # the original version of this code did exactly that
    # ("not authorized" if "bound to" in error else "not found"), which
    # breaks silently the moment anyone rewords a message.
    error_code: str | None = None

    @classmethod
    def allow(cls) -> "PolicyResult":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str, error_code: str | None = None) -> "PolicyResult":
        return cls(allowed=False, reason=reason, error_code=error_code)


class Policy(Protocol):
    """A single, independently testable rule. Policies decide; they never
    have side effects and never execute the underlying action themselves."""

    name: str

    def check(self, ctx: PolicyContext) -> PolicyResult: ...


# ---------------------------------------------------------------------------
# Audit log -- every decision, not just the successful ones.
#
# Deliberately separate from the target agent's own action log (which only
# ever records completed actions). Razorpay's stated principle is a "full
# audit trail" -- a trail that shows only what succeeded is not full. A
# merchant reviewing this agent should be able to see every attempt a policy
# blocked and why, not just the ones that got through.
#
# Storage is pluggable (`use_audit_backend`) rather than hard-wired to a
# Redis client, so this module keeps zero required dependencies beyond the
# standard library -- staying genuinely reusable by a different agent, not
# just ClearDue-shaped. Defaults to a plain in-memory list; ClearDue points
# it at agent/store.py (Redis-backed when deployed) at startup.
# ---------------------------------------------------------------------------

audit_log: list[dict[str, Any]] = []
_audit_backend: Any = None

# Resolves (merchant_id, policy_version, session_id) for every receipt this
# module mints. Pluggable for the same reason use_audit_backend is: this
# module must not import agent.merchant_policy_store or agent.session
# directly (the "no ClearDue-specific imports" rule policy_engine/ follows
# throughout). Unconfigured, receipts still get minted -- just stamped
# "unknown"/"unversioned" rather than left out, so a caller that never
# bothers to configure this still gets a working (if less informative)
# receipt rather than a crash.
_receipt_context: Callable[[], tuple[str, str, str]] = lambda: ("unknown", "unversioned", "")


def use_audit_backend(backend: Any) -> None:
    """Redirect the audit log to an external backend exposing
    append_audit(record) / list_audit() -> list[dict], and OPTIONALLY
    save_receipt(receipt_id, record) / list_receipts() -> list[dict] for
    per-receipt lookup (see policy_engine/decision_receipt.py). The receipt
    methods are duck-typed, not required -- a backend that only implements
    the original audit contract still works, it just won't support
    GET /api/receipts/{id} lookups. Optional either way; call site's
    responsibility to also handle resetting the backend."""
    global _audit_backend
    _audit_backend = backend


def use_receipt_context(fn: Callable[[], tuple[str, str, str]]) -> None:
    global _receipt_context
    _receipt_context = fn


def _record(
    tool_name: str, call_args: dict, result: PolicyResult, policy_name: str
) -> PolicyDecisionReceipt:
    merchant_id, policy_version, session_id = _receipt_context()
    receipt = new_receipt(
        tool_name=tool_name,
        args=call_args,
        decision="ALLOW" if result.allowed else "DENY",
        reason_code=result.error_code,
        reason_text=result.reason,
        merchant_id=merchant_id,
        policy_version=policy_version,
        session_id=session_id,
    )
    entry = {
        "tool": tool_name,
        "args": {k: v for k, v in call_args.items() if k != "state"},
        "policy": policy_name,
        "allowed": result.allowed,
        "reason": result.reason,
        "ts": time.time(),
        # Everything above is unchanged from before receipts existed --
        # /debug/policy_log keeps working exactly as it always has. These
        # ride along on the SAME dict so the audit-log entry and the
        # indexed receipt can never drift out of sync with each other.
        "receipt_id": receipt.receipt_id,
        "policy_version": receipt.policy_version,
        "merchant_id": receipt.merchant_id,
        "inputs_fingerprint": receipt.inputs_fingerprint,
        "decision": receipt.decision,
    }
    if _audit_backend is not None:
        _audit_backend.append_audit(entry)
        save_receipt = getattr(_audit_backend, "save_receipt", None)
        if save_receipt is not None:
            save_receipt(receipt.receipt_id, entry)
    else:
        audit_log.append(entry)
    return receipt


def record_decision(
    tool_name: str, call_args: dict, result: PolicyResult, policy_name: str
) -> PolicyDecisionReceipt:
    """Public entry point for enforcement points that are not the guarded()
    decorator -- notably mcp_gateway, which intercepts MCP `tools/call`
    rather than wrapping a Python function. Same audit log either way, so a
    reviewer sees one trail regardless of which layer made the decision.
    Returns the receipt so a caller (mcp_gateway attaches receipt_id to its
    own execution records) can cross-reference exactly which decision
    authorized what actually ran."""
    return _record(tool_name, call_args, result, policy_name)


def list_audit_log() -> list[dict[str, Any]]:
    if _audit_backend is not None:
        return _audit_backend.list_audit()
    return list(audit_log)


def reset_audit_log() -> None:
    audit_log.clear()


def _default_on_deny(rejection_prefix: str) -> Callable[[PolicyResult], Any]:
    return lambda result: f"{rejection_prefix}: {result.reason}"


def guarded(
    policies: list[Policy],
    rejection_prefix: str = "REJECTED",
    on_deny: Callable[[PolicyResult], Any] | None = None,
):
    """Run every policy before the wrapped function executes.

    Stops at the first denial (fail-fast, same behaviour every hand-written
    check in this project already had). By default returns a rejection
    string, since that's the contract every action tool in this project
    already used -- pass `on_deny` for a tool whose success return type
    isn't a plain string (e.g. a lookup tool that returns a dict either way)
    so a denial still matches that same shape instead of silently changing
    the tool's return type.

    `sig.bind()` is used rather than assuming kwargs-only, so this works
    whether the caller (LangChain's tool-calling machinery, or a direct
    unit test) passes arguments positionally or by keyword.
    """
    on_deny = on_deny or _default_on_deny(rejection_prefix)

    def decorator(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)
            state = call_args.get("state") or {}

            ctx = PolicyContext(tool_name=fn.__name__, args=call_args, state=state)
            for policy in policies:
                result = policy.check(ctx)
                policy_name = getattr(policy, "name", type(policy).__name__)
                _record(fn.__name__, call_args, result, policy_name)
                if not result.allowed:
                    return on_deny(result)

            return fn(*args, **kwargs)

        # Exposes the ordered policy chain for introspection -- e.g.
        # agent/simulate.py's dry-run endpoint, which needs to run these same
        # checks against hypothetical arguments without executing `fn` or
        # writing to the audit log. Read-only; nothing here calls .check()
        # except the wrapper above and whatever introspects this list.
        wrapper.policies = policies

        return wrapper

    return decorator

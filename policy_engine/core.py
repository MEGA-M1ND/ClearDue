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


def use_audit_backend(backend: Any) -> None:
    """Redirect the audit log to an external backend exposing
    append_audit(record) and list_audit() -> list[dict]. Optional -- call
    site's responsibility to also handle resetting that backend."""
    global _audit_backend
    _audit_backend = backend


def _record(tool_name: str, call_args: dict, result: PolicyResult, policy_name: str) -> None:
    entry = {
        "tool": tool_name,
        "args": {k: v for k, v in call_args.items() if k != "state"},
        "policy": policy_name,
        "allowed": result.allowed,
        "reason": result.reason,
        "ts": time.time(),
    }
    if _audit_backend is not None:
        _audit_backend.append_audit(entry)
    else:
        audit_log.append(entry)


def record_decision(
    tool_name: str, call_args: dict, result: PolicyResult, policy_name: str
) -> None:
    """Public entry point for enforcement points that are not the guarded()
    decorator -- notably mcp_gateway, which intercepts MCP `tools/call`
    rather than wrapping a Python function. Same audit log either way, so a
    reviewer sees one trail regardless of which layer made the decision."""
    _record(tool_name, call_args, result, policy_name)


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

        return wrapper

    return decorator

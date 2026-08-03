"""Dry-runs a native tool's declared policy chain against hypothetical
arguments -- lets a reviewer see exactly which guardrail would ALLOW or DENY
a call, and why, without going through an LLM turn and without executing the
tool, the obligation ledger, or the real audit log.

Every check here is the SAME Policy object `agent/agent.py` wired onto the
real tool via `@guarded(...)` -- this module never reimplements a rule, it
only reads `fn.policies` (see policy_engine/core.py's `guarded()`) and runs
them in the same fail-fast order the real decorator does. If a policy is
added, removed, or reordered on a tool, this endpoint reflects that on its
next call automatically; there is nothing here to keep in sync by hand.

Deliberately does not call `policy_engine.core.record_decision` -- a
simulated call is not a real (or even LLM-attempted) action, and writing it
to the same audit trail /debug/policy_log and GET /api/receipts read from
would misrepresent the record: a merchant reviewing "every decision this
agent made" should never see one that was never actually asked of the agent.
"""

from __future__ import annotations

from typing import Any

from policy_engine.core import PolicyContext

from . import agent as agent_module

_TOOLS_BY_NAME = {t.name: t for t in agent_module.TOOLS}


class UnknownToolError(Exception):
    """Raised for a tool name that isn't one of ClearDue's native tools --
    keeps a typo'd name from silently simulating against an empty policy
    list and reporting a false ALLOW."""


def available_tools() -> dict[str, list[str]]:
    """tool_name -> the argument names a caller must supply. Never includes
    `state` -- that is injected server-side for a real call (the model can't
    see or set it either), and here it's built from `bound_invoice_id`
    below, not accepted as a free-form argument."""
    return {name: [a for a in t.args.keys() if a != "state"] for name, t in _TOOLS_BY_NAME.items()}


def simulate(tool_name: str, args: dict[str, Any], bound_invoice_id: str | None) -> dict[str, Any]:
    """Run every policy `tool_name` is decorated with, in order, stopping at
    the first denial -- identical fail-fast semantics to `guarded()`'s real
    wrapper. `bound_invoice_id`, when given, simulates a session already
    bound to that invoice (the same state `SessionBound`/`ConsentRequired`
    read for a real call), so a reviewer can specifically test the
    authorization guardrail: does a call targeting a DIFFERENT invoice than
    the one this hypothetical session is bound to get denied?
    """
    tool = _TOOLS_BY_NAME.get(tool_name)
    if tool is None:
        raise UnknownToolError(f"{tool_name!r} is not a ClearDue tool. Known tools: {sorted(_TOOLS_BY_NAME)}")

    fn = tool.func  # the @guarded-wrapped function beneath LangChain's StructuredTool
    policies = getattr(fn, "policies", [])

    state = {"bound_invoice_id": bound_invoice_id} if bound_invoice_id else {}
    ctx = PolicyContext(tool_name=tool_name, args={**args, "state": state}, state=state)

    checks: list[dict[str, Any]] = []
    decision, denied_by, reason = "ALLOW", None, None
    for policy in policies:
        result = policy.check(ctx)
        policy_name = getattr(policy, "name", type(policy).__name__)
        checks.append({"policy": policy_name, "allowed": result.allowed, "reason": result.reason})
        if not result.allowed:
            decision, denied_by, reason = "DENY", policy_name, result.reason
            break

    return {
        "tool": tool_name,
        "args": args,
        "bound_invoice_id": bound_invoice_id,
        "decision": decision,
        "denied_by": denied_by,
        "reason": reason,
        "checks": checks,
        "note": "Simulated only -- no tool executed, no ledger reserved, no audit-log entry written.",
    }

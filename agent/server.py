"""FastAPI wrapper around the ClearDue collections agent.

Session/ledger storage goes through agent/store.py: plain in-memory locally,
Redis-backed (REDIS_URL or Upstash KV) when those env vars are set, same
switch PaySentry's target_agent/store.py uses. policy_engine's audit log is
pointed at the same backend at import time below, so a single Redis (or a
single in-memory process) is the one source of truth for everything a
Vercel deployment needs to survive across serverless instances.
"""

import json
import os
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from policy_engine.core import list_audit_log, use_audit_backend, use_receipt_context

from . import agent as agent_module
from . import escalation
from . import mcp_runtime
from . import merchant_policy_store
from . import mock_ledger
from . import razorpay_reconciler
from . import session
from . import rate_limit
from . import simulate as simulate_module
from . import store

use_audit_backend(store)
use_receipt_context(
    # get_policy() self-seeds for ACTIVE_MERCHANT_ID (see
    # merchant_policy_store.ensure_seeded()), so it never returns None here.
    lambda: (
        merchant_policy_store.ACTIVE_MERCHANT_ID,
        merchant_policy_store.get_policy().policy_version,
        session.current(),
    )
)

app = FastAPI(title="ClearDue Collections Agent", version="0.1.0")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(REPO_ROOT, "public", "index.html")
EVALUATION_UI_PATH = os.path.join(REPO_ROOT, "public", "evaluation.html")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    """Serve the demo chat UI -- same pattern as PaySentry: one static file,
    no build step, served directly by this app rather than a separate
    frontend project."""
    return FileResponse(UI_PATH)


@app.get("/evaluation", include_in_schema=False)
def evaluation_ui() -> Response:
    """`public/evaluation.html` has no bare-path static match on Vercel (only
    literal filenames like `/evaluation.html` are served by the static
    layer), so `/evaluation` falls through to this function -- and found,
    live, that Vercel's Python builder does not reliably put `public/` on
    the function's OWN filesystem the way `/`'s FileResponse assumes (that
    route never actually gets exercised in production, since `/` itself is
    resolved by the static layer before the function ever runs -- confirmed
    via `X-Vercel-Cache: HIT` on the live deployment). `/evaluation.html`
    itself, the literal filename, IS reliably served by that same static
    layer. Rather than depend on the function's filesystem having the file
    (`includeFiles` in vercel.json was tried first; it did not fix this),
    check locally and redirect to the proven-working static URL otherwise --
    correct either way this ever runs, on Vercel or off it."""
    if os.path.exists(EVALUATION_UI_PATH):
        return FileResponse(EVALUATION_UI_PATH)
    return RedirectResponse(url="/evaluation.html")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    # Which invoice this collections thread is about. Required to start a
    # new session; ignored on subsequent turns once a session is bound.
    invoice_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[dict[str, Any]]


def _client_ip(request: Request) -> str:
    # Vercel (and any proxy in front of this app) puts the real client IP
    # first in X-Forwarded-For; request.client.host would otherwise just be
    # the proxy's own address. Falls back to that for local dev, where
    # there's no proxy and the header is absent.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, response: Response) -> ChatResponse:
    session.bind(request, response)
    try:
        rate_limit.check_chat_rate_limit(_client_ip(request))
    except rate_limit.RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": str(e.retry_after)})

    if req.session_id:
        history = store.get_history(req.session_id)
        bound_invoice = store.get_bound_invoice(req.session_id)
        if req.invoice_id and not bound_invoice:
            bound_invoice = store.bind_invoice(req.session_id, req.invoice_id.strip().upper())
        result = agent_module.run_turn(history, req.message, bound_invoice)
        store.set_history(req.session_id, result["messages"])
    else:
        result = agent_module.run_turn([], req.message, req.invoice_id)
    return ChatResponse(response=result["response"], tool_calls=result["tool_calls"])


@app.get("/health")
def health(request: Request, response: Response) -> dict[str, Any]:
    session.bind(request, response)
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    model = os.getenv("OPENAI_MODEL", agent_module.DEFAULT_OPENAI_MODEL)
    return {
        "status": "ok",
        "sessions": store.session_count(),
        "provider": provider,
        "model": model,
        "guardrails": "on" if agent_module.GUARDRAILS_ENABLED else "off",
        "authz": "on" if agent_module.AUTHZ_ENABLED else "off",
        "storage": "redis" if store.USING_KV else "memory",
        "mcp": mcp_runtime.status(),
    }


# ---------------------------------------------------------------------------
# Debug/introspection -- ground truth for the adversary suite, same role as
# PaySentry's /debug endpoints. Not part of the simulated product surface.
#
# Optional guard, /debug/reset ONLY: if DEBUG_TOKEN is set in the environment,
# resetting requires a matching X-Debug-Token header. Unset (the default, and
# always the case for local dev) means fully open, exactly as every prior
# phase of this project. The read-only /debug/* endpoints stay open even
# when DEBUG_TOKEN is set -- the demo UI depends on them. The actual risk on
# a public deployment is /debug/reset: any visitor could otherwise wipe the
# ledger mid-demo for everyone else. That's what this protects.
# ---------------------------------------------------------------------------

_DEBUG_TOKEN = os.getenv("DEBUG_TOKEN")


def _check_debug_token(x_debug_token: str | None) -> None:
    if _DEBUG_TOKEN and not (x_debug_token and secrets.compare_digest(x_debug_token, _DEBUG_TOKEN)):
        raise HTTPException(status_code=401, detail="missing or incorrect X-Debug-Token")


@app.get("/debug/action_log")
def debug_action_log(request: Request, response: Response) -> dict[str, Any]:
    session.bind(request, response)
    actions = mock_ledger.list_actions()
    return {"count": len(actions), "actions": actions}


@app.get("/debug/ledger")
def debug_ledger(request: Request, response: Response) -> dict[str, Any]:
    """Current invoice/customer state as *this visitor* sees it, so the
    adversary suite can score violations mechanically instead of
    hand-computing expected values that could drift out of sync with
    mock_ledger.py. Reflects the caller's own overlay, not global state."""
    session.bind(request, response)
    return {"invoices": mock_ledger.all_invoices(), "customers": mock_ledger.all_customers()}


@app.get("/debug/policy")
def debug_policy() -> dict[str, Any]:
    """The merchant policy, so the adversary suite can compute violations
    without duplicating these numbers by hand."""
    return dict(mock_ledger.MERCHANT_POLICY)


@app.get("/debug/policy_log")
def debug_policy_log(request: Request, response: Response) -> dict[str, Any]:
    """Every policy decision -- allowed AND denied -- not just the actions
    that actually happened. mock_ledger's action_log only ever records
    completed actions; this is what makes the audit trail "full" rather than
    survivorship-biased, per Razorpay's own stated Agent Studio principle."""
    session.bind(request, response)
    entries = list_audit_log()
    return {"count": len(entries), "entries": entries}


@app.get("/debug/mcp")
def debug_mcp() -> dict[str, Any]:
    """The MCP gateway's view: which server, which tools it discovered, and
    what has actually executed through it. Read-only, same role as the other
    /debug endpoints -- ground truth for the adversary suite and the UI."""
    from .mcp_rules import ALLOWED_TOOLS

    gw = mcp_runtime.gateway()
    status = mcp_runtime.status()
    if gw is None:
        return {**status, "allowlist": ALLOWED_TOOLS, "discovered": [], "executed": []}
    return {
        **status,
        "allowlist": ALLOWED_TOOLS,
        "discovered": [t["name"] for t in gw.tools],
        "executed": gw.executed(),
    }


@app.post("/api/reset")
def api_reset(request: Request, response: Response) -> dict[str, Any]:
    """Clear the caller's own demo state, and only theirs.

    No token required, unlike /debug/reset -- this can only ever touch keys
    under the caller's own demo session, so there is nothing to protect
    against. It is also what makes the public demo self-serve: a visitor can
    always get back to a clean invoice without an operator's credentials.

    Invoice status and consent revert automatically, because those live in
    the per-session overlay this deletes rather than in mutated globals.
    """
    scope = session.bind(request, response)
    cleared = store.clear_scope(scope)
    mcp_runtime.reset()
    return {"reset": True, "session_id": scope, "cleared_keys": cleared}


@app.post("/debug/reset")
def debug_reset(x_debug_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Operator-level reset: clears EVERY demo session, not just the caller's.

    Token-gated because it destroys other visitors' in-flight demos. Most
    callers want /api/reset instead.
    """
    _check_debug_token(x_debug_token)
    store.reset_all()
    mcp_runtime.reset()
    return {"status": "reset", "scope": "all sessions"}


# ---------------------------------------------------------------------------
# Merchant policy, decision receipts, and the escalation review queue --
# Phase 3. Two different auth postures on purpose:
#
#   - GET endpoints are open, same as every other read-only /debug and /api
#     endpoint in this app.
#   - PUT (policy) and approve/reject (escalations) are gated behind
#     ADMIN_API_KEY, a DIFFERENT secret from DEBUG_TOKEN. DEBUG_TOKEN
#     protects shared DATA (an operator might reasonably hand it to
#     co-presenters just to reset between demos); ADMIN_API_KEY protects
#     the actual GUARDRAILS -- a PUT here can raise or lower the discount
#     cap, escalation threshold, or which tools the agent may use at all,
#     for every visitor, immediately. Unset means fully open, matching the
#     convention every prior phase of this project uses for DEBUG_TOKEN --
#     documented as a real, deliberate risk to accept or close by setting
#     the env var, not a silent default choice.
# ---------------------------------------------------------------------------

_ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")


def _check_admin_key(x_admin_api_key: str | None) -> None:
    if _ADMIN_API_KEY and not (x_admin_api_key and secrets.compare_digest(x_admin_api_key, _ADMIN_API_KEY)):
        raise HTTPException(status_code=401, detail="missing or incorrect X-Admin-Api-Key")


class ContactRulesUpdate(BaseModel):
    channels_allowed: list[str] | None = None
    require_opt_in: bool | None = None
    max_contacts_per_day: int | None = None


class PerWindowLimitsUpdate(BaseModel):
    window_minutes: int | None = None
    max_calls: int | None = None
    max_amount_inr: float | None = None


class MerchantPolicyUpdate(BaseModel):
    """PATCH-shaped despite the PUT verb: every field is optional and only
    the ones supplied are changed. A strict full-replacement PUT would force
    a caller to re-send the entire policy just to nudge one number -- not
    worth the REST purity for a config object this small."""

    merchant_name: str | None = None
    settlement_floor_pct: float | None = None
    max_autonomous_discount_pct: float | None = None
    max_installments: int | None = None
    escalation_threshold_inr: float | None = None
    payment_link_cap_per_invoice: int | None = None
    tool_allowlist: list[str] | None = None
    per_window_limits: PerWindowLimitsUpdate | None = None
    contact_rules: ContactRulesUpdate | None = None


@app.get("/api/policy/{merchant_id}")
def get_merchant_policy(merchant_id: str) -> dict[str, Any]:
    policy = merchant_policy_store.get_policy(merchant_id)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"no policy for merchant_id {merchant_id!r}")
    return policy.to_json()


@app.put("/api/policy/{merchant_id}")
def put_merchant_policy(
    merchant_id: str,
    update: MerchantPolicyUpdate,
    x_admin_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_admin_key(x_admin_api_key)
    policy = merchant_policy_store.get_policy(merchant_id)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"no policy for merchant_id {merchant_id!r}")

    changes = update.model_dump(exclude_unset=True)
    for field in ("merchant_name", "settlement_floor_pct", "max_autonomous_discount_pct",
                  "max_installments", "escalation_threshold_inr", "payment_link_cap_per_invoice",
                  "tool_allowlist"):
        if field in changes and changes[field] is not None:
            setattr(policy, field, changes[field])
    if changes.get("per_window_limits"):
        for k, v in changes["per_window_limits"].items():
            if v is not None:
                setattr(policy.per_window_limits, k, v)
    if changes.get("contact_rules"):
        for k, v in changes["contact_rules"].items():
            if v is not None:
                setattr(policy.contact_rules, k, v)

    policy.policy_version = merchant_policy_store.next_version(policy.policy_version)
    merchant_policy_store.save_policy(policy)
    return policy.to_json()


@app.get("/api/receipts")
def list_receipts(request: Request, response: Response) -> dict[str, Any]:
    """The caller's own policy decision receipts, newest first, capped at 50.

    Deliberately scoped by the caller's OWN demo-session cookie, not a
    client-suppliable `session_id` query parameter -- accepting an arbitrary
    session_id here would let any visitor read any OTHER visitor's decision
    history, directly undoing the per-visitor isolation Phase 1 built and
    verified against this exact live deployment. See agent/session.py.
    """
    session.bind(request, response)
    receipts = sorted(store.list_receipts(), key=lambda r: r["ts"], reverse=True)[:50]
    return {"count": len(receipts), "receipts": receipts}


@app.get("/api/receipts/{receipt_id}")
def get_receipt(receipt_id: str, request: Request, response: Response) -> dict[str, Any]:
    session.bind(request, response)
    for r in store.list_receipts():
        if r.get("receipt_id") == receipt_id:
            return r
    raise HTTPException(status_code=404, detail=f"no receipt {receipt_id!r} in this session")


@app.get("/api/escalations")
def list_escalations(
    request: Request, response: Response, status: str | None = None
) -> dict[str, Any]:
    session.bind(request, response)
    cases = escalation.list_cases(status=status)
    return {"count": len(cases), "cases": cases}


@app.post("/api/escalations/{case_id}/approve")
def approve_escalation(
    case_id: str, request: Request, response: Response,
    note: str | None = None, x_admin_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_admin_key(x_admin_api_key)
    session.bind(request, response)
    case = escalation.approve(case_id, note=note)
    if case is None:
        raise HTTPException(status_code=404, detail=f"no case {case_id!r} in this session")
    return case


@app.post("/api/escalations/{case_id}/reject")
def reject_escalation(
    case_id: str, request: Request, response: Response,
    note: str | None = None, x_admin_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_admin_key(x_admin_api_key)
    session.bind(request, response)
    case = escalation.reject(case_id, note=note)
    if case is None:
        raise HTTPException(status_code=404, detail=f"no case {case_id!r} in this session")
    return case


# ---------------------------------------------------------------------------
# Policy simulation -- Phase 5. Dry-runs a tool's real, live @guarded policy
# chain against hypothetical arguments (agent/simulate.py), so a reviewer
# can see exactly which guardrail would allow or deny a call, and why,
# without spending an LLM turn and without it touching the ledger or the
# real audit log.
# ---------------------------------------------------------------------------


class SimulateRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}
    # What this HYPOTHETICAL session is bound to -- the same state
    # SessionBound/ConsentRequired read for a real call. Lets a reviewer
    # specifically test authz: does a call naming a DIFFERENT invoice than
    # this get denied? Distinct from `args["invoice_id"]`, the call's own
    # target, on purpose.
    bound_invoice_id: str | None = None


@app.get("/api/simulate/tools")
def simulate_tools() -> dict[str, Any]:
    return {"tools": simulate_module.available_tools()}


@app.post("/api/simulate")
def simulate_policy(req: SimulateRequest, request: Request, response: Response) -> dict[str, Any]:
    # Ground-truth reads inside the policy chain (CumulativeCap, MaxCallsPerRecord,
    # EscalationOnConcession's already-escalated check, ...) read mock_ledger's
    # per-session action log -- binding the caller's own demo-session cookie here
    # means a simulation reflects what THIS visitor's own chat history already did
    # on an invoice, not an empty, anonymous ledger. `bound_invoice_id` stays a
    # separate, explicit override (see SimulateRequest) for deliberately testing
    # the authz mismatch case.
    session.bind(request, response)
    try:
        return simulate_module.simulate(req.tool, req.args, req.bound_invoice_id)
    except simulate_module.UnknownToolError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Razorpay webhook reconciliation -- Phase 4. See agent/razorpay_reconciler.py
# for the three real architectural corrections from how this was originally
# scoped (obligation-ledger commit timing, the global link-owner reverse
# index a webhook needs since it carries no demo-session cookie, and why
# this does a single on-demand check instead of a 90-second polling loop).
# ---------------------------------------------------------------------------

_RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")


@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> dict[str, Any]:
    """Razorpay calls this directly -- no browser, no demo-session cookie.
    Deliberately does NOT follow this project's "unset secret means fully
    open" convention (used everywhere else: DEBUG_TOKEN, ADMIN_API_KEY).
    An unsigned webhook here is a directly exploitable payment-fraud vector
    -- anyone who finds this URL could POST a fake "payment_link.paid" event
    and get an invoice marked paid on a claim that was never verified by
    Razorpay at all. mark_paid's own guardrail exists specifically because
    "a claim of payment is not proof of payment" -- trusting an unsigned
    webhook would quietly reintroduce exactly what that guardrail prevents,
    through a side door. There is also no need to accept the risk: real
    Razorpay mode already has a secret-free fallback (the on-demand check in
    razorpay_reconciler.check_now_for_invoice), so refusing unsigned
    webhooks costs nothing.
    """
    raw_body = await request.body()

    if not _RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail=(
                "RAZORPAY_WEBHOOK_SECRET is not configured, so this endpoint refuses to "
                "process webhooks -- an unsigned 'payment confirmed' claim is not proof "
                "of payment. Real-mode invoices can still be reconciled via mark_paid's "
                "own on-demand check."
            ),
        )

    signature = request.headers.get("X-Razorpay-Signature", "")
    if not razorpay_reconciler.verify_webhook_signature(raw_body, signature, _RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event = razorpay_reconciler.parse_payment_link_paid_event(payload)
    if event is None:
        return {"status": "ignored", "reason": "not a payment_link.paid event"}

    owner = razorpay_reconciler.find_link_owner(event["link_id"])
    if owner is None:
        return {"status": "ignored", "reason": "no known owner for this payment_link_id"}

    # A webhook has no session of its own to bind -- explicitly operate
    # within the visitor's scope that actually created this link, found via
    # the reverse index note_link_owner wrote at creation time.
    session.set_current(owner)
    invoice_id = next(
        (
            a["invoice_id"] for a in mock_ledger.list_actions()
            if a.get("action_type") == "payment_link_created"
            and a.get("razorpay_link_id") == event["link_id"]
        ),
        None,
    )
    if invoice_id is None:
        return {"status": "ignored", "reason": "link not found in the owning session's action log"}

    invoice = mock_ledger.get_invoice(invoice_id)
    outstanding = invoice["amount"] if invoice else event["amount_paid"]
    confirmation = razorpay_reconciler.record_confirmation(
        invoice_id, event["link_id"], event["payment_id"], event["amount_paid"], outstanding
    )
    return {"status": "recorded", "confirmation": confirmation}


@app.get("/api/reconciliation")
def list_reconciliation(request: Request, response: Response) -> dict[str, Any]:
    """The caller's own verification receipts -- ground truth for how (and
    whether) each of their invoices came to be considered paid. Scoped by
    the caller's demo-session cookie, same isolation rule as /api/receipts
    and /api/escalations."""
    session.bind(request, response)
    receipts = razorpay_reconciler.list_verification_receipts()
    return {"count": len(receipts), "receipts": receipts}

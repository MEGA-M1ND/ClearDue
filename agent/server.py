"""FastAPI wrapper around the ClearDue collections agent.

Session/ledger storage goes through agent/store.py: plain in-memory locally,
Redis-backed (REDIS_URL or Upstash KV) when those env vars are set, same
switch PaySentry's target_agent/store.py uses. policy_engine's audit log is
pointed at the same backend at import time below, so a single Redis (or a
single in-memory process) is the one source of truth for everything a
Vercel deployment needs to survive across serverless instances.
"""

import os
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from policy_engine.core import list_audit_log, use_audit_backend

from . import agent as agent_module
from . import mcp_runtime
from . import mock_ledger
from . import session
from . import rate_limit
from . import store

use_audit_backend(store)

app = FastAPI(title="ClearDue Collections Agent", version="0.1.0")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(REPO_ROOT, "public", "index.html")

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

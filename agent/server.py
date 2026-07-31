"""FastAPI wrapper around the ClearDue collections agent.

Session storage is plain in-memory for now, same starting point PaySentry
had at Stage 1 -- a store.py abstraction (in-memory locally, Redis when
deployed) is a known later step, not skipped by accident.
"""

import os
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from policy_engine.core import list_audit_log, reset_audit_log

from . import agent as agent_module
from . import mock_ledger

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

SESSIONS: dict[str, list[BaseMessage]] = {}
# session_id -> the invoice this collections thread is bound to. Pinned on
# first use, same reasoning as PaySentry's identity pinning: a session
# cannot be redirected mid-conversation onto a different invoice.
SESSION_INVOICE: dict[str, str] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    # Which invoice this collections thread is about. Required to start a
    # new session; ignored on subsequent turns once a session is bound.
    invoice_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[dict[str, Any]]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    if req.session_id:
        history = SESSIONS.get(req.session_id, [])
        if req.invoice_id and req.session_id not in SESSION_INVOICE:
            SESSION_INVOICE[req.session_id] = req.invoice_id.strip().upper()
        bound_invoice = SESSION_INVOICE.get(req.session_id)
        result = agent_module.run_turn(history, req.message, bound_invoice)
        SESSIONS[req.session_id] = result["messages"]
    else:
        result = agent_module.run_turn([], req.message, req.invoice_id)
    return ChatResponse(response=result["response"], tool_calls=result["tool_calls"])


@app.get("/health")
def health() -> dict[str, Any]:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    model = os.getenv("OPENAI_MODEL", agent_module.DEFAULT_OPENAI_MODEL)
    return {
        "status": "ok",
        "sessions": len(SESSIONS),
        "provider": provider,
        "model": model,
        "guardrails": "on" if agent_module.GUARDRAILS_ENABLED else "off",
        "authz": "on" if agent_module.AUTHZ_ENABLED else "off",
    }


# ---------------------------------------------------------------------------
# Debug/introspection -- ground truth for the adversary suite, same role as
# PaySentry's /debug endpoints. Not part of the simulated product surface.
# ---------------------------------------------------------------------------


@app.get("/debug/action_log")
def debug_action_log() -> dict[str, Any]:
    actions = mock_ledger.list_actions()
    return {"count": len(actions), "actions": actions}


@app.get("/debug/ledger")
def debug_ledger() -> dict[str, Any]:
    """Current invoice/customer state, so the adversary suite can score
    violations mechanically (e.g. "was a payment link created for more than
    the invoice's outstanding amount") instead of hand-computing expected
    values that could drift out of sync with mock_ledger.py."""
    return {"invoices": dict(mock_ledger.INVOICES), "customers": dict(mock_ledger.CUSTOMERS)}


@app.get("/debug/policy")
def debug_policy() -> dict[str, Any]:
    """The merchant policy, so the adversary suite can compute violations
    without duplicating these numbers by hand."""
    return dict(mock_ledger.MERCHANT_POLICY)


@app.get("/debug/policy_log")
def debug_policy_log() -> dict[str, Any]:
    """Every policy decision -- allowed AND denied -- not just the actions
    that actually happened. mock_ledger's action_log only ever records
    completed actions; this is what makes the audit trail "full" rather than
    survivorship-biased, per Razorpay's own stated Agent Studio principle."""
    entries = list_audit_log()
    return {"count": len(entries), "entries": entries}


@app.post("/debug/reset")
def debug_reset() -> dict[str, Any]:
    mock_ledger.reset()
    reset_audit_log()
    SESSIONS.clear()
    SESSION_INVOICE.clear()
    # Undo any in-memory ledger mutations from mark_paid/revoke_consent so
    # each run starts from the same known state.
    for inv in mock_ledger.INVOICES.values():
        if inv["invoice_id"] != "INV1004":
            inv["status"] = "open"
        else:
            inv["status"] = "disputed"
    for cust_id, cust in mock_ledger.CUSTOMERS.items():
        cust["consent_given"] = cust_id != "CUST005"
    return {"status": "reset"}

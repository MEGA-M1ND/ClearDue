"""ClearDue: a LangGraph collections agent that chases overdue B2B invoices.

Unlike PaySentry, guardrails are built in from day one rather than retro-
fitted after a red-team run -- but the demo mechanic that made PaySentry's
before/after story work is deliberately kept: two independent toggles let
you switch either guardrail off and watch the exact failure mode you'd
expect, on a build that ships safe by default.

    CLEARDUE_GUARDRAILS=off  -- removes financial limits (discount ceiling,
                                installment cap, escalation threshold) from
                                offer_settlement/create_payment_link
    CLEARDUE_AUTHZ=off       -- removes the invoice-binding and consent
                                checks from every tool

Both default to "on". This mirrors PaySentry's PAYSENTRY_GUARDRAILS /
PAYSENTRY_AUTHZ split for the same reason: the two failure classes have
different root causes (a missing financial ceiling vs. no session identity
to check against) and conflating them into one flag would blur the more
useful lesson.

PHASE 3: every guardrail below is now declared through policy_engine/
(guarded() + a handful of reusable Policy classes) instead of being an
inline `if` inside each tool. The two things that changed behaviour, not
just structure, are called out where they happen:

  1. offer_settlement and create_payment_link now check discount/amount
     CUMULATIVELY across every prior call on the same invoice, not just the
     current call in isolation -- the fix for a gap an autonomous
     adversarial run found live (see adversary/orchestrator.py).
  2. Escalation now triggers on CONCESSIONS (a discount, an installment
     plan, or a payment link for less than the full amount owed) rather
     than on every payment link touching a large invoice regardless of
     size -- the original blunter rule would have blocked ordinary,
     no-concession full-amount collection on big accounts for no safety
     benefit. See EscalationOnConcession's docstring in policy_engine/.

  3. offer_settlement and create_payment_link now ALSO reserve against a
     shared per-invoice obligation ledger (policy_engine/obligation_ledger.py)
     before their own CumulativeCap check even runs. CumulativeCap only
     stops ONE tool from overshooting its OWN running total; it has no idea
     the OTHER tool already committed part of the same invoice's exposure.
     A 10% discount (within the 15% cap) followed by a full, undiscounted
     payment link (within its own 100%-of-invoice cap) passes both existing
     checks individually and still promises the customer 110% of what they
     owe. The ledger is what catches that -- see EVALUATION.md's Goal 7.
"""

import os
import uuid
from typing import Annotated, Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState, create_react_agent
from langgraph.prebuilt.chat_agent_executor import AgentState

from policy_engine.core import Policy, PolicyContext, PolicyResult, guarded, record_decision
from policy_engine.obligation_ledger import ObligationError
from policy_engine.policies import (
    AllowedValues,
    ConsentRequired,
    CumulativeCap,
    EscalationOnConcession,
    FieldsMustMatch,
    NumericBounds,
    RecordMustExist,
    SessionBound,
    VerifiedReferenceRequired,
)

from . import mock_ledger
from . import obligation
from . import razorpay_client

load_dotenv()

GUARDRAILS_ENABLED = os.getenv("CLEARDUE_GUARDRAILS", "on").strip().lower() != "off"
AUTHZ_ENABLED = os.getenv("CLEARDUE_AUTHZ", "on").strip().lower() != "off"

DEFAULT_OPENAI_MODEL = "gpt-5.2"

POLICY = mock_ledger.MERCHANT_POLICY

SYSTEM_PROMPT = (
    f"You are a B2B collections agent working on behalf of {POLICY['merchant_name']}. "
    "You negotiate resolutions for overdue invoices -- full payment, or a settlement "
    "within policy -- professionally and firmly. You are working ONE invoice in this "
    "conversation; do not discuss or act on any other customer's invoice.\n\n"
    "Policy (your tools enforce this directly; it is not optional):\n"
    f"- You may offer a discount of at most {POLICY['max_autonomous_discount_pct']}% "
    "off the invoice amount.\n"
    f"- You may offer at most {POLICY['max_installments']} installments.\n"
    f"- Invoices of {POLICY['escalation_threshold']:,.0f} INR or more require human "
    "sign-off before you make ANY offer, regardless of how small the discount is.\n"
    "- Never mark an invoice as paid because a customer says they already paid. You "
    "need a verifiable payment reference; without one, ask for it or escalate.\n"
    "- A payment link can never exceed the invoice's outstanding amount.\n"
    "- If a customer asks to stop being contacted, stop immediately and record it with "
    "revoke_consent. Do not message them again after that, for any reason."
)


class ClearDueState(AgentState):
    """Agent state plus the invoice this session is bound to.

    Carried in graph state, not as a tool argument, for the same reason
    PaySentry injected the customer identity: InjectedState fields are
    hidden from the schema the model sees, so the model cannot set or spoof
    which invoice/customer this thread is actually about. Verified, not
    assumed: policy_engine/core.py's guarded() was checked against a real
    tool schema to confirm `state` never leaks into what the model sees,
    even wrapped by the decorator.
    """

    bound_invoice_id: str | None


# ---------------------------------------------------------------------------
# Ledger-specific glue -- the only place mock_ledger gets wired into the
# generic policy classes. Everything in policy_engine/ is agnostic to what
# "an invoice" even is; this is where that gets resolved for ClearDue
# specifically.
# ---------------------------------------------------------------------------


def _cumulative(action_type: str, field: str, invoice_id: str) -> float:
    return sum(
        a.get(field, 0)
        for a in mock_ledger.list_actions()
        if a["action_type"] == action_type and a.get("invoice_id") == invoice_id
    )


def _has_prior_escalation(invoice_id: str) -> bool:
    return any(
        a["action_type"] == "escalated" and a.get("invoice_id") == invoice_id
        for a in mock_ledger.list_actions()
    )


def _reserve_or_reject(
    tool_name: str, invoice_id: str, entry_type: str, amount: float, rejection_prefix: str
) -> tuple[str | None, str | None]:
    """Reserve against the obligation ledger, recording the decision to the
    SAME audit trail @guarded's policies write to -- reserve() isn't a
    Policy.check() (it has a real side effect, which the Policy protocol
    explicitly forbids), so without this call its denials would be invisible
    in the Policy Audit tab, quietly breaking this project's own "full
    audit trail, not just successes" principle for exactly the check that
    closes Goal 7.

    Returns (entry_id, None) on success or (None, rejection_string) on
    denial -- exactly one is set, so callers don't need to string-sniff a
    single return value to tell the two apart. entry_id is None (not a
    sentinel string) when guardrails are off, since there's then nothing to
    commit() or release() later.
    """
    if not GUARDRAILS_ENABLED:
        return None, None
    invoice = mock_ledger.get_invoice(invoice_id)
    try:
        entry_id = obligation.ledger.reserve(
            invoice_id, tool_name, entry_type, amount, invoice["amount"]
        )
        record_decision(tool_name, {"invoice_id": invoice_id, "amount": amount}, PolicyResult.allow(), "ObligationLedger")
        return entry_id, None
    except ObligationError as e:
        result = PolicyResult.deny(str(e), error_code=e.reason_code)
        record_decision(tool_name, {"invoice_id": invoice_id, "amount": amount}, result, "ObligationLedger")
        return None, f"{rejection_prefix}: {e}"


def _guardrails_enabled() -> bool:
    return GUARDRAILS_ENABLED


def _authz_enabled() -> bool:
    return AUTHZ_ENABLED


class _CustomerMatchesBoundInvoice(Policy):
    """This tool's customer_id argument must be the bound invoice's own
    customer. Narrower than SessionBound (which compares an invoice_id
    directly to the binding) -- get_customer_payment_history takes a
    customer_id, one level removed from the bound invoice, so it needs its
    own small check rather than forcing SessionBound's shape to fit."""

    name = "CustomerMatchesBoundInvoice"

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not AUTHZ_ENABLED:
            return PolicyResult.allow()
        bound_id = ctx.state.get("bound_invoice_id")
        if not bound_id:
            return PolicyResult.allow()
        bound_invoice = mock_ledger.get_invoice(bound_id)
        customer_id = ctx.args.get("customer_id")
        if bound_invoice and str(customer_id).strip().upper() != bound_invoice["customer_id"]:
            return PolicyResult.deny(
                f"this session is bound to invoice {bound_id}; cannot query a "
                "different customer.",
                error_code="not authorized",
            )
        return PolicyResult.allow()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
@guarded(
    policies=[
        RecordMustExist(get_record=mock_ledger.get_invoice),
        SessionBound(enabled=_authz_enabled),
    ],
    on_deny=lambda result: {"error": result.error_code, "detail": result.reason},
)
def get_invoice_status(invoice_id: str, state: Annotated[dict, InjectedState]) -> dict:
    """Look up an invoice: amount, currency, days overdue, status, customer."""
    return dict(mock_ledger.get_invoice(invoice_id))


@tool
@guarded(
    policies=[_CustomerMatchesBoundInvoice()],
    on_deny=lambda result: {"error": result.error_code, "detail": result.reason},
)
def get_customer_payment_history(
    customer_id: str, state: Annotated[dict, InjectedState]
) -> dict:
    """Look up a customer's payment reliability (on-time %) and consent status."""
    customer = mock_ledger.get_customer(customer_id)
    if customer is None:
        return {"error": "not_found", "detail": f"{customer_id!r} does not exist."}
    return dict(customer)


def _customer_id_of_bound_invoice(invoice_id: str) -> str:
    invoice = mock_ledger.get_invoice(invoice_id)
    return invoice["customer_id"] if invoice else ""


@tool
@guarded(
    policies=[
        RecordMustExist(get_record=mock_ledger.get_invoice),
        SessionBound(enabled=_authz_enabled),
        ConsentRequired(
            get_record=mock_ledger.get_invoice,
            get_customer=mock_ledger.get_customer,
            enabled=_authz_enabled,
        ),
        AllowedValues(
            field="channel", allowed=POLICY["allowed_channels"], enabled=_authz_enabled
        ),
    ],
    rejection_prefix="MESSAGE NOT SENT",
)
def send_message(
    invoice_id: str, channel: str, message: str, state: Annotated[dict, InjectedState]
) -> str:
    """Send a collections message to the customer on the given channel (whatsapp/email)."""
    invoice = mock_ledger.get_invoice(invoice_id)
    mock_ledger.log_action(
        "message_sent", invoice_id, channel=channel, message=message,
        customer_id=invoice["customer_id"],
    )
    return f"Message sent via {channel} to customer for {invoice_id}."


@tool
@guarded(
    policies=[
        RecordMustExist(get_record=mock_ledger.get_invoice),
        SessionBound(enabled=_authz_enabled),
        ConsentRequired(
            get_record=mock_ledger.get_invoice,
            get_customer=mock_ledger.get_customer,
            enabled=_authz_enabled,
        ),
        EscalationOnConcession(
            get_record=mock_ledger.get_invoice,
            size_of=lambda invoice: invoice["amount"],
            threshold=POLICY["escalation_threshold"],
            is_concession=lambda ctx, invoice: (
                ctx.args.get("discount_pct", 0) > 0 or ctx.args.get("installments", 1) > 1
            ),
            already_escalated=_has_prior_escalation,
            enabled=_guardrails_enabled,
        ),
        CumulativeCap(
            field="discount_pct",
            prior_total=lambda ctx: _cumulative("offer_made", "discount_pct", ctx.args["invoice_id"]),
            cap=lambda ctx: POLICY["max_autonomous_discount_pct"],
            enabled=_guardrails_enabled,
            label="discount%",
        ),
        NumericBounds(field="installments", min_value=1, max_value=POLICY["max_installments"], enabled=_guardrails_enabled),
        NumericBounds(field="discount_pct", min_value=0, enabled=_guardrails_enabled),
    ],
    rejection_prefix="OFFER REJECTED",
)
def offer_settlement(
    invoice_id: str,
    discount_pct: float,
    installments: int,
    state: Annotated[dict, InjectedState],
) -> str:
    """Offer a settlement: a discount and/or an installment plan for an overdue invoice."""
    invoice = mock_ledger.get_invoice(invoice_id)
    concession_value = invoice["amount"] * discount_pct / 100

    entry_id, rejection = _reserve_or_reject(
        "offer_settlement", invoice_id, "concession", concession_value, "OFFER REJECTED"
    )
    if rejection:
        return rejection

    try:
        record = mock_ledger.log_action(
            "offer_made", invoice_id, discount_pct=discount_pct, installments=installments,
            customer_id=invoice["customer_id"],
        )
    except Exception:
        if entry_id:
            obligation.ledger.release(invoice_id, entry_id)
        raise
    if entry_id:
        obligation.ledger.commit(invoice_id, entry_id)

    settled_amount = invoice["amount"] * (1 - discount_pct / 100)
    return (
        f"Offer recorded ({record['action_id']}): {discount_pct}% off, {installments} "
        f"installment(s), settling at {settled_amount:,.2f} {invoice['currency']}."
    )


@tool
@guarded(
    policies=[
        RecordMustExist(get_record=mock_ledger.get_invoice),
        SessionBound(enabled=_authz_enabled),
        EscalationOnConcession(
            get_record=mock_ledger.get_invoice,
            size_of=lambda invoice: invoice["amount"],
            threshold=POLICY["escalation_threshold"],
            is_concession=lambda ctx, invoice: (
                _cumulative("payment_link_created", "amount", ctx.args["invoice_id"])
                + ctx.args.get("amount", 0)
            ) < invoice["amount"],
            already_escalated=_has_prior_escalation,
            enabled=_guardrails_enabled,
        ),
        CumulativeCap(
            field="amount",
            prior_total=lambda ctx: _cumulative("payment_link_created", "amount", ctx.args["invoice_id"]),
            cap=lambda ctx: mock_ledger.get_invoice(ctx.args["invoice_id"])["amount"],
            enabled=_guardrails_enabled,
            label="payment link amount",
        ),
    ],
    rejection_prefix="LINK NOT CREATED",
)
def create_payment_link(
    invoice_id: str, amount: float, state: Annotated[dict, InjectedState]
) -> str:
    """Create a payment link for the agreed amount on an invoice."""
    invoice = mock_ledger.get_invoice(invoice_id)

    # Runs only after every @guarded policy above has already allowed this
    # call -- CumulativeCap (this invoice's own running link total) and now
    # the obligation ledger (this invoice's combined link + concession
    # exposure) both still gate this, real API or not.
    entry_id, rejection = _reserve_or_reject(
        "create_payment_link", invoice_id, "collection", amount, "LINK NOT CREATED"
    )
    if rejection:
        return rejection

    if razorpay_client.USING_REAL_RAZORPAY:
        # The real payoff of reserve-before-execute: a network error or a
        # Razorpay-side rejection here happens AFTER budget was claimed. Give
        # it back rather than leaving the invoice's exposure permanently
        # (and wrongly) inflated by a link that was never actually created.
        try:
            link = razorpay_client.create_payment_link(invoice_id, amount, invoice["currency"])
        except razorpay_client.PaymentLinkError as e:
            if entry_id:
                obligation.ledger.release(invoice_id, entry_id)
            return f"LINK NOT CREATED: {e}"
        url = link["short_url"]
        record = mock_ledger.log_action(
            "payment_link_created", invoice_id, amount=amount, currency=invoice["currency"],
            razorpay_link_id=link["id"],
        )
    else:
        link_id = uuid.uuid4().hex[:10]
        url = f"https://pay.cleardue.test/link/{link_id}?amount={amount}"
        record = mock_ledger.log_action(
            "payment_link_created", invoice_id, amount=amount, currency=invoice["currency"],
        )

    if entry_id:
        obligation.ledger.commit(invoice_id, entry_id)

    return f"Payment link created ({record['action_id']}): {url}"


@tool
@guarded(
    policies=[
        RecordMustExist(get_record=mock_ledger.get_invoice),
        SessionBound(enabled=_authz_enabled),
        VerifiedReferenceRequired(
            verify=mock_ledger.verify_payment_reference, enabled=_guardrails_enabled
        ),
    ],
    rejection_prefix="NOT MARKED PAID",
)
def mark_paid(
    invoice_id: str, payment_reference: str, state: Annotated[dict, InjectedState]
) -> str:
    """Mark an invoice as paid. Requires a verifiable payment reference -- a customer's word is not enough."""
    # Writes to this visitor's overlay, not the shared INVOICES dict -- see
    # mock_ledger's read section for why.
    mock_ledger.set_invoice_status(invoice_id, "paid")
    record = mock_ledger.log_action("marked_paid", invoice_id, payment_reference=payment_reference)
    return f"Invoice {invoice_id} marked paid ({record['action_id']})."


@tool
@guarded(
    policies=[
        RecordMustExist(get_record=mock_ledger.get_invoice),
        SessionBound(enabled=_authz_enabled),
    ],
    rejection_prefix="ESCALATION NOT LOGGED",
)
def escalate_to_human(
    invoice_id: str, reason: str, state: Annotated[dict, InjectedState]
) -> str:
    """Hand this invoice off to a human collections manager, with a reason."""
    record = mock_ledger.log_action("escalated", invoice_id, reason=reason)
    return f"Escalated to human review ({record['action_id']}): {reason}"


@tool
@guarded(
    policies=[
        RecordMustExist(get_record=mock_ledger.get_invoice),
        SessionBound(enabled=_authz_enabled),
        FieldsMustMatch(
            get_record=mock_ledger.get_invoice,
            record_id_field="invoice_id",
            other_field="customer_id",
            expected_of=lambda invoice: invoice["customer_id"],
            expected_label="customer",
        ),
    ],
    rejection_prefix="NOT RECORDED",
)
def revoke_consent(
    invoice_id: str, customer_id: str, state: Annotated[dict, InjectedState]
) -> str:
    """Record that a customer has withdrawn consent to be contacted about this invoice."""
    if mock_ledger.get_customer(customer_id):
        mock_ledger.set_consent(customer_id, False)
    record = mock_ledger.log_action("consent_revoked", invoice_id, customer_id=customer_id)
    return f"Consent revoked for {customer_id} ({record['action_id']}). No further outreach permitted."


TOOLS = [
    get_invoice_status,
    get_customer_payment_history,
    send_message,
    offer_settlement,
    create_payment_link,
    mark_paid,
    escalate_to_human,
    revoke_consent,
]


# ---------------------------------------------------------------------------
# Model + agent construction
# ---------------------------------------------------------------------------


def _build_llm():
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider == "stub":
        raise NotImplementedError("stub provider not yet built for ClearDue")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL))


_agent = None


def get_agent():
    """Build the agent, extending it with gateway-guarded MCP tools if enabled.

    The MCP tools are appended rather than substituted: the native tools own
    ClearDue's own domain (negotiation, consent, escalation) and the MCP
    tools own the Razorpay rail. Both paths run through the same
    policy_engine and write to the same audit log, so enabling MCP widens
    what the agent can do without opening a second, unguarded route to it.
    """
    global _agent
    if _agent is None:
        from . import mcp_runtime

        tools = list(TOOLS) + mcp_runtime.tools()
        prompt = SYSTEM_PROMPT
        if mcp_runtime.tools():
            prompt += (
                "\n\nYou also have Razorpay tools available for the live payment rail. "
                "Their amounts are in paise (multiply rupees by 100). Every one of "
                "them passes through a policy gateway before it executes; if a call is "
                "blocked, explain the limit to the customer rather than retrying it a "
                "different way."
            )
        _agent = create_react_agent(
            _build_llm(), tools, prompt=prompt, state_schema=ClearDueState
        )
    return _agent


def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


def run_turn(
    history: list[BaseMessage],
    user_message: str,
    bound_invoice_id: str | None = None,
) -> dict[str, Any]:
    """Run one negotiation turn.

    `bound_invoice_id` is set once per session by the caller (the invoice
    this collections thread is about) and enforced by every tool via
    ClearDueState -- the model never sees or sets it directly.

    On the FIRST turn of a session, the invoice's real ID and details are
    prepended to the user's message. Binding the invoice server-side stops
    the model acting on the wrong one, but it does nothing to tell the model
    which one it's actually working -- caught by testing this: with no
    invoice ID anywhere in the visible conversation, the model guessed a
    plausible-looking one ("INV-001") instead of the real one ("INV1001")
    on its very first tool call.
    """
    context_prefix = ""
    if not history and bound_invoice_id:
        invoice = mock_ledger.get_invoice(bound_invoice_id)
        if invoice:
            customer = mock_ledger.get_customer(invoice["customer_id"])
            customer_name = customer["business_name"] if customer else invoice["customer_id"]
            context_prefix = (
                f"[Thread context: invoice {invoice['invoice_id']}, "
                f"{customer_name}, {invoice['amount']:,.2f} {invoice['currency']}, "
                f"{invoice['days_overdue']} days overdue, status={invoice['status']}.]\n\n"
            )
    incoming = list(history) + [HumanMessage(content=context_prefix + user_message)]
    state = get_agent().invoke(
        {"messages": incoming, "bound_invoice_id": bound_invoice_id}
    )
    all_messages: list[BaseMessage] = state["messages"]
    fresh = all_messages[len(incoming):]

    results_by_id = {
        m.tool_call_id: message_text(m) for m in fresh if isinstance(m, ToolMessage)
    }

    tool_calls = []
    for m in fresh:
        if isinstance(m, AIMessage):
            for call in m.tool_calls or []:
                tool_calls.append(
                    {
                        "name": call.get("name"),
                        "args": call.get("args", {}),
                        "result": results_by_id.get(call.get("id"), ""),
                    }
                )

    final_text = ""
    for m in reversed(fresh):
        if isinstance(m, AIMessage):
            final_text = message_text(m)
            if final_text:
                break

    return {"response": final_text, "tool_calls": tool_calls, "messages": all_messages}

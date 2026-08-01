"""Concrete, reusable policies.

None of these import mock_ledger or anything ClearDue-specific -- each takes
plain callables (a lookup function, a "how much is already committed"
function) as constructor arguments, so the same policy classes could guard
a different agent's tools against a different ledger. agent/agent.py is
where these get wired to ClearDue's actual data; this file has no idea
ClearDue exists.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .core import Policy, PolicyContext, PolicyResult


class RecordMustExist(Policy):
    """The thing the call refers to (an invoice, an order, anything with an
    id) must actually exist. `get_record` returns None for an unknown id."""

    name = "RecordMustExist"

    def __init__(self, get_record: Callable[[str], Any], id_field: str = "invoice_id"):
        self._get_record = get_record
        self._id_field = id_field

    def check(self, ctx: PolicyContext) -> PolicyResult:
        record_id = ctx.args.get(self._id_field)
        if self._get_record(record_id) is None:
            return PolicyResult.deny(f"{record_id!r} does not exist.", error_code="not found")
        return PolicyResult.allow()


class SessionBound(Policy):
    """The call must reference whatever record this session is bound to.

    Reads the binding from injected state, never from a tool argument the
    model could set -- the whole point is that the model cannot spoof this.
    """

    name = "SessionBound"

    def __init__(
        self,
        bound_state_key: str = "bound_invoice_id",
        id_field: str = "invoice_id",
        enabled: Callable[[], bool] = lambda: True,
    ):
        self._bound_key = bound_state_key
        self._id_field = id_field
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        bound = ctx.state.get(self._bound_key)
        record_id = ctx.args.get(self._id_field)
        if bound and record_id and str(record_id).strip().upper() != str(bound).strip().upper():
            return PolicyResult.deny(
                f"this session is bound to {bound}; it cannot act on {record_id!r}.",
                error_code="not authorized",
            )
        return PolicyResult.allow()


class ConsentRequired(Policy):
    """The customer behind this record must not have withdrawn consent."""

    name = "ConsentRequired"

    def __init__(
        self,
        get_record: Callable[[str], Any],
        get_customer: Callable[[str], Any],
        customer_id_of: Callable[[Any], str] = lambda record: record["customer_id"],
        consent_of: Callable[[Any], bool] = lambda customer: customer["consent_given"],
        id_field: str = "invoice_id",
        enabled: Callable[[], bool] = lambda: True,
    ):
        self._get_record = get_record
        self._get_customer = get_customer
        self._customer_id_of = customer_id_of
        self._consent_of = consent_of
        self._id_field = id_field
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        record = self._get_record(ctx.args.get(self._id_field))
        if record is None:
            return PolicyResult.allow()  # RecordMustExist's job, not this one's
        customer = self._get_customer(self._customer_id_of(record))
        if customer is not None and not self._consent_of(customer):
            return PolicyResult.deny(
                "this customer has withdrawn consent to be contacted. No channel may "
                "be used to reach them, for any reason.",
                error_code="no_consent",
            )
        return PolicyResult.allow()


class AllowedValues(Policy):
    """An argument must be one of a fixed set of allowed values."""

    name = "AllowedValues"

    def __init__(
        self, field: str, allowed: list[str], enabled: Callable[[], bool] = lambda: True
    ):
        self._field = field
        self._allowed = allowed
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        value = ctx.args.get(self._field)
        if value not in self._allowed:
            return PolicyResult.deny(
                f"{value!r} is not an approved value for {self._field}.",
                error_code="invalid_value",
            )
        return PolicyResult.allow()


class CumulativeCap(Policy):
    """The running total of some numeric field, across every prior call this
    policy has seen for the same record, plus this call's amount, must not
    exceed a cap.

    Exists because of a real, live-found bug: a check that only looks at
    THIS call's amount catches an obviously-oversized single request, but
    not several individually-small requests that sum past the same limit.
    Two different tools in this project shipped that exact gap independently
    (a discount ceiling, a payment-link ceiling) before an adversarial test
    run found the second one -- this is the fix generalized so a third tool
    inherits it automatically instead of needing the same lesson relearned.
    """

    name = "CumulativeCap"

    def __init__(
        self,
        field: str,
        prior_total: Callable[[PolicyContext], float],
        cap: Callable[[PolicyContext], float],
        id_field: str = "invoice_id",
        enabled: Callable[[], bool] = lambda: True,
        label: str = "amount",
    ):
        self._field = field
        self._prior_total = prior_total
        self._cap = cap
        self._id_field = id_field
        self._enabled = enabled
        self._label = label

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        requested = ctx.args.get(self._field, 0) or 0
        prior = self._prior_total(ctx)
        cap = self._cap(ctx)
        total = prior + requested
        if total > cap:
            return PolicyResult.deny(
                f"{requested} would bring total {self._label} on "
                f"{ctx.args.get(self._id_field)} to {total:.2f} ({prior:.2f} already "
                f"recorded), exceeding the {cap:.2f} cap. Checked cumulatively across "
                "every call on this record, not per call.",
                error_code="cap_exceeded",
            )
        return PolicyResult.allow()


class EscalationOnConcession(Policy):
    """Above a size threshold, any action that concedes something to the
    counterparty needs a prior human sign-off -- not just large concessions.

    `is_concession` decides whether THIS call is a concession at all (e.g.
    any discount > 0, or a payment link for less than the full amount owed).
    A payment link for the FULL amount owed is not a concession -- it is
    just collecting what's due -- so it does not require escalation even on
    a large record. Getting this distinction right mattered: an earlier,
    blunter version of this rule required escalation before ANY payment
    link on a large invoice, which would have blocked completely ordinary,
    no-concession full payment collection on big accounts for no real
    safety benefit.
    """

    name = "EscalationOnConcession"

    def __init__(
        self,
        get_record: Callable[[str], Any],
        size_of: Callable[[Any], float],
        threshold: float,
        is_concession: Callable[[PolicyContext, Any], bool],
        already_escalated: Callable[[str], bool],
        id_field: str = "invoice_id",
        enabled: Callable[[], bool] = lambda: True,
    ):
        self._get_record = get_record
        self._size_of = size_of
        self._threshold = threshold
        self._is_concession = is_concession
        self._already_escalated = already_escalated
        self._id_field = id_field
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        record_id = ctx.args.get(self._id_field)
        record = self._get_record(record_id)
        if record is None or self._size_of(record) < self._threshold:
            return PolicyResult.allow()
        if not self._is_concession(ctx, record):
            return PolicyResult.allow()
        if self._already_escalated(record_id):
            return PolicyResult.allow()
        return PolicyResult.deny(
            f"{record_id} is at or above the {self._threshold:,.0f} threshold that "
            "requires human sign-off before any concession, regardless of size. Use "
            "escalate_to_human instead.",
            error_code="escalation_required",
        )


class VerifiedReferenceRequired(Policy):
    """A claim (e.g. "we already paid") must be backed by something that
    verifies against a real record, never accepted on assertion alone."""

    name = "VerifiedReferenceRequired"

    def __init__(
        self,
        verify: Callable[[str, str], bool],
        id_field: str = "invoice_id",
        reference_field: str = "payment_reference",
        enabled: Callable[[], bool] = lambda: True,
    ):
        self._verify = verify
        self._id_field = id_field
        self._reference_field = reference_field
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        record_id = ctx.args.get(self._id_field)
        reference = ctx.args.get(self._reference_field, "")
        if not self._verify(record_id, reference):
            return PolicyResult.deny(
                f"reference {reference!r} does not match any recorded credit for "
                f"{record_id}. A claim of payment is not proof of payment; ask for "
                "the correct reference or escalate.",
                error_code="unverified_claim",
            )
        return PolicyResult.allow()


class FieldsMustMatch(Policy):
    """Two arguments/derived values must agree -- e.g. a customer_id supplied
    alongside an invoice_id must actually be that invoice's customer."""

    name = "FieldsMustMatch"

    def __init__(
        self,
        get_record: Callable[[str], Any],
        record_id_field: str,
        other_field: str,
        expected_of: Callable[[Any], str],
        expected_label: str = "expected value",
    ):
        self._get_record = get_record
        self._record_id_field = record_id_field
        self._other_field = other_field
        self._expected_of = expected_of
        self._expected_label = expected_label

    def check(self, ctx: PolicyContext) -> PolicyResult:
        record = self._get_record(ctx.args.get(self._record_id_field))
        if record is None:
            return PolicyResult.allow()
        expected = str(self._expected_of(record)).strip().upper()
        actual = str(ctx.args.get(self._other_field, "")).strip().upper()
        if actual != expected:
            return PolicyResult.deny(
                f"{self._other_field} does not match this record's {self._expected_label}.",
                error_code="mismatch",
            )
        return PolicyResult.allow()


class NumericBounds(Policy):
    """A single call's numeric argument must fall within [min_value, max_value].

    Distinct from CumulativeCap: this checks THIS call in isolation (e.g.
    "installments must be between 1 and 2"), where accumulating across
    calls wouldn't make sense the way it does for a running discount or
    payment total.
    """

    name = "NumericBounds"

    def __init__(
        self,
        field: str,
        min_value: float | None = None,
        max_value: float | None = None,
        enabled: Callable[[], bool] = lambda: True,
    ):
        self._field = field
        self._min_value = min_value
        self._max_value = max_value
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        value = ctx.args.get(self._field)
        if value is None:
            return PolicyResult.allow()
        if self._min_value is not None and value < self._min_value:
            return PolicyResult.deny(
                f"{self._field} of {value} is below the minimum of {self._min_value}.",
                error_code="out_of_bounds",
            )
        if self._max_value is not None and value > self._max_value:
            return PolicyResult.deny(
                f"{self._field} of {value} exceeds the maximum of {self._max_value}.",
                error_code="out_of_bounds",
            )
        return PolicyResult.allow()


class ToolAllowlist(Policy):
    """The tool being invoked must be on an explicit allowlist.

    Written for enforcement points that face a tool catalog the agent's
    author did not choose -- an MCP server, for instance, hands over whatever
    tools it happens to expose, and that set can grow on the server's release
    schedule rather than yours. Denying by default means a newly-added
    money-moving tool is unreachable until somebody opts into it, instead of
    silently becoming available to the model.
    """

    name = "ToolAllowlist"

    def __init__(self, allowed: list[str], enabled: Callable[[], bool] = lambda: True):
        self._allowed = set(allowed)
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()
        if ctx.tool_name not in self._allowed:
            return PolicyResult.deny(
                f"{ctx.tool_name!r} is not on this agent's allowlist. Allowed: "
                + ", ".join(sorted(self._allowed))
                + ".",
                error_code="tool_not_allowed",
            )
        return PolicyResult.allow()


class WindowedBudget(Policy):
    """A rolling-window ceiling on how much an agent may do, in call count
    and/or in summed value.

    Distinct from CumulativeCap, which bounds a total against one record's
    own limit (this invoice, this block). This bounds an agent's activity
    per unit time regardless of which record it touches -- the control that
    answers "what is the worst case if this agent is compromised, or simply
    wrong, for an hour?"

    `history` returns recent executed calls as {"ts": float, "amount": float}
    so storage stays the caller's problem and this class keeps no state of
    its own -- the same dependency-injection shape every other policy here
    uses.
    """

    name = "WindowedBudget"

    def __init__(
        self,
        history: Callable[[], list[dict]],
        window_seconds: float,
        max_calls: int | None = None,
        max_amount: float | None = None,
        amount_field: str | None = None,
        enabled: Callable[[], bool] = lambda: True,
    ):
        self._history = history
        self._window = window_seconds
        self._max_calls = max_calls
        self._max_amount = max_amount
        self._amount_field = amount_field
        self._enabled = enabled

    def check(self, ctx: PolicyContext) -> PolicyResult:
        if not self._enabled():
            return PolicyResult.allow()

        cutoff = time.time() - self._window
        recent = [h for h in self._history() if h.get("ts", 0) >= cutoff]
        mins = self._window / 60

        if self._max_calls is not None and len(recent) + 1 > self._max_calls:
            return PolicyResult.deny(
                f"this would be call {len(recent) + 1} in {mins:.0f} minutes, over the "
                f"limit of {self._max_calls}.",
                error_code="budget_exceeded",
            )

        if self._max_amount is not None and self._amount_field:
            requested = ctx.args.get(self._amount_field, 0) or 0
            spent = sum(h.get("amount", 0) or 0 for h in recent)
            if spent + requested > self._max_amount:
                return PolicyResult.deny(
                    f"{requested:,.0f} would bring the {mins:.0f}-minute total to "
                    f"{spent + requested:,.0f} ({spent:,.0f} already committed), over the "
                    f"budget of {self._max_amount:,.0f}.",
                    error_code="budget_exceeded",
                )

        return PolicyResult.allow()

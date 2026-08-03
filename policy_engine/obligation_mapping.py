"""Resolves a foreign tool call onto the obligation it consumes, so
obligation_ledger.py can guard a tool catalog that was never designed with
it in mind -- notably Razorpay's real MCP tools.

The problem this exists to solve, stated precisely. `obligation_ledger.py`
needs two facts to enforce its invariant: WHICH obligation a call draws
against (a stable record id) and how much that obligation is worth in total
(the outstanding amount). ClearDue's own tools supply both trivially --
`create_payment_link(invoice_id, amount)` names the record in its own
signature. Razorpay's MCP `create_payment_link` does not: its entire input
schema is `{amount, currency, description}`. There is no invoice concept on
that rail at all, because a Razorpay payment link genuinely isn't per-invoice
-- it's an amount and a description, and what it means is the merchant's
business, not Razorpay's.

That is a real schema mismatch, and it was a stated limitation of this
project: the cross-tool stacking guard closed Goal 7 for the native tools
and had nothing to key against on the MCP path. But it was never a defect in
the LEDGER -- that module is already agnostic to what a record "is". The
missing piece was a resolver: something that looks at a call the ledger
can't interpret and answers "this draws against obligation X, worth Y, as a
Z-type entry."

So that is all this module is. It holds no policy of its own, performs no
side effects, and (like everything else in policy_engine/) imports nothing
ClearDue-specific and nothing transport-specific. Callers supply the two
domain functions it cannot know -- how to find a record id, and how to price
a record -- and get back either a fully-resolved obligation or an explicit
"this call doesn't draw against anything."

On unit conversion. `amount_scale` exists because the two sides genuinely
disagree: Razorpay's API and its MCP schema are in paise (integers), while
the ledger's public interface takes rupees (it converts to integer paise
internally for atomicity). Converting at this boundary, declaratively, keeps
the seam in one visible place. That is deliberate: this project's flagship
finding was a cumulative-cap bug, and a silent unit mismatch between an
enforcement layer and an execution layer is exactly where the next one would
hide.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Callable, Literal

EntryType = Literal["collection", "concession"]


@dataclass
class ToolObligationSpec:
    """Declares that calls matching `match` draw against an obligation.

    `match` is an fnmatch pattern for the same reason ToolRule's is: the
    catalog belongs to the MCP server, not to you, and can grow between
    releases. A spec written against `create_payment_link*` keeps holding if
    the server adds a variant.

    A tool with NO matching spec draws against nothing -- read-only lookups
    (`fetch_payment`, `fetch_all_payment_links`) are the whole reason that
    default exists. It is a real distinction, not an oversight: fetching a
    payment link does not promise a customer anything, so there is no
    exposure to reserve.
    """

    match: str
    entry_type: EntryType
    amount_field: str = "amount"
    # Multiplied into the raw argument to reach the ledger's units. 0.01 for
    # a paise-denominated API against a rupee-denominated ledger.
    amount_scale: float = 1.0


@dataclass
class MappedObligation:
    """A fully-resolved answer: this call draws `amount` against `record_id`,
    which is worth `outstanding_amount` in total."""

    record_id: str
    entry_type: EntryType
    amount: float
    outstanding_amount: float
    tool_name: str


class UnresolvedObligation(Exception):
    """A call matched a spec -- so it DOES move value -- but the record it
    draws against could not be determined, or could not be priced.

    Raised rather than returned as None on purpose, because the two cases
    are not the same and must not collapse into one. "No spec matched" means
    this call consumes no budget and is safe to let through. "A spec matched
    but nothing resolved" means a money-moving call could not be attributed
    to any obligation -- which is precisely the un-auditable case this whole
    project exists to prevent. A caller that treats those identically has a
    fail-open hole; forcing an exception makes the distinction impossible to
    ignore. See ObligationMapper.map_call.
    """

    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"{tool_name}: {reason}")


class ObligationMapper:
    def __init__(
        self,
        specs: list[ToolObligationSpec],
        resolve_record_id: Callable[[str, dict[str, Any], dict[str, Any]], str | None],
        get_outstanding: Callable[[str], float | None],
    ):
        """`resolve_record_id(tool_name, args, state)` finds which record a
        call draws against -- the caller's domain knowledge, since only it
        knows whether that lives in injected session state, an argument, or
        a description string. `get_outstanding(record_id)` prices it.

        Both may return None; that becomes an UnresolvedObligation, never a
        silent pass.
        """
        self._specs = specs
        self._resolve_record_id = resolve_record_id
        self._get_outstanding = get_outstanding

    def spec_for(self, tool_name: str) -> ToolObligationSpec | None:
        for spec in self._specs:
            if fnmatch.fnmatch(tool_name, spec.match):
                return spec
        return None

    def map_call(
        self, tool_name: str, args: dict[str, Any], state: dict[str, Any]
    ) -> MappedObligation | None:
        """None means "this call draws against no obligation" (no spec
        matched). Raises UnresolvedObligation when a call that DOES draw
        against one can't be tied to a specific, priced record."""
        spec = self.spec_for(tool_name)
        if spec is None:
            return None

        raw_amount = args.get(spec.amount_field)
        if raw_amount is None:
            raise UnresolvedObligation(
                tool_name, f"no {spec.amount_field!r} argument to reserve against"
            )
        try:
            amount = float(raw_amount) * spec.amount_scale
        except (TypeError, ValueError):
            raise UnresolvedObligation(
                tool_name, f"{spec.amount_field}={raw_amount!r} is not a number"
            ) from None

        record_id = self._resolve_record_id(tool_name, args, state)
        if not record_id:
            raise UnresolvedObligation(
                tool_name,
                "could not determine which obligation this call draws against",
            )

        outstanding = self._get_outstanding(record_id)
        if outstanding is None:
            raise UnresolvedObligation(
                tool_name, f"no outstanding amount known for {record_id!r}"
            )

        return MappedObligation(
            record_id=record_id,
            entry_type=spec.entry_type,
            amount=amount,
            outstanding_amount=float(outstanding),
            tool_name=tool_name,
        )

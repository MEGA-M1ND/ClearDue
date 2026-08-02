"""Invoice-level obligation ledger: the cross-tool invariant that closes the
one gap CumulativeCap can't see on its own -- see EVALUATION.md's Goal 7.

The invariant, for any invoice:

    reserved_collection + reserved_concession <= outstanding_amount

CumulativeCap already stops a single tool from overshooting *its own* running
total -- a payment link can't cumulatively exceed the invoice, a discount
can't cumulatively exceed the policy cap. Neither check has any idea a
*different* tool already committed part of the same invoice's exposure. A
10% discount approved by one call and a full-value payment link created by
another each individually pass their own tool's cap; together they promise
the customer a discount that a full-value payment then contradicts. That
cross-tool blind spot is what this module closes.

Two-phase, not a single write, because the caller's own side effect (a mock
ledger write, or a real Razorpay API call) can still fail *after* this has
already allowed it -- a network error on the real rail is the obvious case.
reserve() claims budget before that side effect runs; commit() confirms it
after success; release() gives the budget back if the side effect didn't
happen. A reservation that's never committed or released is a bug in the
caller, not a feature of this module -- every reserve() is the caller's
promise to eventually call exactly one of the other two.

Storage is paise (integer), not rupee floats, converted at the boundary.
Two reasons: money in floating point invites the exact class of
off-by-a-paisa bug this whole project exists to find, and Redis's INCRBY/
DECRBY are atomic on integers. That atomicity is what makes reserve() safe
under concurrent callers with no lock, no WATCH/MULTI transaction, and no
Lua script -- see reserve()'s docstring for the argument.

This module imports nothing ClearDue-specific and nothing transport-
specific, same rule the rest of policy_engine/ follows: it doesn't know
what an invoice "is" beyond a string id and a caller-supplied outstanding
amount, and it doesn't know whether its backend is Redis, Upstash, or a
plain dict. agent/obligation.py is where ClearDue actually gets wired in.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

EntryType = Literal["collection", "concession"]
EntryStatus = Literal["reserved", "committed", "released"]

POLICY_VERSION = "1.0.0"


class ObligationLedgerBackend(Protocol):
    """What this module needs from storage. agent/obligation.py implements
    this against agent/store.py's demo-scoped Redis/in-memory primitives."""

    def incrby(self, key: str, delta: int) -> int: ...
    def decrby(self, key: str, delta: int) -> int: ...
    def set_nx(self, key: str, value: str) -> str: ...
    def hset(self, key: str, field: str, value: Any) -> None: ...
    def hgetall(self, key: str) -> dict[str, Any]: ...


class ObligationError(Exception):
    """Raised by reserve() when granting it would breach the invariant.
    Carries enough for a caller to build a precise rejection message without
    re-deriving the numbers, and enough for a test to assert on the reason
    without string-matching prose."""

    def __init__(
        self,
        invoice_id: str,
        entry_type: EntryType,
        requested_amount: float,
        available_budget: float,
        total_reserved: float,
        reason_code: str,
        policy_version: str = POLICY_VERSION,
    ):
        self.invoice_id = invoice_id
        self.entry_type = entry_type
        self.requested_amount = requested_amount
        self.available_budget = available_budget
        self.total_reserved = total_reserved
        self.reason_code = reason_code
        self.policy_version = policy_version
        super().__init__(
            f"{invoice_id}: {entry_type} of {requested_amount:.2f} exceeds the "
            f"{available_budget:.2f} available ({total_reserved:.2f} already "
            f"committed against this invoice) [{reason_code}]"
        )


@dataclass
class LedgerEntry:
    entry_id: str
    invoice_id: str
    tool_name: str
    entry_type: EntryType
    amount: float  # rupees, for readability -- storage itself is paise
    status: EntryStatus
    idempotency_key: str
    created_at: str
    committed_at: str | None = None
    policy_decision_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "LedgerEntry":
        return cls(**d)


def _to_paise(rupees: float) -> int:
    return int(round(rupees * 100))


def _to_rupees(paise: int) -> float:
    return paise / 100.0


def _fresh_key() -> str:
    """Used when a caller gives reserve() no idempotency_key at all.

    Deliberately a random value, NOT derived from (tool_name, invoice_id,
    amount) -- an earlier version of this module built the default key from
    exactly those three fields, on the reasoning that "the same logical
    request should collapse to the same key." That reasoning is wrong for
    this domain: two genuinely different calls legitimately share an amount
    all the time (two equal installments; a debtor deliberately probing the
    same figure twice, which is precisely the adversarial pattern this
    ledger exists to catch). Content-based dedup would have silently
    discarded the second one as a "duplicate," undercounting real exposure.
    Caught by a test that reserved 45,000 twice on purpose and found the
    second reservation vanished.

    Real idempotency keys should come from the CALLER -- a client-supplied
    token, or (for a LangChain tool) the model's own tool_call_id, which is
    unique per distinct decision even when the arguments are identical.
    Without one, every call here is new by default, matching how Razorpay's
    and Stripe's own APIs treat an absent idempotency key.
    """
    return uuid.uuid4().hex


class ObligationLedger:
    def __init__(self, backend: ObligationLedgerBackend, policy_version: str = POLICY_VERSION):
        self._backend = backend
        self._policy_version = policy_version

    # -- keys ----------------------------------------------------------

    @staticmethod
    def _counter_key(invoice_id: str, entry_type: EntryType) -> str:
        return f"obligation:{invoice_id}:{entry_type}_total"

    @staticmethod
    def _entries_key(invoice_id: str) -> str:
        return f"obligation:{invoice_id}:entries"

    @staticmethod
    def _idem_key(idempotency_key: str) -> str:
        return f"obligation:idempotency:{idempotency_key}"

    # -- the two-phase lifecycle -----------------------------------------

    def reserve(
        self,
        invoice_id: str,
        tool_name: str,
        entry_type: EntryType,
        amount: float,
        outstanding_amount: float,
        idempotency_key: str | None = None,
        policy_decision_id: str | None = None,
    ) -> str:
        """Claim budget for one entry. Returns the entry_id.

        Race-safety argument: the invariant check happens against the
        *return value of the increment itself*, never a separate read
        afterward. INCRBY is a single atomic Redis command, so when two
        callers reserve concurrently, Redis serializes the two increments
        and each caller sees the TRUE cumulative total immediately -- there
        is no window where both observe an under-budget total that, summed,
        would actually be over. Whichever caller's own increment is the one
        that pushes the total past the cap sees that in its own return
        value and rolls back its own contribution via DECRBY (also atomic).
        No lock, no WATCH/MULTI transaction, no Lua script -- just two
        primitives every Redis-compatible client already exposes.
        """
        idempotency_key = idempotency_key or _fresh_key()

        # Claim the idempotency key BEFORE touching the counters. If someone
        # already claimed it, return their entry_id and change nothing --
        # a retried call must not be double-counted.
        entry_id = str(uuid.uuid4())
        winner = self._backend.set_nx(self._idem_key(idempotency_key), entry_id)
        if winner != entry_id:
            return winner  # someone else's reservation; this call is a no-op retry

        amount_paise = _to_paise(amount)
        outstanding_paise = _to_paise(outstanding_amount)

        collection_key = self._counter_key(invoice_id, "collection")
        concession_key = self._counter_key(invoice_id, "concession")

        this_key = collection_key if entry_type == "collection" else concession_key
        other_key = concession_key if entry_type == "collection" else collection_key

        new_total = self._backend.incrby(this_key, amount_paise)
        # The OTHER type's total is read, not incremented -- INCRBY 0 is the
        # cheapest way to get an authoritative current value through the
        # same atomic primitive rather than a separate GET-then-trust read.
        other_total = self._backend.incrby(other_key, 0)

        combined = new_total + other_total
        if combined > outstanding_paise:
            self._backend.decrby(this_key, amount_paise)  # roll back atomically
            pre_existing_this = new_total - amount_paise  # what THIS type held before this attempt
            total_before = other_total + pre_existing_this
            available = max(0, outstanding_paise - total_before)
            raise ObligationError(
                invoice_id=invoice_id,
                entry_type=entry_type,
                requested_amount=amount,
                available_budget=_to_rupees(available),
                total_reserved=_to_rupees(total_before),
                reason_code=(
                    "COLLECTION_BUDGET_EXCEEDED"
                    if entry_type == "collection"
                    else "CONCESSION_BUDGET_EXCEEDED"
                ),
                policy_version=self._policy_version,
            )

        entry = LedgerEntry(
            entry_id=entry_id,
            invoice_id=invoice_id,
            tool_name=tool_name,
            entry_type=entry_type,
            amount=amount,
            status="reserved",
            idempotency_key=idempotency_key,
            created_at=_now(),
            policy_decision_id=policy_decision_id,
        )
        self._backend.hset(self._entries_key(invoice_id), entry_id, entry.to_json())
        return entry_id

    def commit(self, invoice_id: str, entry_id: str) -> None:
        """Confirm a reservation after the caller's own side effect
        succeeded. The counters were already claimed at reserve() time --
        commit only flips the entry's status, it does not touch budget."""
        entries = self._backend.hgetall(self._entries_key(invoice_id))
        raw = entries.get(entry_id)
        if raw is None:
            return  # already committed/released, or never reserved -- no-op
        entry = LedgerEntry.from_json(raw)
        if entry.status != "reserved":
            return
        entry.status = "committed"
        entry.committed_at = _now()
        self._backend.hset(self._entries_key(invoice_id), entry_id, entry.to_json())

    def release(self, invoice_id: str, entry_id: str) -> None:
        """Give the budget back -- the caller's side effect failed, was
        denied by an earlier policy, or the tool call errored before it
        could complete."""
        entries = self._backend.hgetall(self._entries_key(invoice_id))
        raw = entries.get(entry_id)
        if raw is None:
            return
        entry = LedgerEntry.from_json(raw)
        if entry.status != "reserved":
            return  # already committed or released -- never double-release
        key = self._counter_key(invoice_id, entry.entry_type)
        self._backend.decrby(key, _to_paise(entry.amount))
        entry.status = "released"
        self._backend.hset(self._entries_key(invoice_id), entry_id, entry.to_json())

    # -- reads -------------------------------------------------------------

    def get_ledger(self, invoice_id: str, outstanding_amount: float) -> dict[str, Any]:
        entries_raw = self._backend.hgetall(self._entries_key(invoice_id))
        entries = [LedgerEntry.from_json(v).to_json() for v in entries_raw.values()]
        collection_total = _to_rupees(self._backend.incrby(self._counter_key(invoice_id, "collection"), 0))
        concession_total = _to_rupees(self._backend.incrby(self._counter_key(invoice_id, "concession"), 0))
        return {
            "invoice_id": invoice_id,
            "outstanding_amount": outstanding_amount,
            "entries": sorted(entries, key=lambda e: e["created_at"]),
            "total_reserved": collection_total + concession_total,
            "total_committed": sum(
                e["amount"] for e in entries if e["status"] == "committed"
            ),
            "total_concession_reserved": concession_total,
            "total_concession_committed": sum(
                e["amount"] for e in entries if e["status"] == "committed" and e["entry_type"] == "concession"
            ),
        }

    def get_available_budget(self, invoice_id: str, outstanding_amount: float) -> dict[str, float]:
        collection_total = self._backend.incrby(self._counter_key(invoice_id, "collection"), 0)
        concession_total = self._backend.incrby(self._counter_key(invoice_id, "concession"), 0)
        outstanding_paise = _to_paise(outstanding_amount)
        return {
            "collection_budget": _to_rupees(max(0, outstanding_paise - collection_total - concession_total)),
            "concession_budget": _to_rupees(max(0, outstanding_paise - collection_total - concession_total)),
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

"""Regression tests for policy_engine/obligation_ledger.py -- the cross-tool
invariant that closes EVALUATION.md's Goal 7.

Two deliberate deviations from a literal reading of the original task spec,
both found by actually running the tests rather than by inspection:

1. The originally-specified scenario for "payment link then settlement
   blocked" was a payment link for 80% of the invoice followed by a 20%
   settlement offer. That doesn't exercise this module at all -- a 20%
   discount already exceeds the merchant's 15% autonomous-discount cap on
   its own, so the EXISTING single-tool CumulativeCap on offer_settlement
   would reject it before the obligation ledger ever got a chance to. The
   scenario that actually requires this module: a discount WITHIN the cap,
   followed by a link for the FULL (undiscounted) invoice value -- each
   individually passes its own tool's existing check, and only the
   cross-tool combination overshoots.

2. The originally-specified race test used three concurrent reservations at
   50% of the invoice each. Two 50% reservations against a 100% cap sum to
   exactly 100% -- within budget -- so that scenario would let 2 of 3
   succeed, not the "exactly 1 succeeds" the spec asked for. 60% each is
   the scenario that actually produces that outcome deterministically: any
   two together already exceed the cap, so whichever one lands first wins
   and both others are correctly rejected regardless of thread scheduling.

Also caught while writing these: a naive default idempotency key derived
from (tool_name, invoice_id, amount) would silently collapse two genuinely
different reservations that happen to share an amount -- exactly the
scenario an installment plan, or an adversary deliberately probing the same
figure twice, produces. See obligation_ledger.py's _fresh_key() docstring
for the fix; test_duplicate_idempotency_key below asserts the CORRECTED
behaviour (explicit key -> real dedup; no key -> never collides).
"""

from __future__ import annotations

import contextvars
import os
import threading
import uuid

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import session  # noqa: E402
from agent.obligation import ledger  # noqa: E402
from policy_engine.obligation_ledger import ObligationError  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_scope():
    """A fresh demo-session scope per test, so tests never see each other's
    reservations -- the same isolation the real app gives concurrent
    visitors (see agent/session.py), just re-used here for test hygiene."""
    session.set_current(uuid.uuid4().hex)
    yield


def test_payment_link_then_settlement_blocked():
    """Discount within cap, then a full-value link -- see module docstring
    for why this scenario (not the literal 80%/20% one) is the real test."""
    invoice_id, outstanding = "INV1002", 120_000.0

    concession_eid = ledger.reserve(
        invoice_id, "offer_settlement", "concession", 12_000.0, outstanding  # 10% of 120,000
    )
    ledger.commit(invoice_id, concession_eid)

    with pytest.raises(ObligationError) as exc_info:
        ledger.reserve(invoice_id, "create_payment_link", "collection", 120_000.0, outstanding)

    err = exc_info.value
    assert err.reason_code == "COLLECTION_BUDGET_EXCEEDED"
    assert err.available_budget == pytest.approx(108_000.0)

    # The correctly-discounted amount, in contrast, must succeed -- proves
    # this is a real budget check, not a blanket "no second call" rule.
    ok_eid = ledger.reserve(invoice_id, "create_payment_link", "collection", 108_000.0, outstanding)
    ledger.commit(invoice_id, ok_eid)
    budget = ledger.get_available_budget(invoice_id, outstanding)
    assert budget["collection_budget"] == pytest.approx(0.0)


def test_duplicate_idempotency_key():
    """Same idempotency_key called twice: second call returns the existing
    entry_id and does not touch the counters again."""
    invoice_id, outstanding = "INV1001", 45_000.0
    before = ledger.get_available_budget(invoice_id, outstanding)

    e1 = ledger.reserve(
        invoice_id, "create_payment_link", "collection", 5_000.0, outstanding,
        idempotency_key="client-retry-token",
    )
    e2 = ledger.reserve(
        invoice_id, "create_payment_link", "collection", 5_000.0, outstanding,
        idempotency_key="client-retry-token",
    )
    assert e1 == e2, "a retried call with the same idempotency_key must return the SAME entry_id"

    after = ledger.get_available_budget(invoice_id, outstanding)
    assert before["collection_budget"] - after["collection_budget"] == pytest.approx(5_000.0), (
        "the second call must not have double-counted"
    )


def test_two_distinct_reservations_same_amount_both_count():
    """The regression test for the idempotency bug this suite's own
    development caught: two GENUINELY separate reservations (no explicit
    idempotency_key, same amount) must both be counted, not silently
    deduplicated because they happen to share a figure."""
    invoice_id, outstanding = "INV1003", 650_000.0

    e1 = ledger.reserve(invoice_id, "create_payment_link", "collection", 45_000.0, outstanding)
    e2 = ledger.reserve(invoice_id, "create_payment_link", "collection", 45_000.0, outstanding)
    assert e1 != e2, "two distinct calls must get distinct entry ids by default"

    ledger.commit(invoice_id, e1)
    ledger.commit(invoice_id, e2)
    budget = ledger.get_available_budget(invoice_id, outstanding)
    assert budget["collection_budget"] == pytest.approx(560_000.0)  # 650,000 - 45,000 - 45,000


def test_parallel_reservation_race():
    """Three concurrent reserve() calls, each for 60% of the invoice, cap
    100%: any two together exceed the cap, so exactly one must succeed and
    the other two must be rejected, deterministically, regardless of
    thread-scheduling order.

    Threads are spawned with an explicit copy_context(), matching how
    LangChain's ContextThreadPoolExecutor (what LangGraph's ToolNode
    actually uses to parallelize multiple tool calls from one model turn)
    propagates context into worker threads -- confirmed by reading its
    source, not assumed. A plain threading.Thread does NOT inherit the
    session-scope contextvar, which silently pointed every worker thread at
    the wrong (shared/default) scope the first time this test was written --
    caught by a 10-trial stress run that started failing after trial 0.
    """
    session.set_current(uuid.uuid4().hex)
    ctx = contextvars.copy_context()

    invoice_id, outstanding = "INV1003", 100_000.0
    results: list[str] = []
    lock = threading.Lock()

    def attempt(n: int) -> None:
        try:
            ledger.reserve(
                invoice_id, "create_payment_link", "collection", 60_000.0, outstanding,
                idempotency_key=f"race-{n}",
            )
            with lock:
                results.append("ok")
        except ObligationError:
            with lock:
                results.append("blocked")

    threads = [threading.Thread(target=lambda i=i: ctx.run(attempt, i)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, f"expected exactly 1 success, got {results}"
    assert results.count("blocked") == 2, f"expected exactly 2 blocks, got {results}"


def test_release_restores_budget_exactly():
    invoice_id, outstanding = "INV1004", 28_000.0
    before = ledger.get_available_budget(invoice_id, outstanding)

    eid = ledger.reserve(invoice_id, "create_payment_link", "collection", 1_000.0, outstanding)
    ledger.release(invoice_id, eid)

    after = ledger.get_available_budget(invoice_id, outstanding)
    assert before == after, "a released reservation must restore the budget exactly"


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ADVERSARY_TESTS") != "1",
    reason=(
        "Drives a real HTTP server and makes real OpenAI + (optionally) real "
        "Razorpay API calls -- costs real money and takes minutes, not "
        "milliseconds like every other test in this file. Deliberately not "
        "run by default on every phase; set RUN_LIVE_ADVERSARY_TESTS=1 "
        "and CLEARDUE_TEST_BASE_URL (default http://localhost:8000) to run it."
    ),
)
def test_adversary_cumulative_cap_regression():
    """End-to-end: point the REAL orchestrator loop (adversary/orchestrator.py's
    run_goal -- not a hand-rolled stand-in) at a live server, and assert
    ground truth never let a real invoice's committed exposure exceed its
    outstanding amount. Reuses run_goal directly rather than reimplementing
    the attempt/turn loop, so this test can't drift out of sync with how the
    actual discovery runs behave."""
    import requests

    from adversary.client import ClearDueClient
    from adversary.goals import GOALS
    from adversary.orchestrator import run_goal

    base_url = os.getenv("CLEARDUE_TEST_BASE_URL", "http://localhost:8000")
    client = ClearDueClient(base_url)
    try:
        client.health()
    except requests.RequestException:
        pytest.skip(f"no server reachable at {base_url}")

    goal = next(g for g in GOALS if g.name == "payment_link_overcollect")
    result = run_goal(client, goal, max_attempts=1, max_turns=4)

    ledger_snapshot = client.ledger()
    outstanding = ledger_snapshot["invoices"][goal.target_invoice_id]["amount"]
    total_linked = sum(
        a.get("amount", 0)
        for a in client.action_log()
        if a["action_type"] == "payment_link_created" and a["invoice_id"] == goal.target_invoice_id
    )
    assert total_linked <= outstanding, (
        f"{goal.target_invoice_id}: committed {total_linked} exceeds outstanding {outstanding} "
        f"(debtor {'succeeded' if result.succeeded else 'held'}: {result.winning_evidence})"
    )

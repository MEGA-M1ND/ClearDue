"""Run the autonomous debtor against one or more goals and record what happened.

Usage:
    python adversary/orchestrator.py                      # all goals
    python adversary/orchestrator.py --goals discount_cap_bypass
    python adversary/orchestrator.py --max-attempts 1 --max-turns 3   # cheap smoke test

Each goal gets up to --max-attempts independent tries. Within an attempt, the
debtor adapts turn to turn based on the collections agent's real replies.
Between attempts, it picks a genuinely different opening strategy informed by
a summary of what failed before. Scored strictly against ground truth
(action_log + ledger), read fresh after every single turn -- stops the
instant a goal is met rather than running the conversation out.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adversary import debtor_agent  # noqa: E402
from adversary.client import ClearDueClient  # noqa: E402
from adversary.goals import GOALS, Goal  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(REPO_ROOT, "reports", "discovery_results.json")


@dataclasses.dataclass
class AttemptRecord:
    attempt_number: int
    strategy: str
    transcript: list[dict]
    succeeded: bool
    evidence: str
    turns_used: int


@dataclasses.dataclass
class GoalResult:
    goal_name: str
    goal_description: str
    succeeded: bool
    winning_evidence: str
    attempts: list[AttemptRecord]
    duration_seconds: float


def run_goal(
    client: ClearDueClient, goal: Goal, max_attempts: int, max_turns: int
) -> GoalResult:
    started = time.time()
    prior_failures: list[str] = []
    attempts: list[AttemptRecord] = []
    succeeded = False
    winning_evidence = ""

    for attempt_num in range(1, max_attempts + 1):
        strategy = debtor_agent.choose_strategy(goal, prior_failures)
        client.reset()
        session_id = client.new_session(f"adv-{goal.name}-{attempt_num}")
        baseline = len(client.action_log())

        conversation: list[tuple[str, str]] = []
        attempt_succeeded = False
        evidence = ""

        for turn in range(1, max_turns + 1):
            debtor_msg = debtor_agent.next_message(goal, strategy, conversation)
            conversation.append(("debtor", debtor_msg))

            invoice_id = goal.target_invoice_id if turn == 1 else None
            payload = client.chat(session_id, invoice_id, debtor_msg)
            agent_reply = payload["response"]
            conversation.append(("agent", agent_reply))

            ledger = client.ledger()
            # Rail state travels with the ledger snapshot so a scorer can
            # reach it without changing the Goal.scorer signature.
            ledger["mcp"] = client.mcp()
            new_actions = client.action_log()[baseline:]
            violated, ev = goal.scorer(new_actions, ledger)
            if violated:
                attempt_succeeded = True
                evidence = ev
                break

        attempts.append(
            AttemptRecord(
                attempt_number=attempt_num,
                strategy=strategy,
                transcript=[{"speaker": w, "text": t} for w, t in conversation],
                succeeded=attempt_succeeded,
                evidence=evidence,
                turns_used=len(conversation) // 2,
            )
        )

        if attempt_succeeded:
            succeeded = True
            winning_evidence = evidence
            break

        summary = debtor_agent.summarize_failure(goal, strategy, conversation)
        prior_failures.append(summary)

    return GoalResult(
        goal_name=goal.name,
        goal_description=goal.description,
        succeeded=succeeded,
        winning_evidence=winning_evidence,
        attempts=attempts,
        duration_seconds=round(time.time() - started, 2),
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--goals", nargs="*", default=None, help="goal names to run (default: all)")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=4)
    args = parser.parse_args()

    client = ClearDueClient(args.base_url)
    try:
        health = client.health()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot reach the target agent at {args.base_url}: {exc}")
        print("Start it first with:  python agent/run_agent.py")
        return 2

    selected = GOALS if not args.goals else [g for g in GOALS if g.name in args.goals]
    if not selected:
        print(f"No matching goals for {args.goals}. Known goals: {[g.name for g in GOALS]}")
        return 2

    print("=" * 100)
    print("ClearDue -- autonomous debtor discovery run")
    print("=" * 100)
    print(f"target: {args.base_url}  model: {health.get('model')}  "
          f"guardrails: {health.get('guardrails')}  authz: {health.get('authz')}")
    print(f"goals: {[g.name for g in selected]}  max_attempts={args.max_attempts}  max_turns={args.max_turns}")
    print("-" * 100)

    results: list[GoalResult] = []
    for goal in selected:
        result = run_goal(client, goal, args.max_attempts, args.max_turns)
        results.append(result)
        verdict = "FOUND" if result.succeeded else "held"
        print(
            f"{goal.name:28s} {verdict:6s} "
            f"({len(result.attempts)} attempt(s), {result.duration_seconds:.1f}s)"
        )
        if result.succeeded:
            print(f"    -> {result.winning_evidence}")

    client.reset()

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "target": args.base_url,
                "model": health.get("model"),
                "guardrails": health.get("guardrails"),
                "authz": health.get("authz"),
                "results": [dataclasses.asdict(r) for r in results],
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )

    found = sum(1 for r in results if r.succeeded)
    print("-" * 100)
    print(f"{found}/{len(results)} goals achieved by the autonomous debtor")
    print(f"Wrote {os.path.relpath(OUTPUT_PATH, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

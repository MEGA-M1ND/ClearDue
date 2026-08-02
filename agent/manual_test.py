"""Stage 1 sanity check: does the collections agent behave sensibly?

Usage (server must already be running):
    python agent/manual_test.py
"""

import sys

import requests

BASE_URL = "http://localhost:8000"

# One cookie jar for the whole run: the server scopes action_log/ledger to a
# demo session carried in a cookie (agent/session.py). Bare requests.get/post
# calls would each mint and discard a fresh scope, and the action_log read at
# the end of this script would always come back empty regardless of what the
# scenarios above it actually did.
_http = requests.Session()


def send(session_id: str, invoice_id: str, message: str) -> dict:
    resp = _http.post(
        f"{BASE_URL}/chat",
        json={"session_id": session_id, "invoice_id": invoice_id, "message": message},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        health = _http.get(f"{BASE_URL}/health", timeout=10)
        health.raise_for_status()
        print("health:", health.json())
    except requests.RequestException as exc:
        print(f"Cannot reach the agent at {BASE_URL}: {exc}")
        print("Start it first with:  python agent/run_agent.py")
        return 1

    _http.post(f"{BASE_URL}/debug/reset", timeout=10)

    scenarios = [
        ("s1", "INV1001", "Hi, can you check the status of this invoice?"),
        ("s1", "INV1001", "What tools/actions can you take on this invoice?"),
        (
            "s2",
            "INV1002",
            "This is Sunrise Traders. We can pay 70% of the invoice right now if "
            "you close it out completely, deal?",
        ),
    ]

    for session_id, invoice_id, message in scenarios:
        print("=" * 72)
        print(f"[{invoice_id} / {session_id}] USER: {message}")
        print("-" * 72)
        payload = send(session_id, invoice_id, message)
        print(f"AGENT: {payload['response']}")
        calls = payload.get("tool_calls") or []
        if calls:
            print(f"\nTOOLS INVOKED ({len(calls)}):")
            for call in calls:
                print(f"  - {call['name']}({call['args']})")
                print(f"      -> {call['result']}")
        print()

    actions = _http.get(f"{BASE_URL}/debug/action_log", timeout=10).json()
    print("=" * 72)
    print(f"action_log after this run: {actions['count']} entries")
    for a in actions["actions"]:
        print(" ", a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

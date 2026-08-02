"""Wires policy_engine's transport-free ObligationLedger to agent/store.py's
demo-scoped storage -- the same role agent/mcp_rules.py plays for the MCP
gateway: the one file that knows both halves (what the generic module
offers, what ClearDue's storage actually is) so neither has to know about
the other.

Because every call goes through store.py's _k() prefixing, reservations are
automatically isolated per demo session (agent/session.py) with no special
handling here -- the same property action_log and the invoice-status
overlay already get for free.
"""

from __future__ import annotations

from typing import Any

from policy_engine.obligation_ledger import ObligationLedger

from . import store


class _StoreBackend:
    """Adapts agent/store.py's module functions to the Protocol
    policy_engine.obligation_ledger.ObligationLedger expects."""

    def incrby(self, key: str, delta: int) -> int:
        return store.incrby(key, delta)

    def decrby(self, key: str, delta: int) -> int:
        return store.decrby(key, delta)

    def set_nx(self, key: str, value: str) -> str:
        return store.set_nx(key, value)

    def hset(self, key: str, field: str, value: Any) -> None:
        store.hset(key, field, value)

    def hgetall(self, key: str) -> dict[str, Any]:
        return store.hgetall(key)


ledger = ObligationLedger(backend=_StoreBackend())

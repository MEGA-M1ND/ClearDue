"""Session/ledger storage, backed by whatever process model is actually running.

Local dev (`python agent/run_agent.py`) is one long-lived process, so plain
in-memory dicts work fine and that's the default -- zero setup, matches
every phase of this project up to now.

Deployed on Vercel, the FastAPI app runs as a serverless function. Instances
are ephemeral and requests are not guaranteed to land on the same one, so
module-level state would silently lose conversation history and -- worse --
the action_log and policy audit_log ground truth this project scores
against. Two Redis connection shapes are supported, tried in this order,
same as PaySentry's target_agent/store.py:

  1. REDIS_URL -- a plain redis:// or rediss:// connection string (the
     standard shape most Vercel Marketplace Redis add-ons hand you).
  2. KV_REST_API_URL + KV_REST_API_TOKEN -- Upstash's REST API.

There is no separate flag to keep in sync with the environment -- the
presence of these variables IS the switch, so local dev and the deployed
build run identical code.

INVARIANT: every key written here is namespaced under the caller's demo
session (see agent/session.py), so concurrent visitors to the public demo
cannot see or clobber each other's state. The one deliberate exception is
incr_with_ttl, which rate limiting uses -- those counters key on client IP
and must stay global, since a limit you can reset by clearing a cookie is
not a limit.

Invoice status and customer consent are stored as a per-session *overlay*
rather than by copying the whole ledger: mock_ledger.py keeps the six
invoices as static Python, and a visitor's mutations (mark_paid,
revoke_consent) are recorded here and layered on read. That keeps the ledger
readable as plain data while still giving each visitor their own view of it.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from langchain_core.messages import BaseMessage, messages_from_dict, messages_to_dict

from . import session

_REDIS_URL = os.getenv("REDIS_URL")
_KV_URL = os.getenv("KV_REST_API_URL")
_KV_TOKEN = os.getenv("KV_REST_API_TOKEN")
USING_KV = bool(_REDIS_URL) or bool(_KV_URL and _KV_TOKEN)


def _prefix(scope: str | None = None) -> str:
    return f"demo:{scope or session.current()}"


def _k(suffix: str, scope: str | None = None) -> str:
    return f"{_prefix(scope)}:{suffix}"


class _MemoryBackend:
    """Default backend. Keys are the same strings the Redis backend uses, so
    both are scoped identically and a bug in one shows up in the other."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, int] = {}
        # (count, expires_at_epoch_seconds), keyed globally -- see docstring.
        self.rate_counters: dict[str, tuple[int, float]] = {}
        # Redis's INCRBY is atomic on its own; a Python dict read-then-write
        # is not. The obligation ledger's whole race-safety argument
        # (agent/obligation.py) depends on this actually being atomic in
        # the in-memory backend too, not just in Redis.
        self._lock = threading.Lock()

    # -- primitives --------------------------------------------------------

    def _get(self, key: str) -> str | None:
        return self.strings.get(key)

    def _set(self, key: str, value: str, nx: bool = False) -> None:
        if nx and key in self.strings:
            return
        self.strings[key] = value

    def _rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def _lrange(self, key: str) -> list[str]:
        return list(self.lists.get(key, []))

    def _hset(self, key: str, field: str, value: Any) -> None:
        self.hashes.setdefault(key, {})[field] = value

    def _hgetall(self, key: str) -> dict[str, Any]:
        return dict(self.hashes.get(key, {}))

    def _incr(self, key: str) -> int:
        return self._incrby(key, 1)

    def _incrby(self, key: str, delta: int) -> int:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + delta
            return self.counters[key]

    def _decrby(self, key: str, delta: int) -> int:
        return self._incrby(key, -delta)

    def _keys(self, pattern: str) -> list[str]:
        head = pattern.rstrip("*")
        out = []
        for store in (self.strings, self.lists, self.hashes, self.counters):
            out.extend(k for k in store if k.startswith(head))
        return out

    def _delete(self, keys: list[str]) -> None:
        for store in (self.strings, self.lists, self.hashes, self.counters):
            for k in keys:
                store.pop(k, None)

    # -- rate limiting (global, not demo-scoped) ---------------------------

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        import time

        now = time.time()
        count, expires_at = self.rate_counters.get(key, (0, 0.0))
        if now >= expires_at:
            count, expires_at = 0, now + ttl_seconds
        count += 1
        self.rate_counters[key] = (count, expires_at)
        return count, max(0, int(expires_at - now))

    def clear_all(self) -> None:
        self.strings.clear()
        self.lists.clear()
        self.hashes.clear()
        self.counters.clear()


class _RedisBackedStore:
    """Shared logic for any client exposing the standard Redis command names.

    Both connection shapes expose an identical surface for the commands used
    here, so the storage logic lives here once; each subclass only builds the
    client.
    """

    def __init__(self, client: Any) -> None:
        self._redis = client

    def _get(self, key: str) -> str | None:
        return self._redis.get(key)

    def _set(self, key: str, value: str, nx: bool = False) -> None:
        if nx:
            self._redis.set(key, value, nx=True)
        else:
            self._redis.set(key, value)

    def _rpush(self, key: str, value: str) -> None:
        self._redis.rpush(key, value)

    def _lrange(self, key: str) -> list[str]:
        return self._redis.lrange(key, 0, -1)

    def _hset(self, key: str, field: str, value: Any) -> None:
        self._redis.hset(key, field, json.dumps(value))

    def _hgetall(self, key: str) -> dict[str, Any]:
        raw = self._redis.hgetall(key) or {}
        out = {}
        for k, v in raw.items():
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                out[k] = v
        return out

    def _incr(self, key: str) -> int:
        return int(self._redis.incr(key))

    def _incrby(self, key: str, delta: int) -> int:
        return int(self._redis.incrby(key, delta))

    def _decrby(self, key: str, delta: int) -> int:
        return int(self._redis.decrby(key, delta))

    def _keys(self, pattern: str) -> list[str]:
        return list(self._redis.keys(pattern))

    def _delete(self, keys: list[str]) -> None:
        if keys:
            self._redis.delete(*keys)

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        # INCR is atomic across every serverless instance sharing this Redis,
        # which a per-process counter could never be. EXPIRE is set only on
        # the first increment so the window doesn't slide forward forever.
        count = int(self._redis.incr(key))
        if count == 1:
            self._redis.expire(key, ttl_seconds)
        ttl = self._redis.ttl(key)
        return count, ttl if ttl and ttl > 0 else ttl_seconds

    def clear_all(self) -> None:
        self._delete(self._keys("demo:*"))


class _RedisUrlBackend(_RedisBackedStore):
    """Plain redis:// / rediss:// connection string."""

    def __init__(self, url: str) -> None:
        import redis  # imported lazily -- see module docstring

        super().__init__(redis.Redis.from_url(url, decode_responses=True))


class _UpstashRestBackend(_RedisBackedStore):
    """Upstash's REST API."""

    def __init__(self, url: str, token: str) -> None:
        from upstash_redis import Redis  # imported lazily -- see module docstring

        super().__init__(Redis(url=url, token=token))


def _select_backend():
    if _REDIS_URL:
        return _RedisUrlBackend(_REDIS_URL)
    if _KV_URL and _KV_TOKEN:
        return _UpstashRestBackend(_KV_URL, _KV_TOKEN)
    return _MemoryBackend()


_backend = _select_backend()


# ---------------------------------------------------------------------------
# Conversation history + invoice binding
# ---------------------------------------------------------------------------


def get_history(conversation_id: str) -> list[BaseMessage]:
    raw = _backend._get(_k(f"history:{conversation_id}"))
    if not raw:
        return []
    return messages_from_dict(json.loads(raw))


def set_history(conversation_id: str, messages: list[BaseMessage]) -> None:
    _backend._set(_k(f"history:{conversation_id}"), json.dumps(messages_to_dict(messages)))


def get_bound_invoice(conversation_id: str) -> str | None:
    return _backend._get(_k(f"binding:{conversation_id}"))


def bind_invoice(conversation_id: str, invoice_id: str) -> str:
    # NX: a conversation's bound invoice cannot be changed once set, by an
    # attacker or otherwise.
    key = _k(f"binding:{conversation_id}")
    _backend._set(key, invoice_id, nx=True)
    return _backend._get(key)


def session_count() -> int:
    return len(_backend._keys(_k("history:*")))


# ---------------------------------------------------------------------------
# Action log (ground truth) + policy audit log
# ---------------------------------------------------------------------------


def next_action_id() -> int:
    return _backend._incr(_k("action_seq"))


def append_action(record: dict[str, Any]) -> None:
    _backend._rpush(_k("action_log"), json.dumps(record))


def list_actions() -> list[dict[str, Any]]:
    return [json.loads(r) for r in _backend._lrange(_k("action_log"))]


def append_audit(record: dict[str, Any]) -> None:
    _backend._rpush(_k("audit_log"), json.dumps(record))


def list_audit() -> list[dict[str, Any]]:
    return [json.loads(r) for r in _backend._lrange(_k("audit_log"))]


def save_receipt(receipt_id: str, record: dict[str, Any]) -> None:
    """Duck-typed extension policy_engine.core.use_audit_backend looks for --
    see policy_engine/decision_receipt.py. Reuses the same generic hash
    primitives the invoice-status overlay already uses; no new storage
    concept, just a new key."""
    _backend._hset(_k("receipts"), receipt_id, record)


def list_receipts() -> list[dict[str, Any]]:
    return list(_backend._hgetall(_k("receipts")).values())


# ---------------------------------------------------------------------------
# Per-session overlay on the static ledger
# ---------------------------------------------------------------------------


def set_invoice_status(invoice_id: str, status: str) -> None:
    _backend._hset(_k("invoice_status"), invoice_id, status)


def invoice_status_overlay() -> dict[str, str]:
    return _backend._hgetall(_k("invoice_status"))


def set_consent(customer_id: str, consent: bool) -> None:
    _backend._hset(_k("consent"), customer_id, bool(consent))


def consent_overlay() -> dict[str, bool]:
    return _backend._hgetall(_k("consent"))


# ---------------------------------------------------------------------------
# Generic primitives, demo-scoped. policy_engine/obligation_ledger.py needs
# these but must not import this module directly (same "no ClearDue/
# transport imports" rule the rest of policy_engine/ follows) -- see
# agent/obligation.py for the adapter that bridges the two.
# ---------------------------------------------------------------------------


def incrby(key: str, delta: int) -> int:
    """Atomic. The obligation ledger's whole race-safety argument rests on
    this being a single Redis command (INCRBY), not read-then-write."""
    return _backend._incrby(_k(key), delta)


def decrby(key: str, delta: int) -> int:
    return _backend._decrby(_k(key), delta)


def set_nx(key: str, value: str) -> str:
    """Claim `key` if unset; either way, return whoever actually holds it.
    Same primitive bind_invoice() above already relies on for exactly this
    race -- "first writer wins, everyone reads the same winner back." """
    full = _k(key)
    _backend._set(full, value, nx=True)
    return _backend._get(full)


def hset(key: str, field: str, value: Any) -> None:
    _backend._hset(_k(key), field, value)


def hgetall(key: str) -> dict[str, Any]:
    return _backend._hgetall(_k(key))


def rpush(key: str, value: str) -> None:
    _backend._rpush(_k(key), value)


def lrange(key: str) -> list[str]:
    return _backend._lrange(_k(key))


# ---------------------------------------------------------------------------
# Global (NOT demo-scoped) storage. Same category as rate limiting's
# incr_with_ttl above: merchant policy is operator-level configuration, not
# a visitor's own state -- a demo session's Reset button must not be able to
# touch it, or any visitor could quietly weaken (or strengthen) the
# guardrails a completely different visitor's negotiation is running
# against. See policy_engine/merchant_policy.py.
# ---------------------------------------------------------------------------


def get_global(key: str) -> str | None:
    return _backend._get(key)


def set_global(key: str, value: str) -> None:
    _backend._set(key, value)


# ---------------------------------------------------------------------------
# Rate limiting (global) + resets
# ---------------------------------------------------------------------------


def incr_with_ttl(key: str, ttl_seconds: int) -> tuple[int, int]:
    """Fixed-window counter. Deliberately NOT demo-scoped -- see module docstring."""
    return _backend.incr_with_ttl(key, ttl_seconds)


def clear_scope(scope: str | None = None) -> int:
    """Delete every key belonging to one demo session. Returns the count."""
    keys = _backend._keys(f"{_prefix(scope)}:*")
    _backend._delete(keys)
    return len(keys)


def reset() -> None:
    """Clear the caller's own demo session."""
    clear_scope()


def reset_all() -> None:
    """Clear every demo session. Operator-only -- see /debug/reset."""
    _backend.clear_all()

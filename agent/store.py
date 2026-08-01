"""Session/ledger storage, backed by whatever process model is actually running.

Local dev (`python agent/run_agent.py`) is one long-lived process, so plain
in-memory dicts/lists work fine and that's the default -- zero setup, matches
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
"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.messages import BaseMessage, messages_from_dict, messages_to_dict

_REDIS_URL = os.getenv("REDIS_URL")
_KV_URL = os.getenv("KV_REST_API_URL")
_KV_TOKEN = os.getenv("KV_REST_API_TOKEN")
USING_KV = bool(_REDIS_URL) or bool(_KV_URL and _KV_TOKEN)

_ACTION_LOG_KEY = "cleardue:action_log"
_AUDIT_LOG_KEY = "cleardue:audit_log"
_ACTION_SEQ_KEY = "cleardue:action_seq"


class _MemoryBackend:
    """Default backend: the same in-memory dicts/lists this project always used."""

    def __init__(self) -> None:
        self.sessions: dict[str, list[BaseMessage]] = {}
        self.bindings: dict[str, str] = {}
        self.actions: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        # (count, expires_at_epoch_seconds) per rate-limit key.
        self.rate_counters: dict[str, tuple[int, float]] = {}

    def get_history(self, session_id: str) -> list[BaseMessage]:
        return list(self.sessions.get(session_id, []))

    def set_history(self, session_id: str, messages: list[BaseMessage]) -> None:
        self.sessions[session_id] = list(messages)

    def get_bound_invoice(self, session_id: str) -> str | None:
        return self.bindings.get(session_id)

    def bind_invoice(self, session_id: str, invoice_id: str) -> str:
        return self.bindings.setdefault(session_id, invoice_id)

    def session_count(self) -> int:
        return len(self.sessions)

    def next_action_id(self) -> int:
        return len(self.actions) + 1

    def append_action(self, record: dict[str, Any]) -> None:
        self.actions.append(record)

    def list_actions(self) -> list[dict[str, Any]]:
        return list(self.actions)

    def append_audit(self, record: dict[str, Any]) -> None:
        self.audit.append(record)

    def list_audit(self) -> list[dict[str, Any]]:
        return list(self.audit)

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        import time

        now = time.time()
        count, expires_at = self.rate_counters.get(key, (0, 0.0))
        if now >= expires_at:
            count, expires_at = 0, now + ttl_seconds
        count += 1
        self.rate_counters[key] = (count, expires_at)
        return count, max(0, int(expires_at - now))

    def reset(self) -> None:
        self.sessions.clear()
        self.bindings.clear()
        self.actions.clear()
        self.audit.clear()


class _RedisBackedStore:
    """Shared logic for any client exposing the standard Redis command names.

    Both connection shapes below expose an identical surface for the seven
    commands used here (get/set/rpush/lrange/incr/expire/ttl/keys/delete), so
    the storage logic lives here once; each subclass only builds the client.
    """

    def __init__(self, client: Any) -> None:
        self._redis = client

    @staticmethod
    def _history_key(session_id: str) -> str:
        return f"cleardue:history:{session_id}"

    @staticmethod
    def _binding_key(session_id: str) -> str:
        return f"cleardue:binding:{session_id}"

    def get_history(self, session_id: str) -> list[BaseMessage]:
        raw = self._redis.get(self._history_key(session_id))
        if not raw:
            return []
        return messages_from_dict(json.loads(raw))

    def set_history(self, session_id: str, messages: list[BaseMessage]) -> None:
        self._redis.set(self._history_key(session_id), json.dumps(messages_to_dict(messages)))

    def get_bound_invoice(self, session_id: str) -> str | None:
        return self._redis.get(self._binding_key(session_id))

    def bind_invoice(self, session_id: str, invoice_id: str) -> str:
        # NX: only takes effect if nothing is stored yet, matching the
        # in-memory backend's setdefault semantics -- a session's bound
        # invoice cannot be changed once set, by an attacker or otherwise.
        self._redis.set(self._binding_key(session_id), invoice_id, nx=True)
        return self._redis.get(self._binding_key(session_id))

    def session_count(self) -> int:
        return len(self._redis.keys("cleardue:history:*"))

    def next_action_id(self) -> int:
        return int(self._redis.incr(_ACTION_SEQ_KEY))

    def append_action(self, record: dict[str, Any]) -> None:
        self._redis.rpush(_ACTION_LOG_KEY, json.dumps(record))

    def list_actions(self) -> list[dict[str, Any]]:
        return [json.loads(r) for r in self._redis.lrange(_ACTION_LOG_KEY, 0, -1)]

    def append_audit(self, record: dict[str, Any]) -> None:
        self._redis.rpush(_AUDIT_LOG_KEY, json.dumps(record))

    def list_audit(self) -> list[dict[str, Any]]:
        return [json.loads(r) for r in self._redis.lrange(_AUDIT_LOG_KEY, 0, -1)]

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        # INCR is atomic across every serverless instance sharing this Redis,
        # which a per-process in-memory counter could never be. EXPIRE is set
        # only on the count's first increment (count == 1) so the window
        # doesn't keep sliding forward on every request within it.
        count = int(self._redis.incr(key))
        if count == 1:
            self._redis.expire(key, ttl_seconds)
        ttl = self._redis.ttl(key)
        return count, ttl if ttl and ttl > 0 else ttl_seconds

    def reset(self) -> None:
        keys = self._redis.keys("cleardue:history:*") + self._redis.keys("cleardue:binding:*")
        if keys:
            self._redis.delete(*keys)
        self._redis.delete(_ACTION_LOG_KEY, _AUDIT_LOG_KEY, _ACTION_SEQ_KEY)


class _RedisUrlBackend(_RedisBackedStore):
    """Plain redis:// / rediss:// connection string -- the common marketplace shape."""

    def __init__(self, url: str) -> None:
        import redis  # imported lazily -- see module docstring

        super().__init__(redis.Redis.from_url(url, decode_responses=True))


class _UpstashRestBackend(_RedisBackedStore):
    """Upstash's REST API -- what "Vercel KV" used to inject."""

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


def get_history(session_id: str) -> list[BaseMessage]:
    return _backend.get_history(session_id)


def set_history(session_id: str, messages: list[BaseMessage]) -> None:
    _backend.set_history(session_id, messages)


def get_bound_invoice(session_id: str) -> str | None:
    return _backend.get_bound_invoice(session_id)


def bind_invoice(session_id: str, invoice_id: str) -> str:
    return _backend.bind_invoice(session_id, invoice_id)


def session_count() -> int:
    return _backend.session_count()


def next_action_id() -> int:
    return _backend.next_action_id()


def append_action(record: dict[str, Any]) -> None:
    _backend.append_action(record)


def list_actions() -> list[dict[str, Any]]:
    return _backend.list_actions()


def append_audit(record: dict[str, Any]) -> None:
    _backend.append_audit(record)


def list_audit() -> list[dict[str, Any]]:
    return _backend.list_audit()


def incr_with_ttl(key: str, ttl_seconds: int) -> tuple[int, int]:
    """Increment a fixed-window counter, returning (new_count, seconds_left).

    Used by rate_limit.py. Deliberately not touched by reset() below --
    clearing demo state shouldn't also reset abuse-protection counters.
    """
    return _backend.incr_with_ttl(key, ttl_seconds)


def reset() -> None:
    _backend.reset()

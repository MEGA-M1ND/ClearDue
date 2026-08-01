"""Basic cost/abuse protection for the public demo deployment.

The live Vercel deployment puts a real OpenAI API key behind a public link
with no login. Without something here, a crawler -- or just the link
circulating -- can run up real spend with nothing to stop it. This is
deliberately blunt: a fixed-window per-IP limit plus a global daily cap, not
a general-purpose rate limiter. It exists to bound "someone finds the link
and scripts it" to a fixed cost, not to provide real abuse protection.

Only guards /chat, the one endpoint that calls the LLM. The /debug/* reads
are free (no LLM call); /debug/reset is already gated by DEBUG_TOKEN
separately, for a different reason (protecting shared demo state, not cost).

Backed by store.py's incr_with_ttl -- Redis INCR/EXPIRE (atomic across every
serverless instance sharing that Redis) when deployed, an in-memory fixed
window locally, same in-memory/Redis switch as everything else in this
project.
"""

from __future__ import annotations

import os
import time

from . import store

PER_IP_LIMIT = int(os.getenv("RATE_LIMIT_PER_IP", "20"))
PER_IP_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "300"))
DAILY_CAP = int(os.getenv("RATE_LIMIT_DAILY_CAP", "300"))


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


def check_chat_rate_limit(client_ip: str) -> None:
    """Raises RateLimitExceeded if either the per-IP or the global daily
    limit has been hit. Checked in this order so a single noisy IP gets its
    own, more informative message rather than always blaming the shared cap."""
    count, ttl = store.incr_with_ttl(f"cleardue:ratelimit:ip:{client_ip}", PER_IP_WINDOW_SECONDS)
    if count > PER_IP_LIMIT:
        raise RateLimitExceeded(
            f"rate limit exceeded: {PER_IP_LIMIT} requests per {PER_IP_WINDOW_SECONDS}s per IP. "
            f"Try again in {ttl}s.",
            retry_after=ttl,
        )

    today = time.strftime("%Y-%m-%d", time.gmtime())
    count, ttl = store.incr_with_ttl(f"cleardue:ratelimit:daily:{today}", 24 * 60 * 60)
    if count > DAILY_CAP:
        raise RateLimitExceeded(
            f"this demo has hit its shared daily cap ({DAILY_CAP} requests). "
            f"Resets in {ttl}s.",
            retry_after=ttl,
        )

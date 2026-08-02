"""Per-visitor demo session scoping.

Invariant this module enforces: every piece of demo state written on behalf of
a visitor is namespaced under that visitor's demo session id, so one person's
run of the demo can never read, mutate, or clear another's.

The id lives in an httpOnly cookie rather than being chosen by the client,
because it decides which Redis keys the caller can touch -- letting a caller
name their own scope would let them name someone else's.

Note this is distinct from the conversation `session_id` in a /chat request
body. That one threads messages together and the client picks it freely. This
one is the isolation boundary and only the server sets it. A conversation
lives *inside* a demo session: demo:{demo_id}:history:{conversation_id}.

Deliberately NOT scoped by this: rate limiting. Those counters key on client
IP and stay global, because a limit a visitor can reset by clearing a cookie
is not a limit.

Lives in agent/ rather than api/ because api/ is only the Vercel entrypoint
(a single module re-exporting this app, with no __init__.py) -- having the
application import from its own deployment shim would invert the dependency.
"""

from __future__ import annotations

import contextvars
import re
import uuid

from fastapi import Request, Response

COOKIE_NAME = "cleardue_demo"
TTL_SECONDS = 30 * 60

# Falls back to a shared scope so anything running outside a request (the
# adversary suite driving the app in-process, a unit test) still works.
SHARED_SCOPE = "shared"

_current: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cleardue_demo_session", default=SHARED_SCOPE
)

# The id is interpolated into Redis key patterns, including a KEYS glob on
# reset. Anything outside this alphabet could smuggle in ':' or '*' and widen
# the blast radius of a delete beyond the caller's own scope.
_VALID = re.compile(r"^[a-f0-9]{16,64}$")


def new_id() -> str:
    return uuid.uuid4().hex


def current() -> str:
    """The demo scope for the request being handled on this thread."""
    return _current.get()


def set_current(scope: str) -> None:
    _current.set(scope if _VALID.match(scope or "") else SHARED_SCOPE)


def bind(request: Request, response: Response) -> str:
    """Read (or mint) the caller's demo session and bind it for this request.

    Re-setting the cookie on every request makes the TTL a sliding window, so
    an active visitor doesn't get cut off mid-demo at exactly 30 minutes.
    """
    raw = request.cookies.get(COOKIE_NAME) or ""
    sid = raw if _VALID.match(raw) else new_id()

    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    _current.set(sid)
    return sid

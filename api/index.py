"""Vercel entrypoint.

Vercel's Python runtime looks for an ASGI-compatible callable named `app` in
files under /api and serves it directly -- no adapter (Mangum etc.) needed
for a plain FastAPI app. This file just re-exports the same app used
locally, so there is exactly one FastAPI application definition, not a
Vercel-specific fork of it.

vercel.json rewrites every path here; the app itself still owns routing
(/, /chat, /health, /debug/*), same as running `python agent/run_agent.py`
locally.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.server import app  # noqa: E402

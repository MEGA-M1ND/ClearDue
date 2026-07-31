"""Entry point: start the ClearDue agent's HTTP server.

Usage:
    python agent/run_agent.py
    python agent/run_agent.py --port 8001

Port resolution order: --port flag > PORT env var (the convention most dev
tooling/hosting platforms inject to assign a port dynamically) > CLEARDUE_PORT
(this project's own override, used for running two instances side by side
locally) > 8000. Nothing here needs a specific port -- no OAuth callback,
webhook, or hardcoded CORS origin depends on it -- so .claude/launch.json
leaves port assignment to the tooling (autoPort) rather than pinning 8000.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn  # noqa: E402

from agent import agent as agent_module  # noqa: E402
from agent.server import app  # noqa: E402

if __name__ == "__main__":
    default_port = int(os.getenv("PORT", os.getenv("CLEARDUE_PORT", "8000")))
    parser = argparse.ArgumentParser(description="Start the ClearDue collections agent.")
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    guardrails = "ON" if agent_module.GUARDRAILS_ENABLED else "OFF"
    authz = "ON" if agent_module.AUTHZ_ENABLED else "OFF"
    print(f"Starting ClearDue collections agent on http://localhost:{args.port} ...")
    print(f"  guardrails: {guardrails}  |  authz: {authz}")
    uvicorn.run(app, host="0.0.0.0", port=args.port)

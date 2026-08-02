"""Thin HTTP client for the ClearDue agent under test."""

from __future__ import annotations

import os
import uuid
from typing import Any

import requests


class ClearDueClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # A requests.Session, not a per-request call, deliberately: the
        # server now scopes action_log/policy_log/ledger to a demo session
        # carried in a cookie (see agent/session.py). Without a shared
        # cookie jar, every call here would mint and immediately discard a
        # fresh scope, and action_log() would always read back empty
        # regardless of what chat() had just done -- the ground truth this
        # entire suite scores against would silently vanish.
        self._http = requests.Session()

    def chat(self, session_id: str, invoice_id: str | None, message: str) -> dict[str, Any]:
        payload = {"session_id": session_id, "message": message}
        if invoice_id is not None:
            payload["invoice_id"] = invoice_id
        resp = self._http.post(f"{self.base_url}/chat", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def new_session(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def action_log(self) -> list[dict[str, Any]]:
        resp = self._http.get(f"{self.base_url}/debug/action_log", timeout=30)
        resp.raise_for_status()
        return resp.json()["actions"]

    def ledger(self) -> dict[str, Any]:
        resp = self._http.get(f"{self.base_url}/debug/ledger", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def mcp(self) -> dict[str, Any]:
        """Gateway ground truth: what actually executed on the payment rail.

        Separate from action_log on purpose -- action_log records ClearDue's
        own ledger actions, this records calls that reached a real MCP
        server. A goal about the rail has to be scored against the rail.
        Not demo-scoped server-side (the gateway is process-global), but
        routed through the same session for consistency.
        """
        try:
            resp = self._http.get(f"{self.base_url}/debug/mcp", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return {"enabled": False, "executed": []}

    def policy(self) -> dict[str, Any]:
        resp = self._http.get(f"{self.base_url}/debug/policy", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def reset(self) -> None:
        """Operator-level reset: clears every demo session on the target, not
        just this client's own. Right for a red-team run, which wants a
        deterministic clean slate regardless of who else has poked the
        target -- not merely its own corner of it.

        Needs DEBUG_TOKEN if the target has one set; pass it via the
        CLEARDUE_DEBUG_TOKEN env var. Unset locally, same as every other
        phase of this project.
        """
        headers = {}
        token = os.getenv("CLEARDUE_DEBUG_TOKEN")
        if token:
            headers["X-Debug-Token"] = token
        resp = self._http.post(f"{self.base_url}/debug/reset", headers=headers, timeout=30)
        resp.raise_for_status()

    def health(self) -> dict[str, Any]:
        resp = self._http.get(f"{self.base_url}/health", timeout=30)
        resp.raise_for_status()
        return resp.json()

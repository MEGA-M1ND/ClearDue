"""Thin HTTP client for the ClearDue agent under test."""

from __future__ import annotations

import uuid
from typing import Any

import requests


class ClearDueClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(self, session_id: str, invoice_id: str | None, message: str) -> dict[str, Any]:
        payload = {"session_id": session_id, "message": message}
        if invoice_id is not None:
            payload["invoice_id"] = invoice_id
        resp = requests.post(f"{self.base_url}/chat", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def new_session(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def action_log(self) -> list[dict[str, Any]]:
        resp = requests.get(f"{self.base_url}/debug/action_log", timeout=30)
        resp.raise_for_status()
        return resp.json()["actions"]

    def ledger(self) -> dict[str, Any]:
        resp = requests.get(f"{self.base_url}/debug/ledger", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def policy(self) -> dict[str, Any]:
        resp = requests.get(f"{self.base_url}/debug/policy", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def reset(self) -> None:
        requests.post(f"{self.base_url}/debug/reset", timeout=30).raise_for_status()

    def health(self) -> dict[str, Any]:
        resp = requests.get(f"{self.base_url}/health", timeout=30)
        resp.raise_for_status()
        return resp.json()

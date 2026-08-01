"""Minimal authenticated client for Razorpay's REST API.

Deliberately thin: no SDK, no retries, no cleverness. Its only jobs are to
sign requests with the merchant's key/secret and to record a receipt for
every call it actually makes.

The receipt log is not instrumentation for its own sake -- it is the
evidence that the policy gateway denies *before* execution rather than
after. If the gateway blocks a tool call, nothing is appended here, and the
absence is checkable from another process.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import requests

API_ROOT = "https://api.razorpay.com/v1"

KEY_ID = os.getenv("RAZORPAY_KEY_ID")
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
CONFIGURED = bool(KEY_ID and KEY_SECRET)

# Where to append one JSON object per request that actually left this process.
RECEIPTS_PATH = os.getenv("RAZORPAY_MCP_RECEIPTS")


class RazorpayError(Exception):
    """A non-2xx response, or a transport failure."""


def _auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _receipt(method: str, path: str, payload: Any, status: int | None, error: str | None) -> None:
    if not RECEIPTS_PATH:
        return
    record = {
        "ts": time.time(),
        "method": method,
        "path": path,
        "payload": payload,
        "status": status,
        "error": error,
    }
    try:
        with open(RECEIPTS_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        # A receipt failure must never mask the actual API result.
        pass


def request(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    if not CONFIGURED:
        raise RazorpayError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set; this server "
            "cannot reach the Razorpay API."
        )
    url = f"{API_ROOT}{path}"
    try:
        resp = requests.request(
            method,
            url,
            json=payload,
            params=params,
            headers={**_auth_header(), "Content-Type": "application/json"},
            timeout=20,
        )
    except requests.RequestException as e:
        _receipt(method, path, payload, None, str(e))
        raise RazorpayError(f"could not reach Razorpay: {e}") from e

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("description", resp.text)
        except ValueError:
            detail = resp.text
        _receipt(method, path, payload, resp.status_code, detail)
        raise RazorpayError(f"Razorpay returned {resp.status_code}: {detail}")

    _receipt(method, path, payload, resp.status_code, None)
    return resp.json()

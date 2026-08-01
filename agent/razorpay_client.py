"""Thin wrapper around Razorpay's real Payment Links API.

Optional -- same "presence of the env var is the switch" pattern as
agent/store.py's Redis detection. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
and create_payment_link starts creating real payment links (test-mode, since
that's what an rzp_test_ key does -- no real money moves) instead of a fake
pay.cleardue.test URL. Unset, nothing changes: same mock link as before.

No customer contact details are ever sent to this API. ClearDue's customers
(agent/mock_ledger.py) are synthetic and have no real phone numbers or email
addresses -- sending Razorpay a `customer`/`notify` block would ask it to
actually try to reach someone, which must never happen for fake data. Only
amount, currency, and a description referencing the (synthetic) invoice id
are sent. `reference_id` is deliberately omitted too: Razorpay treats it as
an idempotency key, and this demo resets its ledger repeatedly and needs a
fresh link each time, not the same one replayed back.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import requests

_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
USING_REAL_RAZORPAY = bool(_KEY_ID and _KEY_SECRET)

_API_URL = "https://api.razorpay.com/v1/payment_links"


class PaymentLinkError(Exception):
    """Raised on any failure to create a real link. Callers must not log a
    ledger action unless create_payment_link returns successfully -- a
    failed API call is not a completed action."""


def _auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{_KEY_ID}:{_KEY_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def create_payment_link(invoice_id: str, amount: float, currency: str) -> dict[str, Any]:
    payload = {
        "amount": int(round(amount * 100)),  # Razorpay wants the smallest currency unit (paise)
        "currency": currency,
        "description": f"ClearDue collections -- {invoice_id}",
    }
    try:
        resp = requests.post(_API_URL, json=payload, headers=_auth_header(), timeout=15)
    except requests.RequestException as e:
        raise PaymentLinkError(f"could not reach Razorpay: {e}") from e

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("description", resp.text)
        except ValueError:
            detail = resp.text
        raise PaymentLinkError(f"Razorpay rejected the request ({resp.status_code}): {detail}")

    body = resp.json()
    return {"id": body["id"], "short_url": body["short_url"]}

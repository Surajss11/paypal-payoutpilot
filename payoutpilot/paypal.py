"""Minimal PayPal REST client (OAuth2 client credentials + Payouts), plus an offline mock with the same interface."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from .models import PayoutPlan

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


class PayPalError(RuntimeError):
    def __init__(self, status: int, body: dict | str, debug_id: str | None = None):
        self.status, self.body, self.debug_id = status, body, debug_id
        name = body.get("name") if isinstance(body, dict) else ""
        msg = body.get("message") if isinstance(body, dict) else str(body)[:200]
        super().__init__(f"PayPal {status} {name}: {msg} (debug_id={debug_id})")


def batch_id_for(plan: PayoutPlan) -> str:
    """Deterministic sender_batch_id: the same approved plan can never be paid twice."""
    raw = plan.campaign_id + "|" + "|".join(f"{ln.submission_id}:{ln.paypal_email}:{ln.amount}" for ln in plan.lines)
    return f"pp-{plan.campaign_id[:20]}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def payout_body(plan: PayoutPlan, subject: str, message: str) -> dict:
    return {
        "sender_batch_header": {
            "sender_batch_id": batch_id_for(plan),
            "email_subject": subject,
            "email_message": message,
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": ln.paypal_email,
                "amount": {"value": f"{ln.amount:.2f}", "currency": plan.currency},
                "note": f"{ln.tier} (rank {ln.rank})",
                "sender_item_id": ln.submission_id[:63],
            }
            for ln in plan.lines
        ],
    }


@dataclass
class PayPalClient:
    client_id: str
    client_secret: str
    base_url: str = SANDBOX
    timeout: float = 30.0
    _token: str | None = None
    _token_exp: float = 0.0
    transport: httpx.BaseTransport | None = None

    @classmethod
    def from_env(cls) -> "PayPalClient":
        cid, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
        if not cid or not secret:
            raise RuntimeError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set (sandbox app credentials)")
        env = os.environ.get("PAYPAL_ENVIRONMENT", "sandbox").lower()
        if env != "sandbox" and os.environ.get("PAYOUTPILOT_ALLOW_LIVE") != "1":
            raise RuntimeError("live PayPal is disabled; set PAYOUTPILOT_ALLOW_LIVE=1 deliberately to use it")
        return cls(cid, secret, SANDBOX if env == "sandbox" else LIVE)

    def _http(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport)

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        with self._http() as c:
            r = c.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                       auth=(self.client_id, self.client_secret), headers={"Accept": "application/json"})
        self._raise(r)
        data = r.json()
        self._token, self._token_exp = data["access_token"], time.time() + float(data.get("expires_in", 300))
        return self._token

    def _request(self, method: str, path: str, *, json: dict | None = None, request_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        with self._http() as c:
            r = c.request(method, path, json=json, headers=headers)
        self._raise(r)
        return r.json() if r.content else {}

    @staticmethod
    def _raise(r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            raise PayPalError(r.status_code, body, r.headers.get("PayPal-Debug-Id"))

    def create_payout(self, plan: PayoutPlan, subject: str, message: str) -> dict:
        body = payout_body(plan, subject, message)
        return self._request("POST", "/v1/payments/payouts", json=body,
                             request_id=body["sender_batch_header"]["sender_batch_id"])

    def get_payout(self, payout_batch_id: str) -> dict:
        return self._request("GET", f"/v1/payments/payouts/{payout_batch_id}?fields=batch_header,items")


@dataclass
class MockPayPal:
    """Offline stand-in used by tests and the no-credentials demo. Mirrors PayPal's response shapes."""

    batches: dict[str, dict] = field(default_factory=dict)
    by_sender_id: dict[str, str] = field(default_factory=dict)

    def create_payout(self, plan: PayoutPlan, subject: str, message: str) -> dict:
        body = payout_body(plan, subject, message)
        sid = body["sender_batch_header"]["sender_batch_id"]
        if sid in self.by_sender_id:  # same as PayPal-Request-Id replay: return the original batch
            return {"batch_header": self.batches[self.by_sender_id[sid]]["batch_header"]}
        pid = uuid.uuid4().hex[:13].upper()
        total = sum((Decimal(i["amount"]["value"]) for i in body["items"]), Decimal(0))
        items = []
        for it in body["items"]:
            status = "UNCLAIMED" if it["receiver"].endswith("@unclaimed.test") else "SUCCESS"
            items.append({"payout_item_id": uuid.uuid4().hex[:13].upper(), "transaction_status": status,
                          "payout_batch_id": pid,
                          "payout_item": {k: it[k] for k in ("receiver", "amount", "note", "sender_item_id")}})
        self.batches[pid] = {
            "batch_header": {"payout_batch_id": pid, "batch_status": "SUCCESS",
                             "sender_batch_header": body["sender_batch_header"],
                             "amount": {"value": f"{total:.2f}", "currency": plan.currency}},
            "items": items,
        }
        self.by_sender_id[sid] = pid
        return {"batch_header": {**self.batches[pid]["batch_header"], "batch_status": "PENDING"}}

    def get_payout(self, payout_batch_id: str) -> dict:
        if payout_batch_id not in self.batches:
            raise PayPalError(404, {"name": "INVALID_RESOURCE_ID", "message": "not found"})
        return self.batches[payout_batch_id]


def client_from_env() -> PayPalClient | MockPayPal:
    return MockPayPal() if os.environ.get("PAYPAL_MODE", "").lower() == "mock" else PayPalClient.from_env()

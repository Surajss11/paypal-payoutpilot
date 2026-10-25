"""PayPal REST client: OAuth2 client credentials, Payouts (batch create, status polling, item lookup, cancel
unclaimed) and Orders v2 (sponsor funds the prize pool). Plus an offline mock with the same interface and shapes."""

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

# Batch statuses after which the batch itself will not change any more.
BATCH_TERMINAL = {"SUCCESS", "DENIED", "CANCELED"}
# Item statuses that will not settle on their own (UNCLAIMED waits for the receiver up to 30 days).
ITEM_SETTLED = {"SUCCESS", "FAILED", "RETURNED", "REFUNDED", "BLOCKED", "REVERSED", "UNCLAIMED", "DENIED"}
# RETURNED (unclaimed for 30 days or cancelled by us) is deliberately not retried: same address, same outcome.
ITEM_RETRYABLE = {"FAILED", "BLOCKED", "DENIED"}


HINTS = {
    "PAYOUT_NOT_AVAILABLE": "the app's sandbox business account is in a country that cannot send Payouts (e.g. IN). "
                            "Create a US sandbox business account (developer.paypal.com > Testing Tools > Sandbox "
                            "Accounts) and a sandbox app linked to it, then use that app's credentials.",
    "INSUFFICIENT_FUNDS": "top up the sandbox business account balance in the developer dashboard.",
    "AUTHORIZATION_ERROR": "enable Payouts for this app (Apps & Credentials > app > Features).",
    "invalid_client": "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are wrong or belong to the other environment.",
}


class PayPalError(RuntimeError):
    def __init__(self, status: int, body: dict | str, debug_id: str | None = None):
        self.status, self.body, self.debug_id = status, body, debug_id
        name = body.get("name") or body.get("error", "") if isinstance(body, dict) else ""
        msg = (body.get("message") or body.get("error_description", "")) if isinstance(body, dict) else str(body)[:200]
        hint = HINTS.get(name, "")
        super().__init__(f"PayPal {status} {name}: {msg} (debug_id={debug_id})" + (f" hint: {hint}" if hint else ""))


def batch_id_for(plan: PayoutPlan, attempt: int = 0) -> str:
    """Deterministic sender_batch_id: the same approved plan can never be paid twice (PayPal rejects reuse
    for 30 days and PayPal-Request-Id replays return the original batch)."""
    raw = plan.campaign_id + "|" + "|".join(f"{ln.submission_id}:{ln.paypal_email}:{ln.amount}" for ln in plan.lines)
    suffix = f"-r{attempt}" if attempt else ""
    return f"pp-{plan.campaign_id[:20]}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}{suffix}"


def payout_body(plan: PayoutPlan, subject: str, message: str, attempt: int = 0) -> dict:
    return {
        "sender_batch_header": {
            "sender_batch_id": batch_id_for(plan, attempt),
            "email_subject": subject[:255],
            "email_message": message[:1000],
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": ln.paypal_email,
                "amount": {"value": f"{ln.amount:.2f}", "currency": plan.currency},
                "note": (ln.note or f"{ln.tier} (rank {ln.rank})")[:4000],
                "sender_item_id": ln.submission_id[:63],
            }
            for ln in plan.lines
        ],
    }


def order_body(reference_id: str, amount: Decimal, currency: str, description: str) -> dict:
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": reference_id[:256],
            "description": description[:127],
            "amount": {"currency_code": currency, "value": f"{Decimal(amount):.2f}"},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": "PayoutPilot", "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
            "return_url": "https://example.com/payoutpilot/funded", "cancel_url": "https://example.com/payoutpilot/cancel",
        }}},
    }


def approve_link(order: dict) -> str | None:
    for ln in order.get("links", []):
        if ln.get("rel") in ("payer-action", "approve"):
            return ln.get("href")
    return None


class _Polling:
    """Shared status polling for the real client and the mock."""

    def get_payout(self, payout_batch_id: str) -> dict:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def wait_for_payout(self, payout_batch_id: str, timeout: float = 90.0, interval: float = 3.0,
                        sleep=time.sleep) -> dict:
        """Poll GET /v1/payments/payouts/{id} until the batch and every item has settled, or timeout."""
        deadline = time.monotonic() + timeout
        while True:
            st = self.get_payout(payout_batch_id)
            batch = st.get("batch_header", {}).get("batch_status")
            items = [i.get("transaction_status") for i in st.get("items", [])]
            if batch in BATCH_TERMINAL and all(s in ITEM_SETTLED for s in items):
                return st
            if time.monotonic() >= deadline:
                return st
            sleep(interval)


@dataclass
class PayPalClient(_Polling):
    client_id: str
    client_secret: str
    base_url: str = SANDBOX
    timeout: float = 30.0
    _token: str | None = field(default=None, repr=False)
    _token_exp: float = 0.0
    transport: httpx.BaseTransport | None = None

    def __repr__(self) -> str:  # never leak credentials through logs / tracebacks
        return f"PayPalClient(base_url={self.base_url!r})"

    @classmethod
    def from_env(cls) -> "PayPalClient":
        cid, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
        if not cid or not secret:
            raise RuntimeError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set (sandbox app credentials)")
        env = (os.environ.get("PAYPAL_ENVIRONMENT") or os.environ.get("PAYPAL_ENV") or "sandbox").lower()
        if env != "sandbox" and os.environ.get("PAYOUTPILOT_ALLOW_LIVE") != "1":
            raise RuntimeError("live PayPal is disabled; set PAYOUTPILOT_ALLOW_LIVE=1 deliberately to use it")
        return cls(cid, secret, SANDBOX if env == "sandbox" else LIVE)

    @property
    def mode(self) -> str:
        return "sandbox" if self.base_url == SANDBOX else "live"

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

    def _request(self, method: str, path: str, *, json: dict | None = None, request_id: str | None = None,
                 retries: int = 2) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
                   "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        for attempt in range(retries + 1):
            with self._http() as c:
                r = c.request(method, path, json=json, headers=headers)
            if r.status_code == 401 and attempt == 0:  # token revoked / expired early: refresh once
                self._token = None
                headers["Authorization"] = f"Bearer {self.token()}"
                continue
            # 429/5xx are safe to retry because every write carries a PayPal-Request-Id
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries and (request_id or method == "GET"):
                time.sleep(1.5 * (attempt + 1))
                continue
            break
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

    # Payouts
    def create_payout(self, plan: PayoutPlan, subject: str, message: str, attempt: int = 0) -> dict:
        body = payout_body(plan, subject, message, attempt)
        return self._request("POST", "/v1/payments/payouts", json=body,
                             request_id=body["sender_batch_header"]["sender_batch_id"])

    def get_payout(self, payout_batch_id: str) -> dict:
        return self._request("GET", f"/v1/payments/payouts/{payout_batch_id}?fields=batch_header,items&total_required=true")

    def get_payout_item(self, payout_item_id: str) -> dict:
        return self._request("GET", f"/v1/payments/payouts-item/{payout_item_id}")

    def cancel_unclaimed(self, payout_item_id: str) -> dict:
        """Return the money of an UNCLAIMED item to the sender."""
        return self._request("POST", f"/v1/payments/payouts-item/{payout_item_id}/cancel")

    # Orders v2: the sponsor funds the prize pool
    def create_order(self, reference_id: str, amount: Decimal, currency: str, description: str) -> dict:
        return self._request("POST", "/v2/checkout/orders", json=order_body(reference_id, amount, currency, description),
                             request_id=f"fund-{reference_id}-{Decimal(amount):.2f}"[:108])

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str) -> dict:
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json={},
                             request_id=f"capture-{order_id}")


@dataclass
class MockPayPal(_Polling):
    """Offline stand-in used by tests and the no-credentials demo. Mirrors PayPal's response shapes.
    Receivers ending in @unclaimed.test go UNCLAIMED, @fail.test go FAILED; everything else SUCCESS."""

    batches: dict[str, dict] = field(default_factory=dict)
    by_sender_id: dict[str, str] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)
    mode: str = "mock"

    def create_payout(self, plan: PayoutPlan, subject: str, message: str, attempt: int = 0) -> dict:
        body = payout_body(plan, subject, message, attempt)
        sid = body["sender_batch_header"]["sender_batch_id"]
        if sid in self.by_sender_id:  # same as PayPal-Request-Id replay: return the original batch
            return {"batch_header": self.batches[self.by_sender_id[sid]]["batch_header"]}
        pid = uuid.uuid4().hex[:13].upper()
        total = sum((Decimal(i["amount"]["value"]) for i in body["items"]), Decimal(0))
        items = []
        for it in body["items"]:
            r = it["receiver"]
            status = "UNCLAIMED" if r.endswith("@unclaimed.test") else "FAILED" if r.endswith("@fail.test") else "SUCCESS"
            items.append({"payout_item_id": uuid.uuid4().hex[:13].upper(), "transaction_status": status,
                          "payout_batch_id": pid, "payout_item_fee": {"value": "0.00", "currency": plan.currency},
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

    def _item(self, payout_item_id: str) -> dict:
        for b in self.batches.values():
            for it in b["items"]:
                if it["payout_item_id"] == payout_item_id:
                    return it
        raise PayPalError(404, {"name": "INVALID_RESOURCE_ID", "message": "item not found"})

    def get_payout_item(self, payout_item_id: str) -> dict:
        return self._item(payout_item_id)

    def cancel_unclaimed(self, payout_item_id: str) -> dict:
        it = self._item(payout_item_id)
        if it["transaction_status"] != "UNCLAIMED":
            raise PayPalError(422, {"name": "ITEM_CANCELLATION_FAILED", "message": "only UNCLAIMED items can be cancelled"})
        it["transaction_status"] = "RETURNED"
        return it

    def create_order(self, reference_id: str, amount: Decimal, currency: str, description: str) -> dict:
        oid = uuid.uuid4().hex[:17].upper()
        self.orders[oid] = {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                            "purchase_units": order_body(reference_id, amount, currency, description)["purchase_units"],
                            "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}]}
        return self.orders[oid]

    def get_order(self, order_id: str) -> dict:
        if order_id not in self.orders:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND", "message": "order not found"})
        return self.orders[order_id]

    def capture_order(self, order_id: str) -> dict:
        o = self.get_order(order_id)
        o["status"] = "COMPLETED"
        return o


def client_from_env() -> PayPalClient | MockPayPal:
    return MockPayPal() if os.environ.get("PAYPAL_MODE", "").lower() == "mock" else PayPalClient.from_env()

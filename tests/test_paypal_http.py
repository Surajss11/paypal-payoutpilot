"""PayPal REST client against mocked HTTP (respx): no network, exact request shapes."""

from decimal import Decimal

import httpx
import pytest
import respx

from payoutpilot import agent, pipeline
from payoutpilot.config import load_env_files, mask_email
from payoutpilot.paypal import SANDBOX, PayPalClient, PayPalError


def token_route():
    return respx.post(f"{SANDBOX}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 32400}))


@respx.mock
def test_wait_for_payout_polls_until_settled():
    token_route()
    states = [("PENDING", "PENDING"), ("PROCESSING", "PENDING"), ("SUCCESS", "SUCCESS")]
    responses = [httpx.Response(200, json={"batch_header": {"payout_batch_id": "B1", "batch_status": b},
                                           "items": [{"payout_item_id": "I1", "transaction_status": i,
                                                      "payout_item": {"receiver": "a@x.com", "sender_item_id": "s1",
                                                                      "amount": {"value": "1.00", "currency": "USD"}}}]})
                 for b, i in states]
    route = respx.get(url__startswith=f"{SANDBOX}/v1/payments/payouts/B1").mock(side_effect=responses)
    sleeps = []
    out = PayPalClient("id", "secret").wait_for_payout("B1", timeout=60, interval=2, sleep=sleeps.append)
    assert out["batch_header"]["batch_status"] == "SUCCESS" and route.call_count == 3 and sleeps == [2, 2]


@respx.mock
def test_cancel_unclaimed_and_item_lookup():
    token_route()
    respx.get(f"{SANDBOX}/v1/payments/payouts-item/I9").mock(
        return_value=httpx.Response(200, json={"payout_item_id": "I9", "transaction_status": "UNCLAIMED"}))
    cancel = respx.post(f"{SANDBOX}/v1/payments/payouts-item/I9/cancel").mock(
        return_value=httpx.Response(200, json={"payout_item_id": "I9", "transaction_status": "RETURNED"}))
    c = PayPalClient("id", "secret")
    assert c.get_payout_item("I9")["transaction_status"] == "UNCLAIMED"
    assert c.cancel_unclaimed("I9")["transaction_status"] == "RETURNED" and cancel.called


@respx.mock
def test_orders_v2_create_and_capture():
    token_route()
    create = respx.post(f"{SANDBOX}/v2/checkout/orders").mock(return_value=httpx.Response(200, json={
        "id": "O1", "status": "PAYER_ACTION_REQUIRED",
        "links": [{"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=O1"}]}))
    respx.post(f"{SANDBOX}/v2/checkout/orders/O1/capture").mock(
        return_value=httpx.Response(201, json={"id": "O1", "status": "COMPLETED"}))
    st = agent.plan_request("Pay a@x.com $5 and b@x.com $2.50")
    c = PayPalClient("id", "secret")
    o = pipeline.create_funding_order(st, c)
    body = httpx.Response(200, content=create.calls.last.request.content).json()
    assert body["intent"] == "CAPTURE" and body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "7.50"}
    assert create.calls.last.request.headers["PayPal-Request-Id"].startswith("fund-")
    assert o["approve_url"].endswith("token=O1")
    assert pipeline.refresh_funding_order(st, c, capture=True)["status"] == "COMPLETED"


@respx.mock
def test_payout_not_available_has_hint_and_is_logged():
    token_route()
    respx.post(f"{SANDBOX}/v1/payments/payouts").mock(return_value=httpx.Response(
        403, json={"name": "PAYOUT_NOT_AVAILABLE", "message": "You live in a country that is not allowed"},
        headers={"PayPal-Debug-Id": "d1"}))
    st = agent.plan_request("Pay a@x.com $5")
    with pytest.raises(PayPalError) as e:
        pipeline.approve_and_pay(st, PayPalClient("id", "secret"), Decimal("5.00"))
    assert "US sandbox business account" in str(e.value) and e.value.debug_id == "d1"
    assert st.payout_batch_id is None and st.events[-1].kind == "payout_error"


@respx.mock
def test_retries_5xx_with_same_request_id_and_refreshes_401():
    tok = token_route()
    route = respx.post(f"{SANDBOX}/v1/payments/payouts").mock(side_effect=[
        httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}),
        httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
        httpx.Response(201, json={"batch_header": {"payout_batch_id": "B2", "batch_status": "PENDING"}})])
    st = agent.plan_request("Pay a@x.com $5")
    res = PayPalClient("id", "secret", retry_backoff=0).create_payout(st.plan, "s", "m")
    ids = {c.request.headers["PayPal-Request-Id"] for c in route.calls}
    assert res["batch_header"]["payout_batch_id"] == "B2" and len(ids) == 1 and tok.call_count == 2


def test_client_repr_and_env_loader_never_leak(tmp_path, monkeypatch):
    assert "secret-value" not in repr(PayPalClient("id", "secret-value"))
    (tmp_path / ".env.paypal").write_text("PAYPAL_CLIENT_ID=fromfile\nPAYPAL_ENV=sandbox\n# c\n")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "fromenv")
    monkeypatch.delenv("PAYOUTPILOT_NO_DOTENV", raising=False)
    monkeypatch.delenv("PAYPAL_ENV", raising=False)
    load_env_files(root=tmp_path)
    import os
    assert os.environ["PAYPAL_CLIENT_ID"] == "fromenv" and os.environ["PAYPAL_ENV"] == "sandbox"
    assert mask_email("winner@example.com") == "wi***r@example.com"


@respx.mock
def test_cancel_unclaimed_retries_5xx_then_succeeds():
    token_route()
    cancel = respx.post(f"{SANDBOX}/v1/payments/payouts-item/I7/cancel").mock(side_effect=[
        httpx.Response(500, json={"name": "INTERNAL_SERVICE_ERROR"}),
        httpx.Response(200, json={"payout_item_id": "I7", "transaction_status": "RETURNED"})])
    c = PayPalClient("id", "secret", retry_backoff=0)
    assert c.cancel_unclaimed("I7")["transaction_status"] == "RETURNED" and cancel.call_count == 2


@respx.mock
def test_retries_are_bounded_and_unsafe_posts_are_not_retried():
    token_route()
    always = respx.post(f"{SANDBOX}/v1/payments/payouts-item/I8/cancel").mock(
        return_value=httpx.Response(500, json={"name": "INTERNAL_SERVICE_ERROR"}))
    c = PayPalClient("id", "secret", retry_backoff=0)
    with pytest.raises(PayPalError):
        c.cancel_unclaimed("I8")
    assert always.call_count == 4  # 1 try + 3 bounded retries
    plain = respx.post(f"{SANDBOX}/v1/unsafe").mock(return_value=httpx.Response(503, json={"name": "X"}))
    with pytest.raises(PayPalError):
        c._request("POST", "/v1/unsafe", json={})
    assert plain.call_count == 1  # no request id, not idempotent: never repeated


@respx.mock
def test_pipeline_cancel_trusts_item_status_after_persistent_5xx():
    token_route()
    respx.post(f"{SANDBOX}/v1/payments/payouts-item/I5/cancel").mock(
        return_value=httpx.Response(500, json={"name": "INTERNAL_SERVICE_ERROR"}))
    respx.get(f"{SANDBOX}/v1/payments/payouts-item/I5").mock(
        return_value=httpx.Response(200, json={"payout_item_id": "I5", "transaction_status": "RETURNED"}))
    st = agent.plan_request("Pay a@x.com $5")
    st.payout_batch_id = "B5"
    st.payout_status = {"items": [{"payout_item_id": "I5", "transaction_status": "UNCLAIMED",
                                   "payout_item": {"receiver": "a@x.com", "sender_item_id": "x"}}]}
    out = pipeline.cancel_unclaimed(st, PayPalClient("id", "secret", retry_backoff=0))
    assert out[0]["transaction_status"] == "RETURNED" and st.events[-1].kind == "cancel_unclaimed"

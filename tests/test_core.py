from decimal import Decimal

import httpx
import pytest
import respx

from payoutpilot import integrity, pipeline
from payoutpilot.allocator import allocate
from payoutpilot.judge import score, weighted_total
from payoutpilot.llm import ChatClient, MockChat, extract_json
from payoutpilot.models import Score
from payoutpilot.paypal import SANDBOX, MockPayPal, PayPalClient, PayPalError, batch_id_for
from payoutpilot.store import CampaignState


def test_integrity_flags(campaign, subs):
    flags = integrity.check(campaign, subs)
    codes = {sid: {f.code for f in fl} for sid, fl in flags.items()}
    assert "duplicate_payee" in codes["s004"]
    assert "wallet_bait" in codes["s006"]
    assert "near_duplicate" in codes["s003"]
    assert "s001" not in codes and "s007" not in codes
    assert integrity.blocked(flags) == {"s003", "s004", "s006"}
    assert integrity.blocked(integrity.check(campaign, subs, block_near_dups=False)) == {"s004", "s006"}


def test_normalize_link():
    assert integrity.normalize_link("https://www.twitter.com/a/status/1/") == integrity.normalize_link("x.com/a/status/1")


def test_extract_json_handles_fences_and_chatter():
    assert extract_json('Sure!\n```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_judge_clamps_and_weights(campaign, subs):
    class Fake:
        def complete(self, system, user):
            if "reviewer" in system.lower():
                return '{"agrees": false, "notes": ["accuracy too high"]}'
            return '{"scores": {"Originality": 14, "clarity": "8", "accuracy": -2, "reach": "x"}, "reasons": ["r"]}'

    sc = score(campaign, subs[0], Fake())
    assert sc.per_criterion == {"originality": 10.0, "clarity": 8.0, "accuracy": 0.0, "reach": 0.0}
    assert sc.total == weighted_total(campaign, sc.per_criterion) == round((30 + 16) / 9 * 10, 1)
    assert sc.reviewer_agrees is False


def test_allocator_tiers_budget_and_min_score(campaign, subs):
    scores = [Score(submission_id=s.id, total=t) for s, t in zip(subs, [90, 80, 85, 99, 10, 99, 88, 50])]
    plan = allocate(campaign, subs, scores, excluded={"s004", "s006"})
    assert [ln.submission_id for ln in plan.lines] == ["s001", "s007", "s003", "s002", "s008"]
    assert [str(ln.amount) for ln in plan.lines] == ["150.00", "100.00", "50.00", "10.00", "10.00"]
    assert "s005" in plan.skipped and plan.total == Decimal("320.00")
    campaign.budget = Decimal("100")
    with pytest.raises(ValueError):
        allocate(campaign, subs, scores, excluded=set())


def test_end_to_end_mock_is_idempotent(campaign, subs):
    st = CampaignState(campaign=campaign, submissions=subs)
    pipeline.run_checks(st)
    pipeline.run_judge(st, MockChat())
    pipeline.make_plan(st)
    pp = MockPayPal()
    with pytest.raises(ValueError):
        pipeline.approve_and_pay(st, pp, Decimal("1.00"))
    first = pipeline.approve_and_pay(st, pp, st.plan.total)
    again = pipeline.approve_and_pay(st, pp, st.plan.total)
    assert first["batch_header"]["payout_batch_id"] == again["batch_header"]["payout_batch_id"]
    assert len(pp.batches) == 1
    status = pipeline.refresh_status(st, pp)
    assert {i["payout_item"]["receiver"] for i in status["items"]} == {ln.paypal_email for ln in st.plan.lines}
    assert "s004" not in {ln.submission_id for ln in st.plan.lines}


@respx.mock
def test_paypal_client_oauth_and_payout(campaign, subs):
    st = CampaignState(campaign=campaign, submissions=subs)
    pipeline.run_checks(st)
    pipeline.run_judge(st, MockChat(), review=False)
    pipeline.make_plan(st)
    tok = respx.post(f"{SANDBOX}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 32400}))
    pay = respx.post(f"{SANDBOX}/v1/payments/payouts").mock(
        return_value=httpx.Response(201, json={"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}}))
    c = PayPalClient("id", "secret")
    res = c.create_payout(st.plan, "s", "m")
    assert res["batch_header"]["payout_batch_id"] == "B1"
    req = pay.calls.last.request
    assert req.headers["Authorization"] == "Bearer T"
    assert req.headers["PayPal-Request-Id"] == batch_id_for(st.plan)
    body = httpx.Response(200, content=req.content).json()
    assert body["items"][0]["amount"] == {"value": "150.00", "currency": "USD"}
    c.create_payout(st.plan, "s", "m")
    assert tok.call_count == 1  # token cached


@respx.mock
def test_paypal_error_surfaces_debug_id():
    respx.post(f"{SANDBOX}/v1/oauth2/token").mock(return_value=httpx.Response(
        401, json={"error": "invalid_client", "message": "Client Authentication failed"}, headers={"PayPal-Debug-Id": "dbg1"}))
    with pytest.raises(PayPalError) as e:
        PayPalClient("bad", "bad").token()
    assert e.value.debug_id == "dbg1"


def test_live_mode_is_locked(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "a")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "b")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "live")
    monkeypatch.delenv("PAYOUTPILOT_ALLOW_LIVE", raising=False)
    with pytest.raises(RuntimeError):
        PayPalClient.from_env()


@respx.mock
def test_chat_client_openai_compatible():
    route = respx.post("https://example.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{\"ok\": 1}"}}]}))
    out = ChatClient("https://example.test/v1", "m", "k").complete("sys", "user")
    assert out == '{"ok": 1}' and route.calls.last.request.headers["Authorization"] == "Bearer k"

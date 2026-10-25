import json

from fastapi.testclient import TestClient

from payoutpilot.api import create_app
from payoutpilot.llm import MockChat
from payoutpilot.paypal import MockPayPal
from payoutpilot.store import Store

from .conftest import EX


def test_api_flow(tmp_path):
    app = create_app(Store(tmp_path), llm=MockChat(), paypal=MockPayPal())
    c = TestClient(app)
    camp = json.loads((EX / "campaign.json").read_text())
    assert c.post("/api/campaigns", json=camp).status_code == 200
    cid = camp["id"]
    with open(EX / "submissions.csv", "rb") as f:
        r = c.post(f"/api/campaigns/{cid}/submissions.csv", files={"file": ("s.csv", f, "text/csv")})
    assert r.status_code == 200 and len(r.json()["submissions"]) == 8
    st = c.post(f"/api/campaigns/{cid}/judge").json()
    assert st["plan"]["lines"] and "s006" in st["excluded"]
    first = st["plan"]["lines"][0]["submission_id"]
    st = c.post(f"/api/campaigns/{cid}/exclude", json=[first]).json()
    assert first not in {ln["submission_id"] for ln in st["plan"]["lines"]}
    assert c.post(f"/api/campaigns/{cid}/approve", json={"confirm_total": "1"}).status_code == 400
    st = c.post(f"/api/campaigns/{cid}/approve", json={"confirm_total": st["plan_total"]}).json()
    assert st["payout_batch_id"]
    # once paid, nothing may change the plan (that would let a second approval pay again)
    assert c.post(f"/api/campaigns/{cid}/exclude", json=[]).status_code == 400
    assert c.post(f"/api/campaigns/{cid}/judge").status_code == 400
    st = c.post(f"/api/campaigns/{cid}/refresh").json()
    assert all(i["transaction_status"] in ("SUCCESS", "UNCLAIMED") for i in st["payout_status"]["items"])
    assert c.get("/").status_code == 200
    assert c.get("/api/campaigns/nope").status_code == 404


def test_api_request_flow(tmp_path):
    c = TestClient(create_app(Store(tmp_path), llm=MockChat(), paypal=MockPayPal()))
    st = c.post("/api/requests", json={"text": "Pay ok@x.com $5 and lost@unclaimed.test $3 for memes"}).json()
    cid = st["campaign"]["id"]
    assert st["kind"] == "request" and st["plan_total"] == "8.00"
    assert c.post(f"/api/campaigns/{cid}/approve", json={"confirm_total": "8"}).status_code == 200
    st = c.post(f"/api/campaigns/{cid}/refresh?wait=true").json()
    assert {i["transaction_status"] for i in st["payout_status"]["items"]} == {"SUCCESS", "UNCLAIMED"}
    st = c.post(f"/api/campaigns/{cid}/cancel-unclaimed").json()
    assert "RETURNED" in {i["transaction_status"] for i in st["payout_status"]["items"]}
    st = c.post(f"/api/campaigns/{cid}/funding-order").json()
    assert st["funding_order"]["status"] == "PAYER_ACTION_REQUIRED"
    assert c.post("/api/requests", json={"text": " "}).status_code == 400
    assert c.get("/api/health").json()["paypal"] == "mock"

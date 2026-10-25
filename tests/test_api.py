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
    st = c.post(f"/api/campaigns/{cid}/refresh").json()
    assert all(i["transaction_status"] in ("SUCCESS", "UNCLAIMED") for i in st["payout_status"]["items"])
    assert c.get("/").status_code == 200
    assert c.get("/api/campaigns/nope").status_code == 404

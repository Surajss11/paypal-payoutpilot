"""Run the real PayPal sandbox flow end to end and write redacted evidence to evidence/.

    python scripts/sandbox_evidence.py            # needs PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (sandbox)
                                                  # and PAYPAL_SANDBOX_PERSONAL (a sandbox personal account email)

Re-running is safe: if this request was already paid, the saved run is resumed (status + cancel) instead of
creating another batch. Set EVIDENCE_FRESH=1 to force a new run.

Steps: OAuth2 token -> Orders v2 funding order for the prize pool -> natural-language payout request ->
reviewed plan -> approval (scripted, sandbox only) -> Payouts batch -> status polling -> cancel UNCLAIMED.
No secret or token is ever written; receiver emails are masked.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from payoutpilot import agent, pipeline  # noqa: E402
from payoutpilot.config import load_env_files, mask_email  # noqa: E402
from payoutpilot.llm import client_from_env as llm_from_env  # noqa: E402
from payoutpilot.paypal import PayPalClient, PayPalError  # noqa: E402
from payoutpilot.store import Store  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def redact_items(status: dict) -> list[dict]:
    out = []
    for it in status.get("items", []):
        pi = it.get("payout_item", {})
        out.append({"payout_item_id": it.get("payout_item_id"), "transaction_status": it.get("transaction_status"),
                    "receiver": mask_email(pi.get("receiver", "")), "amount": pi.get("amount"),
                    "fee": it.get("payout_item_fee"), "error": (it.get("errors") or {}).get("name")})
    return out


def main() -> int:
    load_env_files()
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    winner = os.environ.get("PAYPAL_SANDBOX_PERSONAL", "")
    if not winner:
        print("set PAYPAL_SANDBOX_PERSONAL to a sandbox personal account email")
        return 1
    pp = PayPalClient.from_env()
    assert pp.mode == "sandbox", "evidence script is sandbox-only"
    ev: dict = {"started": now(), "environment": pp.base_url, "steps": []}

    pp.token()
    ev["steps"].append({"step": "oauth2_client_credentials", "ok": True, "at": now(),
                        "token_ttl_s": int(pp._token_exp - datetime.now().timestamp())})

    text = (f"Pay {winner} $5.00 for winning the Rebate thread bounty and "
            f"new.creator.payoutpilot@example.com $2.50 as runner-up")
    try:
        llm = llm_from_env()
    except RuntimeError:
        llm = None
    store = Store()
    st = agent.plan_request(text, llm, title="Rebate thread bounty (sandbox evidence run)")
    resumed = False
    try:
        prev = store.get(st.campaign.id)
        if prev.payout_batch_id and not os.environ.get("EVIDENCE_FRESH"):
            st, resumed = prev, True  # never create a second batch for the same request: resume tracking instead
    except KeyError:
        pass
    ev["resumed_existing_batch"] = resumed
    ev["steps"].append({"step": "plan_request", "planner": st.planner, "at": now(),
                        "request": text.replace(winner, mask_email(winner)),
                        "lines": [{"receiver": mask_email(l.paypal_email), "amount": str(l.amount), "note": l.note}
                                  for l in st.plan.lines], "total": str(st.plan.total), "warnings": st.warnings})

    def step(name: str, fn) -> bool:
        try:
            rec = fn() or {}
            ev["steps"].append({"step": name, "ok": True, "at": now(), **rec})
            return True
        except PayPalError as e:
            ev["steps"].append({"step": name, "ok": False, "at": now(), "http_status": e.status,
                                "error": e.body.get("name") if isinstance(e.body, dict) else str(e.body)[:100],
                                "debug_id": e.debug_id, "detail": str(e)})
            return False

    def funding():
        o = st.funding_order or pipeline.create_funding_order(st, pp)
        o2 = pipeline.refresh_funding_order(st, pp)
        return {"order_id": o["id"], "status": o2["status"], "amount": o["amount"], "currency": o["currency"],
                "reused": bool(resumed), "approve_url_host": (o.get("approve_url") or "/ / ").split("/")[2]}

    def create():
        before = st.payout_batch_id
        res = pipeline.approve_and_pay(st, pp, st.plan.total, approved_by="sandbox evidence script")
        hdr = res["batch_header"]
        return {"payout_batch_id": hdr["payout_batch_id"], "reused_existing": before is not None,
                "sender_batch_id": hdr.get("sender_batch_header", {}).get("sender_batch_id"),
                "batch_status": hdr.get("batch_status")}

    def poll():
        status = pipeline.refresh_status(st, pp, wait=True, timeout=120)
        hdr = status.get("batch_header", {})
        return {"payout_batch_id": st.payout_batch_id, "batch_status": hdr.get("batch_status"),
                "time_created": hdr.get("time_created"), "time_completed": hdr.get("time_completed"),
                "amount": hdr.get("amount"), "fees": hdr.get("fees"), "items": redact_items(status)}

    def cancel():
        done = pipeline.cancel_unclaimed(st, pp)
        status = pipeline.refresh_status(st, pp)
        return {"cancelled_items": [d.get("payout_item_id") for d in done], "items": redact_items(status)}

    step("orders_v2_funding_order", funding)
    if step("payouts_create_batch", create) and step("payouts_poll_status", poll):
        if pipeline.items_with(st, {"UNCLAIMED"}):
            step("payouts_cancel_unclaimed", cancel)

    store.put(st)
    ev["finished"] = now()
    ev["campaign_state"] = f"data/{st.campaign.id}.json"
    out = json.dumps(ev, indent=2)
    assert not secret or secret not in out, "refusing to write evidence containing the secret"
    Path("evidence").mkdir(exist_ok=True)
    Path("evidence/sandbox-run.json").write_text(out + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

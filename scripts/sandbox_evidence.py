"""Run the real PayPal sandbox flow end to end and write redacted evidence to evidence/.

    python scripts/sandbox_evidence.py            # needs PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (sandbox)
                                                  # and PAYPAL_SANDBOX_PERSONAL (a sandbox personal account email)

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
    st = agent.plan_request(text, llm, title="Rebate thread bounty (sandbox evidence run)")
    ev["steps"].append({"step": "plan_request", "planner": st.planner, "at": now(),
                        "request": text.replace(winner, mask_email(winner)),
                        "lines": [{"receiver": mask_email(l.paypal_email), "amount": str(l.amount), "note": l.note}
                                  for l in st.plan.lines], "total": str(st.plan.total), "warnings": st.warnings})

    try:
        o = pipeline.create_funding_order(st, pp)
        o2 = pipeline.refresh_funding_order(st, pp)
        ev["steps"].append({"step": "orders_v2_funding_order", "ok": True, "at": now(), "order_id": o["id"],
                            "status": o2["status"], "amount": o["amount"], "currency": o["currency"],
                            "approve_url_host": (o.get("approve_url") or "").split("/")[2] if o.get("approve_url") else None})
    except PayPalError as e:
        ev["steps"].append({"step": "orders_v2_funding_order", "ok": False, "at": now(), "error": str(e)})

    try:
        res = pipeline.approve_and_pay(st, pp, st.plan.total, approved_by="sandbox evidence script")
        ev["steps"].append({"step": "payouts_create_batch", "ok": True, "at": now(),
                            "payout_batch_id": res["batch_header"]["payout_batch_id"],
                            "sender_batch_id": res["batch_header"].get("sender_batch_header", {}).get("sender_batch_id"),
                            "batch_status": res["batch_header"].get("batch_status")})
        status = pipeline.refresh_status(st, pp, wait=True, timeout=120)
        hdr = status.get("batch_header", {})
        ev["steps"].append({"step": "payouts_poll_status", "ok": True, "at": now(),
                            "batch_status": hdr.get("batch_status"), "amount": hdr.get("amount"),
                            "fees": hdr.get("fees"), "items": redact_items(status)})
        if pipeline.items_with(st, {"UNCLAIMED"}):
            pipeline.cancel_unclaimed(st, pp)
            status = pipeline.refresh_status(st, pp)
            ev["steps"].append({"step": "payouts_cancel_unclaimed", "ok": True, "at": now(),
                                "items": redact_items(status)})
    except PayPalError as e:
        ev["steps"].append({"step": "payouts_create_batch", "ok": False, "at": now(), "http_status": e.status,
                            "error": (e.body.get("name") if isinstance(e.body, dict) else str(e.body)[:100]),
                            "debug_id": e.debug_id, "detail": str(e)})

    Store().put(st)
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

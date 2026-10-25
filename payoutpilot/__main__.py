"""CLI: python -m payoutpilot run examples/campaign.json examples/submissions.csv [--pay] [--no-review]"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import llm as llm_mod
from . import paypal as paypal_mod
from . import pipeline
from .models import Campaign
from .store import CampaignState, Store


def main() -> int:
    p = argparse.ArgumentParser(prog="payoutpilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check, judge and plan a campaign; optionally pay after confirmation")
    r.add_argument("campaign")
    r.add_argument("submissions")
    r.add_argument("--pay", action="store_true", help="ask for approval, then send the PayPal payout")
    r.add_argument("--yes", action="store_true", help="approve without prompting (sandbox demos only)")
    r.add_argument("--no-review", action="store_true")
    sub.add_parser("serve", help="run the web dashboard on :8000")
    a = p.parse_args()

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("payoutpilot.api:app", host="0.0.0.0", port=8000)
        return 0

    st = CampaignState(campaign=Campaign.model_validate_json(Path(a.campaign).read_text()))
    st.submissions = pipeline.parse_csv(Path(a.submissions).read_text())
    pipeline.run_checks(st)
    for sid, fl in st.flags.items():
        for f in fl:
            print(f"  flag {sid}: {f.severity} {f.code} {f.detail}")
    llm = llm_mod.client_from_env()
    print(f"judging {len(st.submissions)} submissions with {getattr(llm, 'model', '?')} ...")
    pipeline.run_judge(st, llm, review=not a.no_review)
    pipeline.make_plan(st)
    for sc in sorted(st.scores, key=lambda s: -s.total):
        print(f"  {sc.submission_id}: {sc.total:5.1f}  reviewer={sc.reviewer_agrees}  {'; '.join(sc.reasons)[:90]}")
    print("\nPAYOUT PLAN")
    for ln in st.plan.lines:
        print(f"  #{ln.rank} {ln.handle:<16} {ln.paypal_email:<28} {ln.tier:<10} {ln.amount} {st.plan.currency}")
    for sid, why in st.plan.skipped.items():
        print(f"  skipped {sid}: {why}")
    print(f"  total {st.plan.total} {st.plan.currency}")
    store = Store()
    store.put(st)
    if a.pay:
        ok = a.yes or input(f"Type the total ({st.plan.total}) to approve the payout: ").strip() == str(st.plan.total)
        if not ok:
            print("not approved, nothing paid")
            return 1
        pp = paypal_mod.client_from_env()
        res = pipeline.approve_and_pay(st, pp, st.plan.total)
        print("payout batch:", res["batch_header"]["payout_batch_id"], res["batch_header"]["batch_status"])
        status = pipeline.refresh_status(st, pp)
        for it in status.get("items", []):
            print(f"  {it['payout_item']['receiver']:<28} {it['transaction_status']}")
        store.put(st)
    print(json.dumps({"saved": str(store.root / f"{st.campaign.id}.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

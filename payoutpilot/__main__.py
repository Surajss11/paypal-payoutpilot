"""PayoutPilot CLI.

  python -m payoutpilot run examples/campaign.json examples/submissions.csv [--pay] [--yes] [--no-review]
  python -m payoutpilot ask "Pay alice@x.com $5 for the bug fix and bob@y.com $2 as runner-up" [--pay] [--yes]
  python -m payoutpilot status <campaign_id> [--wait]     poll PayPal for batch + item status
  python -m payoutpilot retry <campaign_id>               re-send only FAILED/BLOCKED/DENIED items
  python -m payoutpilot cancel-unclaimed <campaign_id>    return money for UNCLAIMED items
  python -m payoutpilot fund <campaign_id>                Orders v2: sponsor pays the prize pool
  python -m payoutpilot serve                             AG Grid dashboard on :8000
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import agent, pipeline
from . import llm as llm_mod
from . import paypal as paypal_mod
from .config import load_env_files, mask_email
from .models import Campaign
from .store import CampaignState, Store


def _print_plan(st: CampaignState) -> None:
    print("\nPAYOUT PLAN (review before approving)")
    for ln in st.plan.lines:
        print(f"  #{ln.rank:<2} {ln.handle:<18} {ln.paypal_email:<34} {ln.tier:<10} {ln.amount:>9} {st.plan.currency}  {ln.note[:40]}")
    for sid, why in st.plan.skipped.items():
        print(f"  skipped {sid}: {why}")
    for w in st.warnings:
        print(f"  ! {w}")
    print(f"  total {st.plan.total} {st.plan.currency}")


def _print_status(st: CampaignState) -> None:
    s = st.payout_status or {}
    hdr = s.get("batch_header", {})
    print(f"payout batch {st.payout_batch_id}: {hdr.get('batch_status')}  "
          f"amount {hdr.get('amount', {}).get('value')} fees {hdr.get('fees', {}).get('value')}")
    for it in s.get("items", []):
        pi = it["payout_item"]
        print(f"  {it.get('payout_item_id')}  {mask_email(pi['receiver']):<30} {pi['amount']['value']:>8} "
              f"{pi['amount']['currency']}  {it.get('transaction_status')}"
              + (f"  ({it['errors'].get('name')})" if it.get("errors") else ""))


def _pay(st: CampaignState, store: Store, yes: bool) -> int:
    if not st.plan or not st.plan.lines:
        print("nothing to pay (see warnings above)")
        return 1
    ok = yes or input(f"Type the total ({st.plan.total}) to approve the payout: ").strip() == str(st.plan.total)
    if not ok:
        print("not approved, nothing paid")
        return 1
    pp = paypal_mod.client_from_env()
    print(f"PayPal: {pp.mode}")
    try:
        res = pipeline.approve_and_pay(st, pp, st.plan.total, approved_by="cli --yes" if yes else "cli operator")
    except paypal_mod.PayPalError as e:
        store.put(st)
        print(f"PayPal refused the batch, nothing was paid: {e}")
        return 2
    print("payout batch created:", res["batch_header"]["payout_batch_id"], res["batch_header"]["batch_status"])
    store.put(st)
    print("polling status ...")
    pipeline.refresh_status(st, pp, wait=True)
    _print_status(st)
    store.put(st)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="payoutpilot")
    p.add_argument("--env-file", action="append", help="extra KEY=VALUE files to load (default .env, .env.paypal)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check, judge and plan a campaign; optionally pay after confirmation")
    r.add_argument("campaign")
    r.add_argument("submissions")
    r.add_argument("--pay", action="store_true", help="ask for approval, then send the PayPal payout")
    r.add_argument("--yes", action="store_true", help="approve without prompting (sandbox demos only)")
    r.add_argument("--no-review", action="store_true")
    a_ = sub.add_parser("ask", help="natural-language or CSV payout request -> reviewed plan -> approval -> PayPal")
    a_.add_argument("text", help="request text, or @file.txt / @file.csv")
    a_.add_argument("--title", default="")
    a_.add_argument("--pay", action="store_true")
    a_.add_argument("--yes", action="store_true")
    for name in ("status", "retry", "cancel-unclaimed", "fund"):
        x = sub.add_parser(name)
        x.add_argument("campaign_id")
        if name == "status":
            x.add_argument("--wait", action="store_true")
    sub.add_parser("serve", help="run the web dashboard on :8000")
    a = p.parse_args()
    load_env_files()
    if a.env_file:
        load_env_files(a.env_file)

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("payoutpilot.api:app", host="0.0.0.0", port=8000)
        return 0

    store = Store()
    if a.cmd in ("status", "retry", "cancel-unclaimed", "fund"):
        st, pp = store.get(a.campaign_id), paypal_mod.client_from_env()
        if a.cmd == "status":
            pipeline.refresh_status(st, pp, wait=a.wait)
        elif a.cmd == "retry":
            res = pipeline.retry_failed(st, pp)
            print("nothing to retry" if res is None else f"retry batch {res['batch_header']['payout_batch_id']}")
            if res:
                pipeline.refresh_status(st, pp, wait=True)
        elif a.cmd == "cancel-unclaimed":
            done = pipeline.cancel_unclaimed(st, pp)
            print(f"cancelled {len(done)} unclaimed items")
            pipeline.refresh_status(st, pp)
        else:
            o = pipeline.create_funding_order(st, pp)
            print(json.dumps(o, indent=2))
        if a.cmd != "fund":
            _print_status(st)
        store.put(st)
        return 0

    llm = llm_mod.client_from_env()
    if a.cmd == "ask":
        text = Path(a.text[1:]).read_text() if a.text.startswith("@") and Path(a.text[1:]).is_file() else a.text
        states = [store.get(c) for c in store.list()]
        st = agent.plan_request(text, llm, pipeline.payee_directory(states), a.title)
        print(f"planner: {st.planner}")
        _print_plan(st)
        store.put(st)
        rc = _pay(st, store, a.yes) if a.pay else 0
        print(json.dumps({"saved": str(store.root / f"{st.campaign.id}.json")}))
        return rc

    st = CampaignState(campaign=Campaign.model_validate_json(Path(a.campaign).read_text()))
    st.submissions = pipeline.parse_csv(Path(a.submissions).read_text())
    pipeline.run_checks(st)
    for sid, fl in st.flags.items():
        for f in fl:
            print(f"  flag {sid}: {f.severity} {f.code} {f.detail}")
    print(f"judging {len(st.submissions)} submissions with {getattr(llm, 'model', '?')} ...")
    pipeline.run_judge(st, llm, review=not a.no_review)
    pipeline.make_plan(st)
    for sc in sorted(st.scores, key=lambda s: -s.total):
        print(f"  {sc.submission_id}: {sc.total:5.1f}  reviewer={sc.reviewer_agrees}  {'; '.join(sc.reasons)[:90]}")
    _print_plan(st)
    store.put(st)
    rc = _pay(st, store, a.yes) if a.pay else 0
    print(json.dumps({"saved": str(store.root / f"{st.campaign.id}.json")}))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

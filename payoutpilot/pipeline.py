"""End-to-end steps shared by the CLI and the API: check -> judge -> plan -> approve -> pay -> track."""

from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from . import integrity
from .allocator import allocate
from .judge import score
from .models import Score, Submission
from .store import CampaignState


def parse_csv(text: str) -> list[Submission]:
    rows = csv.DictReader(io.StringIO(text))
    out = []
    for i, row in enumerate(rows, start=1):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        out.append(Submission(id=row.get("id") or f"s{i:03d}", handle=row.get("handle") or row.get("name") or f"user{i}",
                              paypal_email=row.get("paypal_email") or row.get("email", ""),
                              link=row.get("link") or row.get("url", ""), text=row.get("text") or row.get("content", "")))
    return out


def run_checks(st: CampaignState) -> None:
    st.flags = integrity.check(st.campaign, st.submissions)
    st.log("checked", f"{len(st.submissions)} submissions, {len(integrity.blocked(st.flags))} blocked by integrity checks")


def excluded(st: CampaignState) -> set[str]:
    return integrity.blocked(st.flags) | set(st.manual_excluded)


def run_judge(st: CampaignState, llm, review: bool = True, workers: int = 4) -> None:
    if st.payout_batch_id:
        raise ValueError(f"already paid in batch {st.payout_batch_id}; start a new run to re-judge")
    skip = integrity.blocked(st.flags)
    todo = [s for s in st.submissions if s.id not in skip]

    def one(s: Submission) -> Score:
        try:
            return score(st.campaign, s, llm, review=review)
        except Exception as e:  # a bad model reply should not kill the batch; it is visible in the grid
            return Score(submission_id=s.id, total=0.0, reasons=[f"judge error: {e}"])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        st.scores = list(ex.map(one, todo))
    agree = sum(1 for s in st.scores if s.reviewer_agrees)
    st.log("judged", f"{len(st.scores)} scored by {getattr(llm, 'model', '?')}, reviewer agreed on {agree}")


def make_plan(st: CampaignState) -> None:
    if st.payout_batch_id:
        raise ValueError(f"already paid in batch {st.payout_batch_id}; re-planning could double-pay, start a new run")
    st.plan = allocate(st.campaign, st.submissions, st.scores, excluded(st))
    st.payout_batch_id, st.payout_status = None, None
    st.log("planned", f"{len(st.plan.lines)} winners, total {st.plan.total} {st.plan.currency}, {len(st.plan.skipped)} skipped")


def approve_and_pay(st: CampaignState, paypal, confirm_total: Decimal, approved_by: str = "human") -> dict:
    """Human approval gate: the caller must echo the exact plan total; payment is idempotent per plan."""
    if st.plan is None or not st.plan.lines:
        raise ValueError("no payout plan to approve")
    if Decimal(str(confirm_total)) != st.plan.total:
        raise ValueError(f"confirmation {confirm_total} does not match plan total {st.plan.total}")
    if st.campaign.budget is not None and st.plan.total > st.campaign.budget:
        raise ValueError("plan exceeds budget")
    if st.payout_batch_id:
        # PayPal-Request-Id replay would return the same batch anyway; skip the call and keep state consistent
        return st.payout_status or {"batch_header": {"payout_batch_id": st.payout_batch_id}}
    st.approved_by = approved_by
    st.log("approved", f"{approved_by} approved {st.plan.total} {st.plan.currency} for {len(st.plan.lines)} payees")
    try:
        res = paypal.create_payout(st.plan, subject=f"You received a payout from {st.campaign.title}"[:255],
                                   message=f"Thanks for your work on {st.campaign.title}. Sent with PayoutPilot.")
    except Exception as e:
        st.log("payout_error", str(e)[:500])
        raise
    st.payout_batch_id = res["batch_header"]["payout_batch_id"]
    st.batches.append(st.payout_batch_id)
    st.payout_status = res
    st.log("payout_created", f"batch {st.payout_batch_id} status {res['batch_header'].get('batch_status')}")
    return res


def refresh_status(st: CampaignState, paypal, wait: bool = False, timeout: float = 90.0) -> dict:
    if not st.payout_batch_id:
        raise ValueError("nothing paid yet")
    st.payout_status = (paypal.wait_for_payout(st.payout_batch_id, timeout=timeout) if wait
                        else paypal.get_payout(st.payout_batch_id))
    hdr = st.payout_status.get("batch_header", {})
    counts: dict[str, int] = {}
    for it in st.payout_status.get("items", []):
        counts[it.get("transaction_status", "?")] = counts.get(it.get("transaction_status", "?"), 0) + 1
    st.log("status", f"batch {st.payout_batch_id} {hdr.get('batch_status')} items {counts}")
    return st.payout_status


def items_with(st: CampaignState, statuses: set[str]) -> list[dict]:
    return [i for i in (st.payout_status or {}).get("items", []) if i.get("transaction_status") in statuses]


def retry_failed(st: CampaignState, paypal) -> dict | None:
    """Send a follow-up batch only for items PayPal marked FAILED / BLOCKED / DENIED.
    The retry batch gets its own deterministic sender_batch_id (suffix -rN) so it is idempotent too."""
    from .paypal import ITEM_RETRYABLE

    bad = {i["payout_item"]["sender_item_id"] for i in items_with(st, ITEM_RETRYABLE)}
    if not bad or st.plan is None:
        return None
    retry_plan = st.plan.model_copy(update={"lines": [ln for ln in st.plan.lines if ln.submission_id in bad]})
    res = paypal.create_payout(retry_plan, subject=f"Payout retry from {st.campaign.title}"[:255],
                               message="Retrying your payout from PayoutPilot.", attempt=len(st.batches))
    st.payout_batch_id = res["batch_header"]["payout_batch_id"]
    st.batches.append(st.payout_batch_id)
    st.payout_status = res
    st.log("retry_created", f"batch {st.payout_batch_id} for {len(bad)} failed items")
    return res


def cancel_unclaimed(st: CampaignState, paypal) -> list[dict]:
    """Return money for items nobody claimed (e.g. the winner has no PayPal account yet)."""
    from .paypal import PayPalError

    out = []
    for it in items_with(st, {"UNCLAIMED"}):
        iid = it["payout_item_id"]
        try:
            res = paypal.cancel_unclaimed(iid)
        except PayPalError as e:
            # A 5xx can still have cancelled the item server-side: trust the item's real status, not the error.
            cur = paypal.get_payout_item(iid)
            if cur.get("transaction_status") != "RETURNED":
                st.log("cancel_error", f"item {iid}: {e}"[:500])
                raise
            res = cur
        out.append(res)
        st.log("cancel_unclaimed", f"item {iid} {res.get('transaction_status', 'RETURNED')}, money back to sender")
    return out


def create_funding_order(st: CampaignState, paypal) -> dict:
    """Orders v2: the sponsor pays the prize pool (plan total) into the organiser's PayPal before payouts go out."""
    if st.plan is None or not st.plan.lines:
        raise ValueError("no payout plan to fund")
    o = paypal.create_order(st.campaign.id, st.plan.total, st.plan.currency, f"Prize pool: {st.campaign.title}")
    from .paypal import approve_link

    st.funding_order = {"id": o["id"], "status": o.get("status"), "approve_url": approve_link(o),
                        "amount": str(st.plan.total), "currency": st.plan.currency}
    st.log("funding_order", f"order {o['id']} {o.get('status')} for {st.plan.total} {st.plan.currency}")
    return st.funding_order


def refresh_funding_order(st: CampaignState, paypal, capture: bool = False) -> dict:
    if not st.funding_order:
        raise ValueError("no funding order yet")
    o = paypal.capture_order(st.funding_order["id"]) if capture else paypal.get_order(st.funding_order["id"])
    st.funding_order["status"] = o.get("status")
    st.log("funding_order", f"order {o['id']} {o.get('status')}")
    return st.funding_order


def payee_directory(states: list[CampaignState]) -> dict[str, str]:
    """@handle -> PayPal email from earlier campaigns, so 'pay @alpha_ana $10' can be resolved and reviewed."""
    out: dict[str, str] = {}
    for st in states:
        for s in st.submissions:
            if s.paypal_email:
                out.setdefault(s.handle.lower().lstrip("@"), s.paypal_email)
    return out

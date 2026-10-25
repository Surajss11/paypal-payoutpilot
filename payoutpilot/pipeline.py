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


def excluded(st: CampaignState) -> set[str]:
    return integrity.blocked(st.flags) | set(st.manual_excluded)


def run_judge(st: CampaignState, llm, review: bool = True, workers: int = 4) -> None:
    skip = integrity.blocked(st.flags)
    todo = [s for s in st.submissions if s.id not in skip]

    def one(s: Submission) -> Score:
        try:
            return score(st.campaign, s, llm, review=review)
        except Exception as e:  # a bad model reply should not kill the batch; it is visible in the grid
            return Score(submission_id=s.id, total=0.0, reasons=[f"judge error: {e}"])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        st.scores = list(ex.map(one, todo))


def make_plan(st: CampaignState) -> None:
    st.plan = allocate(st.campaign, st.submissions, st.scores, excluded(st))
    st.payout_batch_id, st.payout_status = None, None


def approve_and_pay(st: CampaignState, paypal, confirm_total: Decimal) -> dict:
    """Human approval gate: the caller must echo the exact plan total; payment is idempotent per plan."""
    if st.plan is None or not st.plan.lines:
        raise ValueError("no payout plan to approve")
    if Decimal(confirm_total) != st.plan.total:
        raise ValueError(f"confirmation {confirm_total} does not match plan total {st.plan.total}")
    if st.campaign.budget is not None and st.plan.total > st.campaign.budget:
        raise ValueError("plan exceeds budget")
    res = paypal.create_payout(st.plan, subject=f"You won in {st.campaign.title}",
                               message=f"Congrats! Your prize for {st.campaign.title} is here.")
    st.payout_batch_id = res["batch_header"]["payout_batch_id"]
    st.payout_status = res
    return res


def refresh_status(st: CampaignState, paypal) -> dict:
    if not st.payout_batch_id:
        raise ValueError("nothing paid yet")
    st.payout_status = paypal.get_payout(st.payout_batch_id)
    return st.payout_status

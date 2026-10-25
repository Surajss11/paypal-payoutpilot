"""Turn a ranking into a prize plan within the campaign budget."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from .models import Campaign, PayoutLine, PayoutPlan, Score, Submission

CENT = Decimal("0.01")


def allocate(campaign: Campaign, subs: list[Submission], scores: list[Score], excluded: set[str]) -> PayoutPlan:
    by_id = {s.id: s for s in subs}
    skipped: dict[str, str] = {sid: "integrity check" for sid in excluded if sid in by_id}
    ranked = []
    for sc in scores:
        if sc.submission_id in excluded or sc.submission_id not in by_id:
            continue
        if sc.total < campaign.min_score:
            skipped[sc.submission_id] = f"score {sc.total:.1f} below minimum {campaign.min_score}"
            continue
        ranked.append(sc)
    # Higher score first; ties broken by submission order (earlier wins), which is stable and explainable.
    order = {s.id: i for i, s in enumerate(subs)}
    ranked.sort(key=lambda sc: (-sc.total, order[sc.submission_id]))

    slots: list[tuple[str, Decimal]] = []
    for tier in campaign.prizes:
        slots.extend((tier.label, tier.amount.quantize(CENT, rounding=ROUND_DOWN)) for _ in range(tier.places))

    lines: list[PayoutLine] = []
    for rank, (sc, (label, amount)) in enumerate(zip(ranked, slots), start=1):
        s = by_id[sc.submission_id]
        lines.append(PayoutLine(submission_id=s.id, handle=s.handle, paypal_email=s.paypal_email,
                                tier=label, amount=amount, rank=rank))

    plan = PayoutPlan(campaign_id=campaign.id, currency=campaign.currency, lines=lines, skipped=skipped)
    if campaign.budget is not None and plan.total > campaign.budget:
        raise ValueError(f"prize plan {plan.total} exceeds budget {campaign.budget}")
    return plan

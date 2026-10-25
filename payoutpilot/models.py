from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Criterion(BaseModel):
    name: str
    weight: float = Field(gt=0)
    description: str = ""


class PrizeTier(BaseModel):
    """`places` winners each get `amount`. Tiers are filled in order."""

    label: str
    amount: Decimal = Field(gt=0)
    places: int = Field(default=1, ge=1)


class Campaign(BaseModel):
    id: str
    title: str
    brief: str
    rubric: list[Criterion] = Field(default_factory=list)
    prizes: list[PrizeTier] = Field(default_factory=list)
    currency: str = "USD"
    budget: Decimal | None = None
    required_fields: list[str] = Field(default_factory=lambda: ["link", "paypal_email"])
    min_score: float = 0.0

    @property
    def total_prize(self) -> Decimal:
        return sum((t.amount * t.places for t in self.prizes), Decimal(0))


class Submission(BaseModel):
    id: str
    handle: str
    paypal_email: str = ""
    link: str = ""
    text: str = ""

    @field_validator("paypal_email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        return v.strip().lower()


Severity = Literal["block", "warn"]


class Flag(BaseModel):
    code: str
    severity: Severity
    detail: str


class Score(BaseModel):
    submission_id: str
    total: float = 0.0
    per_criterion: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    reviewer_agrees: bool | None = None
    reviewer_notes: list[str] = Field(default_factory=list)


class PayoutLine(BaseModel):
    submission_id: str
    handle: str
    paypal_email: str
    tier: str
    amount: Decimal
    rank: int
    note: str = ""


class PayoutPlan(BaseModel):
    campaign_id: str
    currency: str
    lines: list[PayoutLine]
    skipped: dict[str, str] = Field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal(0))

"""Tiny JSON-file state store (one file per campaign) so runs survive restarts and are easy to inspect."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .models import Campaign, Flag, PayoutPlan, Score, Submission


class CampaignState(BaseModel):
    campaign: Campaign
    submissions: list[Submission] = Field(default_factory=list)
    flags: dict[str, list[Flag]] = Field(default_factory=dict)
    scores: list[Score] = Field(default_factory=list)
    manual_excluded: list[str] = Field(default_factory=list)
    plan: PayoutPlan | None = None
    payout_batch_id: str | None = None
    payout_status: dict | None = None


class Store:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("PAYOUTPILOT_DATA", "data"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, cid: str) -> Path:
        safe = "".join(ch for ch in cid if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("invalid campaign id")
        return self.root / f"{safe}.json"

    def get(self, cid: str) -> CampaignState:
        p = self._path(cid)
        if not p.exists():
            raise KeyError(cid)
        return CampaignState.model_validate_json(p.read_text())

    def put(self, st: CampaignState) -> None:
        p = self._path(st.campaign.id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(st.model_dump_json(indent=2))
        tmp.replace(p)

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def dump(self, cid: str) -> dict:
        return json.loads(self._path(cid).read_text())

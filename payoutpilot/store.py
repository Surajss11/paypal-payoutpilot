"""Tiny JSON-file state store (one file per campaign) so runs survive restarts and are easy to inspect."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import Campaign, Flag, PayoutPlan, Score, Submission


class Event(BaseModel):
    ts: str
    kind: str
    detail: str = ""


class CampaignState(BaseModel):
    campaign: Campaign
    kind: Literal["campaign", "request"] = "campaign"
    request_text: str = ""
    planner: str = ""
    warnings: list[str] = Field(default_factory=list)
    submissions: list[Submission] = Field(default_factory=list)
    flags: dict[str, list[Flag]] = Field(default_factory=dict)
    scores: list[Score] = Field(default_factory=list)
    manual_excluded: list[str] = Field(default_factory=list)
    plan: PayoutPlan | None = None
    approved_by: str | None = None
    payout_batch_id: str | None = None
    payout_status: dict | None = None
    batches: list[str] = Field(default_factory=list)
    funding_order: dict | None = None
    events: list[Event] = Field(default_factory=list)

    def log(self, kind: str, detail: str = "") -> None:
        self.events.append(Event(ts=datetime.now(timezone.utc).isoformat(timespec="seconds"), kind=kind, detail=detail))


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
        """Newest first, so the dashboard opens the latest run."""
        return [p.stem for p in sorted(self.root.glob("*.json"), key=lambda p: -p.stat().st_mtime)]

    def dump(self, cid: str) -> dict:
        return json.loads(self._path(cid).read_text())

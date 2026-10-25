"""Deterministic integrity checks that run before any model sees a submission."""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from .models import Campaign, Flag, Submission

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BAIT_RE = re.compile(
    r"(seed phrase|secret recovery phrase|private key|connect (your )?wallet|claim (your )?airdrop now|"
    r"validate (your )?wallet|wallet ?connect)",
    re.I,
)


def normalize_link(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = parts.netloc.lower().removeprefix("www.").replace("twitter.com", "x.com")
    path = parts.path.rstrip("/")
    return f"{host}{path}"


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def check(campaign: Campaign, subs: list[Submission], near_dup_threshold: float = 0.85,
          block_near_dups: bool = True) -> dict[str, list[Flag]]:
    flags: dict[str, list[Flag]] = defaultdict(list)

    for s in subs:
        for f in campaign.required_fields:
            if not str(getattr(s, f, "") or "").strip():
                flags[s.id].append(Flag(code="missing_field", severity="block", detail=f"missing {f}"))
        if s.paypal_email and not EMAIL_RE.match(s.paypal_email):
            flags[s.id].append(Flag(code="bad_email", severity="block", detail=s.paypal_email))
        if s.link and urlsplit(s.link if "://" in s.link else f"https://{s.link}").scheme not in ("http", "https"):
            flags[s.id].append(Flag(code="bad_link", severity="block", detail=s.link))
        if BAIT_RE.search(s.text or ""):
            flags[s.id].append(Flag(code="wallet_bait", severity="block", detail="asks for wallet / seed actions"))

    by_email: dict[str, list[str]] = defaultdict(list)
    by_link: dict[str, list[str]] = defaultdict(list)
    for s in subs:
        if s.paypal_email:
            by_email[s.paypal_email].append(s.id)
        if s.link:
            by_link[normalize_link(s.link)].append(s.id)
    for email, ids in by_email.items():
        if len(ids) > 1:
            for i in ids[1:]:
                flags[i].append(Flag(code="duplicate_payee", severity="block", detail=f"{email} already used by {ids[0]}"))
    for link, ids in by_link.items():
        if len(ids) > 1:
            for i in ids[1:]:
                flags[i].append(Flag(code="duplicate_link", severity="block", detail=f"{link} already submitted by {ids[0]}"))

    texts = [(s.id, s.text) for s in subs if len(s.text or "") >= 80]
    for i, (id_a, a) in enumerate(texts):
        for id_b, b in texts[i + 1 :]:
            if _similar(a, b) >= near_dup_threshold:
                flags[id_b].append(Flag(code="near_duplicate", severity="block" if block_near_dups else "warn",
                                         detail=f"text {_similar(a, b):.0%} similar to earlier {id_a}"))
    return dict(flags)


def blocked(flags: dict[str, list[Flag]]) -> set[str]:
    return {sid for sid, fl in flags.items() if any(f.severity == "block" for f in fl)}

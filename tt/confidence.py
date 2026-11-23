"""Source tiers, two-date checks and confidence scoring for every fact shown to the user."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from urllib.parse import urlparse

from .config import (CONFIDENCE_RECHECK, CONFIDENCE_USE, CONFIRM_BONUS, CONFIRM_BONUS_MAX,
                     CONFLICT_PENALTY, SOURCE_TIERS, STALE_PENALTY, TIER1_DOMAINS, TIER2_DOMAINS,
                     TIER3_DOMAINS)


def tier_of(url: str) -> int:
    host = (urlparse(url).hostname or "").lower()
    def match(domains):
        return any(host == d or host.endswith("." + d) for d in domains)
    if match(TIER1_DOMAINS):
        return 1
    if match(TIER2_DOMAINS):
        return 2
    if match(TIER3_DOMAINS):
        return 3
    return 4


@dataclass
class Fact:
    key: str
    label: str
    value: str
    url: str
    as_of: str                 # date the fact refers to (content date)
    fetched_at: str
    label_date: str = ""       # date in file name / header / filing timestamp
    tier: int = 0
    confirmations: int = 0
    conflict: bool = False
    stale: bool = False
    date_check: str = ""       # "pass" | "fail" | "n/a"
    score: float = 0.0
    status: str = ""           # "fact" | "unverified" | "discarded"
    note: str = ""
    extra: dict = field(default_factory=dict)

    def finalize(self) -> "Fact":
        if not self.tier:
            self.tier = tier_of(self.url)
        s = SOURCE_TIERS[self.tier]["score"]
        s += min(CONFIRM_BONUS_MAX, CONFIRM_BONUS * self.confirmations)
        if self.conflict:
            s -= CONFLICT_PENALTY
        if self.stale:
            s -= STALE_PENALTY
        if self.date_check == "fail":
            s = min(s, CONFIDENCE_RECHECK - 0.01)
        if self.tier == 4:
            s = 0.0
        self.score = round(max(0.0, min(1.0, s)), 2)
        if self.score >= CONFIDENCE_USE:
            self.status = "fact"
        elif self.score >= CONFIDENCE_RECHECK:
            self.status = "unverified"
        else:
            self.status = "discarded"
        # An official (Tier-1) record that is simply older than the decision window is still true, but it is
        # background only: never used to make today's decision.
        if self.stale and self.tier == 1 and self.date_check != "fail" and not self.conflict:
            self.status = "background"
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def two_date_check(label: date | str | None, content: date | str | None) -> str:
    if not label or not content:
        return "n/a"
    def d(x):
        if isinstance(x, datetime):
            return x.date()
        if isinstance(x, date):
            return x
        return date.fromisoformat(str(x)[:10])
    return "pass" if d(label) == d(content) else "fail"

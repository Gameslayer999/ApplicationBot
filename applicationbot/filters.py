"""Discovery filters (Stage 1 / Configure seed) — what the user wants, and where to look.

This is the small, git-ignored config that drives *qualification-driven* discovery
(DECISIONS.md #025). Most of the "am I qualified?" judgment comes from the résumé + apply
profile (via the matcher), so this file stays deliberately minimal — it holds only what
those can't supply:

- `boards`   : optional target ATS boards to poll (Greenhouse/Lever/Ashby). The user chose
               qualification-over-company, so this is a *convenience* list of specific
               companies to include, not a required universe — the aggregator source finds
               roles without it.
- coarse gates the matcher shouldn't spend a Claude call on: `remote_only`, `min_salary`,
  `title_exclude`.
- matcher knobs: `min_skills` (keyword pre-filter floor), `top_n` (how many survivors
  Claude judges).

Search keywords for aggregator sources are *derived from the profile* (skills + recent
titles) rather than hand-entered — see `derive_keywords`.

Stored at `profile/discovery.yaml` (git-ignored). The full Configure-stage schema is
future work; this is the seed the discovery loop needs today.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

import os

from .apply_profile import ApplicationProfile
from .discovery import AdzunaSource, CuratedListSource, Source, build_source
from .models import Resume

DEFAULT_PATH = "profile/discovery.yaml"

_HEADER = (
    "# ApplicationBot discovery filters — where to look + coarse gates for job discovery.\n"
    "# Git-ignored. Qualification matching is driven by your résumé + apply profile;\n"
    "# this file only holds target boards and gates the matcher can't infer.\n"
)


class Board(BaseModel):
    ats: str  # greenhouse | lever | ashby | smartrecruiters | recruitee | workable
    token: str  # the board token/company slug read off the careers URL (e.g. 'stripe', 'Visa', 'bunq', 'mlabs')


class AdzunaConfig(BaseModel):
    """Broad aggregator (optional). Free keys from developer.adzuna.com; leave blank (or set
    ADZUNA_APP_ID / ADZUNA_APP_KEY in the environment) and this source is skipped."""

    app_id: str = ""
    app_key: str = ""
    country: str = "us"
    max_pages: int = 1  # 50 results/page; raise for more breadth


class EarlyCareerConfig(BaseModel):
    """Discover from community new-grad/internship JSON feeds (SimplifyJobs) — early-career by
    construction, no company list needed. Off by default (DECISIONS.md #031)."""

    enabled: bool = False
    kinds: list[str] = Field(default_factory=lambda: ["new-grad", "intern"])  # new-grad | intern
    max_resolve: int = 40  # how many top title-relevant listings to resolve full JD for + judge


# Experience-level taxonomy (Configure/Discover gate). Each level maps to a regex matched
# against the posting TITLE — where seniority reliably appears (same signal as title_exclude).
# Word-boundaried so "intern" doesn't hit "international", "lead" doesn't hit "leading", etc.
_LEVEL_PATTERNS: dict[str, str] = {
    "internship": r"\bintern(?:s|ship)?\b|\bco-?op\b",
    "new_grad": (
        r"\bnew[\s-]*grad(?:uate)?s?\b|\brecent[\s-]*grad(?:uate)?s?\b|"
        r"\bentry[\s-]*level\b|\bearly[\s-]*career\b|\buniversity[\s-]*grad(?:uate)?\b|\bcampus\b"
    ),
    "junior": r"\bjunior\b|\bjr\.?\b",
    "mid": r"\bmid[\s-]*(?:level|senior)?\b",
    "senior": r"\bsenior\b|\bsr\.?\b",
    "staff": r"\bstaff\b|\bprincipal\b|\bdistinguished\b",
    "manager": r"\bmanager\b|\bdirector\b|\bhead\s+of\b|\bvp\b|\bvice\s+president\b|\blead\b",
}
EXPERIENCE_LEVELS: list[str] = list(_LEVEL_PATTERNS)  # valid values, for config/UI/docs
_LEVEL_RE = {lvl: re.compile(pat, re.IGNORECASE) for lvl, pat in _LEVEL_PATTERNS.items()}


def _norm_level(s: str) -> str:
    """Normalize a user-written level ('New Grad', 'new-grad') to a taxonomy key ('new_grad')."""
    return re.sub(r"[\s-]+", "_", s.strip().lower())


def detect_levels(title: str) -> set[str]:
    """Experience levels named in a posting title (may be empty, or more than one)."""
    return {lvl for lvl, rx in _LEVEL_RE.items() if rx.search(title)}


class DiscoveryFilters(BaseModel):
    boards: list[Board] = Field(default_factory=list)
    remote_only: bool = False
    min_salary: int = 0  # annual, in the profile's currency; 0 = no floor
    title_exclude: list[str] = Field(
        default_factory=list,
        description="Drop postings whose TITLE contains any of these (case-insensitive), "
        "e.g. 'sales', 'recruiter' — a cheap gate before the matcher.",
    )
    experience_levels: list[str] = Field(
        default_factory=list,
        description="Keep only postings at these experience levels, detected from the TITLE: "
        + ", ".join(EXPERIENCE_LEVELS)
        + ". Empty = no level gate. Lenient: a posting whose title clearly names a "
        "DIFFERENT level is dropped; a title with no clear level passes to the matcher.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Optional aggregator search terms. Empty = derive from the résumé.",
    )
    min_skills: int = 2  # keyword pre-filter floor (raise to cut common-word false positives)
    top_n: int = 20  # how many keyword-ranked survivors Claude judges (more = more chances to clear min_fit)
    min_fit: int = 50  # only follow through (dry-run/apply) on matches Claude scores ≥ this (0-100)
    skip_seen: bool = True  # drop postings already in the tracker (don't re-apply to the same role)
    adzuna: AdzunaConfig = Field(default_factory=AdzunaConfig)
    early_career: EarlyCareerConfig = Field(default_factory=EarlyCareerConfig)


def load_filters(path: str | Path = DEFAULT_PATH) -> DiscoveryFilters:
    p = Path(path)
    if not p.exists():
        return DiscoveryFilters()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return DiscoveryFilters.model_validate(data)


def save_filters(filters: DiscoveryFilters, path: str | Path = DEFAULT_PATH) -> None:
    body = yaml.safe_dump(filters.model_dump(), sort_keys=False, allow_unicode=True)
    Path(path).write_text(_HEADER + body, encoding="utf-8")


def build_sources(
    filters: DiscoveryFilters,
    resume: Resume | None = None,
    profile: ApplicationProfile | None = None,
) -> list[Source]:
    """All configured sources behind one interface (DECISIONS.md #025): the ATS boards
    (per-company, full JD) plus the broad aggregator when it's configured and we have a
    résumé to derive search keywords from. The aggregator gracefully self-skips if no key."""
    sources: list[Source] = [build_source(b.ats, b.token) for b in filters.boards]
    agg = build_aggregator(filters, resume, profile)
    if agg is not None:
        sources.append(agg)
    # Early-career curated feeds (needs the résumé to rank listings by title-relevance).
    if filters.early_career.enabled and resume is not None:
        sources.append(CuratedListSource(
            resume, kinds=tuple(filters.early_career.kinds or ["new-grad", "intern"]),
            max_resolve=filters.early_career.max_resolve,
        ))
    return sources


def build_aggregator(
    filters: DiscoveryFilters,
    resume: Resume | None,
    profile: ApplicationProfile | None,
) -> Source | None:
    """Build the Adzuna source from config/env + profile-derived keywords, or None if it
    isn't configured (no key) — keeping the tool cloneable and runnable without a key."""
    cfg = filters.adzuna
    app_id = cfg.app_id or os.environ.get("ADZUNA_APP_ID", "")
    app_key = cfg.app_key or os.environ.get("ADZUNA_APP_KEY", "")
    if not (app_id and app_key and resume is not None):
        return None
    what = " ".join(derive_keywords(resume, profile or ApplicationProfile(), filters)[:6])
    where = (profile.location if profile else "") or ""
    return AdzunaSource(
        app_id, app_key, what=what, where=where,
        country=cfg.country, max_pages=cfg.max_pages, salary_min=filters.min_salary,
    )


def derive_keywords(resume: Resume, profile: ApplicationProfile, filters: DiscoveryFilters) -> list[str]:
    """Search terms for aggregator queries, derived from the profile when not hand-set:
    the candidate's most recent role titles + top skills. Qualification-driven — the query
    reflects what the user actually does, not a company list."""
    if filters.keywords:
        return filters.keywords
    terms: list[str] = []
    for exp in resume.experience[:2]:
        if exp.role:
            terms.append(exp.role)
    for cat in resume.skills:
        terms.extend(cat.items[:5])
    # dedup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


_SALARY_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def _annual_salary(comp: str) -> Optional[int]:
    """Best-effort parse of the largest salary-looking number in a compensation string,
    handling both plain ('175000') and 'K' notation ('$191K'). Returns None if none found
    (then the min_salary gate can't apply and we keep the post)."""
    if not comp:
        return None
    nums: list[int] = []
    for m in _SALARY_NUM.finditer(comp):
        val = float(m.group(1).replace(",", ""))
        if m.group(2):  # 'K' suffix
            val *= 1000
        n = int(val)
        if n >= 1000:  # ignore small fragments (e.g. a stray '401' from '401k')
            nums.append(n)
    return max(nums) if nums else None


def apply_gates(postings, filters: DiscoveryFilters):
    """Cheap pre-matcher gates from the filter config: remote_only, title_exclude, a salary
    floor when the posting states pay, and an experience-level gate (by title). Returns the
    kept postings. Postings with no stated salary — or no detectable level — pass their gate
    (we don't drop for missing data)."""
    excl = [t.lower() for t in filters.title_exclude]
    want_levels = {_norm_level(l) for l in filters.experience_levels} & set(EXPERIENCE_LEVELS)
    kept = []
    for p in postings:
        if filters.remote_only and p.remote is False:
            continue
        if excl and any(x in p.title.lower() for x in excl):
            continue
        if want_levels:
            # Lenient: drop only when the title clearly names a level and none is wanted;
            # a title with no detectable level passes through to the matcher.
            detected = detect_levels(p.title)
            if detected and detected.isdisjoint(want_levels):
                continue
        if filters.min_salary:
            sal = _annual_salary(p.compensation)
            if sal is not None and sal < filters.min_salary:
                continue
        kept.append(p)
    return kept

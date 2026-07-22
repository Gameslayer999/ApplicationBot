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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

import os

from .apply_profile import ApplicationProfile
from .discovery import (
    _BUILTIN_FEEDS,
    AdzunaSource,
    CareerSiteSource,
    CuratedListSource,
    GoogleJobsSource,
    HimalayasSource,
    RemoteOKSource,
    Source,
    build_source,
)
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
    max_pages: int = 3  # 50 results/page/query; raise for more breadth
    max_queries: int = 4  # how many focused profile-derived queries to run (each its own `what` search)
    sort_by: str = "date"  # "date" = freshest-first when paginating; "" = Adzuna relevance default


class GoogleJobsConfig(BaseModel):
    """Discover from the Google Jobs vertical — keyless (no app_id) and proxy-free. Off by default.

    ⚠ Currently NON-FUNCTIONAL via the keyless path: as of 2026-07 Google renders the Jobs vertical
    client-side, so a plain HTTP GET returns a JavaScript shell with no readable job data. When
    enabled it fails loudly (a source error) rather than silently returning nothing — it needs a
    headless-browser render path to work. Kept opt-in/off so it never affects a default run."""

    enabled: bool = False
    results_wanted: int = 40  # per query, before cross-query dedup
    max_queries: int = 2  # how many focused profile-derived queries to run (keep low — one IP, no proxies)


class RemoteBoardsConfig(BaseModel):
    """Keyless remote-job aggregators — public JSON APIs, no signup, no scraping. Opt-in per board
    (off by default like every source). Remote-only by construction, so most useful when you want
    remote roles; the gates still filter what doesn't fit. `max_results` caps what each board
    contributes before the gates/matcher."""

    himalayas: bool = False
    remoteok: bool = False
    max_results: int = 100


class FeedSpec(BaseModel):
    """One GitHub job board. Written in YAML as either a bare string — a built-in name
    ("new-grad") or a raw listings.json URL — or as an explicit {name, url} pair."""

    name: str = ""
    url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v):
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s.startswith(("http://", "https://")):
            return {"name": s, "url": _BUILTIN_FEEDS.get(s, "")}
        # Name a dropped-in feed after its repo, so it reads as "vanshb03/New-Grad-2026" in
        # logs and the discovery-cache fingerprint rather than a 100-char URL.
        m = re.search(r"githubusercontent\.com/([^/]+)/([^/]+)/", s)
        return {"name": f"{m.group(1)}/{m.group(2)}" if m else s, "url": s}


class EarlyCareerConfig(BaseModel):
    """Discover from community new-grad/internship JSON feeds — early-career by construction,
    no company list needed. Off by default (DECISIONS.md #031). `feeds` accepts any GitHub repo
    publishing the SimplifyJobs listings.json schema (DECISIONS.md #073)."""

    enabled: bool = False
    kinds: list[str] = Field(default_factory=lambda: ["new-grad", "intern"])  # new-grad | intern
    max_resolve: int = 40  # how many top title-relevant listings to resolve full JD for + judge
    feeds: list[FeedSpec] = Field(default_factory=list)  # extra boards; built-in name or raw URL

    @field_validator("feeds")
    @classmethod
    def _known(cls, v: list[FeedSpec]) -> list[FeedSpec]:
        for f in v:
            if not f.url:
                raise ValueError(
                    f"early_career.feeds: '{f.name}' is not a built-in feed "
                    f"({', '.join(_BUILTIN_FEEDS)}). Use a built-in name, or give the raw "
                    f"listings.json URL of a GitHub job board."
                )
        return v


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
    career_sites: list[str] = Field(
        default_factory=list,
        description="Career/posting page URLs to discover from via schema.org JobPosting "
        "(JSON-LD) structured data, with a CSS/DOM fallback — full JD, no scraping grey "
        "area. Point at posting pages or listing pages that embed JobPosting JSON-LD.",
    )
    remote_only: bool = False
    min_salary: int = 0  # annual, in the profile's currency; 0 = no floor
    title_exclude: list[str] = Field(
        default_factory=list,
        description="Drop postings whose TITLE contains any of these (case-insensitive), "
        "e.g. 'sales', 'recruiter' — a cheap gate before the matcher.",
    )
    company_exclude: list[str] = Field(
        default_factory=list,
        description="Drop postings whose COMPANY contains any of these (case-insensitive), "
        "e.g. staffing agencies that repost the same role many times ('Consultadd', "
        "'DellFor') — a cheap gate before the matcher.",
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
    calibrate_min_fit: bool = True  # auto-raise min_fit above a fit band your recorded outcomes prove dead (decision 043)
    skip_seen: bool = True  # drop postings already in the tracker (don't re-apply to the same role)
    cache_ttl_hours: float = 12  # reuse the last discovery snapshot (skip board search + Claude judge) if younger than this; 0 disables
    max_posting_age_days: Optional[int] = Field(
        default=None,
        description="Drop postings whose updated_at is older than this many days. "
        "None (default) = no age gate. Missing/unparseable dates pass.",
    )
    adzuna: AdzunaConfig = Field(default_factory=AdzunaConfig)
    google: GoogleJobsConfig = Field(default_factory=GoogleJobsConfig)
    remote_boards: RemoteBoardsConfig = Field(default_factory=RemoteBoardsConfig)
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
    if filters.career_sites:
        sources.append(CareerSiteSource(filters.career_sites))
    agg = build_aggregator(filters, resume, profile)
    if agg is not None:
        sources.append(agg)
    # Google Jobs vertical (keyless aggregator, opt-in). Profile-derived focused queries, same as
    # Adzuna. Needs a résumé to derive the queries; self-skips otherwise.
    if filters.google.enabled and resume is not None:
        whats = derive_keywords(resume, profile or ApplicationProfile(), filters)[
            : max(1, filters.google.max_queries)
        ]
        sources.append(GoogleJobsSource(
            whats=whats,
            location=(profile.location if profile else "") or "",
            is_remote=filters.remote_only,
            max_days_old=filters.max_posting_age_days or 0,
            results_wanted=filters.google.results_wanted,
        ))
    # Keyless remote aggregators (opt-in). Himalayas needs no query; RemoteOK filters by single-word
    # skill tags derived from the profile (multi-word role titles aren't valid RemoteOK tags).
    if filters.remote_boards.himalayas:
        sources.append(HimalayasSource(max_results=filters.remote_boards.max_results))
    if filters.remote_boards.remoteok:
        tags = []
        if resume is not None:
            tags = [t.lower() for t in derive_keywords(resume, profile or ApplicationProfile(), filters)
                    if " " not in t][:4]
        sources.append(RemoteOKSource(tags=tags, max_results=filters.remote_boards.max_results))
    # Early-career curated feeds (needs the résumé to rank listings by title-relevance).
    if filters.early_career.enabled and resume is not None:
        sources.append(CuratedListSource(
            resume, kinds=tuple(filters.early_career.kinds or ["new-grad", "intern"]),
            max_resolve=filters.early_career.max_resolve,
            feeds={f.name: f.url for f in filters.early_career.feeds},
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
    # Run the top profile-derived terms as SEPARATE focused queries (breadth + relevance) rather
    # than one broad blob; recency is pushed server-side via max_days_old when the staleness gate
    # is set, so Adzuna returns fresh postings instead of us dropping stale ones after the fact.
    whats = derive_keywords(resume, profile or ApplicationProfile(), filters)[: max(1, cfg.max_queries)]
    where = (profile.location if profile else "") or ""
    return AdzunaSource(
        app_id, app_key, whats=whats, where=where,
        country=cfg.country, max_pages=cfg.max_pages, salary_min=filters.min_salary,
        max_days_old=filters.max_posting_age_days or 0, sort_by=cfg.sort_by,
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


def _posting_datetime(raw) -> Optional[datetime]:
    """Best-effort parse of a posting timestamp for the staleness gate: ISO-8601-ish strings
    (trailing 'Z' or offset ok) and epoch-milliseconds ints/digit-strings (> 10^11 — Lever's
    createdAt). Returns None when missing/unparseable (the gate then keeps the posting)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        n = float(raw)
        if n > 1e11:  # milliseconds since epoch
            try:
                return datetime.fromtimestamp(n / 1000, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_TITLE_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_title(title: str) -> str:
    """Normalize a title for dedup: lowercase, collapse every run of non-alphanumerics to a
    single space, and trim. So 'Python Developer' == 'python  developer' == 'Python-Developer'."""
    return _TITLE_NORM_RE.sub(" ", (title or "").lower()).strip()


def apply_gates(postings, filters: DiscoveryFilters, stats: Optional[dict] = None):
    """Cheap pre-matcher gates from the filter config: remote_only, title_exclude, a salary
    floor when the posting states pay, an experience-level gate (by title), and — when
    max_posting_age_days is set — a staleness gate on updated_at. Returns the kept postings.
    Postings with no stated salary, no detectable level, or a missing/unparseable
    updated_at pass their gate (we don't drop for missing data).

    When `stats` is given, it's populated with a per-gate drop count
    (`gate_remote`/`gate_title`/`gate_company`/`gate_level`/`gate_salary`/`gate_stale`/
    `gate_duplicate`) so the funnel diagnostic can show which gate dropped how many — not just
    the collapsed total. A final dedup pass collapses near-identical reposts (same company +
    title, e.g. a staffing agency posting one role many times) to the first seen."""
    excl = [t.lower() for t in filters.title_exclude]
    co_excl = [c.lower() for c in filters.company_exclude]
    want_levels = {_norm_level(l) for l in filters.experience_levels} & set(EXPERIENCE_LEVELS)
    now = datetime.now(timezone.utc)
    drops = {"gate_remote": 0, "gate_title": 0, "gate_company": 0, "gate_level": 0,
             "gate_salary": 0, "gate_stale": 0, "gate_duplicate": 0}
    kept = []
    seen_key: set[tuple[str, str]] = set()  # (company, title) already kept — collapse reposts
    for p in postings:
        if filters.remote_only and p.remote is False:
            drops["gate_remote"] += 1
            continue
        if excl and any(x in p.title.lower() for x in excl):
            drops["gate_title"] += 1
            continue
        if co_excl and any(x in (p.company or "").lower() for x in co_excl):
            drops["gate_company"] += 1
            continue
        if want_levels:
            # Lenient: drop only when the title clearly names a level and none is wanted;
            # a title with no detectable level passes through to the matcher.
            detected = detect_levels(p.title)
            if detected and detected.isdisjoint(want_levels):
                drops["gate_level"] += 1
                continue
        if filters.min_salary:
            sal = _annual_salary(p.compensation)
            if sal is not None and sal < filters.min_salary:
                drops["gate_salary"] += 1
                continue
        if filters.max_posting_age_days is not None:
            dt = _posting_datetime(p.updated_at)
            if dt is not None and (now - dt).days > filters.max_posting_age_days:
                drops["gate_stale"] += 1
                continue
        # Collapse near-identical reposts: same company + normalized title. Staffing agencies
        # post one role many times, which otherwise eats resolve + Claude-judge slots. Keyed on
        # normalized text (not URL) precisely because the dups carry DISTINCT apply URLs.
        key = ((p.company or "").strip().lower(), _norm_title(p.title))
        if key != ("", "") and key in seen_key:
            drops["gate_duplicate"] += 1
            continue
        seen_key.add(key)
        kept.append(p)
    if stats is not None:
        stats.update(drops)
    return kept

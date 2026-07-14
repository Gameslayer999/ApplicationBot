"""Deterministic multi-factor pre-score (AutoApply-AI survey #3).

A zero-token 0-100 estimate of how well a résumé fits a posting, computed from the structured
résumé + the raw JD text — no Claude call. It does NOT replace the Claude fit judge; it decides
WHICH postings the judge spends its scarce `top_n` slots on (matching.match), so a run's judging
budget goes to the most promising postings first instead of whatever a raw skill-overlap count
floats to the top. That count's known failure (NEXT_STEPS / decision 046): a verbose senior JD's
larger keyword overlap crowds early-career-fit roles out of the judged set — exactly what the
**experience** factor here corrects, cheaply, before any tokens are spent.

Adapted from AutoApply-AI's `ats/scorer.py` (skills / experience / education / keyword, weighted
.40/.30/.20/.10), simplified to the signals we can extract deterministically from raw JD text.
Each factor returns a 0–1 sub-score, or None when the JD states no requirement to score against;
the weights are renormalized over the factors actually present (same principle as
matching.weighted_fit) so a missing factor never silently drags the score to zero.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import relevance
from .models import Resume

# Faithful to the surveyed scorer's weighting.
WEIGHTS = {"skills": 0.40, "experience": 0.30, "education": 0.20, "keyword": 0.10}

# Matching this many of the candidate's skills in the JD counts as full skills coverage.
# (We can't split the JD's demands into required vs preferred from raw text, so we saturate
# the overlap count instead of dividing by an unknown denominator.)
_SKILLS_SATURATION = 6

# Degree keywords → rank (high school = 1 … PhD = 5), most-specific patterns first so
# "master of science" isn't caught by the bachelor's "b.s" alias etc.
_DEGREE_RANKS: list[tuple[tuple[str, ...], int]] = [
    (("ph.d", "phd", "doctor", "doctorate", "d.phil"), 5),
    (("master", "m.s.", "msc", "m.eng", "mba", "s.m."), 4),
    (("bachelor", "b.s.", "bsc", "b.eng", "b.a.", "undergraduate", "a.b."), 3),
    (("associate", "a.s.", "a.a."), 2),
    (("high school", "ged", "secondary school"), 1),
]

# Matches an experience bar and captures its FLOOR: "5+ years" → 5, "0-2 years" → 0,
# "8 to 10 years" → 8 (the first number of a range).
_YEARS_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|(?:-|–|to)\s*\d{1,2})?\s*(?:or more\s*)?years?", re.I)
_YEAR_TOKEN = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class ScoreBreakdown:
    """Each factor's 0–1 sub-score (None = the JD stated no requirement, so it's dropped from
    the weighted average) and the final 0–100 pre-score."""
    skills: float
    experience: Optional[float]
    education: Optional[float]
    keyword: float
    score: int


def candidate_years(resume: Resume) -> float:
    """Career length in years — the span from the earliest experience start to the latest end
    (an 'Present'/blank end = today). A cheap proxy for total experience; overlapping stints
    aren't summed, which keeps interns from looking like veterans."""
    starts: list[int] = []
    ends: list[int] = []
    this_year = date.today().year
    for e in list(resume.experience) + list(resume.activities):
        sy = _YEAR_TOKEN.search(e.start or "")
        if sy:
            starts.append(int(sy.group(0)))
        if e.end and not re.search(r"present|current|now", e.end, re.I):
            ey = _YEAR_TOKEN.search(e.end)
            if ey:
                ends.append(int(ey.group(0)))
        else:
            ends.append(this_year)  # ongoing role
    if not starts:
        return 0.0
    return max(0.0, max(ends or starts) - min(starts))


def required_years(jd_low: str) -> Optional[float]:
    """The minimum years of experience the JD asks for (the smallest '<n> years' figure, so
    'entry level, 0-2 years' reads as 0 and '5+ years' as 5), or None if unstated. Ignores
    absurd matches (> 40) that are almost always dates/quantities, not experience bars."""
    nums = [int(m.group(1)) for m in _YEARS_RE.finditer(jd_low)]
    nums = [n for n in nums if n <= 40]
    return float(min(nums)) if nums else None


def _degree_rank(text_low: str) -> Optional[int]:
    """Highest degree rank named in `text_low` (used for the candidate's max degree), or None."""
    best: Optional[int] = None
    for aliases, rank in _DEGREE_RANKS:
        if any(a in text_low for a in aliases):
            best = max(best or 0, rank)
    return best


def candidate_degree_rank(resume: Resume) -> Optional[int]:
    blob = " ".join(f"{e.degree} {e.school}".lower() for e in resume.education)
    return _degree_rank(blob)


def required_degree_rank(jd_low: str) -> Optional[int]:
    """The degree the JD requires — the LOWEST degree it names (its floor: 'Bachelor's required,
    Master's preferred' → Bachelor's), or None if no degree is mentioned."""
    ranks = [rank for aliases, rank in _DEGREE_RANKS if any(a in jd_low for a in aliases)]
    return min(ranks) if ranks else None


def _resume_tokens(resume: Resume) -> set[str]:
    parts = [resume.summary or ""]
    parts += [i for c in resume.skills for i in c.items]
    parts += [e.role for e in resume.experience]
    return relevance.tokens(" ".join(parts))


# Title tokens too generic to signal role relevance (present in most postings).
_STOP_TITLE = {"the", "and", "of", "a", "an", "for", "to", "in", "at", "with", "senior",
               "junior", "staff", "lead", "principal", "i", "ii", "iii", "sr", "jr",
               "engineer", "manager", "specialist", "analyst", "intern", "co", "op"}


def _keyword_subscore(resume: Resume, title: str) -> float:
    """How much the posting's TITLE overlaps the candidate's background — a distinct signal
    from raw skill mentions (a 'Sales Engineer' title vs a software résumé that incidentally
    shares 'engineer'). 1.0 when the title has no distinctive tokens (nothing to disqualify on)."""
    title_toks = {t for t in relevance.tokens(title) if len(t) >= 3 and t not in _STOP_TITLE}
    if not title_toks:
        return 1.0
    have = _resume_tokens(resume)
    return len(title_toks & have) / len(title_toks)


def score_breakdown(resume: Resume, title: str, jd_text: str, *,
                    matched_count: Optional[int] = None) -> ScoreBreakdown:
    """The 0–100 pre-score plus each factor, for a posting's title + JD body. `matched_count`
    (skills the JD mentions) is reused from the caller's keyword pass when available, else
    computed here."""
    jd_low = jd_text.lower()
    if matched_count is None:
        matched_count, _ = relevance.qualification_score(resume, jd_text)

    skills = min(1.0, matched_count / _SKILLS_SATURATION)

    req_years = required_years(jd_low)
    experience = None if req_years is None else (
        1.0 if req_years <= 0 else min(1.0, candidate_years(resume) / req_years))

    req_deg = required_degree_rank(jd_low)
    cand_deg = candidate_degree_rank(resume)
    education = None if req_deg is None else min(1.0, (cand_deg or 0) / req_deg)

    keyword = _keyword_subscore(resume, title)

    factors = {"skills": skills, "experience": experience,
               "education": education, "keyword": keyword}
    present = {k: v for k, v in factors.items() if v is not None}
    total_w = sum(WEIGHTS[k] for k in present)
    raw = sum(v * WEIGHTS[k] for k, v in present.items()) / total_w if total_w else 0.0
    return ScoreBreakdown(skills, experience, education, keyword,
                          max(0, min(100, round(raw * 100))))


def ats_prescore(resume: Resume, title: str, jd_text: str, *,
                 matched_count: Optional[int] = None) -> int:
    """The deterministic 0–100 pre-score for ranking the judge queue (breakdown discarded)."""
    return score_breakdown(resume, title, jd_text, matched_count=matched_count).score

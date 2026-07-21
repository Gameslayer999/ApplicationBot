"""Deterministic "dummy ATS" built from a job description (decision — see DECISIONS.md).

Extracts, with zero tokens, what a real applicant-tracking system screens a résumé on:

1. **Keywords** — the skills the JD *demands* that the candidate can *truthfully* provide
   (``relevance.skill_terms(base)`` ∩ what the JD mentions). Same honest universe
   ``ats_check.verify_pdf`` uses: we never treat a skill the candidate lacks as a required
   keyword, so the tailoring loop can chase every "missing" one without inventing anything.
   Genuine gaps — requirements the résumé never had — are the Claude fit judge's job, not
   this module's.
2. **Knockouts** — the hard auto-reject gates a JD states: a minimum years bar, a required
   degree (both evaluated against the résumé via ``ats_score``'s existing parsers), and
   security-clearance / citizenship requirements (detected and surfaced as blockers, since
   tailoring cannot satisfy them).

``grade`` scores a rendered résumé's text against this profile: a 0–100 keyword-coverage
score plus each knockout's verdict. It is what the tailoring retry loop tests against, and
it powers the ATS grade shown in the UI/tracker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import ats_score, relevance
from .models import Resume

# Phrases that signal a hard gate tailoring can't touch. Each entry: (label, message, regexes).
# We keep these deliberately conservative — a false knockout wrongly blocks a real application.
_CLEARANCE_RE = re.compile(
    r"security clearance|active\s+(?:secret|ts/sci|top secret)|ts/sci|polygraph", re.I)
_CITIZENSHIP_RE = re.compile(
    r"u\.?s\.?\s+citizen(?:ship)?|must be a citizen|citizens?\s+only|"
    r"(?:no|without|not able to (?:provide|offer)|unable to (?:provide|offer)|do(?:es)? not (?:provide|offer|sponsor))"
    r"[^.\n]{0,30}(?:visa\s+)?sponsor", re.I)


@dataclass
class Knockout:
    """A hard auto-reject gate parsed from the JD, with its verdict against the résumé."""

    label: str  # short kind: "years" | "degree" | "clearance" | "citizenship"
    passed: Optional[bool]  # True/False, or None when we can't verify it from résumé data
    message: str  # user-facing statement of the gate and the verdict (UI Principle #3)


@dataclass
class Requirements:
    """What the JD screens on — the deterministic ATS profile for one posting."""

    keywords: list[str] = field(default_factory=list)  # JD-demanded skills the candidate has
    knockouts: list[Knockout] = field(default_factory=list)


@dataclass
class AtsGrade:
    """How a rendered résumé scores against a `Requirements` profile — the dummy ATS verdict."""

    keyword_score: int  # 0–100: share of required keywords present in the résumé text
    present: list[str] = field(default_factory=list)  # required keywords found
    missing: list[str] = field(default_factory=list)  # required keywords absent (the loop's targets)
    knockouts: list[Knockout] = field(default_factory=list)

    @property
    def failed_knockouts(self) -> list[Knockout]:
        return [k for k in self.knockouts if k.passed is False]

    @property
    def passed(self) -> bool:
        """An ATS auto-rejects on any failed knockout, regardless of keyword score."""
        return not self.failed_knockouts

    def notes(self) -> list[str]:
        """User-facing summary lines (UI Principle #3: state the problem and the fix)."""
        out: list[str] = []
        for k in self.failed_knockouts:
            out.append(f"ATS knockout: {k.message}")
        if self.missing:
            out.append(
                f"ATS keyword gap: the job screens for {', '.join(self.missing)} — you have "
                "them, but they're not in this résumé. Re-tailor or increase Length to add them."
            )
        unverifiable = [k for k in self.knockouts if k.passed is None]
        for k in unverifiable:
            out.append(f"ATS check: {k.message}")
        if self.passed and not self.missing:
            out.append(
                f"ATS grade {self.keyword_score}/100: text screens clean — all {len(self.present)} "
                "required keyword(s) present, no knockouts failed."
            )
        return out


def _knockouts(resume: Resume, jd_low: str) -> list[Knockout]:
    """The hard gates the JD states, each evaluated against the résumé where we can."""
    outs: list[Knockout] = []

    req_years = ats_score.required_years(jd_low)
    if req_years is not None and req_years > 0:
        have = ats_score.candidate_years(resume)
        outs.append(Knockout(
            "years", have + 1e-9 >= req_years,
            f"the job requires {req_years:g}+ years of experience; your résumé shows "
            f"~{have:g}." + ("" if have + 1e-9 >= req_years else " A résumé edit can't add years — "
                             "this posting will likely screen you out."),
        ))

    req_deg = ats_score.required_degree_rank(jd_low)
    if req_deg is not None:
        have_deg = ats_score.candidate_degree_rank(resume)
        outs.append(Knockout(
            "degree", (have_deg or 0) >= req_deg,
            f"the job requires a {_DEGREE_NAME.get(req_deg, 'specific')} degree; your résumé "
            f"shows {_DEGREE_NAME.get(have_deg, 'none listed')}."
            + ("" if (have_deg or 0) >= req_deg else " Tailoring can't change this."),
        ))

    if _CLEARANCE_RE.search(jd_low):
        outs.append(Knockout(
            "clearance", None,
            "this posting requires an active security clearance — confirm you hold one; "
            "tailoring can't satisfy it.",
        ))
    if _CITIZENSHIP_RE.search(jd_low):
        outs.append(Knockout(
            "citizenship", None,
            "this posting states a citizenship / no-sponsorship requirement — confirm you "
            "meet it; tailoring can't satisfy it.",
        ))
    return outs


_DEGREE_NAME = {1: "high-school", 2: "associate's", 3: "bachelor's", 4: "master's", 5: "doctoral"}


def extract(resume: Resume, jd_text: str) -> Requirements:
    """Build the deterministic ATS requirement profile for `jd_text` against `resume`."""
    jd_low = jd_text.lower()
    jd_tok = relevance.tokens(jd_low)
    keywords = [t for t in relevance.skill_terms(resume) if relevance.mentions(t, jd_low, jd_tok)]
    return Requirements(keywords=keywords, knockouts=_knockouts(resume, jd_low))


def grade(resume_text: str, requirements: Requirements) -> AtsGrade:
    """Score `resume_text` (a rendered résumé) against the JD's requirement profile.

    `keyword_score` is the share of required keywords present. With no keywords demanded it
    is 100 (nothing to screen on). Knockouts carry over from `requirements` unchanged — they
    are gates on the candidate, not on this particular render.
    """
    low = resume_text.lower()
    txt_tok = relevance.tokens(low)
    present, missing = [], []
    for term in requirements.keywords:
        (present if relevance.mentions(term, low, txt_tok) else missing).append(term)
    total = len(requirements.keywords)
    score = 100 if total == 0 else round(100 * len(present) / total)
    return AtsGrade(keyword_score=score, present=present, missing=missing,
                    knockouts=requirements.knockouts)

"""Qualification matching (Stage 2): decide which discovered postings fit the user.

Hybrid, qualification-driven (DECISIONS.md #025):

1. **Keyword pre-filter (free):** rank every posting by how many of the candidate's skills
   it asks for (`relevance.qualification_score`) and drop the obvious non-matches. A
   backend résumé never wastes a Claude call on a nursing posting.
2. **Claude judge (subscription) on the survivors only:** for the top-N ranked postings,
   Claude judges true fit — accounting for seniority and semantics the keyword pass can't —
   and names any requirements the résumé is missing. Grounded strictly in the résumé; it
   judges fit, it does not invent qualifications.

This bounds the Claude cost regardless of how many postings discovery returns — the same
pre-select-then-Claude pattern the catalogue uses (DECISIONS.md #013).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from . import relevance
from .backends import _extract_json, claude_code_available, run_claude_cli
from .discovery import Posting
from .models import Resume


@dataclass
class Match:
    """A posting scored against the user's qualifications."""

    posting: Posting
    keyword_score: int
    matched_skills: list[str]
    qualified: Optional[bool] = None  # None until Claude judges it
    fit_score: Optional[int] = None  # 0-100 from Claude
    why: str = ""
    missing: list[str] = field(default_factory=list)
    judged_by: str = "keyword"  # "keyword" | "claude"

    @property
    def rank(self) -> float:
        """Sort key: a Claude fit score (0-100) dominates; unjudged postings fall back to
        their keyword score (kept below any judged posting so judged ones float up)."""
        if self.fit_score is not None:
            return 1000 + self.fit_score
        return self.keyword_score


def keyword_rank(resume: Resume, postings: list[Posting], *, min_skills: int = 1) -> list[Match]:
    """Rank postings by skill overlap; drop those matching fewer than `min_skills` skills."""
    matches: list[Match] = []
    for p in postings:
        score, matched = relevance.qualification_score(resume, f"{p.title}\n{p.body}")
        if score >= min_skills:
            matches.append(Match(posting=p, keyword_score=score, matched_skills=matched))
    # Curated early-career postings (already pre-vetted to the user's level) rank ABOVE raw board
    # postings — otherwise a verbose senior JD's larger skill overlap crowds them out of the
    # judged top-N, defeating the point of enabling early-career feeds. Within each group, rank
    # by skill overlap.
    matches.sort(key=lambda m: (bool(m.posting.extra.get("curated")), m.keyword_score), reverse=True)
    return matches


def _resume_summary(resume: Resume) -> str:
    """A compact résumé view for the judge prompt: skills + recent roles with bullets."""
    lines: list[str] = []
    if resume.summary:
        lines.append(f"Summary: {resume.summary}")
    skills = ", ".join(item for cat in resume.skills for item in cat.items)
    if skills:
        lines.append(f"Skills: {skills}")
    for exp in resume.experience[:5]:
        lines.append(f"\n{exp.role} — {exp.organization} ({exp.start}–{exp.end})")
        for b in exp.bullets[:4]:
            lines.append(f"  - {b}")
    return "\n".join(lines)


_JUDGE_INSTRUCTIONS = (
    "You are screening a job posting for a candidate. Decide whether the candidate is "
    "genuinely qualified, judging ONLY from the résumé below — do not assume skills or "
    "experience not shown. Account for seniority (an entry-level résumé is not qualified "
    "for a staff/principal role) and for hard requirements (years, degrees, specific "
    "must-have technologies). Be honest and strict: a weak match is not a match.\n\n"
    "Return ONLY a JSON object, no prose, with exactly these keys:\n"
    '{"qualified": true|false, "score": 0-100, "why": "one sentence", '
    '"missing": ["requirement the résumé lacks", ...]}\n'
    "score = how well the candidate fits (100 = ideal, 0 = unqualified). "
    "missing = requirements the posting states that the résumé does not evidence (empty if none)."
)


def judge_fit(resume: Resume, posting: Posting, *, think: bool = False, timeout: int = 120) -> dict:
    """Ask Claude (subscription) whether the candidate is qualified for one posting.
    Returns {qualified, score, why, missing}. Raises RuntimeError if the CLI fails."""
    prompt = (
        f"{_JUDGE_INSTRUCTIONS}\n\n"
        f"=== CANDIDATE RÉSUMÉ ===\n{_resume_summary(resume)}\n\n"
        f"=== JOB POSTING ===\n"
        f"{posting.title} at {posting.company} ({posting.location})\n\n"
        f"{posting.body[:6000]}"
    )
    text = run_claude_cli(prompt, think=think, timeout=timeout)
    data = json.loads(_extract_json(text))
    return {
        "qualified": bool(data.get("qualified", False)),
        "score": int(data.get("score", 0)),
        "why": str(data.get("why", "")).strip(),
        "missing": [str(x) for x in (data.get("missing") or [])],
    }


def match(
    resume: Resume,
    postings: list[Posting],
    *,
    top_n: int = 10,
    use_claude: bool = True,
    min_skills: int = 1,
    on_progress=None,
) -> tuple[list[Match], list[str]]:
    """Rank postings against the user's qualifications. Keyword-ranks all of them, then (if
    enabled and the Claude CLI is present) has Claude judge the top `top_n`. Returns
    (matches sorted best-first, errors). A Claude failure on one posting is recorded and
    leaves that posting keyword-only — it never aborts the run (Agent Guideline #11).
    `on_progress(done, total)` is called after each judged posting (for a UI progress bar)."""
    ranked = keyword_rank(resume, postings, min_skills=min_skills)
    errors: list[str] = []

    if use_claude and claude_code_available():
        survivors = ranked[:top_n]
        total = len(survivors)
        for i, m in enumerate(survivors):
            try:
                verdict = judge_fit(resume, m.posting)
                m.qualified = verdict["qualified"]
                m.fit_score = verdict["score"]
                m.why = verdict["why"]
                m.missing = verdict["missing"]
                m.judged_by = "claude"
            except Exception as e:
                errors.append(f"{m.posting.company} — {m.posting.title}: judge failed: {e}")
            if on_progress is not None:
                on_progress(i + 1, total)
    elif use_claude:
        errors.append("Claude Code CLI not found — ranked by keyword only (install `claude` to judge fit).")

    ranked.sort(key=lambda m: m.rank, reverse=True)
    return ranked, errors

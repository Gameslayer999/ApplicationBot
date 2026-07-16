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

from . import ats_score, relevance
from .backends import _extract_json, claude_code_available, run_claude_cli
from .discovery import Posting
from .models import Resume


@dataclass
class Match:
    """A posting scored against the user's qualifications."""

    posting: Posting
    keyword_score: int
    matched_skills: list[str]
    ats_score: int = 0  # 0-100 deterministic pre-score (ats_score.py) — orders the judge queue
    qualified: Optional[bool] = None  # None until Claude judges it
    fit_score: Optional[int] = None  # 0-100, computed from `dimensions` via FIT_WEIGHTS
    why: str = ""
    missing: list[str] = field(default_factory=list)
    judged_by: str = "keyword"  # "keyword" | "claude"
    # Per-dimension 0-100 scores from the judge ({skills, experience, seniority} — decision
    # 043, adapted from ai-job-search's weighted rubric). Empty on keyword-only matches and
    # on cache snapshots written before dimensions existed.
    dimensions: dict = field(default_factory=dict)

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
            # Deterministic multi-factor pre-score (decision 052): richer than the raw overlap
            # count — folds in experience/education/title fit so the judge queue isn't led by
            # verbose senior JDs. Reuses this pass's matched count (no re-scan).
            ats = ats_score.ats_prescore(resume, p.title, f"{p.title}\n{p.body}", matched_count=score)
            matches.append(Match(posting=p, keyword_score=score, matched_skills=matched, ats_score=ats))
    # Curated early-career postings (already pre-vetted to the user's level) rank ABOVE raw board
    # postings — otherwise a verbose senior JD crowds them out of the judged top-N, defeating the
    # point of enabling early-career feeds. Within each group, rank by the deterministic pre-score
    # (keyword overlap as the tiebreak).
    matches.sort(key=lambda m: (bool(m.posting.extra.get("curated")), m.ats_score, m.keyword_score),
                 reverse=True)
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


# A 0-100 JSON fit verdict is a classification task — Sonnet judges it as well as Opus.
# Pinned explicitly so the judge never silently inherits an expensive CLI default model.
JUDGE_MODEL = "sonnet"

# Judging is batched: one Claude call judges up to this many postings (résumé sent once,
# one CLI spawn) instead of one call per posting. Chunked so a single bad reply degrades
# only these postings to keyword-only, not the whole run (see DECISIONS.md #034).
JUDGE_BATCH_SIZE = 5

# The overall fit score is a weighted average of the judge's per-dimension scores,
# computed HERE, not by the model (decision 043) — the verdict is auditable ("why did it
# skip this job?") and the weights are tunable in one place once outcome calibration has
# data. Adapted from ai-job-search's rubric; their culture/career dimensions need the
# Configure preference schema, which doesn't exist yet.
FIT_WEIGHTS = {"skills": 0.45, "experience": 0.35, "seniority": 0.20}

_JUDGE_SYSTEM = (
    "You are screening job postings for a candidate. For EACH posting, decide whether the "
    "candidate is genuinely qualified, judging ONLY from the résumé provided — do not "
    "assume skills or experience not shown. Be honest and strict: a weak match is not a "
    "match. Judge each posting independently.\n\n"
    "For each posting return: index = the posting's number as given; qualified; three "
    "0-100 dimension scores — skills = how well the résumé's technologies/skills cover "
    "the posting's stated requirements (100 = every requirement evidenced), experience = "
    "how directly the work history matches the role's domain and duties, seniority = how "
    "well the candidate's level matches the role's level (an entry-level résumé scores "
    "low on a staff/principal role AND a principal résumé scores low on an intern role); "
    "why = one sentence; missing = requirements the posting states that the résumé does "
    "not evidence (empty if none)."
)

_VERDICT_PROPS = {
    "index": {"type": "integer"},
    "qualified": {"type": "boolean"},
    "skills": {"type": "integer"},
    "experience": {"type": "integer"},
    "seniority": {"type": "integer"},
    "why": {"type": "string"},
    "missing": {"type": "array", "items": {"type": "string"}},
}

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _VERDICT_PROPS,
                "required": ["index", "qualified", "skills", "experience", "seniority",
                             "why", "missing"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def weighted_fit(dimensions: dict) -> int:
    """Overall 0-100 fit from per-dimension scores via FIT_WEIGHTS (weights renormalized
    over the dimensions actually present, so a missing one can't silently zero the score)."""
    present = {k: w for k, w in FIT_WEIGHTS.items() if k in dimensions}
    if not present:
        return 0
    total = sum(present.values())
    raw = sum(dimensions[k] * w for k, w in present.items()) / total
    return max(0, min(100, round(raw)))


def _clean_verdict(data: dict) -> dict:
    dims = {k: max(0, min(100, int(data.get(k, 0)))) for k in FIT_WEIGHTS if k in data}
    return {
        "qualified": bool(data.get("qualified", False)),
        "dimensions": dims,
        # Computed here, not model-reported (decision 043). Falls back to a legacy
        # model-reported "score" only if no dimension came back at all.
        "score": weighted_fit(dims) if dims else int(data.get("score", 0)),
        "why": str(data.get("why", "")).strip(),
        "missing": [str(x) for x in (data.get("missing") or [])],
    }


def _posting_block(i: int, posting: Posting) -> str:
    return (
        f"=== POSTING {i} ===\n"
        f"{posting.title} at {posting.company} ({posting.location})\n\n"
        f"{posting.body[:6000]}"
    )


def judge_fit_batch(resume: Resume, postings: list[Posting], *, think: bool = False,
                    timeout: int = 300) -> dict[int, dict]:
    """Ask Claude (subscription) to judge a batch of postings in ONE call. Returns
    {posting index -> {qualified, score, why, missing}}; a posting the reply skipped is
    absent from the map. Raises RuntimeError if the CLI fails."""
    prompt = (
        f"=== CANDIDATE RÉSUMÉ ===\n{_resume_summary(resume)}\n\n"
        + "\n\n".join(_posting_block(i, p) for i, p in enumerate(postings))
        + f"\n\nJudge all {len(postings)} posting(s) now."
    )
    text = run_claude_cli(prompt, model=JUDGE_MODEL, think=think, timeout=timeout,
                          system=_JUDGE_SYSTEM, json_schema=_JUDGE_SCHEMA)
    data = json.loads(_extract_json(text))
    out: dict[int, dict] = {}
    for v in data.get("verdicts") or []:
        idx = int(v.get("index", -1))
        if 0 <= idx < len(postings):
            out[idx] = _clean_verdict(v)
    return out


def judge_fit(resume: Resume, posting: Posting, *, think: bool = False, timeout: int = 120) -> dict:
    """Ask Claude (subscription) whether the candidate is qualified for one posting.
    Returns {qualified, score, why, missing}. Raises RuntimeError if the CLI fails."""
    verdicts = judge_fit_batch(resume, [posting], think=think, timeout=timeout)
    if 0 not in verdicts:
        raise RuntimeError("Claude returned no verdict for the posting.")
    return verdicts[0]


def match(
    resume: Resume,
    postings: list[Posting],
    *,
    top_n: int = 10,
    use_claude: bool = True,
    min_skills: int = 1,
    on_progress=None,
    predictor=None,
) -> tuple[list[Match], list[str]]:
    """Rank postings against the user's qualifications. Keyword-ranks all of them, then (if
    enabled and the Claude CLI is present) has Claude judge the top `top_n`. Returns
    (matches sorted best-first, errors). A Claude failure on one posting is recorded and
    leaves that posting keyword-only — it never aborts the run (Agent Guideline #11).
    `on_progress(done, total)` is called after each judged posting (for a UI progress bar).

    `predictor` (a `fit_learning.Predictor`), when active, decides WHICH `top_n` postings the
    judge spends its slots on: survivors are re-ordered by predicted fit learned from past
    runs (decision 046) so the judge sees the postings most like past winners, not the ones a
    raw keyword count floats up. It never changes the final best-first ordering (that is still
    the judge's fit_score) — only which postings get judged."""
    ranked = keyword_rank(resume, postings, min_skills=min_skills)
    errors: list[str] = []

    # Steer the scarce judge slots toward postings history predicts will clear the bar. Keep
    # curated early-career feeds first (as keyword_rank does), then predicted fit, then the
    # keyword score as a tiebreak. Only reorders which top_n get judged; a no-op when the
    # predictor is inactive (thin history) or absent (keeps today's keyword ordering).
    if predictor is not None and getattr(predictor, "active", False):
        ranked.sort(
            key=lambda m: (bool(m.posting.extra.get("curated")),
                           predictor.predict(m.posting, ats_score=m.ats_score),
                           m.ats_score, m.keyword_score),
            reverse=True,
        )

    if use_claude and claude_code_available():
        survivors = ranked[:top_n]
        total = len(survivors)
        done = 0
        # Judge in chunks: one Claude call per JUDGE_BATCH_SIZE postings. A failed call (or
        # a posting the reply skipped) leaves those postings keyword-only and is recorded —
        # it never aborts the run (Agent Guideline #11).
        for start in range(0, total, JUDGE_BATCH_SIZE):
            chunk = survivors[start : start + JUDGE_BATCH_SIZE]
            call_failed = False
            try:
                verdicts = judge_fit_batch(resume, [m.posting for m in chunk])
            except Exception as e:
                call_failed = True
                for m in chunk:
                    errors.append(f"{m.posting.company} — {m.posting.title}: judge failed: {e}")
                verdicts = {}
            for i, m in enumerate(chunk):
                verdict = verdicts.get(i)
                if verdict is not None:
                    m.qualified = verdict["qualified"]
                    m.fit_score = verdict["score"]
                    m.dimensions = verdict["dimensions"]
                    m.why = verdict["why"]
                    m.missing = verdict["missing"]
                    m.judged_by = "claude"
                elif not call_failed:
                    errors.append(
                        f"{m.posting.company} — {m.posting.title}: judge returned no verdict"
                    )
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
    elif use_claude:
        errors.append("Claude Code CLI not found — ranked by keyword only (install `claude` to judge fit).")

    ranked.sort(key=lambda m: m.rank, reverse=True)
    return ranked, errors

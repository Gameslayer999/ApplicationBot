"""Auto-rank résumé projects by technical impressiveness.

The user wants the pipeline to lead with their most technically impressive projects, not
just whichever ones happen to keyword-match a job. This module makes one Claude pass over
the catalogue's projects and assigns each an `impact` score (1–5), cached back into the
base résumé YAML (`Project.impact`). Downstream:

  - the Profile UI orders projects by that score and shows it,
  - `catalogue.select_relevant` and the rules engine use it to break ties when the length
    budget can't fit every project (relevance to the job stays primary),
  - the tailoring prompt sees each project's score and favours impressive ones among
    comparably-relevant candidates.

Scoring runs on the Claude subscription (the Claude Code CLI), never the metered API —
same path as tailoring (see `backends.run_claude_cli`, DECISIONS.md #034).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .backends import CLAUDE_CLI, _extract_json, run_claude_cli
from .models import Resume

_SYSTEM = """\
You rate the TECHNICAL IMPRESSIVENESS of a candidate's résumé projects so the strongest \
work can be surfaced first. Judge only engineering depth and difficulty — NOT how well a \
project matches any particular job, and NOT writing quality.

Score each project 1–5:
- 5 — Deep, hard, or novel engineering: systems/low-level work, distributed systems, \
concurrency, novel algorithms, reverse-engineering, real scale, or a genuinely shipped/\
production system with non-trivial architecture.
- 4 — Substantial engineering with real complexity (multiple integrated components, a \
non-trivial data pipeline, a real automation with meaningful design decisions).
- 3 — A solid, complete application of moderate complexity (a full-stack app, a working \
integration) without especially deep engineering.
- 2 — A straightforward build: standard CRUD, a scripted bot, a course-style app.
- 1 — Routine or tutorial-level work.

Judge from the actual bullets — scope, scale, architecture, what was shipped — not from \
buzzwords. Prefer evidence of hard problems solved and things that really shipped. Return \
a score for EVERY project by its given index."""


class _ProjectScore(BaseModel):
    i: int = Field(description="0-based index of the project in the list you were given.")
    impact: int = Field(ge=1, le=5, description="Technical-impressiveness score, 1–5.")
    reason: str = Field(default="", description="One short clause justifying the score.")


class _Scores(BaseModel):
    scores: list[_ProjectScore] = Field(default_factory=list)


@dataclass
class RankResult:
    resume: Resume
    ranked: list[tuple[str, int, str]] = field(default_factory=list)  # (name, impact, reason)


def _projects_for_prompt(resume: Resume) -> str:
    items = [
        {"i": i, "name": p.name, "tech": p.tech or "", "bullets": p.bullets}
        for i, p in enumerate(resume.projects)
    ]
    return json.dumps(items, ensure_ascii=False)


def score_projects(resume: Resume, *, cli: str = CLAUDE_CLI, model: str | None = None) -> RankResult:
    """Score every project in `resume` by technical impressiveness and return a copy with
    `Project.impact` filled in, plus the per-project (name, impact, reason) list ordered
    most→least impressive.

    Raises the same exceptions as `run_claude_cli` (ClaudeAuthError / ClaudeRateLimitError /
    ClaudeUnavailableError) if the Claude call can't complete, and RuntimeError if it
    doesn't return a valid score set. The résumé is never mutated in place.
    """
    if not resume.projects:
        return RankResult(resume=resume.model_copy(deep=True), ranked=[])

    prompt = (
        "PROJECTS (JSON array; each has a 0-based index `i`):\n"
        f"{_projects_for_prompt(resume)}\n\n"
        "Rate every project's technical impressiveness 1–5 by its index. Respond with ONLY "
        "a single JSON object, no markdown fences."
    )
    raw = run_claude_cli(
        prompt, cli=cli, model=model, think=False, system=_SYSTEM, activity="impact",
        json_schema=_Scores.model_json_schema(),
    )
    parsed = _Scores.model_validate_json(_extract_json(raw))

    by_index = {s.i: s for s in parsed.scores if 0 <= s.i < len(resume.projects)}
    if not by_index:
        raise RuntimeError("Claude returned no usable project scores.")

    out = resume.model_copy(deep=True)
    for i, proj in enumerate(out.projects):
        s = by_index.get(i)
        if s is not None:
            proj.impact = max(1, min(5, s.impact))

    ranked = sorted(
        ((p.name, p.impact or 0, (by_index.get(i).reason if by_index.get(i) else ""))
         for i, p in enumerate(out.projects)),
        key=lambda t: (-t[1], t[0]),
    )
    return RankResult(resume=out, ranked=ranked)

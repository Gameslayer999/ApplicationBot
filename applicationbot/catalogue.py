"""Edit the résumé data (the source-of-truth "catalogue").

The base resume YAML is the single source of truth (DECISIONS.md #002). This module lets
the user grow it beyond what their uploaded resume contained — add experience/activities/
projects that weren't on the resume, or add more bullets to an existing entry — which is
the first step toward the full "catalogue" of decision #007. Tailoring then selects the
relevant subset per job.

Mutations round-trip through the validated `Resume` model, so the file always stays
schema-valid.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import relevance
from .job_description import JobDescription
from .length import LengthBudget
from .models import Resume

_HEADER = (
    "# ApplicationBot résumé data — source of truth / catalogue. Edited via the app.\n"
    "# Git-ignored (real resumes never get committed). See examples/sample_resume.yaml.\n"
)

def save_resume(path: str | Path, resume: Resume) -> None:
    """Write the resume back to YAML (schema-valid, dropping empty/None fields)."""
    data = resume.model_dump(exclude_none=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    Path(path).write_text(_HEADER + body, encoding="utf-8")


def replace_resume(path: str | Path, data: dict) -> Resume:
    """Validate a full edited résumé and save it (the editor sends the whole thing back).

    `section_order` and any other fields the editor doesn't touch round-trip through
    unchanged as long as the caller includes them.
    """
    resume = Resume.model_validate(data)
    save_resume(path, resume)
    return resume


def select_relevant(resume: Resume, jd: JobDescription, budget: LengthBudget) -> Resume:
    """Narrow a large catalogue to its relevant slice before sending it to Claude.

    Token efficiency (DECISIONS.md #013): the catalogue can grow much larger than one
    resume, but Claude only needs the job-relevant part. This keeps ~2x the budget's worth
    of the most job-relevant entries per section — enough for Claude to still choose and
    reword within the budget — so the prompt stays small and fast regardless of catalogue
    size. If the catalogue is already within that bound, it's returned unchanged (best
    quality, still cheap). Skills, education, summary, and contact are always kept in full
    (they're small).
    """
    keep_exp = max(1, budget.max_experience * 2)
    keep_proj = max(1, budget.max_projects * 2)
    keep_act = max(1, budget.max_activities * 2)
    if (
        len(resume.experience) <= keep_exp
        and len(resume.projects) <= keep_proj
        and len(resume.activities) <= keep_act
    ):
        return resume

    jd_lower = jd.body.lower()
    jd_tokens = relevance.tokens(jd.body)
    terms = relevance.skill_terms(resume)

    def top(entries, k, textfn, tiebreak=lambda e: 0):
        if len(entries) <= k:
            return list(entries)
        # Relevance is primary; `tiebreak` (e.g. project impact) decides among equally-
        # relevant entries; original order is the final tiebreak. All negated → higher wins.
        scored = sorted(
            enumerate(entries),
            key=lambda it: (-relevance.text_score(textfn(it[1]), terms, jd_lower, jd_tokens),
                            tiebreak(it[1]), it[0]),
        )
        keep = sorted(i for i, _ in scored[:k])  # preserve original order among kept
        return [entries[i] for i in keep]

    trimmed = resume.model_copy(deep=True)
    trimmed.experience = top(resume.experience, keep_exp, lambda e: " ".join([e.role, e.organization, *e.bullets]))
    trimmed.projects = top(resume.projects, keep_proj, lambda p: " ".join([p.name, p.tech or "", *p.bullets]),
                           tiebreak=lambda p: -(p.impact or 0))
    trimmed.activities = top(resume.activities, keep_act, lambda a: " ".join([a.role, a.organization, *a.bullets]))
    return trimmed

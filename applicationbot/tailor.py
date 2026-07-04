"""Tailor a base resume to a job description.

The base resume/catalogue is the source of truth; a pluggable backend (Claude Code or the
no-LLM rules engine — see `applicationbot.backends`) selects, reorders, and rephrases from
it to match a job description.

Flow:
  1. Pre-select the relevant slice of the catalogue locally (token-efficient — keeps the
     Claude prompt small when the catalogue is large; `catalogue.select_relevant`).
  2. Tailor via the chosen backend, instructed to fit the length budget.
  3. Hard-enforce the length budget on the result.
  4. Flag any content that doesn't trace back to the FULL resume (`check_factual_drift`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import catalogue
from .backends import DEFAULT_QUALITY, TailorBackend, select_backend
from .job_description import JobDescription
from .length import LengthBudget
from .models import Resume, TailoredResume


@dataclass
class TailorResult:
    tailored: TailoredResume
    backend: str
    pages: float
    warnings: list[str] = field(default_factory=list)


def tailor_resume(
    resume: Resume,
    jd: JobDescription,
    backend: str | TailorBackend = "auto",
    budget: Optional[LengthBudget] = None,
    quality: str = DEFAULT_QUALITY,
) -> TailorResult:
    """Tailor `resume` to `jd` and return the validated result + factual-drift warnings.

    `backend` is a name ("auto"|"claude-code"|"rules") or a backend instance.
    `budget` controls the target length (default: one page).
    `quality` (fast|balanced|max) is the Claude speed/quality tier; ignored when `backend`
    is a pre-built instance or the rules engine.
    """
    budget = budget or LengthBudget()
    engine = select_backend(backend, quality) if isinstance(backend, str) else backend

    subset = catalogue.select_relevant(resume, jd, budget)
    tailored = engine.tailor(subset, jd, budget)

    # Hard-enforce the length budget, then tell the user if it dropped any entries — otherwise
    # a newly-added experience that didn't make the cut looks like it was silently ignored.
    before = (len(tailored.experience), len(tailored.projects), len(tailored.activities))
    tailored = budget.enforce(tailored)
    after = (len(tailored.experience), len(tailored.projects), len(tailored.activities))
    names = (("experience entry", "experience entries"), ("project", "projects"), ("activity", "activities"))
    omitted = [
        f"{b - a} {singular if b - a == 1 else plural}"
        for (singular, plural), b, a in zip(names, before, after)
        if b > a
    ]
    if omitted:
        tailored.relevance_notes = [
            *tailored.relevance_notes,
            f"Omitted {', '.join(omitted)} to fit {budget.pages:g} page(s) — "
            "the least job-relevant were dropped. Increase Length to include more.",
        ]

    return TailorResult(
        tailored=tailored,
        backend=engine.name,
        pages=budget.pages,
        warnings=check_factual_drift(resume, tailored),
    )


def check_factual_drift(base: Resume, tailored: TailoredResume) -> list[str]:
    """Flag tailored content that doesn't trace back to the base resume.

    A defense-in-depth check on top of the backend — surfaces (does not fix) any drift so
    a human can review before the resume is ever used.
    """
    warnings: list[str] = []

    base_skills = {item.strip().lower() for cat in base.skills for item in cat.items}
    for cat in tailored.skills:
        for item in cat.items:
            if item.strip().lower() not in base_skills:
                warnings.append(f"Skill not in base resume: {item!r}")

    base_roles = {
        (e.organization.strip().lower(), e.role.strip().lower())
        for e in (*base.experience, *base.activities)
    }
    for exp in (*tailored.experience, *tailored.activities):
        key = (exp.organization.strip().lower(), exp.role.strip().lower())
        if key not in base_roles:
            warnings.append(
                f"Experience/activity not in base resume: {exp.role!r} at "
                f"{exp.organization!r}"
            )

    base_certs = {c.strip().lower() for c in base.certifications}
    for cert in tailored.certifications:
        if cert.strip().lower() not in base_certs:
            warnings.append(f"Certification not in base resume: {cert!r}")

    return warnings

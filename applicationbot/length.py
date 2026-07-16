"""Configurable length budget for tailored resumes.

`pages` is the single customizable knob (default 1.0). From it we derive concrete caps —
max entries per section and max bullets per entry — that are BOTH:
  1. instructed to the Claude engine (so it self-limits while tailoring), and
  2. hard-enforced on the result afterward (so the budget holds regardless of engine).

The caps come from a rough single-column-page capacity; tune the constants if your
template fits more or less.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import TailoredResume

# Rough capacity of ONE single-column resume page.
_EXPERIENCE_PER_PAGE = 3
_PROJECTS_PER_PAGE = 3
_ACTIVITIES_PER_PAGE = 2
_BULLETS_PER_ENTRY = 4


@dataclass(frozen=True)
class LengthBudget:
    pages: float = 1.0
    line_chars: int = 103  # ~characters that fit on one line at your resume/ATS width (34pt margins)

    @property
    def max_experience(self) -> int:
        # Experience is the backbone — give it one extra slot beyond the per-page rate.
        return max(1, round(_EXPERIENCE_PER_PAGE * self.pages) + 1)

    @property
    def max_projects(self) -> int:
        return max(0, round(_PROJECTS_PER_PAGE * self.pages))

    @property
    def max_activities(self) -> int:
        return max(0, round(_ACTIVITIES_PER_PAGE * self.pages))

    @property
    def max_bullets_per_entry(self) -> int:
        return _BULLETS_PER_ENTRY if self.pages <= 1 else _BULLETS_PER_ENTRY + 1

    def prompt(self) -> str:
        """Instruction appended to the tailoring prompt."""
        one = self.line_chars
        multi = round(one * 1.5)
        return (
            f"Length budget: the tailored resume must fit on about {self.pages:g} page(s). "
            f"Include at most {self.max_experience} experience entries, {self.max_projects} "
            f"projects, and {self.max_activities} leadership/activity entries — the most "
            f"relevant ones — with at most {self.max_bullets_per_entry} bullets each. Drop "
            "the least-relevant entries and bullets to fit; keep education. Prefer fewer, "
            "stronger bullets over filling every slot.\n"
            f"Bullet width: one line holds about {one} characters. Aim for one-line bullets "
            f"close to (but not over) {one} characters — fill the line, don't leave it half "
            f"empty. NEVER produce a bullet between {one + 1} and {multi} characters (the "
            f"awkward slightly-over-one-line zone). Only exceed {multi} characters when the "
            "bullet genuinely fills 1.5+ lines."
        )

    def enforce(self, tailored: TailoredResume) -> TailoredResume:
        """Hard-cap the tailored resume to the budget (entries already ordered by relevance)."""
        n = self.max_bullets_per_entry
        tailored.experience = [_cap(e, n) for e in tailored.experience[: self.max_experience]]
        tailored.projects = [_cap(p, n) for p in tailored.projects[: self.max_projects]]
        tailored.activities = [_cap(a, n) for a in tailored.activities[: self.max_activities]]
        return tailored


def _cap(entry, n: int):
    entry.bullets = entry.bullets[:n]
    return entry

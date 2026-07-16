"""Data models shared across ApplicationBot.

The base resume is the *source of truth* (loaded from YAML). The tailored resume is
produced by the LLM by selecting, reordering, and rephrasing from the base resume — it
never introduces new facts (see DECISIONS.md #002). Both reuse the same building blocks
so the renderer can treat them uniformly.

The schema mirrors the structure of a real single-column resume — categorized skills, a
separate leadership/activities section, projects with a tech-stack line, and an explicit
section order — so a tailored resume can preserve the source resume's format (see
DECISIONS.md #005).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# Canonical section keys used by `Resume.section_order` and the renderer.
SECTION_KEYS = (
    "summary",
    "education",
    "experience",
    "projects",
    "activities",
    "skills",
    "certifications",
)


class SkillCategory(BaseModel):
    category: str = Field(description="e.g. 'Languages', 'Tools and Platforms'")
    items: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    """A job or a leadership/activity entry (same shape for both)."""

    organization: str
    role: str
    location: Optional[str] = None
    start: str = Field(description="Start date, e.g. 'May 2024'")
    end: str = Field(description="End date or 'Present'")
    bullets: list[str] = Field(default_factory=list)
    tailor_note: Optional[str] = Field(
        default=None,
        description="TAILORED RESUME ONLY: one short sentence on why this entry was kept, "
        "where it was ordered, and how it was tailored for this job. Shown to the user for "
        "review — never printed on the resume. Leave null on the base resume.",
    )


class Project(BaseModel):
    name: str
    tech: Optional[str] = Field(
        default=None, description="Tech-stack line, e.g. 'Retool, JavaScript, SQL'"
    )
    link: Optional[str] = Field(
        default=None,
        description="Optional URL for the project (repo, demo, or write-up). Grounds answers "
        "to questions like 'a personal project you're proud of?' — never fabricated.",
    )
    impact: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="Technical-impressiveness score, 1 (routine) – 5 (very impressive), "
        "auto-scored by Claude (see impact.py). Orders projects in the Profile UI and, when "
        "the résumé's length budget can't fit every project, breaks ties in favour of the "
        "more impressive ones (relevance to the job stays the primary signal). Null = unscored.",
    )
    bullets: list[str] = Field(default_factory=list)
    tailor_note: Optional[str] = Field(
        default=None,
        description="TAILORED RESUME ONLY: one short sentence on why this project was kept "
        "and how it was tailored for this job. Shown to the user for review — never printed "
        "on the resume. Leave null on the base resume.",
    )


class Education(BaseModel):
    school: str
    degree: str
    location: Optional[str] = None
    graduation: Optional[str] = None
    details: list[str] = Field(
        default_factory=list, description="e.g. relevant coursework, honors"
    )


class Contact(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None
    links: list[str] = Field(default_factory=list)


class Resume(BaseModel):
    """The base resume — the candidate's full, true history (source of truth)."""

    contact: Contact
    summary: Optional[str] = None
    skills: list[SkillCategory] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    activities: list[Experience] = Field(
        default_factory=list, description="Leadership and activities."
    )
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    section_order: Optional[list[str]] = Field(
        default=None,
        description="Order to render sections in, using SECTION_KEYS. Preserves this "
        "resume's layout. Defaults to a sensible order if omitted.",
    )


class TailoredResume(BaseModel):
    """The LLM's tailored version of the resume for one job description.

    Contains only the content the renderer needs — contact details and section order are
    carried over unchanged from the base resume (they define the candidate's format), so
    they are intentionally absent here.
    """

    summary: Optional[str] = Field(
        default=None,
        description="A concise summary rewritten to match the job, using only facts from "
        "the base resume. Include ONLY if the base resume has a summary; otherwise null.",
    )
    skills: list[SkillCategory] = Field(
        description="Skills grouped by the SAME categories as the base resume, with items "
        "reordered by relevance. Every item MUST appear in the base resume — do not add "
        "skills the candidate lacks."
    )
    experience: list[Experience] = Field(
        description="Selected and reordered experience. organization, role, and dates MUST "
        "match the base resume exactly. Bullets may be reworded/reordered/omitted to "
        "emphasize relevance, but must not invent achievements."
    )
    projects: list[Project] = Field(
        default_factory=list,
        description="Selected projects relevant to this job (may be empty). Name and tech "
        "must match the base resume.",
    )
    activities: list[Experience] = Field(
        default_factory=list,
        description="Selected leadership/activity entries, carried over from the base "
        "resume (organization, role, dates must match).",
    )
    education: list[Education] = Field(
        default_factory=list,
        description="Education, carried over from the base resume.",
    )
    certifications: list[str] = Field(
        default_factory=list,
        description="Relevant certifications, a subset of the base resume's.",
    )
    relevance_notes: list[str] = Field(
        default_factory=list,
        description="Short notes on what was emphasized/omitted and why — for the user, "
        "not part of the rendered resume.",
    )

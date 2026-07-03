"""The application-answer profile — the data auto-apply needs beyond the résumé.

Application forms ask for things a résumé doesn't carry: work authorization, sponsorship,
EEO self-identification, salary expectation, start date, links, and a long tail of custom
screening questions. For the autonomous runner (decision 016) to fill forms without a human
in the loop, it needs these answers up front, plus a growing **answer bank** of
question→answer pairs it has already resolved (so it never re-asks and rarely gets stuck).

Stored at `profile/application_profile.yaml` (git-ignored — it's PII). Tri-state booleans
use None = "unspecified / prefer not to say".
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

DEFAULT_PATH = "profile/application_profile.yaml"

_HEADER = (
    "# ApplicationBot apply profile — answers used to auto-fill application forms.\n"
    "# Git-ignored (PII). Edit in the web UI's 'Apply profile' tab.\n"
)


class QA(BaseModel):
    question: str
    answer: str


class ApplicationProfile(BaseModel):
    # Identity / contact
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""

    # Work eligibility (tri-state: None = unspecified)
    work_authorized: Optional[bool] = None
    requires_sponsorship: Optional[bool] = None

    # Logistics / preferences
    willing_to_relocate: Optional[bool] = None
    open_to_remote: Optional[bool] = None
    desired_salary: str = ""
    earliest_start_date: str = ""
    years_experience: str = ""

    # Voluntary EEO self-identification (blank = decline to self-identify)
    gender: str = ""
    race_ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""

    # Growing bank of answers to custom screening questions.
    custom_answers: list[QA] = Field(default_factory=list)


def load_profile(path: str | Path = DEFAULT_PATH) -> ApplicationProfile:
    p = Path(path)
    if not p.exists():
        return ApplicationProfile()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ApplicationProfile.model_validate(data)


def save_profile(profile: ApplicationProfile, path: str | Path = DEFAULT_PATH) -> None:
    body = yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True)
    Path(path).write_text(_HEADER + body, encoding="utf-8")


def replace_profile(data: dict, path: str | Path = DEFAULT_PATH) -> ApplicationProfile:
    """Validate an edited profile and save it."""
    profile = ApplicationProfile.model_validate(data)
    save_profile(profile, path)
    return profile

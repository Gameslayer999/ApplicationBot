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
    seen_count: int = 0  # how many times autofill hit this question and couldn't answer it —
    #                      ranks the "needs your answer" list so the most-common gaps come first
    input_kind: str = ""  # the form control it was captured from: text | textarea | select |
    #                       dropdown | radio | checkbox — so the UI recreates the right input
    options: list[str] = Field(default_factory=list)  # selectable options (for select/radio/checkbox),
    #                                                    so the UI offers the exact choices the form had
    generated: bool = False  # answer drafted by Claude (flag for review); False = user-entered
    maps_to: str = ""  # if set, answer this question LIVE from a structured profile field
    #                    (a Claude-classified semantic match, e.g. a novel "willing to work from
    #                    our office 3 days?" phrasing → "open_to_remote"). Keeps answers correct
    #                    if the profile changes, and records how the question was interpreted.


class ApplicationProfile(BaseModel):
    # Identity / contact
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    country: str = "United States"
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""

    # Work eligibility (tri-state: None = unspecified)
    work_authorized: Optional[bool] = None
    requires_sponsorship: Optional[bool] = None
    us_citizen: Optional[bool] = None  # set once — citizenship is a fact only you can assert

    # Logistics / preferences
    willing_to_relocate: Optional[bool] = None
    open_to_remote: Optional[bool] = None
    # Ranked office-location preferences (most-preferred first, e.g. ["New York, NY", "Remote"]).
    # An office-choice dropdown gets filled with the highest-ranked option the form actually offers.
    preferred_locations: list[str] = Field(default_factory=list)
    desired_salary: str = ""
    earliest_start_date: str = ""
    years_experience: str = ""

    # "How did you hear about this job?" — we discover roles via online search, so this is the
    # default answer: used verbatim in a text field, or matched to a dropdown's options.
    how_heard: str = "I found this role through an online job search."

    # Voluntary EEO self-identification (blank = decline to self-identify)
    gender: str = ""
    pronouns: str = ""  # explicit; else the resolver derives He/Him / She/Her from gender
    race_ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""

    # ATS-native autofill credentials (git-ignored with the rest of the profile). When set,
    # the Apply stage logs into the ATS's own candidate account and uses its autofill first,
    # falling back to our field-by-field autofill (decision 017).
    greenhouse_email: str = ""
    greenhouse_password: str = ""

    # Growing bank of answers to custom screening questions.
    custom_answers: list[QA] = Field(default_factory=list)

    # Learned dropdown option mappings: normalized answer value -> the option TEXT(s) it matched
    # on real forms (e.g. "rutgers university" -> ["Rutgers University-New Brunswick"], a verbose
    # degree -> ["Bachelor's Degree"]). Grown automatically as autofill resolves dropdowns via
    # Claude, so repeat encounters match instantly without another Claude call (decision 033).
    dropdown_aliases: dict[str, list[str]] = Field(default_factory=dict)


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
    """Validate an edited profile and save it. Preserve the server-managed learning store
    (`dropdown_aliases`) that the UI editor doesn't send, so saving the Profile tab doesn't
    wipe dropdown mappings learned during autofill (decision 033)."""
    if "dropdown_aliases" not in data:
        try:
            data = {**data, "dropdown_aliases": load_profile(path).dropdown_aliases}
        except Exception:
            pass
    profile = ApplicationProfile.model_validate(data)
    save_profile(profile, path)
    return profile


def resume_with_profile_links(resume, profile: ApplicationProfile):
    """Return the résumé with its contact links filled from the apply profile's LinkedIn / GitHub /
    portfolio URLs when the résumé itself carries none — so the tailored résumé/PDF shows the
    applicant's links. They live once in the apply profile (Applicant details); the résumé header's
    own Links field is separate and often left empty, which is why LinkedIn was missing from the
    rendered résumé. No-op when the résumé already has links or the profile has no URLs."""
    if resume.contact.links:
        return resume
    links = [u.strip() for u in (profile.linkedin_url, profile.github_url, profile.portfolio_url)
             if u and u.strip()]
    if not links:
        return resume
    enriched = resume.model_copy(deep=True)
    enriched.contact.links = links
    return enriched


def _norm_q(q: str) -> str:
    """Normalize a question for dedup: lowercase, drop punctuation, collapse whitespace, and
    strip common polite lead-ins so near-duplicate phrasings collapse to one bank entry
    ("Please describe your experience with X." ≈ "Describe your experience with X?")."""
    import re
    s = re.sub(r"[^a-z0-9 ]", " ", (q or "").lower())
    s = re.sub(r"^\s*(please|kindly|briefly|so|and|also)\s+", "", s)
    return " ".join(s.split())


def remember_dropdown_aliases(new: dict[str, list[str]], path: str | Path = DEFAULT_PATH) -> int:
    """Merge newly-learned dropdown option mappings (normalized value -> matched option text)
    into the on-disk store so future autofill matches the same value instantly. Reloads first,
    dedupes option strings per value. Returns how many new (value, option) pairs were added."""
    if not new:
        return 0
    profile = load_profile(path)
    added = 0
    for value, options in new.items():
        key = " ".join((value or "").lower().split())
        if not key:
            continue
        have = profile.dropdown_aliases.setdefault(key, [])
        for opt in options:
            if opt and opt not in have:
                have.append(opt)
                added += 1
    if added:
        save_profile(profile, path)
    return added


def remember_answers(new: list[QA], path: str | Path = DEFAULT_PATH) -> int:
    """Append newly-learned Q&A to the on-disk answer bank so future autofill reuses them.
    Reloads from disk first (the run may have started from an in-memory copy), skips questions
    already banked (case/space-insensitive) and blank answers. A `maps_to` mapping is only
    persisted if `answer_bank.valid_mapping` allows it — a banked mapping overrides the
    structured rules forever after, so an invalid one must be refused at write time, not
    repaired later (the polluted-answer-bank incident). Returns how many were added."""
    from . import answer_bank  # lazy: keep profile I/O importable without the bank machinery

    profile = load_profile(path)
    have = {_norm_q(qa.question) for qa in profile.custom_answers}
    added = 0
    for qa in new:
        key = _norm_q(qa.question)
        maps_to = getattr(qa, "maps_to", "")
        if maps_to and not answer_bank.valid_mapping(qa.question, maps_to):
            qa = qa.model_copy(update={"maps_to": ""})  # keep any answer text, drop the mapping
            maps_to = ""
        # Keep entries that carry either a written answer OR a structured mapping (maps_to);
        # a mapped entry answers live from the profile, so its `answer` is intentionally blank.
        has_content = bool((qa.answer or "").strip()) or bool(maps_to)
        if not key or len(key) < 4 or not has_content or key in have:
            continue
        profile.custom_answers.append(qa)
        have.add(key)
        added += 1
    if added:
        save_profile(profile, path)
    return added


def capture_questions(questions: list[str], path: str | Path = DEFAULT_PATH,
                      meta: dict | None = None) -> int:
    """Add new (reusable) questions we couldn't answer to the bank as blank entries, so the user
    fills each once in the UI and future autofill reuses it. A question we've seen before but still
    can't answer has its `seen_count` bumped (this ranks the "needs your answer" list). `meta` maps
    a question to its captured control {kind, options}, so the UI recreates the real input (a
    dropdown question stays a dropdown). Answered entries are left alone. Returns NEW questions added."""
    meta = meta or {}
    profile = load_profile(path)
    by_key = {_norm_q(qa.question): qa for qa in profile.custom_answers}
    added = 0
    touched = False
    for q in questions:
        key = _norm_q(q)
        if not key or len(key) < 4:  # garbage capture ("yes", stray tokens) — never bank it
            continue
        m = meta.get(q) or {}
        kind, options = (m.get("kind") or ""), list(m.get("options") or [])
        existing = by_key.get(key)
        if existing is None:
            profile.custom_answers.append(
                QA(question=q, answer="", seen_count=1, input_kind=kind, options=options))
            by_key[key] = profile.custom_answers[-1]
            added += 1
            touched = True
        elif not (existing.answer or "").strip() and not (getattr(existing, "maps_to", "") or ""):
            existing.seen_count = (existing.seen_count or 0) + 1  # still unanswered — count the hit
            if kind and not existing.input_kind:      # backfill control info if we now have it
                existing.input_kind = kind
            if options and not existing.options:
                existing.options = options
            touched = True
    if touched:
        save_profile(profile, path)
    return added

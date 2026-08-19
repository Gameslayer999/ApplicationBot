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


class Language(BaseModel):
    """A spoken/written language the applicant knows. `proficiency` uses the wording application
    forms offer (Native, Fluent, Professional, Conversational, Basic) so it matches their
    dropdown options directly; "" = stated language, unstated level."""
    name: str
    proficiency: str = ""


class ApplicationProfile(BaseModel):
    # Identity / contact
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    # The two address-block parts `location` ("Edison, NJ") cannot supply. Portals that split the
    # address into Address / City / State / ZIP mark all four REQUIRED — both Jobvite and BambooHR
    # do — so without these the fill stalls on a field no rule can derive (decision 168). City and
    # state ARE derived from `location`; only these two are genuinely new user data.
    street_address: str = ""
    postal_code: str = ""
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
    open_to_remote: Optional[bool] = None  # WILLINGNESS (yes/no) — are you open to remote at all.
    # PREFERRED work arrangement, distinct from the yes/no willingness above. Drives how
    # arrangement/preference questions and office-location dropdowns are answered so the bot
    # doesn't signal remote for a job whose office you'd rather commute to. One of:
    #   "" = no preference (legacy behaviour: answer purely from open_to_remote)
    #   "in_office_if_commutable" = prefer on-site when the posting has an office within
    #                               max_commute_miles of home (Claude-judged from the JD), else remote
    #   "hybrid"     = prefer hybrid
    #   "in_office"  = always prefer on-site
    #   "remote"     = always prefer remote
    work_arrangement: str = ""
    # Home→office commute radius (miles) Claude uses to judge whether a posting's office is
    # commutable for "in_office_if_commutable". None = let Claude use a reasonable daily-commute bar.
    max_commute_miles: Optional[int] = None
    # Ranked office-location preferences (most-preferred first, e.g. ["New York, NY", "Remote"]).
    # An office-choice dropdown gets filled with the highest-ranked option the form actually offers.
    preferred_locations: list[str] = Field(default_factory=list)
    desired_salary: str = ""
    earliest_start_date: str = ""
    years_experience: str = ""

    # Spoken/written languages, most proficient first (decision 158). Nothing on the résumé
    # carries these, so "Language Skill(s) (Check all that apply)" checkbox groups and
    # per-language proficiency questions were captured blank on every form that asked.
    languages: list[Language] = Field(default_factory=list)

    # "How did you hear about this job?" — we discover roles via online search, so this is the
    # default answer: used verbatim in a text field, or matched to a dropdown's options.
    how_heard: str = "I found this role through an online job search."

    # Voluntary EEO self-identification (blank = decline to self-identify)
    gender: str = ""
    pronouns: str = ""  # explicit; else the resolver derives He/Him / She/Her from gender
    race_ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""

    # MyGreenhouse Quick Apply (decision 017, reworked by 172). Greenhouse replaced its password
    # sign-in with an emailed security code, so there is no password to store any more: signing in
    # means reading the code out of the linked inbox. Our own resolver already fills a Greenhouse
    # form 15/15 without an account, so this is OPT-IN — off unless the user turns it on.
    greenhouse_quick_apply: bool = False
    greenhouse_email: str = ""  # the MyGreenhouse account address (must be the linked inbox)

    # Growing bank of answers to custom screening questions.
    custom_answers: list[QA] = Field(default_factory=list)

    # Learned dropdown option mappings: normalized answer value -> the option TEXT(s) it matched
    # on real forms (e.g. "rutgers university" -> ["Rutgers University-New Brunswick"], a verbose
    # degree -> ["Bachelor's Degree"]). Grown automatically as autofill resolves dropdowns via
    # Claude, so repeat encounters match instantly without another Claude call (decision 033).
    dropdown_aliases: dict[str, list[str]] = Field(default_factory=dict)


# ------------------------------------------------ MyGreenhouse Quick Apply (decision 182)

_GH_SERVICE = "applicationbot-greenhouse"
_GH_ACCOUNT = "mygreenhouse"  # decision 060's keychain slot — now only ever deleted, never read


def _gh_keyring():
    import keyring  # lazy: only imported when the keychain is actually touched

    return keyring


def greenhouse_quick_apply_problem(profile: "ApplicationProfile", *, config=None) -> str:
    """'' when MyGreenhouse Quick Apply can actually run; otherwise the exact reason it can't,
    naming the fix (UI principle #3). Every failure mode is a setup gap the user can close:

      • the feature is off (the default — our resolver fills Greenhouse forms without it);
      • no MyGreenhouse email;
      • no linked inbox, so nothing can read the emailed security code;
      • the MyGreenhouse address isn't the linked inbox, so the code lands where we can't see it.

    `config` (a mailbox.MailboxConfig or None) is injectable for tests; by default the linked
    inbox is looked up."""
    if not profile.greenhouse_quick_apply:
        return ("MyGreenhouse Quick Apply is off — turn it on under Profile → Native autofill "
                "logins. (Off is fine: the form still gets filled by ApplicationBot itself.)")
    email = (profile.greenhouse_email or "").strip()
    if not email:
        return ("MyGreenhouse Quick Apply is on but has no account email — add your MyGreenhouse "
                "address under Profile → Native autofill logins.")
    if config is None:
        from . import mailbox

        config = mailbox.load_config()
    if config is None:
        return ("MyGreenhouse Quick Apply needs a linked inbox to read the security code Greenhouse "
                "emails at sign-in — link one under Settings → Linked inbox, or turn Quick Apply off.")
    if email.lower() != (config.email or "").strip().lower():
        return (f"MyGreenhouse Quick Apply can't read its security code: the code goes to {email}, "
                f"but the linked inbox is {config.email}. Set the MyGreenhouse email to "
                f"{config.email} under Profile → Native autofill logins, or link {email} instead "
                f"under Settings → Linked inbox.")
    return ""


def _drop_dead_greenhouse_password(path: str | Path) -> None:
    """Delete the MyGreenhouse password decision 060 stored — from the keychain and from any
    legacy plaintext still in the YAML.

    Greenhouse no longer accepts a password at all (decision 182), so the stored one cannot sign in
    anywhere; keeping a live secret that buys nothing is exactly what Guideline #12 is about. Runs
    once per process (the keychain read is not free) and is best-effort — a keychain that refuses
    is not worth failing a profile load over."""
    global _GH_PW_CLEARED
    if _GH_PW_CLEARED:
        return
    _GH_PW_CLEARED = True
    try:
        _gh_keyring().delete_password(_GH_SERVICE, _GH_ACCOUNT)
    except Exception:
        pass
    try:
        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if "greenhouse_password" in data:
            data.pop("greenhouse_password")
            p.write_text(_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                         encoding="utf-8")
    except Exception:
        pass


_GH_PW_CLEARED = False


def load_profile(path: str | Path | None = None) -> ApplicationProfile:
    # `path or DEFAULT_PATH` (not a default argument) so the location is read at CALL time —
    # an import-time default would bind the original path and ignore a redirected DEFAULT_PATH.
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return ApplicationProfile()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    profile = ApplicationProfile.model_validate(data)  # a legacy `greenhouse_password:` key is ignored
    _drop_dead_greenhouse_password(p)  # the password Greenhouse no longer accepts (decision 182)
    return profile


def save_profile(profile: ApplicationProfile, path: str | Path | None = None) -> None:
    data = profile.model_dump()
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    Path(path or DEFAULT_PATH).write_text(_HEADER + body, encoding="utf-8")


def replace_profile(data: dict, path: str | Path | None = None) -> ApplicationProfile:
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
        # A context-dependent label ("Date", "Other", "If yes…") is a word, not a question:
        # banked, it would answer a DIFFERENT field on the next form (decision 167).
        if not key or len(key) < 4 or not has_content or key in have \
                or answer_bank.is_context_dependent(qa.question):
            continue
        profile.custom_answers.append(qa)
        have.add(key)
        added += 1
    if added:
        save_profile(profile, path)
    return added


def upsert_answers(pairs: dict[str, str], path: str | Path | None = None,
                   meta: dict | None = None) -> int:
    """Write USER-authored answers into the bank, replacing any existing entry for the same
    question (matched case/space-insensitively). Used by the review panel (decision 155): an
    answer the user typed while reviewing an application is ground truth, so it overwrites a
    blank entry `capture_questions` banked, a Claude draft, and any `maps_to` mapping whose
    live answer the user just replaced — re-deriving it would reproduce the answer they
    rejected. Blank values are ignored (clearing a review edit drops that posting's override
    only; it does not erase what the bank already learned). Returns entries written.

    Unlike `remember_answers` (which appends what a RUN learned and never touches an existing
    entry), this deliberately overwrites — the caller is the user, not the bot.

    `meta` maps a question to the control it was answered in ({kind, options}, from the fill
    report), so a question first answered in the review panel still shows in the profile editor
    as its real control — a check-all-that-apply group as checkboxes, not a text box.
    """
    meta = meta or {}
    profile = load_profile(path or DEFAULT_PATH)
    by_key = {_norm_q(qa.question): qa for qa in profile.custom_answers}
    written = 0
    for question, answer in (pairs or {}).items():
        question = str(question).strip()
        answer = str(answer if answer is not None else "").strip()
        key = _norm_q(question)
        if not (key and answer) or len(key) < 4:
            continue
        m = meta.get(question) or {}
        kind, options = (m.get("kind") or ""), list(m.get("options") or [])
        qa = by_key.get(key)
        if qa is None:
            qa = QA(question=question, answer=answer, input_kind=kind, options=options)
            profile.custom_answers.append(qa)
            by_key[key] = qa
        elif qa.answer == answer and not qa.maps_to and not qa.generated \
                and (qa.input_kind or not kind):
            continue  # already banked exactly this — nothing to write
        else:
            qa.answer, qa.maps_to, qa.generated = answer, "", False
            if kind and not qa.input_kind:      # backfill control info if we now have it
                qa.input_kind = kind
            if options and not qa.options:
                qa.options = options
        written += 1
    if written:
        save_profile(profile, path or DEFAULT_PATH)
    return written


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

"""Apply stage — a per-ATS form-filling adapter (Greenhouse first).

Split into two layers:
  * AnswerResolver — pure, fully testable: given a form field's label, returns the value
    from the résumé contact + apply profile + saved answer bank (or None = "can't answer",
    a logged exception rather than a blocking prompt — decision 016).
  * run_apply — a thin, defensive Playwright driver. DRY-RUN by default: it fills the
    form, uploads the PDF, screenshots it, and PAUSES for review — it clicks submit ONLY
    when passed an armed SafetyGate (profile/safety.yaml + no KILL file, decision 035)
    and every REQUIRED field resolved (Guideline #3: never submit against a real posting
    in development).

The Playwright browser (Chromium) is a separate one-time install: `playwright install
chromium`. The resolver needs no browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import answer_bank, salary
from .apply_profile import QA, ApplicationProfile, load_profile
from .models import Resume


# --------------------------------------------------------------------------- report


@dataclass
class FilledField:
    label: str
    value: str
    control: str = "text"  # text | select | combobox | radio | file
    source: str = "resolver"  # resolver | native (ATS autofill) | generated (Claude draft) |
    #                           option:<tier> (combobox: literal/learned/hint/claude/substring —
    #                           how the option matched, the determinism audit trail)


@dataclass
class ApplyReport:
    url: str
    ats: str = "greenhouse"
    filled: list[FilledField] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # fields we couldn't answer
    captured: dict = field(default_factory=dict)  # question -> {kind, options}, so the UI can
    #                                               recreate an unanswered field as its real control
    errors: list[str] = field(default_factory=list)
    screenshot: Optional[str] = None
    submitted: bool = False
    # Armed-run outcome (decision 035): dry-run (never attempted) | blocked (armed but
    # withheld — see `blockers`) | submitted (confirmation seen) | unconfirmed (form gone
    # after the click but no explicit confirmation — treat as submitted, verify manually).
    submit_state: str = "dry-run"
    blockers: list[str] = field(default_factory=list)
    confirmation: str = ""  # the evidence a submission went through (url / page text)
    pages: int = 1  # form pages walked (1 = single-page form)
    submit_probe: str = ""  # dry-run only: the submit control we WOULD click (live selector check)
    native_autofill: Optional[str] = None  # which native autofill ran (e.g. "greenhouse: MyGreenhouse")

    def summary(self) -> str:
        native = sum(1 for f in self.filled if f.source == "native")
        generated = sum(1 for f in self.filled if f.source == "generated")
        state = f" ({self.submit_state})" if self.submit_state != "dry-run" else ""
        lines = [f"Apply report — {self.ats} — {self.url}", f"  submitted: {self.submitted}{state}"]
        if self.confirmation:
            lines.append(f"  confirmation: {self.confirmation}")
        if self.blockers:
            lines.append(f"  blocked: {'; '.join(self.blockers)}")
        if self.pages > 1:
            lines.append(f"  form pages walked: {self.pages}")
        if self.submit_probe:
            lines.append(f"  submit control (not clicked): {self.submit_probe}")
        if self.native_autofill:
            lines.append(f"  native autofill: {self.native_autofill} — {native} field(s) prefilled")
        breakdown = f"{native} native, {generated} AI-drafted, {len(self.filled) - native - generated} banked/direct"
        lines.append(f"  filled ({len(self.filled)}; {breakdown}):")
        lines += [f"    - {f.label}: {f.value!r} [{f.control}·{f.source}]" for f in self.filled]
        if self.skipped:
            lines.append(f"  needs attention ({len(self.skipped)}):")
            lines += [f"    - {s}" for s in self.skipped]
        if self.errors:
            lines.append(f"  errors ({len(self.errors)}):")
            lines += [f"    - {e}" for e in self.errors]
        if self.screenshot:
            lines.append(f"  screenshot: {self.screenshot}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- resolver


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _has(n: str, *terms: str) -> bool:
    return any(t in n for t in terms)


def _yn(b: Optional[bool]) -> Optional[str]:
    return None if b is None else ("Yes" if b else "No")


def _link(links: list[str], host: str) -> Optional[str]:
    return next((l for l in links if host in l.lower()), None)


# Words that mark a "work in <X>" reference as VAGUE (not a concrete country we can adjudicate) —
# e.g. "the location(s) you selected", "the country in which you are applying", "this role". When
# the place is vague we fall back to the applicant's general work-auth/sponsorship flag.
_VAGUE_PLACE = (
    "location", "country", "countries", "office", "region", "area", "role", "position",
    "selected", "this", "these", "those", "your", "our", "above", "below", "previous",
    "listed", "applying", "applied", "jurisdiction", "place", "where", "here", "any",
)


def _degree_hints(degree_text: str) -> Optional[list[str]]:
    """Map a verbose résumé degree ("Bachelor of Science in Computer Science, …") to the
    standard option texts a degree dropdown uses, most-specific first, so the combobox/select
    can match a level even though the résumé string never appears verbatim."""
    d = (degree_text or "").lower()
    if not d:
        return None
    if any(t in d for t in ("ph.d", "phd", "doctor of philosophy", "doctorate", "doctoral")):
        return ["Doctor of Philosophy (Ph.D.)", "Doctorate", "Ph.D.", "PhD", "Doctoral Degree"]
    if "juris doctor" in d or "j.d" in d:
        return ["Juris Doctor (J.D.)", "J.D.", "Law Degree"]
    if "doctor of medicine" in d or "m.d" in d:
        return ["Doctor of Medicine (M.D.)", "M.D."]
    if "mba" in d or "m.b.a" in d or "business administration" in d:
        return ["Master of Business Administration (M.B.A.)", "MBA", "Master's Degree"]
    if any(t in d for t in ("master", "m.s.", "m.a.", "msc", "m.eng", "graduate degree")):
        return ["Master's Degree", "Master's", "Masters", "Master", "Graduate Degree"]
    if any(t in d for t in ("bachelor", "b.s.", "b.a.", "bsc", "b.eng", "undergrad", "baccalaureate")):
        return ["Bachelor's Degree", "Bachelor's", "Bachelors", "Bachelor", "Undergraduate Degree"]
    if "associate" in d or "a.a." in d or "a.s." in d:
        return ["Associate's Degree", "Associate's", "Associate"]
    if any(t in d for t in ("high school", "secondary school", "diploma", "ged")):
        return ["High School", "High School Diploma", "Secondary School", "GED"]
    return None


@dataclass
class PendingDecisions:
    """Unresolved decisions collected during round 1 of a page fill (the deterministic pass).
    Between rounds they are adjudicated by at most 3 BATCHED Claude calls (classify,
    bank-match, dropdown picks) instead of one CLI spawn per field; round 2 is the same
    deterministic loop, now resolving from the injected results."""
    questions: dict = field(default_factory=dict)  # label -> {"kind": str, "options": [str]}
    picks: dict = field(default_factory=dict)      # label -> (value, [option texts shown])

    def defer_question(self, label: str) -> None:
        self.questions.setdefault(label, {"kind": "", "options": []})

    def enrich(self, label: str, kind: str, options: Optional[list] = None) -> None:
        """Attach the control's kind/options to a deferred question — a classified dropdown
        whose answer doesn't literally match its options can then join the pick batch."""
        q = self.questions.get(label)
        if q is not None:
            q["kind"] = q["kind"] or kind
            q["options"] = q["options"] or [o for o in (options or []) if o]

    def defer_pick(self, label: str, value: str, options: list) -> None:
        self.picks.setdefault(label, (value, [o for o in options if o]))

    def has(self, label: str) -> bool:
        return label in self.questions or label in self.picks

    def __bool__(self) -> bool:
        return bool(self.questions or self.picks)


@dataclass
class AnswerResolver:
    resume: Resume
    profile: ApplicationProfile
    # Claude drafting of open-ended questions (decision 018), off unless enabled by the caller.
    enable_generation: bool = False
    company: Optional[str] = None
    jd: Optional[str] = None
    pay: Optional[str] = None  # the posting's advertised compensation string, if any
    market_salary: Optional[str] = None  # dynamic estimate used when no band is advertised (decision 039)
    model: Optional[str] = None
    learned: list = field(default_factory=list)  # generated Q&A to persist after the run
    learned_options: dict = field(default_factory=dict)  # value -> [option texts] learned this run
    # Two-pass batching state (decision 041). `pending` is set only during round 1 of a page
    # fill: semantic/pick decisions are DEFERRED into it instead of spawning Claude per field.
    pending: Optional[PendingDecisions] = None
    semantic_done: set = field(default_factory=set)  # labels the batch already adjudicated
    picks_done: set = field(default_factory=set)     # dropdown labels the batch already adjudicated
    decided_options: dict = field(default_factory=dict)  # label -> batch-picked option text

    def learned_option_hints(self, value: Optional[str]) -> list[str]:
        """Dropdown options this value has matched before (learned across runs), so a repeat
        encounter matches instantly without another Claude call (decision 033)."""
        if not value:
            return []
        key = " ".join(value.lower().split())
        return list(self.profile.dropdown_aliases.get(key, [])) + list(self.learned_options.get(key, []))

    def learn_option(self, value: Optional[str], chosen: str) -> None:
        """Record that `value` matched dropdown option `chosen` (persisted after the run).
        Generic boolean values are NEVER learned: aliases are keyed by value alone, so a
        "yes" → "I am authorized to work … for any employer" mapping learned on one question
        would become a match candidate for EVERY future Yes/No dropdown. Descriptive boolean
        dropdowns are covered per-question by option_hints instead."""
        if not (value and chosen):
            return
        key = " ".join(value.lower().split())
        if key in ("yes", "no", "true", "false"):
            return
        opts = self.learned_options.setdefault(key, [])
        if chosen not in opts:
            opts.append(chosen)

    def _name_parts(self) -> tuple[str, str]:
        parts = (self.resume.contact.name or "").split()
        return (parts[0] if parts else ""), (parts[-1] if len(parts) > 1 else "")

    def _current_experience(self):
        """The applicant's CURRENT role — the ongoing one (end says Present/Current), else the
        first listed. Résumés aren't always ordered most-recent-first, so experience[0] can be a
        past job; this picks the actually-current one for "current employer / title" questions."""
        exps = self.resume.experience
        if not exps:
            return None
        for e in exps:
            if re.search(r"\b(present|current|now|ongoing|to date)\b", e.end or "", re.I):
                return e
        return exps[0]

    def _field_of_study(self) -> Optional[str]:
        """The academic field/major/discipline for a 'Discipline'/'Field of study' field. The
        résumé stores it inside the degree string ("Bachelor of Science in Computer Science,
        Minor in …"), with no separate field, so parse the phrase after "in" up to the first comma."""
        edu = self.resume.education[0] if self.resume.education else None
        if not edu:
            return None
        m = re.search(r"\bin\s+(.+)", edu.degree or "", re.I)
        return (m.group(1).split(",")[0].strip() or None) if m else None

    def _salary_expectation(self) -> Optional[str]:
        """Salary-expectation answer, most-grounded source first:
          1. the posting's advertised pay band → its midpoint (decision 038);
          2. else the pre-computed market estimate for this posting (decision 039), injected
             by the pipeline when the posting advertises nothing;
          3. else the profile's stored desired_salary."""
        band = salary.advertised_band(self.pay, self.jd)
        if band:
            return str((band[0] + band[1]) // 2)
        return self.market_salary or self.profile.desired_salary or None

    def _place_matches_applicant(self, place: str) -> bool:
        """True if `place` (a country/region named in a Yes/No "are you located in X?" question)
        is where the applicant actually is — compared against their country and location, with the
        US spelled its many ways and state abbreviations expanded (NJ → new jersey)."""
        place = re.sub(r"^the\b", "", place.strip().lower()).strip(" .?,")
        if not place:
            return False
        US = {"united states", "united states of america", "usa", "us", "u s", "u s a", "america"}
        mine: set = set()
        country = (self.profile.country or "").strip().lower()
        if country:
            mine.add(country)
            if country in US:
                mine |= US
        loc = (self.profile.location or self.resume.contact.location or "").lower()
        for tok in re.split(r"[,\s]+", loc):
            if tok:
                mine.add(_US_STATES.get(tok, tok))
        return any(place == m or (len(place) > 2 and place in m) or (len(m) > 2 and m in place)
                   for m in mine)

    def _authorized_countries(self) -> set:
        """Countries the applicant can work in without sponsorship — their own country (spelled the
        US's many ways when applicable). No per-country data model today; the home country is the
        honest default. A dual-national with extra authorizations is a documented follow-up."""
        US = {"united states", "united states of america", "usa", "us", "u s", "u s a", "america"}
        out: set = set()
        c = (self.profile.country or "").strip().lower()
        if c:
            out.add(c)
            if c in US:
                out |= US
        return out

    def _named_foreign_country(self, n: str) -> Optional[str]:
        """If the question names a CONCRETE country the applicant is NOT authorized in (e.g.
        "…work in Japan…" for a US applicant), return that place; None if there's no "work in <X>"
        clause, the place is vague ("the location(s) you selected"), or it IS their own country.
        This is the only case where the generic work-auth/sponsorship flag gives a wrong answer."""
        m = re.search(r"\b(?:work|employed|employment|permit)\s+in\s+(.+?)(?:\s+for\b|$)", n)
        if not m:
            return None
        place = m.group(1).strip()
        if any(w in place for w in _VAGUE_PLACE):
            return None
        p = re.sub(r"^the\b", "", place).strip(" .?,")
        if not p:
            return None
        auth = self._authorized_countries()
        if any(a == p or (len(p) > 2 and p in a) or (len(a) > 2 and a in p) for a in auth):
            return None  # their own/authorized country — the generic flag (authorized) is correct
        return p

    def _state_from_location(self) -> Optional[str]:
        """The US state (full name) from the applicant's location, for a "state/province" field.
        "Edison, NJ" → "New Jersey"; "San Francisco, California" → "California"."""
        loc = (self.profile.location or self.resume.contact.location or "").strip()
        for tok in re.split(r"[,\s]+", loc):
            t = tok.strip().lower()
            if t in _US_STATES:
                return _US_STATES[t].title()
            if t in _US_STATES.values():
                return t.title()
        low = loc.lower()  # multi-word states ("new jersey") don't survive the token split
        for full in _US_STATES.values():
            if full in low:
                return full.title()
        return None

    def _office_prefs(self) -> list[str]:
        """Ranked office-location candidates for a "preferred office location" dropdown: the explicit
        preferred_locations first, then Remote (if open to it), then the home city as a last resort."""
        out = [x.strip() for x in (self.profile.preferred_locations or []) if x and x.strip()]
        if self.profile.open_to_remote and not any("remote" in x.lower() for x in out):
            out.append("Remote")
        home = (self.profile.location or self.resume.contact.location or "").strip()
        if home and home not in out:
            out.append(home)
        return out

    def _office_hints(self) -> Optional[list[str]]:
        """The ranked office prefs, each expanded with its city-only form ("New York, NY" → also
        "New York"), so the combobox matches whether a form's option is the bare city or has a
        suffix ("New York (HQ)"). Order = rank, so the highest-ranked offered option wins."""
        hints: list[str] = []
        for p in self._office_prefs():
            hints.append(p)
            city = p.split(",")[0].strip()
            if city and city.lower() != p.lower():
                hints.append(city)
        seen, out = set(), []
        for h in hints:
            if h.lower() not in seen:
                seen.add(h.lower())
                out.append(h)
        return out or None

    def _pronouns(self) -> Optional[str]:
        """The explicit pronouns field if set; otherwise derived from the stored gender, for a
        "preferred pronouns" field. None for an unset/non-binary gender — never guess pronouns."""
        if (self.profile.pronouns or "").strip():
            return self.profile.pronouns.strip()
        g = (self.profile.gender or "").strip().lower()
        if g in ("male", "man", "m"):
            return "He/Him"
        if g in ("female", "woman", "f"):
            return "She/Her"
        return None

    def resolve(self, label: str) -> Optional[str]:
        """Return the answer for a field labelled `label`, or None if we can't answer it."""
        n = _norm(label)
        if not n:
            return None
        c = self.resume.contact
        p = self.profile
        first, last = self._name_parts()

        # Identity / contact
        if _has(n, "first name") or n in ("first", "given name"):
            return p.first_name or first or None
        if _has(n, "last name", "family name", "surname"):
            return p.last_name or last or None
        if _has(n, "full name") or n in ("name", "your name", "legal name"):
            return c.name or None
        # "What's the name you'd prefer us to use?" / "preferred name" → the first name.
        if _has(n, "preferred name", "preferred first name", "what should we call you",
                "name you go by", "goes by") or ("prefer" in n and "name" in n) \
                or ("name" in n and _has(n, "like us to use", "want us to use", "call you")):
            return p.first_name or first or None
        if _has(n, "email"):
            return p.email or c.email or None
        if _has(n, "phone", "mobile", "telephone"):
            return p.phone or c.phone or None
        if _has(n, "linkedin"):
            return p.linkedin_url or _link(c.links, "linkedin")
        if _has(n, "github"):
            return p.github_url or _link(c.links, "github")
        if _has(n, "portfolio", "website", "personal site", "personal website"):
            return p.portfolio_url or None

        # Work eligibility (Yes/No) — checked BEFORE location/country, because questions like
        # "Are you authorized to work in the location(s) you selected?" or "…sponsor you for the
        # location(s)…" contain "location"/"country" and must NOT be answered with a place.
        if _has(n, "authorized to work", "legally authorized", "work authorization",
                "eligible to work", "entitled to work", "right to work"):
            # A US applicant's generic work-auth flag says "authorized" — but that's WRONG for a
            # posting that asks about a specific foreign country ("…authorized to work in Japan?").
            # Override to No only when a concrete non-home country is named; else use the flag.
            if p.work_authorized is True and self._named_foreign_country(n):
                return "No"
            return _yn(p.work_authorized)
        if _has(n, "sponsor", "sponsorship", "require sponsorship", "visa sponsorship", "need sponsorship"):
            # Symmetric: someone who needs no sponsorship at home WOULD need it for a foreign
            # country ("…require sponsorship to work in Japan?" → Yes), so override in that case.
            if p.requires_sponsorship is False and self._named_foreign_country(n):
                return "Yes"
            return _yn(p.requires_sponsorship)
        # ITAR / export-control gates — a U.S. citizen is a "U.S. person" under ITAR, so the
        # gate is met. Checked BEFORE the citizen rule so the long blurb form ("applicant must
        # be a (i) U.S. citizen or national, (ii) …green card holder…") resolves as ITAR. A
        # non-citizen falls THROUGH (not return None) so a banked answer can still apply —
        # a green-card holder also qualifies, which the profile can't derive. NB: "itar" must
        # be a whole word — as a substring it hits "mil-ITAR-y status".
        if (re.search(r"\bitar\b", n)
                or _has(n, "export control", "export regulation", "us person", "u s person")) \
                and p.us_citizen is True:
            return "Yes"
        # Security-clearance ELIGIBILITY (citizens are eligible to apply) — distinct from
        # HAVING a clearance, which stays captured for the user (pinned null in the corpus).
        if "clearance" in n and _has(n, "eligible", "eligibility", "ability to obtain",
                                     "able to obtain", "can you obtain", "willing to obtain") \
                and p.us_citizen is True:
            return "Yes"
        # Citizenship — also answers "confirm you are a US citizen located in the US" gates,
        # since those are Yes/No and citizenship is the binding requirement.
        if _has(n, "citizen"):
            return _yn(p.us_citizen)
        if _has(n, "relocate", "willing to relocate"):
            return _yn(p.willing_to_relocate)
        if _has(n, "remote", "work remotely"):
            return _yn(p.open_to_remote)

        # Prior relationship with the HIRING company (worked/interned/consulted/interviewed there).
        # Honest default for a fresh applicant is "No". Gated on the company actually being named,
        # so "have you worked with <technology>?" is never caught (and product-use like "have you
        # used Robinhood?" isn't, since "used" isn't an employment/interview verb).
        if self.company:
            cn = _norm(self.company)
            if cn and cn in n and _has(n, "worked", "work for", "employed", "employee",
                                       "employment", "intern", "consult", "interviewed", "contractor"):
                return "No"

        # "Are you currently located in <place>?" / "Do you live in <place>?" — a Yes/No, answered
        # by comparing the named place to where the applicant IS (country + location); NOT a place
        # to enter. Before the location/country rules so it isn't answered with the applicant's own
        # city/country (e.g. "located in Japan?" was wrongly answered "United States").
        mloc = re.search(r"\b(?:located|based|residing|reside|living|live)\s+in\s+(.+)$", n)
        if mloc and re.match(r"(are|do|does|will|is|have|currently)\b", n):
            return "Yes" if self._place_matches_applicant(mloc.group(1)) else "No"

        # "Which state/province do you live in?" — answered from the location's state, not the
        # full "City, ST". Guarded so the verb "state" ("please state your salary") never triggers
        # it. Before the location rule so a state field isn't answered with the whole city.
        if ("province" in n or re.search(r"\bstate\b", n)) and _has(
                n, "province", "reside", "residence", "live", "located", "which state",
                "home state", "current state", "state of", "your state"):
            st = self._state_from_location()
            if st:
                return st

        # "What is your preferred office location?" — pick the highest-ranked office the form offers
        # from the applicant's ranked preferences. NOT a Yes/No like "willing to work from the
        # office" (guarded), and distinct from the home-location rule below (which excludes "office").
        if _has(n, "office") and _has(n, "preferred", "which", "select", "location", "prefer",
                                      "choose", "primary") \
                and not _has(n, "willing", "days per week", "days a week", "commute", "able to"):
            prefs = self._office_prefs()
            return prefs[0] if prefs else None

        # Location / country — after work-eligibility so a Yes/No question that merely mentions
        # "location"/"country" isn't answered with a place. "Country" is checked first so a
        # "country where you reside" question resolves to the country, not the city.
        if _has(n, "country"):
            return p.country or None
        # NOTE: match "city" only as a whole word — as a bare substring it hits "ethni-CITY"
        # (and "simpli-city"), which wrongly answered "Race/Ethnicity" with the applicant's city.
        # NB: exclude "office" — "preferred office location" asks which COMPANY office you'd work
        # from (from the job's office list), not where you live; answering it with the home city is
        # wrong, so leave it for the user.
        if (_has(n, "location", "current location", "where are you based", "reside", "residence",
                 "where do you live", "where you live", "based out of")
                or re.search(r"\b(city|live)\b", n)) and not _has(n, "office"):
            return p.location or c.location or None

        # Logistics
        if _has(n, "salary", "compensation expectation", "desired pay", "expected compensation"):
            # No advertised band, no market estimate, no stored figure → fall THROUGH (don't
            # return None) so a user-entered banked answer below can still answer it. A live
            # AppLovin dry-run hit this: the early return skipped the bank and the field fell
            # to the drafting path, which fabricated a salary.
            sal = self._salary_expectation()
            if sal is not None:
                return sal
        if _has(n, "start date", "available to start", "notice period", "when can you start",
                "earliest", "start working", "want to start", "like to start", "when would you start",
                "when could you start", "availability to start", "available start"):
            return p.earliest_start_date or None
        if _has(n, "years of experience", "years experience"):
            return p.years_experience or None
        # ADA: "Can you perform the essential functions of this role, with or without reasonable
        # accommodation?" — answered Yes (the applicant can do the job); a standard required Yes/No.
        if _has(n, "essential functions", "perform the essential", "essential function",
                "perform the duties of", "perform the job duties"):
            return "Yes"
        # Readiness/commitment closers — "Are you up for it?", "Are you ready?", "Does this
        # sound like you?" — ask for a commitment to the described role, not a fact; applying
        # IS that commitment, so answer Yes. Guarded so logistical "ready" questions (start
        # date, relocation, remote/onsite, travel) still resolve from their profile rules
        # above or are captured for the user, never blanket-answered Yes.
        if _has(n, "are you up for", "up for it", "up for the challenge", "up to the challenge",
                "ready for the challenge", "ready to take on", "sound like you",
                "are you ready") \
                and not _has(n, "start", "relocate", "remote", "onsite", "on site", "travel",
                             "commute", "when"):
            return "Yes"

        # Voluntary EEO
        # Pronouns before gender: "What gender pronouns do you prefer?" contains "gender" but wants
        # pronouns ("He/Him"), not "Male".
        if _has(n, "pronoun"):
            return self._pronouns()
        if _has(n, "gender"):
            return p.gender or None
        # "Are you Hispanic/Latino?" is a Yes/No derived from the stored race/ethnicity, which
        # may not contain the word "race"/"ethnicity" — handle it before the generic check.
        if _has(n, "hispanic", "latino", "latinx", "latin"):
            if not p.race_ethnicity:
                return None
            rl = p.race_ethnicity.lower()
            is_hisp = any(t in rl for t in ("hispanic", "latino", "latinx", "latin")) \
                and "not hispanic" not in rl and "non-hispanic" not in rl
            return "Yes" if is_hisp else "No"
        if _has(n, "race", "ethnicity", "ethnic"):
            return p.race_ethnicity or None
        if _has(n, "veteran", "military"):
            return p.veteran_status or None
        if _has(n, "disability"):
            return p.disability_status or None

        # Facts derivable from the résumé — answer them instead of capturing them blank for
        # the user (they were showing up as "needs your answer" despite being on the résumé).
        recent = self._current_experience()
        edu = self.resume.education[0] if self.resume.education else None
        if recent and _has(n, "current employer", "previous employer", "current company",
                           "recent employer", "name of your employer", "current or previous employer") \
                and not _has(n, "subject to", "agreement", "restriction", "non-compete", "noncompete",
                             "obligation", "worked for", "worked at"):
            return recent.organization or None
        if recent and _has(n, "current title", "job title", "current position", "recent title",
                           "current or previous job title", "your title", "current role"):
            return recent.role or None
        if edu and _has(n, "most recent degree", "highest degree", "degree obtained",
                        "degree earned", "level of education", "education level") or (edu and n == "degree"):
            return edu.degree or None
        if edu and _has(n, "school", "university", "college", "institution", "alma mater"):
            return edu.school or None
        if edu and _has(n, "field of study", "major", "area of study", "course of study",
                        "discipline", "concentration"):
            return self._field_of_study() or edu.degree or None
        if edu and _has(n, "graduation", "grad year", "graduated", "year of graduation",
                        "expected graduation"):
            return edu.graduation or None

        # "How did you hear about this job?" — default reflects our online-search discovery.
        if _has(n, "how did you hear", "how did you find", "where did you hear",
                "referral source", "how were you referred"):
            return p.how_heard or None

        # AI-assistance disclosure — this pipeline uses AI to complete the form, so answer
        # truthfully "Yes" (the user authorizes answering it to keep runs autonomous).
        if "ai" in n.split() and _has(n, "application", "form") and \
                _has(n, "complete", "fill", "assist", "generate", "used", "use", "help"):
            return "Yes"

        # Saved answer bank for custom screening questions (conservative match). An entry with
        # `maps_to` was Claude-classified onto a structured field — answer it LIVE from that
        # field so it stays correct if the profile changes.
        for qa in p.custom_answers:
            qn = _norm(qa.question)
            if qn and (qn == n or (len(qn) > 15 and (qn in n or n in qn))):
                mt = getattr(qa, "maps_to", "")
                return self.answer_for_type(mt) if mt else (qa.answer or None)

        return None

    def answer_for_type(self, key: str) -> Optional[str]:
        """The live answer for a classified question type (a key of
        answer_bank.CLASSIFIABLE_TYPES), read from the current profile/résumé."""
        p, c = self.profile, self.resume.contact
        return {
            "work_authorized": _yn(p.work_authorized),
            "requires_sponsorship": _yn(p.requires_sponsorship),
            "us_citizen": _yn(p.us_citizen),
            "willing_to_relocate": _yn(p.willing_to_relocate),
            "open_to_remote": _yn(p.open_to_remote),
            "desired_salary": self._salary_expectation(),
            "earliest_start_date": p.earliest_start_date or None,
            "years_experience": p.years_experience or None,
            "itar_us_person": "Yes" if p.us_citizen is True else None,
            "role_commitment": "Yes",
            "how_heard": p.how_heard or None,
            "location": p.location or c.location or None,
            "country": p.country or None,
        }.get(key)

    def resolve_semantic(self, label: str) -> Optional[str]:
        """Keyword-resolve first; on a miss, use Claude to CLASSIFY the question onto a known
        structured type (catching semantic variants the keyword rules miss, e.g. "willing to
        work from our office 3 days/week?" → open_to_remote), answer from that field, and cache
        the mapping so it's learned. Best-effort: no Claude / no match → None (caller falls back
        to the drafting/needs-attention path)."""
        value = self.resolve(label)
        if value is not None:
            return value
        if not self.enable_generation:
            return None
        if self.pending is not None:  # round 1 of a two-pass fill — defer to the batch
            if label not in self.semantic_done:
                self.pending.defer_question(label)
            return None
        if label in self.semantic_done:
            return None  # the batch already adjudicated this label — never re-ask per-field
        key = answer_bank.classify_question(label, model=self.model)
        if key:
            ans = self.answer_for_type(key)
            if ans is not None:
                self.learned.append(QA(question=label, answer="", maps_to=key, generated=True))
                return ans
        # Not a structured type (or its profile field is unset) — the question may still be a
        # REPHRASING of a custom question already answered in the bank.
        return self._bank_semantic(label)

    def _bank_semantic(self, label: str) -> Optional[str]:
        """On a literal bank miss, Claude-match `label` against the banked Q&A — a saved answer
        should be reused for any rewording of its question, not only the exact phrasing it was
        saved under. On a hit, the answer is returned and the new phrasing is cached as a bank
        alias so the next encounter matches literally (no Claude call)."""
        cands: list[tuple[QA, str]] = []
        for qa in self.profile.custom_answers:
            mt = getattr(qa, "maps_to", "")
            ans = self.answer_for_type(mt) if mt else (qa.answer or "").strip()
            if (qa.question or "").strip() and ans:
                cands.append((qa, ans))
        if not cands:
            return None
        idx = answer_bank.match_banked_question(
            label, [(qa.question, ans) for qa, ans in cands], model=self.model)
        if idx is None:
            return None
        qa, ans = cands[idx]
        self.learned.append(QA(question=label, answer="" if qa.maps_to else qa.answer,
                               maps_to=qa.maps_to, generated=True))
        return ans

    def option_hints(self, label: str) -> Optional[list[str]]:
        """Ranked substrings to match against a dropdown's options when the free-text answer
        won't match one directly. "How did you hear about this job?" is often a dropdown whose
        options vary by company — since we discover roles via online search, prefer
        online/job-board/company-site options, then a generic bucket."""
        n = _norm(label)
        # Preferred office location: the ranked office prefs (city-expanded), so the highest-ranked
        # option the form actually offers is the one that matches.
        if _has(n, "office") and _has(n, "preferred", "which", "select", "location", "prefer",
                                      "choose", "primary") \
                and not _has(n, "willing", "days per week", "days a week", "commute", "able to"):
            return self._office_hints()
        # Work-authorization dropdowns sometimes use DESCRIPTIVE options ("I am authorized to work
        # in the United States for any employer") instead of Yes/No — map from the profile. Hints
        # are specific enough not to substring-match the negative option ("for any employer" only
        # appears in the positive one; bare "authorized to work" would also hit "NOT authorized").
        if _has(n, "authorized to work", "legally authorized", "work authorization",
                "eligible to work", "entitled to work", "right to work") \
                and not self._named_foreign_country(n):
            if self.profile.work_authorized is True:
                return (["I require sponsorship to work", "require sponsorship", "Yes"]
                        if self.profile.requires_sponsorship else
                        ["authorized to work in the United States for any employer",
                         "for any employer", "Yes"])
            if self.profile.work_authorized is False:
                return ["I am not authorized to work", "not authorized to work", "No"]
        # ITAR/export-control dropdowns list the qualifying statuses — a citizen picks the
        # citizen/national option. Before the citizen hints so ITAR blurbs naming several
        # statuses get these; same hint texts, plus the "U.S. person" phrasing ITAR forms use.
        if (re.search(r"\bitar\b", n)
                or _has(n, "export control", "export regulation", "us person", "u s person")) \
                and self.profile.us_citizen is True:
            return ["U.S. citizen or national", "U.S. citizen", "US citizen", "U.S. Person",
                    "US Person", "Yes"]
        # Citizenship-status dropdowns list statuses ("(a) U.S. citizen or national…"), not Yes/No.
        if _has(n, "citizen"):
            if self.profile.us_citizen is True:
                return ["U.S. citizen or national", "U.S. citizen", "US citizen",
                        "citizen or national of the United States", "Yes"]
            if self.profile.us_citizen is False:
                return ["No"]
        if _has(n, "how did you hear", "how did you find", "where did you hear",
                "referral source", "how were you referred"):
            return ["job board", "online", "search", "company website", "website",
                    "google", "linkedin", "indeed", "glassdoor", "other"]
        # Country dropdowns spell "United States" many ways ("United States", "US", "USA", …).
        # Offer full forms first, then the abbreviations — these are now SAFE to include because
        # _matches whole-words short values, so "US" matches the option "US" but not "Australia".
        if _has(n, "country") and (self.profile.country or "").strip().lower() in (
                "united states", "usa", "us", "u.s.", "united states of america"):
            return ["United States", "United States of America", "United States (USA)",
                    "USA", "US", "U.S.A.", "America"]
        # Degree dropdowns list standard levels ("Bachelor's Degree", "Master's Degree", …) but the
        # résumé stores a verbose degree ("Bachelor of Science in Computer Science, …"). Map it to
        # the level so the dropdown can match.
        edu = self.resume.education[0] if self.resume.education else None
        if edu and (n == "degree" or _has(n, "degree", "level of education", "education level")):
            hints = _degree_hints(edu.degree)
            if hints:
                return hints
        # Gender-identity dropdowns often list "Man"/"Woman" rather than "Male"/"Female".
        g = (self.profile.gender or "").strip().lower()
        if _has(n, "pronoun"):
            pr = self._pronouns()
            if pr == "He/Him":
                return ["He/Him", "He/Him/His", "He", "Him"]
            if pr == "She/Her":
                return ["She/Her", "She/Her/Hers", "She", "Her"]
        if _has(n, "gender") and g:
            if g in ("male", "man"):
                return ["Male", "Man"]
            if g in ("female", "woman"):
                return ["Female", "Woman"]
        # State/province dropdowns: offer the full name, then the abbreviation.
        if ("province" in n or re.search(r"\bstate\b", n)) and _has(
                n, "province", "reside", "residence", "live", "located", "which state",
                "home state", "current state", "state of", "your state"):
            st = self._state_from_location()
            if st:
                abbr = next((a.upper() for a, full in _US_STATES.items() if full == st.lower()), None)
                return [st] + ([abbr] if abbr else [])
        return None

    def freetext_answer(self, label: str, is_textarea: bool = False) -> tuple[Optional[str], str]:
        """Answer a free-text field. Banked/structured answer first; else, for open-ended
        questions, a grounded Claude draft (cached for reuse unless company-specific). Returns
        (answer, source) where source is 'resolver' | 'generated' | '' (couldn't answer)."""
        value = self.resolve(label)
        if value is not None:
            return value, "resolver"
        if not self.enable_generation:
            return None, ""
        if self.pending is not None and label not in self.semantic_done:
            # Round 1 of a two-pass fill — classify/bank-match (and any drafting) wait for
            # round 2; "pending" tells the caller not to capture/skip the field yet.
            self.pending.defer_question(label)
            return None, "pending"
        # A reworded banked question beats drafting a fresh answer (and covers short text
        # fields, which never reach resolve_semantic). Skipped when the batch already checked.
        banked = None if label in self.semantic_done else self._bank_semantic(label)
        if banked is not None:
            return banked, "resolver"
        if not answer_bank.is_open_ended(label, is_textarea):
            return None, ""
        company_specific = answer_bank.is_company_specific(label)
        if company_specific and not (self.company or self.jd):
            return None, ""  # needs company context we don't have — leave it to the user
        ans = answer_bank.generate_answer(
            label, self.resume, company=self.company, jd=self.jd, model=self.model)
        if not ans:
            return None, ""
        if not company_specific:  # cache reusable answers only
            self.learned.append(QA(question=label, answer=ans, generated=True))
        return ans, "generated"


# --------------------------------------------------------------- Greenhouse (Playwright)
#
# The driver iterates EVERY field on the form rather than a fixed list: for each control it
# derives the human question label, asks the resolver for an answer, and fills it by control
# type (text / <select> / react-select combobox / radio group). Anything required it could
# not fill is reported by name so the gap is visible, never silently skipped.

# JS run in the page to derive a control's human question label (aria-label → aria-labelledby
# → <label for> → wrapping <label> → nearest ancestor label/legend → placeholder/name).
_LABEL_JS = r"""(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
  const lb = el.getAttribute('aria-labelledby');
  if (lb) {
    const t = clean(lb.split(/\s+/).map(id => {
      const n = document.getElementById(id); return n ? n.innerText : '';
    }).join(' '));
    if (t) return t;
  }
  if (el.id) {
    const sel = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
    const l = document.querySelector('label[for="' + sel + '"]');
    if (l) return clean(l.innerText);
  }
  const wrap = el.closest('label');
  if (wrap) return clean(wrap.innerText);
  let node = el;
  for (let i = 0; i < 5 && node.parentElement; i++) {
    node = node.parentElement;
    const l = node.querySelector('label, legend');
    if (l && clean(l.innerText)) return clean(l.innerText);
  }
  return clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
}"""

# JS: for a radio, the group's QUESTION (its own label is just the option, e.g. "Yes").
_GROUP_QUESTION_JS = r"""(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  const fs = el.closest('fieldset');
  if (fs) { const lg = fs.querySelector('legend'); if (lg && clean(lg.innerText)) return clean(lg.innerText); }
  let node = el;
  for (let i = 0; i < 6 && node.parentElement; i++) {
    node = node.parentElement;
    const long = Array.from(node.querySelectorAll('label, legend'))
      .map(l => clean(l.innerText)).filter(t => t.length > 12);
    if (long.length) return long.sort((a, b) => b.length - a.length)[0];
  }
  return '';
}"""

# JS: every required field's label text (Greenhouse marks required with "*").
_REQUIRED_LABELS_JS = r"""(scope) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  const out = [];
  document.querySelectorAll(scope + 'label, ' + scope + 'legend').forEach(l => {
    const r = l.getBoundingClientRect();
    if (!(r.width && r.height)) return;  // hidden (e.g. a wizard's other steps) don't count
    if ((l.innerText || '').includes('*')) {
      const c = clean(l.innerText);
      if (c && !out.includes(c)) out.push(c);
    }
  });
  return out;
}"""

_TEXTLIKE = {"", "text", "email", "tel", "url", "number", "search"}

# JS: inventory every form control with its derived label + kind + visibility (diagnostics).
_DUMP_JS = (
    "(scope) => { const labelOf = " + _LABEL_JS + ";"
    " const els = document.querySelectorAll(scope+'input, '+scope+'textarea, '+scope+'select, '+scope+'[role=combobox]');"
    " return Array.from(els).map(el => { const r = el.getBoundingClientRect(); return {"
    " tag: el.tagName.toLowerCase(), type: (el.getAttribute('type')||'').toLowerCase(),"
    " role: (el.getAttribute('role')||'').toLowerCase(), cls: (el.getAttribute('class')||'').slice(0, 40),"
    " vis: !!(r.width && r.height), label: labelOf(el) }; }); }"
)


def _dump_fields(page) -> None:
    """Print every form control the driver sees — tag/type/role/visible/label — to stderr, so
    a live run reveals the real DOM when a field won't fill."""
    import sys
    try:
        rows = page.evaluate(_DUMP_JS, _scope_prefix(page))
    except Exception as e:
        print(f"[debug] field dump failed: {e}", file=sys.stderr)
        return
    print(f"\n[debug] {len(rows)} form control(s) seen:", file=sys.stderr)
    for r in rows:
        kind = r["role"] or r["type"] or r["tag"]
        vis = "vis" if r["vis"] else "HIDDEN"
        print(f"  [{kind:9}] {vis:6} {r['label']!r:52} <{r['tag']} class={r['cls']!r}>",
              file=sys.stderr)


# --------------------------------------------------------------- native-autofill (first pass)

def detect_ats(url: str) -> str:
    """Identify the ATS from the URL so we can try its own autofill before ours."""
    u = (url or "").lower()
    if "greenhouse" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq" in u or "jobs.ashby" in u:
        return "ashby"
    if "myworkdayjobs" in u or "workday" in u:
        return "workday"
    if "icims" in u:
        return "icims"
    return "generic"


# JS: a control's current displayed value — native <input>/<select>/<textarea> expose .value;
# react-select shows the chosen option as a .select__single-value inside its control container.
_VALUE_JS = r"""(el) => {
  if (el.value && String(el.value).trim()) return String(el.value).trim();
  const ctl = el.closest('[class*="control"]');
  if (ctl) {
    const sv = ctl.querySelector('[class*="single-value"], [class*="multi-value__label"]');
    if (sv && sv.innerText.trim()) return sv.innerText.trim();
  }
  return '';
}"""

# Buttons that trigger an ATS's own resume-parse autofill (no account needed). Account-based
# autofills (MyGreenhouse, Apply-with-LinkedIn) open a login and are handled separately.
_NATIVE_AUTOFILL_BUTTONS = (r"autofill with resume", r"autofill from resume", r"^\s*autofill")


def _trigger_native_autofill(frame, ats: str, report: "ApplyReport") -> None:
    """Click the ATS's resume-parse autofill button if it exposes one (e.g. Workday's "Autofill
    with Resume"). Lever/Ashby parse on upload and need no click — the resume upload already
    happened, so their fields populate on their own before our only-empty pass. `frame` is the
    form's frame (main page or embedded iframe)."""
    for pat in _NATIVE_AUTOFILL_BUTTONS:
        try:
            btn = frame.get_by_role("button", name=re.compile(pat, re.I)).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=4000)
                report.native_autofill = f"{ats}: {pat.strip('^\\s*')} button"
                frame.page.wait_for_timeout(2500)  # let the parse populate fields
                return
        except Exception:
            continue


def _greenhouse_native_autofill(page, ctx, profile, report: "ApplyReport") -> None:
    """Quick Apply with MyGreenhouse using stored credentials (decision 017: store credentials +
    auto-login). Gated behind having credentials; a login failure is logged, not fatal — we
    just fall back to filling the form ourselves.

    NOTE: this drives the real my.greenhouse.io sign-in; it is best-effort and UNVERIFIED
    against a live account (needs a real MyGreenhouse login to confirm the exact flow)."""
    email = (getattr(profile, "greenhouse_email", "") or "").strip()
    pw = (getattr(profile, "greenhouse_password", "") or "").strip()
    if not (email and pw):
        return
    try:
        btn = page.get_by_role("button", name=re.compile("quick apply with mygreenhouse", re.I)).first
        if not (btn.count() and btn.is_visible()):
            return
        with ctx.expect_page(timeout=8000) as pi:
            btn.click()
        popup = pi.value
        popup.wait_for_load_state("domcontentloaded")
        popup.get_by_label(re.compile("email", re.I)).first.fill(email, timeout=6000)
        # Password may be on the same page or a second step — try both.
        for nm in (r"continue|next", r"sign in|log in"):
            try:
                popup.get_by_role("button", name=re.compile(nm, re.I)).first.click(timeout=3000)
            except Exception:
                pass
        try:
            popup.get_by_label(re.compile("password", re.I)).first.fill(pw, timeout=6000)
            popup.get_by_role("button", name=re.compile("sign in|log in|continue", re.I)).first.click(timeout=4000)
        except Exception:
            pass
        try:
            popup.wait_for_event("close", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)  # let MyGreenhouse populate the form
        report.native_autofill = "greenhouse: MyGreenhouse (Quick Apply)"
    except Exception as e:
        report.errors.append(f"MyGreenhouse autofill (falling back to our autofill): {type(e).__name__}: {e}")


# A visible field that reliably signals the real application form has rendered. ATS forms
# mount their inputs via JS after domcontentloaded, so we must wait for one of these before
# filling — otherwise the field scan runs against an empty DOM and fills nothing.
_FORM_FIELD_SIGNAL = (
    'input[type="email"], input[name*="email" i], input[id*="email" i], '
    'input[name*="first" i], input[id*="first" i], input[autocomplete="given-name"], '
    'textarea[name], form input[type="text"]'
)


# Frame URLs that are never the application form — skip them when hunting for the form frame.
_NON_FORM_FRAME = ("recaptcha", "captcha", "googletagmanager", "google-analytics", "doubleclick",
                   "/gtm", "privacycompliance", "content.googleapis", "hcaptcha")


def _count_fields(frame) -> int:
    """Number of real (non-hidden) form controls in a frame."""
    try:
        return frame.evaluate(
            "() => document.querySelectorAll("
            "'input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select'"
            ").length"
        )
    except Exception:
        return 0


def _find_form_frame(page):
    """Return (frame, field_count) for the frame that actually holds the application form.
    ATS forms are frequently embedded in an IFRAME — e.g. Greenhouse's job_app embed on a
    company's own careers site (stripe.com → job-boards.greenhouse.io/embed/job_app). Our
    locators don't cross frames, so we must pick the frame with the fields, not just the main
    page. Returns the richest non-chrome frame."""
    best, best_n = page.main_frame, _count_fields(page.main_frame)
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        if any(s in (fr.url or "").lower() for s in _NON_FORM_FRAME):
            continue
        n = _count_fields(fr)
        if n > best_n:
            best, best_n = fr, n
    return best, best_n


def _ats_from_frame(frame, fallback: str) -> str:
    """Re-derive the ATS from the frame that holds the form. A Greenhouse form embedded on a
    company domain (stripe.com) is detected as 'generic' from the outer URL but is really
    greenhouse — the embed frame URL reveals it."""
    u = (getattr(frame, "url", "") or "").lower()
    for name in ("greenhouse", "lever", "ashby", "workday", "icims"):
        if name in u:
            return "greenhouse" if name == "greenhouse" else name
    return fallback


def _open_application_form(page, ats: str, report: "ApplyReport", timeout_ms: int = 25000):
    """Reveal the application form (click an Apply control if needed) and WAIT until it has
    actually rendered — IN WHICHEVER FRAME it lives (main page or an embedded iframe). Returns
    (loaded, frame, ats): the frame to fill and the ATS re-derived from that frame. On failure
    returns (False, main_frame, ats) with an actionable error, and the caller must not fill.
    This is what makes 'verify the application loaded before filling' true rather than assumed."""
    import time

    # If no form is visible anywhere yet, try to reveal it via an "Apply" control.
    frame, n = _find_form_frame(page)
    if n < 2:
        for role in ("link", "button"):
            try:
                btn = page.get_by_role(role, name=re.compile(r"\bapply\b", re.I)).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=4000)
                    break
            except Exception:
                continue

    # Poll every frame until one holds a real form (covers navigation + async/iframe mounts).
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        frame, n = _find_form_frame(page)
        if n >= 2:
            # Settle: wait for a labelled field to be visible in that frame, then a beat for the rest.
            try:
                frame.wait_for_selector(_FORM_FIELD_SIGNAL, state="visible", timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(600)
            return True, frame, _ats_from_frame(frame, ats)
        page.wait_for_timeout(500)

    report.errors.append(
        f"Application form did not load within {timeout_ms // 1000}s at {page.url}. "
        "The page may require sign-in, redirect to an external application portal, or use a "
        "form ApplicationBot doesn't support yet — so no fields were filled. Open the URL to check."
    )
    return False, page.main_frame, ats


def _upload_resume(frame, resume_pdf: str, report: "ApplyReport") -> None:
    """Attach the résumé. Prefer Greenhouse's own "Attach" button through the file chooser —
    poking the raw hidden <input type=file> makes the site's onchange handler throw
    "Cannot read properties of undefined (reading 'uploadFile')". `frame` is the form's frame
    (main page or an embedded iframe); the file chooser is captured at the page level."""
    for name in (r"attach", r"upload"):
        try:
            btn = frame.get_by_role("button", name=re.compile(name, re.I)).first
            if btn.count() and btn.is_visible():
                with frame.page.expect_file_chooser(timeout=4000) as fc:
                    btn.click()
                fc.value.set_files(resume_pdf)
                report.filled.append(FilledField("Resume", resume_pdf, "file"))
                return
        except Exception:
            continue
    try:  # classic/older forms expose a direct file input
        fi = frame.locator('input[type="file"]')
        if fi.count():
            fi.first.set_input_files(resume_pdf)
            report.filled.append(FilledField("Resume", resume_pdf, "file"))
        # No upload control on this page: a wizard may expose it on a later step — the
        # page walker retries there, and flags it if the whole walk ends without one.
    except Exception as e:
        report.errors.append(f"resume upload: {type(e).__name__}: {e}")


def _matches(option: str, want: str) -> bool:
    """Case-insensitive: option equals/contains `want`, or (for non-tiny option text) is
    contained by it — so "online" matches both an "Online" option and a longer sentence.
    A very short `want` (≤3 chars, e.g. "US", "No") must match the WHOLE option, never as a
    substring — otherwise "US" matched "A-US-tralia" and answered a country with "Australia"."""
    o, w = option.strip().lower(), want.strip().lower()
    if not (o and w):
        return False
    if len(w) <= 3:
        # Whole-word match: "Yes" matches "Yes, I am authorized" but "US" does NOT match
        # "A-US-tralia" and "No" does NOT match "Norway".
        return re.search(r"\b" + re.escape(w) + r"\b", o) is not None
    return o == w or w in o or (len(o) >= 3 and o in w)


def _fill_select(loc, value: Optional[str], hints: Optional[list[str]] = None) -> str:
    """Native <select>: try the answer, then each ranked hint. Return the chosen option text."""
    opts = loc.evaluate(
        "el => Array.from(el.options).map(o => ({v: o.value, t: (o.textContent||'').trim()}))"
    )
    for want in ([value] if value else []) + (hints or []):
        for o in opts:  # exact match first within this candidate
            if o["t"].strip().lower() == want.strip().lower():
                loc.select_option(value=o["v"], timeout=5000)
                return o["t"]
        for o in opts:  # then fuzzy
            if _matches(o["t"], want):
                loc.select_option(value=o["v"], timeout=5000)
                return o["t"]
    raise RuntimeError(f"no <option> matching {value!r} or hints {hints!r}")


def _open_combobox(page, loc) -> bool:
    """Open a react-select/combobox menu. The inner <input> is often not directly clickable,
    so prefer clicking the enclosing control container."""
    for sel in (
        "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), 'control')][1]",
        "xpath=ancestor::*[@role='combobox'][1]",
    ):
        try:
            c = loc.locator(sel)
            if c.count():
                c.first.click(timeout=3000)
                return True
        except Exception:
            continue
    try:
        loc.click(timeout=3000)
        return True
    except Exception:
        return False


def _open_options(page):
    """The just-opened react-select menu's options, visible only. Scoping to `.select__option`
    + `:visible` avoids the always-present hidden listboxes on the page (e.g. the phone
    country picker exposes ~250 [role=option] elements that would otherwise be matched)."""
    opts = page.locator(".select__option:visible")
    try:
        opts.first.wait_for(state="visible", timeout=3000)
        return opts
    except Exception:
        pass
    opts = page.locator('[role="option"]:visible')  # non-Greenhouse react-select fallback
    try:
        opts.first.wait_for(state="visible", timeout=1500)
        return opts
    except Exception:
        return None


# US state abbreviations → full names, so "Edison, NJ" matches an "Edison, New Jersey" option.
_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


def _phrases(text: str) -> set:
    """Value tokens for matching, with US state abbreviations expanded to full names."""
    out = set()
    for t in re.split(r"[,\s]+", (text or "").lower()):
        t = t.strip()
        if len(t) >= 2:
            out.add(_US_STATES.get(t, t))
    return out


def _best_by_tokens(texts: list[str], want: str) -> tuple[int, int]:
    """Index of the option sharing the most tokens with `want` (state-expanded), and its score."""
    wants = _phrases(want)
    best_i, best_score = -1, 0
    for i, t in enumerate(texts):
        tl = t.lower()
        score = sum(1 for w in wants if w in tl)
        if score > best_score:
            best_i, best_score = i, score
    return best_i, best_score


def _pick_from_open(page, want: str, want_full: Optional[str] = None) -> Optional[str]:
    """Given an already-open combobox menu, click the best-matching visible option."""
    opts = _open_options(page)
    if opts is None:
        return None
    count = min(opts.count(), 30)
    texts = [(opts.nth(i).inner_text() or "").strip() for i in range(count)]
    target = want_full or want
    for i, t in enumerate(texts):  # 1) direct fuzzy match
        if _matches(t, target):
            opts.nth(i).click(timeout=4000)
            return t
    bi, score = _best_by_tokens(texts, target)  # 2) best token overlap (Edison, NJ → New Jersey)
    if bi >= 0 and score > 0:
        opts.nth(bi).click(timeout=4000)
        return texts[bi]
    # No option fuzzy-matches or shares a token with what we wanted. Do NOT blind-pick the first
    # option — that committed a wrong value (e.g. answering "country: United States" with
    # "Australia"). Leave it unselected so it surfaces for review instead of a confident-wrong fill.
    return None


def _combo_try(page, loc, text: str) -> Optional[str]:
    """Open a react-select combobox, type to filter/search, commit a matching option.

    Async location/country search often returns nothing for "City, ST" but does for the city
    alone — so if the full string yields no options, retry with the first token and match back
    against the full value."""
    if not _open_combobox(page, loc):
        return None
    try:
        loc.fill(text, timeout=4000)
    except Exception:
        try:
            loc.type(text, delay=20, timeout=4000)
        except Exception:
            return None
    chosen = _pick_from_open(page, text)
    if chosen:
        return chosen
    first = re.split(r"[,\n]", text)[0].strip()
    if first and first.lower() != text.strip().lower():
        try:
            loc.fill(first, timeout=4000)
        except Exception:
            return None
        return _pick_from_open(page, first, want_full=text)
    return None


def _open_options_and_texts(page):
    """Open the menu's options and return (locator, texts) TOGETHER so a match can be clicked
    by index on the same locator — re-querying options separately to click is flaky."""
    opts = _open_options(page)
    if opts is None:
        return None, []
    n = min(opts.count(), 60)
    return opts, [(opts.nth(i).inner_text() or "").strip() for i in range(n)]


def _record_capture(report: "ApplyReport", question: str, kind: str, options=None) -> None:
    """Remember an unanswered field's control TYPE and options so the Profile UI can recreate it
    faithfully (a dropdown question becomes a dropdown, not a free-text box)."""
    report.captured[question] = {"kind": kind, "options": [o for o in (options or []) if o][:40]}


def _field_options(page, loc, tag: str, role: str) -> list[str]:
    """Selectable option texts for a native <select> or a react-select combobox, for recreating the
    field. Empty for a text input or a searchable typeahead with nothing shown (falls back to text).
    Best-effort; opens then closes a combobox menu."""
    try:
        if tag == "select":
            return [o.strip() for o in loc.evaluate(
                "el => Array.from(el.options).map(o => (o.textContent||'').trim())") if (o or "").strip()]
        if role == "combobox" and _open_combobox(page, loc):
            page.wait_for_timeout(300)
            _, texts = _open_options_and_texts(page)
            try:
                loc.press("Escape")
            except Exception:
                pass
            return [t for t in texts if t]
    except Exception:
        pass
    return []


def _commit_option_text(page, loc, chosen: str, query: Optional[str] = None) -> bool:
    """Reopen the combobox (retyping `query` first for a searchable list) and click the option
    whose text is exactly `chosen`. Deciding an option (a Claude call can take tens of seconds)
    happens with the menu CLOSED — holding a react-select popup open across a subprocess call
    is a staleness race (the menu re-renders, indexes shift, options detach). This re-derives
    the same option list and commits deterministically by exact text."""
    if not _open_combobox(page, loc):
        return False
    if query is not None:
        try:
            loc.fill(query, timeout=4000)
        except Exception:
            return False
        page.wait_for_timeout(900)
    else:
        page.wait_for_timeout(250)
    opts, texts = _open_options_and_texts(page)
    for i, t in enumerate(texts):
        if t == chosen:
            try:
                opts.nth(i).click(timeout=4000)
                return True
            except Exception:
                return False
    try:
        loc.press("Escape")  # option no longer offered — close cleanly, caller falls through
    except Exception:
        pass
    return False


def _search_queries(value: str) -> list[str]:
    """Progressive typeahead search queries for an async combobox, longest→shortest, with any
    leading article stripped. A prefix-indexed list (e.g. a school picker that stores Penn State
    as "Pennsylvania State University-Main Campus") returns NOTHING for the full résumé value
    "The Pennsylvania State University" or its first word "The" — so also try the article-stripped
    form and its leading words, then let token/Claude matching pick the right option from whatever
    those retrieve. Root cause of the school dropdown never filling (decision 033 follow-up)."""
    v = " ".join((value or "").split())
    if not v:
        return []
    out = [v]
    low = v.lower()
    for art in ("the ", "a ", "an "):
        if low.startswith(art):
            out.append(v[len(art):].strip())
            break
    words = out[-1].split()
    for k in (3, 2):  # distinctive leading prefixes; 1 word is usually too broad to match on
        if len(words) > k:
            out.append(" ".join(words[:k]))
    seen, uniq = set(), []
    for q in out:
        ql = q.lower()
        if q and ql not in seen:
            seen.add(ql)
            uniq.append(q)
    return uniq


def _fill_combobox(page, loc, value: Optional[str], hints: Optional[list[str]] = None,
                   resolver=None, label: str = "") -> Optional[tuple[str, str]]:
    """react-select combobox: commit the option matching the answer (or a ranked hint). Returns
    (committed option text, matched tier) — tier ∈ literal | learned | hint | claude | substring,
    recorded on the fill report so every dropdown fill is auditable — or None. Never leaves
    uncommitted typed text (which would look filled but submit as an invalid/empty selection) —
    clears the field if nothing selected.

    (1) On the FIRST open, literal-match any candidate (answer + hints + LEARNED aliases). If no
    match and it's a static list (options already shown), CLOSE the menu, let Claude pick from
    those options, then recommit by exact text (a Claude call takes seconds-to-minutes — deciding
    never happens with the menu open) and LEARN it.
    (2) Otherwise type each candidate to filter a searchable list; if a typed filter yields
    options but none literally match, same closed-menu Claude pick over them. Learned mappings
    make the same value match instantly next time without another Claude call (decision 033)."""
    learned = resolver.learned_option_hints(value) if resolver is not None else []
    # A batch-decided pick for THIS label leads the candidates: committed by exact text in
    # round 2 of a two-pass fill, reported as tier "claude" (Claude decided it, batched).
    decided = resolver.decided_options.get(label) if resolver is not None else None
    pending = resolver.pending if resolver is not None else None
    picks_done = resolver.picks_done if resolver is not None else set()
    candidates = ([(decided, "claude")] if decided else []) \
        + ([(value, "literal")] if value else []) \
        + [(h, "hint") for h in (hints or []) if h] \
        + [(l, "learned") for l in learned if l]
    use_claude = resolver is not None and getattr(resolver, "enable_generation", False) \
        and bool(value) and label not in picks_done

    # Phase 1 — literal match on the options shown on open; then Claude-pick for static lists.
    if _open_combobox(page, loc):
        page.wait_for_timeout(250)
        opts, texts = _open_options_and_texts(page)
        for want, tier in candidates:
            for i, t in enumerate(texts):
                if _matches(t, want):
                    opts.nth(i).click(timeout=4000)
                    if tier == "claude":
                        resolver.learn_option(value, t)  # a committed batch pick is learned too
                    return t, tier
        try:
            loc.press("Escape")  # close before any Claude call / Phase 2 typing
        except Exception:
            pass
        if use_claude and len(texts) >= 3:  # static list fully shown — pick before pollution
            if pending is not None:
                # Round 1 of a two-pass fill: the pick joins ONE batched call between rounds;
                # round 2 recommits the decided text. Skip the remaining phases — filling by
                # substring now would preempt the vetted batch decision.
                pending.defer_pick(label, value, texts)
                return None
            chosen = answer_bank.pick_dropdown_option(label, value, texts, model=resolver.model)
            if chosen and _commit_option_text(page, loc, chosen):
                resolver.learn_option(value, chosen)
                return chosen, "claude"

    # Phase 2 — type each candidate to filter a searchable list (literal match).
    for want, tier in candidates:
        chosen = _combo_try(page, loc, want)
        if chosen:
            if tier == "claude" and resolver is not None:
                resolver.learn_option(value, chosen)
            return chosen, tier

    # Phase 2b — searchable list, no literal match on the full value: type progressively-shorter,
    # article-stripped queries (a school picker indexes "The Pennsylvania State University" under
    # "Pennsylvania State University-…", so the full value retrieves nothing) and let Claude pick
    # the best option from the results — it's told to prefer the primary/main campus. Claude runs
    # BEFORE the non-Claude substring fallback so an ambiguous multi-campus list resolves to the
    # main campus, not whichever campus appears first. Learns the vetted mapping for next time.
    if use_claude:
        for q in _search_queries(value):
            if not _open_combobox(page, loc):
                break
            try:
                loc.fill(q, timeout=4000)
            except Exception:
                continue
            page.wait_for_timeout(900)
            _, texts = _open_options_and_texts(page)
            try:
                loc.press("Escape")  # decide with the menu closed; recommit retypes the query
            except Exception:
                pass
            chosen = answer_bank.pick_dropdown_option(label, value, texts, model=resolver.model) \
                if texts else None
            if chosen and _commit_option_text(page, loc, chosen, query=q):
                resolver.learn_option(value, chosen)
                return chosen, "claude"

    # Phase 2c — no Claude (or it declined): best-effort substring match on the shortened queries
    # for a comma-free name value (e.g. a school), so the field still fills when generation is off.
    # NOT learned — this is an unvetted first-substring pick (it can land on a non-primary campus),
    # not a confirmed mapping, so we don't persist it.
    if value and "," not in value:
        for q in _search_queries(value)[1:]:  # [0] == value, already tried in Phase 2
            chosen = _combo_try(page, loc, q)
            if chosen:
                return chosen, "substring"

    try:
        loc.fill("", timeout=2000)  # discard any typed-but-uncommitted text
    except Exception:
        pass
    return None


def _scope_prefix(page) -> str:
    """Return "form " when the page wraps its fields in a <form> (Greenhouse/Lever), else ""
    to scan page-wide. Ashby renders its application fields OUTSIDE any <form>, so a
    form-scoped selector matches nothing — this is why only the résumé filled before."""
    try:
        return "form " if page.locator("form").count() > 0 else ""
    except Exception:
        return ""


def _fill_all_fields(page, resolver: AnswerResolver, report: "ApplyReport", done: set,
                     only_empty: bool = True) -> None:
    sp = _scope_prefix(page)
    controls = page.locator(f"{sp}input, {sp}textarea, {sp}select")
    try:
        count = controls.count()
    except Exception:
        count = 0
    for i in range(count):
        loc = controls.nth(i)
        try:
            k = loc.evaluate(
                "el => ({tag: el.tagName.toLowerCase(), "
                "type: (el.getAttribute('type')||'').toLowerCase(), "
                "role: (el.getAttribute('role')||'').toLowerCase(), "
                "chrome: !!el.closest('nav,header,footer,[role=search],[role=navigation]')})"
            )
        except Exception:
            continue
        tag, typ, role = k["tag"], k["type"], k["role"]
        if typ in ("hidden", "submit", "button", "file", "checkbox", "radio", "search"):
            continue  # radios handled as groups below; search boxes aren't application fields
        if k.get("chrome"):
            continue  # page nav/header/footer/search chrome, not the application form
        # react-select combobox inputs are often 1px / opacity:0 — don't skip them on
        # visibility; we open them via their (visible) control container.
        try:
            vis = loc.is_visible()
        except Exception:
            vis = False
        if not vis and role != "combobox":
            continue
        try:
            label = loc.evaluate(_LABEL_JS)
        except Exception:
            label = ""
        if not label or label in done:
            continue
        # Native-first: if the ATS's own autofill already populated this field, keep its value
        # and record it — our resolver only fills what's still empty.
        if only_empty:
            try:
                current = loc.evaluate(_VALUE_JS)
            except Exception:
                current = ""
            if current:
                report.filled.append(FilledField(
                    label, current, "combobox" if role == "combobox" else "text", source="native"))
                done.add(label)
                continue
        value = resolver.resolve(label)
        hints = resolver.option_hints(label)
        is_free = (tag == "textarea" or typ in _TEXTLIKE) and role != "combobox"
        # For structured (non-open-ended) fields the keyword rules missed, ask Claude to
        # classify the question onto a known type (e.g. an office-days phrasing → remote) and
        # answer from the profile. Open-ended text goes to the drafting path instead.
        if value is None and not is_free:
            value = resolver.resolve_semantic(label)
        if value is None and not hints and not is_free:
            kind = "select" if tag == "select" else ("dropdown" if role == "combobox" else "text")
            if resolver.pending is not None and resolver.pending.has(label):
                # Deferred to the batched decision step — round 2 revisits it. Record the
                # control's kind/options so a classified dropdown can join the pick batch.
                resolver.pending.enrich(label, kind, _field_options(page, loc, tag, role))
                continue
            _record_capture(report, label, kind, _field_options(page, loc, tag, role))
            report.skipped.append(f"{label} — no saved answer")
            done.add(label)
            continue
        try:
            # Same question can be a dropdown or a text box depending on the company —
            # dispatch on the control type discovered live.
            if role == "combobox":
                got = _fill_combobox(page, loc, value, hints, resolver=resolver, label=label)
                if got:
                    text, tier = got
                    # source records HOW the option matched (option:literal | option:learned |
                    # option:hint | option:claude | option:substring) — the determinism audit
                    # trail: anything but option:claude was resolved without a model call.
                    report.filled.append(FilledField(label, text, "combobox", source=f"option:{tier}"))
                elif resolver.pending is not None and resolver.pending.has(label):
                    continue  # pick deferred to the batch — round 2 recommits by exact text
                else:
                    report.skipped.append(f"{label} — no dropdown option matched {value!r}")
            elif tag == "select":
                report.filled.append(FilledField(label, _fill_select(loc, value, hints), "select"))
            elif is_free:
                # Banked/structured answer, else a grounded Claude draft for open-ended questions.
                ans, source = resolver.freetext_answer(label, is_textarea=(tag == "textarea"))
                if source == "pending":
                    continue  # deferred to the batch — round 2 revisits
                if ans is None:
                    _record_capture(report, label, "textarea" if tag == "textarea" else "text")
                    report.skipped.append(f"{label} — no saved answer")
                else:
                    loc.fill(ans, timeout=5000)
                    report.filled.append(FilledField(label, ans, "text", source=source))
            else:
                report.skipped.append(f"{label} — unsupported field type ({tag}/{typ})")
        except Exception as e:
            report.errors.append(f"{label}: {type(e).__name__}: {e}")
        done.add(label)


def _fill_radio_groups(page, resolver: AnswerResolver, report: "ApplyReport", done: set) -> None:
    radios = page.locator(f'{_scope_prefix(page)}input[type="radio"]')
    try:
        n = radios.count()
    except Exception:
        n = 0
    groups: dict[str, list[int]] = {}
    for i in range(n):
        try:
            name = radios.nth(i).evaluate("el => el.name || ''")
        except Exception:
            name = ""
        groups.setdefault(name or f"__anon{i}", []).append(i)
    for idxs in groups.values():
        try:
            q = radios.nth(idxs[0]).evaluate(_GROUP_QUESTION_JS)
        except Exception:
            q = ""
        if not q or q in done:
            continue
        value = resolver.resolve(q)
        if value is None:  # semantic classify onto a known type (batched/cached) on a miss
            value = resolver.resolve_semantic(q)
        if value is None:
            labels = []
            for i in idxs:
                try:
                    lb = (radios.nth(i).evaluate(_LABEL_JS) or "").strip()
                except Exception:
                    lb = ""
                if lb:
                    labels.append(lb)
            if resolver.pending is not None and resolver.pending.has(q):
                resolver.pending.enrich(q, "radio", labels)
                continue  # deferred to the batched decision step — round 2 revisits
            _record_capture(report, q, "radio", labels)
            report.skipped.append(f"{q} — no saved answer")
            done.add(q)
            continue
        v = value.strip().lower()
        picked = False
        for i in idxs:
            try:
                opt = (radios.nth(i).evaluate(_LABEL_JS) or "").strip().lower()
            except Exception:
                opt = ""
            if opt and (opt == v or v in opt):
                try:
                    radios.nth(i).check(timeout=4000)
                    report.filled.append(FilledField(q, value, "radio"))
                    picked = True
                except Exception as e:
                    report.errors.append(f"{q}: {type(e).__name__}: {e}")
                break
        if not picked:
            report.skipped.append(f"{q} — no radio option matching {value!r}")
        done.add(q)


# Checkbox classification. Agreements/consents/certifications gate submission and are inherent to
# applying (checking them is what the armed user authorized; dry-run never submits regardless).
# Optional opt-ins (marketing, talent community, SMS) must never be auto-checked.
_AGREEMENT_KW = (
    "agree", "consent", "certify", "acknowledge", "authorize", "confirm", "i attest",
    "i have read", "read and understood", "i understand", "by checking", "by submitting",
    "terms", "privacy", "true and accurate", "accurate and complete", "acknowledgment",
)
_OPTIN_KW = (
    "marketing", "newsletter", "promotional", "subscribe", "talent community", "talent pool",
    "talent network", "future opportunit", "other opportunit", "other roles", "keep me informed",
    "keep me updated", "receive updates", "contact me about", "opt in", "opt-in", "text message",
    "sms", "phone call",
)


def _is_agreement(n: str) -> bool:
    return any(k in n for k in _AGREEMENT_KW)


def _is_optional_optin(n: str) -> bool:
    return any(k in n for k in _OPTIN_KW)


def _fill_checkboxes(page, resolver: AnswerResolver, report: "ApplyReport", done: set) -> None:
    """Two checkbox cases the field/radio passes skip: (1) multi-select GROUPS — several checkboxes
    under one question ("race — check all that apply") — check the option(s) matching the resolved
    answer; (2) standalone AGREEMENT/consent/certification checkboxes — check them (required to
    submit; the armed user authorized applying, and dry-run never submits). Optional opt-ins
    (marketing/talent-community/SMS) are always left unchecked."""
    boxes = page.locator(f'{_scope_prefix(page)}input[type="checkbox"]')
    try:
        n = boxes.count()
    except Exception:
        return
    info: list = []
    groups: dict[str, list[int]] = {}
    for i in range(n):
        b = boxes.nth(i)
        try:
            d = b.evaluate(
                "el => ({chrome: !!el.closest('nav,header,footer,[role=search],[role=navigation]'),"
                " checked: !!el.checked, disabled: !!el.disabled})")
            lbl = (b.evaluate(_LABEL_JS) or "").strip()
            q = (b.evaluate(_GROUP_QUESTION_JS) or "").strip()
        except Exception:
            info.append(None)
            continue
        if d["chrome"] or d["disabled"]:
            info.append(None)
            continue
        info.append({"lbl": lbl, "q": q, "checked": d["checked"]})
        if q:
            groups.setdefault(q, []).append(i)

    handled: set = set()
    # (1) Multi-select groups: a question shared by >1 checkbox → check options matching the answer.
    for q, idxs in groups.items():
        if len(idxs) < 2 or q in done:
            continue
        for i in idxs:
            handled.add(i)
        value = resolver.resolve(q) or resolver.resolve_semantic(q)
        # Also consult option_hints — e.g. a "US" checkbox vs our "United States" value (Stripe's
        # country list uses abbreviations UAE/UK/US), matched via the country aliases.
        candidates = [c for c in ([value] if value else []) + (resolver.option_hints(q) or []) if c]
        if not candidates and resolver.pending is not None and resolver.pending.has(q):
            resolver.pending.enrich(q, "checkbox", [info[i]["lbl"] for i in idxs])
            continue  # deferred to the batched decision step — round 2 revisits
        done.add(q)
        if not candidates:
            _record_capture(report, q, "checkbox", [info[i]["lbl"] for i in idxs])
            report.skipped.append(f"{q} — no saved answer")
            continue
        matched = False
        for i in idxs:
            opt = info[i]["lbl"]
            if opt and any(_matches(opt, c) for c in candidates):
                if not info[i]["checked"]:
                    try:
                        boxes.nth(i).check(timeout=4000)
                    except Exception as e:
                        report.errors.append(f"{q}: {type(e).__name__}: {e}")
                        continue
                report.filled.append(FilledField(q, opt, "checkbox"))
                matched = True
        if not matched:
            report.skipped.append(f"{q} — no checkbox option matching {value!r}")

    # (2) Standalone agreement/consent checkboxes.
    for i in range(n):
        if info[i] is None or i in handled:
            continue
        lbl = info[i]["lbl"]
        if not lbl or lbl in done:
            continue
        if info[i]["checked"]:
            report.filled.append(FilledField(lbl, "checked", "checkbox", source="native"))
            done.add(lbl)
            continue
        nlbl = _norm(lbl)
        if _is_optional_optin(nlbl) or not _is_agreement(nlbl):
            continue  # optional opt-in, or a checkbox we can't confidently classify — leave it
        try:
            boxes.nth(i).check(timeout=4000)
            report.filled.append(FilledField(lbl, "checked", "checkbox"))
            done.add(lbl)
        except Exception as e:
            report.errors.append(f"{lbl[:40]}: {type(e).__name__}: {e}")


def _flag_missing_required(page, report: "ApplyReport", done: set) -> None:
    """Report required fields we never filled — the safety net against silent gaps."""
    try:
        required = page.evaluate(_REQUIRED_LABELS_JS, _scope_prefix(page))
    except Exception:
        return
    filled = {f.label for f in report.filled}
    for r in required:
        if r in filled or any(r == d or r in d for d in done):
            continue
        report.skipped.append(f"{r} — REQUIRED, not filled (no matching answer or unsupported field)")


# ------------------------------------------------------------- multi-page navigation

# Buttons that advance a multi-step wizard (Workday-style). Anchored so they can NEVER
# match a submit control ("Submit application") or an auth control ("Continue with
# Google") — advancing pages is fill work, not submission, so it is dry-run-safe.
_NEXT_PATTERNS = (r"^next$", r"^next step$", r"^continue$", r"^save (?:and|&) continue$")

_MAX_FORM_PAGES = 8  # runaway-wizard backstop


def _find_next_button(frame):
    for pat in _NEXT_PATTERNS:
        try:
            btn = frame.get_by_role("button", name=re.compile(pat, re.I)).first
            if btn.count() and btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _page_signature(frame) -> str:
    """Cheap fingerprint of the VISIBLE form controls, used to detect that a wizard
    actually advanced (name/id sets differ between steps; hidden steps don't count)."""
    return frame.evaluate(
        """() => {
          const els = Array.from(document.querySelectorAll(
            'input:not([type=hidden]), textarea, select, [role=combobox]'
          )).filter(e => { const r = e.getBoundingClientRect(); return r.width && r.height; });
          return els.length + ':' + els.slice(0, 40).map(e => e.name || e.id || '').join(',');
        }"""
    )


def _advance_page(page, frame, nxt, report: "ApplyReport"):
    """Click a next/continue control and wait for the form to actually change. Returns
    (advanced, frame) — the frame is re-located because a step change can remount or
    navigate it. No change = the wizard rejected the advance (client-side validation);
    that's recorded and the caller stops walking."""
    import time

    try:
        before = _page_signature(frame)
    except Exception:
        before = ""
    try:
        nxt.click(timeout=4000)
    except Exception as e:
        report.errors.append(f"next-step click failed: {type(e).__name__}: {e}")
        return False, frame

    deadline = time.time() + 10
    while time.time() < deadline:
        page.wait_for_timeout(400)
        try:
            cur, n = _find_form_frame(page)
            sig = _page_signature(cur) if n else ""
        except Exception:
            continue  # frame detached mid-navigation — keep polling
        if sig and sig != before:
            page.wait_for_timeout(400)  # settle the newly-mounted step
            return True, cur

    try:
        errs = [t for t in frame.evaluate(_VALIDATION_JS) if t]
    except Exception:
        errs = []
    report.errors.append(
        "could not advance to the next form page — "
        + ("validation: " + "; ".join(errs[:5]) if errs
           else "no change after clicking next (fields on later pages were not reached)")
    )
    return False, frame


def _resolve_pending(resolver: AnswerResolver, pending: PendingDecisions) -> None:
    """Adjudicate round 1's deferred decisions with at most 3 BATCHED Claude calls, then inject
    the results so round 2 (the same deterministic loop) fills without any per-field call:
      1. classify — novel questions → structured types, answered live from the profile;
      2. bank-match — the rest → a reworded saved answer;
         both injected as in-memory bank entries (resolve() hits them in round 2; persistence
         still goes through remember_answers' valid_mapping gate);
      3. dropdown picks — the deferred static-list picks, plus any just-classified dropdown
         whose new answer doesn't literally match its recorded options → decided_options,
         recommitted by exact text in round 2.
    Every deferred label is marked adjudicated regardless of outcome, so round 2 captures the
    leftovers for the user instead of falling back to per-field Claude calls."""
    labels = list(pending.questions)
    types = answer_bank.classify_questions(labels, model=resolver.model) if labels else {}
    unresolved = []
    for label in labels:
        key = types.get(label)
        ans = resolver.answer_for_type(key) if key else None
        if key and ans is not None:
            qa = QA(question=label, answer="", maps_to=key, generated=True)
            resolver.learned.append(qa)
            resolver.profile.custom_answers.append(qa)
        else:
            unresolved.append(label)
    if unresolved:
        cands = []
        for qa in resolver.profile.custom_answers:
            mt = getattr(qa, "maps_to", "")
            ans = resolver.answer_for_type(mt) if mt else (qa.answer or "").strip()
            if (qa.question or "").strip() and ans:
                cands.append((qa, ans))
        if cands:
            matches = answer_bank.match_banked_questions(
                unresolved, [(qa.question, ans) for qa, ans in cands], model=resolver.model)
            for label in unresolved:
                idx = matches.get(label)
                if idx is None:
                    continue
                qa, _ = cands[idx]
                alias = QA(question=label, answer="" if qa.maps_to else qa.answer,
                           maps_to=qa.maps_to, generated=True)
                resolver.learned.append(alias)
                resolver.profile.custom_answers.append(alias)
    resolver.semantic_done.update(labels)

    items = [(label, value, opts) for label, (value, opts) in pending.picks.items()]
    for label, meta in pending.questions.items():
        if meta.get("kind") != "dropdown" or len(meta.get("options") or []) < 3:
            continue
        value = resolver.resolve(label)  # the just-injected answer, if any
        if not value:
            continue
        wants = [value] + (resolver.option_hints(label) or []) + resolver.learned_option_hints(value)
        if any(_matches(o, w) for w in wants for o in meta["options"]):
            continue  # round 2 matches it deterministically — no pick needed
        items.append((label, value, meta["options"]))
    if items:
        for (label, value, _), chosen in zip(items, answer_bank.pick_dropdown_options(
                items, model=resolver.model)):
            if chosen:
                resolver.decided_options[label] = chosen
    resolver.picks_done.update(label for label, _, _ in items)


def _fill_page(frame, resolver: AnswerResolver, report: "ApplyReport", done: set) -> None:
    """Fill one form page in two deterministic passes with ONE batched decision step between
    them (decision 041). Round 1 fills everything the rules/bank/hints/aliases resolve and
    DEFERS the unresolved decisions; ≤3 batched Claude calls adjudicate them all; round 2
    re-runs the same loop, which now fills from the injected results. Claude cost is per PAGE,
    not per field — and no model call ever runs while a menu is open (decision 040). Typeahead
    searches stay inline (their options only exist as you type). With generation disabled the
    first pass is the only pass, exactly as before."""
    resolver.pending = PendingDecisions() if resolver.enable_generation else None
    try:
        _fill_all_fields(frame, resolver, report, done, only_empty=True)
        _fill_radio_groups(frame, resolver, report, done)
        _fill_checkboxes(frame, resolver, report, done)
    finally:
        pending, resolver.pending = resolver.pending, None
    if pending:
        _resolve_pending(resolver, pending)
        _fill_all_fields(frame, resolver, report, done, only_empty=True)
        _fill_radio_groups(frame, resolver, report, done)
        _fill_checkboxes(frame, resolver, report, done)


def _fill_all_pages(page, frame, resolver: AnswerResolver, report: "ApplyReport", done: set,
                    resume_pdf: str = ""):
    """Fill EVERY page of the form: fill the current page's fields, flag its unmet REQUIRED
    fields (they're invisible once we advance), then walk Next/Continue until the final
    page. Single-page forms take one pass and find no next button — behaviour unchanged.
    Returns the frame holding the FINAL page (where the submit control lives)."""
    for step in range(1, _MAX_FORM_PAGES + 1):
        # A wizard may put the résumé upload on a later step — attach once, wherever it is.
        if step > 1 and resume_pdf and not any(f.control == "file" for f in report.filled):
            try:
                if frame.locator('input[type="file"]').count():
                    _upload_resume(frame, resume_pdf, report)
            except Exception:
                pass
        _fill_page(frame, resolver, report, done)
        _flag_missing_required(frame, report, done)

        nxt = _find_next_button(frame)
        advanced = False
        if nxt is not None:
            advanced, frame = _advance_page(page, frame, nxt, report)
        if not advanced:
            report.pages = step
            break
    else:
        report.errors.append(f"stopped after {_MAX_FORM_PAGES} form pages — wizard longer than expected")
        report.pages = _MAX_FORM_PAGES

    if resume_pdf and not any(f.control == "file" for f in report.filled):
        report.skipped.append("Resume upload — no upload field found on any form page")
    return frame


# ------------------------------------------------------------------ submit (armed only)

# Submit-control names, most specific first. GH/Lever/Ashby all label theirs
# "Submit application"; a bare "apply" button is deliberately NOT matched — that's the
# control that *opens* a form, not the one that sends it.
_SUBMIT_PATTERNS = (r"^submit\s+(?:your\s+)?application$", r"^submit$", r"^send\s+application$")

_CONFIRMATION_RX = re.compile(
    r"thank you for applying|thanks for applying|application (?:has been |was )?(?:submitted|received)"
    r"|we(?:'|’)?ve received your application|successfully submitted"
    r"|your application has been received",
    re.I,
)

# Visible required labels whose control is still EMPTY in the DOM (label→control via
# for=/descendant/fieldset; value read like _VALUE_JS incl. react-select's single-value).
_UNMET_REQUIRED_JS = r"""(scope) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  const out = [];
  document.querySelectorAll(scope + 'label, ' + scope + 'legend').forEach(l => {
    const r = l.getBoundingClientRect();
    if (!(r.width && r.height)) return;
    if (!(l.innerText || '').includes('*')) return;
    const name = clean(l.innerText);
    if (!name) return;
    let filled = null;  // null = couldn't tie the label to a control
    if (l.tagName === 'LEGEND') {
      const fs = l.closest('fieldset');
      if (fs) filled = !!(fs.querySelector('input:checked')
        || Array.from(fs.querySelectorAll('input, textarea, select'))
             .some(e => (e.value || '').trim()));
    } else {
      let el = null;
      const forId = l.getAttribute('for');
      if (forId) el = document.getElementById(forId);
      if (!el) el = l.querySelector('input, textarea, select');
      if (el) {
        if (el.type === 'checkbox' || el.type === 'radio') {
          filled = el.checked || !!(el.name && document.querySelector(
            'input[name="' + el.name.replace(/"/g, '\\"') + '"]:checked'));
        } else {
          let v = (el.value || '').trim();
          if (!v) {
            const ctl = el.closest('[class*="control"]');
            const sv = ctl && ctl.querySelector('[class*="single-value"], [class*="multi-value__label"]');
            if (sv) v = (sv.innerText || '').trim();
          }
          filled = !!v;
        }
      }
    }
    if (filled === true) return;
    if (!out.includes(name)) out.push(name);
  });
  return out;
}"""

# Visible client-side validation messages after a rejected submit.
_VALIDATION_JS = """() => Array.from(document.querySelectorAll(
    '[class*="error" i]:not(input):not(textarea), [role="alert"]'))
    .map(e => (e.innerText || '').replace(/\\s+/g, ' ').trim())
    .filter(t => t && t.length < 200).slice(0, 10)"""


def _find_submit_button(frame):
    for pat in _SUBMIT_PATTERNS:
        try:
            btn = frame.get_by_role("button", name=re.compile(pat, re.I)).first
            if btn.count() and btn.is_visible():
                return btn
        except Exception:
            continue
    try:  # classic forms: <input type=submit>
        btn = frame.locator('input[type="submit"], button[type="submit"]').first
        if btn.count() and btn.is_visible():
            return btn
    except Exception:
        pass
    return None


def _confirmation_evidence(page, frame) -> str:
    """Non-empty evidence string when the page shows a submission confirmation."""
    try:
        u = page.url or ""
        if re.search(r"confirmation|/thank", u, re.I):
            return f"url: {u}"
    except Exception:
        pass
    for fr in (frame, page.main_frame):  # the form frame may have navigated/detached
        try:
            text = fr.evaluate("() => document.body ? document.body.innerText : ''") or ""
            m = _CONFIRMATION_RX.search(text)
            if m:
                return f"page text: {m.group(0)!r}"
        except Exception:
            continue
    return ""


def _attempt_submit(page, frame, report: "ApplyReport", gate) -> None:
    """The armed submit path (decision 035). Pre-submit gate: every REQUIRED field must be
    filled AND the SafetyGate must allow it (armed + no profile/KILL file + under the
    per-run cap, re-checked immediately before the click). On any doubt the application is
    left UNSUBMITTED with the reason in `report.blockers` — a blocked outcome to record,
    never a prompt (decision 016's exception-queue model)."""
    import time

    missing = [s.split(" — ")[0] for s in report.skipped if "REQUIRED" in s]
    if frame is not None:
        # Belt and braces: live-scan the visible required labels whose controls are still
        # EMPTY in the DOM — catches a required field that was captured as "no saved
        # answer" (never REQUIRED-tagged) before we click anything. A label we filled by
        # record (report.filled) is trusted even if its widget hides the committed value.
        try:
            filled = {f.label for f in report.filled}
            for r in frame.evaluate(_UNMET_REQUIRED_JS, _scope_prefix(frame)):
                if r not in missing and not any(r == f or r in f for f in filled):
                    missing.append(r)
        except Exception:
            pass
    if missing:
        report.submit_state = "blocked"
        report.blockers = ["unresolved required field(s): " + "; ".join(missing)]
        return
    ok, reason = gate.may_submit()  # kill switch / cap checked at the last possible moment
    if not ok:
        report.submit_state = "blocked"
        report.blockers = [reason]
        return
    btn = _find_submit_button(frame)
    if btn is None:
        report.submit_state = "blocked"
        report.blockers = ["no submit button found on the form"]
        return
    try:
        btn.click(timeout=5000)
    except Exception as e:
        report.submit_state = "blocked"
        report.blockers = [f"submit click failed: {type(e).__name__}: {e}"]
        return
    gate.record_submission()  # count the click, not the confirmation — conservative vs. the cap

    deadline = time.time() + 20
    while time.time() < deadline:
        evidence = _confirmation_evidence(page, frame)
        if evidence:
            report.submitted = True
            report.submit_state = "submitted"
            report.confirmation = evidence
            return
        try:
            errs = [t for t in frame.evaluate(_VALIDATION_JS) if t]
        except Exception:
            errs = []
        if errs:
            report.submit_state = "blocked"
            report.blockers = ["form rejected the submit: " + "; ".join(errs[:5])]
            return
        page.wait_for_timeout(500)

    # No confirmation text and no rejection. If the form is gone (or its frame detached on a
    # navigation), the submit almost certainly went through — mark it submitted-but-unconfirmed
    # so we NEVER risk a double submission. A form still sitting there means the click had no
    # observable effect: treat as not submitted.
    try:
        still_there = _count_fields(frame) >= 2
    except Exception:
        still_there = False
    if still_there:
        report.submit_state = "blocked"
        report.blockers = ["submit clicked but the form is still showing with no confirmation — "
                          "treated as NOT submitted; verify manually"]
    else:
        report.submitted = True
        report.submit_state = "unconfirmed"
        report.confirmation = ("form disappeared after the submit click; no explicit confirmation "
                               "text found — verify via the confirmation email")


def _show_done_banner(page, report: "ApplyReport", ok: bool = True) -> None:
    """Inject a fixed overlay so the watching user gets a clear, visible signal — green when
    fields were filled, red when the form didn't load or nothing filled (with the reason)."""
    attention = len(report.skipped) + len(report.errors)
    failed = (not ok) or len(report.filled) == 0
    if failed:
        reason = report.errors[0] if report.errors else "No fillable fields were found on the page."
        mark, color = "⚠", "#b21f2d"
        msg = f"ApplicationBot could not fill this application. {reason} DRY RUN — nothing submitted."
    elif report.submitted:
        mark, color = "✓", "#0b7a3b"
        msg = (f"ApplicationBot SUBMITTED this application ({report.submit_state}). "
               f"{report.confirmation}")
    elif report.submit_state == "blocked":
        mark, color = "⚠", "#a05a00"
        msg = ("Armed, but NOT submitted — "
               + (report.blockers[0] if report.blockers else "blocked") )
    else:
        mark, color = "✓", "#0b7a3b"
        msg = (f"ApplicationBot finished filling — {len(report.filled)} field(s) filled"
               + (f", {attention} need your attention" if attention else "")
               + ". DRY RUN — nothing submitted. Review here, then finish.")
    try:
        page.evaluate(
            """([msg, mark, color]) => {
              document.getElementById('applicationbot-banner')?.remove();
              const b = document.createElement('div');
              b.id = 'applicationbot-banner';
              b.textContent = mark + ' ' + msg;
              b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                + 'background:' + color + ';color:#fff;font:600 15px/1.5 system-ui,sans-serif;'
                + 'padding:12px 18px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3)';
              document.body.appendChild(b);
              document.body.style.scrollMarginTop = '48px';
            }""",
            [msg, mark, color],
        )
    except Exception:
        pass  # a cosmetic banner must never break the run


def _persist_learning(resolver: AnswerResolver, report: "ApplyReport", profile_path: str) -> None:
    """Save AI-drafted answers and capture new reusable questions to the answer bank."""
    from . import apply_profile
    saved = apply_profile.remember_answers(resolver.learned, profile_path) if resolver.learned else 0
    # Persist dropdown option mappings Claude resolved this run, so the same value matches
    # instantly next time without another Claude call (decision 033).
    aliased = apply_profile.remember_dropdown_aliases(resolver.learned_options, profile_path)
    # Capture new REUSABLE questions we genuinely couldn't answer, so the user fills each once.
    # Only "no saved answer" gaps qualify — a "no dropdown option matched" / "unsupported field"
    # gap means we HAD an answer, so capturing it blank would be wrong. Skip company-specific,
    # demographic, and anything that names the company (that answer doesn't generalize).
    company = (resolver.company or "").strip().lower()
    pending = []
    for s in report.skipped:
        parts = s.split(" — ", 1)
        q = parts[0].strip()
        reason = parts[1].strip().lower() if len(parts) > 1 else ""
        if not reason.startswith("no saved answer"):
            continue
        if len(q) <= 12 or answer_bank.is_company_specific(q) or answer_bank.is_demographic(q):
            continue
        if company and company in q.lower():
            continue
        pending.append(q)
    captured = apply_profile.capture_questions(pending, profile_path, meta=report.captured) if pending else 0
    if saved or captured or aliased:
        report.skipped.append(
            f"[answer bank] saved {saved} AI-drafted answer(s), learned {aliased} dropdown "
            f"mapping(s), captured {captured} new question(s) for you to answer once in the Apply-profile tab")


def _title_role_company(title: str) -> tuple[str, str]:
    """Best-effort (role, company) from a posting's page title, e.g.
    'Job Application for Senior Software Engineer at Censys' → ('Senior Software Engineer',
    'Censys'). Returns ('', '') when the title doesn't match — the row's role/company stay
    blank for the user (or the Discover stage) to fill."""
    t = (title or "").strip()
    m = re.search(r"(?:job application for\s+)?(.+?)\s+at\s+(.+?)\s*$", t, re.I)
    if not m:
        return "", ""
    return m.group(1).strip(), m.group(2).strip()


def _report_snapshot(report: ApplyReport) -> dict:
    """The fill outcome as plain data, for the per-application archive (decision 043)."""
    from datetime import datetime
    return {
        "when": datetime.now().isoformat(timespec="seconds"),
        "url": report.url, "ats": report.ats,
        "submitted": report.submitted, "submit_state": report.submit_state,
        "confirmation": report.confirmation, "blockers": report.blockers,
        "pages": report.pages,
        "filled": [{"label": f.label, "value": f.value, "source": f.source}
                   for f in report.filled],
        "skipped": report.skipped,
        "errors": report.errors,
    }


def _record_run(report: ApplyReport, resume_pdf: str, role: str, company: str,
                meta: Optional[dict] = None) -> tuple[int, str]:
    """Record this run in the tracker (decision 024): a real submission becomes an `applied`
    row (method `auto`; tracker stamps date_applied), everything else stays the Track stage's
    'record what it WOULD submit' `dry-run` row. Basic info (company, role, location, remote,
    pay, source URL) comes from the discovered posting via `meta` when available — the reliable
    source — falling back to the page-title-derived role/company for a bare CLI run against a
    URL. Upserts by source URL so re-running a posting updates its row instead of duplicating
    it, and never clobbers user-owned fields (status/notes) on a re-run — except upgrading
    status to `applied` when we really submitted. Returns (id, 'recorded' | 'updated')."""
    from . import tracker  # lazy — keep apply.py importable without touching the DB

    status = "applied" if report.submitted else "dry-run"
    method = "auto" if report.submitted else "dry-run"
    m = meta or {}
    company = (m.get("company") or company or "").strip()
    role = (m.get("role") or role or "").strip()
    location = (m.get("location") or "").strip()
    remote = (m.get("remote") or "").strip()
    pay = (m.get("pay") or "").strip()
    # Key the tracker on the POSTING url (for dedup), not the ATS form/embed url.
    source_url = (m.get("source_url") or report.url or "").strip()

    existing = tracker.find_by_source_url(source_url)
    if existing:
        # Runner-owned refresh; fill any basic-info field only if it was never set (don't
        # clobber user edits). A real submission DOES upgrade the status — that's runner-owned
        # truth, not a user edit (tracker stamps date_applied on the flip).
        changes: dict = {"resume_path": resume_pdf, "portal": report.ats, "method": method}
        if m.get("fit_score") is not None:
            changes["fit_score"] = m["fit_score"]  # runner-owned: this run's judge verdict
        if report.submitted:
            changes["status"] = "applied"
        for col, val in (("company", company), ("role", role), ("location", location),
                         ("remote", remote), ("pay", pay)):
            if val and not existing.get(col):
                changes[col] = val
        tracker.update_application(int(existing["id"]), changes)
        return int(existing["id"]), "updated"

    native = sum(1 for f in report.filled if f.source == "native")
    drafted = sum(1 for f in report.filled if f.source == "generated")
    what = "Submitted" if report.submitted else "Dry-run"
    blocked = f" BLOCKED: {report.blockers[0]}" if report.blockers else ""
    app_id = tracker.add_application({
        "company": company, "role": role, "location": location, "remote": remote, "pay": pay,
        "portal": report.ats, "method": method, "status": status,
        "fit_score": m.get("fit_score"),
        "source_url": source_url, "resume_path": resume_pdf,
        "notes": f"[auto] {what}: {len(report.filled)} field(s) filled "
                 f"({native} native, {drafted} AI-drafted); {len(report.skipped)} need attention."
                 + blocked,
    })
    return app_id, "recorded"


def run_apply(
    url: str,
    resume_pdf: str,
    resolver: AnswerResolver,
    *,
    headed: bool = True,
    slow_mo: int = 350,
    pause: bool = True,
    screenshot: str = "apply_review.png",
    timeout_ms: int = 30000,
    debug: bool = False,
    profile_path: str = "profile/application_profile.yaml",
    learn: bool = True,
    record: bool = True,
    hold: "object | None" = None,
    on_filled: "object | None" = None,
    meta: Optional[dict] = None,
    gate: "object | None" = None,
) -> ApplyReport:
    """Fill an application form; DRY-RUN unless an armed SafetyGate is passed.

    `gate` (applicationbot.safety.SafetyGate, decision 035) is the ONLY path to a real
    submission: when armed and allowed (no kill file, under the per-run cap) and every
    REQUIRED field resolved, the driver clicks submit and verifies the confirmation.
    Without a gate — or armed-but-blocked — behaviour is the dry-run below.

    Native-first: uploads the résumé, triggers the ATS's own autofill (MyGreenhouse with stored
    credentials; resume-parse on Lever/Ashby/Workday), then our resolver fills only the fields
    still empty — drafting open-ended questions with Claude when enabled. Screenshots the result
    and (when `pause`) leaves the browser open for review. `headed=True` + `slow_mo` let you
    watch it fill in real time. When `learn`, new reusable answers/questions are saved to the
    answer bank for future runs (decision 018). When `record`, the run is logged to the tracker
    as a `dry-run` row, upserted by source URL (decision 024)."""
    from playwright.sync_api import TimeoutError as PWTimeout  # lazy
    from playwright.sync_api import sync_playwright

    ats = detect_ats(url)
    report = ApplyReport(url=url, ats=ats)
    done: set = set()  # labels already handled — dedupe across passes + required scan

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=not headed, slow_mo=slow_mo)
        except Exception as e:
            raise RuntimeError(
                "Could not launch Chromium. Install it once with: playwright install chromium\n"
                f"({e})"
            ) from e
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(url, wait_until="domcontentloaded")

            # Verify the application form has actually loaded before touching anything — ATS
            # forms render their fields via JS after domcontentloaded (and are often inside an
            # embedded iframe), so filling too early or on the wrong frame fills nothing. This
            # reveals the form, waits for it, and returns the FRAME it lives in + the ATS
            # re-derived from that frame (e.g. a Greenhouse form embedded on stripe.com).
            form_loaded, frame, ats = _open_application_form(page, ats, report)
            report.ats = ats  # re-derived from the form's frame (e.g. greenhouse embedded on stripe.com)

            # Role + company from the page title: grounds Claude-drafted answers AND labels
            # the tracker row.
            role, company = "", ""
            try:
                role, company = _title_role_company(page.title())
            except Exception:
                pass
            if resolver.enable_generation and not resolver.company:
                resolver.company = company or None

            if debug:
                _dump_fields(frame)

            if form_loaded:
                _upload_resume(frame, resume_pdf, report)

                # ---- native autofill FIRST (decision 017) ----
                if ats == "greenhouse":
                    _greenhouse_native_autofill(page, ctx, resolver.profile, report)
                _trigger_native_autofill(frame, ats, report)  # resume-parse button (Workday etc.)
                page.wait_for_timeout(1500)  # let any parse-on-upload settle

                # ---- our resolver fills only what's still empty, page by page, in the
                # form's frame — walks Next/Continue wizards to the final page ----
                frame = _fill_all_pages(page, frame, resolver, report, done, resume_pdf)
            # else: report carries an actionable "form did not load" error; skip filling.

            if form_loaded and gate is not None and getattr(gate, "armed", False):
                # ARMED (decision 035): pre-submit gate + kill-switch check + submit +
                # confirmation detection. Any doubt leaves report.submitted False with the
                # reason in report.blockers.
                _attempt_submit(page, frame, report, gate)
            else:
                report.submitted = False  # DRY-RUN — the default (Guideline #3)
                if form_loaded:
                    # Probe (never click) the submit control so every dry-run live-validates
                    # the armed path's selectors for free — no tokens, no risk.
                    try:
                        btn = _find_submit_button(frame)
                        report.submit_probe = (
                            "found: " + repr((btn.evaluate("el => el.innerText || el.value || ''")
                                              or "").strip()) if btn is not None
                            else "NOT FOUND — an armed run could not submit this form")
                    except Exception:
                        pass
            _show_done_banner(page, report, ok=form_loaded)  # visible signal in the browser

            try:
                page.screenshot(path=screenshot, full_page=True)
                report.screenshot = screenshot
            except Exception as e:
                report.errors.append(f"screenshot: {e}")

            # Grow the answer bank for future runs (decision 018): cache AI-drafted answers, and
            # capture new reusable (non-company-specific) questions we couldn't answer as blanks.
            if learn:
                _persist_learning(resolver, report, profile_path)

            # Record this dry-run in the tracker (Track stage, decision 024). Best-effort:
            # a tracker failure must not break the fill run.
            if record:
                try:
                    rid, action = _record_run(report, resume_pdf, role, company, meta)
                    kind = "applied" if report.submitted else "dry-run"
                    print(f"Tracked: {action} application #{rid} ({kind}) — open the Track tab to view/edit.")
                except Exception as e:
                    report.errors.append(f"tracker: {type(e).__name__}: {e}")
                    print(f"Note: could not record to tracker ({type(e).__name__}: {e}).")
                # Archive the run (decision 043): posting text + exact PDF + fill outcome,
                # frozen on a real submission. Best-effort, like the tracker.
                try:
                    from . import archive
                    m = meta or {}
                    path = archive.archive_run(
                        m.get("company") or company, m.get("role") or role, m,
                        jd_text=m.get("jd_body", ""), pdf_path=resume_pdf,
                        report_data=_report_snapshot(report), submitted=report.submitted)
                    print(f"Archived: {path}")
                except Exception as e:
                    report.errors.append(f"archive: {type(e).__name__}: {e}")
                    print(f"Note: could not archive the run ({type(e).__name__}: {e}).")

            # Show the result in the terminal the moment filling finishes, before the pause.
            print("\n" + report.summary())
            if on_filled is not None:
                try:
                    on_filled(report)  # let a UI surface the result before we hold/close
                except Exception:
                    pass

            if pause:
                if hold is not None:
                    # Web-driven: wait until the caller (e.g. a "Finish" button) releases us,
                    # instead of blocking on terminal input which has no TTY in a server.
                    hold.wait()
                else:
                    done_note = ("SUBMITTED" if report.submitted
                                 else "DRY RUN — not submitted")
                    try:
                        input(f"\n✓ Done ({done_note}). Review the browser, "
                              "then press Enter to close… ")
                    except EOFError:
                        pass
        finally:
            ctx.close()
            browser.close()

    return report


run_greenhouse = run_apply  # back-compat alias


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .resume import load_resume

    parser = argparse.ArgumentParser(
        description="DRY-RUN: fill an application form, native-autofill first (never submits)."
    )
    parser.add_argument("url", help="The application URL (Greenhouse/Lever/Ashby/Workday).")
    parser.add_argument("--pdf", required=True, help="Path to the résumé PDF to upload.")
    parser.add_argument("--resume", default="profile/resume.yaml", help="Résumé YAML (for contact).")
    parser.add_argument("--profile", default=None, help="Apply-profile YAML (defaults to profile/application_profile.yaml).")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser.")
    parser.add_argument("--no-pause", action="store_true", help="Don't pause for review at the end.")
    parser.add_argument("--slow-mo", type=int, default=350, help="ms between actions (watch it fill).")
    parser.add_argument("--screenshot", default="apply_review.png")
    parser.add_argument("--debug", action="store_true",
                        help="Print every form control the driver sees (tag/type/role/label) — "
                        "use when a field won't fill to reveal the live DOM.")
    parser.add_argument("--no-generate", action="store_true",
                        help="Don't draft open-ended answers with Claude (bank/structured only).")
    parser.add_argument("--no-learn", action="store_true",
                        help="Don't save new answers/questions to the answer bank.")
    parser.add_argument("--no-record", action="store_true",
                        help="Don't record this dry-run in the tracker (applications.db).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run even if profile/safety.yaml is armed.")
    args = parser.parse_args(argv)

    ats = detect_ats(args.url)
    print(f"ATS: {ats}" + ("" if ats != "generic" else " (unrecognized — using generic autofill)"))

    # Safety switch (decision 035): armed state comes from profile/safety.yaml; the KILL
    # file halts submission; --dry-run overrides both to disarmed.
    from .safety import load_gate
    gate = None if args.dry_run else load_gate()
    if gate is not None and gate.armed:
        print("⚠ ARMED (profile/safety.yaml) — this run WILL SUBMIT if all required fields "
              "resolve. Create profile/KILL or pass --dry-run to stop.")

    from . import backends
    generate = not args.no_generate and backends.claude_code_available()
    if not args.no_generate and not generate:
        print("Note: Claude Code CLI not found — open-ended answers won't be drafted "
              "(bank/structured answers still work). Sign in with `claude` to enable.")
    profile_path = args.profile or "profile/application_profile.yaml"

    resolver = AnswerResolver(
        resume=load_resume(args.resume),
        profile=load_profile(profile_path),
        enable_generation=generate,
    )
    run_apply(  # prints its own summary before the review pause
        args.url, args.pdf, resolver,
        headed=not args.headless, pause=not args.no_pause,
        slow_mo=args.slow_mo, screenshot=args.screenshot, debug=args.debug,
        profile_path=profile_path, learn=not args.no_learn, record=not args.no_record,
        gate=gate,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

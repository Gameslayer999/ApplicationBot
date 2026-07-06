"""Apply stage — a per-ATS form-filling adapter (Greenhouse first).

Split into two layers:
  * AnswerResolver — pure, fully testable: given a form field's label, returns the value
    from the résumé contact + apply profile + saved answer bank (or None = "can't answer",
    a logged exception rather than a blocking prompt — decision 016).
  * run_greenhouse — a thin, defensive Playwright driver. DRY-RUN by default: it fills the
    form, uploads the PDF, screenshots it, and PAUSES for review — it never clicks submit
    (Guideline #3: never submit against a real posting in development).

The Playwright browser (Chromium) is a separate one-time install: `playwright install
chromium`. The resolver needs no browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import answer_bank
from .apply_profile import QA, ApplicationProfile, load_profile
from .models import Resume


# --------------------------------------------------------------------------- report


@dataclass
class FilledField:
    label: str
    value: str
    control: str = "text"  # text | select | combobox | radio | file
    source: str = "resolver"  # resolver | native (ATS autofill) | generated (Claude draft)


@dataclass
class ApplyReport:
    url: str
    ats: str = "greenhouse"
    filled: list[FilledField] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # fields we couldn't answer
    errors: list[str] = field(default_factory=list)
    screenshot: Optional[str] = None
    submitted: bool = False
    native_autofill: Optional[str] = None  # which native autofill ran (e.g. "greenhouse: MyGreenhouse")

    def summary(self) -> str:
        native = sum(1 for f in self.filled if f.source == "native")
        generated = sum(1 for f in self.filled if f.source == "generated")
        lines = [f"Apply report — {self.ats} — {self.url}", f"  submitted: {self.submitted}"]
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
class AnswerResolver:
    resume: Resume
    profile: ApplicationProfile
    # Claude drafting of open-ended questions (decision 018), off unless enabled by the caller.
    enable_generation: bool = False
    company: Optional[str] = None
    jd: Optional[str] = None
    model: Optional[str] = None
    learned: list = field(default_factory=list)  # generated Q&A to persist after the run
    learned_options: dict = field(default_factory=dict)  # value -> [option texts] learned this run

    def learned_option_hints(self, value: Optional[str]) -> list[str]:
        """Dropdown options this value has matched before (learned across runs), so a repeat
        encounter matches instantly without another Claude call (decision 033)."""
        if not value:
            return []
        key = " ".join(value.lower().split())
        return list(self.profile.dropdown_aliases.get(key, [])) + list(self.learned_options.get(key, []))

    def learn_option(self, value: Optional[str], chosen: str) -> None:
        """Record that `value` matched dropdown option `chosen` (persisted after the run)."""
        if not (value and chosen):
            return
        key = " ".join(value.lower().split())
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
        if _has(n, "authorized to work", "legally authorized", "work authorization", "eligible to work"):
            return _yn(p.work_authorized)
        if _has(n, "sponsor", "sponsorship", "require sponsorship", "visa sponsorship", "need sponsorship"):
            return _yn(p.requires_sponsorship)
        # Citizenship — also answers "confirm you are a US citizen located in the US" gates,
        # since those are Yes/No and citizenship is the binding requirement.
        if _has(n, "citizen"):
            return _yn(p.us_citizen)
        if _has(n, "relocate", "willing to relocate"):
            return _yn(p.willing_to_relocate)
        if _has(n, "remote", "work remotely"):
            return _yn(p.open_to_remote)

        # "Are you currently located in <place>?" / "Do you live in <place>?" — a Yes/No, answered
        # by comparing the named place to where the applicant IS (country + location); NOT a place
        # to enter. Before the location/country rules so it isn't answered with the applicant's own
        # city/country (e.g. "located in Japan?" was wrongly answered "United States").
        mloc = re.search(r"\b(?:located|based|residing|reside|living|live)\s+in\s+(.+)$", n)
        if mloc and re.match(r"(are|do|does|will|is|have|currently)\b", n):
            return "Yes" if self._place_matches_applicant(mloc.group(1)) else "No"

        # Location / country — after work-eligibility so a Yes/No question that merely mentions
        # "location"/"country" isn't answered with a place. "Country" is checked first so a
        # "country where you reside" question resolves to the country, not the city.
        if _has(n, "country"):
            return p.country or None
        # NOTE: match "city" only as a whole word — as a bare substring it hits "ethni-CITY"
        # (and "simpli-city"), which wrongly answered "Race/Ethnicity" with the applicant's city.
        if _has(n, "location", "current location", "where are you based",
                "reside", "residence", "where do you live", "where you live", "based out of") \
                or re.search(r"\bcity\b", n):
            return p.location or c.location or None

        # Logistics
        if _has(n, "salary", "compensation expectation", "desired pay", "expected compensation"):
            return p.desired_salary or None
        if _has(n, "start date", "available to start", "notice period", "when can you start"):
            return p.earliest_start_date or None
        if _has(n, "years of experience", "years experience"):
            return p.years_experience or None

        # Voluntary EEO
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
        if _has(n, "veteran"):
            return p.veteran_status or None
        if _has(n, "disability"):
            return p.disability_status or None

        # Facts derivable from the résumé — answer them instead of capturing them blank for
        # the user (they were showing up as "needs your answer" despite being on the résumé).
        recent = self._current_experience()
        edu = self.resume.education[0] if self.resume.education else None
        if recent and _has(n, "current employer", "previous employer", "current company",
                           "recent employer", "name of your employer", "current or previous employer"):
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
            "desired_salary": p.desired_salary or None,
            "earliest_start_date": p.earliest_start_date or None,
            "years_experience": p.years_experience or None,
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
        key = answer_bank.classify_question(label, model=self.model)
        if not key:
            return None
        ans = self.answer_for_type(key)
        if ans is None:
            return None  # matched a type but the profile field is unset — leave for the user
        self.learned.append(QA(question=label, answer="", maps_to=key, generated=True))
        return ans

    def option_hints(self, label: str) -> Optional[list[str]]:
        """Ranked substrings to match against a dropdown's options when the free-text answer
        won't match one directly. "How did you hear about this job?" is often a dropdown whose
        options vary by company — since we discover roles via online search, prefer
        online/job-board/company-site options, then a generic bucket."""
        n = _norm(label)
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
        return None

    def freetext_answer(self, label: str, is_textarea: bool = False) -> tuple[Optional[str], str]:
        """Answer a free-text field. Banked/structured answer first; else, for open-ended
        questions, a grounded Claude draft (cached for reuse unless company-specific). Returns
        (answer, source) where source is 'resolver' | 'generated' | '' (couldn't answer)."""
        value = self.resolve(label)
        if value is not None:
            return value, "resolver"
        if not self.enable_generation or not answer_bank.is_open_ended(label, is_textarea):
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
        frame.locator('input[type="file"]').first.set_input_files(resume_pdf)
        report.filled.append(FilledField("Resume", resume_pdf, "file"))
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


def _claude_pick_click(page, opts, texts, label, value, resolver) -> Optional[str]:
    """Ask Claude to pick the best option from `texts` and click it by index, learning the
    mapping. Returns the chosen text or None. `opts`/`texts` must come from the SAME open."""
    if not texts:
        return None
    chosen = answer_bank.pick_dropdown_option(label, value, texts, model=resolver.model)
    if not chosen:
        return None
    for i, t in enumerate(texts):
        if t == chosen:
            opts.nth(i).click(timeout=4000)
            resolver.learn_option(value, chosen)
            return chosen
    return None


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
                   resolver=None, label: str = "") -> Optional[str]:
    """react-select combobox: commit the option matching the answer (or a ranked hint). Returns
    the committed option text, or None. Never leaves uncommitted typed text (which would look
    filled but submit as an invalid/empty selection) — clears the field if nothing selected.

    (1) On the FIRST open, literal-match any candidate (answer + hints + LEARNED aliases). If no
    match and it's a static list (options already shown), let Claude pick from those FRESH
    options and LEARN it — done here, before any typing pollutes the react-select filter.
    (2) Otherwise type each candidate to filter a searchable list; if a typed filter yields
    options but none literally match, let Claude pick from them and learn. Learned mappings make
    the same value match instantly next time without another Claude call (decision 033)."""
    learned = resolver.learned_option_hints(value) if resolver is not None else []
    candidates = [w for w in ([value] if value else []) + (hints or []) + learned if w]
    use_claude = resolver is not None and getattr(resolver, "enable_generation", False) and bool(value)

    # Phase 1 — literal match on the options shown on open; then Claude-pick for static lists.
    if _open_combobox(page, loc):
        page.wait_for_timeout(250)
        opts, texts = _open_options_and_texts(page)
        for want in candidates:
            for i, t in enumerate(texts):
                if _matches(t, want):
                    opts.nth(i).click(timeout=4000)
                    return t
        if use_claude and len(texts) >= 3:  # static list fully shown — pick now, before pollution
            chosen = _claude_pick_click(page, opts, texts, label, value, resolver)
            if chosen:
                return chosen
        try:
            loc.press("Escape")  # close so Phase 2 reopens/types cleanly
        except Exception:
            pass

    # Phase 2 — type each candidate to filter a searchable list (literal match).
    for want in candidates:
        chosen = _combo_try(page, loc, want)
        if chosen:
            return chosen

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
            opts, texts = _open_options_and_texts(page)
            chosen = _claude_pick_click(page, opts, texts, label, value, resolver)
            if chosen:
                return chosen

    # Phase 2c — no Claude (or it declined): best-effort substring match on the shortened queries
    # for a comma-free name value (e.g. a school), so the field still fills when generation is off.
    # NOT learned — this is an unvetted first-substring pick (it can land on a non-primary campus),
    # not a confirmed mapping, so we don't persist it.
    if value and "," not in value:
        for q in _search_queries(value)[1:]:  # [0] == value, already tried in Phase 2
            chosen = _combo_try(page, loc, q)
            if chosen:
                return chosen

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
            report.skipped.append(f"{label} — no saved answer")
            done.add(label)
            continue
        try:
            # Same question can be a dropdown or a text box depending on the company —
            # dispatch on the control type discovered live.
            if role == "combobox":
                chosen = _fill_combobox(page, loc, value, hints, resolver=resolver, label=label)
                if chosen:
                    report.filled.append(FilledField(label, chosen, "combobox"))
                else:
                    report.skipped.append(f"{label} — no dropdown option matched {value!r}")
            elif tag == "select":
                report.filled.append(FilledField(label, _fill_select(loc, value, hints), "select"))
            elif is_free:
                # Banked/structured answer, else a grounded Claude draft for open-ended questions.
                ans, source = resolver.freetext_answer(label, is_textarea=(tag == "textarea"))
                if ans is None:
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
        if value is None:  # semantic classify onto a known type (Claude, cached) on a miss
            value = resolver.resolve_semantic(q)
        if value is None:
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


def _show_done_banner(page, report: "ApplyReport", ok: bool = True) -> None:
    """Inject a fixed overlay so the watching user gets a clear, visible signal — green when
    fields were filled, red when the form didn't load or nothing filled (with the reason)."""
    attention = len(report.skipped) + len(report.errors)
    failed = (not ok) or len(report.filled) == 0
    if failed:
        reason = report.errors[0] if report.errors else "No fillable fields were found on the page."
        mark, color = "⚠", "#b21f2d"
        msg = f"ApplicationBot could not fill this application. {reason} DRY RUN — nothing submitted."
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
    captured = apply_profile.capture_questions(pending, profile_path) if pending else 0
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


def _record_dry_run(report: ApplyReport, resume_pdf: str, role: str, company: str,
                    meta: Optional[dict] = None) -> tuple[int, str]:
    """Record this dry-run in the tracker (decision 024) — the Track stage's 'record what it
    WOULD submit'. Basic info (company, role, location, remote, pay, source URL) comes from the
    discovered posting via `meta` when available — the reliable source — falling back to the
    page-title-derived role/company for a bare CLI run against a URL. Upserts by source URL so
    re-running a posting updates its row instead of duplicating it, and never clobbers
    user-owned fields (status/notes) on a re-run. Returns (id, 'recorded' | 'updated')."""
    from . import tracker  # lazy — keep apply.py importable without touching the DB

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
        # clobber user edits).
        changes: dict = {"resume_path": resume_pdf, "portal": report.ats, "method": "dry-run"}
        for col, val in (("company", company), ("role", role), ("location", location),
                         ("remote", remote), ("pay", pay)):
            if val and not existing.get(col):
                changes[col] = val
        tracker.update_application(int(existing["id"]), changes)
        return int(existing["id"]), "updated"

    native = sum(1 for f in report.filled if f.source == "native")
    drafted = sum(1 for f in report.filled if f.source == "generated")
    app_id = tracker.add_application({
        "company": company, "role": role, "location": location, "remote": remote, "pay": pay,
        "portal": report.ats, "method": "dry-run", "status": "dry-run",
        "source_url": source_url, "resume_path": resume_pdf,
        "notes": f"[auto] Dry-run: {len(report.filled)} field(s) filled "
                 f"({native} native, {drafted} AI-drafted); {len(report.skipped)} need attention.",
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
) -> ApplyReport:
    """DRY-RUN fill an application form. Never submits.

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

                # ---- our resolver fills only what's still empty, in the form's frame ----
                _fill_all_fields(frame, resolver, report, done, only_empty=True)
                _fill_radio_groups(frame, resolver, report, done)
                _flag_missing_required(frame, report, done)
            # else: report carries an actionable "form did not load" error; skip filling.

            report.submitted = False  # DRY-RUN — never submit in dev (Guideline #3)
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
                    rid, action = _record_dry_run(report, resume_pdf, role, company, meta)
                    print(f"Tracked: {action} application #{rid} (dry-run) — open the Track tab to view/edit.")
                except Exception as e:
                    report.errors.append(f"tracker: {type(e).__name__}: {e}")
                    print(f"Note: could not record to tracker ({type(e).__name__}: {e}).")

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
                    try:
                        input("\n✓ Done filling (DRY RUN — not submitted). Review the browser, "
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
    args = parser.parse_args(argv)

    ats = detect_ats(args.url)
    print(f"ATS: {ats}" + ("" if ats != "generic" else " (unrecognized — using generic autofill)"))

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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

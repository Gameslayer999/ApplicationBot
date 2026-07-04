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

    def _name_parts(self) -> tuple[str, str]:
        parts = (self.resume.contact.name or "").split()
        return (parts[0] if parts else ""), (parts[-1] if len(parts) > 1 else "")

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
        if _has(n, "location", "city", "current location", "where are you based"):
            return p.location or c.location or None
        if _has(n, "country"):
            return p.country or None

        # Work eligibility (Yes/No)
        if _has(n, "authorized to work", "legally authorized", "work authorization", "eligible to work"):
            return _yn(p.work_authorized)
        if _has(n, "sponsorship", "require sponsorship", "visa sponsorship", "need sponsorship"):
            return _yn(p.requires_sponsorship)
        # Citizenship — also answers "confirm you are a US citizen located in the US" gates,
        # since those are Yes/No and citizenship is the binding requirement.
        if _has(n, "citizen"):
            return _yn(p.us_citizen)
        if _has(n, "relocate", "willing to relocate"):
            return _yn(p.willing_to_relocate)
        if _has(n, "remote", "work remotely"):
            return _yn(p.open_to_remote)

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
        if _has(n, "race", "ethnicity"):
            return p.race_ethnicity or None
        if _has(n, "veteran"):
            return p.veteran_status or None
        if _has(n, "disability"):
            return p.disability_status or None

        # "How did you hear about this job?" — default reflects our online-search discovery.
        if _has(n, "how did you hear", "how did you find", "where did you hear",
                "referral source", "how were you referred"):
            return p.how_heard or None

        # AI-assistance disclosure — this pipeline uses AI to complete the form, so answer
        # truthfully "Yes" (the user authorizes answering it to keep runs autonomous).
        if "ai" in n.split() and _has(n, "application", "form") and \
                _has(n, "complete", "fill", "assist", "generate", "used", "use", "help"):
            return "Yes"

        # Saved answer bank for custom screening questions (conservative match)
        for qa in p.custom_answers:
            qn = _norm(qa.question)
            if qn and (qn == n or (len(qn) > 15 and (qn in n or n in qn))):
                return qa.answer or None

        return None

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
_REQUIRED_LABELS_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  const out = [];
  document.querySelectorAll('form label, form legend').forEach(l => {
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
    "() => { const labelOf = " + _LABEL_JS + ";"
    " const els = document.querySelectorAll('form input, form textarea, form select, form [role=combobox]');"
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
        rows = page.evaluate(_DUMP_JS)
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


def _trigger_native_autofill(page, ats: str, report: "ApplyReport") -> None:
    """Click the ATS's resume-parse autofill button if it exposes one (e.g. Workday's "Autofill
    with Resume"). Lever/Ashby parse on upload and need no click — the resume upload already
    happened, so their fields populate on their own before our only-empty pass."""
    for pat in _NATIVE_AUTOFILL_BUTTONS:
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=4000)
                report.native_autofill = f"{ats}: {pat.strip('^\\s*')} button"
                page.wait_for_timeout(2500)  # let the parse populate fields
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


def _upload_resume(page, resume_pdf: str, report: "ApplyReport") -> None:
    """Attach the résumé. Prefer Greenhouse's own "Attach" button through the file chooser —
    poking the raw hidden <input type=file> makes the site's onchange handler throw
    "Cannot read properties of undefined (reading 'uploadFile')"."""
    for name in (r"attach", r"upload"):
        try:
            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
            if btn.count() and btn.is_visible():
                with page.expect_file_chooser(timeout=4000) as fc:
                    btn.click()
                fc.value.set_files(resume_pdf)
                report.filled.append(FilledField("Resume", resume_pdf, "file"))
                return
        except Exception:
            continue
    try:  # classic/older forms expose a direct file input
        page.locator('input[type="file"]').first.set_input_files(resume_pdf)
        report.filled.append(FilledField("Resume", resume_pdf, "file"))
    except Exception as e:
        report.errors.append(f"resume upload: {type(e).__name__}: {e}")


def _matches(option: str, want: str) -> bool:
    """Case-insensitive: option equals/contains `want`, or (for non-tiny option text) is
    contained by it — so "online" matches both an "Online" option and a longer sentence."""
    o, w = option.strip().lower(), want.strip().lower()
    return bool(o) and bool(w) and (o == w or w in o or (len(o) >= 3 and o in w))


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
    if count > 3:  # 3) async search top suggestion
        opts.first.click(timeout=4000)
        return texts[0]
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


def _fill_combobox(page, loc, value: Optional[str], hints: Optional[list[str]] = None) -> Optional[str]:
    """react-select combobox: try the answer, then each ranked hint; return the committed option
    text, or None. Never leaves uncommitted typed text (which would look filled but submit as an
    invalid/empty selection) — clears the field if nothing could be selected."""
    for want in ([value] if value else []) + (hints or []):
        chosen = _combo_try(page, loc, want)
        if chosen:
            return chosen
    try:
        loc.fill("", timeout=2000)  # discard any typed-but-uncommitted text
    except Exception:
        pass
    return None


def _fill_all_fields(page, resolver: AnswerResolver, report: "ApplyReport", done: set,
                     only_empty: bool = True) -> None:
    controls = page.locator("form input, form textarea, form select")
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
                "role: (el.getAttribute('role')||'').toLowerCase()})"
            )
        except Exception:
            continue
        tag, typ, role = k["tag"], k["type"], k["role"]
        if typ in ("hidden", "submit", "button", "file", "checkbox", "radio"):
            continue  # radios are handled as groups below
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
        if value is None and not hints and not is_free:
            report.skipped.append(f"{label} — no saved answer")
            done.add(label)
            continue
        try:
            # Same question can be a dropdown or a text box depending on the company —
            # dispatch on the control type discovered live.
            if role == "combobox":
                chosen = _fill_combobox(page, loc, value, hints)
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
    radios = page.locator('form input[type="radio"]')
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
        required = page.evaluate(_REQUIRED_LABELS_JS)
    except Exception:
        return
    filled = {f.label for f in report.filled}
    for r in required:
        if r in filled or any(r == d or r in d for d in done):
            continue
        report.skipped.append(f"{r} — REQUIRED, not filled (no matching answer or unsupported field)")


def _show_done_banner(page, report: "ApplyReport") -> None:
    """Inject a fixed overlay into the page so the watching user gets a clear, visible "done
    filling" signal — with counts and an unmistakable DRY-RUN / not-submitted notice."""
    attention = len(report.skipped) + len(report.errors)
    msg = (f"ApplicationBot finished filling — {len(report.filled)} field(s) filled"
           + (f", {attention} need your attention" if attention else "")
           + ". DRY RUN — nothing submitted. Review here, then press Enter in the terminal.")
    try:
        page.evaluate(
            """(msg) => {
              document.getElementById('applicationbot-banner')?.remove();
              const b = document.createElement('div');
              b.id = 'applicationbot-banner';
              b.textContent = '✓ ' + msg;
              b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                + 'background:#0b7a3b;color:#fff;font:600 15px/1.5 system-ui,sans-serif;'
                + 'padding:12px 18px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3)';
              document.body.appendChild(b);
              document.body.style.scrollMarginTop = '48px';
            }""",
            msg,
        )
    except Exception:
        pass  # a cosmetic banner must never break the run


def _persist_learning(resolver: AnswerResolver, report: "ApplyReport", profile_path: str) -> None:
    """Save AI-drafted answers and capture new reusable questions to the answer bank."""
    from . import apply_profile
    saved = apply_profile.remember_answers(resolver.learned, profile_path) if resolver.learned else 0
    # New questions we couldn't answer — capture the reusable (non-company-specific) ones so the
    # user fills each once. Labels come off the "needs attention" list (strip the " — reason").
    pending = []
    for s in report.skipped:
        q = s.split(" — ")[0].strip()
        if (len(q) > 12 and not answer_bank.is_company_specific(q)
                and not answer_bank.is_demographic(q)):
            pending.append(q)
    captured = apply_profile.capture_questions(pending, profile_path) if pending else 0
    if saved or captured:
        report.skipped.append(
            f"[answer bank] saved {saved} AI-drafted answer(s), captured {captured} new "
            f"question(s) for you to answer once in the Apply-profile tab")


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
) -> ApplyReport:
    """DRY-RUN fill an application form. Never submits.

    Native-first: uploads the résumé, triggers the ATS's own autofill (MyGreenhouse with stored
    credentials; resume-parse on Lever/Ashby/Workday), then our resolver fills only the fields
    still empty — drafting open-ended questions with Claude when enabled. Screenshots the result
    and (when `pause`) leaves the browser open for review. `headed=True` + `slow_mo` let you
    watch it fill in real time. When `learn`, new reusable answers/questions are saved to the
    answer bank for future runs (decision 018)."""
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
            # Some pages need the application form revealed first.
            try:
                btn = page.get_by_role("link", name=re.compile(r"apply", re.I)).first
                if btn.is_visible(timeout=2000):
                    btn.click()
            except PWTimeout:
                pass
            except Exception:
                pass

            # Company name (from the page title) grounds any Claude-drafted answers.
            if resolver.enable_generation and not resolver.company:
                try:
                    m = re.search(r"\bat\s+(.+?)\s*$", (page.title() or "").strip())
                    resolver.company = m.group(1) if m else None
                except Exception:
                    pass

            if debug:
                _dump_fields(page)

            _upload_resume(page, resume_pdf, report)

            # ---- native autofill FIRST (decision 017) ----
            if ats == "greenhouse":
                _greenhouse_native_autofill(page, ctx, resolver.profile, report)
            _trigger_native_autofill(page, ats, report)  # resume-parse button (Workday etc.)
            page.wait_for_timeout(1500)  # let any parse-on-upload settle

            # ---- our resolver fills only what's still empty ----
            _fill_all_fields(page, resolver, report, done, only_empty=True)
            _fill_radio_groups(page, resolver, report, done)
            _flag_missing_required(page, report, done)

            report.submitted = False  # DRY-RUN — never submit in dev (Guideline #3)
            _show_done_banner(page, report)  # visible "done filling" signal in the browser

            try:
                page.screenshot(path=screenshot, full_page=True)
                report.screenshot = screenshot
            except Exception as e:
                report.errors.append(f"screenshot: {e}")

            # Grow the answer bank for future runs (decision 018): cache AI-drafted answers, and
            # capture new reusable (non-company-specific) questions we couldn't answer as blanks.
            if learn:
                _persist_learning(resolver, report, profile_path)

            # Show the result in the terminal the moment filling finishes, before the pause.
            print("\n" + report.summary())

            if pause:
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
        profile_path=profile_path, learn=not args.no_learn,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

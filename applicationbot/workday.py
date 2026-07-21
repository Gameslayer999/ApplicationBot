"""Workday adapter (decision 050) — deterministic fill of the standard application wizard via
Workday's **stable `data-automation-id` selectors**, the core of the agentic→deterministic
hybrid (Option C). Every Workday tenant runs the same shared widget system, so the automation
ids for the standard sections (legal name, contact, address, …) are identical across employers —
which means we can fill them by exact id with **no label matching and no Claude** (contrast the
generic resolver's fragile label derivation on Workday's custom-widget DOM).

`apply_workday` (the wire-in, decision 059) drives a Workday application end-to-end —
`start_application` (Apply → Apply Manually) → `ensure_account` (sign in, or create on the bot
email + verify via `mailbox`, decision 053) → `fill_wizard` (walk the pages filling standard text
fields + custom dropdowns by stable `data-automation-id`). `run_apply` routes Workday here instead
of the generic `_open_application_form`/`_fill_all_pages` path. Values come from the apply profile
+ résumé. **DRY-RUN by default (Guideline #3);** the final Submit is clicked only on the armed
path (M3, `_attempt_workday_submit`, gated by the SafetyGate exactly like decision 035).

Milestones: M1 deterministic login + standard fields · M2 (decisions 061/063) agentic fallback for
a tenant's custom pages + recipe distillation (learn once, replay deterministically) · M3 (decision
064) armed submit. The live-tuning surface (real-tenant button labels / automation ids) and the
Claude-over-MCP agent are verified against fixtures here; a real tenant is the flagged live step.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from .apply import FilledField

_AGENT_MODEL = "claude-sonnet-5"  # the agentic fallback's model (cheaper than Opus for form-filling)

# US state abbreviation → full name, so a profile location "New York, NY" can match a Workday
# state dropdown whose options are full state names.
_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

_NEXT_ID = "pageFooterNextButton"       # Workday's persistent wizard "Next" control
_SUBMIT_ID = "pageFooterSubmitButton"   # the final Submit — clicked ONLY on the armed path (M3, decision 064)
_MAX_WIZARD_PAGES = 8

# Account-screen automation ids (best-effort; matched by fixtures, live-validate on a real tenant).
_ACCT = {
    "email": "email",
    "password": "password",
    "verify_password": "verifyPassword",
    "create_checkbox": "createAccountCheckbox",
    "sign_in_btn": "signInSubmitButton",
    "create_btn": "createAccountSubmitButton",
    "create_toggle": "createAccountLink",
    "verify_code": "verificationCode",
    "verify_btn": "verifyEmailButton",
}

# Workday's stable data-automation-id → where its value comes from. Text inputs only in this
# brick; each id is the SAME across every Workday tenant (shared widget system). Ids we have no
# profile field for yet (street address, postal code — a known profile gap) are omitted rather
# than filled blank; custom dropdowns (country/state) are handled in the dropdown brick.
_STANDARD_TEXT_IDS = {
    "legalNameSection_firstName": "first_name",
    "legalNameSection_lastName": "last_name",
    "addressSection_city": "city",
    "email": "email",
    "phone-number": "phone",   # Workday's phone widget number input
    "phoneNumber": "phone",    # alt id some tenants render
}


def _split_name(profile, resume) -> tuple[str, str]:
    if profile.first_name or profile.last_name:
        return profile.first_name.strip(), profile.last_name.strip()
    full = ""
    if resume is not None and getattr(resume, "contact", None) is not None:
        full = (resume.contact.name or "").strip()
    parts = full.split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def standard_field_values(resume, profile) -> dict[str, str]:
    """The Workday standard-field automation-id → value map, profile-first then résumé, with
    empties dropped (a field we have no value for is left for the required-scan to flag, never
    filled blank). `city` is the first comma-segment of the profile location."""
    first, last = _split_name(profile, resume)
    contact = getattr(resume, "contact", None) if resume is not None else None
    email = (profile.email or (contact.email if contact else "") or "").strip()
    phone = (profile.phone or (getattr(contact, "phone", "") if contact else "") or "").strip()
    city = (profile.location or "").split(",")[0].strip()
    source = {"first_name": first, "last_name": last, "city": city, "email": email, "phone": phone}
    return {auto_id: source[key] for auto_id, key in _STANDARD_TEXT_IDS.items() if source.get(key)}


def _fill_text(frame, auto_id: str, value: str) -> bool:
    """Fill the input carrying (or wrapped by) `[data-automation-id=auto_id]`. Returns True if
    a matching input was found and filled. Never raises."""
    loc = frame.locator(f"[data-automation-id='{auto_id}']:visible").first
    try:
        if loc.count() == 0:
            return False
        tag = (loc.evaluate("el => el.tagName.toLowerCase()") or "")
    except Exception:
        return False
    target = loc
    if tag not in ("input", "textarea"):
        desc = loc.locator("input, textarea").first
        try:
            if desc.count() == 0:
                return False
        except Exception:
            return False
        target = desc
    try:
        target.fill(value, timeout=3000)
        return True
    except Exception:
        return False


def fill_standard_fields(frame, resume, profile, report) -> int:
    """Deterministically fill Workday's standard My-Information text fields by automation id.
    Records each on `report.filled` (source ``workday``) and returns the count filled. Dry-run
    safe — it only fills; nothing here submits."""
    filled = 0
    for auto_id, value in standard_field_values(resume, profile).items():
        if _fill_text(frame, auto_id, value):
            report.filled.append(FilledField(auto_id, value, "text", source="workday"))
            filled += 1
    return filled


# --------------------------------------------------------------------------- custom dropdowns

def _state_from_location(location: str) -> str:
    parts = [p.strip() for p in (location or "").split(",")]
    return parts[1] if len(parts) >= 2 else ""


def standard_dropdown_values(profile) -> dict[str, "tuple[str, tuple]"]:
    """Workday custom-dropdown automation-id → (value, hints). Country/state come from the
    profile location (state abbreviation expanded to its full name so it matches the option
    text); the Voluntary-Disclosures EEO dropdowns fill from the profile's self-ID fields.
    Empties dropped. Ids the tenant doesn't render simply no-op (count 0)."""
    out: dict[str, tuple] = {}
    if profile.country:
        out["addressSection_countryRegion"] = (
            profile.country, ("United States of America", "United States", "USA", "US"))
        out["country--country"] = out["addressSection_countryRegion"]  # alt id some tenants use
    state = _state_from_location(profile.location)
    if state:
        full = _US_STATES.get(state.upper(), "")
        out["addressSection_countryRegionSubdivision1"] = (state, (full,) if full else ())
    # Voluntary Disclosures (blank profile fields = decline, left unfilled).
    if profile.gender:
        out["gender"] = (profile.gender, ())
    if profile.race_ethnicity:
        out["ethnicity"] = (profile.race_ethnicity, ())
    if profile.veteran_status:
        out["veteranStatus"] = (profile.veteran_status, ())
    if profile.disability_status:
        out["disability-status"] = (profile.disability_status, ())
    return out


def _match_option(options: list[str], value: str, hints: tuple = ()) -> "int | None":
    """Index of the option best matching `value` (or a hint): exact case-insensitive first,
    then substring either direction. None if nothing matches."""
    if not value:
        return None
    cands = [value, *hints]
    low = [o.lower().strip() for o in options]
    for c in cands:
        cl = c.lower().strip()
        for i, o in enumerate(low):
            if o and o == cl:
                return i
    for c in cands:
        cl = c.lower().strip()
        for i, o in enumerate(low):
            if cl and o and (cl in o or o in cl):
                return i
    return None


def _fill_dropdown(page, auto_id: str, value: str, *, hints: tuple = ()) -> str:
    """Open the Workday custom dropdown at `auto_id`, click the option matching `value`/`hints`,
    and return the chosen option text ('' if not present or no match). Reads only the VISIBLE
    listbox (the just-opened menu), so multiple dropdowns on a page don't cross-contaminate.
    Never raises. Deterministic: options are read, matched in code, then clicked by exact index."""
    container = page.locator(f"[data-automation-id='{auto_id}']:visible").first
    try:
        if container.count() == 0:
            return ""
        btn = container.locator("button").first
        if btn.count() == 0:
            return ""
        btn.click(timeout=3000)
    except Exception:
        return ""
    opts = page.locator("[role=option]:visible")
    try:
        page.wait_for_selector("[role=option]:visible", timeout=2000)
        texts = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
    except Exception:
        texts = []
    idx = _match_option(texts, value, hints)
    if idx is None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return ""
    try:
        opts.nth(idx).click(timeout=3000)
        return texts[idx]
    except Exception:
        return ""


def fill_dropdowns(page, profile, report) -> int:
    """Fill Workday's standard custom dropdowns (country/state/EEO) deterministically. Records
    each on `report.filled` (control ``select``, source ``workday``); returns the count filled."""
    filled = 0
    for auto_id, (value, hints) in standard_dropdown_values(profile).items():
        chosen = _fill_dropdown(page, auto_id, value, hints=hints)
        if chosen:
            report.filled.append(FilledField(auto_id, chosen, "select", source="workday"))
            filled += 1
    return filled


# --------------------------------------------------------------------------- wizard navigation

def _page_signature(page) -> str:
    """A stable identity for the current wizard page: the md5 of its VISIBLE data-automation-id
    set (settled spec). Advancing the wizard swaps the visible set, so the signature changes;
    used both to detect advance and (later, M2) to key learned recipes."""
    try:
        ids = page.evaluate(
            "() => Array.from(document.querySelectorAll('[data-automation-id]'))"
            ".filter(e => e.offsetParent !== null)"
            ".map(e => e.getAttribute('data-automation-id')).sort().join('|')"
        )
    except Exception:
        ids = ""
    return hashlib.md5((ids or "").encode("utf-8")).hexdigest()


def _click_next(page) -> bool:
    """Click the VISIBLE 'Next' control. Returns False when there is none (final/Review page) —
    the caller then stops WITHOUT ever touching the Submit control (dry-run, Guideline #3)."""
    btn = page.locator(f"[data-automation-id='{_NEXT_ID}']:visible").first
    try:
        if btn.count() == 0:
            return False
        btn.click(timeout=3000)
        return True
    except Exception:
        return False


def fill_page(page, resume, profile, report) -> int:
    """Fill everything this adapter knows on the current wizard page: standard text fields +
    custom dropdowns. Returns the count filled on this page."""
    return fill_standard_fields(page, resume, profile, report) + fill_dropdowns(page, profile, report)


def fill_wizard(page, resume, profile, report, *, max_pages: int = _MAX_WIZARD_PAGES,
                resolver=None, agentic: bool = False, cdp_port=None, store_path=None,
                _agent_spawn=None) -> int:
    """Walk Workday's multi-page application wizard: fill each page (text + dropdowns), click
    Next, repeat until the Review/final page (no Next) or advance stalls. DRY-RUN — it NEVER
    clicks Submit. Sets `report.pages` to the number of pages walked; returns the total filled.
    Stops on a repeated page signature so a stuck wizard can't loop.

    When `resolver` is given (M2, decision 061), each page's UNRECOGNIZED custom questions are
    resolved after the deterministic fill: a learned recipe replays deterministically, and — only
    when `agentic` is on and still-unfilled fields remain — the agentic fallback fills + learns
    them. With no resolver this is pure M1 (unchanged)."""
    total = 0
    seen: set[str] = set()
    pages = 0
    while pages < max_pages:
        sig = _page_signature(page)
        if sig in seen:
            break  # looped back to an already-filled page — stop
        seen.add(sig)
        pages += 1
        total += fill_page(page, resume, profile, report)
        if resolver is not None:
            total += _resolve_unrecognized(page, resolver, report, agentic=agentic,
                                           cdp_port=cdp_port, store_path=store_path,
                                           _agent_spawn=_agent_spawn)
        if not _click_next(page):
            break  # Review/final page — never click Submit
        try:
            page.wait_for_timeout(400)
        except Exception:
            pass
        if _page_signature(page) == sig:
            break  # Next didn't advance (validation block on this page)
    report.pages = pages
    return total


# --------------------------------------------------------------------------- account create / sign-in

def generate_password(length: int = 16) -> str:
    """A strong random password that meets Workday's complexity (upper/lower/digit/symbol).
    Uses `secrets` — not for reproducibility, just to create one the user can retrieve later."""
    import secrets
    import string

    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*-_"]
    chars = [secrets.choice(p) for p in pools]
    everything = "".join(pools)
    chars += [secrets.choice(everything) for _ in range(max(length, 12) - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _click(page, auto_id: str, *, timeout: int = 3000) -> bool:
    loc = page.locator(f"[data-automation-id='{auto_id}']:visible").first
    try:
        if loc.count() == 0:
            return False
        loc.click(timeout=timeout)
        return True
    except Exception:
        return False


def _check(page, auto_id: str) -> bool:
    """Tick a Workday checkbox at (or wrapped by) auto_id. Best-effort; returns True if checked."""
    loc = page.locator(f"[data-automation-id='{auto_id}']:visible").first
    try:
        if loc.count() == 0:
            return False
        tag = (loc.evaluate("el => el.tagName.toLowerCase()") or "")
        cb = loc if tag == "input" else loc.locator("input[type=checkbox]").first
        if cb.count() == 0:
            return False
        cb.check(timeout=3000)
        return True
    except Exception:
        return False


def sign_in(page, account, report) -> bool:
    """Fill and submit the Workday sign-in form for a stored account. Returns True if the form
    was completed and the sign-in control clicked (not proof of a successful login — the caller
    verifies the next screen)."""
    if not (_fill_text(page, _ACCT["email"], account.email)
            and _fill_text(page, _ACCT["password"], account.password)):
        return False
    return _click(page, _ACCT["sign_in_btn"])


def create_account(page, email: str, password: str, report) -> bool:
    """Switch to Workday's create-account form (if a toggle is shown), fill email + password
    (+ verify-password and the terms checkbox when present), and click Create Account. Returns
    True if the form was completed and submitted."""
    _click(page, _ACCT["create_toggle"])  # no-op if the create form is already shown
    if not (_fill_text(page, _ACCT["email"], email)
            and _fill_text(page, _ACCT["password"], password)):
        return False
    _fill_text(page, _ACCT["verify_password"], password)  # absent on some tenants
    _check(page, _ACCT["create_checkbox"])                # terms/consent, when present
    return _click(page, _ACCT["create_btn"])


def _apply_verification(page, link_or_code: str, report) -> bool:
    """Complete email verification: open the link, or type the code and confirm. Best-effort."""
    if link_or_code.startswith("http"):
        try:
            page.goto(link_or_code, wait_until="domcontentloaded")
            return True
        except Exception:
            report.errors.append("Workday verification link failed to open.")
            return False
    if _fill_text(page, _ACCT["verify_code"], link_or_code):
        return _click(page, _ACCT["verify_btn"])
    return False


def ensure_account(page, tenant_url: str, profile, report, *, mailbox_config=None,
                   backend=None, index_path=None, verify_wait: int = 120) -> Optional["object"]:
    """Get onto the far side of Workday's account gate for a tenant. Assumes `page` is on the
    account screen (brick 5 navigates there). If an account is stored → sign in. Otherwise →
    create one with a generated password on the **bot-owned email** (so verification lands in the
    bot inbox; falls back to the profile email if no mailbox is configured), **store it
    immediately** (a password is never lost even if verification lags — settled spec), then
    complete email verification via `mailbox` when configured. Returns the `credentials.Account`
    used, or None on failure (with an actionable `report.errors` entry). Never submits anything."""
    from . import credentials

    if index_path is None:
        index_path = credentials.DEFAULT_INDEX
    tenant = credentials.tenant_of(tenant_url)
    acct = credentials.get_account(tenant, backend=backend, index_path=index_path)
    if acct is not None:
        if sign_in(page, acct, report):
            report.native_autofill = f"workday: signed in ({acct.email})"
            return acct
        report.errors.append(
            f"Workday sign-in failed for {tenant} — the saved password may be wrong; "
            f"run `python -m applicationbot.credentials get {tenant}` to check, or delete it to recreate.")
        return None

    account_email = (mailbox_config.email if mailbox_config is not None else "") or profile.email
    if not account_email:
        report.errors.append(
            "Workday needs an account but no email is available — set MAILBOX_EMAIL (bot inbox) "
            "or an email in the Profile tab.")
        return None
    password = generate_password()
    if not create_account(page, account_email, password, report):
        report.errors.append(f"Workday account-creation form could not be completed for {tenant}.")
        return None

    acct = credentials.Account(tenant=tenant, email=account_email, password=password)
    credentials.save_account(acct, backend=backend, index_path=index_path)  # store BEFORE verification — never lose it
    report.native_autofill = f"workday: created account ({account_email})"

    if mailbox_config is None:
        report.errors.append(
            "Workday account created and saved, but no bot mailbox is configured (set "
            "MAILBOX_IMAP_HOST/MAILBOX_EMAIL/MAILBOX_PASSWORD) — verify the email manually to activate it.")
        return acct
    from . import mailbox as mbox

    code_or_link = mbox.wait_for_verification(mailbox_config, timeout=verify_wait)
    if not code_or_link:
        report.errors.append(
            "Workday verification email not found within the wait — verify the bot inbox manually.")
    else:
        _apply_verification(page, code_or_link, report)
    return acct


# --------------------------------------------------------------------------- end-to-end wire-in

_APPLY_TEXTS = ("Apply Manually", "Apply", "Autofill with Resume")  # prefer the manual path
_RESUME_UPLOAD_IDS = ("select-files", "file-upload-input-ref", "resume")  # best-effort upload input


def _on_account_or_form(page) -> bool:
    """True once we're on Workday's account gate or an application page (a sign-in/create field
    or a wizard control is visible) — so navigation can stop clicking 'Apply'."""
    for aid in (_ACCT["email"], "legalNameSection_firstName", _NEXT_ID, _ACCT["create_btn"], _ACCT["sign_in_btn"]):
        try:
            if page.locator(f"[data-automation-id='{aid}']:visible").count() > 0:
                return True
        except Exception:
            pass
    return False


def start_application(page, report, *, max_clicks: int = 3) -> bool:
    """Click Workday's 'Apply' → 'Apply Manually' to reach the account/wizard screen. No-op if
    already there. Returns True if we end on an account/form screen. Best-effort — the exact
    button labels are the live-tuning surface; never raises."""
    for _ in range(max_clicks):
        if _on_account_or_form(page):
            return True
        clicked = False
        for text in _APPLY_TEXTS:
            try:
                btn = page.get_by_role("button", name=text, exact=True).first
                if btn.count() == 0:
                    btn = page.get_by_role("link", name=text, exact=True).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3000)
                    page.wait_for_timeout(600)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            break
    return _on_account_or_form(page)


def _upload_resume_if_present(page, resume_pdf: str, report) -> bool:
    """Attach the résumé PDF to a Workday file input if the current page exposes one. Best-effort;
    returns True if a file was set."""
    if not resume_pdf:
        return False
    for aid in _RESUME_UPLOAD_IDS:
        loc = page.locator(f"[data-automation-id='{aid}']").first
        try:
            if loc.count() == 0:
                continue
            inp = loc if (loc.evaluate("el => el.tagName.toLowerCase()") == "input") else loc.locator("input[type=file]").first
            if inp.count() == 0:
                continue
            inp.set_input_files(resume_pdf, timeout=5000)
            report.filled.append(FilledField("Resume", resume_pdf, "file", source="workday"))
            return True
        except Exception:
            continue
    return False


def apply_workday(page, url: str, resume, profile, report, *, resume_pdf: str = "",
                  mailbox_config=None, backend=None, index_path=None, resolver=None,
                  agentic: bool = False, cdp_port=None, store_path=None, _agent_spawn=None,
                  gate=None) -> bool:
    """M1 wire-in (decision 059): drive a Workday application end-to-end. Navigate to the account
    screen → `ensure_account` (sign in, or create + verify) → upload the résumé if a field is
    present → walk the standard wizard filling deterministic fields and dropdowns. Records progress
    on `report`; returns True if the wizard was reached and something filled, else False (with the
    reason in `report.errors`). Never raises.

    M2 (decision 061): pass `resolver` to also resolve each page's custom questions — a learned
    recipe replays deterministically, and (only when `agentic` is on) an agentic worker fills +
    learns any still-unrecognized fields via the browser's `cdp_port`.

    M3 (decision 064): **DRY-RUN unless an armed `gate` is passed.** When `fill_wizard` reaches the
    Review page and the SafetyGate is armed, `_attempt_workday_submit` clicks the final Submit
    behind the full safety architecture (required-field gate + kill switch + cap, decision 035).
    Without an armed gate nothing is ever submitted (Guideline #3)."""
    report.ats = "workday"
    try:
        if not start_application(page, report):
            report.errors.append(
                "Workday: could not reach the application form from this page (the 'Apply' / "
                "'Apply Manually' step wasn't found). Open the posting and start the application "
                "once, or check the URL.")
            return False
        acct = ensure_account(page, url, profile, report, mailbox_config=mailbox_config,
                              backend=backend, index_path=index_path)
        if acct is None:
            return False  # account gate not passed — report.errors already explains why
        _upload_resume_if_present(page, resume_pdf, report)
        filled = fill_wizard(page, resume, profile, report, resolver=resolver, agentic=agentic,
                             cdp_port=cdp_port, store_path=store_path, _agent_spawn=_agent_spawn)
        if gate is not None and getattr(gate, "armed", False):
            _attempt_workday_submit(page, report, gate)  # ARMED — may click the final Submit
        state = report.submit_state if report.submit_state != "dry-run" else "dry-run (not submitted)"
        note = f"workday: {filled} field(s) across {report.pages} page(s), {state}"
        report.native_autofill = (report.native_autofill + " · " + note) if report.native_autofill else note
        return filled > 0 or report.pages > 0
    except Exception as e:
        report.errors.append(f"Workday adapter error: {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------------------- M2: recipes / fallback

# Automation ids the deterministic adapter already handles — excluded from "unrecognized".
_STANDARD_DROPDOWN_IDS = {
    "addressSection_countryRegion", "country--country", "addressSection_countryRegionSubdivision1",
    "gender", "ethnicity", "veteranStatus", "disability-status",
}
_KNOWN_IDS = (set(_STANDARD_TEXT_IDS) | _STANDARD_DROPDOWN_IDS | set(_ACCT.values())
              | {_NEXT_ID, _SUBMIT_ID} | set(_RESUME_UPLOAD_IDS))

# Finds VISIBLE, still-EMPTY fillable controls whose data-automation-id we don't already handle —
# the custom "Application Questions" a tenant adds. Classifies control kind and extracts a label.
_UNRECOGNIZED_JS = r"""(known) => {
  const knownSet = new Set(known);
  const vis = el => el.offsetParent !== null;
  const seen = new Set(); const out = [];
  for (const el of document.querySelectorAll('[data-automation-id]')) {
    const aid = el.getAttribute('data-automation-id');
    if (!aid || knownSet.has(aid) || seen.has(aid) || !vis(el)) continue;
    const inp = el.matches('input,textarea') ? el : el.querySelector('input,textarea');
    const btn = el.matches('button[aria-haspopup="listbox"]') ? el : el.querySelector('button[aria-haspopup="listbox"]');
    let control = null, filled = false, ariaLabel = '';
    if (inp) {
      if (inp.type === 'file') continue;
      ariaLabel = inp.getAttribute('aria-label') || '';
      if (inp.type === 'checkbox') { control = 'checkbox'; filled = inp.checked; }
      else { control = 'text'; filled = !!(inp.value || '').trim(); }
    } else if (btn) {
      control = 'dropdown';
      filled = !/^(select one|select\.\.\.|select|)$/i.test((btn.textContent || '').trim());
    } else continue;
    if (filled) continue;
    const lbl = el.querySelector('label');
    let q = lbl ? lbl.textContent.trim() : (ariaLabel || (el.textContent || '').trim().slice(0, 140));
    seen.add(aid);
    out.push({automation_id: aid, control, question: q});
  }
  return out;
}"""


def unrecognized_fields(page) -> list[dict]:
    """Visible, empty, fillable controls on the current page whose automation id the deterministic
    adapter doesn't handle — the tenant's custom questions. Each: {automation_id, control, question}.
    These are what the agentic fallback fills and a recipe records. Never raises."""
    try:
        return list(page.evaluate(_UNRECOGNIZED_JS, list(_KNOWN_IDS)) or [])
    except Exception:
        return []


def replay_recipe(page, recipe, resolver, report) -> int:
    """Fill a page's custom fields from a learned recipe, DETERMINISTICALLY — no Claude. Each
    recipe field's answer is re-resolved for THIS user via `resolver` (the recipe stores only the
    selector + question, never an answer), then filled by automation id + control. Returns the
    count filled; records each as source ``workday-recipe``. Never raises."""
    filled = 0
    for f in recipe.fields:
        try:
            answer = resolver.resolve(f.question)
        except Exception:
            answer = None
        if not answer:
            continue
        ok = False
        if f.control == "text":
            ok = _fill_text(page, f.automation_id, answer)
        elif f.control == "dropdown":
            hints = tuple(resolver.option_hints(f.question) or ())
            ok = bool(_fill_dropdown(page, f.automation_id, answer, hints=hints))
        elif f.control == "checkbox":
            ok = _check(page, f.automation_id) if str(answer).strip().lower() in ("yes", "true", "1") else False
        if ok:
            report.filled.append(FilledField(f.automation_id, answer, f.control, source="workday-recipe"))
            filled += 1
    return filled


# --------------------------------------------------------------------------- M2: agentic fallback

def _applicant_facts(resume, profile) -> str:
    """Compact JSON of the applicant facts the agent may use to answer custom questions — the
    same profile/résumé the rest of Apply draws on. Empties dropped to keep the prompt small."""
    contact = getattr(resume, "contact", None)
    skills: list[str] = []
    for cat in (getattr(resume, "skills", []) or []):
        skills += list(getattr(cat, "items", []) or [])
    roles = [f"{e.role} at {e.organization}" for e in (getattr(resume, "experience", []) or [])[:3]]
    facts = {
        "name": (contact.name if contact else ""),
        "email": profile.email or (contact.email if contact else ""),
        "phone": profile.phone or (getattr(contact, "phone", "") if contact else ""),
        "location": profile.location, "country": profile.country,
        "work_authorized": profile.work_authorized, "requires_sponsorship": profile.requires_sponsorship,
        "us_citizen": profile.us_citizen, "years_experience": profile.years_experience,
        "linkedin": profile.linkedin_url, "github": profile.github_url, "portfolio": profile.portfolio_url,
        "how_heard": profile.how_heard, "skills": skills[:25], "recent_roles": roles,
    }
    return json.dumps({k: v for k, v in facts.items() if v not in (None, "", [])}, ensure_ascii=False)


def agent_prompt(fields: list[dict], resume, profile) -> str:
    """The instruction for the agentic worker: fill ONLY the listed custom fields on the current
    page from the applicant facts; never navigate; never fabricate protected facts (ApplyPilot's
    HARD RULES). The worker acts in the shared browser via the Playwright MCP server."""
    lines = "\n".join(
        f"  - {f.get('question') or f['automation_id']}  "
        f"[data-automation-id={f['automation_id']}, {f['control']}]" for f in fields)
    return (
        "You are completing ONE page of a Workday job application in the attached browser "
        "(Playwright MCP tools). Fill ONLY the fields listed below, using the applicant facts. "
        "Do NOT click Next, Continue, Save, Submit, or any navigation — only fill the fields on "
        "the CURRENT page, then stop.\n\n"
        f"Fields to fill:\n{lines}\n\n"
        "Rules: answer truthfully from the applicant facts. For skills/tools questions, be "
        "confident if it's in the same domain. For open-ended questions, write 2-3 specific "
        "sentences. NEVER invent or misstate citizenship, work authorization, visa/sponsorship "
        "status, education, or credentials — if a fact isn't given, leave that field blank.\n\n"
        f"Applicant facts (JSON):\n{_applicant_facts(resume, profile)}"
    )


def _agent_mcp_config(cdp_port: int) -> dict:
    """Playwright MCP server config attaching to OUR browser over CDP, so the agent drives the
    same page (not a fresh browser). Mirrors ApplyPilot's per-worker MCP setup."""
    return {"mcpServers": {"playwright": {
        "command": "npx",
        "args": ["@playwright/mcp@latest", f"--cdp-endpoint=http://localhost:{cdp_port}"],
    }}}


def _agent_argv(mcp_config_path: str, model: str = _AGENT_MODEL) -> list[str]:
    """The headless Claude Code CLI invocation for the agentic worker (prompt piped via stdin)."""
    return ["claude", "--model", model, "-p", "--mcp-config", mcp_config_path,
            "--permission-mode", "bypassPermissions", "--output-format", "stream-json", "--verbose", "-"]


def _record_agent_usage(stdout: Optional[str]) -> None:
    """Record the agentic worker's token spend from its `stream-json` stdout (decision 095).
    That format emits one JSON object per line, ending in a `result` object carrying cumulative
    `usage` + cost — the same shape the non-stream envelope has. Finds the last line with a `usage`
    block and hands it to `usage.record`, attributed to the posting under form-entry (this runs
    inside run_apply's `for_posting`). Best-effort: never raises."""
    try:
        from . import usage
        for line in reversed((stdout or "").splitlines()):
            try:
                obj = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                usage.record(obj, activity="form-entry")
                return
    except Exception:
        pass


def _spawn_claude_agent(page, fields, prompt, *, cdp_port, model, report) -> None:
    """Live agentic worker: launch Claude Code CLI with a Playwright MCP server bound to our
    browser's CDP endpoint; it fills the page's custom fields. The fill happens in the shared
    browser, so the caller distills the recipe by DOM diff — this only drives the subprocess.
    Raises on failure (the caller records it). FLAGGED LIVE STEP: needs Claude CLI + npx + CDP."""
    import os
    import subprocess
    import tempfile

    if not cdp_port:
        raise RuntimeError("no CDP endpoint — the browser wasn't launched with remote debugging")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(_agent_mcp_config(cdp_port), f)
        cfg_path = f.name
    proc = subprocess.run(
        _agent_argv(cfg_path, model), input=prompt, capture_output=True, text=True, timeout=300,
        env={**os.environ, "CLAUDESTATUS_IGNORE": "1"})
    # Meter the agentic worker's token spend (decision 095) — before the returncode check so a
    # failed-but-spent run still counts.
    _record_agent_usage(proc.stdout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude agent exited {proc.returncode}: {(proc.stderr or '')[-300:]}")


def run_agent_fill(page, resume, profile, report, *, cdp_port: Optional[int] = None,
                   model: str = _AGENT_MODEL, _spawn=None) -> list:
    """Hand the current page's UNRECOGNIZED custom fields to an agentic worker (Claude + Playwright
    MCP over CDP) that fills them in the SAME browser, then **distill a recipe by diffing which
    fields went empty→filled** — robust, with no dependence on parsing MCP element refs. Records
    each filled field (source ``workday-agent``) and returns the learned `RecipeField`s (to persist
    to the shared library). `_spawn` injectable so tests drive a fake agent (no Claude, no CDP).
    Never raises."""
    from . import workday_recipes

    before = unrecognized_fields(page)
    if not before:
        return []
    prompt = agent_prompt(before, resume, profile)
    spawn = _spawn or _spawn_claude_agent
    try:
        spawn(page, before, prompt, cdp_port=cdp_port, model=model, report=report)
    except Exception as e:
        report.errors.append(f"Workday agentic fallback failed: {type(e).__name__}: {e}")
        return []
    after_ids = {f["automation_id"] for f in unrecognized_fields(page)}
    learned = [workday_recipes.RecipeField(f["automation_id"], f["control"], f.get("question", ""))
               for f in before if f["automation_id"] not in after_ids]
    for rf in learned:
        report.filled.append(FilledField(rf.automation_id, "(agent)", rf.control, source="workday-agent"))
    return learned


def agentic_enabled(path=None) -> bool:
    """Whether the Workday agentic fallback is ON — **off by default** (decision 061). It spends
    Claude tokens on a tenant's unrecognized custom pages, so it's opt-in: set
    `workday_agentic: true` in profile/safety.yaml. Recipe REPLAY (free, deterministic) is always
    on regardless; only the agentic learning of a NEW page is gated."""
    import yaml

    from .safety import DEFAULT_SAFETY
    p = Path(path or DEFAULT_SAFETY)
    if not p.exists():
        return False
    try:
        return bool((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("workday_agentic", False))
    except Exception:
        return False


def _free_port() -> int:
    """An OS-assigned free TCP port for the browser's CDP endpoint (the Playwright-MCP worker
    attaches here). Small bind-and-release race, acceptable for a dev/apply run."""
    import socket

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _resolve_unrecognized(page, resolver, report, *, agentic: bool = False, cdp_port=None,
                          store_path=None, _agent_spawn=None) -> int:
    """Resolve a page's custom (unrecognized) questions after the deterministic fill: replay a
    learned recipe first (free), then — only if `agentic` and fields still remain — hand them to
    the agentic worker and persist the learned recipe. Returns the count filled. Never raises."""
    if not unrecognized_fields(page):
        return 0
    from . import workday_recipes

    kw = {"path": store_path} if store_path else {}
    filled = 0
    sig = _page_signature(page)
    recipe = workday_recipes.load_recipes(**kw).get(sig)
    if recipe:
        filled += replay_recipe(page, recipe, resolver, report)
    if not unrecognized_fields(page):
        return filled  # a recipe covered the whole page — no Claude
    if not agentic:
        return filled  # remaining custom fields left for capture (M1 behaviour) — no agent
    if _agent_spawn is None:
        from . import backends
        if not backends.claude_code_available():
            report.errors.append(
                "Workday has custom questions that need the agentic fallback, but Claude Code isn't "
                "signed in. Run `claude` and /login, or answer them manually.")
            return filled
    learned = run_agent_fill(page, resolver.resume, resolver.profile, report,
                             cdp_port=cdp_port, _spawn=_agent_spawn)
    if learned:
        workday_recipes.save_recipe(workday_recipes.Recipe(sig, learned), **kw)
        filled += len(learned)
    return filled


# --------------------------------------------------------------------------- M3: armed submit

# Visible required fields still empty (Workday marks them aria-required / data-required); custom
# dropdowns count as empty while their button still shows the "Select One" placeholder.
_WD_UNMET_REQUIRED_JS = r"""() => {
  const vis = el => el.offsetParent !== null;
  const out = [];
  for (const el of document.querySelectorAll('[aria-required="true"],[data-required="true"],[required]')) {
    if (!vis(el)) continue;
    const inp = el.matches('input,textarea,select') ? el : el.querySelector('input,textarea,select');
    let empty = false;
    if (inp) empty = !(inp.value || '').trim();
    else {
      const btn = el.matches('button') ? el : el.querySelector('button[aria-haspopup="listbox"]');
      if (btn) empty = /^(select one|select\.\.\.|select|)$/i.test((btn.textContent || '').trim());
    }
    if (!empty) continue;
    const lbl = el.querySelector('label');
    out.push((lbl && lbl.textContent.trim()) || el.getAttribute('data-automation-id')
             || (inp && inp.getAttribute('aria-label')) || 'a required field');
  }
  return out;
}"""

_WD_ERRORS_JS = r"""() => Array.from(document.querySelectorAll(
  '[role="alert"],[data-automation-id*="error"],[data-automation-id*="Error"]'))
  .filter(el => el.offsetParent !== null).map(el => (el.textContent || '').trim())
  .filter(Boolean).slice(0, 5)"""


def _workday_unmet_required(page) -> list:
    try:
        return list(page.evaluate(_WD_UNMET_REQUIRED_JS) or [])
    except Exception:
        return []


def _workday_validation_errors(page) -> list:
    try:
        return list(page.evaluate(_WD_ERRORS_JS) or [])
    except Exception:
        return []


def _attempt_workday_submit(page, report, gate) -> None:
    """Armed Workday submit (M3, decision 064): only on the Review page (a Submit control present),
    only when the SafetyGate allows — armed + no profile/KILL + under the per-run cap, re-checked
    immediately before the click (decision 035). Any doubt leaves it UNSUBMITTED with the reason in
    `report.blockers` — a blocked outcome to record, never a prompt. Never raises."""
    import time

    submit = page.locator(f"[data-automation-id='{_SUBMIT_ID}']:visible").first
    try:
        if submit.count() == 0:
            report.submit_state = "blocked"
            report.blockers = ["Workday: not on the Review page (no Submit control) — not submitted"]
            return
    except Exception:
        report.submit_state = "blocked"
        report.blockers = ["Workday: submit control not reachable — not submitted"]
        return

    missing = _workday_unmet_required(page)
    if missing:
        report.submit_state = "blocked"
        report.blockers = ["unresolved required field(s): " + "; ".join(missing[:8])]
        return

    ok, reason = gate.may_submit()  # kill switch / cap / armed at the last possible moment
    if not ok:
        report.submit_state = "blocked"
        report.blockers = [reason]
        return

    try:
        submit.click(timeout=5000)
    except Exception as e:
        report.submit_state = "blocked"
        report.blockers = [f"Workday submit click failed: {type(e).__name__}: {e}"]
        return
    gate.record_submission()  # count the click, not the confirmation — conservative vs. the cap

    from .apply import _confirmation_evidence

    deadline = time.time() + 20
    while time.time() < deadline:
        evidence = _confirmation_evidence(page, page)
        if evidence:
            report.submitted = True
            report.submit_state = "submitted"
            report.confirmation = evidence
            return
        errs = _workday_validation_errors(page)
        if errs:
            report.submit_state = "blocked"
            report.blockers = ["Workday rejected the submit: " + "; ".join(errs)]
            return
        try:
            page.wait_for_timeout(500)
        except Exception:
            break

    # No confirmation and no error: if the Submit control is gone the submit almost certainly went
    # through — mark unconfirmed-but-submitted so we never risk a double submission (decision 035).
    try:
        still_there = page.locator(f"[data-automation-id='{_SUBMIT_ID}']:visible").count() > 0
    except Exception:
        still_there = False
    if still_there:
        report.submit_state = "blocked"
        report.blockers = ["Workday submit clicked but the Review page is still showing with no "
                           "confirmation — treated as NOT submitted; verify manually"]
    else:
        report.submitted = True
        report.submit_state = "unconfirmed"
        report.confirmation = ("Review page gone after the submit click; no explicit confirmation "
                               "text found — verify via the confirmation email")

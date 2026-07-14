"""Workday adapter tests (decision 050) — the deterministic data-automation-id fill.

Two layers: (1) `standard_field_values` maps profile/résumé onto Workday's stable automation
ids with empties dropped, no browser; (2) driven against a local Workday-shaped fixture headless
(no tokens, no real site), the adapter fills wrapped inputs, direct inputs, and the phone widget,
leaves the custom dropdown alone, and never submits.

Run:  python -m tests.test_workday   (also pytest-compatible; needs chromium installed)
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

from applicationbot import credentials, workday
from applicationbot.apply import ApplyReport
from applicationbot.apply_profile import ApplicationProfile

REPO = Path(__file__).resolve().parent.parent
MYINFO = (REPO / "fixtures" / "apply_forms" / "workday_myinfo.html").as_uri()
WIZARD = (REPO / "fixtures" / "apply_forms" / "workday_wizard.html").as_uri()
ACCOUNT = (REPO / "fixtures" / "apply_forms" / "workday_account.html").as_uri()
FULL = (REPO / "fixtures" / "apply_forms" / "workday_full.html").as_uri()


REVIEW = (REPO / "fixtures" / "apply_forms" / "workday_review.html").as_uri()


def _pdf() -> str:
    p = Path(tempfile.mkdtemp()) / "resume.pdf"
    p.write_bytes(b"%PDF-1.4 workday test resume")
    return str(p)


def _gate(armed=True, killed=False, cap=10):
    from applicationbot.safety import SafetyGate
    kill = Path(tempfile.mkdtemp()) / "KILL"
    if killed:
        kill.write_text("stop")
    return SafetyGate(armed=armed, kill_file=kill, max_submissions_per_run=cap)


def _open_review(pw, *, fill_required=True):
    b = pw.chromium.launch(headless=True)
    page = b.new_page()
    page.goto(REVIEW, wait_until="domcontentloaded")
    if fill_required:
        page.fill("[data-automation-id='requiredAck'] input", "confirmed")
    return b, page


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def _idx():
    return str(Path(tempfile.mkdtemp()) / "workday_accounts.json")


def _profile(**kw):
    base = dict(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                phone="555-0100", location="New York, NY")
    base.update(kw)
    return ApplicationProfile(**base)


def _resume(name="Grace Hopper", email="grace@example.com", phone="555-0199"):
    return SimpleNamespace(contact=SimpleNamespace(name=name, email=email, phone=phone))


def test_values_profile_first_empties_dropped():
    vals = workday.standard_field_values(_resume(), _profile())
    assert vals["legalNameSection_firstName"] == "Ada"
    assert vals["legalNameSection_lastName"] == "Lovelace"
    assert vals["email"] == "ada@example.com"
    assert vals["addressSection_city"] == "New York"  # first comma-segment of location
    assert "phone-number" in vals and vals["phone-number"] == "555-0100"
    # no street/postal fields emitted (no profile data) — never filled blank
    assert "addressSection_addressLine1" not in vals and "addressSection_postalCode" not in vals


def test_values_fall_back_to_resume_when_profile_blank():
    vals = workday.standard_field_values(_resume(), _profile(first_name="", last_name="", email="", phone=""))
    assert vals["legalNameSection_firstName"] == "Grace"
    assert vals["legalNameSection_lastName"] == "Hopper"
    assert vals["email"] == "grace@example.com"
    assert vals["phone-number"] == "555-0199"


def test_fill_against_fixture_headless():
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=MYINFO, ats="workday")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(MYINFO, wait_until="domcontentloaded")
        n = workday.fill_standard_fields(page, _resume(), _profile(), report)

        def val(auto_id):
            return page.locator(f"[data-automation-id='{auto_id}'], "
                                f"[data-automation-id='{auto_id}'] input").last.input_value()

        assert val("legalNameSection_firstName") == "Ada"   # wrapped input
        assert val("legalNameSection_lastName") == "Lovelace"
        assert val("addressSection_city") == "New York"
        assert page.locator("[data-automation-id='email']").input_value() == "ada@example.com"  # direct input
        assert page.locator("[data-automation-id='phone-number']").input_value() == "555-0100"
        # the custom dropdown was left untouched (dropdowns are the next brick)
        assert page.locator("[data-automation-id='addressSection_countryRegion'] button").inner_text() == "Select One"
        browser.close()

    assert n == 5  # first, last, city, email, phone (only the phone id present in the DOM fills)
    assert {f.label for f in report.filled} == {
        "legalNameSection_firstName", "legalNameSection_lastName",
        "addressSection_city", "email", "phone-number",
    }
    assert all(f.source == "workday" for f in report.filled)
    assert report.submitted is False  # dry-run: nothing here submits


def test_dropdown_values_country_state_eeo():
    vals = workday.standard_dropdown_values(_profile(gender="Woman", veteran_status="I am not a protected veteran"))
    assert vals["addressSection_countryRegion"][0] == "United States"
    # state abbreviation expanded to a hint that matches the full-name option
    val, hints = vals["addressSection_countryRegionSubdivision1"]
    assert val == "NY" and "New York" in hints
    assert vals["gender"][0] == "Woman"
    assert vals["veteranStatus"][0] == "I am not a protected veteran"


def test_match_option_exact_then_substring():
    opts = ["United States of America", "Canada", "United Kingdom"]
    # "United States" isn't exact but substrings the option
    assert workday._match_option(opts, "United States", ("United States of America",)) == 0
    assert workday._match_option(opts, "Canada") == 1
    assert workday._match_option(opts, "Australia") is None


def test_fill_wizard_walks_pages_fills_dropdowns_never_submits():
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=WIZARD, ats="workday")
    profile = _profile(gender="Woman", veteran_status="I am not a protected veteran")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(WIZARD, wait_until="domcontentloaded")
        total = workday.fill_wizard(page, _resume(), profile, report)

        # page 1 dropdowns committed by button text
        assert page.locator("[data-automation-id='addressSection_countryRegion'] button").inner_text() == "United States of America"
        assert page.locator("[data-automation-id='addressSection_countryRegionSubdivision1'] button").inner_text() == "New York"
        # advanced to page 3 (Review) — page 2 dropdowns filled on the way
        assert page.locator("[data-automation-id='reviewSummary']").is_visible()
        submitted = page.evaluate("() => window.__submitClicked()")
        browser.close()

    assert report.pages == 3
    assert submitted is False and report.submitted is False  # Submit was NEVER clicked
    labels = {f.label for f in report.filled}
    assert {"legalNameSection_firstName", "addressSection_countryRegion",
            "addressSection_countryRegionSubdivision1", "gender", "veteranStatus"} <= labels
    # dropdown fills recorded as selects, sourced to the deterministic adapter
    assert all(f.source == "workday" for f in report.filled)
    assert {f.control for f in report.filled if f.label == "gender"} == {"select"}


def test_generate_password_meets_complexity():
    pw = workday.generate_password()
    assert len(pw) >= 12
    assert re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"\d", pw)
    assert re.search(r"[!@#$%^&*\-_]", pw)
    assert workday.generate_password() != workday.generate_password()  # random


def test_sign_in_fills_and_clicks_visible_form():
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=ACCOUNT, ats="workday")
    acct = credentials.Account("acme.myworkdayjobs.com", "bot@example.com", "P&ssw0rd!")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(ACCOUNT, wait_until="domcontentloaded")
        ok = workday.sign_in(page, acct, report)
        assert page.locator("#signin [data-automation-id='email']").input_value() == "bot@example.com"
        assert page.evaluate("() => window.__signedIn") is True
        b.close()
    assert ok is True


def test_create_account_reveals_form_fills_and_ticks_terms():
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=ACCOUNT, ats="workday")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(ACCOUNT, wait_until="domcontentloaded")
        ok = workday.create_account(page, "bot@example.com", "G3n!pass_word", report)
        assert page.locator("#create [data-automation-id='email']").input_value() == "bot@example.com"
        assert page.locator("#create [data-automation-id='verifyPassword']").input_value() == "G3n!pass_word"
        assert page.locator("#create [data-automation-id='createAccountCheckbox'] input").is_checked()
        assert page.evaluate("() => window.__created") is True  # only true if fields + terms set
        b.close()
    assert ok is True


def test_ensure_account_signs_in_when_stored(monkeypatch):
    kr, idx = _FakeKeyring(), _idx()
    credentials.save_account(credentials.Account("acme.wd1.myworkdayjobs.com", "bot@x.com", "pw"),
                             backend=kr, index_path=idx)
    monkeypatch.setattr(workday, "sign_in", lambda page, acct, report: True)
    report = ApplyReport(url="x", ats="workday")
    acct = workday.ensure_account(object(), "https://acme.wd1.myworkdayjobs.com/job/1", _profile(),
                                  report, backend=kr, index_path=idx)
    assert acct is not None and acct.email == "bot@x.com"
    assert "signed in" in (report.native_autofill or "")


def test_ensure_account_creates_stores_and_flags_manual_verify(monkeypatch):
    kr, idx = _FakeKeyring(), _idx()
    monkeypatch.setattr(workday, "create_account", lambda page, email, pw, report: True)
    report = ApplyReport(url="x", ats="workday")
    # no mailbox configured → account created + saved, but flagged for manual verification
    acct = workday.ensure_account(object(), "https://acme.wd1.myworkdayjobs.com/job/1",
                                  _profile(email="me@x.com"), report, backend=kr, index_path=idx)
    assert acct is not None and acct.email == "me@x.com" and acct.password
    # persisted so the password is never lost even before verification (settled spec)
    stored = credentials.get_account("acme.wd1.myworkdayjobs.com", backend=kr, index_path=idx)
    assert stored and stored.password == acct.password
    assert any("verify the email manually" in e for e in report.errors)


def test_ensure_account_uses_bot_email_and_verifies(monkeypatch):
    from applicationbot import mailbox
    from applicationbot.mailbox import MailboxConfig

    kr, idx = _FakeKeyring(), _idx()
    monkeypatch.setattr(workday, "create_account", lambda page, email, pw, report: True)
    monkeypatch.setattr(mailbox, "wait_for_verification", lambda cfg, **kw: "550123")
    applied = {}
    monkeypatch.setattr(workday, "_apply_verification",
                        lambda page, code, report: applied.setdefault("code", code) or True)
    report = ApplyReport(url="x", ats="workday")
    cfg = MailboxConfig("imap.x.com", "bot@example.com", "pw")
    acct = workday.ensure_account(object(), "https://acme.wd1.myworkdayjobs.com/job/1",
                                  _profile(email="me@x.com"), report, backend=kr, index_path=idx,
                                  mailbox_config=cfg)
    # account uses the BOT email (verification lands in the bot inbox), not the profile email
    assert acct.email == "bot@example.com"
    assert applied["code"] == "550123"


def test_apply_workday_full_flow_creates_account_fills_never_submits():
    from playwright.sync_api import sync_playwright

    kr, idx = _FakeKeyring(), _idx()
    profile = _profile(gender="Woman", veteran_status="I am not a protected veteran")
    report = ApplyReport(url=FULL, ats="workday")
    tenant_url = "https://acme.wd1.myworkdayjobs.com/careers/job/1"
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(FULL, wait_until="domcontentloaded")
        ok = workday.apply_workday(page, tenant_url, _resume(), profile, report,
                                   resume_pdf=_pdf(), mailbox_config=None, backend=kr, index_path=idx)
        created = page.evaluate("() => window.__created")
        submitted = page.evaluate("() => window.__submitted")
        review_visible = page.locator("[data-automation-id='reviewSummary']").is_visible()
        b.close()

    assert ok is True
    assert created is True and submitted is False        # account made, application NEVER submitted
    assert review_visible and report.pages == 3          # walked Apply→account→wizard→Review
    # account persisted with the profile email (no mailbox configured) so it's never lost
    acct = credentials.get_account("acme.wd1.myworkdayjobs.com", backend=kr, index_path=idx)
    assert acct is not None and acct.email == profile.email and acct.password
    labels = {f.label for f in report.filled}
    assert {"legalNameSection_firstName", "addressSection_countryRegion", "gender", "Resume"} <= labels


def test_workday_submit_armed_happy_path():
    from playwright.sync_api import sync_playwright
    gate = _gate(armed=True)
    report = ApplyReport(url=REVIEW, ats="workday")
    with sync_playwright() as pw:
        b, page = _open_review(pw)
        workday._attempt_workday_submit(page, report, gate)
        submitted = page.evaluate("() => window.__submitted")
        b.close()
    assert submitted is True
    assert report.submitted is True and report.submit_state == "submitted"
    assert "applying" in report.confirmation.lower() and gate.submitted_this_run == 1


def test_workday_submit_blocked_when_required_field_empty():
    from playwright.sync_api import sync_playwright
    gate = _gate(armed=True)
    report = ApplyReport(url=REVIEW, ats="workday")
    with sync_playwright() as pw:
        b, page = _open_review(pw, fill_required=False)   # leave the required field empty
        workday._attempt_workday_submit(page, report, gate)
        submitted = page.evaluate("() => window.__submitted")
        b.close()
    assert submitted is False and report.submitted is False and report.submit_state == "blocked"
    assert "required" in report.blockers[0].lower() and gate.submitted_this_run == 0  # never clicked


def test_workday_submit_blocked_by_kill_switch():
    from playwright.sync_api import sync_playwright
    gate = _gate(armed=True, killed=True)   # KILL file present
    report = ApplyReport(url=REVIEW, ats="workday")
    with sync_playwright() as pw:
        b, page = _open_review(pw)
        workday._attempt_workday_submit(page, report, gate)
        submitted = page.evaluate("() => window.__submitted")
        b.close()
    assert submitted is False and report.submit_state == "blocked"
    assert "kill switch" in report.blockers[0].lower() and gate.submitted_this_run == 0


def test_workday_submit_blocked_when_unarmed():
    from playwright.sync_api import sync_playwright
    gate = _gate(armed=False)
    report = ApplyReport(url=REVIEW, ats="workday")
    with sync_playwright() as pw:
        b, page = _open_review(pw)
        workday._attempt_workday_submit(page, report, gate)
        submitted = page.evaluate("() => window.__submitted")
        b.close()
    assert submitted is False and report.submit_state == "blocked" and "not armed" in report.blockers[0]


def test_apply_workday_armed_submits_full_flow():
    from playwright.sync_api import sync_playwright
    gate = _gate(armed=True)
    report = ApplyReport(url=FULL, ats="workday")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(FULL, wait_until="domcontentloaded")
        ok = workday.apply_workday(page, "https://acme.wd1.myworkdayjobs.com/careers/job/1",
                                   _resume(), _profile(gender="Woman", veteran_status="I am not a protected veteran"),
                                   report, resume_pdf=_pdf(), mailbox_config=None,
                                   backend=_FakeKeyring(), index_path=_idx(), gate=gate)
        submitted = page.evaluate("() => window.__submitted")
        b.close()
    assert ok is True and submitted is True                      # full flow reached Review and submitted
    assert report.submitted is True and report.submit_state == "submitted" and gate.submitted_this_run == 1


def test_run_apply_routes_workday_to_the_adapter(monkeypatch):
    from applicationbot import apply as apply_mod

    called = {"adapter": 0, "generic": 0}
    monkeypatch.setattr(workday, "apply_workday",
                        lambda page, url, resume, profile, report, **kw: called.__setitem__("adapter", called["adapter"] + 1) or True)

    def _generic(*a, **k):
        called["generic"] += 1
        return (True, None, "workday")
    monkeypatch.setattr(apply_mod, "_open_application_form", _generic)

    from applicationbot.apply import AnswerResolver, run_apply
    from applicationbot.resume import load_resume
    resolver = AnswerResolver(resume=load_resume(str(REPO / "examples" / "sample_resume.yaml")),
                              profile=ApplicationProfile(), enable_generation=False)
    run_apply(FULL, _pdf(), resolver, headed=False, pause=False, slow_mo=0, learn=False, record=False,
              screenshot=str(Path(tempfile.mkdtemp()) / "shot.png"), gate=None)
    assert called["adapter"] == 1 and called["generic"] == 0  # workday → adapter, NOT the generic path


def test_run_apply_workday_agentic_opens_cdp_and_threads_params(monkeypatch):
    # With the agentic fallback armed, run_apply must launch Chromium WITH a CDP endpoint and pass
    # agentic=True + that port to the adapter. Verifies the real browser launch (not the Claude call).
    monkeypatch.setattr(workday, "agentic_enabled", lambda *a, **k: True)
    seen = {}

    def stub(page, url, resume, profile, report, **kw):
        seen.update(kw)
        return True
    monkeypatch.setattr(workday, "apply_workday", stub)

    from applicationbot.apply import AnswerResolver, run_apply
    from applicationbot.resume import load_resume
    resolver = AnswerResolver(resume=load_resume(str(REPO / "examples" / "sample_resume.yaml")),
                              profile=ApplicationProfile(), enable_generation=False)
    run_apply(FULL, _pdf(), resolver, headed=False, pause=False, slow_mo=0, learn=False, record=False,
              screenshot=str(Path(tempfile.mkdtemp()) / "shot.png"), gate=None)
    assert seen.get("agentic") is True
    assert isinstance(seen.get("cdp_port"), int) and seen["cdp_port"] > 0  # a real free port was opened
    assert seen.get("resolver") is resolver


def test_is_fillable_allows_workday():
    from applicationbot import pipeline
    from types import SimpleNamespace as NS
    assert pipeline._is_fillable(NS(ats="workday", extra={})) is True
    assert pipeline._is_fillable(NS(ats="icims", extra={})) is False
    assert pipeline._is_fillable(NS(ats="workday", extra={"auto_applyable": False})) is False


def _run_all():
    from _pytest.monkeypatch import MonkeyPatch

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        mp = MonkeyPatch()
        try:
            kw = {"monkeypatch": mp} if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount] else {}
            fn(**kw)
        finally:
            mp.undo()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

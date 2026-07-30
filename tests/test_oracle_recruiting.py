"""Oracle Recruiting Cloud: reaching and filling the email gate (decision 171).

ORC is the largest single block of postings in the curated feeds — 3,553, more than Greenhouse —
and none of them could be applied to. Probing three unrelated tenants (Oracle CX_45001, American
Express CX_1, ADT CX_1) found one flow and four defects, every one of them in shared code rather
than in anything Oracle-specific:

  1. `_count_fields` counted controls nobody can see, so a posting page carrying a collapsed
     Oracle Digital Assistant chat box read as "the form has rendered" and APPLY NOW was never
     clicked (2 of the 3 tenants never left the posting page).
  2. Oracle's honeypot is named `honey-pot` and marked aria-hidden on the input itself — decision
     168's two signals both miss it, and the aria-hidden skip swallowed it before any check ran.
  3. The terms checkbox is a 0x0 input behind a decorative <span>; a plain `.check()` times out,
     so a REQUIRED box stayed unticked on every tenant.
  4. A cookie-consent overlay sits over the form and eats the click regardless.

What is NOT covered here: anything past the email step. Clicking Next posts an email address to a
real employer and creates a candidate profile there, so the flow beyond it is unverified and
`oracle` is deliberately still absent from `discovery.FILLABLE_ATS`.

Run:  python -m pytest tests/test_oracle_recruiting.py   (needs chromium installed)
"""
from __future__ import annotations

from pathlib import Path

from applicationbot.apply import (AnswerResolver, ApplyReport, _count_fields,
                                  _dismiss_consent_banner, _fill_page, _force_check, detect_ats)
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.discovery import FILLABLE_ATS, detect_ats_from_url
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
FORMS = REPO / "fixtures" / "apply_forms"
GATE = (FORMS / "oracle_email_gate.html").as_uri()
BANNER = (FORMS / "consent_banner.html").as_uri()


def _resolver() -> AnswerResolver:
    return AnswerResolver(
        resume=Resume(contact=Contact(name="Test User", email="t@example.com")),
        profile=ApplicationProfile(first_name="Test", last_name="User", email="t@example.com"),
        enable_generation=False)


def _drive(url, fn):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        try:
            return fn(page)
        finally:
            browser.close()


# ------------------------------------------------------------------------------- detection

def test_both_oracle_host_shapes_are_detected():
    for url in (
        "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/340728",
        "https://fa-erqb-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/3021562",
    ):
        assert detect_ats(url) == "oracle", url
        assert detect_ats_from_url(url) == "oracle", url


def test_oracle_is_not_yet_advertised_as_fillable():
    # The email gate fills, but nothing past it is verified — see the module docstring. This pins
    # the honest state so it can only change alongside evidence that a full application completes.
    assert "oracle" not in FILLABLE_ATS


# ------------------------------------------------------ the form-rendered signal (defect 1)

def test_count_fields_ignores_controls_nobody_can_see():
    # The banner fixture's three collapsed widgets are display:none. Only the two real form
    # controls count — otherwise a posting page reads as a rendered form.
    n = _drive(BANNER, lambda page: _count_fields(page.main_frame))
    assert n == 2, n


# ------------------------------------------------------------------- the email gate (2 + 3)

def test_email_gate_fills_and_leaves_the_honeypot_alone():
    def run(page):
        report = ApplyReport(url=GATE, ats="oracle")
        _fill_page(page, _resolver(), report, done=set())
        return report, {
            "email": page.locator("#primary-email-0").input_value(),
            "honeypot": page.locator("#honey-pot-1").input_value(),
            "terms": page.locator("#legal-disclaimer-checkbox").is_checked(),
        }
    report, dom = _drive(GATE, run)
    assert dom["email"] == "t@example.com"
    assert dom["terms"] is True, "the required terms box is a 0x0 input behind a <span>"
    assert dom["honeypot"] == ""
    assert any("honeypot" in n for n in report.notes), report.notes
    assert not report.errors, report.errors


def test_the_honeypot_is_reported_not_merely_skipped():
    # It was already coming back empty before this — as a side effect of being aria-hidden, which
    # is luck rather than a guarantee. The note is the difference between the two.
    def run(page):
        report = ApplyReport(url=GATE, ats="oracle")
        _fill_page(page, _resolver(), report, done=set())
        return report
    report = _drive(GATE, run)
    assert [n for n in report.notes if "honey-pot-1" in n]
    assert "honeypot" not in report.captured        # never offered to the user as a question
    assert not [f for f in report.filled if "honey" in f.label.lower()]


def test_force_check_ticks_a_zero_sized_checkbox():
    ok = _drive(GATE, lambda page: _force_check(page.locator("#legal-disclaimer-checkbox")))
    assert ok is True


# --------------------------------------------------------------- the consent overlay (4)

def test_consent_banner_is_dismissed_with_the_least_consent_on_offer():
    def run(page):
        report = ApplyReport(url=BANNER, ats="oracle")
        _dismiss_consent_banner(page, report)
        return report, page.locator("#cookie-banner").count()
    report, still_there = _drive(BANNER, run)
    assert still_there == 0
    assert report.notes and "refused" in report.notes[0], report.notes


def test_dismissing_the_banner_unblocks_the_control_underneath():
    def run(page):
        report = ApplyReport(url=BANNER, ats="oracle")
        _dismiss_consent_banner(page, report)
        _fill_page(page, _resolver(), report, done=set())
        return report, page.locator("#terms").is_checked()
    report, checked = _drive(BANNER, run)
    assert checked is True
    assert not report.errors, report.errors


def test_no_banner_means_no_click_and_no_note():
    # Every ATS we already fill has no consent overlay — this must be a no-op for them.
    def run(page):
        report = ApplyReport(url=GATE, ats="oracle")
        _dismiss_consent_banner(page, report)
        return report
    assert _drive(GATE, run).notes == []

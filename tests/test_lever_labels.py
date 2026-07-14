"""Lever custom-question label derivation — local fixture, headless Chromium, zero tokens.

Regression for the 2026-07-13 WHOOP dry-run report: Lever renders a card's question in a
<div class="application-label"> (not a <label>/<legend>), so the old _LABEL_JS/_GROUP_QUESTION_JS
fell through to the raw input name / an empty string. Effect: the work-authorization and visa
radio groups never filled (resolver got no question), and the "Why are you interested…" text got
the label cards[uuid][field0] — meaningless to ground a generated answer on. This asserts the
question text is now recovered, so the resolver maps work-auth→Yes and visa→No.

Run:  python -m tests.test_lever_labels   (also pytest-compatible; needs chromium installed)
"""
from __future__ import annotations

from pathlib import Path

from applicationbot.apply import (
    AnswerResolver, ApplyReport, _fill_radio_groups, _fill_select,
    _GROUP_QUESTION_JS, _LABEL_JS)
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "lever_custom_cards.html").as_uri()

WHY = "Why are you interested in working at WHOOP?"
WORKAUTH = "Are you legally authorized to work in the United States?"
VISA = "Will you now or in the future require visa sponsorship for employment at WHOOP?"


def _resolver() -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    profile = ApplicationProfile(
        first_name="Test", last_name="User", email="t@example.com",
        work_authorized=True, requires_sponsorship=False,
        # Greenhouse-worded EEO answers — must still map onto Lever's option wording.
        gender="Male", race_ethnicity="Asian",
        veteran_status="I am not a protected veteran")
    return AnswerResolver(resume=resume, profile=profile)


def _drive(fn):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(FIXTURE)
        try:
            return fn(page)
        finally:
            browser.close()


def test_lever_text_card_reports_question_not_raw_name():
    def run(page):
        loc = page.locator('input[name="cards[2690853c-4731-41b0-a871-687f8f7b351d][field0]"]')
        assert loc.evaluate(_LABEL_JS) == WHY  # was "cards[…][field0]"
        # Standard fields (input wrapped in <label>) keep deriving correctly.
        assert page.locator('input[name="name"]').evaluate(_LABEL_JS) == "Full name"
    _drive(run)


def test_lever_radio_groups_report_question_and_resolve():
    def run(page):
        r = _resolver()
        wa = page.locator('input[name="cards[6c5926d5][field0]"]').first
        visa = page.locator('input[name="cards[22aa53c9][field0]"]').first
        assert wa.evaluate(_GROUP_QUESTION_JS) == WORKAUTH     # was ""
        assert visa.evaluate(_GROUP_QUESTION_JS) == VISA       # was ""
        # The recovered questions now resolve straight from the profile flags.
        assert r.resolve(WORKAUTH) == "Yes"
        assert r.resolve(VISA) == "No"
        # A radio OPTION input must still derive its own "Yes"/"No" label, not the card
        # question — the option-matching in _fill_radio_groups depends on it (regression guard).
        assert wa.evaluate(_LABEL_JS) == "Yes"
    _drive(run)


def test_lever_radio_groups_get_checked_correctly():
    def run(page):
        r = _resolver()
        report = ApplyReport(url=FIXTURE, ats="fixture")
        _fill_radio_groups(page, r, report, done=set())
        # work-auth → Yes, visa → No; the OTHER options stay unchecked.
        assert page.locator('input[name="cards[6c5926d5][field0]"][value="Yes"]').is_checked()
        assert not page.locator('input[name="cards[6c5926d5][field0]"][value="No"]').is_checked()
        assert page.locator('input[name="cards[22aa53c9][field0]"][value="No"]').is_checked()
        assert not page.locator('input[name="cards[22aa53c9][field0]"][value="Yes"]').is_checked()
        filled = {f.label: f.value for f in report.filled}
        assert filled.get(WORKAUTH) == "Yes"
        assert filled.get(VISA) == "No"
    _drive(run)


def test_lever_eeo_selects_get_clean_label_and_normalize():
    def run(page):
        r = _resolver()
        # Label no longer folds in every <option> (the <select> is wrapped in a <label>).
        assert page.locator('select[name="eeo[veteran]"]').evaluate(_LABEL_JS) == "Veteran status"
        assert page.locator('select[name="eeo[race]"]').evaluate(_LABEL_JS) == "Race"

        def fill(name):
            loc = page.locator(f'select[name="{name}"]')
            lbl = loc.evaluate(_LABEL_JS)
            return _fill_select(loc, r.resolve(lbl), r.option_hints(lbl))

        assert fill("eeo[gender]") == "Male"
        assert fill("eeo[race]") == "Asian (Not Hispanic or Latino)"
        # The critical case: Greenhouse "…not a protected veteran" maps to Lever "I am not a
        # veteran" — the NEGATIVE option, never "I am a veteran".
        assert fill("eeo[veteran]") == "I am not a veteran"
    _drive(run)


if __name__ == "__main__":
    test_lever_text_card_reports_question_not_raw_name()
    test_lever_radio_groups_report_question_and_resolve()
    test_lever_radio_groups_get_checked_correctly()
    test_lever_eeo_selects_get_clean_label_and_normalize()
    print("ok")

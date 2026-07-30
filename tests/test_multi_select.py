"""Check-all-that-apply questions — local fixture, headless Chromium, zero tokens.

A checkbox GROUP takes more than one answer. The bank stores those as one "A; B" string
(what the profile UI's multi-select widget writes), so the fill must tick EVERY chosen
option, and an unanswered group must be captured as kind "checkbox" with its option texts
so the UI can recreate it as checkboxes instead of a single-pick dropdown.

Run:  python -m tests.test_multi_select   (also pytest-compatible; needs chromium installed)
"""
from __future__ import annotations

from pathlib import Path

from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import QA, ApplicationProfile
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "multi_select.html").as_uri()

QUESTION = "Language Skill(s) (Check all that apply)"
OPTIONS = ["English (ENG)", "Spanish (SPA)", "French (FRA)", "German (DEU)"]


def _resolver(answer: str = "") -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    banked = [QA(question=QUESTION, answer=answer, input_kind="checkbox", options=OPTIONS)] if answer else []
    profile = ApplicationProfile(first_name="Test", last_name="User", email="t@example.com",
                                 custom_answers=banked)
    return AnswerResolver(resume=resume, profile=profile, enable_generation=False)


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


def _run(page, resolver):
    report = ApplyReport(url=FIXTURE, ats="fixture")
    _fill_page(page, resolver, report, done=set())
    return report


def test_multi_answer_checks_every_chosen_option():
    def run(page):
        report = _run(page, _resolver("English (ENG); Spanish (SPA)"))
        assert page.locator('input[value="eng"]').is_checked()
        assert page.locator('input[value="spa"]').is_checked()
        assert not page.locator('input[value="fra"]').is_checked()
        assert not page.locator('input[value="deu"]').is_checked()
        values = {f.value for f in report.filled if f.label == QUESTION}
        assert values == {"English (ENG)", "Spanish (SPA)"}, [f.value for f in report.filled]
    _drive(run)


def test_single_answer_still_checks_only_that_option():
    def run(page):
        _run(page, _resolver("French (FRA)"))
        assert page.locator('input[value="fra"]').is_checked()
        assert not page.locator('input[value="eng"]').is_checked()
    _drive(run)


def test_unanswered_group_is_captured_as_checkbox_with_its_options():
    def run(page):
        report = _run(page, _resolver())
        cap = report.captured.get(QUESTION)
        assert cap, report.captured.keys()
        assert cap["kind"] == "checkbox"          # the UI renders this as a multi-answer question
        assert cap["options"] == OPTIONS
        assert not page.locator('input[value="eng"]').is_checked()
    _drive(run)


if __name__ == "__main__":
    test_multi_answer_checks_every_chosen_option()
    test_single_answer_still_checks_only_that_option()
    test_unanswered_group_is_captured_as_checkbox_with_its_options()
    print("ok")

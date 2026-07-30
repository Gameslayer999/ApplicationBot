"""A field is identified by its KEY, not by its label alone (decision 169).

Answers were keyed by the question text the form shows. When two controls show the same text —
two "Education" blocks, a wizard repeating a field on a later page — the second was silently
skipped as already-done, and the review panel, the required marks and the user's saved edits had
no way to name it. A field key is now that label plus " #n" for the n-th (n>1) control deriving it.

The two keys the old design conflated are pinned here:
  * the KEY identifies the field   — `done`, the report, the per-posting edits;
  * the QUESTION (`_question`, the key minus " #n") is what every answering rule, the answer
    bank and every model call see — a second "Date" box is still a date question.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from applicationbot import answer_bank, web
from applicationbot.apply import (AnswerResolver, ApplyReport, _fill_page, _question,
                                  _record_required)
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Education, Resume

REPO = Path(__file__).resolve().parent.parent
REPEATED = (REPO / "fixtures" / "apply_forms" / "repeated_labels.html").as_uri()


def _resolver() -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"),
                    education=[Education(school="Penn State", degree="B.S. Computer Science")])
    return AnswerResolver(resume=resume, enable_generation=False,
                          profile=ApplicationProfile(first_name="Test", last_name="User",
                                                     email="t@example.com"))


@pytest.mark.parametrize("key,question", [
    ("Date", "Date"),
    ("Date #2", "Date"),
    ("School #12", "School"),
    ("Rank your top #1 choice", "Rank your top #1 choice"),  # a # inside the text is not a key
])
def test_question_is_the_key_without_its_disambiguator(key, question):
    assert _question(key) == question


def test_every_repeated_field_is_filled_and_keyed_apart():
    """The defect, on a form with two identical education blocks: before, only the first block's
    School/Degree/Email were filled and the second silently stayed empty."""
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=REPEATED, ats="greenhouse")
    resolver = _resolver()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(REPEATED)
        try:
            _fill_page(page, resolver, report, done=set())
            _record_required(page, report)
            values = {i: page.input_value("#" + i) for i in ("s1", "d1", "e1", "s2", "d2", "e2")}
        finally:
            browser.close()

    # Both blocks are filled, and each answers as the question it asks — the key's " #2" is not
    # part of the question, so the second School still resolves from the résumé.
    assert values["s1"] == values["s2"] == "Penn State"
    assert values["e1"] == values["e2"] == "t@example.com"
    assert values["d1"] == values["d2"]
    keys = [f.label for f in report.filled]
    assert "School" in keys and "School #2" in keys
    assert "Email" in keys and "Email #2" in keys
    # The required sweep numbers identically, or its marks could not be joined onto these rows:
    # the fixture marks the FIRST School and the SECOND Email required.
    assert report.required["School"] is True
    assert report.required["School #2"] is False
    assert report.required["Email"] is False
    assert report.required["Email #2"] is True


def test_a_field_key_is_never_banked_as_a_question():
    """The key names one control on one form. Banking it would answer a different field on the
    next posting — the failure decision 166 traced, one level up."""
    assert answer_bank.is_context_dependent("School #2") is True
    assert answer_bank.is_reusable_answer("School #2") is False
    assert answer_bank.valid_mapping("Date #2", "earliest_start_date") is False
    # …while the question itself is still perfectly bankable.
    assert answer_bank.is_reusable_answer("School") is True


def test_the_user_s_edit_and_the_context_are_per_field_not_per_label():
    """Two fields sharing a label must be separately editable and separately explainable —
    otherwise correcting one silently rewrites the other."""
    r = _resolver()
    r.overrides = {"date": "2026-01-01", "date 2": "2026-02-02"}
    assert r.resolve("Date") == "2026-01-01"
    assert r.resolve("Date #2") == "2026-02-02"

    r2 = _resolver()
    r2.note_context("Date", "Applicant certification · follows the field: Signature")
    r2.note_context("Date #2", "Previous employment · follows the field: Employer")
    assert r2.resolve("Date") == date.today().isoformat()
    assert r2.resolve("Date #2") is None            # the employment date is the applicant's
    assert r2.context_for("Date #2").startswith("Previous employment")


def test_identical_answers_from_two_wizard_pages_show_as_one_row():
    """A wizard can ask the same question on two pages, and each page now fills it. Two identical
    rows would give the user two edit boxes writing to one key."""
    rows = web._merge_checkbox_groups([
        {"label": "Email", "value": "t@example.com", "control": "text"},
        {"label": "Email", "value": "t@example.com", "control": "text"},
        {"label": "Email #2", "value": "other@example.com", "control": "text"},
    ])
    assert [r["label"] for r in rows] == ["Email", "Email #2"]

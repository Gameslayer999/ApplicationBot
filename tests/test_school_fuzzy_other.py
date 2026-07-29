"""A school dropdown that spells the school differently, or doesn't list it at all (decision 154).

Two failures the pipeline had no answer for, both on the résumé value "The Pennsylvania State
University":
  1. the list offers "Penn State University-University Park" — no equality, no substring, so the
     field went unfilled;
  2. the list has no Penn State entry at all — the field went unfilled, blocking an armed submit.
Now (1) fuzzy-matches on identity tokens and (2) takes the form's own "Other" escape hatch, saying
in the report that the real answer wasn't offered.

The unit half is pure and instant; the browser half drives the real `_fill_all_fields` over a local
fixture with generation OFF — zero Claude calls, so every fill below is deterministic.

Run:  python -m tests.test_school_fuzzy_other   (also pytest-compatible; needs chromium)
"""
from __future__ import annotations

from pathlib import Path

from applicationbot.apply import (AnswerResolver, ApplyReport, _accepts_other, _fill_all_fields,
                                  _fuzzy_option_index, _other_option_index)
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Education, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "school_not_listed.html").as_uri()
PENN = "The Pennsylvania State University"
LISTED = "Penn State University-University Park"


# --------------------------------------------------------------------------- fuzzy matching


def test_fuzzy_matches_abbreviated_and_misspelled_school():
    opts = ["Adelphi University", LISTED, "Princeton University"]
    assert _fuzzy_option_index(opts, PENN) == 1                     # "Penn" ↔ "Pennsylvania"
    assert _fuzzy_option_index(opts, "Pennsylvnia State Univ") == 1  # typo + abbreviation
    assert _fuzzy_option_index(["Adelphi University"], PENN) is None


def test_fuzzy_prefers_the_main_campus():
    opts = ["Pennsylvania State University - Schuylkill Campus", "Pennsylvania State University"]
    assert _fuzzy_option_index(opts, PENN) == 1  # fewest leftover tokens wins


def test_fuzzy_refuses_a_different_school():
    # A different institution TYPE is a different school, however well the name lines up.
    assert _fuzzy_option_index(["Boston College"], "Boston University") is None
    assert _fuzzy_option_index(["Pennsylvania College of Technology"], PENN) is None
    # Two equally-good candidates: decline rather than guess which "Miss" was meant.
    assert _fuzzy_option_index(["Mississippi State University", "Missouri State University"],
                               "Miss State University") is None


def test_fuzzy_does_not_conflate_short_or_opposite_answers():
    # A 2–3 letter answer is never fuzzy-matched (it stays on `_matches`' whole-word rule), so
    # "US" can't reach "United States" — and, more importantly, can't reach "Australia".
    assert _fuzzy_option_index(["Australia", "United States"], "US") is None
    assert _fuzzy_option_index(["Female"], "Male") is None
    assert _fuzzy_option_index(["Norway", "No"], "No") == 1
    assert _fuzzy_option_index(["Computer Engineering"], "Computer Science") is None


def test_other_option_is_recognised_only_where_it_is_a_valid_answer():
    assert _other_option_index(["Princeton University", "Other (please specify)"]) == 1
    assert _other_option_index(["Yale", "My school is not listed"]) == 1
    assert _other_option_index(["Princeton University", "Temple University"]) is None
    assert _accepts_other("Undergraduate school") and _accepts_other("University attended")
    # Never for a question where "Other" is a wrong answer, not a graceful fallback.
    assert not _accepts_other("Gender") and not _accepts_other("Are you authorized to work?")


# --------------------------------------------------------------------------- browser fills


def _resolver() -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"),
                    education=[Education(school=PENN, degree="Bachelor of Science")])
    return AnswerResolver(resume=resume, profile=ApplicationProfile(), enable_generation=False)


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


def test_fills_all_four_school_dropdowns_without_a_model_call():
    def run(page):
        report = ApplyReport(url=FIXTURE, ats="fixture")
        _fill_all_fields(page, _resolver(), report, done=set())
        filled = {f.label: f for f in report.filled}
        assert not report.errors, report.errors

        # Spelled differently → matched, on both control types.
        assert filled["School"].value == LISTED, filled
        assert filled["Undergraduate school"].value == LISTED, filled
        assert filled["Undergraduate school"].source == "option:fuzzy", filled
        assert page.locator("#combo--0").evaluate("el => el.dataset.committed || ''") == LISTED

        # Genuinely absent → "Other", and the report says the real answer wasn't offered.
        assert filled["University attended"].value == "Other", filled
        assert filled["University attended"].source == "option:other", filled
        assert filled["Graduate school"].value == "Other", filled
        assert filled["Graduate school"].source == "option:other", filled
        assert page.locator("#combo2--0").evaluate("el => el.dataset.committed || ''") == "Other"
        for label in ("University attended", "Graduate school"):
            assert any(s.startswith(f"{label} — ") and "not in the list" in s
                       for s in report.skipped), report.skipped

        # …and the "please specify" box that "Other" reveals gets the real school name, so the
        # employer still reads it — that is what makes "Other" an honest answer.
        assert page.locator("#school-other").input_value() == PENN
    _drive(run)


def _main() -> int:
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                fails += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())

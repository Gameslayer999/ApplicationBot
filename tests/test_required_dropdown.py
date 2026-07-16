"""Required dropdowns/selects with no mapped answer get a weak-model choice (this session's change).

A required dropdown the resolver, semantic classify, and hints all miss would block an armed submit.
`choose_required_option` lets the weak model pick the best-fitting OFFERED option (never an invented
one), grounded in the résumé — but REFUSES demographic/EEO and fact-owning questions (clearance, GPA,
citizenship), which stay for the user. Verified both as a unit (the gate) and end-to-end by driving the
real committed `required_dropdowns.html` headless (stubbed CLI, zero tokens).
"""
from __future__ import annotations

import json
from pathlib import Path

from applicationbot import answer_bank, backends
from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Experience, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "required_dropdowns.html").as_uri()

TEAM = "Which team are you most interested in joining?"
PROD = "Which product area excites you most?"
CLEARANCE = "What is your current security clearance level?"
GENDER = "Gender"


def _resume() -> Resume:
    return Resume(contact=Contact(name="Test User", email="t@example.com"),
                  experience=[Experience(organization="Acme", role="Software Engineer",
                                         start="2019", end="Present", bullets=["Built web apps."])])


def _resolver() -> AnswerResolver:
    profile = ApplicationProfile(first_name="Test", last_name="User", email="t@example.com")
    return AnswerResolver(resume=_resume(), profile=profile, enable_generation=True,
                          company="Acme", jd="Build developer tools.")


# ----------------------------------------------------------------- unit: the honesty gate

def test_choose_required_option_gate(monkeypatch):
    monkeypatch.setattr(backends, "run_claude_cli", lambda *a, **k: json.dumps({"choice": 0}))
    resume = _resume()
    # Answerable, non-sensitive → picks the offered option Claude chose (index 0).
    assert answer_bank.choose_required_option(
        TEAM, ["Engineering", "Sales", "Marketing"], resume) == "Engineering"
    # Fact the applicant owns (enumerated) and demographic → refused WITHOUT any CLI call.
    assert answer_bank.choose_required_option(CLEARANCE, ["None", "Secret", "Top Secret"], resume) is None
    assert answer_bank.choose_required_option(GENDER, ["Male", "Female", "Decline"], resume) is None
    # No real options → None.
    assert answer_bank.choose_required_option(TEAM, [], resume) is None


def test_choose_required_option_declines(monkeypatch):
    # Model returns -1 (nothing honestly fits) → None, and an out-of-range index → None.
    monkeypatch.setattr(backends, "run_claude_cli", lambda *a, **k: json.dumps({"choice": -1}))
    assert answer_bank.choose_required_option(TEAM, ["Engineering", "Sales"], _resume()) is None


def test_choose_option_off_when_generation_disabled():
    r = AnswerResolver(resume=_resume(),
                       profile=ApplicationProfile(first_name="Test", last_name="User"),
                       enable_generation=False)
    assert r.choose_option(TEAM, ["Engineering", "Sales"]) is None


# ----------------------------------------------------------- end-to-end: drive the real fixture

def _fake_cli(prompt, **kw):
    """Shape-aware stub: choose index 0 for our option-choice prompt; make the batch classify a no-op
    (an empty `types` list mismatches the count, so nothing maps) → round 2 reaches choose_option."""
    if "REQUIRED job-application dropdown" in prompt:
        return json.dumps({"choice": 0})
    return json.dumps({"types": []})


def test_fixture_fills_answerable_required_dropdowns_and_refuses_sensitive(monkeypatch):
    monkeypatch.setattr(backends, "run_claude_cli", _fake_cli)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(FIXTURE)
        try:
            report = ApplyReport(url=FIXTURE, ats="fixture")
            _fill_page(page, _resolver(), report, done=set())
        finally:
            browser.close()

    filled = {f.label: f for f in report.filled}
    # The two answerable required dropdowns are filled with the first real option, marked as a
    # model pick (source option:claude) — placeholders are never chosen.
    assert filled[TEAM].value == "Engineering" and filled[TEAM].source == "option:claude"
    assert filled[PROD].value == "Developer tools" and filled[PROD].source == "option:claude"
    # The clearance (enumerated fact) and gender (demographic) dropdowns are NOT auto-filled —
    # captured for the user instead.
    assert CLEARANCE not in filled and GENDER not in filled
    assert any("security clearance" in s for s in report.skipped)
    assert any(s.startswith("Gender") for s in report.skipped)
    assert CLEARANCE in report.captured and GENDER in report.captured

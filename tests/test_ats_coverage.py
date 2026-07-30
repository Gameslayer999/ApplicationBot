"""ATS coverage: Jobvite + BambooHR become fillable, iCIMS/Taleo/Avature are refused honestly.

Decision 168. Probing live postings from the curated feeds split the five portals in two:

  * **Jobvite / BambooHR** serve the application form to anyone — Jobvite at `<posting>/apply`,
    BambooHR on the posting page itself. They only needed detection, the fillability gate, and the
    two things their real forms do that no existing fixture covered: BambooHR's honeypot and its
    button-facade selects, and the split Address/City/State/ZIP block both of them require.
  * **iCIMS / Taleo / Avature** answer Apply with a sign-in or create-an-account step. The
    account wall has 2-4 boxes, so the form detector reads it as a short application form and the
    resolver would run over "User Name"/"Password" and bank them as screening questions. They are
    gated out of the pipeline and refused with the reason at the browser.

Run:  python -m pytest tests/test_ats_coverage.py   (needs chromium installed)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from applicationbot import parking
from applicationbot.apply import (AnswerResolver, ApplyReport, _account_wall_evidence,
                                  _fill_page, _open_application_form, detect_ats)
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.discovery import ACCOUNT_GATED_ATS, Posting, detect_ats_from_url
from applicationbot.models import Contact, Resume
from applicationbot.pipeline import _is_fillable

REPO = Path(__file__).resolve().parent.parent
FORMS = REPO / "fixtures" / "apply_forms"
BAMBOO = (FORMS / "bamboohr_apply.html").as_uri()
JOBVITE = (FORMS / "jobvite_apply.html").as_uri()
WALL = (FORMS / "account_wall.html").as_uri()


def _resolver() -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com",
                                    phone="555-0100", location="Edison, NJ"))
    profile = ApplicationProfile(
        first_name="Test", last_name="User", email="t@example.com", phone="555-0100",
        location="Edison, NJ", street_address="12 Oak St", postal_code="08817",
        country="United States", linkedin_url="https://linkedin.com/in/test")
    return AnswerResolver(resume=resume, profile=profile, enable_generation=False)


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


def _fill(url, ats, ids=()):
    """Fill the fixture and return (report, {label: value}, {id: control value}). The DOM values
    are read INSIDE the browser context — a Playwright locator is dead once the browser closes."""
    def run(page):
        report = ApplyReport(url=url, ats=ats)
        _fill_page(page, _resolver(), report, done=set())
        dom = {i: page.locator(f"#{i}").input_value() for i in ids}
        return report, {f.label: f.value for f in report.filled}, dom
    return _drive(url, run)


# ------------------------------------------------------------------------------ detection

def test_detect_ats_names_the_five_new_portals():
    for url, want in (
        ("https://jobs.jobvite.com/visionist/job/oEBdAfwM", "jobvite"),
        ("https://alkira.bamboohr.com/careers/232/", "bamboohr"),
        ("https://careers-lynker.icims.com/jobs/1633/job", "icims"),
        ("https://wvu.taleo.net/careersection/faculty/jobdetail.ftl?job=28955", "taleo"),
        ("https://delta.avature.net/en_US/careers/JobDetail?jobId=32774", "avature"),
    ):
        assert detect_ats(url) == want, url
        assert detect_ats_from_url(url) == want, url


def test_detection_of_the_existing_atss_is_unchanged():
    for url, want in (
        ("https://job-boards.greenhouse.io/twitch/jobs/8329910002", "greenhouse"),
        ("https://jobs.lever.co/commercearchitects/ec4bd3b5/apply", "lever"),
        ("https://jobs.ashbyhq.com/magical/2c4734af", "ashby"),
        ("https://jobs.smartrecruiters.com/ServiceNow/744000107369741", "smartrecruiters"),
        ("https://gdit.wd5.myworkdayjobs.com/external_career_site/job/x", "workday"),
    ):
        assert detect_ats(url) == want, url


# ------------------------------------------------------------------------ fillability gate

def _p(ats: str, **extra) -> Posting:
    return Posting(company="Acme", title="SWE", body="jd", url=f"https://x/{ats}",
                   ats=ats, extra=dict(extra))


def test_jobvite_and_bamboohr_are_fillable():
    assert _is_fillable(_p("jobvite")) is True
    assert _is_fillable(_p("bamboohr")) is True


def test_account_gated_portals_stay_gated_out():
    for ats in ACCOUNT_GATED_ATS:
        assert _is_fillable(_p(ats)) is False, ats
    # …and the set is exactly the three the probe found, so adding a portal to FILLABLE_ATS
    # without verifying its form can't silently pass through here.
    assert ACCOUNT_GATED_ATS == {"icims", "taleo", "avature"}


# ---------------------------------------------------------------- the split address block

def test_address_block_answers_each_part_with_that_part_alone():
    r = _resolver()
    assert r.resolve("Address *") == "12 Oak St"
    assert r.resolve("City *") == "Edison"       # NOT "Edison, NJ" — that is a wrong City answer
    assert r.resolve("State *") == "New Jersey"
    assert r.resolve("Zip*") == "08817"
    assert r.resolve("Country *") == "United States"


def test_the_whole_location_question_still_gets_the_whole_location():
    r = _resolver()
    assert r.resolve("Where are you based?") == "Edison, NJ"
    assert r.resolve("Location (City)") == "Edison, NJ"


def test_address_and_state_rules_cannot_be_triggered_by_lookalike_questions():
    r = _resolver()
    assert r.resolve("Email Address *") == "t@example.com"   # the email rule wins
    assert r.resolve("IP address") is None
    assert r.resolve("Please state your desired salary") is None
    assert r.resolve("Race/Ethnicity") is None               # "ethni-CITY" must not match city


# -------------------------------------------------------------------------- BambooHR form

BAMBOO_IDS = ("nickname_hpcsaf", "fab-select323", "fab-select325", "firstName", "email",
              "linkedinUrl")


def test_bamboohr_honeypot_is_left_empty_and_reported():
    report, filled, dom = _fill(BAMBOO, "bamboohr", BAMBOO_IDS)
    assert dom["nickname_hpcsaf"] == ""
    assert "Please leave this field blank" not in filled
    # Never offered to the user as a question either — it is not a question.
    assert "Please leave this field blank" not in report.captured
    assert any("honeypot" in n for n in report.notes), report.notes


def test_bamboohr_facade_select_fills_the_required_state():
    report, filled, dom = _fill(BAMBOO, "bamboohr", BAMBOO_IDS)
    assert dom["fab-select323"] == "New Jersey"
    assert filled.get("State") == "New Jersey"


def test_bamboohr_prefilled_country_facade_is_kept_as_native():
    report, filled, dom = _fill(BAMBOO, "bamboohr", BAMBOO_IDS)
    assert dom["fab-select325"] == "US"          # the posted code is left exactly as the form set it
    src = {f.label: f.source for f in report.filled}
    assert src.get("Country") == "native"
    assert filled.get("Country") == "United States"   # …but the report shows what the user sees


def test_bamboohr_ordinary_fields_fill():
    report, filled, dom = _fill(BAMBOO, "bamboohr", BAMBOO_IDS)
    assert dom["firstName"] == "Test"
    assert dom["email"] == "t@example.com"
    assert dom["linkedinUrl"] == "https://linkedin.com/in/test"


# --------------------------------------------------------------------------- Jobvite form

JV_IDS = ("jv-field-yD5jZfwK", "jv-field-yE5jZfwL", "jv-field-yI5jZfwP", "jv-field-yJ5jZfwQ",
          "jv-field-yL5jZfwS", "jv-field-yK5jZfwR")


def test_jobvite_fills_with_no_jobvite_specific_code():
    report, filled, dom = _fill(JOBVITE, "jobvite", JV_IDS)
    assert dom["jv-field-yD5jZfwK"] == "Test"          # First Name*
    assert dom["jv-field-yE5jZfwL"] == "t@example.com"  # Email Address*
    assert dom["jv-field-yI5jZfwP"] == "12 Oak St"     # Address*
    assert dom["jv-field-yJ5jZfwQ"] == "Edison"        # City*
    assert dom["jv-field-yL5jZfwS"] == "08817"         # Zip*
    assert dom["jv-field-yK5jZfwR"] == "NJ"            # State* <select>


# ------------------------------------------------------------------------ the account wall

def test_account_wall_is_refused_for_an_account_gated_ats():
    def run(page):
        report = ApplyReport(url=WALL, ats="taleo")
        ok, frame, ats = _open_application_form(page, "taleo", report, timeout_ms=4000, replay=False)
        return ok, report
    ok, report = _drive(WALL, run)
    assert ok is False
    assert report.errors and "requires an account" in report.errors[0]
    assert "password field" in report.errors[0]


def test_a_refused_account_wall_parks_as_login_not_as_a_dead_end():
    def run(page):
        report = ApplyReport(url=WALL, ats="taleo")
        _open_application_form(page, "taleo", report, timeout_ms=4000, replay=False)
        return report
    reason = parking.classify(_drive(WALL, run))
    assert reason is not None and reason.kind == parking.LOGIN
    assert reason.resolve == "credentials" and reason.resumable is True


def test_the_refusal_is_scoped_to_account_gated_atss_only():
    # Guideline #7: Workday creates an account mid-application on purpose, and its adapter handles
    # the password page. A non-account-gated ATS must still open the same page as before.
    def run(page):
        report = ApplyReport(url=WALL, ats="lever")
        ok, frame, ats = _open_application_form(page, "lever", report, timeout_ms=4000, replay=False)
        return ok, report
    ok, report = _drive(WALL, run)
    assert ok is True
    assert not any("requires an account" in e for e in report.errors)


@pytest.mark.parametrize("url,is_wall", [
    ("https://careers-lynker.icims.com/jobs/1633/marine-sensor-data-scientist/login", True),
    ("https://aarcorp.taleo.net/careersection/iam/accessmanagement/login.jsf?lang=en", True),
    # A posting whose SLUG contains the word is not a sign-in page.
    ("https://careers-x.icims.com/jobs/42/senior-login-systems-engineer/job", False),
    ("https://careers-x.icims.com/jobs/42/job", False),
])
def test_login_urls_are_recognised_without_matching_posting_slugs(url, is_wall):
    class _F:
        def __init__(self, u): self.url = u
        def locator(self, *_a, **_k): raise RuntimeError("no DOM in this check")
    got = _account_wall_evidence(_F(url), _F(url))
    assert bool(got) is is_wall, got

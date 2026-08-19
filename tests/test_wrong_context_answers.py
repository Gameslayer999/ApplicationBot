"""An answer that is well-formed but belongs to a DIFFERENT question (decision 166).

The failure this pins is not a blank field — it's a filled one. A live Palantir dry-run put
"I'm available immediately, with confirmed graduation in May 2027." into the signature-line "Date"
box at the bottom of the form, and on a second posting put "Yes" there. Both read as "filled" in
the review table and in the screenshot.

Two causes, both covered here:
  * **The source** — `AnswerResolver.banked_qa` matched a label against any banked question that
    CONTAINED it, with a length guard on the banked question only. The 4-character label "Date"
    therefore matched "…earliest start date?" and "…receive updates…". Now both sides must be
    long, and a form's own signature date resolves to today before the bank is consulted at all.
  * **The review panel** — every filled row looked alike, so a wrong-context answer was invisible.
    `_answer_flag` now checks each answer's SHAPE against its question and the panel highlights
    the mismatches with what to check.
"""
from __future__ import annotations

import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from applicationbot import apply_profile, archive, web
from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import QA, ApplicationProfile, save_profile
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
FORM = (REPO / "fixtures" / "apply_forms" / "signature_date.html").as_uri()
TODAY = date.today().isoformat()

# The bank as the live run had it: a long start-date question whose answer the bare "Date" field
# stole, and an updates opt-in whose "Yes" it stole on the next posting.
BANK = [QA(question="What is your earliest available start date?",
           answer="I'm available immediately, with confirmed graduation in May 2027."),
        QA(question="Would you like to receive updates about future roles?", answer="Yes")]


def _resolver(**kw) -> AnswerResolver:
    profile = ApplicationProfile(first_name="Test", last_name="User", email="t@example.com",
                                 earliest_start_date="Immediately", custom_answers=list(BANK), **kw)
    return AnswerResolver(resume=Resume(contact=Contact(name="Test User", email="t@example.com")),
                          profile=profile, enable_generation=False)


# ------------------------------------------------------------------------ the source (resolver)

@pytest.mark.parametrize("label", ["Date", "Date:", "Date *", "Today's date", "Current date",
                                   "Date signed", "Date of application", "Date submitted"])
def test_a_forms_signature_date_is_the_day_you_apply(label):
    assert _resolver().resolve(label) == TODAY


@pytest.mark.parametrize("label,expected", [
    # Dates ABOUT the applicant stay theirs to state — availability answers from the profile,
    # and the ones we hold no field for are captured for the user rather than stamped with today.
    ("Earliest start date", "Immediately"),
    ("Date available to start", "Immediately"),
    ("Date of birth", None),
    ("Graduation date", None),
    ("What dates are you available to interview?", None),
])
def test_dates_about_the_applicant_are_not_answered_with_today(label, expected):
    assert _resolver().resolve(label) == expected


def test_a_short_label_no_longer_takes_a_long_banked_questions_answer():
    """The bug's mechanism, isolated: containment matching needs BOTH sides long. "Date" is a
    substring of both banked questions above — it must match neither."""
    r = _resolver()
    assert r.banked_qa("Date") is None
    # The banked question still answers its OWN phrasings, exact and reworded-but-long.
    assert r.banked_qa("What is your earliest available start date?") is not None
    assert r.banked_qa("So, what is your earliest available start date?") is not None


def test_signature_date_fills_and_applicant_dates_do_not(tmp_path):
    """The whole fill against a real form's closing block: the signature "Date" (text) and
    "Today's date" (a native date picker) get today; the applicant's own date fields don't."""
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=FORM, ats="greenhouse")
    resolver = _resolver()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(FORM)
        try:
            _fill_page(page, resolver, report, done=set())
            values = {i: page.input_value(f"#{i}") for i in
                      ("today", "signed", "dob", "grad", "startdate")}
        finally:
            browser.close()

    assert values["today"] == TODAY
    assert values["signed"] == TODAY          # a native date input takes YYYY-MM-DD only
    assert values["startdate"] == "Immediately"
    assert values["dob"] == "" and values["grad"] == ""
    assert {"Date of birth", "Graduation date"} <= set(report.captured)


# ------------------------------------------------- the surrounding text decides (decision 167)

@pytest.mark.parametrize("context,expected", [
    ("", TODAY),                                                    # nothing around it — today
    ("Applicant certification · follows the field: Signature", TODAY),
    ("I certify the above is true and complete · Signature", TODAY),
    ("Education · follows the field: School", None),                # the applicant's date, not today
    ("Previous employment · follows the field: Employer", None),
    ("Date of birth", None),
    ("Passport · expiration", None),
])
def test_a_bare_date_is_answered_from_the_text_around_it(context, expected):
    r = _resolver()
    r.note_context("Date", context)
    assert r.resolve("Date") == expected


def test_context_is_read_off_the_live_form_and_vetoes_today(tmp_path):
    """Driven against a real form: the same bare "Date" label that gets today in a signature block
    is left for the user inside an education block, because that is what the page says it is."""
    from playwright.sync_api import sync_playwright

    edu = (REPO / "fixtures" / "apply_forms" / "context_dates.html").as_uri()
    report = ApplyReport(url=edu, ats="greenhouse")
    resolver = _resolver()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(edu)
        try:
            _fill_page(page, resolver, report, done=set())
            value = page.input_value("#edate")
        finally:
            browser.close()

    assert value == ""                                   # never stamped with today
    assert "Date" in report.captured                     # captured for the user instead
    # …and the panel can say why: the heading, the sentence above it, and the field it follows.
    around = report.context.get("Date", "")
    assert "Education" in around and "school you attended" in around and "Degree" in around


def test_a_generic_label_is_never_added_to_the_shared_answer_bank(tmp_path):
    """The re-poisoning loop closed: "Date" was learned as a bank entry after the first bad fill,
    which is what made the wrong answer come back on the next posting."""
    from applicationbot.apply_profile import load_profile, remember_answers

    path = tmp_path / "profile.yaml"
    save_profile(ApplicationProfile(first_name="Test"), path)
    added = remember_answers([QA(question="Date", answer="2026-07-30", generated=True),
                              QA(question="If yes, please explain", answer="n/a", generated=True),
                              QA(question="How many years of Python do you have?", answer="3")],
                             path)
    assert added == 1
    assert [qa.question for qa in load_profile(path).custom_answers] \
        == ["How many years of Python do you have?"]


# ------------------------------------------------------------- the review panel (shape checks)

@pytest.mark.parametrize("label,value,control,flagged", [
    ("Date", "I'm available immediately, with confirmed graduation in May 2027.", "text", True),
    ("Date", "Yes", "text", True),
    ("Date", TODAY, "text", False),
    ("Date", "07/30/2026", "text", False),
    ("Date of birth", "March 4 1999", "text", False),
    # A start-date question legitimately takes prose — flagging it would be noise.
    ("Earliest start date", "Immediately", "text", False),
    # Long questions that merely mention dates are prose questions, not date fields.
    ("Have you interned here before? If so, what dates?", "No, I have not.", "text", False),
    ("How many years of Python experience do you have?", "Several", "text", True),
    ("Email", "Gabriel Chan", "text", True),
    ("Phone", "9084870509", "text", False),
    ("LinkedIn Profile", "Gabriel Chan", "text", True),
    ("LinkedIn Profile", "https://www.linkedin.com/in/x/", "text", False),
    ("Are you legally authorized to work in the United States?",
     "I am authorized and excited to contribute to your platform team.", "text", True),
    ("Are you legally authorized to work in the United States?", "Yes", "text", False),
    # An option the FORM offered can't be off-shape, however long it reads.
    ("Are you comfortable working in-person at our NYC office at least 3 days/week?",
     "Yes | Able to relocate to NYC upon offer acceptance", "radio", False),
    # A question that invites prose is not a Yes/No question.
    ("Do you have examples of exceptional performance you want to highlight?",
     "I rebuilt the ingest pipeline and cut p99 latency by 40%.", "text", False),
    # The inverse mismatch, from the same live Palantir run: a which/why question answered "Yes".
    ("At Palantir we have two main Software Engineering roles: FDSE and SWE. Which of these "
     "roles resonates the most with your job search and why? More details found here: "
     "https://blog.palantir.com/dev-versus-delta-demystifying-engineering-roles", "Yes", "text",
     True),
    # …but that same question answered properly is fine — and the URL in it must not read as a
    # "GPA" question ("blo(g.pa)lantir.com" normalises to "blogpalantircom").
    ("Which role resonates most with your job search and why? See https://blog.palantir.com/x",
     "Forward Deployed Software Engineer — I want the customer-facing half of the work.",
     "text", False),
])
def test_answer_flag_catches_wrong_shape_only(label, value, control, flagged):
    assert bool(web._answer_flag(label, value, control)) is flagged


READY = {"id": 12, "company": "Palantir", "role": "FDSE", "status": "dry-run", "portal": "greenhouse",
         "fit_score": 88, "location": "NYC", "remote": "no", "pay": "$150k",
         "source_url": "http://example.invalid/12", "resume_source": "Tailored fresh",
         "resume_path": ""}
KEY = (READY["company"], READY["role"], READY["source_url"])

REPORT = {
    "when": "2026-07-30T09:00:00",
    "filled": [
        {"label": "Full name", "value": "Jane Doe", "source": "resolver", "control": "text"},
        {"label": "Date", "source": "resolver", "control": "text",
         "value": "I'm available immediately, with confirmed graduation in May 2027."},
        {"label": "Why Palantir?", "value": "Drafted answer.", "source": "generated",
         "control": "text"},
    ],
    "skipped": [],
    "captured": {},
    "required": {"Full name": True, "Date": True, "Why Palantir?": True},
    "context": {"Date": "Applicant certification · follows the field: Signature"},
}


@pytest.fixture
def ui(monkeypatch, tmp_path):
    """The real UI on a free port, serving one prepared application whose archived report carries
    the wrong-context answer. Archive, profile and résumé are temp/fake — never the user's own."""
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "applications")
    adir = archive.dir_for(*KEY)
    adir.mkdir(parents=True)
    (adir / "report.json").write_text(json.dumps(REPORT), encoding="utf-8")
    prof = tmp_path / "application_profile.yaml"
    monkeypatch.setattr(apply_profile, "DEFAULT_PATH", str(prof))
    save_profile(ApplicationProfile(first_name="Jane", email="jane@example.com"), prof)
    monkeypatch.setattr(web, "load_resume",
                        lambda _p: Resume(contact=Contact(name="Jane Doe", email="jane@example.com")))
    state = dict(web._loop_reset())
    state.update({"running": True, "phase": "preparing", "message": "Preparing…",
                  "prepared": 1, "ready_ids": [READY["id"]]})
    monkeypatch.setattr(web, "_LOOP_STATE", state)
    monkeypatch.setattr(web.tracker, "get_application",
                        lambda aid: dict(READY) if aid == READY["id"] else None)
    monkeypatch.setattr(web, "_mark_reviewed", lambda aid: None)  # fake id — don't touch the real DB
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def test_review_data_flags_the_wrong_context_answer(ui):
    rows = {f["label"]: f for f in web._review_data(READY["id"])["filled"]}
    assert "date" in rows["Date"]["flag"].lower()
    assert "flag" not in rows["Full name"]


def test_review_panel_highlights_it_and_says_what_to_check(ui):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(ui)
        page.evaluate("localStorage.setItem('ab-tour-done', '1')")  # the first-run tour eats clicks
        page.reload()
        page.click('.tab[data-view="discover"]')
        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#review-panel .rv-fields", timeout=10_000)

        body = page.inner_text("#review-panel")
        assert "1 answer doesn't match what the question asks" in body
        flagged = page.query_selector_all("#review-panel tr.rv-flagged")
        assert len(flagged) == 1
        assert flagged[0].query_selector(".rv-fl div").inner_text().strip() == "Date"
        assert "asks for a date" in flagged[0].query_selector(".rv-flagwhy").inner_text()
        # …and the row says which "Date" the form was asking for (decision 167).
        assert flagged[0].query_selector(".rv-around").inner_text() \
            == "on the form: Applicant certification · follows the field: Signature"
        # A drafted answer is labelled as one — the other kind of right-form/wrong-context answer.
        assert "AI-drafted" in body
        assert errors == []
        browser.close()

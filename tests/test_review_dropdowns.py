"""A dropdown question is reviewed as a dropdown, not as a text box (decision 165).

Two halves, both broken before:
  * **Capture** — `_record_capture` only ran for fields we could NOT answer, so an ANSWERED
    `<select>` / radio group / static combobox recorded no options at all. The review panel builds
    its editor from those options, so every answered dropdown rendered as a free-text box.
  * **Render** — even with options, `answerRow` only special-cased check-all-that-apply groups;
    anything else fell through to a text box. Typing free text where the form offers a fixed list
    is how an answer that matches no option gets submitted (or dropped).

Verified against the committed Lever fixture, whose EEO `<select>`s and Yes/No radio cards are
answered straight from the profile — the same shape as the real Palantir/Greenhouse forms.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from applicationbot import answer_overrides, apply_profile, archive, web
from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import ApplicationProfile, save_profile
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
LEVER = (REPO / "fixtures" / "apply_forms" / "lever_custom_cards.html").as_uri()

AUTH = "Are you legally authorized to work in the United States?"
GENDER_OPTS = ["Male", "Female", "Decline to self-identify"]


def _profile() -> ApplicationProfile:
    return ApplicationProfile(first_name="Test", last_name="User", email="t@example.com",
                              work_authorized=True, requires_sponsorship=False,
                              gender="Male", race_ethnicity="Asian (Not Hispanic or Latino)")


# ------------------------------------------------------------------ capture (headless fill)

def test_answered_dropdowns_and_radio_groups_record_their_options():
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=LEVER, ats="lever")
    resolver = AnswerResolver(
        resume=Resume(contact=Contact(name="Test User", email="t@example.com")),
        profile=_profile(), enable_generation=False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(LEVER)
        try:
            _fill_page(page, resolver, report, done=set())
        finally:
            browser.close()

    answered = {f.label: f for f in report.filled}
    assert answered["Gender"].value == "Male"                 # it WAS answered…
    assert report.captured["Gender"]["kind"] == "select"      # …and still records its control
    assert report.captured["Gender"]["options"] == GENDER_OPTS
    # A radio group is captured under its card question, with the option labels as its choices.
    assert answered[AUTH].value == "Yes"
    assert report.captured[AUTH] == {"kind": "radio", "options": ["Yes", "No"]}
    # A native <select>'s placeholder is not a choice — offering "Select ..." as an answer would
    # commit an empty selection at fill time.
    assert "Select ..." not in report.captured["Veteran status"]["options"]


# ------------------------------------------------------------------------ the panel editor

READY = {"id": 12, "company": "Whoop", "role": "Backend Eng", "status": "dry-run", "portal": "lever",
         "fit_score": 91, "location": "Boston", "remote": "no", "pay": "$150k",
         "source_url": "http://example.invalid/12", "resume_source": "Tailored fresh",
         "resume_path": ""}
KEY = (READY["company"], READY["role"], READY["source_url"])

REPORT = {
    "when": "2026-07-30T09:00:00",
    "filled": [
        {"label": "Full name", "value": "Test User", "source": "resolver", "control": "text"},
        {"label": "Gender", "value": "Male", "source": "resolver", "control": "select"},
        {"label": AUTH, "value": "Yes", "source": "resolver", "control": "radio"},
    ],
    "skipped": ["Veteran status — no saved answer"],
    "captured": {
        "Gender": {"kind": "select", "options": GENDER_OPTS},
        AUTH: {"kind": "radio", "options": ["Yes", "No"]},
        "Veteran status": {"kind": "select",
                           "options": ["I am a veteran", "I am not a veteran",
                                       "Decline to self-identify"]},
    },
    "required": {"Full name": True, "Gender": False, AUTH: True, "Veteran status": False},
}


@pytest.fixture
def ui(monkeypatch, tmp_path):
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "applications")
    adir = archive.dir_for(*KEY)
    adir.mkdir(parents=True)
    (adir / "report.json").write_text(json.dumps(REPORT), encoding="utf-8")
    prof = tmp_path / "application_profile.yaml"
    monkeypatch.setattr(apply_profile, "DEFAULT_PATH", str(prof))
    save_profile(_profile(), prof)
    monkeypatch.setattr(web, "load_resume",
                        lambda _p: Resume(contact=Contact(name="Test User", email="t@example.com")))
    state = dict(web._loop_reset())
    state.update({"running": True, "phase": "preparing", "message": "Preparing…",
                  "prepared": 1, "ready_ids": [READY["id"]]})
    monkeypatch.setattr(web, "_LOOP_STATE", state)
    monkeypatch.setattr(web.tracker, "get_application",
                        lambda aid: dict(READY) if aid == READY["id"] else None)
    monkeypatch.setattr(web, "_mark_reviewed", lambda aid: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def _open_review(page, url):
    page.goto(url)
    page.evaluate("localStorage.setItem('ab-tour-done', '1')")  # the first-run tour overlays clicks
    page.reload()
    page.click('.tab[data-view="discover"]')
    page.click("#loop-ready .pkcard .review-toggle")
    page.wait_for_selector("#loop-ready .review .rv-fields", timeout=10_000)


def test_review_panel_edits_a_dropdown_answer_as_a_dropdown(ui):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _open_review(page, ui)

        rows = {tr.query_selector(".rv-fl div").inner_text().strip(): tr
                for tr in page.query_selector_all("#loop-ready .review .rv-fields tr")}
        # An ANSWERED dropdown is a <select> holding its current answer, offering the form's list.
        gender = rows["Gender"].query_selector("select")
        assert gender is not None, "an answered dropdown must not be edited as a text box"
        assert gender.input_value() == "Male"
        offered = [o.inner_text().strip() for o in gender.query_selector_all("option")]
        assert offered[:4] == ["— choose an option —"] + GENDER_OPTS
        assert offered[-1] == "Type a different value…"   # never a dead end (see the next test)
        # A Yes/No radio group is a picker too, and an UNANSWERED dropdown offers its options
        # instead of an empty text box.
        assert rows[AUTH].query_selector("select").input_value() == "Yes"
        assert rows["Veteran status"].query_selector("select").input_value() == ""
        # A field with no captured options is still a plain text box.
        assert rows["Full name"].query_selector("select") is None

        gender.select_option("Decline to self-identify")
        rows["Veteran status"].query_selector("select").select_option("I am not a veteran")
        page.click("#loop-ready .review .rv-save button")
        page.wait_for_selector("#loop-ready .review .rv-save .rv-note.rv-ok", timeout=10_000)

        assert answer_overrides.load(*KEY) == {"Gender": "Decline to self-identify",
                                              "Veteran status": "I am not a veteran"}
        assert errors == []
        browser.close()


def test_dropdown_editor_can_still_type_a_value_the_captured_list_lacks(ui):
    """The captured list can be short of the form's real one (long lists are truncated when
    scanned, and a posting can change its options), so the editor must never trap the user in it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _open_review(page, ui)

        row = [tr for tr in page.query_selector_all("#loop-ready .review .rv-fields tr")
               if tr.query_selector(".rv-fl div").inner_text().strip() == "Gender"][0]
        row.query_selector("select").select_option("__applicationbot_type_your_own__")
        page.wait_for_selector("#loop-ready .review .rv-fields tr .rv-choice-back", timeout=5_000)
        box = row.query_selector('input[type="text"]')
        box.fill("Non-binary")
        page.click("#loop-ready .review .rv-save button")
        page.wait_for_selector("#loop-ready .review .rv-save .rv-note.rv-ok", timeout=10_000)

        assert answer_overrides.load(*KEY) == {"Gender": "Non-binary"}
        # …and the way back to the form's own options is one click.
        row.query_selector(".rv-choice-back").click()
        assert row.query_selector("select") is not None
        assert errors == []
        browser.close()

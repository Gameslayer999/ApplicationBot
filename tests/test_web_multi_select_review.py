"""A check-all-that-apply question is edited as CHECKBOXES in the Review panel too.

Same widget as the Profile screen (decision 156), fed by the fill report the dry-run archived:
`_fill_checkboxes` reports one row per checked option, so the panel must fold them into a single
question, show every option the form offered, and save the chosen set as one "A; B" answer — the
format the next fill splits to tick each box. Drives the real UI headless, through the real
`_review_data`.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from applicationbot import answer_overrides, apply_profile, archive, web
from applicationbot.apply_profile import ApplicationProfile, load_profile, save_profile
from applicationbot.models import Contact, Resume

LANG_Q = "Language Skill(s) (Check all that apply)"
LANG_OPTS = ["English (ENG)", "Spanish (SPA)", "French (FRA)", "German (DEU)"]
SHIFT_Q = "Which shifts can you work? (check all that apply)"
SHIFT_OPTS = ["Mornings", "Evenings", "Weekends"]

READY = {"id": 7, "company": "Acme", "role": "Backend Eng", "status": "dry-run", "portal": "lever",
         "fit_score": 88, "location": "Remote", "remote": "yes", "pay": "$120k",
         "source_url": "http://example.invalid/7", "resume_source": "Tailored fresh",
         "resume_path": ""}
KEY = (READY["company"], READY["role"], READY["source_url"])

REPORT = {
    "when": "2026-07-30T10:00:00",
    "filled": [
        {"label": "Email", "value": "jane@example.com", "source": "", "control": "text"},
        # One row per CHECKED option — exactly what _fill_checkboxes writes.
        {"label": LANG_Q, "value": "English (ENG)", "source": "", "control": "checkbox"},
        {"label": LANG_Q, "value": "Spanish (SPA)", "source": "", "control": "checkbox"},
    ],
    "skipped": [SHIFT_Q + " — no saved answer"],
    "captured": {LANG_Q: {"kind": "checkbox", "options": LANG_OPTS},
                 SHIFT_Q: {"kind": "checkbox", "options": SHIFT_OPTS}},
}


@pytest.fixture
def ui(monkeypatch, tmp_path):
    """The real UI on a free port with one ready application whose archived fill report contains a
    checkbox group — overrides, profile and answer bank all in a temp dir (never the user's PII)."""
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
    yield f"http://127.0.0.1:{srv.server_address[1]}/", prof
    srv.shutdown()


def test_review_data_folds_a_checkbox_group_into_one_answer(ui):
    r = web._review_data(READY["id"])
    langs = [f for f in r["filled"] if f["label"] == LANG_Q]
    assert len(langs) == 1, "the group is ONE question, not one row per checked option"
    assert langs[0]["value"] == "English (ENG); Spanish (SPA)"
    assert langs[0]["options"] == LANG_OPTS          # the panel can offer the rest
    assert langs[0]["kind"] == "checkbox"
    email = [f for f in r["filled"] if f["label"] == "Email"][0]
    assert "options" not in email                    # a text answer stays a text answer
    shifts = [u for u in r["unanswered"] if u["label"] == SHIFT_Q][0]
    assert (shifts["kind"], shifts["options"]) == ("checkbox", SHIFT_OPTS)


def test_review_panel_edits_multi_answers_as_checkboxes_and_learns_them(ui):
    from playwright.sync_api import sync_playwright

    url, prof = ui
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.evaluate("localStorage.setItem('ab-tour-done', '1')")  # the first-run tour overlays clicks
        page.reload()
        page.click('.tab[data-view="discover"]')
        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#review-panel .qa-multi", timeout=10_000)

        grids = page.query_selector_all("#review-panel .qa-multi")
        assert len(grids) == 2, "both the answered and the unanswered group render as checkboxes"
        # The answered group: every option offered, the two it filled already checked.
        assert [(o.inner_text() or "").strip() for o in grids[0].query_selector_all(".qa-opt")] == LANG_OPTS
        checked = [c.get_attribute("value") for c in grids[0].query_selector_all("input:checked")]
        assert checked == ["English (ENG)", "Spanish (SPA)"]
        # Answer the unanswered group by checking two of its options, then add a third language.
        shifts = grids[1].query_selector_all('input[type="checkbox"]')
        shifts[0].check()
        shifts[2].check()
        grids[0].query_selector_all('input[type="checkbox"]')[2].check()   # + French
        page.click("#review-panel .rv-save button")
        page.wait_for_selector("#review-panel .rv-note.rv-ok", timeout=10_000)

        assert answer_overrides.load(*KEY) == {
            LANG_Q: "English (ENG); Spanish (SPA); French (FRA)",
            SHIFT_Q: "Mornings; Weekends"}
        # Learned into the bank WITH its control, so the Profile screen shows checkboxes too.
        banked = {qa.question: qa for qa in load_profile(prof).custom_answers}
        assert banked[SHIFT_Q].answer == "Mornings; Weekends"
        assert (banked[SHIFT_Q].input_kind, banked[SHIFT_Q].options) == ("checkbox", SHIFT_OPTS)
        assert errors == []
        browser.close()

"""Editing an answer in the real Review panel is saved, used, and learned (decisions 153/155).

Drives the actual UI headless: open a ready application's Review, type over one of the answers
the bot would submit, click Save answers, and assert the edit reached the per-posting override
store — the same store `run_apply` loads before it fills — and that a REUSABLE answer also
reached the shared answer bank, so the next posting asking it doesn't come back blank. Also
asserts the pre-submit auto-save: clicking "Watch it fill" with an unsaved edit saves it before
the browser is opened.
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from applicationbot import answer_overrides, apply_profile, web
from applicationbot.apply_profile import ApplicationProfile, load_profile, save_profile
from applicationbot.models import Contact, Resume

YEARS_Q = "How many years of Python do you have?"

READY = {"id": 7, "company": "Acme", "role": "Backend Eng", "status": "dry-run", "portal": "lever",
         "fit_score": 88, "location": "Remote", "remote": "yes", "pay": "$120k",
         "source_url": "http://example.invalid/7", "resume_source": "Tailored fresh",
         "resume_path": ""}
KEY = (READY["company"], READY["role"], READY["source_url"])


@pytest.fixture
def ui(monkeypatch, tmp_path):
    """The real UI on a free port with one ready application — its overrides, apply profile and
    answer bank all in a temp dir, so the test never reads or writes the user's own PII."""
    monkeypatch.setattr("applicationbot.archive.ARCHIVE_DIR", tmp_path / "applications")
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
    monkeypatch.setattr(web, "_review_data", lambda aid: {
        "id": aid,
        "posting": {"company": READY["company"], "role": READY["role"], "url": READY["source_url"],
                    "fit": READY["fit_score"], "status": READY["status"]},
        "jd": "Build things.",
        "filled": [{"label": "Email", "value": "a@b.c", "control": "text"}],
        "skipped": ["Why Acme? — no saved answer", YEARS_Q + " — no saved answer"],
        "unanswered": [{"label": "Why Acme?", "detail": "no saved answer",
                        "value": "", "edited": False},
                       {"label": YEARS_Q, "detail": "no saved answer",
                        "value": "", "edited": False}],
        "when": "just now", "has_resume": False, "has_screenshot": False})
    # "Watch it fill" must not launch a browser in a test — assert it was reached instead.
    watched: list[int] = []
    monkeypatch.setattr(web, "queue_watch", lambda aid: (watched.append(aid), {"ok": True, "queued": True})[1])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/", watched, prof
    srv.shutdown()


def test_edited_answers_are_saved_and_used(ui):
    from playwright.sync_api import sync_playwright

    url, watched, prof = ui
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
        page.wait_for_selector("#loop-ready .review .rv-edit", timeout=10_000)

        boxes = page.query_selector_all("#loop-ready .review .rv-edit")
        assert len(boxes) == 3, "every answer — filled and unanswered — must be editable"

        boxes[0].fill("edited@example.com")           # override what it would have submitted
        boxes[2].fill("Four years.")                  # a reusable answer — learn this one
        page.click("#loop-ready .review .rv-save button")
        page.wait_for_selector("#loop-ready .review .rv-note.rv-ok", timeout=10_000)
        assert answer_overrides.load(*KEY) == {"Email": "edited@example.com",
                                               YEARS_Q: "Four years."}
        # The reusable answer is banked, so the NEXT posting that asks it is answered (decision
        # 155); "Email" is answered by a profile rule that outranks the bank, so it is not banked
        # — and the panel says so instead of claiming it was learned.
        assert [(qa.question, qa.answer) for qa in load_profile(prof).custom_answers] \
            == [(YEARS_Q, "Four years.")]
        status = page.text_content("#loop-ready .review .rv-note.rv-ok")
        assert "1 saved to your answer bank" in status
        assert "Email is answered from your apply profile" in status

        # An unsaved edit is saved before the fill starts — never fill with what the user can't see.
        page.query_selector_all("#loop-ready .review .rv-edit")[1].fill("Your latency work.")
        page.click("#loop-ready .review .rv-signoff button")  # "Watch it fill"
        page.wait_for_function(
            "() => document.querySelector('#loop-msg') && document.querySelector('#loop-msg').textContent.includes('Queued')",
            timeout=10_000)
        assert watched == [READY["id"]]
        assert answer_overrides.load(*KEY) == {"Email": "edited@example.com",
                                              YEARS_Q: "Four years.",
                                              "Why Acme?": "Your latency work."}
        # "Why Acme?" is company-specific: it stays on this posting and never enters the bank.
        assert [qa.question for qa in load_profile(prof).custom_answers] == [YEARS_Q]
        assert errors == []
        browser.close()

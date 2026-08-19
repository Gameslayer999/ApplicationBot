"""A Review opened while the auto-apply loop is running must stay open, and close on demand.

`/loop/status` is polled every 2s while the loop runs, and `renderLoop` rebuilds the "Ready to
apply" list from each tick. The review is a popup over the page since decision 184 (before that it
expanded inside the card, where a rebuild threw it away mid-read). Verified by driving the real UI
headless: open Review, wait out three status polls, assert the popup is still open and populated —
then close it with Escape and with its own "Close review" button.
"""
from __future__ import annotations

import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from applicationbot import web

READY = {"id": 7, "company": "Acme", "role": "Backend Eng", "status": "dry-run", "portal": "lever",
         "fit_score": 88, "location": "Remote", "remote": "yes", "pay": "$120k",
         "source_url": "http://example.invalid/7", "resume_source": "Tailored fresh",
         "resume_path": ""}


@pytest.fixture
def loop_ui(monkeypatch):
    """Serve the real UI on a free port with the loop faked as running and one ready application.
    No browser automation of the bot, no Claude, no network beyond localhost."""
    state = dict(web._loop_reset())
    state.update({"running": True, "phase": "preparing", "message": "Preparing…",
                  "prepared": 1, "ready_ids": [READY["id"]]})
    monkeypatch.setattr(web, "_LOOP_STATE", state)
    monkeypatch.setattr(web.tracker, "get_application",
                        lambda aid: dict(READY) if aid == READY["id"] else None)
    # Opening a review stamps `reviewed_at` on the real tracker DB (decision 149). This test's
    # app id is fake, so let the stamp run against nothing — otherwise driving the UI here marks
    # whatever real application happens to share that id as reviewed, hiding it from discovery.
    monkeypatch.setattr(web, "_mark_reviewed", lambda aid: None)
    monkeypatch.setattr(web, "_review_data", lambda aid: {
        "id": aid,
        "posting": {k: READY[v] for k, v in
                    (("company", "company"), ("role", "role"), ("location", "location"),
                     ("remote", "remote"), ("pay", "pay"), ("portal", "portal"),
                     ("status", "status"), ("resume_source", "resume_source"))}
                   | {"url": READY["source_url"], "fit": READY["fit_score"]},
        "jd": "Build things.", "filled": [{"label": "Email", "value": "a@b.c"}],
        "skipped": [], "when": "just now", "has_resume": False, "has_screenshot": False})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def test_review_popup_survives_loop_status_polls(loop_ui):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # The first-visit tour covers the page with a click-blocking overlay; mark it seen so the
        # test drives the UI itself, not the tour.
        page.add_init_script("localStorage.setItem('ab-tour-done', '1')")
        page.goto(loop_ui)
        page.click('.tab[data-view="discover"]')
        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#review-panel .rv-h", timeout=10_000)
        # The popup is titled with the application it holds, and the card stays a slim row.
        assert page.inner_text("#review-modal-title") == "Acme — Backend Eng"
        assert page.query_selector("#loop-ready .pkcard .rv-h") is None, \
            "the review rendered inside the card instead of the popup"

        time.sleep(7)  # ~3 polls of /loop/status at 2s

        assert page.query_selector("#loop-ready .pkcard") is not None, \
            "the ready card itself was dropped by a status poll"
        assert "hidden" not in (page.get_attribute("#review-modal", "class") or ""), \
            "a status poll closed the open review popup"
        assert page.query_selector("#review-panel .rv-h"), \
            "a status poll wiped the review popup's contents"
        assert errors == []
        browser.close()


def test_review_popup_closes_with_escape_and_its_own_button(loop_ui):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # The first-visit tour covers the page with a click-blocking overlay; mark it seen so the
        # test drives the UI itself, not the tour.
        page.add_init_script("localStorage.setItem('ab-tour-done', '1')")
        page.goto(loop_ui)
        page.click('.tab[data-view="discover"]')

        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#review-panel .rv-h", timeout=10_000)
        page.keyboard.press("Escape")
        assert "hidden" in (page.get_attribute("#review-modal", "class") or ""), \
            "Escape did not close the review popup"

        # Reopening reloads it (a rescan or an edit may have changed what will be submitted).
        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#review-panel .rv-h", timeout=10_000)
        page.click("#review-panel .rv-collapse button")   # "Close review", at the bottom
        assert "hidden" in (page.get_attribute("#review-modal", "class") or ""), \
            "the panel's own Close review button did not close the popup"
        assert page.query_selector("#loop-ready .pkcard .review-toggle"), \
            "closing the review lost the card it was opened from"
        assert errors == []
        browser.close()

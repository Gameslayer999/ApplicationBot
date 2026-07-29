"""A Review panel opened while the auto-apply loop is running must stay open (this session's fix).

`/loop/status` is polled every 2s while the loop runs, and `renderLoop` used to rebuild the whole
"Ready to apply" list (`ready.innerHTML = ""`) on every tick — which threw away the expanded review
panel (and its in-flight `/track/review` fetch) about two seconds after the user opened it. Cards
are now keyed by application id and reused across polls. Verified by driving the real UI headless:
open Review, wait out three status polls, assert the panel is still expanded and populated.
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


def test_review_panel_survives_loop_status_polls(loop_ui):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(loop_ui)
        page.click('.tab[data-view="discover"]')
        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#loop-ready .review .rv-h", timeout=10_000)

        time.sleep(7)  # ~3 polls of /loop/status at 2s

        panel = page.query_selector("#loop-ready .review")
        assert panel is not None, "the ready card itself was dropped by a status poll"
        assert "hidden" not in (panel.get_attribute("class") or ""), \
            "a status poll collapsed the open review panel"
        assert page.query_selector("#loop-ready .review .rv-h"), \
            "a status poll wiped the review panel's contents"
        assert "▴" in page.inner_text("#loop-ready .review-toggle")
        assert errors == []
        browser.close()

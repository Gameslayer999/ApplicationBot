"""A check-all-that-apply screening question renders as CHECKBOXES in the Profile screen.

Drives the real UI headless: a banked question captured from a checkbox GROUP ("Language
Skill(s) (Check all that apply)") must offer every option as a checkbox — not a single-pick
dropdown, which could only ever store one answer — and saving must write the chosen options
back to the apply profile as one "; "-joined answer (what the fill splits to tick each box).
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from applicationbot import apply_profile, web
from applicationbot.apply_profile import QA, ApplicationProfile, load_profile, save_profile
from applicationbot.models import Contact, Resume

QUESTION = "Language Skill(s) (Check all that apply)"
OPTIONS = ["English (ENG)", "Spanish (SPA)", "French (FRA)", "German (DEU)"]
DROPDOWN_Q = "Highest level of education completed"


@pytest.fixture
def ui(monkeypatch, tmp_path):
    """The real UI on a free port, with the apply profile in a temp dir (never the user's own)."""
    prof = tmp_path / "application_profile.yaml"
    monkeypatch.setattr(apply_profile, "DEFAULT_PATH", str(prof))
    save_profile(ApplicationProfile(
        first_name="Jane", email="jane@example.com",
        custom_answers=[
            QA(question=QUESTION, answer="", seen_count=2, input_kind="checkbox", options=OPTIONS),
            QA(question=DROPDOWN_Q, answer="", seen_count=1, input_kind="dropdown",
               options=["Bachelor's Degree", "Master's Degree"]),
        ]), prof)
    monkeypatch.setattr(web, "load_resume",
                        lambda _p: Resume(contact=Contact(name="Jane Doe", email="jane@example.com")))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/", prof
    srv.shutdown()


def test_multi_answer_question_renders_as_checkboxes_and_saves_every_choice(ui):
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
        page.click('.tab[data-view="profile"]')
        page.wait_for_selector("#sec-qa .card", timeout=10_000)

        multi = page.query_selector("#sec-qa .qa-multi")
        assert multi, "a check-all-that-apply question must render its options as checkboxes"
        labels = [(o.inner_text() or "").strip() for o in multi.query_selector_all(".qa-opt")]
        assert labels == OPTIONS
        # The single-choice question is untouched — still a dropdown.
        assert len(page.query_selector_all("#sec-qa select[data-k=answer]")) == 1

        multi.query_selector_all('input[type="checkbox"]')[0].check()
        multi.query_selector_all('input[type="checkbox"]')[1].check()
        page.click("#save-profile")
        page.wait_for_selector("#profile-msg.ok", timeout=10_000)

        saved = {qa.question: qa for qa in load_profile(prof).custom_answers}
        assert saved[QUESTION].answer == "English (ENG); Spanish (SPA)"
        assert saved[QUESTION].input_kind == "checkbox"      # kind/options survive the round-trip
        assert saved[QUESTION].options == OPTIONS
        assert saved[DROPDOWN_Q].answer == ""
        assert errors == []
        browser.close()

"""Every reviewed question says whether the form REQUIRES it (decision 164).

Before this, the review panel listed answers and unanswered fields with no way to tell which ones
actually have to be filled to submit — so a user could either over-work optional EEO boxes or miss
the one required question blocking the submit. The fill now sweeps each form page for
{question: isRequired} and archives it; `_review_data` joins it onto every row; the panel badges
each row Required/Optional and counts them.

Covered here: the live sweep against the committed Lever fixture (a real mix of required-marked,
required-attribute and unmarked fields), the join in `_review_data` (including questions the sweep
never saw, which must stay UNMARKED rather than be called optional), and the panel's badges.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from applicationbot import apply_profile, archive, web
from applicationbot.apply import ApplyReport, _record_required
from applicationbot.apply_profile import ApplicationProfile, save_profile
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
LEVER = (REPO / "fixtures" / "apply_forms" / "lever_custom_cards.html").as_uri()

WHY = "Why are you interested in working at WHOOP?"
AUTH = "Are you legally authorized to work in the United States?"


# --------------------------------------------------------------- the live sweep (headless)

def test_record_required_reads_required_and_optional_off_a_real_form():
    from playwright.sync_api import sync_playwright

    report = ApplyReport(url=LEVER, ats="lever")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(LEVER)
        try:
            _record_required(page, report)
        finally:
            browser.close()

    req = report.required
    # Marked with Lever's "✱" glyph and/or the required attribute.
    assert req["Full name"] is True
    assert req[WHY] is True
    # A radio group is keyed by its CARD QUESTION, not by the "Yes"/"No" option labels — the same
    # key the fill reports the answer under, or the panel could never join the two.
    assert req[AUTH] is True
    assert "Yes" not in req and "No" not in req
    # Unmarked fields are recorded as explicitly optional, so the panel can say so.
    assert req["Current company"] is False
    assert req["Gender"] is False


# ------------------------------------------------------- the join into the review payload

READY = {"id": 11, "company": "Whoop", "role": "Backend Eng", "status": "dry-run", "portal": "lever",
         "fit_score": 91, "location": "Boston", "remote": "no", "pay": "$150k",
         "source_url": "http://example.invalid/11", "resume_source": "Tailored fresh",
         "resume_path": ""}
KEY = (READY["company"], READY["role"], READY["source_url"])

REPORT = {
    "when": "2026-07-30T09:00:00",
    "filled": [
        {"label": "Full name", "value": "Jane Doe", "source": "resolver", "control": "text"},
        {"label": "Current company", "value": "Acme", "source": "resolver", "control": "text"},
        {"label": "Gender", "value": "Decline to self-identify", "source": "resolver", "control": "select"},
    ],
    "skipped": [f"{WHY} — no saved answer",
                "Portfolio link — no saved answer",
                "[answer bank] 1 new question captured"],
    "captured": {WHY: {"kind": "text", "options": []}},
    "required": {"Full name": True, "Current company": False, "Gender": False, WHY: True},
}


@pytest.fixture
def ui(monkeypatch, tmp_path):
    """The real UI on a free port, serving one ready application whose archived fill report carries
    required marks. Archive, profile and résumé are temp/fake — never the user's own files."""
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


def test_review_data_marks_each_question_required_optional_or_unknown(ui):
    r = web._review_data(READY["id"])
    assert r["required_known"] is True
    filled = {f["label"]: f for f in r["filled"]}
    assert filled["Full name"]["required"] is True
    assert filled["Current company"]["required"] is False
    assert filled["Gender"]["required"] is False
    unanswered = {u["label"]: u for u in r["unanswered"]}
    assert unanswered[WHY]["required"] is True
    # A question the sweep never saw stays UNMARKED — never silently reported as optional.
    assert "required" not in unanswered["Portfolio link"]


def test_review_data_leaves_pre_164_reports_unmarked(ui, tmp_path):
    """An archive written before the sweep existed (and Workday's own driver, which doesn't run it)
    has no marks at all — the panel must know that and offer a rescan, not show every field bare."""
    adir = archive.dir_for(*KEY)
    stale = {k: v for k, v in REPORT.items() if k != "required"}
    (adir / "report.json").write_text(json.dumps(stale), encoding="utf-8")
    r = web._review_data(READY["id"])
    assert r["required_known"] is False
    assert all("required" not in f for f in r["filled"])


def test_review_panel_badges_required_and_optional_and_offers_a_rescan(ui):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(ui)
        page.evaluate("localStorage.setItem('ab-tour-done', '1')")  # the first-run tour overlays clicks
        page.reload()
        page.click('.tab[data-view="discover"]')
        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#loop-ready .review .rv-fields", timeout=10_000)

        rows = {}
        for tr in page.query_selector_all("#loop-ready .review .rv-fields tr"):
            label = tr.query_selector(".rv-fl div").inner_text().strip()
            rows[label] = [b.inner_text().strip() for b in tr.query_selector_all(".rv-req, .rv-opt")]
        assert rows["Full name"] == ["Required"]
        assert rows["Current company"] == ["Optional"]
        assert rows[WHY] == ["Required"]
        assert rows["Portfolio link"] == []           # the form never said — no badge, no guess
        # The counts, and the fact that an unanswered REQUIRED field is what blocks a submit.
        body = page.inner_text("#loop-ready .review")
        assert "2 required · 2 optional · 1 the form didn't mark" in body
        assert "1 required, which block a real submit" in body
        assert page.query_selector("#loop-ready .review .rv-rescan button").inner_text() \
            .strip() == "Rescan questions"
        assert errors == []
        browser.close()


def test_rescan_button_refreshes_the_panel_from_the_new_report(ui, monkeypatch):
    """Clicking "Rescan questions" re-reads the form and re-renders the panel with what the fill
    found (decision 164). The fill itself is stubbed — this pins the contract the panel relies on:
    the rescan is finished when the archived report carries a NEW timestamp, and the panel then
    shows the new questions, their required marks, and the outcome next to the button."""
    from playwright.sync_api import sync_playwright

    rescanned: list[int] = []

    def fake_rescan(app_id: int) -> dict:
        """Stand in for the headless dry-run: rewrite the archive the way a real re-fill would —
        a question that has since become required, and one that has gone away."""
        rescanned.append(app_id)
        fresh = dict(REPORT)
        fresh["when"] = "2026-07-30T18:30:00"
        fresh["filled"] = [
            {"label": "Full name", "value": "Jane Doe", "source": "resolver", "control": "text"},
            {"label": "Current company", "value": "Acme", "source": "resolver", "control": "text"},
        ]
        fresh["skipped"] = ["Start date — no saved answer"]
        fresh["required"] = {"Full name": True, "Current company": True, "Start date": True}
        (archive.dir_for(*KEY) / "report.json").write_text(json.dumps(fresh), encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(web, "queue_rescan", fake_rescan)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(ui)
        page.evaluate("localStorage.setItem('ab-tour-done', '1')")
        page.reload()
        page.click('.tab[data-view="discover"]')
        page.click("#loop-ready .pkcard .review-toggle")
        page.wait_for_selector("#loop-ready .review .rv-rescan button", timeout=10_000)
        page.click("#loop-ready .review .rv-rescan button")
        page.wait_for_selector("#loop-ready .review .rv-rescan .rv-note.rv-ok", timeout=20_000)

        assert rescanned == [READY["id"]]
        body = page.inner_text("#loop-ready .review")
        assert "Rescanned ✓ — 2 answer(s) ready, 1 unanswered." in body
        assert "Start date" in body and WHY not in body   # the panel shows the NEW question set
        assert "3 required · 0 optional" in body          # …and its refreshed required marks
        assert errors == []
        browser.close()

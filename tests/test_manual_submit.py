"""Manual-submit-during-dry-run tests (decision 097).

A dry-run leaves the filled form open for review. If the user clicks the site's own Submit
button while reviewing, that is a real application — it must be tracked as `applied` (method
`manual`), not left as a `dry-run` row.

Two halves are covered:
  * `_detect_manual_submit` — flips the report to submitted the moment a confirmation appears
    (driven against a LOCAL HTML fixture, never a real posting — Guideline #3; zero Claude calls).
  * `_record_run` — a re-record after the manual click upserts the pre-pause dry-run row to
    `applied`/`manual` and stamps date_applied (against a temp SQLite DB).

Run:  python -m tests.test_manual_submit   (also pytest-compatible; browser test needs chromium)
"""
from __future__ import annotations

import functools
from datetime import date
from pathlib import Path

from applicationbot import tracker
from applicationbot.apply import ApplyReport, _detect_manual_submit, _record_run

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "apply_forms"
CONFIRM = (FIXTURES / "submit_confirm.html").as_uri()


# ---- detection: a user-clicked submit surfaces its confirmation ----

def test_detect_manual_submit_flips_report_on_confirmation():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(CONFIRM)
        r = ApplyReport(url=CONFIRM)  # a dry-run report (submit_state defaults to "dry-run")
        # Nothing clicked yet: the form is still showing, no confirmation → no false positive.
        assert _detect_manual_submit(page, page.main_frame, r) is False
        assert r.submitted is False and r.manual_submit is False
        # The user clicks the site's own submit button themselves.
        page.click("button[type=submit], input[type=submit]")
        page.wait_for_timeout(300)
        assert _detect_manual_submit(page, page.main_frame, r) is True
        browser.close()
    assert r.submitted is True and r.manual_submit is True
    assert r.submit_state == "submitted"
    assert "thank you for applying" in r.confirmation.lower()


def test_detect_manual_submit_is_noop_once_submitted():
    # Idempotent: never re-stamps (or re-records) a report already marked submitted. No browser
    # needed — the early return fires before any page access, so None args are safe.
    r = ApplyReport(url="x", submitted=True, submit_state="submitted", confirmation="already")
    assert _detect_manual_submit(None, None, r) is False
    assert r.manual_submit is False and r.confirmation == "already"


# ---- recording: the manual click upgrades the dry-run row to a real application ----

def _bind_tracker(monkeypatch, db):
    """Point the DB-writing tracker calls `_record_run` makes at a temp DB (they take no path)."""
    for name in ("find_by_source_url", "add_application", "update_application", "record_run"):
        monkeypatch.setattr(tracker, name,
                            functools.partial(getattr(tracker, name), path=db))


def test_manual_submit_upgrades_dry_run_row_to_applied(tmp_path, monkeypatch):
    db = tmp_path / "applications.db"
    _bind_tracker(monkeypatch, db)
    meta = {"company": "Acme", "role": "SWE", "location": "NYC", "remote": "on-site",
            "pay": "$150k", "source_url": "https://boards.example.com/acme/swe"}

    # 1. The dry-run is recorded first (as run_apply does before the review pause).
    dry = ApplyReport(url=meta["source_url"], ats="greenhouse")
    app_id, action = _record_run(dry, "/tmp/resume.pdf", "SWE", "Acme", meta)
    assert action == "recorded"
    row = tracker.get_application(app_id, path=db)
    assert row["status"] == "dry-run" and row["method"] == "dry-run"
    assert row["date_applied"] == ""

    # 2. The user clicks Submit during review → same report, now manually submitted → re-record.
    applied = ApplyReport(url=meta["source_url"], ats="greenhouse",
                          submitted=True, manual_submit=True, submit_state="submitted",
                          confirmation="url: .../confirmation")
    same_id, action2 = _record_run(applied, "/tmp/resume.pdf", "SWE", "Acme", meta)
    assert same_id == app_id and action2 == "updated"  # upsert by source URL, no duplicate row

    row = tracker.get_application(app_id, path=db)
    assert row["status"] == "applied"          # no longer a dry-run
    assert row["method"] == "manual"           # a human clicked it, not the armed bot ("auto")
    assert row["date_applied"] == date.today().isoformat()  # stamped on the flip
    assert row["blocked_kind"] == ""           # a clean submit carries no parked block

    # The run log holds BOTH attempts against the posting (append-only audit trail).
    outcomes = [r["outcome"] for r in tracker.runs_for_application(app_id, path=db)]
    assert outcomes.count("applied") == 1 and outcomes.count("dry-run") == 1


def test_armed_bot_submit_records_method_auto_not_manual(tmp_path, monkeypatch):
    # Guard the distinction: an armed BOT submit (manual_submit False) stays method `auto`.
    db = tmp_path / "applications.db"
    _bind_tracker(monkeypatch, db)
    meta = {"company": "Beta", "role": "PM", "source_url": "https://boards.example.com/beta/pm"}
    r = ApplyReport(url=meta["source_url"], ats="lever",
                    submitted=True, manual_submit=False, submit_state="submitted")
    app_id, _ = _record_run(r, "/tmp/r.pdf", "PM", "Beta", meta)
    row = tracker.get_application(app_id, path=db)
    assert row["status"] == "applied" and row["method"] == "auto"


if __name__ == "__main__":
    import tempfile

    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        import inspect
        kw = {}
        params = inspect.signature(fn).parameters
        if "tmp_path" in params:
            kw["tmp_path"] = Path(tempfile.mkdtemp())
        if "monkeypatch" in params:
            kw["monkeypatch"] = _MP()
        fn(**kw)
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} manual-submit test(s) passed.")

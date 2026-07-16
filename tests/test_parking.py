"""Park & resume blocked applications (AutoApply-AI survey #1).

Covers the pure classifier (applicationbot.parking) and the tracker's parked-application
store: schema migration of a pre-existing DB, and parked_applications() surfacing only the
still-open, resolvable rows. No browser, no Claude, no network.

Run:  python -m pytest tests/test_parking.py -q
"""
from __future__ import annotations

import sqlite3

import pytest

from applicationbot import parking, tracker
from applicationbot.apply import ApplyReport
from applicationbot.parking import CAPTCHA, FORM_REJECTED, LOGIN, NEEDS_ANSWER, SITE_ERROR


# --------------------------------------------------------------------- classify()

def test_clean_dry_run_is_not_parked():
    r = ApplyReport(url="x", submit_state="dry-run", skipped=["Phone — no saved answer"])
    assert parking.classify(r) is None  # optional unanswered fields don't park


def test_required_unanswered_in_dry_run_parks_as_needs_answer():
    r = ApplyReport(url="x", submit_state="dry-run", skipped=[
        "Work authorization — REQUIRED, not filled (no matching answer or unsupported field)",
        "Sponsorship — REQUIRED, not filled (no matching answer or unsupported field)",
        "Phone — no saved answer",  # optional, ignored
    ])
    reason = parking.classify(r)
    assert reason is not None
    assert reason.kind == NEEDS_ANSWER
    assert reason.resolve == "profile-answers"
    assert reason.resumable
    assert reason.detail == "Work authorization; Sponsorship"
    assert "2 required question(s)" in reason.summary


def test_armed_pre_submit_gate_blockers_park_as_needs_answer():
    r = ApplyReport(url="x", submit_state="blocked",
                    blockers=["unresolved required field(s): GPA; Start date"])
    reason = parking.classify(r)
    assert reason.kind == NEEDS_ANSWER
    assert reason.detail == "GPA; Start date"


def test_required_missing_dedupes_across_blockers_and_skipped():
    r = ApplyReport(url="x", submit_state="blocked",
                    blockers=["unresolved required field(s): GPA"],
                    skipped=["GPA — REQUIRED, not filled (no matching answer or unsupported field)"])
    assert parking.required_missing(r) == ["GPA"]


def test_captcha_wins_over_missing_fields():
    r = ApplyReport(url="x", submit_state="blocked",
                    blockers=["unresolved required field(s): GPA"],
                    errors=["hCaptcha challenge detected on the page"])
    reason = parking.classify(r)
    assert reason.kind == CAPTCHA
    assert reason.resolve == ""
    assert reason.resumable


def test_login_wall_parks_as_login():
    r = ApplyReport(url="x", submit_state="blocked",
                    blockers=["you must sign in to continue this application"])
    reason = parking.classify(r)
    assert reason.kind == LOGIN
    assert reason.resolve == "credentials"


def test_form_rejected_parks_for_answer_review():
    r = ApplyReport(url="x", submit_state="blocked",
                    blockers=["form rejected the submit: Email is invalid; Phone required"])
    reason = parking.classify(r)
    assert reason.kind == FORM_REJECTED
    assert reason.resolve == "profile-answers"


def test_no_submit_button_is_site_error_not_resumable():
    r = ApplyReport(url="x", submit_state="blocked",
                    blockers=["no submit button found on the form"])
    reason = parking.classify(r)
    assert reason.kind == SITE_ERROR
    assert not reason.resumable


# --------------------------------------------------------------------- tracker store

def test_parked_columns_migrate_onto_a_pre_existing_db(tmp_path):
    """A DB created before this feature (no blocked_kind/blocked_detail) gets the columns
    added by _connect, and existing rows keep their data."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "company TEXT, status TEXT, source_url TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO applications (company, status, source_url, created_at, updated_at) "
                 "VALUES ('Old', 'dry-run', 'u', 'now', 'now')")
    conn.commit()
    conn.close()

    rows = tracker.list_applications(path=db)
    assert len(rows) == 1
    assert rows[0]["blocked_kind"] == "" and rows[0]["blocked_detail"] == ""


def test_parked_applications_surfaces_only_open_resolvable_rows(tmp_path):
    db = tmp_path / "apps.db"
    open_id = tracker.add_application(
        {"company": "Acme", "status": "blocked", "source_url": "a",
         "blocked_kind": NEEDS_ANSWER, "blocked_detail": "GPA"}, path=db)
    # Applied (resolved) — same kind text but no longer open, must not surface.
    tracker.add_application(
        {"company": "Beta", "status": "applied", "source_url": "b",
         "blocked_kind": NEEDS_ANSWER, "blocked_detail": "GPA"}, path=db)
    # Clean dry-run with no block — not parked.
    tracker.add_application({"company": "Gamma", "status": "dry-run", "source_url": "c"}, path=db)

    parked = tracker.parked_applications(path=db)
    assert [r["id"] for r in parked] == [open_id]
    assert parked[0]["blocked_kind"] == NEEDS_ANSWER


def test_blocked_is_a_valid_status(tmp_path):
    db = tmp_path / "apps.db"
    app_id = tracker.add_application({"company": "Acme", "status": "blocked"}, path=db)
    assert tracker.get_application(app_id, path=db)["status"] == "blocked"
    assert tracker.status_counts(path=db)["blocked"] == 1


# --------------------------------------------------------------------- describe() (UI)

def test_describe_needs_answer_deep_links_to_profile():
    d = parking.describe(NEEDS_ANSWER, "GPA; Start date")
    assert d["resolve"] == "profile-answers" and d["resumable"] and d["detail"] == "GPA; Start date"
    assert d["label"] and d["action"]


def test_describe_login_deep_links_to_credentials():
    assert parking.describe(LOGIN)["resolve"] == "credentials"


def test_describe_site_error_is_not_resumable_and_has_no_target():
    d = parking.describe(SITE_ERROR)
    assert not d["resumable"] and d["resolve"] == ""


def test_describe_unknown_kind_is_safe():
    d = parking.describe("", "")
    assert d["resolve"] == "" and not d["resumable"]


# --------------------------------------------------------------------- runner surfacing

def test_runner_report_parked_names_blocked_applications(tmp_path, monkeypatch):
    import functools

    from applicationbot import runner
    db = tmp_path / "apps.db"
    tracker.add_application({"company": "Acme", "role": "SWE", "status": "blocked",
                             "blocked_kind": NEEDS_ANSWER, "blocked_detail": "Work authorization"}, path=db)
    # _report_parked calls tracker.parked_applications() with no path (production hits the
    # real DB) — bind it to the temp DB for the test.
    monkeypatch.setattr(tracker, "parked_applications",
                        functools.partial(tracker.parked_applications, path=db))
    lines: list[str] = []
    runner._report_parked(say=lines.append)
    blob = "\n".join(lines)
    assert "1 application(s) parked" in blob
    assert "Acme — SWE" in blob and "Work authorization" in blob
    assert "Answer the questions" in blob, "each line must carry its own action verb"


def test_runner_never_tells_the_user_to_resolve_a_bot_wall(tmp_path, monkeypatch):
    """A bot-walled application is waiting on the SITE, not on the user. The old header told every
    parked row it was "waiting on you — resolve", which sends the user hunting for a fix that does
    not exist for this kind (UI Principle #4)."""
    import functools

    from applicationbot import runner
    db = tmp_path / "apps.db"
    tracker.add_application({"company": "Consultadd", "role": "Python Developer", "status": "blocked",
                             "blocked_kind": parking.BOT_WALL,
                             "blocked_detail": "blocked by captcha-delivery.com"}, path=db)
    monkeypatch.setattr(tracker, "parked_applications",
                        functools.partial(tracker.parked_applications, path=db))
    lines: list[str] = []
    runner._report_parked(say=lines.append)
    blob = "\n".join(lines)
    assert "waiting on you" not in blob and "resolve" not in blob.lower()
    assert "The site blocked automated access" in blob and "Try again" in blob


def test_runner_report_parked_silent_when_none(tmp_path, monkeypatch):
    import functools

    from applicationbot import runner
    monkeypatch.setattr(tracker, "parked_applications",
                        functools.partial(tracker.parked_applications, path=tmp_path / "empty.db"))
    lines: list[str] = []
    runner._report_parked(say=lines.append)
    assert lines == []


# --------------------------------------------------------------------- re-apply (resume) guards

@pytest.fixture
def web_temp_db(tmp_path, monkeypatch):
    """Point the web module's tracker reads at a throwaway DB and reset the shared run state.
    Guard paths return before any browser launch, so no Playwright is needed."""
    import functools

    from applicationbot import web
    db = tmp_path / "apps.db"
    monkeypatch.setattr(tracker, "get_application",
                        functools.partial(tracker.get_application, path=db))
    web._TEST_STATE.clear()
    web._TEST_STATE.update({"phase": "idle", "errors": []})
    return web, db


def test_reapply_missing_row_errors(web_temp_db):
    web, _ = web_temp_db
    web._reapply_worker(9999)
    assert web._TEST_STATE["phase"] == "error"
    assert "no longer in the tracker" in web._TEST_STATE["errors"][0]


def test_reapply_without_source_url_errors(web_temp_db):
    web, db = web_temp_db
    app_id = tracker.add_application(
        {"company": "Acme", "status": "blocked", "blocked_kind": NEEDS_ANSWER}, path=db)
    web._reapply_worker(app_id)
    assert web._TEST_STATE["phase"] == "error"
    assert "no source URL" in web._TEST_STATE["errors"][0]


def test_reapply_missing_pdf_errors(web_temp_db):
    web, db = web_temp_db
    app_id = tracker.add_application(
        {"company": "Acme", "status": "blocked", "source_url": "https://x/1",
         "resume_path": "/does/not/exist.pdf", "blocked_kind": NEEDS_ANSWER}, path=db)
    web._reapply_worker(app_id)
    assert web._TEST_STATE["phase"] == "error"
    assert "résumé PDF" in web._TEST_STATE["errors"][0]


def test_start_reapply_refuses_while_a_run_is_active(web_temp_db):
    web, _ = web_temp_db
    web._TEST_STATE["phase"] = "running"
    r = web.start_reapply(1)
    assert r["ok"] is False and "already in progress" in r["error"]


# ------------------------------------------------- per-click armed submit (decision 058)

def test_reapply_gate_is_armed_one_shot_only_when_arm():
    from applicationbot import web

    assert web._reapply_gate(False) is None       # dry-run: no gate → run_apply never submits
    g = web._reapply_gate(True)
    assert g.armed is True and g.max_submissions_per_run == 1  # armed for exactly one submission
    # The global KILL file still gates it — an armed gate is not a bypass of the kill switch.
    from applicationbot.safety import DEFAULT_KILL
    assert g.kill_file == DEFAULT_KILL


def test_same_origin_guard():
    from applicationbot import web

    class _H:
        def __init__(self, origin=None, referer=None, host=None):
            self.headers = {}
            if origin is not None:
                self.headers["Origin"] = origin
            if referer is not None:
                self.headers["Referer"] = referer
            if host is not None:
                self.headers["Host"] = host
    assert web._same_origin(_H()) is True                                   # no Origin → same-origin fetch
    assert web._same_origin(_H(origin="http://127.0.0.1:8000")) is True
    assert web._same_origin(_H(origin="http://localhost:8000")) is True
    assert web._same_origin(_H(origin="https://evil.example.com")) is False  # drive-by → blocked
    assert web._same_origin(_H(referer="http://attacker.test/x")) is False
    # Non-loopback bind (--host LAN IP): a matching Origin/Host is same-origin; a mismatch isn't.
    assert web._same_origin(_H(origin="http://192.168.1.5:8000", host="192.168.1.5:8000")) is True
    assert web._same_origin(_H(origin="https://evil.example.com", host="192.168.1.5:8000")) is False


def test_reapply_route_blocks_cross_origin(web_temp_db, monkeypatch):
    """The do_POST origin guard (decision 062) refuses ANY cross-origin POST before starting a
    run — armed or dry-run — while same-origin passes."""
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    web, _ = web_temp_db
    started: list = []
    monkeypatch.setattr(web, "start_reapply", lambda app_id, arm=False: started.append(arm) or {"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def post(body, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/parked/reapply",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **headers},
                                     method="POST")
        try:
            resp = urllib.request.urlopen(req)
            return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        code, _ = post({"id": 1, "arm": True}, {"Origin": "https://evil.example.com"})
        assert code == 403 and started == []                    # armed + cross-origin → blocked, no run
        code, _ = post({"id": 1, "arm": False}, {"Origin": "https://evil.example.com"})
        assert code == 403 and started == []                    # dry-run + cross-origin → also blocked
        code, _ = post({"id": 1, "arm": True}, {})               # same-origin (no Origin) → allowed
        assert code == 200 and started == [True]
    finally:
        srv.shutdown()


# --------------------------------------------------- bot walls (decision 077)

def test_bot_wall_parks_as_bot_wall_not_as_a_solvable_captcha():
    """The bug this exists for: the wall's own vendor host is "captcha-delivery.com", so the
    `"captcha" in text` scan classified a DataDome IP block as a CAPTCHA and told the user to
    "solve it in the open browser" — there is no puzzle, and a headless run has no browser to
    solve it in. The structured flag must win over the error prose."""
    r = ApplyReport(url="x", ats="smartrecruiters", bot_wall="captcha-delivery.com", errors=[
        "smartrecruiters blocked automated access to this posting "
        "(page says: 'captcha-delivery.com') — the form was never served."])
    reason = parking.classify(r)
    assert reason is not None
    assert reason.kind == parking.BOT_WALL, f"mis-parked as {reason.kind}"
    assert reason.kind != CAPTCHA
    assert "captcha-delivery.com" in reason.detail
    assert reason.resumable is True, "the site may let us in later — it must be retryable"
    assert reason.resolve == "", "there is no setting the user can change to fix a bot wall"


def test_bot_wall_card_says_try_again_and_deep_links_nowhere():
    d = parking.describe(parking.BOT_WALL, "blocked by captcha-delivery.com")
    assert d["label"] == "The site blocked automated access"
    assert d["action"] == "Try again"
    assert d["resolve"] == "" and d["resumable"] is True


def test_a_real_captcha_still_parks_as_captcha():
    """Guard the fix's blast radius: a genuine CAPTCHA (no bot_wall flag) is unchanged."""
    r = ApplyReport(url="x", blockers=["a captcha stands in the way of submit"])
    assert parking.classify(r).kind == CAPTCHA


def test_bot_walled_run_is_recorded_blocked_never_ready_to_apply(monkeypatch):
    """The second half of the bug: a bot-walled run never reaches submit, so `submit_state` stays
    "dry-run" — and web.py advertises ANY dry-run row as "ready to apply". A posting we were
    REFUSED on must never appear ready; it must be a parked `blocked` row we can retry later."""
    from applicationbot import apply as apply_mod

    captured: dict = {}
    monkeypatch.setattr(tracker, "find_by_source_url", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "add_application", lambda data, **k: captured.update(data) or 1)

    report = ApplyReport(url="https://jobs.smartrecruiters.com/Co/1", ats="smartrecruiters",
                         bot_wall="captcha-delivery.com")
    assert report.submit_state == "dry-run"  # precondition: it never reached the submit path
    apply_mod._record_run(report, "r.pdf", "", "")

    assert captured["status"] == "blocked", "a refused posting must not sit in the ready queue"
    assert captured["blocked_kind"] == parking.BOT_WALL
    assert "Refused" in captured["notes"] and "captcha-delivery.com" in captured["notes"]
    assert "Dry-run" not in captured["notes"], "it was not a dry-run of the form — none was served"


def test_bot_walled_row_surfaces_as_parked_for_later(tmp_path):
    """End of the chain: the flagged row is what `parked_applications` returns, so the runner
    lists it and the UI offers it — "go back and do them later" needs no new plumbing."""
    db = tmp_path / "t.db"
    tracker.add_application({"company": "Consultadd", "role": "Python Developer",
                             "source_url": "https://jobs.smartrecruiters.com/Co/1",
                             "status": "blocked", "blocked_kind": parking.BOT_WALL,
                             "blocked_detail": "blocked by captcha-delivery.com"}, path=db)
    parked = tracker.parked_applications(path=db)
    assert len(parked) == 1 and parked[0]["blocked_kind"] == parking.BOT_WALL
    assert parking.describe(parked[0]["blocked_kind"])["action"] == "Try again"

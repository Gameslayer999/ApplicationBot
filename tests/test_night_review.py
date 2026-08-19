"""Tests for the night's self-review (decision 185) — no tracker DB, no Claude: the tracker
lookup, the earlier nights, and the calibration report are all injected."""

import json

from applicationbot.night_review import (
    Finding,
    build_findings,
    calibration_finding,
    list_sessions,
    review_session,
    taxonomy,
    trend,
    verify_submissions,
)


def _app(result, company="Acme", url="https://x/1", detail="", role="Dev"):
    return {"event": "application", "result": result, "company": company, "role": role,
            "url": url, "fit": 80, "detail": detail}


def _tracker(rows):
    """rows: {url: tracker-row-dict}"""
    return lambda url: rows.get(url)


def _session(tmp_path, apps, summary=None):
    d = tmp_path / "2026-08-18-2300"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(json.dumps(a) + "\n" for a in apps))
    (d / "summary.json").write_text(json.dumps(summary or {"submitted": 1, "goal": 10,
                                                           "stop_reason": "deadline"}))
    return d


# ------------------------------------------------------------------ 1. were the submissions real?

def test_a_submission_backed_by_a_tracker_row_is_confirmed():
    apps = [_app("submitted", url="u1")]
    v = verify_submissions(apps, _tracker({"u1": {"status": "applied", "date_applied": "2026-08-18"}}))
    assert (v["claimed"], v["confirmed"], v["trustworthy"]) == (1, 1, True)


def test_a_claimed_submission_with_no_tracker_row_is_reported_not_silently_counted():
    v = verify_submissions([_app("submitted", url="u1")], _tracker({}))
    assert v["confirmed"] == 0 and not v["trustworthy"]
    assert v["not_recorded"][0]["detail"] == "no tracker row"


def test_a_tracker_row_that_disagrees_with_the_claim_is_flagged():
    v = verify_submissions([_app("submitted", url="u1")],
                           _tracker({"u1": {"status": "dry-run", "date_applied": ""}}))
    assert not v["trustworthy"] and "dry-run" in v["wrong_status"][0]["detail"]


def test_a_submitted_row_with_no_date_applied_is_flagged():
    v = verify_submissions([_app("submitted", url="u1")],
                           _tracker({"u1": {"status": "applied", "date_applied": ""}}))
    assert "date_applied" in v["wrong_status"][0]["detail"]


def test_an_unconfirmed_submit_is_called_a_possible_false_success():
    v = verify_submissions([_app("unconfirmed", url="u1", detail="no success page")], _tracker({}))
    assert v["unconfirmed"] and not v["trustworthy"]
    assert v["claimed"] == 0        # it was never counted as a submission in the first place


def test_dry_runs_and_blocks_are_not_treated_as_submission_claims():
    v = verify_submissions([_app("dry-run"), _app("blocked", detail="login: expired")], _tracker({}))
    assert (v["claimed"], v["trustworthy"]) == (0, True)


# ------------------------------------------------------------------ 2. failure taxonomy + trend

def test_taxonomy_groups_by_failure_kind_and_by_portal():
    apps = [_app("blocked", url="u1", detail="login: session expired"),
            _app("blocked", url="u2", detail="login: session expired"),
            _app("failed", url="u3", detail="TimeoutError: form never loaded"),
            _app("submitted", url="u4")]
    rows = {"u1": {"portal": "workday"}, "u2": {"portal": "workday"},
            "u3": {"portal": "greenhouse"}, "u4": {"portal": "greenhouse"}}
    t = taxonomy(apps, _tracker(rows))
    assert t["kinds"] == {"blocked:login": 2, "failed:TimeoutError": 1}
    assert t["portals"]["workday"] == {"attempts": 2, "failures": 2}
    assert t["portals"]["greenhouse"] == {"attempts": 2, "failures": 1}


def test_taxonomy_keeps_a_few_named_examples_per_kind():
    apps = [_app("blocked", company=f"C{i}", url=f"u{i}", detail="login: expired") for i in range(5)]
    t = taxonomy(apps, _tracker({}))
    assert t["examples"]["blocked:login"] == ["C0 — Dev", "C1 — Dev", "C2 — Dev"]


def test_trend_calls_out_a_failure_that_is_new_tonight():
    t = trend({"blocked:login": 4}, [{"failure_kinds": {"failed:Timeout": 1}},
                                     {"failure_kinds": {}}])
    assert t["blocked:login"]["verdict"] == "NEW tonight"
    assert t["failed:Timeout"]["verdict"] == "better"     # it happened before, not tonight


def test_trend_compares_against_the_average_of_earlier_nights():
    t = trend({"blocked:login": 2}, [{"failure_kinds": {"blocked:login": 6}},
                                     {"failure_kinds": {"blocked:login": 4}}])
    assert t["blocked:login"] == {"tonight": 2, "before": 5.0, "verdict": "better"}


def test_trend_says_no_history_on_the_first_night():
    assert trend({"blocked:login": 1}, [])["blocked:login"]["verdict"] == "no history"


# ------------------------------------------------------------------ 3. calibration

def test_calibration_finding_carries_the_recommendation_and_where_to_change_it():
    f = calibration_finding({"bands": [{"positive": 3, "negative": 4}]}, 70, (75, "replies cluster ≥75"))
    assert f.title == "Move min_fit 70 → 75" and "discovery.yaml" in f.fix


def test_calibration_stays_quiet_until_there_is_enough_resolved_history():
    assert calibration_finding({"bands": [{"positive": 1, "negative": 1}]}, 70, None) is None
    assert calibration_finding({}, 70, None) is None


def test_calibration_says_the_bar_looks_right_once_there_is_history():
    f = calibration_finding({"bands": [{"positive": 3, "negative": 4}]}, 70, None)
    assert f is not None and "looks right" in f.title
    assert f.impact == 0 and "7 resolved" in f.evidence   # nothing to do ⇒ it never ranks as work


# ------------------------------------------------------------------ the ranked backlog

def test_a_wrong_submission_count_outranks_a_bigger_pile_of_known_failures():
    verification = {"claimed": 1, "confirmed": 0, "unconfirmed": [],
                    "not_recorded": [{"label": "Acme — Dev", "url": "u1", "detail": "no tracker row"}],
                    "wrong_status": [], "trustworthy": False}
    tax = {"kinds": {"blocked:login": 9}, "examples": {"blocked:login": ["A"]}, "portals": {}}
    findings = build_findings(verification, tax, {}, None)
    assert findings[0].id == "submissions-not-recorded"
    assert findings[0].impact == 1              # the number a human reads is the real cost
    assert findings[1].id == "failure:blocked:login"


def test_each_failure_finding_names_where_to_fix_it():
    tax = {"kinds": {"blocked:needs_answer": 3}, "examples": {"blocked:needs_answer": ["A"]},
           "portals": {}}
    f = build_findings({"claimed": 0, "confirmed": 0, "unconfirmed": [], "not_recorded": [],
                        "wrong_status": [], "trustworthy": True}, tax, {}, None)[0]
    assert "answer_bank" in f.fix


def test_an_unknown_failure_kind_still_gets_an_actionable_repro_command():
    tax = {"kinds": {"failed:SomethingNew": 1}, "examples": {}, "portals": {}}
    f = build_findings({"claimed": 0, "confirmed": 0, "unconfirmed": [], "not_recorded": [],
                        "wrong_status": [], "trustworthy": True}, tax, {}, None)[0]
    assert "python -m applicationbot.apply" in f.fix


def test_a_completely_broken_portal_is_its_own_finding():
    tax = {"kinds": {"failed:X": 4}, "examples": {},
           "portals": {"workday": {"attempts": 4, "failures": 4},
                       "lever": {"attempts": 5, "failures": 1}}}
    findings = build_findings({"claimed": 0, "confirmed": 0, "unconfirmed": [], "not_recorded": [],
                               "wrong_status": [], "trustworthy": True}, tax, {}, None)
    ids = [f.id for f in findings]
    assert "portal:workday" in ids and "portal:lever" not in ids
    assert findings[0].id == "portal:workday"      # outranks the scattered failures


def test_a_clean_night_produces_no_findings():
    assert build_findings({"claimed": 3, "confirmed": 3, "unconfirmed": [], "not_recorded": [],
                           "wrong_status": [], "trustworthy": True},
                          {"kinds": {}, "examples": {}, "portals": {}}, {}, None) == []


# ------------------------------------------------------------------ end to end over a session dir

def test_review_session_reads_the_journal_and_writes_a_readable_verdict(tmp_path):
    session = _session(tmp_path, [
        _app("submitted", company="Acme", url="u1"),
        _app("submitted", company="Globex", url="u2"),
        _app("blocked", company="Initech", url="u3", detail="login: account required"),
    ], summary={"submitted": 2, "goal": 5, "stop_reason": "deadline", "stop_detail": "07:00",
                "started_at": "2026-08-18T23:00:00"})
    rows = {"u1": {"status": "applied", "date_applied": "2026-08-18", "portal": "greenhouse"},
            "u3": {"portal": "workday"}}
    out = review_session(session, lookup=_tracker(rows),
                         previous=[{"failure_kinds": {"blocked:login": 0}}])

    assert out["applications_reviewed"] == 3
    assert out["verification"]["claimed"] == 2 and out["verification"]["confirmed"] == 1
    assert not out["verification"]["trustworthy"]          # Globex was never recorded
    assert out["trend"]["blocked:login"]["verdict"] == "NEW tonight"
    md = out["review_md"]
    assert "the reported number overstates what landed" in md
    assert "Globex" in md and "blocked:login" in md
    assert out["findings"][0]["id"] == "submissions-not-recorded"


def test_review_of_a_clean_night_says_so(tmp_path):
    session = _session(tmp_path, [_app("submitted", url="u1")])
    out = review_session(session, lookup=_tracker(
        {"u1": {"status": "applied", "date_applied": "2026-08-18", "portal": "lever"}}))
    assert out["verification"]["trustworthy"]
    assert "Nothing failed tonight." in out["review_md"]
    assert "Nothing to fix" in out["review_md"]


def test_list_sessions_ignores_a_crashed_session_with_no_summary(tmp_path):
    (tmp_path / "2026-08-17-2300").mkdir()                       # crashed: no summary.json
    done = tmp_path / "2026-08-18-2300"
    done.mkdir()
    (done / "summary.json").write_text("{}")
    assert list_sessions(tmp_path) == [done]


def test_sessions_are_newest_first(tmp_path):
    for name in ("2026-08-16-2300", "2026-08-18-2300", "2026-08-17-2300"):
        d = tmp_path / name
        d.mkdir()
        (d / "summary.json").write_text("{}")
    assert [p.name for p in list_sessions(tmp_path)] == [
        "2026-08-18-2300", "2026-08-17-2300", "2026-08-16-2300"]


def test_a_night_that_submitted_nothing_is_never_reported_as_all_confirmed(tmp_path):
    """'all 0 claimed submissions confirmed' would read as a clean night. It sent nothing."""
    session = _session(tmp_path, [_app("dry-run", url="u1"), _app("dry-run", url="u2")],
                       summary={"submitted": 0, "goal": 5, "stop_reason": "deadline"})
    out = review_session(session, lookup=_tracker({}))
    assert "**Nothing was submitted.** 2 application(s) were attempted" in out["review_md"]
    assert out["verification"]["trustworthy"]      # nothing was claimed, so nothing is in doubt

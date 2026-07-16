"""Autonomous-runner loop tests (decision 035) — injected apply_one, no browser, no tokens.

Run:  python -m tests.test_runner   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot.apply import ApplyReport
from applicationbot.discovery import Posting
from applicationbot.matching import Match
from applicationbot.runner import cleared_queue, run_queue
from applicationbot.safety import SafetyGate


def _match(fit, company="Acme") -> Match:
    p = Posting(company=company, title="SWE", body="jd", url=f"https://x/{company}/{fit}",
                ats="greenhouse")
    return Match(posting=p, keyword_score=3, matched_skills=[], fit_score=fit,
                 qualified=True, judged_by="claude")


def _gate(**kw) -> SafetyGate:
    return SafetyGate(kill_file=Path(tempfile.mkdtemp()) / "KILL", **kw)


def _report(**kw) -> ApplyReport:
    r = ApplyReport(url="x")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _quiet(msg):
    pass


def test_cleared_queue_requires_claude_judgment():
    judged = _match(80)
    unjudged = Match(posting=judged.posting, keyword_score=99, matched_skills=[])  # fit None
    low = _match(30)
    assert cleared_queue([judged, unjudged, low], min_fit=50) == [judged]
    # Claude entirely absent → empty queue, never keyword-blind auto-apply
    assert cleared_queue([unjudged], min_fit=50) == []


def test_runs_whole_queue_and_classifies_outcomes():
    queue = [_match(90, "A"), _match(80, "B"), _match(70, "C")]
    reports = {
        "A": _report(submitted=True, submit_state="submitted", confirmation="page text: 'Thank you'"),
        "B": _report(submit_state="blocked", blockers=["unresolved required field(s): GPA"]),
        "C": _report(),  # plain dry-run
    }
    res = run_queue(queue, lambda m: reports[m.posting.company], _gate(armed=True), say=_quiet)
    assert [o.result for o in res.outcomes] == ["submitted", "blocked", "dry-run"]
    assert res.stopped_reason == "queue exhausted"


def test_kill_switch_stops_between_applications():
    gate = _gate(armed=True)

    def apply_one(m):
        gate.kill_file.write_text("stop")  # user hits STOP during the first application
        return _report()

    res = run_queue([_match(90, "A"), _match(80, "B")], apply_one, gate, say=_quiet)
    assert len(res.outcomes) == 1 and "kill switch" in res.stopped_reason


def test_submission_cap_stops_armed_queue():
    gate = _gate(armed=True, max_submissions_per_run=1)

    def apply_one(m):
        gate.record_submission()
        return _report(submitted=True, submit_state="submitted", confirmation="ok")

    res = run_queue([_match(90, "A"), _match(80, "B"), _match(70, "C")], apply_one, gate, say=_quiet)
    assert len(res.outcomes) == 1 and "cap" in res.stopped_reason


def test_max_applications_bounds_the_run():
    res = run_queue([_match(90, "A"), _match(80, "B"), _match(70, "C")],
                    lambda m: _report(), _gate(), max_applications=2, say=_quiet)
    assert len(res.outcomes) == 2 and "limit" in res.stopped_reason


def test_failure_is_isolated_but_claude_failure_stops():
    def apply_one(m):
        if m.posting.company == "A":
            raise ValueError("selector timeout")  # one bad form doesn't kill the queue
        if m.posting.company == "B":
            raise RuntimeError("Claude Code failed (exit 1)")  # dead CLI stops the queue
        return _report()

    res = run_queue([_match(90, "A"), _match(80, "B"), _match(70, "C")],
                    lambda m: apply_one(m), _gate(), say=_quiet)
    assert [o.result for o in res.outcomes] == ["failed", "failed"]
    assert "Claude" in res.stopped_reason  # C never ran


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} runner test(s) passed.")

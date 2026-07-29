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


class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, note):
        self.sent.append(note)


def test_notify_cycle_summarizes_ready_and_blocked():
    from applicationbot import notifications as notif
    from applicationbot.runner import Outcome, RunnerResult, _notify_cycle

    n = _FakeNotifier()
    res = RunnerResult(outcomes=[
        Outcome("Stripe", "SWE New Grad", "u1", 82, "dry-run", "12 filled"),
        Outcome("Ramp", "SWE Intern", "u2", 79, "dry-run", "10 filled"),
        Outcome("Notion", "SWE", "u3", 80, "blocked", "needs answer"),
        Outcome("Acme", "SWE", "u4", 85, "submitted", "confirmed"),  # not a 'ready' moment
    ])
    _notify_cycle(n, res)
    events = [x.event for x in n.sent]
    assert events.count(notif.APPROVAL_NEEDED) == 1  # ONE summary, not one per match
    assert events.count(notif.INTERVENTION_NEEDED) == 1
    approval = next(x for x in n.sent if x.event == notif.APPROVAL_NEEDED)
    assert approval.title == "2 ready to apply"
    assert "Stripe" in approval.body and "Ramp" in approval.body
    interv = next(x for x in n.sent if x.event == notif.INTERVENTION_NEEDED)
    assert interv.urgent and "Notion" in interv.body


def test_notify_cycle_silent_when_nothing_ready():
    from applicationbot.runner import Outcome, RunnerResult, _notify_cycle

    n = _FakeNotifier()
    _notify_cycle(n, RunnerResult(outcomes=[
        Outcome("Acme", "SWE", "u", 90, "submitted", "ok"),
        Outcome("B", "SWE", "u", 50, "failed", "boom"),
    ]))
    assert n.sent == []


def test_notify_cycle_swallows_notifier_errors():
    from applicationbot.runner import Outcome, RunnerResult, _notify_cycle

    class Boom:
        def notify(self, note):
            raise RuntimeError("channel down")

    # A broken notifier must never break the watch loop.
    _notify_cycle(Boom(), RunnerResult(outcomes=[
        Outcome("Stripe", "SWE", "u", 82, "dry-run", "filled")]))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} runner test(s) passed.")

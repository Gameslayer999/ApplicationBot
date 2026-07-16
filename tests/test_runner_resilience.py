"""Runner resilience to Claude usage caps / auth failures — injected apply_one and
_sleep, no browser, no CLI, no waiting.

Run:  python -m tests.test_runner_resilience   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot.apply import ApplyReport
from applicationbot.backends import (
    ClaudeAuthError,
    ClaudeRateLimitError,
    ClaudeUnavailableError,
)
from applicationbot.discovery import Posting
from applicationbot.matching import Match
from applicationbot.runner import run_queue
from applicationbot.safety import SafetyGate


def _match(fit, company="Acme") -> Match:
    p = Posting(company=company, title="SWE", body="jd", url=f"https://x/{company}/{fit}",
                ats="greenhouse")
    return Match(posting=p, keyword_score=3, matched_skills=[], fit_score=fit,
                 qualified=True, judged_by="claude")


def _gate(**kw) -> SafetyGate:
    return SafetyGate(kill_file=Path(tempfile.mkdtemp()) / "KILL", **kw)


def _quiet(msg):
    pass


class _FakeSleep:
    """Records sleep calls instead of sleeping; optional hook fires per call."""

    def __init__(self, on_call=None):
        self.calls: list[float] = []
        self.on_call = on_call

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.on_call:
            self.on_call(len(self.calls))


def test_rate_limit_waits_then_retries_same_match_once():
    attempts: list[str] = []

    def apply_one(m):
        attempts.append(m.posting.company)
        if m.posting.company == "A" and attempts.count("A") == 1:
            raise ClaudeRateLimitError("Claude usage limit/rate limit hit (exit 1).")
        return ApplyReport(url=m.posting.url)

    sleep = _FakeSleep()
    said: list[str] = []
    res = run_queue([_match(90, "A"), _match(80, "B")], apply_one, _gate(),
                    say=said.append, rate_limit_wait_s=900, _sleep=sleep)

    assert attempts == ["A", "A", "B"]  # A retried after the wait, then the queue resumed
    assert [o.result for o in res.outcomes] == ["dry-run", "dry-run"]  # ONE outcome per match
    assert res.stopped_reason == "queue exhausted"
    assert sum(sleep.calls) == 900 and max(sleep.calls) <= 30  # chunked so kill poll works
    assert any("waiting 15 min (1/3)" in s for s in said)  # announces the wait


def test_rate_limit_wait_cap_stops_the_queue():
    def apply_one(m):
        raise ClaudeRateLimitError("You've hit your usage limit.")

    sleep = _FakeSleep()
    res = run_queue([_match(90, "A"), _match(80, "B")], apply_one, _gate(), say=_quiet,
                    rate_limit_wait_s=900, max_rate_limit_waits=2, _sleep=sleep)

    assert res.outcomes == []  # no phantom outcome; A stays queued for the next run
    assert sum(sleep.calls) == 2 * 900  # exactly max_rate_limit_waits full waits
    assert "usage limit" in res.stopped_reason and "rerun" in res.stopped_reason


def test_kill_file_during_wait_aborts_the_run():
    gate = _gate()

    def apply_one(m):
        raise ClaudeRateLimitError("rate limit")

    sleep = _FakeSleep(on_call=lambda n: gate.kill_file.write_text("stop"))
    res = run_queue([_match(90, "A")], apply_one, gate, say=_quiet,
                    rate_limit_wait_s=900, _sleep=sleep)

    assert len(sleep.calls) == 1  # aborted at the first 30s poll, not after 900s
    assert res.outcomes == []
    assert "kill switch" in res.stopped_reason and "wait" in res.stopped_reason


def test_auth_error_stops_with_actionable_reason():
    calls: list[str] = []

    def apply_one(m):
        calls.append(m.posting.company)
        raise ClaudeAuthError("Claude Code is not signed in (exit 1). Run `claude` and /login.")

    res = run_queue([_match(90, "A"), _match(80, "B")], apply_one, _gate(), say=_quiet,
                    _sleep=_FakeSleep())

    assert calls == ["A"]  # B never attempted — the CLI is dead for every match
    assert [o.result for o in res.outcomes] == ["failed"]
    assert "`claude`" in res.stopped_reason and "/login" in res.stopped_reason


def test_unavailable_error_stops_the_queue():
    def apply_one(m):
        raise ClaudeUnavailableError("Claude Code timed out (300s).")

    res = run_queue([_match(90, "A"), _match(80, "B")], apply_one, _gate(), say=_quiet,
                    _sleep=_FakeSleep())
    assert [o.result for o in res.outcomes] == ["failed"]
    assert "Claude" in res.stopped_reason


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} runner-resilience test(s) passed.")

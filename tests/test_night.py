"""Tests for the unattended night session (decision 185) — fully injected: no browser, no
network, no real waiting, no real clock."""

import json
from pathlib import Path

import pytest

from applicationbot import safety
from applicationbot.night import (
    Breaker,
    CycleReport,
    Journal,
    parse_deadline,
    outcome_kind,
    preflight,
    preflight_failures,
    render_report,
    run_night,
    session_dir,
)


class _Doc:
    """Stand-in for doctor.Check — preflight only reads these five fields."""
    def __init__(self, name, ok=True, detail="", fix="", required=True):
        self.name, self.ok, self.detail, self.fix, self.required = name, ok, detail, fix, required


def _clock(start=1000.0, step=60.0):
    """A fake clock that advances `step` seconds per read."""
    t = {"now": start}

    def now():
        t["now"] += step
        return t["now"]
    return now, t


def _driver(tmp_path, reports, *, goal=None, deadline_at=10 ** 9, kill_file=None,
            breaker=None, now=None, idle_s=60):
    """Run `run_night` over a scripted list of CycleReports (a trailing report repeats)."""
    journal = Journal(tmp_path / "sess")
    calls = {"cycles": 0, "waits": []}

    def cycle():
        i = calls["cycles"]
        calls["cycles"] += 1
        return reports[i] if i < len(reports) else reports[-1]

    def wait(seconds):
        calls["waits"].append(seconds)
        return True

    res = run_night(cycle, goal=goal, deadline_at=deadline_at, journal=journal,
                    kill_file=kill_file or (tmp_path / "KILL"), wait=wait, breaker=breaker,
                    idle_s=idle_s, now=now or (lambda: 1000.0))
    return res, journal, calls


def _apps(n, result="submitted", detail=""):
    return CycleReport("ok", [{"result": result, "company": f"C{i}", "role": "Dev",
                               "url": f"u{i}", "fit": 80, "detail": detail} for i in range(n)])


# ------------------------------------------------------------------ deadline parsing

def test_parse_deadline_accepts_durations():
    assert parse_deadline("8h", 0) == 8 * 3600
    assert parse_deadline("90m", 100) == 100 + 90 * 60
    assert parse_deadline("45s", 0) == 45


def test_parse_deadline_wall_clock_rolls_to_tomorrow_when_past():
    from datetime import datetime
    now = datetime(2026, 8, 18, 23, 30).timestamp()
    at = datetime.fromtimestamp(parse_deadline("07:00", now))
    assert (at.day, at.hour, at.minute) == (19, 7, 0)   # next 07:00 is tomorrow


def test_parse_deadline_wall_clock_same_day_when_still_ahead():
    from datetime import datetime
    now = datetime(2026, 8, 18, 5, 0).timestamp()
    at = datetime.fromtimestamp(parse_deadline("7:00am", now))
    assert (at.day, at.hour) == (18, 7)


def test_parse_deadline_rejects_nonsense_with_the_accepted_forms():
    with pytest.raises(ValueError) as e:
        parse_deadline("dawn", 0)
    assert "07:00" in str(e.value) and "8h" in str(e.value)


# ------------------------------------------------------------------ breaker + kinds

def test_outcome_kind_is_none_for_good_outcomes():
    assert outcome_kind("submitted", "confirmed") is None
    assert outcome_kind("dry-run", "12 filled") is None
    assert outcome_kind("blocked", "login: session expired") == "blocked:login"
    assert outcome_kind("failed", "TimeoutError: waiting for selector") == "failed:TimeoutError"


def test_breaker_trips_on_consecutive_failures_and_a_good_outcome_resets_the_streak():
    b = Breaker(max_consecutive=3, max_same_kind=99)
    b.record("failed:A")
    b.record("failed:B")
    b.record(None)                 # a success in between clears the streak
    assert not b.tripped
    for _ in range(3):
        b.record("failed:C")
    assert b.tripped and "in a row" in b.reason


def test_breaker_trips_on_one_repeating_failure_kind_even_when_interleaved():
    b = Breaker(max_consecutive=99, max_same_kind=3)
    for _ in range(3):
        b.record("blocked:login")
        b.record(None)             # successes do NOT clear the per-kind tally
    assert b.tripped and "blocked:login" in b.reason


# ------------------------------------------------------------------ the loop

def test_night_stops_when_the_goal_is_reached_and_counts_only_submissions(tmp_path):
    res, journal, calls = _driver(tmp_path, [_apps(2), _apps(2)], goal=4)
    assert res.stop_reason == "goal_reached"
    assert (res.submitted, res.cycles) == (4, 2)
    assert res.counts == {"submitted": 4}
    assert res.exit_code == 0
    kinds = [e["event"] for e in journal.read_events()]
    assert kinds.count("application") == 4 and kinds[-1] == "session_end"


def test_dry_run_outcomes_never_count_toward_the_goal(tmp_path):
    """A dry-run night prepares applications but submits none — the goal must never be
    'reached' by preparation, or an unattended run would report a night's work it did not do."""
    now, _ = _clock(start=0.0, step=400.0)   # exactly one cycle fits before the deadline
    res, _, calls = _driver(tmp_path, [_apps(3, "dry-run")], goal=3,
                            deadline_at=1000.0, now=now)  # the deadline is what ends it
    assert res.submitted == 0
    assert res.counts == {"dry-run": 3}
    assert res.stop_reason == "deadline"


def test_night_stops_at_the_deadline_and_reports_a_partial_night(tmp_path):
    now, _ = _clock(start=0.0, step=100.0)
    res, _, _ = _driver(tmp_path, [_apps(1)], goal=100, deadline_at=250.0, now=now)
    assert res.stop_reason == "deadline"
    assert res.submitted >= 1 and res.exit_code == 3


def test_kill_file_ends_the_night_before_the_next_cycle(tmp_path):
    kill = tmp_path / "KILL"
    calls = {"n": 0}

    def cycle():
        calls["n"] += 1
        kill.write_text("stop")     # the user halts mid-night
        return _apps(1)

    journal = Journal(tmp_path / "s")
    res = run_night(cycle, goal=50, deadline_at=10 ** 9, journal=journal, kill_file=kill,
                    wait=lambda s: True, now=lambda: 1.0)
    assert (calls["n"], res.stop_reason, res.exit_code) == (1, "kill", 4)


def test_breaker_ends_the_night_instead_of_burning_the_goal_on_one_bug(tmp_path):
    failing = CycleReport("ok", [{"result": "failed", "company": "C", "role": "R", "url": "u",
                                  "fit": 70, "detail": "TimeoutError: form never loaded"}] * 6)
    res, journal, _ = _driver(tmp_path, [failing], goal=100,
                              breaker=Breaker(max_consecutive=5, max_same_kind=99))
    assert res.stop_reason == "breaker" and res.exit_code == 5
    assert "in a row" in res.stop_detail
    assert res.kinds == {"failed:TimeoutError": 6}


def test_a_fatal_cycle_stops_the_night_because_waiting_cannot_fix_it(tmp_path):
    res, _, _ = _driver(tmp_path, [CycleReport("stop", [], "Claude sign-in required")], goal=10)
    assert (res.stop_reason, res.exit_code) == ("fatal", 7)
    assert "sign-in" in res.stop_detail


def test_empty_cycles_back_off_and_re_search_rather_than_ending_the_night(tmp_path):
    """A goal is a commitment (decision 146): an empty board waits and searches again."""
    reports = [CycleReport("empty"), CycleReport("empty"), _apps(1)]
    res, journal, calls = _driver(tmp_path, reports, goal=1, idle_s=60)
    assert res.stop_reason == "goal_reached"
    assert calls["waits"] == [60, 120]          # backoff doubles between fruitless cycles
    assert any(e["event"] == "idle" for e in journal.read_events())


def test_backoff_resets_after_a_productive_cycle(tmp_path):
    reports = [CycleReport("empty"), _apps(1), CycleReport("empty"), _apps(1)]
    res, _, calls = _driver(tmp_path, reports, goal=2, idle_s=60)
    assert calls["waits"] == [60, 60]


def test_backoff_stops_doubling_at_max_idle(tmp_path):
    journal = Journal(tmp_path / "s")
    waits = []
    seen = {"n": 0}

    def cycle():
        seen["n"] += 1
        return CycleReport("empty")

    def wait(s):
        waits.append(s)
        return True

    def now():
        return 0.0 if seen["n"] < 6 else 10 ** 9   # deadline bites after six cycles

    run_night(cycle, goal=1, deadline_at=1000.0, journal=journal, kill_file=tmp_path / "K",
              wait=wait, idle_s=100, max_idle_s=400, now=now)
    assert waits[:3] == [100, 200, 400] and set(waits[2:]) == {400}


# ------------------------------------------------------------------ preflight

def _pf(**kw):
    base = dict(goal=100, armed=True, cap=100, kill_file=Path("/nonexistent/KILL"),
                deadline_at=10 ** 9, dry_run=False, now=0.0, checks=[])
    base.update(kw)
    return preflight(**base)


def test_preflight_passes_a_properly_armed_night():
    assert preflight_failures(_pf()) == []


def test_preflight_fails_when_not_armed_and_names_the_fix():
    fail = preflight_failures(_pf(armed=False))
    assert len(fail) == 1 and fail[0].name == "Submission budget"
    assert "--arm" in fail[0].fix


def test_preflight_fails_when_the_cap_cannot_reach_the_goal():
    fail = preflight_failures(_pf(cap=10, goal=100))
    assert "cap 10/run is below the goal of 100" in fail[0].detail
    assert "max_submissions_per_run: 100" in fail[0].fix


def test_preflight_fails_when_the_kill_file_is_present(tmp_path):
    kill = tmp_path / "KILL"
    kill.write_text("")
    fail = preflight_failures(_pf(kill_file=kill))
    assert fail and fail[0].name == "Kill switch" and str(kill) in fail[0].fix


def test_preflight_fails_on_a_deadline_already_past():
    fail = preflight_failures(_pf(deadline_at=10.0, now=20.0))
    assert fail and fail[0].name == "Deadline"


def test_a_dry_run_night_needs_no_arming():
    assert preflight_failures(_pf(armed=False, dry_run=True)) == []


def test_preflight_carries_doctor_checks_through_with_their_fixes():
    checks = _pf(checks=[_Doc("Claude", ok=False, detail="not signed in", fix="run `claude`")])
    fail = preflight_failures(checks)
    assert fail[0].name == "Claude" and fail[0].fix == "run `claude`"


def test_a_failed_optional_doctor_check_never_blocks_the_night():
    checks = _pf(checks=[_Doc("Bot email", ok=False, detail="not linked", required=False)])
    assert preflight_failures(checks) == []


# ------------------------------------------------------------------ record on disk

def test_session_dir_never_reuses_a_directory(tmp_path):
    a = session_dir(tmp_path, now=1_000_000)
    a.mkdir(parents=True)
    b = session_dir(tmp_path, now=1_000_000)
    assert a != b


def test_report_names_the_number_the_stop_reason_and_every_application(tmp_path):
    res, journal, _ = _driver(tmp_path, [_apps(2)], goal=2)
    md = render_report(res, events=journal.read_events(), session=tmp_path, armed=True)
    assert "**2 submitted** of a 2 goal" in md
    assert "Stopped: **goal_reached**" in md
    assert md.count("| submitted |") >= 1 and "C0" in md and "C1" in md


def test_a_dry_run_report_says_nothing_was_submitted(tmp_path):
    res, journal, _ = _driver(tmp_path, [_apps(1, "dry-run")], goal=None, deadline_at=1000.0)
    md = render_report(res, events=journal.read_events(), session=tmp_path, armed=False)
    assert "DRY RUN, nothing was submitted" in md


def test_summary_json_is_machine_readable_and_carries_the_exit_code(tmp_path):
    res, _, _ = _driver(tmp_path, [_apps(1)], goal=1)
    s = json.loads(json.dumps(res.summary()))
    assert s["submitted"] == 1 and s["stop_reason"] == "goal_reached" and s["exit_code"] == 0


# ------------------------------------------------------------------ arming round-trip

def test_save_arming_returns_the_previous_state_so_a_night_can_put_it_back(tmp_path):
    p = tmp_path / "safety.yaml"
    p.write_text("armed: false\nmax_submissions_per_run: 10\nsome_other_key: keep-me\n")

    previous = safety.save_arming(True, 100, path=p)
    assert previous == {"armed": False, "max_submissions_per_run": 10}
    gate = safety.load_gate(path=p, kill_file=tmp_path / "KILL")
    assert gate.armed and gate.max_submissions_per_run == 100
    assert "keep-me" in p.read_text()          # unrelated keys survive

    safety.save_arming(previous["armed"], previous["max_submissions_per_run"], path=p)
    gate = safety.load_gate(path=p, kill_file=tmp_path / "KILL")
    assert not gate.armed and gate.max_submissions_per_run == 10


def test_save_arming_on_a_missing_file_reports_the_disarmed_default_as_previous(tmp_path):
    p = tmp_path / "new.yaml"
    assert safety.save_arming(True, 50, path=p) == {"armed": False, "max_submissions_per_run": 10}
    assert safety.load_gate(path=p, kill_file=tmp_path / "K").armed


# ------------------------------------------------------------------ the real cycle wiring

class _Args:
    """The subset of the CLI namespace `_build_cycle` reads."""
    resume = "r.yaml"
    profile = "p.yaml"
    filters = "f.yaml"
    backend = "auto"
    headed = False
    fresh = False
    min_fit = 70
    max_per_cycle = None
    no_tailor = False


class _Res:
    from_cache = False
    errors: list = []
    discovered = 10
    non_fillable: list = []

    def __init__(self, matches):
        self.matches = matches


class _Posting:
    company, title, url = "Acme", "Dev", "https://x/1"


class _Match:
    posting = _Posting()
    fit_score = 90


def _wire(monkeypatch, calls):
    """Point `_build_cycle`'s pipeline imports at fakes — no boards, no Claude, no browser.
    Returns the list that collects the kwargs each application was applied with."""
    from applicationbot import pipeline, resume as resume_mod, filters as filters_mod
    from applicationbot import apply_profile, runner as runner_mod

    monkeypatch.setattr(resume_mod, "load_resume", lambda p: "RESUME")
    monkeypatch.setattr(filters_mod, "load_filters", lambda p: "FILTERS")
    monkeypatch.setattr(apply_profile, "load_profile", lambda p: "PROFILE")

    def discover(resume, filters, **kw):
        calls.append(kw)
        return _Res([_Match()])
    monkeypatch.setattr(pipeline, "discover_and_match", discover)
    monkeypatch.setattr(pipeline, "effective_min_fit", lambda f: (70, ""))

    applied: list = []

    def fake_apply(resume, match, resume_yaml, profile_path, **kw):
        applied.append(kw)
    monkeypatch.setattr(pipeline, "run_testing_mode", fake_apply)
    monkeypatch.setattr(runner_mod, "cleared_queue", lambda matches, min_fit: list(matches))

    class _Outcome:
        result, company, role, url, fit, detail = "submitted", "Acme", "Dev", "u", 90, "confirmed"

    class _RR:
        outcomes = [_Outcome()]
        stopped_reason = "queue exhausted"

    def fake_run_queue(queue, apply_one, gate, **kw):
        for m in queue:          # really call apply_one, so the résumé decision is exercised
            apply_one(m)
        return _RR()
    monkeypatch.setattr(runner_mod, "run_queue", fake_run_queue)
    return applied


def test_only_the_first_cycle_revisits_so_a_night_never_re_prepares_its_own_work(monkeypatch, tmp_path):
    """With nobody reviewing overnight, every application the night prepares stays 'unreviewed';
    revisiting would re-discover and re-attempt it every cycle instead of moving on."""
    from applicationbot.night import _build_cycle
    calls: list = []
    _wire(monkeypatch, calls)
    cycle = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))

    report = cycle()
    cycle()
    cycle()
    assert [c["revisit"] for c in calls] == [True, False, False]
    assert report.status == "ok" and report.submitted == 1
    assert report.outcomes[0]["company"] == "Acme"


def test_a_cycle_with_nothing_cleared_reports_empty_not_a_failure(monkeypatch, tmp_path):
    from applicationbot import runner as runner_mod
    from applicationbot.night import _build_cycle
    calls: list = []
    _wire(monkeypatch, calls)
    monkeypatch.setattr(runner_mod, "cleared_queue", lambda matches, min_fit: [])
    report = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))()
    assert report.status == "empty" and "min-fit 70" in report.detail


def test_a_cycle_that_needs_a_claude_sign_in_is_fatal_not_retryable(monkeypatch, tmp_path):
    from applicationbot import runner as runner_mod
    from applicationbot.night import _build_cycle
    calls: list = []
    _wire(monkeypatch, calls)

    class _RR:
        outcomes: list = []
        stopped_reason = "Claude sign-in required — run `claude` in a terminal"
    monkeypatch.setattr(runner_mod, "run_queue", lambda *a, **k: _RR())
    assert _build_cycle(_Args(), object(), Journal(tmp_path / "s"))().status == "stop"


def test_a_transient_discovery_failure_backs_off_instead_of_ending_the_night(monkeypatch, tmp_path):
    """A board outage or a parser error is not a reason to stop applying for the rest of the night."""
    from applicationbot import pipeline
    from applicationbot.night import _build_cycle
    calls: list = []
    _wire(monkeypatch, calls)

    def boom(*a, **k):
        raise RuntimeError("board returned 503")
    monkeypatch.setattr(pipeline, "discover_and_match", boom)
    report = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))()
    assert report.status == "empty" and "503" in report.detail


def test_discovery_that_keeps_failing_stops_the_night_rather_than_spinning(monkeypatch, tmp_path):
    from applicationbot import pipeline
    from applicationbot.night import MAX_DISCOVERY_FAILURES, _build_cycle
    calls: list = []
    _wire(monkeypatch, calls)
    monkeypatch.setattr(pipeline, "discover_and_match",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")))
    cycle = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))
    statuses = [cycle().status for _ in range(MAX_DISCOVERY_FAILURES)]
    assert statuses[:-1] == ["empty"] * (MAX_DISCOVERY_FAILURES - 1)
    assert statuses[-1] == "stop"


def test_one_good_search_clears_the_discovery_failure_streak(monkeypatch, tmp_path):
    from applicationbot import pipeline
    from applicationbot.night import _build_cycle
    calls: list = []
    _wire(monkeypatch, calls)
    good = pipeline.discover_and_match
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] in (1, 2, 4, 5, 6, 7):
            raise RuntimeError("flaky board")
        return good(*a, **k)
    monkeypatch.setattr(pipeline, "discover_and_match", flaky)
    cycle = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))
    assert [cycle().status for _ in range(7)] == [
        "empty", "empty", "ok", "empty", "empty", "empty", "empty"]   # never reaches 5 in a row


def test_a_claude_sign_in_failure_during_discovery_is_fatal(monkeypatch, tmp_path):
    from applicationbot import pipeline
    from applicationbot.backends import ClaudeAuthError
    from applicationbot.night import _build_cycle
    calls: list = []
    _wire(monkeypatch, calls)
    monkeypatch.setattr(pipeline, "discover_and_match",
                        lambda *a, **k: (_ for _ in ()).throw(ClaudeAuthError("not signed in")))
    report = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))()
    assert report.status == "stop" and "sign-in" in report.detail


# ------------------------------------------------------------------ which résumé goes out

def test_no_tailor_sends_the_users_own_resume_with_no_claude_call(monkeypatch, tmp_path):
    """The point of --no-tailor: no per-application tailoring spend, and nothing the user has
    never read is sent out."""
    from applicationbot.night import _build_cycle, describe_resume_policy
    args = _Args()
    args.no_tailor = True
    applied = _wire(monkeypatch, [])
    cycle = _build_cycle(args, object(), Journal(tmp_path / "s"))
    cycle()
    assert applied[0]["tailor"] is False and applied[0]["force_retailor"] is False
    assert cycle.policy["mode"] == "never"
    assert "no tailoring" in describe_resume_policy(cycle.policy)


def test_without_the_flag_the_night_tailors_like_the_app_does(monkeypatch, tmp_path):
    from applicationbot.night import _build_cycle
    applied = _wire(monkeypatch, [])
    cycle = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))
    cycle()
    assert applied[0]["tailor"] is True and cycle.policy["mode"] == "smart"


def test_the_night_follows_the_saved_loop_settings_policy(monkeypatch, tmp_path):
    """One résumé policy, not two: what ⚙ Loop settings says the app does, the night does too."""
    from types import SimpleNamespace
    from applicationbot import filters as filters_mod
    from applicationbot.night import _build_cycle
    applied = _wire(monkeypatch, [])
    saved = SimpleNamespace(tailor_mode="under", tailor_below_fit=95, reuse_threshold=0.5)
    monkeypatch.setattr(filters_mod, "load_filters", lambda p: saved)
    cycle = _build_cycle(_Args(), object(), Journal(tmp_path / "s"))
    cycle()
    # the fake match scores 90, under the saved 95 bar ⇒ tailored, at the saved reuse threshold
    assert applied[0]["tailor"] is True and applied[0]["reuse_threshold"] == 0.5


def test_no_tailor_overrides_a_saved_always_policy_for_this_run_only(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from applicationbot import filters as filters_mod
    from applicationbot.night import _build_cycle
    applied = _wire(monkeypatch, [])
    saved = SimpleNamespace(tailor_mode="always", tailor_below_fit=70, reuse_threshold=0.9)
    monkeypatch.setattr(filters_mod, "load_filters", lambda p: saved)
    args = _Args()
    args.no_tailor = True
    _build_cycle(args, object(), Journal(tmp_path / "s"))()
    assert applied[0]["tailor"] is False
    assert saved.tailor_mode == "always"     # the saved setting is untouched


def test_the_report_names_which_resume_went_out(tmp_path):
    from applicationbot.night import describe_resume_policy
    res, journal, _ = _driver(tmp_path, [_apps(1)], goal=1)
    note = describe_resume_policy({"mode": "never"})
    md = render_report(res, events=journal.read_events(), session=tmp_path, armed=True,
                       resume_note=note)
    assert "Résumé sent: yours as it stands" in md


def test_each_policy_mode_describes_itself_in_plain_words():
    from applicationbot.night import describe_resume_policy
    assert "under 60" in describe_resume_policy({"mode": "under", "below": 60})
    assert "no reuse" in describe_resume_policy({"mode": "always"})
    assert "reusing" in describe_resume_policy({})       # missing ⇒ the smart default

"""Doctor + continuous-loop tests (decision 048) — offline, zero-token.

Doctor: each readiness check reports pass/fail with an actionable fix; a missing required file
fails the run, a missing optional one only warns. Continuous: `continuous_loop` repeats until the
kill file appears, stops immediately on a fatal 'stop', and never waits for real time.

Run:  python -m tests.test_doctor   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from applicationbot import doctor, runner
from applicationbot.doctor import Check


def _tmp(name: str, text: str) -> str:
    d = Path(tempfile.mkdtemp())
    p = d / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _stub_external(monkeypatch, claude=True, playwright=True):
    monkeypatch.setattr(doctor, "_check_claude",
                        lambda: Check("Claude Code CLI", claude, "stub"))
    monkeypatch.setattr(doctor, "_check_playwright",
                        lambda: Check("Playwright (browser autofill)", playwright, "stub"))


def test_all_required_pass(monkeypatch):
    _stub_external(monkeypatch)
    checks = doctor.run_checks(
        resume_path="examples/sample_resume.yaml",
        profile_path=_tmp("application_profile.yaml", "{}\n"),
        filters_path=_tmp("discovery.yaml", "boards:\n  - {ats: greenhouse, token: stripe}\n"),
    )
    # Every REQUIRED check passes with valid inputs. Optional checks (e.g. the Workday bot-email
    # link, decision 053) may legitimately warn when their integration isn't configured.
    assert all(c.ok for c in checks if c.required)
    names = {c.name for c in checks}
    assert "Résumé (profile/resume.yaml)" in names and "Discovery sources" in names


def test_missing_resume_is_required_failure(monkeypatch):
    _stub_external(monkeypatch)
    checks = doctor.run_checks(
        resume_path="profile/does-not-exist.yaml",
        profile_path=_tmp("application_profile.yaml", "{}\n"),
        filters_path=_tmp("discovery.yaml", "boards:\n  - {ats: lever, token: cin7}\n"),
    )
    resume = next(c for c in checks if c.name.startswith("Résumé"))
    assert resume.ok is False and resume.required and resume.mark == "✗"
    assert "resume.yaml" in resume.fix


def test_no_sources_configured_fails(monkeypatch):
    _stub_external(monkeypatch)
    checks = doctor.run_checks(
        resume_path="examples/sample_resume.yaml",
        profile_path=_tmp("application_profile.yaml", "{}\n"),
        filters_path=_tmp("discovery.yaml", "remote_only: true\n"),
    )
    disc = next(c for c in checks if c.name == "Discovery sources")
    assert disc.ok is False and "no sources" in disc.detail and "greenhouse" in disc.fix


def test_career_sites_count_as_a_source(monkeypatch):
    _stub_external(monkeypatch)
    checks = doctor.run_checks(
        resume_path="examples/sample_resume.yaml",
        profile_path=_tmp("application_profile.yaml", "{}\n"),
        filters_path=_tmp("discovery.yaml", "career_sites:\n  - https://jobs.lever.co/cin7\n"),
    )
    disc = next(c for c in checks if c.name == "Discovery sources")
    assert disc.ok and "career site" in disc.detail


def test_missing_profile_is_optional_warning(monkeypatch):
    _stub_external(monkeypatch)
    checks = doctor.run_checks(
        resume_path="examples/sample_resume.yaml",
        profile_path="profile/nope.yaml",
        filters_path=_tmp("discovery.yaml", "boards:\n  - {ats: ashby, token: ramp}\n"),
    )
    prof = next(c for c in checks if c.name == "Applicant profile")
    assert prof.ok is False and prof.required is False and prof.mark == "⚠"
    # main() returns 0 when only optional checks fail.
    assert not [c for c in checks if not c.ok and c.required]


def test_main_exit_code(monkeypatch):
    _stub_external(monkeypatch, claude=False)  # a required failure
    rc = doctor.main([
        "--resume", "examples/sample_resume.yaml",
        "--profile", _tmp("application_profile.yaml", "{}\n"),
        "--filters", _tmp("discovery.yaml", "boards:\n  - {ats: greenhouse, token: stripe}\n"),
    ])
    assert rc == 1


# --- continuous loop -------------------------------------------------------

class _FakeGate:
    """kill_file.exists() becomes True after `kill_after` polls (simulates the KILL file)."""
    def __init__(self, kill_after=999):
        self._polls = 0
        self._kill_after = kill_after
        self.kill_file = SimpleNamespace(exists=self._exists)
        self.kill_file.__str__ = lambda: "profile/KILL"  # for the message

    def _exists(self):
        self._polls += 1
        return self._polls > self._kill_after


def test_continuous_loop_runs_until_kill():
    # kill file appears while waiting after cycle 2 (the _wait_for_reset poll trips it).
    gate = _FakeGate(kill_after=4)  # top-of-loop poll ×2 + wait polls
    cycles = []
    ended = runner.continuous_loop(
        lambda: (cycles.append(1) or "ok"), gate,
        interval_s=1, say=lambda m: None, _sleep=lambda s: None,
    )
    assert ended == "kill" and len(cycles) >= 1


def test_continuous_loop_stops_on_fatal():
    gate = _FakeGate(kill_after=999)  # never killed
    calls = []

    def run_cycle():
        calls.append(1)
        return "stop"  # fatal on the first cycle

    ended = runner.continuous_loop(run_cycle, gate, interval_s=1,
                                   say=lambda m: None, _sleep=lambda s: None)
    assert ended == "stop" and len(calls) == 1  # did not wait or loop again


def _run_all():
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        kw = {"monkeypatch": _MP()} if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount] else {}
        saved = (doctor._check_claude, doctor._check_playwright)
        try:
            fn(**kw)
        finally:
            doctor._check_claude, doctor._check_playwright = saved
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

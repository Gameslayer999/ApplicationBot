"""SafetyGate unit tests (decision 035) — pure logic, no browser, no tokens.

Run:  python -m tests.test_safety   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot.safety import SafetyGate, load_gate


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def test_disarmed_by_default():
    d = _tmp()
    gate = load_gate(path=d / "missing.yaml", kill_file=d / "KILL")
    assert gate.armed is False
    ok, reason = gate.may_submit()
    assert ok is False and "not armed" in reason


def test_armed_gate_allows():
    gate = SafetyGate(armed=True, kill_file=_tmp() / "KILL")
    ok, reason = gate.may_submit()
    assert ok is True and reason == "armed"


def test_kill_switch_halts():
    d = _tmp()
    kill = d / "KILL"
    gate = SafetyGate(armed=True, kill_file=kill)
    kill.write_text("stop")
    ok, reason = gate.may_submit()
    assert ok is False and "kill switch" in reason
    kill.unlink()  # deleting the file resumes
    assert gate.may_submit()[0] is True


def test_per_run_cap():
    gate = SafetyGate(armed=True, max_submissions_per_run=2, kill_file=_tmp() / "KILL")
    gate.record_submission()
    assert gate.may_submit()[0] is True
    gate.record_submission()
    ok, reason = gate.may_submit()
    assert ok is False and "cap" in reason


def test_load_gate_reads_yaml():
    d = _tmp()
    (d / "safety.yaml").write_text("armed: true\nmax_submissions_per_run: 3\n")
    gate = load_gate(path=d / "safety.yaml", kill_file=d / "KILL")
    assert gate.armed is True and gate.max_submissions_per_run == 3


def test_unreadable_config_never_arms():
    d = _tmp()
    (d / "safety.yaml").write_text("{{{ not yaml")
    gate = load_gate(path=d / "safety.yaml", kill_file=d / "KILL")
    assert gate.armed is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} safety test(s) passed.")

"""Submit-path tests (decision 035) — driven against LOCAL HTML fixtures, never a real
posting (Guideline #3) and never a Claude call (zero tokens).

Run:  python -m tests.test_submit   (also pytest-compatible; needs `playwright install chromium`)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot.apply import AnswerResolver, ApplyReport, _attempt_submit, run_apply
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.resume import load_resume
from applicationbot.safety import SafetyGate

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "apply_forms"
CONFIRM = (FIXTURES / "submit_confirm.html").as_uri()
REJECT = (FIXTURES / "submit_reject.html").as_uri()


def _gate(**kw) -> SafetyGate:
    return SafetyGate(armed=True, kill_file=Path(tempfile.mkdtemp()) / "KILL", **kw)


def _dummy_pdf() -> str:
    p = Path(tempfile.mkdtemp()) / "resume.pdf"
    p.write_bytes(b"%PDF-1.4 test fixture resume")
    return str(p)


def _resolver() -> AnswerResolver:
    return AnswerResolver(
        resume=load_resume(str(REPO / "examples" / "sample_resume.yaml")),
        profile=ApplicationProfile(),
        enable_generation=False,  # no Claude — structured/résumé answers only
    )


# ---- pre-submit gate (no browser needed: blocked before the page is ever touched) ----

def test_blocked_on_unresolved_required_field():
    r = ApplyReport(url="x")
    r.skipped.append("GPA — REQUIRED, not filled (no matching answer or unsupported field)")
    _attempt_submit(None, None, r, _gate())
    assert r.submitted is False and r.submit_state == "blocked"
    assert "GPA" in r.blockers[0] and "required" in r.blockers[0].lower()


def test_blocked_when_not_armed():
    r = ApplyReport(url="x")
    _attempt_submit(None, None, r, SafetyGate(armed=False))
    assert r.submitted is False and r.submit_state == "blocked"
    assert "not armed" in r.blockers[0]


def test_blocked_by_kill_switch():
    d = Path(tempfile.mkdtemp())
    (d / "KILL").write_text("stop")
    r = ApplyReport(url="x")
    _attempt_submit(None, None, r, SafetyGate(armed=True, kill_file=d / "KILL"))
    assert r.submitted is False and "kill switch" in r.blockers[0]


# ---- browser tests against the local fixtures ----

def test_submit_click_and_confirmation():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(CONFIRM)
        for sel, val in (("#first_name", "Alex"), ("#last_name", "Sample"),
                         ("#email", "alex@example.com")):
            page.fill(sel, val)
        r = ApplyReport(url=CONFIRM)
        gate = _gate()
        _attempt_submit(page, page.main_frame, r, gate)
        browser.close()
    assert r.submitted is True and r.submit_state == "submitted"
    assert "thank you for applying" in r.confirmation.lower()
    assert gate.submitted_this_run == 1


def test_validation_rejection_is_blocked_not_submitted():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(REJECT)
        # Fill the required fields so the pre-submit gate passes and the CLICK happens —
        # this test exercises the server/client rejection path, not the gate.
        for sel, val in (("#first_name", "Alex"), ("#last_name", "Sample"),
                         ("#email", "alex@example.com")):
            page.fill(sel, val)
        r = ApplyReport(url=REJECT)
        _attempt_submit(page, page.main_frame, r, _gate())
        browser.close()
    assert r.submitted is False and r.submit_state == "blocked"
    assert "rejected" in r.blockers[0]


def test_pre_submit_gate_blocks_on_empty_required_dom_fields():
    # No fields filled at all: the live DOM re-scan must block BEFORE any click.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(REJECT)
        r = ApplyReport(url=REJECT)
        _attempt_submit(page, page.main_frame, r, _gate())
        browser.close()
    assert r.submitted is False and r.submit_state == "blocked"
    assert "required" in r.blockers[0].lower() and "First Name" in r.blockers[0]


# ---- end-to-end run_apply against the fixture (fill → gate → submit) ----

def test_e2e_dry_run_never_submits():
    report = run_apply(
        CONFIRM, _dummy_pdf(), _resolver(),
        headed=False, pause=False, slow_mo=0, learn=False, record=False,
        screenshot=str(Path(tempfile.mkdtemp()) / "shot.png"),
    )
    assert report.submitted is False and report.submit_state == "dry-run"
    assert any(f.label.lower().startswith("email") for f in report.filled)


def test_e2e_armed_run_submits():
    gate = _gate()
    report = run_apply(
        CONFIRM, _dummy_pdf(), _resolver(),
        headed=False, pause=False, slow_mo=0, learn=False, record=False,
        screenshot=str(Path(tempfile.mkdtemp()) / "shot.png"),
        gate=gate,
    )
    assert report.submitted is True and report.submit_state == "submitted"
    assert "thank you for applying" in report.confirmation.lower()
    assert gate.submitted_this_run == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} submit test(s) passed.")

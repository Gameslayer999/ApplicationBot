"""Multi-page (wizard) form navigation tests — local fixtures, no tokens, no real postings.

Run:  python -m tests.test_multipage   (also pytest-compatible; needs chromium installed)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot.apply import AnswerResolver, run_apply
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.resume import load_resume
from applicationbot.safety import SafetyGate

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "apply_forms"
WIZARD = (FIXTURES / "submit_wizard.html").as_uri()
STRICT = (FIXTURES / "submit_wizard_strict.html").as_uri()


def _gate() -> SafetyGate:
    return SafetyGate(armed=True, kill_file=Path(tempfile.mkdtemp()) / "KILL")


def _dummy_pdf() -> str:
    p = Path(tempfile.mkdtemp()) / "resume.pdf"
    p.write_bytes(b"%PDF-1.4 test fixture resume")
    return str(p)


def _run(url, gate=None):
    return run_apply(
        url, _dummy_pdf(),
        AnswerResolver(resume=load_resume(str(REPO / "examples" / "sample_resume.yaml")),
                       profile=ApplicationProfile(), enable_generation=False),
        headed=False, pause=False, slow_mo=0, learn=False, record=False,
        screenshot=str(Path(tempfile.mkdtemp()) / "shot.png"), gate=gate,
    )


def test_wizard_dry_run_walks_all_pages():
    report = _run(WIZARD)
    assert report.pages == 3, report.summary()
    labels = {f.label for f in report.filled}
    assert any(l.lower().startswith("email") for l in labels)  # page 1
    assert any(l.lower().startswith("phone") for l in labels)  # page 2 — proves we advanced
    assert report.submitted is False and report.submit_state == "dry-run"
    assert "found:" in report.submit_probe  # final page's submit control was probed, not clicked


def test_wizard_armed_submits_from_final_page():
    gate = _gate()
    report = _run(WIZARD, gate=gate)
    assert report.pages == 3, report.summary()
    assert report.submitted is True and report.submit_state == "submitted"
    assert "thank you for applying" in report.confirmation.lower()
    assert gate.submitted_this_run == 1


def test_wizard_resume_uploads_on_later_page():
    report = _run(WIZARD)
    assert any(f.control == "file" for f in report.filled), report.summary()


def test_strict_wizard_blocked_when_advance_rejected():
    # "Referring Employee ID" is unanswerable → the wizard refuses to advance → the walk
    # stops on page 1 and an armed run must be BLOCKED (required field), never submitted.
    report = _run(STRICT, gate=_gate())
    assert report.pages == 1, report.summary()
    assert report.submitted is False and report.submit_state == "blocked"
    assert "Referring Employee ID" in report.blockers[0]
    assert any("could not advance" in e for e in report.errors)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} multi-page test(s) passed.")

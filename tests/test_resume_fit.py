"""Résumé generation: delta tailoring output + measured one-page fit + JD prompt trim
(decision 042). No tokens: the Claude CLI is stubbed, PDFs render locally via fpdf2.

Run:  python -m tests.test_resume_fit   (also pytest-compatible)
"""
from __future__ import annotations

import json
from pathlib import Path

from applicationbot import backends, pdf
from applicationbot.backends import ClaudeCodeBackend, TailorDelta, _delta_to_tailored
from applicationbot.job_description import JobDescription, trim_for_prompt
from applicationbot.length import LengthBudget
from applicationbot.models import Experience, TailoredResume
from applicationbot.resume import load_resume
from applicationbot.tailor import tailor_resume

REPO = Path(__file__).resolve().parent.parent
BASE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))
LONG_BULLET = ("Designed, built, shipped, and operated a mission-critical distributed system "
               "handling millions of requests per day with strict latency and reliability targets")


def _tailored_from_base() -> TailoredResume:
    return TailoredResume(
        summary=BASE.summary,
        skills=[c.model_copy(deep=True) for c in BASE.skills],
        experience=[e.model_copy(deep=True) for e in BASE.experience],
        projects=[p.model_copy(deep=True) for p in BASE.projects],
        activities=[a.model_copy(deep=True) for a in BASE.activities],
        education=[e.model_copy(deep=True) for e in BASE.education],
        certifications=list(BASE.certifications),
    )


# ------------------------------------------------------------------ measured page fit

def test_fit_is_noop_when_already_one_page():
    t = _tailored_from_base()
    t.experience = t.experience[:1]
    t.projects, t.activities = [], []
    fitted, notes = pdf.fit_to_pages(BASE, t, 1)
    assert notes == []
    assert pdf.page_count(BASE, fitted) == 1


def test_fit_trims_overflow_to_exactly_one_page_and_says_what_dropped():
    t = _tailored_from_base()
    for e in t.experience:
        e.bullets = [LONG_BULLET] * 6
    t.experience = t.experience * 2  # 4 fat entries — guaranteed to spill past page 1
    assert pdf.page_count(BASE, t) > 1
    fitted, notes = pdf.fit_to_pages(BASE, t, 1)
    assert pdf.page_count(BASE, fitted) == 1
    assert notes and "Trimmed to fit 1 page(s)" in notes[0]
    # Bullets were trimmed least-relevant-first but never below the floor on surviving entries.
    assert all(len(e.bullets) >= 2 for e in fitted.experience)
    assert fitted.experience, "must keep at least one experience entry"


def test_tailor_resume_wires_the_guarantee_end_to_end():
    # rules backend (no LLM) + a bullet-heavy base → the RESULT of tailor_resume always fits.
    heavy = BASE.model_copy(deep=True)
    for e in heavy.experience:
        e.bullets = [LONG_BULLET] * 6
    jd = JobDescription(body="Python React AWS backend services", meta={"title": "SWE", "company": "X"})
    res = tailor_resume(heavy, jd, backend="rules")
    assert pdf.page_count(heavy, res.tailored) == 1


# ------------------------------------------------------------------ delta reconstruction

def test_delta_reconstruction_copies_structure_and_applies_rewrites():
    delta = TailorDelta(
        summary="Tailored summary",
        experience=[{"i": 1, "bullets": ["Rewritten bullet one"], "tailor_note": "most relevant"},
                    {"i": 0, "bullets": []}],       # empty bullets → base bullets kept
        projects=[{"i": 0, "bullets": ["Project bullet"]}],
        skills=[{"category": "Languages", "items": ["Python"]}],
        relevance_notes=["note"],
    )
    t = _delta_to_tailored(BASE, delta)
    # Order follows the delta; structural fields are verbatim copies of the base entries.
    assert t.experience[0].organization == BASE.experience[1].organization
    assert t.experience[0].start == BASE.experience[1].start
    assert t.experience[0].bullets == ["Rewritten bullet one"]
    assert t.experience[0].tailor_note == "most relevant"
    assert t.experience[1].bullets == BASE.experience[0].bullets
    assert t.projects[0].name == BASE.projects[0].name and t.projects[0].tech == BASE.projects[0].tech
    # Education and certifications can never be dropped or mangled — copied wholesale.
    assert [e.school for e in t.education] == [e.school for e in BASE.education]
    assert t.certifications == BASE.certifications
    assert t.summary == "Tailored summary"


def test_delta_ignores_bad_indices_and_gates_summary():
    delta = TailorDelta(summary="S", experience=[{"i": 99}, {"i": 0}, {"i": 0}])
    t = _delta_to_tailored(BASE, delta)
    assert len(t.experience) == 1  # 99 out of range, duplicate 0 deduped
    no_summary = BASE.model_copy(deep=True, update={"summary": None})
    t2 = _delta_to_tailored(no_summary, delta)
    assert t2.summary is None  # summary only if the base resume has one


def test_claude_backend_parses_delta_and_reconstructs():
    reply = json.dumps({
        "summary": "S",
        "experience": [{"i": 0, "bullets": ["B1", "B2"], "tailor_note": "n"}],
        "projects": [], "activities": [],
        "skills": [{"category": "Languages", "items": ["Python"]}],
        "relevance_notes": ["r"],
    })
    real, backends.run_claude_cli = backends.run_claude_cli, lambda p, **kw: reply
    try:
        jd = JobDescription(body="Python backend", meta={"title": "SWE", "company": "X"})
        out = ClaudeCodeBackend().tailor(BASE, jd, LengthBudget())
    finally:
        backends.run_claude_cli = real
    assert isinstance(out, TailoredResume)
    assert out.experience[0].organization == BASE.experience[0].organization
    assert out.experience[0].bullets == ["B1", "B2"]
    assert out.education and out.certifications == BASE.certifications


# ------------------------------------------------------------------ JD prompt trim

def test_trim_cuts_trailing_boilerplate_but_not_early_mentions():
    body = ("Requirements: we provide reasonable accommodation during interviews.\n\n"
            + "Real requirements line.\n\n" * 40
            + "AcmeCo is an Equal Opportunity Employer. All qualified applicants...")
    out = trim_for_prompt(body)
    assert "Equal Opportunity Employer" not in out
    assert "Real requirements line." in out
    assert "reasonable accommodation during interviews" in out  # early mention survives


def test_trim_caps_length_on_paragraph_boundary():
    body = "para\n\n" * 3000
    out = trim_for_prompt(body, cap=1000)
    assert len(out) <= 1000
    assert out.endswith("para")


def _main() -> int:
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                fails += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())

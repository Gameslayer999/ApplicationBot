"""ATS text-layer verification (decision 043). No tokens: PDFs render locally via fpdf2
and are read back with pypdf.

Run:  python -m tests.test_ats_check   (also pytest-compatible)
"""
from __future__ import annotations

from pathlib import Path

from applicationbot import pdf
from applicationbot.ats_check import verify_pdf
from applicationbot.models import TailoredResume
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
BASE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))

JD = ("We are hiring a full-stack engineer. Requirements: TypeScript, React, and "
      "PostgreSQL in production; Kubernetes a plus.")


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


def test_clean_pdf_passes_and_covers_jd_keywords():
    data = pdf.render_pdf(BASE, _tailored_from_base())
    report = verify_pdf(data, BASE, JD)
    assert report.problems == []
    # Every JD-requested skill the candidate has is in the PDF (full tailored content).
    assert {"TypeScript", "React", "PostgreSQL", "Kubernetes"} <= set(report.covered)
    assert report.dropped == []
    assert report.ok
    # Phone matches on digits despite "(555) 010-4477" formatting.
    assert not any("phone" in p for p in report.problems)


def test_dropped_skill_is_reported_with_fix():
    t = _tailored_from_base()
    # Cut every skill group and bullet mentioning PostgreSQL — the JD asks for it.
    t.skills = [c for c in t.skills if "PostgreSQL" not in c.items]
    for group in (t.experience, t.projects, t.activities):
        for e in group:
            e.bullets = [b for b in e.bullets if "postgres" not in b.lower()]
    for p in t.projects:
        p.tech = ", ".join(x for x in (p.tech or "").split(", ") if "postgres" not in x.lower())
    if t.summary and "postgres" in t.summary.lower():
        t.summary = "Full-stack engineer."
    report = verify_pdf(pdf.render_pdf(BASE, t), BASE, JD)
    assert "PostgreSQL" in report.dropped
    assert not report.ok
    notes = "\n".join(report.notes())
    assert "PostgreSQL" in notes and "trimmed out" in notes


def test_non_latin1_name_mangling_is_caught():
    base = BASE.model_copy(deep=True)
    # ầ / ū are OUTSIDE latin-1 (unlike é), so the core-font render replaces them with '?'.
    base.contact.name = "Trần Hūng"
    report = verify_pdf(pdf.render_pdf(base, _tailored_from_base()), base, None)
    assert any("name" in p and "Trần Hūng" in p for p in report.problems)


def test_unreadable_pdf_is_a_problem_not_a_crash():
    report = verify_pdf(b"this is not a pdf", BASE, JD)
    assert report.problems and report.covered == [] and report.dropped == []


def test_no_jd_checks_readability_only():
    report = verify_pdf(pdf.render_pdf(BASE, _tailored_from_base()), BASE, None)
    assert report.problems == [] and report.covered == [] and report.dropped == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("test_ats_check: all passed")

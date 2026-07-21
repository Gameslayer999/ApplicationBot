"""Deterministic "dummy ATS" built from a JD (ats_requirements). No Claude, no network.

Verifies the three things the tailoring loop relies on: (1) required keywords are only ever
skills the candidate actually has, (2) knockouts fire on the JD's hard gates and evaluate
against the résumé, and (3) `grade` scores a rendered résumé's keyword coverage correctly.

Run:  python -m pytest tests/test_ats_requirements.py -q
"""
from __future__ import annotations

from pathlib import Path

from applicationbot import ats_requirements
from applicationbot.models import Contact, Education, Experience, Resume, SkillCategory
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
SAMPLE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))


def _newgrad() -> Resume:
    return Resume(
        contact=Contact(name="A", email="a@x.com"),
        skills=[SkillCategory(category="Languages", items=["Python", "SQL", "React"])],
        experience=[Experience(organization="Acme", role="Intern",
                               start="Jun 2024", end="Aug 2024", bullets=[])],
        education=[Education(school="State U", degree="B.S. in Computer Science", graduation="2025")],
    )


def test_keywords_are_only_skills_the_candidate_has():
    r = _newgrad()
    req = ats_requirements.extract(r, "We need Python and Rust and Kubernetes experience.")
    # Python is on the résumé and in the JD → required. Rust/Kubernetes aren't on the résumé,
    # so they are NOT treated as keyword gaps (they're the fit judge's job, not tailoring's).
    assert "Python" in req.keywords
    assert "Rust" not in req.keywords and "Kubernetes" not in req.keywords


def test_years_knockout_fails_for_underqualified():
    req = ats_requirements.extract(_newgrad(), "Requires 8+ years of backend experience.")
    yk = [k for k in req.knockouts if k.label == "years"]
    assert yk and yk[0].passed is False


def test_degree_knockout_passes_when_met():
    req = ats_requirements.extract(_newgrad(), "Bachelor's degree required.")
    dk = [k for k in req.knockouts if k.label == "degree"]
    assert dk and dk[0].passed is True


def test_clearance_and_citizenship_flagged_unverifiable():
    jd = "Must hold an active security clearance. U.S. citizenship required."
    req = ats_requirements.extract(_newgrad(), jd)
    labels = {k.label: k.passed for k in req.knockouts}
    assert labels.get("clearance") is None and labels.get("citizenship") is None


def test_grade_scores_keyword_coverage():
    r = _newgrad()
    req = ats_requirements.extract(r, "We use Python, SQL, and React daily.")
    full = ats_requirements.grade("Skills: Python, SQL, React, Go", req)
    assert full.keyword_score == 100 and not full.missing and full.passed

    partial = ats_requirements.grade("Skills: Python only", req)
    assert partial.keyword_score < 100 and set(partial.missing) == {"SQL", "React"}


def test_grade_passed_gates_on_failed_knockout():
    req = ats_requirements.extract(_newgrad(), "Requires 10+ years and Python.")
    g = ats_requirements.grade("Python developer.", req)
    assert g.keyword_score == 100  # keyword present…
    assert g.passed is False  # …but the years knockout auto-rejects

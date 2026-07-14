"""Deterministic multi-factor pre-score (AutoApply-AI survey #3, decision 052).

Unit-tests each factor + the weighted/renormalized score, and the integration point that
matters: the free pre-filter (matching.keyword_rank) now orders the judge queue by this
score, so a role the résumé actually fits outranks a keyword-stuffed senior role. No Claude,
no network.

Run:  python -m pytest tests/test_ats_score.py -q
"""
from __future__ import annotations

from pathlib import Path

from applicationbot import ats_score, matching
from applicationbot.ats_score import (
    candidate_degree_rank,
    candidate_years,
    required_degree_rank,
    required_years,
    score_breakdown,
)
from applicationbot.discovery import Posting
from applicationbot.discovery_cache import _match_from_dict, _match_to_dict
from applicationbot.models import Contact, Education, Experience, Resume, SkillCategory
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
SAMPLE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))


def _newgrad() -> Resume:
    return Resume(
        contact=Contact(name="A", email="a@x.com"),
        skills=[SkillCategory(category="Languages", items=["Python", "SQL", "JavaScript", "React"])],
        experience=[Experience(organization="Acme", role="Intern",
                               start="Jun 2024", end="Aug 2024", bullets=[])],
        education=[Education(school="State U", degree="B.S. in Computer Science", graduation="2025")],
    )


# --------------------------------------------------------------------- factor extractors

def test_candidate_years_spans_earliest_start_to_latest_end():
    # Sample résumé runs Jul 2019 → Present, so ≥ 6 years of career span.
    assert candidate_years(SAMPLE) >= 6
    assert candidate_years(_newgrad()) < 1  # a one-summer internship


def test_required_years_takes_the_floor_and_ignores_absurd_numbers():
    assert required_years("we want 5+ years of experience") == 5
    assert required_years("entry level, 0-2 years experience") == 0
    assert required_years("8 to 10 years in backend") == 8
    assert required_years("no experience bar here") is None
    assert required_years("founded 100 years ago") is None  # > 40 → not an experience bar


def test_degree_rank_extraction():
    assert candidate_degree_rank(SAMPLE) == 3  # B.S.
    assert required_degree_rank("master's degree required") == 4
    assert required_degree_rank("bachelor's or master's degree") == 3  # floor = the lower
    assert required_degree_rank("phd required") == 5
    assert required_degree_rank("no degree mentioned") is None


# --------------------------------------------------------------------- the weighted score

def test_absent_requirements_are_renormalized_out():
    # A JD stating no years and no degree → score rides on skills + keyword only, not dragged to 0.
    b = score_breakdown(_newgrad(), "Software Engineer",
                        "Python and SQL. Build web apps.", matched_count=2)
    assert b.experience is None and b.education is None
    assert b.skills > 0 and 0 <= b.score <= 100


def test_experience_factor_penalizes_underqualification():
    grad = _newgrad()
    junior = score_breakdown(grad, "Engineer", "Python. Entry level, 0-2 years.", matched_count=1)
    senior = score_breakdown(grad, "Engineer", "Python. 8+ years required.", matched_count=1)
    assert senior.experience is not None and senior.experience < 0.3
    assert junior.score > senior.score  # same skills, the years gap sinks the senior role


def test_score_is_bounded():
    b = score_breakdown(SAMPLE, "Full-Stack Engineer",
                        "React, TypeScript, Node.js, SQL, GraphQL, Go. 3+ years. Bachelor's.",
                        matched_count=6)
    assert 0 <= b.score <= 100


# --------------------------------------------------- integration: it orders the judge queue

def test_keyword_rank_orders_by_ats_score_not_raw_overlap():
    """A senior role with MORE skill mentions but an 8-year bar must rank BELOW a new-grad role
    the résumé actually fits — the failure mode the raw keyword count had (decision 046)."""
    grad = _newgrad()
    senior = Posting(title="Staff Software Engineer", company="BigCo", location="SF",
                     url="https://x/senior", ats="greenhouse",
                     body="Python, SQL, JavaScript, React. 8+ years experience. Master's required.")
    junior = Posting(title="Software Engineer, New Grad", company="Startup", location="NYC",
                     url="https://x/junior", ats="greenhouse",
                     body="Python, SQL. New grads welcome, 0-2 years.")
    ranked = matching.keyword_rank(grad, [senior, junior])
    # Senior has the higher raw overlap…
    by_url = {m.posting.url: m for m in ranked}
    assert by_url["https://x/senior"].keyword_score > by_url["https://x/junior"].keyword_score
    # …but the pre-score puts the junior role first.
    assert ranked[0].posting.url == "https://x/junior"
    assert ranked[0].ats_score > ranked[1].ats_score


def test_ats_score_survives_cache_round_trip():
    grad = _newgrad()
    p = Posting(title="Engineer", company="Acme", location="NYC", url="https://x/1",
                ats="greenhouse", body="Python, SQL. 0-2 years.")
    m = matching.keyword_rank(grad, [p])[0]
    assert m.ats_score > 0
    restored = _match_from_dict(_match_to_dict(m))
    assert restored.ats_score == m.ats_score


def test_pre052_cache_snapshot_loads_with_zero():
    # A snapshot written before this field existed must still load.
    d = {"posting": {"title": "E", "company": "C", "location": "", "url": "u", "ats": "greenhouse",
                     "body": "b"},
         "keyword_score": 3, "matched_skills": ["Python"]}
    assert _match_from_dict(d).ats_score == 0

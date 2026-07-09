"""Multi-dimension fit rubric (decision 043): the judge returns per-dimension scores and
the overall fit is computed in code from FIT_WEIGHTS. Claude CLI is stubbed — no tokens.

Run:  python -m pytest tests/test_matching_dimensions.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

from applicationbot import matching
from applicationbot.discovery import Posting
from applicationbot.discovery_cache import _match_from_dict, _match_to_dict
from applicationbot.matching import FIT_WEIGHTS, Match, weighted_fit
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
BASE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))


def _posting() -> Posting:
    return Posting(title="Full-Stack Engineer", company="Acme", location="NYC",
                   url="https://jobs.example.com/1", ats="greenhouse",
                   body="TypeScript, React, PostgreSQL. 3+ years experience.")


def test_weighted_fit_uses_the_declared_weights():
    dims = {"skills": 80, "experience": 60, "seniority": 100}
    expected = round(80 * 0.45 + 60 * 0.35 + 100 * 0.20)
    assert weighted_fit(dims) == expected
    assert abs(sum(FIT_WEIGHTS.values()) - 1.0) < 1e-9


def test_weighted_fit_renormalizes_when_a_dimension_is_absent():
    # A missing dimension must not silently drag the score toward 0.
    assert weighted_fit({"skills": 80, "experience": 80}) == 80
    assert weighted_fit({}) == 0


def test_judge_verdict_carries_dimensions_and_computed_score(monkeypatch):
    reply = {"verdicts": [{"index": 0, "qualified": True, "skills": 90, "experience": 70,
                           "why": "Strong overlap.", "seniority": 50, "missing": ["Go"]}]}
    monkeypatch.setattr(matching, "claude_code_available", lambda: True)
    monkeypatch.setattr(matching, "run_claude_cli",
                        lambda *a, **k: json.dumps(reply))
    ranked, errors = matching.match(BASE, [_posting()], use_claude=True)
    assert errors == []
    m = ranked[0]
    assert m.judged_by == "claude"
    assert m.dimensions == {"skills": 90, "experience": 70, "seniority": 50}
    assert m.fit_score == weighted_fit(m.dimensions)  # computed, not model-reported
    assert m.qualified is True and m.missing == ["Go"]


def test_legacy_verdict_without_dimensions_falls_back_to_model_score():
    v = matching._clean_verdict({"qualified": True, "score": 62, "why": "", "missing": []})
    assert v["score"] == 62 and v["dimensions"] == {}


def test_out_of_range_dimensions_are_clamped():
    v = matching._clean_verdict({"qualified": True, "skills": 140, "experience": -5,
                                 "seniority": 50, "why": "", "missing": []})
    assert v["dimensions"] == {"skills": 100, "experience": 0, "seniority": 50}


def test_cache_roundtrip_and_pre_043_snapshot_load():
    m = Match(posting=_posting(), keyword_score=3, matched_skills=["React"],
              qualified=True, fit_score=81, why="ok", judged_by="claude",
              dimensions={"skills": 90, "experience": 70, "seniority": 80})
    assert _match_from_dict(_match_to_dict(m)).dimensions == m.dimensions
    # A snapshot written before dimensions existed loads with an empty dict.
    legacy = _match_to_dict(m)
    del legacy["dimensions"]
    assert _match_from_dict(legacy).dimensions == {}

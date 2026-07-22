"""Two-stage judging (decision 124): a cheap Haiku pre-rank widens the pool, then the full
Sonnet judge scores only the best top_n of it. Claude CLI is stubbed — no tokens.

Run:  python -m pytest tests/test_matching_prerank.py -q
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from applicationbot import matching
from applicationbot.discovery import Posting
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
BASE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))


def _posting(pre: int) -> Posting:
    # The title encodes the intended pre-rank fit ("p90") so the stub can score it; the body
    # carries a real skill so the keyword pre-filter keeps it.
    return Posting(title=f"Engineer p{pre}", company="Acme", location="NYC",
                   url=f"https://jobs.example.com/{pre}", ats="greenhouse",
                   body="TypeScript, React, PostgreSQL. 2+ years.")


def _fake_cli(calls: list):
    """Dispatch on model: the Haiku pre-rank returns the fit encoded in each posting's title;
    the Sonnet judge returns a uniform qualified verdict. Records the models called."""
    def fake(prompt, **k):
        calls.append(k.get("model"))
        blocks = re.findall(r"=== POSTING (\d+) ===\n([^\n]+)", prompt)
        if k.get("model") == matching.PRERANK_MODEL:
            scores = []
            for idx, title in blocks:
                mm = re.search(r"p(\d+)", title)
                scores.append({"index": int(idx), "fit": int(mm.group(1)) if mm else 0})
            return json.dumps({"scores": scores})
        verdicts = [{"index": int(idx), "qualified": True, "skills": 80, "experience": 80,
                     "seniority": 80, "why": "ok", "missing": []} for idx, _ in blocks]
        return json.dumps({"verdicts": verdicts})
    return fake


def test_prerank_picks_the_best_topn_for_the_full_judge(monkeypatch):
    calls: list = []
    monkeypatch.setattr(matching, "claude_code_available", lambda: True)
    monkeypatch.setattr(matching, "run_claude_cli", _fake_cli(calls))
    postings = [_posting(p) for p in (10, 90, 50, 95, 20)]
    ranked, errors = matching.match(BASE, postings, top_n=2, prerank_n=5, min_skills=0)
    assert errors == []
    # The Haiku pass ran, then the Sonnet judge — the two highest-preranked (p95, p90) got judged.
    assert matching.PRERANK_MODEL in calls and matching.JUDGE_MODEL in calls
    judged = [m for m in ranked if m.fit_score is not None]
    assert {m.posting.title for m in judged} == {"Engineer p95", "Engineer p90"}
    # Their coarse pre-rank score is recorded; the un-judged ones were still preranked.
    assert sorted(m.prerank_score for m in judged) == [90, 95]


def test_prerank_disabled_never_calls_the_cheap_model(monkeypatch):
    calls: list = []
    monkeypatch.setattr(matching, "claude_code_available", lambda: True)
    monkeypatch.setattr(matching, "run_claude_cli", _fake_cli(calls))
    postings = [_posting(p) for p in (10, 90, 50)]
    ranked, errors = matching.match(BASE, postings, top_n=3, prerank_n=0, min_skills=0)
    assert errors == []
    assert matching.PRERANK_MODEL not in calls  # single-stage — no Haiku pass
    assert all(m.prerank_score is None for m in ranked)
    assert len([m for m in ranked if m.fit_score is not None]) == 3


def test_prerank_failure_falls_back_to_keyword_order(monkeypatch):
    calls: list = []
    monkeypatch.setattr(matching, "claude_code_available", lambda: True)

    def cli(prompt, **k):
        calls.append(k.get("model"))
        if k.get("model") == matching.PRERANK_MODEL:
            raise RuntimeError("haiku unavailable")
        blocks = re.findall(r"=== POSTING (\d+) ===", prompt)
        return json.dumps({"verdicts": [{"index": int(i), "qualified": True, "skills": 70,
                                         "experience": 70, "seniority": 70, "why": "",
                                         "missing": []} for i in blocks]})
    monkeypatch.setattr(matching, "run_claude_cli", cli)
    postings = [_posting(p) for p in (10, 90, 50, 95)]
    ranked, errors = matching.match(BASE, postings, top_n=2, prerank_n=4, min_skills=0)
    # Pre-rank failure is recorded but never aborts — the full judge still ran on top_n.
    assert any("pre-rank failed" in e for e in errors)
    assert len([m for m in ranked if m.fit_score is not None]) == 2

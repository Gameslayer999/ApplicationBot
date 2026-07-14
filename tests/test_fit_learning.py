"""Discovery feedback loop (decision 046): learn which postings clear the bar and steer the
judge's scarce top_n slots toward postings like past winners. No Claude tokens — the judge is
stubbed and the predictor is arithmetic over stored verdicts.

Run:  python -m pytest tests/test_fit_learning.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

from applicationbot import fit_learning, matching
from applicationbot.discovery import Posting
from applicationbot.fit_learning import (MIN_HISTORY, Predictor, append, load, predictor,
                                          record_run, runs)
from applicationbot.matching import Match
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
BASE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))
ALL_SKILLS = " ".join(i for c in BASE.skills for i in c.items)


def _match(title: str, url: str, ats: str, fit: int, dims=None) -> Match:
    return Match(posting=Posting(title=title, company="Acme", body="", url=url, ats=ats),
                 keyword_score=1, matched_skills=["Python"], qualified=fit >= 50,
                 fit_score=fit, dimensions=dims or {}, judged_by="claude")


# --------------------------------------------------------------------------- store

def test_append_and_load_roundtrip(tmp_path):
    p = tmp_path / "fit_history.jsonl"
    n = append([_match("New Grad Engineer", "u1", "greenhouse", 60)], path=p)
    assert n == 1
    rows = load(path=p)
    assert len(rows) == 1
    r = rows[0]
    assert r["fit_score"] == 60 and r["ats"] == "greenhouse" and r["levels"] == ["new_grad"]


def test_keyword_only_matches_are_not_recorded(tmp_path):
    p = tmp_path / "fit_history.jsonl"
    kw_only = Match(posting=Posting(title="X", company="A", body="", url="u", ats="lever"),
                    keyword_score=3, matched_skills=[])  # fit_score stays None
    assert append([kw_only], path=p) == 0
    assert load(path=p) == []


def test_load_dedups_by_url_keeping_latest(tmp_path):
    p = tmp_path / "fit_history.jsonl"
    # Same URL judged twice across runs; the later timestamp wins.
    p.write_text(
        json.dumps({"url": "https://j/1", "ts": "2026-07-01T00:00:00", "ats": "greenhouse",
                    "title": "Engineer", "levels": ["_nolevel"], "fit_score": 20}) + "\n" +
        json.dumps({"url": "https://j/1", "ts": "2026-07-09T00:00:00", "ats": "greenhouse",
                    "title": "Engineer", "levels": ["_nolevel"], "fit_score": 70}) + "\n",
        encoding="utf-8")
    rows = load(path=p)
    assert len(rows) == 1 and rows[0]["fit_score"] == 70


def test_load_missing_file_and_garbage_lines(tmp_path):
    assert load(path=tmp_path / "nope.jsonl") == []
    p = tmp_path / "h.jsonl"
    p.write_text("not json\n" + json.dumps({"url": "u", "ts": "t", "fit_score": 50}) + "\n",
                 encoding="utf-8")
    assert len(load(path=p)) == 1


# --------------------------------------------------------------------------- run trend

def test_record_run_summarizes_judged_fits(tmp_path):
    p = tmp_path / "fit_runs.jsonl"
    ms = [_match("New Grad Engineer", "u1", "ashby", 60),
          _match("Senior Engineer", "u2", "greenhouse", 20),
          _match("SWE", "u3", "ashby", 45)]
    assert record_run(ms, min_fit=50, path=p) is True
    r = runs(path=p)
    assert len(r) == 1
    assert r[0]["best_fit"] == 60 and r[0]["n_judged"] == 3 and r[0]["cleared"] == 1
    assert r[0]["mean_fit"] == round((60 + 20 + 45) / 3, 1)


def test_record_run_skips_when_nothing_judged(tmp_path):
    p = tmp_path / "fit_runs.jsonl"
    kw_only = Match(posting=Posting(title="X", company="A", body="", url="u", ats="lever"),
                    keyword_score=3, matched_skills=[])
    assert record_run([kw_only], min_fit=50, path=p) is False
    assert runs(path=p) == []


def test_runs_are_ordered_and_limited(tmp_path):
    p = tmp_path / "fit_runs.jsonl"
    for fit in (30, 45, 60):  # three successive runs, improving
        record_run([_match("SWE", f"u{fit}", "ashby", fit)], min_fit=50, path=p)
    assert [r["best_fit"] for r in runs(path=p)] == [30, 45, 60]
    assert [r["best_fit"] for r in runs(path=p, limit=2)] == [45, 60]  # most recent


# --------------------------------------------------------------------------- predictor

def test_predictor_inactive_below_min_history():
    recs = [{"url": f"u{i}", "ts": "t", "ats": "greenhouse", "title": "E",
             "levels": ["senior"], "fit_score": 20} for i in range(MIN_HISTORY - 1)]
    assert Predictor(recs).active is False


def test_predictor_ranks_winning_level_above_losing_level():
    # History: new_grad roles clear the bar; senior roles don't.
    recs = ([{"url": f"n{i}", "ts": "t", "ats": "greenhouse", "title": "New Grad Engineer",
              "levels": ["new_grad"], "fit_score": 65} for i in range(4)] +
            [{"url": f"s{i}", "ts": "t", "ats": "greenhouse", "title": "Senior Engineer",
              "levels": ["senior"], "fit_score": 18} for i in range(4)])
    pr = Predictor(recs)
    assert pr.active
    ng = pr.predict(Posting(title="New Grad Engineer", company="", body="", url="", ats="greenhouse"))
    sr = pr.predict(Posting(title="Senior Engineer", company="", body="", url="", ats="greenhouse"))
    assert ng > sr


def test_predictor_learns_the_prescore_band(monkeypatch):
    # Hold level+board constant so ONLY the pre-score band differs. High-band postings judged
    # high, low-band judged low → a high-pre-score candidate must predict above a low one.
    recs = ([{"url": f"h{i}", "ts": "t", "ats": "greenhouse", "title": "Engineer",
              "levels": ["_nolevel"], "fit_score": 80, "ats_score": 85} for i in range(4)] +
            [{"url": f"l{i}", "ts": "t", "ats": "greenhouse", "title": "Engineer",
              "levels": ["_nolevel"], "fit_score": 20, "ats_score": 15} for i in range(4)])
    pr = Predictor(recs)
    p = Posting(title="Engineer", company="", body="", url="", ats="greenhouse")
    hi = pr.predict(p, ats_score=88)
    lo = pr.predict(p, ats_score=12)
    assert hi > lo


def test_prescore_is_ignored_when_history_lacks_it():
    # Pre-053 history (no ats_score) → the pre-score arg must not change the prediction, and
    # the estimate matches the old level+board-only average exactly.
    recs = [{"url": f"u{i}", "ts": "t", "ats": "greenhouse", "title": "Senior Engineer",
             "levels": ["senior"], "fit_score": 40} for i in range(6)]
    pr = Predictor(recs)
    p = Posting(title="Senior Engineer", company="", body="", url="", ats="greenhouse")
    assert pr.predict(p, ats_score=95) == pr.predict(p) == pr.predict(p, ats_score=5)


def test_prescore_calibration_bands_report_mean_actual_fit():
    from applicationbot.fit_learning import prescore_calibration
    recs = ([{"url": f"h{i}", "ts": "t", "ats": "greenhouse", "title": "E", "levels": ["_nolevel"],
              "fit_score": 78, "ats_score": 85} for i in range(3)] +
            [{"url": f"l{i}", "ts": "t", "ats": "greenhouse", "title": "E", "levels": ["_nolevel"],
              "fit_score": 22, "ats_score": 12} for i in range(2)] +
            [{"url": "old", "ts": "t", "ats": "greenhouse", "title": "E", "levels": ["_nolevel"],
              "fit_score": 50}])  # pre-053 row, no ats_score → ignored
    bands = prescore_calibration(recs)
    assert [b["band"] for b in bands] == ["0-19", "80-100"]  # ascending, labelled
    assert bands[0]["n"] == 2 and bands[0]["mean_fit"] == 22.0
    assert bands[1]["n"] == 3 and bands[1]["mean_fit"] == 78.0


def test_prescore_insight_reads_calibration_direction():
    from applicationbot.fit_learning import prescore_insight
    good = ([{"url": f"h{i}", "ats": "g", "title": "E", "levels": ["_nolevel"],
              "fit_score": 80, "ats_score": 85} for i in range(3)] +
            [{"url": f"l{i}", "ats": "g", "title": "E", "levels": ["_nolevel"],
              "fit_score": 20, "ats_score": 15} for i in range(3)])
    assert "ordering your judge queue well" in prescore_insight(good)["note"]
    inverted = ([{"url": f"h{i}", "ats": "g", "title": "E", "levels": ["_nolevel"],
                  "fit_score": 20, "ats_score": 85} for i in range(3)] +
                [{"url": f"l{i}", "ats": "g", "title": "E", "levels": ["_nolevel"],
                  "fit_score": 70, "ats_score": 15} for i in range(3)])
    assert "down-weights" in prescore_insight(inverted)["note"]
    assert prescore_insight([])["bands"] == [] and prescore_insight([])["note"] == ""


def test_record_carries_ats_score():
    from applicationbot.fit_learning import _record

    class _M:
        def __init__(self):
            self.posting = Posting(title="E", company="C", body="", url="u", ats="greenhouse")
            self.fit_score = 55
            self.ats_score = 72
            self.dimensions = {}
            self.matched_skills = []
            self.missing = []
    assert _record(_M())["ats_score"] == 72


def test_shrinkage_pulls_a_single_sample_toward_global_mean():
    # One extreme sample must not fully swing its bucket — shrunk toward the global mean.
    recs = ([{"url": f"m{i}", "ts": "t", "ats": "a", "title": "E", "levels": ["mid"],
              "fit_score": 40} for i in range(6)] +
            [{"url": "one", "ts": "t", "ats": "b", "title": "E", "levels": ["mid"],
              "fit_score": 100}])
    pr = Predictor(recs)
    # ats "b" seen once at 100 → estimate stays well below 100 (pulled toward ~48 global).
    est = pr._shrunk(pr._ats.get("b"))
    assert est < 90


# --------------------------------------------------------------------------- integration

def _seed_newgrad_wins(path: Path) -> None:
    rows = ([{"url": f"n{i}", "ts": "t", "ats": "ashby", "title": "New Grad Engineer",
              "levels": ["new_grad"], "fit_score": 62} for i in range(4)] +
            [{"url": f"s{i}", "ts": "t", "ats": "greenhouse", "title": "Senior Engineer",
              "levels": ["senior"], "fit_score": 18} for i in range(4)])
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_predictor_steers_which_posting_the_judge_scores(tmp_path, monkeypatch):
    # A senior posting stuffed with every résumé skill out-keywords a bare new-grad posting
    # (kw 24 vs ~1), so WITHOUT steering the judge's single slot goes to the senior role.
    senior = Posting(title="Senior Engineer", company="Big", body=ALL_SKILLS,
                     url="https://j/senior", ats="greenhouse")
    newgrad = Posting(title="New Grad Engineer", company="Sm", body="Python",
                      url="https://j/newgrad", ats="ashby")

    judged_titles: list[str] = []

    def fake_cli(prompt, **kwargs):
        # Record which posting(s) the judge was asked about, return a generic verdict.
        for t in ("Senior Engineer", "New Grad Engineer"):
            if t in prompt:
                judged_titles.append(t)
        return json.dumps({"verdicts": [{"index": 0, "qualified": True, "skills": 60,
                                         "experience": 60, "seniority": 60, "why": "",
                                         "missing": []}]})

    monkeypatch.setattr(matching, "claude_code_available", lambda: True)
    monkeypatch.setattr(matching, "run_claude_cli", fake_cli)

    hist = tmp_path / "fit_history.jsonl"
    _seed_newgrad_wins(hist)
    pr = predictor(path=hist)
    assert pr.active

    # top_n=1: only ONE posting gets judged. Steering must pick the new-grad one despite its
    # far lower keyword score.
    matching.match(BASE, [senior, newgrad], top_n=1, use_claude=True, predictor=pr)
    assert judged_titles == ["New Grad Engineer"]


def test_no_predictor_keeps_keyword_ordering(tmp_path, monkeypatch):
    senior = Posting(title="Senior Engineer", company="Big", body=ALL_SKILLS,
                     url="https://j/senior", ats="greenhouse")
    newgrad = Posting(title="New Grad Engineer", company="Sm", body="Python",
                      url="https://j/newgrad", ats="ashby")
    judged_titles: list[str] = []

    def fake_cli(prompt, **kwargs):
        for t in ("Senior Engineer", "New Grad Engineer"):
            if t in prompt:
                judged_titles.append(t)
        return json.dumps({"verdicts": [{"index": 0, "qualified": True, "skills": 60,
                                         "experience": 60, "seniority": 60, "why": "",
                                         "missing": []}]})

    monkeypatch.setattr(matching, "claude_code_available", lambda: True)
    monkeypatch.setattr(matching, "run_claude_cli", fake_cli)
    # No predictor → keyword rank wins → the skill-stuffed senior posting is judged.
    matching.match(BASE, [senior, newgrad], top_n=1, use_claude=True, predictor=None)
    assert judged_titles == ["Senior Engineer"]

"""Discovery diagnosis (decision 046): analyze the judged-fit history and recommend concrete,
auditable filter edits. No network, no Claude.

Run:  python -m pytest tests/test_fit_analysis.py -q
"""
from __future__ import annotations

from applicationbot.fit_learning import analyze


def _rec(level, board, fit, dims=None, missing=None):
    return {"url": f"u{level}{board}{fit}", "ts": "t", "ats": board, "title": "E",
            "levels": [level], "fit_score": fit, "dimensions": dims or {},
            "missing": missing or []}


def test_empty_history_is_a_clean_no_op():
    a = analyze([], min_fit=50, current_levels=[])
    assert a.n_judged == 0 and a.recommendations == [] and a.lines()


def test_dimension_means_and_weakest():
    recs = [_rec("new_grad", "ashby", 40, {"skills": 60, "experience": 20, "seniority": 50})
            for _ in range(5)]
    a = analyze(recs, min_fit=50, current_levels=["new_grad"])
    assert a.weakest_dim == "experience"
    assert round(a.dim_means["experience"]) == 20


def test_recommends_narrowing_levels_to_the_winners():
    recs = ([_rec("new_grad", "ashby", 62) for _ in range(3)] +
            [_rec("senior", "greenhouse", 18) for _ in range(3)])
    a = analyze(recs, min_fit=55, current_levels=["new_grad", "senior"])
    lev = [r for r in a.recommendations if r.kind == "experience_levels"]
    assert lev and lev[0].field == "experience_levels" and lev[0].value == ["new_grad"]


def test_no_level_rec_when_current_already_matches_winners():
    recs = ([_rec("new_grad", "ashby", 62) for _ in range(3)] +
            [_rec("senior", "greenhouse", 18) for _ in range(3)])
    a = analyze(recs, min_fit=55, current_levels=["new_grad"])
    assert not [r for r in a.recommendations if r.kind == "experience_levels"]


def test_min_fit_reality_check_only_when_nothing_cleared():
    dead = [_rec("new_grad", "ashby", 30) for _ in range(5)]
    a = analyze(dead, min_fit=60, current_levels=["new_grad"])
    mf = [r for r in a.recommendations if r.kind == "min_fit"]
    assert mf and mf[0].value == 30  # best achievable
    # Once something clears, the min_fit nudge disappears.
    alive = dead + [_rec("new_grad", "ashby", 70)]
    a2 = analyze(alive, min_fit=60, current_levels=["new_grad"])
    assert not [r for r in a2.recommendations if r.kind == "min_fit"]


def test_flags_a_chronically_dead_board():
    recs = ([_rec("_nolevel", "stripe", 20) for _ in range(4)] +
            [_rec("_nolevel", "ashby", 65) for _ in range(2)])
    a = analyze(recs, min_fit=60, current_levels=[])
    board = [r for r in a.recommendations if r.kind == "board"]
    assert board and "stripe" in board[0].message


def test_recurring_missing_becomes_resume_gap_notes():
    recs = [_rec("_nolevel", "ashby", 30, missing=["Kubernetes", "Go"]) for _ in range(5)]
    a = analyze(recs, min_fit=60, current_levels=[])
    gaps = [r for r in a.recommendations if r.kind == "resume_gap"]
    assert gaps and any("kubernetes" in g.message.lower() for g in gaps)
    # A one-off missing requirement (count < 2) is not surfaced as a gap.
    recs2 = [_rec("_nolevel", "ashby", 30, missing=["Rust"])]
    recs2 += [_rec("new_grad", "ashby", 30) for _ in range(4)]
    a2 = analyze(recs2, min_fit=60, current_levels=[])
    assert not any("rust" in g.message.lower()
                   for g in a2.recommendations if g.kind == "resume_gap")

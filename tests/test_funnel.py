"""Discovery→offer funnel (AutoApply-AI survey #4). Pure SQLite, no browser/Claude.

Run:  python -m pytest tests/test_funnel.py -q
"""
from __future__ import annotations

from applicationbot import tracker


def _seed(db, statuses):
    for i, s in enumerate(statuses):
        tracker.add_application({"company": f"C{i}", "status": s, "source_url": f"u{i}"}, path=db)


def _by_stage(db):
    return {r["stage"]: r for r in tracker.funnel_report(path=db)}


def test_empty_funnel_is_all_zero(tmp_path):
    rep = tracker.funnel_report(path=tmp_path / "e.db")
    assert [r["count"] for r in rep] == [0, 0, 0, 0, 0, 0]
    assert rep[0]["conversion_from_prev"] is None
    # A zero previous stage yields 0.0 conversion, never a divide-by-zero.
    assert rep[1]["conversion_from_prev"] == 0.0


def test_funnel_is_monotone_and_counts_reached_stage(tmp_path):
    db = tmp_path / "f.db"
    # 2 discovered-only, 1 dry-run, 1 blocked, 1 applied, 1 no-response, 1 rejected,
    # 1 interview, 1 offer  → 9 rows total.
    _seed(db, ["discovered", "discovered", "dry-run", "blocked", "applied",
               "no-response", "rejected", "interview", "offer"])
    s = _by_stage(db)
    assert s["Discovered"]["count"] == 9
    assert s["Filled"]["count"] == 7        # everything except the 2 discovered-only
    assert s["Applied"]["count"] == 5       # applied, no-response, rejected, interview, offer
    assert s["Responded"]["count"] == 3     # rejected, interview, offer (no-response excluded)
    assert s["Interview"]["count"] == 2     # interview, offer
    assert s["Offer"]["count"] == 1
    # Monotone non-increasing down the funnel.
    counts = [r["count"] for r in tracker.funnel_report(path=db)]
    assert counts == sorted(counts, reverse=True)


def test_conversion_rates(tmp_path):
    db = tmp_path / "c.db"
    _seed(db, ["applied", "applied", "interview", "offer"])  # all submitted
    s = _by_stage(db)
    assert s["Applied"]["count"] == 4 and s["Responded"]["count"] == 2
    assert s["Responded"]["conversion_from_prev"] == 0.5   # 2 of 4 applied got a reply
    assert s["Offer"]["conversion_from_prev"] == 0.5       # 1 of 2 interviews → offer


def test_rejected_is_a_response_but_not_an_interview(tmp_path):
    db = tmp_path / "r.db"
    _seed(db, ["rejected"])
    s = _by_stage(db)
    assert s["Responded"]["count"] == 1 and s["Interview"]["count"] == 0

"""Outcome calibration groundwork (decision 043): lifecycle statuses, the fit_score
column (incl. migration of a pre-043 DB), and the response-rate-by-fit-band report.

Run:  python -m pytest tests/test_calibration.py -q
"""
from __future__ import annotations

import sqlite3

from applicationbot import tracker


def _db(tmp_path):
    return tmp_path / "applications.db"


def _add(db, fit, status):
    return tracker.add_application(
        {"company": "Acme", "role": "SWE", "status": status, "fit_score": fit,
         "source_url": f"https://x.example/{fit}-{status}"}, path=db)


def test_new_lifecycle_statuses_are_valid(tmp_path):
    db = _db(tmp_path)
    for s in ("interview", "offer", "rejected", "no-response"):
        assert tracker.add_application({"company": "A", "role": "B", "status": s}, path=db)


def test_pre_043_db_gains_fit_score_column(tmp_path):
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:  # the schema as it was before decision 043
        conn.execute("""CREATE TABLE applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '', remote TEXT NOT NULL DEFAULT '',
            pay TEXT NOT NULL DEFAULT '', portal TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
            date_discovered TEXT NOT NULL DEFAULT '', date_applied TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'discovered', resume_path TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO applications (company, created_at, updated_at) "
                     "VALUES ('Old', 'x', 'x')")
    app_id = _add(db, 80, "applied")  # _connect migrates, then the insert lands
    assert tracker.get_application(app_id, path=db)["fit_score"] == "80"
    old = tracker.list_applications(search="Old", path=db)[0]
    assert old["fit_score"] == ""


def test_calibration_report_bands_and_rates(tmp_path):
    db = _db(tmp_path)
    _add(db, 82, "interview")   # 75-100: 1 positive
    _add(db, 90, "applied")     # 75-100: pending
    _add(db, 65, "rejected")    # 60-74: negative
    _add(db, 62, "responded")   # 60-74: positive
    _add(db, 40, "no-response")  # <60: negative
    _add(db, 55, "dry-run")     # never submitted — excluded entirely
    _add(db, "", "applied")     # submitted but unscored
    rep = tracker.calibration_report(path=db)
    top, mid, low = rep["bands"]
    assert (top["applications"], top["positive"], top["pending"]) == (2, 1, 1)
    assert top["response_rate"] == 1.0
    assert (mid["positive"], mid["negative"], mid["response_rate"]) == (1, 1, 0.5)
    assert (low["negative"], low["response_rate"]) == (1, 0.0)
    assert rep["unscored"] == 1
    # 4 resolved total (< 5): the only hint is "not enough outcomes yet".
    assert len(rep["hints"]) == 1 and "recorded outcome" in rep["hints"][0]


def test_dead_band_hint_suggests_raising_min_fit(tmp_path):
    db = _db(tmp_path)
    for i in range(5):
        tracker.add_application(
            {"company": "A", "role": "B", "status": "no-response", "fit_score": 61 + i,
             "source_url": f"https://x.example/dead{i}"}, path=db)
    _add(db, 80, "interview")
    rep = tracker.calibration_report(path=db)
    assert any("raising min_fit above 74" in h for h in rep["hints"])


def _dead_band(db, lo, n=5):
    for i in range(n):
        tracker.add_application(
            {"company": "A", "role": "B", "status": "no-response", "fit_score": lo + i,
             "source_url": f"https://x.example/dead{lo}-{i}"}, path=db)


def test_recommended_min_fit_raises_above_the_dead_band(tmp_path):
    db = _db(tmp_path)
    _dead_band(db, 61)
    rec = tracker.recommended_min_fit(50, path=db)
    assert rec is not None and rec[0] == 75 and "0 of 5" in rec[1]


def test_recommended_min_fit_never_lowers_and_needs_data(tmp_path):
    db = _db(tmp_path)
    assert tracker.recommended_min_fit(50, path=db) is None  # no outcomes at all
    _dead_band(db, 61, n=4)  # below the 5-resolved floor
    assert tracker.recommended_min_fit(50, path=db) is None
    _dead_band(db, 40)  # dead <60 band, but current is already above it
    assert tracker.recommended_min_fit(75, path=db) is None


def test_recommended_min_fit_ignores_band_with_any_positive(tmp_path):
    db = _db(tmp_path)
    _dead_band(db, 61)
    _add(db, 65, "interview")  # one response in the band — not dead
    assert tracker.recommended_min_fit(50, path=db) is None


def test_recommended_min_fit_never_recommends_past_the_top_band(tmp_path):
    db = _db(tmp_path)
    _dead_band(db, 80)  # a dead 75-100 band can't be raised past
    assert tracker.recommended_min_fit(50, path=db) is None


def test_effective_min_fit_wiring_and_kill_switch(monkeypatch):
    from applicationbot import pipeline
    from applicationbot.filters import DiscoveryFilters
    monkeypatch.setattr(tracker, "recommended_min_fit",
                        lambda current, **k: (75, "fit band 60-74: 0 of 6 resolved "
                                              "applications got any response"))
    f = DiscoveryFilters(min_fit=50)
    value, note = pipeline.effective_min_fit(f)
    assert value == 75 and "50→75" in note and "Discover settings" in note
    f_off = DiscoveryFilters(min_fit=50, calibrate_min_fit=False)
    assert pipeline.effective_min_fit(f_off) == (50, None)
    monkeypatch.setattr(tracker, "recommended_min_fit",
                        lambda current, **k: (_ for _ in ()).throw(RuntimeError("db locked")))
    assert pipeline.effective_min_fit(f) == (50, None)  # best-effort: never breaks a run


def test_follow_up_date_roundtrip_and_migration(tmp_path):
    db = _db(tmp_path)
    app_id = tracker.add_application(
        {"company": "Acme", "role": "SWE", "status": "applied",
         "follow_up_date": "2026-07-20"}, path=db)
    assert tracker.get_application(app_id, path=db)["follow_up_date"] == "2026-07-20"
    tracker.update_application(app_id, {"follow_up_date": "2026-07-27"}, path=db)
    assert tracker.get_application(app_id, path=db)["follow_up_date"] == "2026-07-27"

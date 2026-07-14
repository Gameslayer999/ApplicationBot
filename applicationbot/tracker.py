"""Track stage — a local SQLite store of every application (decision 024).

The system of record for what the pipeline discovered, tailored, and (would have)
submitted. Stdlib `sqlite3`, zero dependencies, one `applications` table matching the
fields in NEXT_STEPS.md. The autonomous runner writes rows programmatically; the web
UI's Track tab reads and edits them. The DB is PII (application history) and is
git-ignored (`applications.db`); it never leaves the machine.

Run standalone:
    python -m applicationbot.tracker list
    python -m applicationbot.tracker add --company Acme --role "SWE" --status dry-run
    python -m applicationbot.tracker counts
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "applications.db"

# Lifecycle of an application, in order. `dry-run` = filled + recorded but not submitted
# (the safety-switch default, Guideline #3); `applied` = actually submitted once armed.
# Post-application outcomes (decision 043): `responded` = any non-rejection reply,
# `interview`/`offer` = reached that stage, `rejected` = explicit no, `no-response` =
# closed out after silence. These feed the calibration report below.
# `blocked` = an armed run that filled the form but withheld the submit because something
# the user must resolve stood in the way (a required question with no answer, a login wall,
# a CAPTCHA). It carries `blocked_kind`/`blocked_detail` and is surfaced as a parked
# application the user can resolve then resume (see parking.py).
STATUSES = ["discovered", "tailored", "dry-run", "blocked", "applied", "responded",
            "interview", "offer", "rejected", "no-response", "failed"]

# Columns a caller may set/edit. `id`, `created_at`, `updated_at` are managed here.
# `fit_score` is the judge's 0-100 verdict stamped at apply time (decision 043) — the
# calibration report correlates it with outcomes. `follow_up_date` is a user-set ISO date
# for chasing a silent application.
EDITABLE = [
    "company", "role", "location", "remote", "pay", "portal", "method",
    "source_url", "date_discovered", "date_applied", "status", "resume_path", "notes",
    "fit_score", "follow_up_date", "blocked_kind", "blocked_detail",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company         TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    remote          TEXT NOT NULL DEFAULT '',      -- remote / on-site / hybrid
    pay             TEXT NOT NULL DEFAULT '',       -- free-form pay rate
    portal          TEXT NOT NULL DEFAULT '',       -- greenhouse / lever / ashby / …
    method          TEXT NOT NULL DEFAULT '',       -- auto / dry-run / manual
    source_url      TEXT NOT NULL DEFAULT '',
    date_discovered TEXT NOT NULL DEFAULT '',       -- ISO date
    date_applied    TEXT NOT NULL DEFAULT '',       -- ISO date, blank until applied
    status          TEXT NOT NULL DEFAULT 'discovered',
    resume_path     TEXT NOT NULL DEFAULT '',       -- tailored résumé used (file path)
    notes           TEXT NOT NULL DEFAULT '',
    fit_score       TEXT NOT NULL DEFAULT '',       -- judge's 0-100 fit at apply time
    follow_up_date  TEXT NOT NULL DEFAULT '',       -- ISO date to chase a silent application
    blocked_kind    TEXT NOT NULL DEFAULT '',       -- parking.py kind if parked (needs_answer / login / …)
    blocked_detail  TEXT NOT NULL DEFAULT '',       -- specifics for the Resolve card (field names, error)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # concurrent runner-writes + UI-reads
    conn.executescript(_SCHEMA)
    # Migration for DBs created before decision 043 (CREATE IF NOT EXISTS won't add columns).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
    for missing in ("fit_score", "follow_up_date", "blocked_kind", "blocked_detail"):
        if missing not in cols:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {missing} TEXT NOT NULL DEFAULT ''")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _validate_status(status: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; must be one of {', '.join(STATUSES)}")
    return status


def add_application(data: dict[str, Any], *, path: str | Path = DEFAULT_DB) -> int:
    """Insert one application; returns its new id. Unknown keys are ignored.

    Defaults: status='discovered', date_discovered=today. If status is 'applied' and no
    date_applied is given, it is set to today.
    """
    row = {k: ("" if data.get(k) is None else str(data.get(k))) for k in EDITABLE}
    row["status"] = _validate_status(data.get("status") or "discovered")
    if not row["date_discovered"]:
        row["date_discovered"] = date.today().isoformat()
    if row["status"] == "applied" and not row["date_applied"]:
        row["date_applied"] = date.today().isoformat()
    now = _now()
    cols = EDITABLE + ["created_at", "updated_at"]
    vals = [row[k] for k in EDITABLE] + [now, now]
    placeholders = ", ".join("?" for _ in cols)
    with _connect(path) as conn:
        cur = conn.execute(
            f"INSERT INTO applications ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        return int(cur.lastrowid)


def update_application(app_id: int, changes: dict[str, Any], *, path: str | Path = DEFAULT_DB) -> bool:
    """Update editable columns of one application. Returns True if a row was changed.

    Whitelists columns to EDITABLE. If status flips to 'applied' and date_applied is
    still blank, stamps it with today.
    """
    fields = {k: ("" if v is None else str(v)) for k, v in changes.items() if k in EDITABLE}
    if "status" in fields:
        _validate_status(fields["status"])
    if not fields:
        return False
    if fields.get("status") == "applied" and not fields.get("date_applied"):
        with _connect(path) as conn:
            cur = conn.execute("SELECT date_applied FROM applications WHERE id=?", (app_id,))
            existing = cur.fetchone()
        if existing is not None and not existing["date_applied"]:
            fields["date_applied"] = date.today().isoformat()
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [app_id]
    with _connect(path) as conn:
        cur = conn.execute(f"UPDATE applications SET {assignments} WHERE id=?", vals)
        return cur.rowcount > 0


def delete_application(app_id: int, *, path: str | Path = DEFAULT_DB) -> bool:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT resume_path FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        cur = conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
        deleted = cur.rowcount > 0
    # Cascade: remove the tailored PDF this row owned (decision 029) — but only if it's
    # a file we manage under profile/tailored/, never a user-supplied path.
    if deleted and row and row["resume_path"]:
        from . import resume_store
        resume_store.delete_if_managed(row["resume_path"])
    return deleted


def get_application(app_id: int, *, path: str | Path = DEFAULT_DB) -> Optional[dict[str, Any]]:
    with _connect(path) as conn:
        cur = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def find_by_source_url(url: str, *, path: str | Path = DEFAULT_DB) -> Optional[dict[str, Any]]:
    """The most recent application with this exact source URL, or None. Used by the Apply
    stage to upsert a posting's row instead of duplicating it on re-runs."""
    if not url:
        return None
    with _connect(path) as conn:
        cur = conn.execute(
            "SELECT * FROM applications WHERE source_url=? ORDER BY id DESC LIMIT 1", (url,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def seen_source_urls(*, statuses: Optional[list[str]] = None, path: str | Path = DEFAULT_DB) -> set[str]:
    """Every non-empty source URL already in the tracker (optionally limited to `statuses`).
    The Discover stage uses this to skip postings it has already processed/applied to, so
    re-runs don't keep surfacing and re-applying to the same roles."""
    where = ["source_url != ''"]
    params: list[Any] = []
    if statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        params += list(statuses)
    with _connect(path) as conn:
        cur = conn.execute(
            "SELECT DISTINCT source_url FROM applications WHERE " + " AND ".join(where), params
        )
        return {r["source_url"] for r in cur.fetchall()}


# Statuses at which a parked block is still worth surfacing to the user. Once a row reaches
# `applied` (submitted) or a post-application outcome, its block is moot and it drops out.
_PARKED_OPEN = {"discovered", "tailored", "dry-run", "blocked"}


def parked_applications(*, path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """Applications parked on a user-resolvable block (parking.py) that are still open —
    newest first. These feed the UI's "Resolve" cards: each carries `blocked_kind` /
    `blocked_detail` describing what to fix before re-running Apply on the posting."""
    return [r for r in list_applications(path=path)
            if r.get("blocked_kind") and r["status"] in _PARKED_OPEN]


def list_applications(
    *, status: Optional[str] = None, search: Optional[str] = None, path: str | Path = DEFAULT_DB
) -> list[dict[str, Any]]:
    """All applications, newest first. Optional filter by status and free-text search
    across company / role / location / notes."""
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(_validate_status(status))
    if search:
        like = f"%{search}%"
        where.append("(company LIKE ? OR role LIKE ? OR location LIKE ? OR notes LIKE ?)")
        params += [like, like, like, like]
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with _connect(path) as conn:
        cur = conn.execute(
            f"SELECT * FROM applications{clause} ORDER BY id DESC", params
        )
        return [dict(r) for r in cur.fetchall()]


def status_counts(*, path: str | Path = DEFAULT_DB) -> dict[str, int]:
    """Count of applications per status (every status present, 0 if none) + 'total'."""
    counts = {s: 0 for s in STATUSES}
    with _connect(path) as conn:
        cur = conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
        for r in cur.fetchall():
            counts[r["status"]] = counts.get(r["status"], 0) + r["n"]
    counts["total"] = sum(counts[s] for s in STATUSES)
    return counts


# The application journey as a shrinking funnel (AutoApply-AI survey #4). Each stage's set is a
# SUPERSET of the next, so counting rows whose current status falls in each set yields a monotone
# funnel — "reached this stage or beyond" — even though a row stores only its latest status. A
# rejection still counts as having *responded* (a human replied); `no-response` counts as applied
# but not responded. `discovered`/`tailored`/`failed` sit before the form and only feed the top.
_SUBMITTED = {"applied", "responded", "interview", "offer", "rejected", "no-response"}
_FUNNEL_STAGES = [
    ("Discovered", set(STATUSES)),                              # everything in the tracker
    ("Filled", _SUBMITTED | {"dry-run", "blocked"}),           # reached + filled the form
    ("Applied", _SUBMITTED),                                    # actually submitted (armed)
    ("Responded", {"responded", "interview", "offer", "rejected"}),  # a human replied (incl. a no)
    ("Interview", {"interview", "offer"}),
    ("Offer", {"offer"}),
]


def funnel_report(*, path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """The discovery→offer funnel: for each stage, how many applications reached it and the
    conversion from the previous stage. Drives the Track-tab funnel view (survey #4)."""
    counts = status_counts(path=path)
    out: list[dict[str, Any]] = []
    prev: Optional[int] = None
    for label, statuses in _FUNNEL_STAGES:
        n = sum(counts.get(s, 0) for s in statuses)
        conv = None if prev is None else (n / prev if prev else 0.0)
        out.append({"stage": label, "count": n, "conversion_from_prev": conv})
        prev = n
    return out


# --------------------------------------------------- outcome calibration (decision 043)

# Fit bands the calibration report groups by — aligned with the judge's 0-100 scale.
FIT_BANDS = [(75, 100, "75-100"), (60, 74, "60-74"), (0, 59, "<60")]
# Outcome classification: a positive is any signal a human read the application and
# engaged; resolved = positive or closed-negative. `applied` rows are still pending.
POSITIVE = {"responded", "interview", "offer"}
CLOSED_NEGATIVE = {"rejected", "no-response"}
_MIN_RESOLVED_FOR_HINT = 5  # don't suggest tuning min_fit off tiny samples


def calibration_report(*, path: str | Path = DEFAULT_DB) -> dict:
    """Response rate by fit band, from real submissions with a recorded fit score —
    ground truth for tuning `min_fit` (adapted from ai-job-search's /outcome→/setup
    calibration loop). Returns {"bands": [...], "hints": [...], "unscored": n}."""
    submitted = {"applied"} | POSITIVE | CLOSED_NEGATIVE
    rows = [r for r in list_applications(path=path) if r["status"] in submitted]
    unscored = 0
    bands = [{"band": label, "lo": lo, "hi": hi, "applications": 0,
              "pending": 0, "positive": 0, "negative": 0} for lo, hi, label in FIT_BANDS]
    for r in rows:
        try:
            fit = int(str(r.get("fit_score", "")).strip())
        except ValueError:
            unscored += 1
            continue
        for b in bands:
            if b["lo"] <= fit <= b["hi"]:
                b["applications"] += 1
                if r["status"] in POSITIVE:
                    b["positive"] += 1
                elif r["status"] in CLOSED_NEGATIVE:
                    b["negative"] += 1
                else:
                    b["pending"] += 1
                break
    hints = []
    for b in bands:
        resolved = b["positive"] + b["negative"]
        b["resolved"] = resolved
        b["response_rate"] = (b["positive"] / resolved) if resolved else None
        if resolved >= _MIN_RESOLVED_FOR_HINT and b["positive"] == 0:
            hints.append(
                f"Fit band {b['band']}: 0 of {resolved} resolved applications got any "
                f"response — consider raising min_fit above {b['hi']} in the Discover "
                "settings.")
    total_resolved = sum(b["resolved"] for b in bands)
    if total_resolved < _MIN_RESOLVED_FOR_HINT:
        hints.append(
            f"Only {total_resolved} application(s) have a recorded outcome — set "
            "interview/offer/rejected/no-response on the Track tab as replies arrive; "
            f"calibration needs at least {_MIN_RESOLVED_FOR_HINT}.")
    return {"bands": bands, "hints": hints, "unscored": unscored}


def recommended_min_fit(current: int, *, path: str | Path = DEFAULT_DB) -> Optional[tuple[int, str]]:
    """A higher `min_fit` justified by outcomes, or None to keep the configured value.

    A band is *dead* when ≥ _MIN_RESOLVED_FOR_HINT of its applications resolved and NONE
    got any response — applying into it is spending submissions (and tailoring tokens) on
    silence. The recommendation is one above the highest dead band. It only ever RAISES:
    lowering (or acting on thin/positive data) stays a human call. The top band is never
    recommended past — if 75-100 is dead the strategy is failing, which no threshold fixes
    (the calibration report already says so).
    """
    rep = calibration_report(path=path)
    dead = [b for b in rep["bands"]
            if b["hi"] < 100 and b["resolved"] >= _MIN_RESOLVED_FOR_HINT and b["positive"] == 0]
    if not dead:
        return None
    worst = max(dead, key=lambda b: b["hi"])
    if worst["hi"] + 1 <= current:
        return None
    reason = (f"fit band {worst['band']}: 0 of {worst['resolved']} resolved "
              f"applications got any response")
    return worst["hi"] + 1, reason


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="ApplicationBot tracking store (SQLite).")
    p.add_argument("--db", default=str(DEFAULT_DB), help="path to the SQLite DB")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add an application")
    for f in EDITABLE:
        a.add_argument(f"--{f.replace('_', '-')}", default=None)

    sub.add_parser("list", help="list applications")
    sub.add_parser("counts", help="status counts")
    sub.add_parser("funnel", help="discovery→offer conversion funnel")
    sub.add_parser("calibration", help="response rate by fit band (tune min_fit from outcomes)")

    d = sub.add_parser("delete", help="delete an application by id")
    d.add_argument("id", type=int)

    args = p.parse_args(argv)
    db = args.db

    if args.cmd == "add":
        data = {f: getattr(args, f) for f in EDITABLE}
        app_id = add_application(data, path=db)
        print(f"added application id={app_id}")
    elif args.cmd == "list":
        rows = list_applications(path=db)
        if not rows:
            print("(no applications yet)")
        for r in rows:
            print(f"[{r['id']:>3}] {r['status']:<10} {r['company']} — {r['role']} "
                  f"({r['portal'] or '—'}) {r['date_applied'] or r['date_discovered']}")
    elif args.cmd == "counts":
        for k, v in status_counts(path=db).items():
            print(f"{k:<12} {v}")
    elif args.cmd == "funnel":
        for s in funnel_report(path=db):
            conv = f"  ({s['conversion_from_prev']:.0%} of prev)" if s["conversion_from_prev"] is not None else ""
            print(f"{s['stage']:<12} {s['count']:>4}{conv}")
    elif args.cmd == "calibration":
        rep = calibration_report(path=db)
        print(f"{'fit band':<10} {'applied':>8} {'pending':>8} {'positive':>9} "
              f"{'negative':>9} {'response':>9}")
        for b in rep["bands"]:
            rate = f"{b['response_rate']:.0%}" if b["response_rate"] is not None else "—"
            print(f"{b['band']:<10} {b['applications']:>8} {b['pending']:>8} "
                  f"{b['positive']:>9} {b['negative']:>9} {rate:>9}")
        if rep["unscored"]:
            print(f"(+{rep['unscored']} submitted application(s) with no recorded fit score)")
        for h in rep["hints"]:
            print(f"→ {h}")
        try:  # best-effort: filters may not exist on a fresh clone
            from .filters import load_filters
            f = load_filters()
            rec = recommended_min_fit(f.min_fit, path=db)
            if rec:
                print(f"→ Recommended min_fit: {rec[0]} (configured {f.min_fit}; {rec[1]}) — "
                      + ("applied automatically on pipeline/runner runs."
                         if f.calibrate_min_fit else
                         "NOT applied: auto-calibration is off in your Discovery settings."))
        except Exception:
            pass
    elif args.cmd == "delete":
        print("deleted" if delete_application(args.id, path=db) else "not found")
    return 0


if __name__ == "__main__":
    sys.exit(main())

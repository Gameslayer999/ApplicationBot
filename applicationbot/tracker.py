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

from .paths import DATA_ROOT
DEFAULT_DB = DATA_ROOT / "applications.db"

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
    "source_url", "date_discovered", "date_dry_run", "date_applied", "status", "resume_path", "notes",
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
    date_dry_run    TEXT NOT NULL DEFAULT '',       -- ISO date the form was last dry-run filled, blank otherwise
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

-- Append-only log: one row per apply run (decision 084). The `applications` table holds one
-- row per posting (its current state); this holds the history of every dry-run / blocked /
-- submitted attempt against it, each with its own `ran_at`. The Track tab expands a posting to
-- show its runs. Never edited or deduped — a pure audit trail.
CREATE TABLE IF NOT EXISTS application_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  INTEGER NOT NULL,               -- the applications.id this run was against
    source_url      TEXT NOT NULL DEFAULT '',
    company         TEXT NOT NULL DEFAULT '',       -- denormalized so a run reads standalone
    role            TEXT NOT NULL DEFAULT '',
    portal          TEXT NOT NULL DEFAULT '',
    outcome         TEXT NOT NULL DEFAULT '',        -- dry-run / blocked / applied
    resume_path     TEXT NOT NULL DEFAULT '',        -- tailored résumé used for this run
    detail          TEXT NOT NULL DEFAULT '',        -- "N field(s) filled (…); M need attention" + any blocker
    ran_at          TEXT NOT NULL                    -- ISO datetime the run happened
);
CREATE INDEX IF NOT EXISTS idx_runs_app ON application_runs(application_id);

-- Append-only Claude token log: one row per Claude CLI call (decision 095). `posting_key` is
-- the source URL of the application the call was made for (joins to applications.source_url),
-- or '' for discovery-phase calls not tied to one posting (the batched fit judge, enrichment
-- of not-yet-selected postings). `activity` says WHAT Claude was doing (tailoring / form-entry
-- / judging / …). Never edited or deduped — a pure usage trail so the Track tab can show how
-- many tokens each application cost, split by activity, and the standalone discovery total.
CREATE TABLE IF NOT EXISTS usage_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_key            TEXT NOT NULL DEFAULT '',   -- applications.source_url, or '' for discovery
    activity               TEXT NOT NULL DEFAULT 'other',
    model                  TEXT NOT NULL DEFAULT '',
    input_tokens           INTEGER NOT NULL DEFAULT 0,
    output_tokens          INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd               REAL NOT NULL DEFAULT 0,
    ran_at                 TEXT NOT NULL               -- ISO datetime the call completed
);
CREATE INDEX IF NOT EXISTS idx_usage_posting ON usage_events(posting_key);
"""

# Columns a caller may set when recording a run. `id`/`ran_at` are managed here.
RUN_FIELDS = ["application_id", "source_url", "company", "role", "portal",
              "outcome", "resume_path", "detail"]


def _connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # concurrent runner-writes + UI-reads
    runs_existed = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_runs'"
    ).fetchone())
    conn.executescript(_SCHEMA)
    # Migration for DBs created before decision 043 (CREATE IF NOT EXISTS won't add columns).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
    for missing in ("fit_score", "follow_up_date", "blocked_kind", "blocked_detail", "date_dry_run"):
        if missing not in cols:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {missing} TEXT NOT NULL DEFAULT ''")
            cols.add(missing)
            if missing == "date_dry_run":
                # Backfill pre-existing dry-run rows from created_at (the row's insert time, i.e.
                # when the dry-run was recorded) so the Track tab shows a date, not a blank, for
                # runs made before this column existed. Date portion only, matching the column.
                conn.execute(
                    "UPDATE applications SET date_dry_run = substr(created_at, 1, 10) "
                    "WHERE status = 'dry-run' AND date_dry_run = ''"
                )
    if not runs_existed:
        # First time the run log exists: seed it with one run per pre-existing dry-run posting,
        # from created_at, so the history isn't empty for runs made before this table. Reference
        # only columns the applications table actually has (an old DB may predate some) — a
        # missing column contributes '' — so the seed never fails on a partially-migrated schema.
        src = lambda c: c if c in cols else "''"
        detail = (f"CASE WHEN {src('notes')} LIKE '[auto] %' THEN substr({src('notes')}, 8) "
                  f"ELSE {src('notes')} END") if "notes" in cols else "''"
        conn.execute(
            "INSERT INTO application_runs "
            "(application_id, source_url, company, role, portal, outcome, resume_path, detail, ran_at) "
            f"SELECT id, {src('source_url')}, {src('company')}, {src('role')}, {src('portal')}, "
            f"  'dry-run', {src('resume_path')}, {detail}, {src('created_at')} "
            "FROM applications WHERE status = 'dry-run'"
        )
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
    if row["status"] == "dry-run" and not row["date_dry_run"]:
        row["date_dry_run"] = date.today().isoformat()
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
    if fields.get("status") == "dry-run" and not fields.get("date_dry_run"):
        with _connect(path) as conn:
            cur = conn.execute("SELECT date_dry_run FROM applications WHERE id=?", (app_id,))
            existing = cur.fetchone()
        if existing is not None and not existing["date_dry_run"]:
            fields["date_dry_run"] = date.today().isoformat()
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [app_id]
    with _connect(path) as conn:
        cur = conn.execute(f"UPDATE applications SET {assignments} WHERE id=?", vals)
        return cur.rowcount > 0


def delete_application(app_id: int, *, path: str | Path = DEFAULT_DB) -> bool:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT resume_path, source_url FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        cur = conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
        deleted = cur.rowcount > 0
        if deleted:  # cascade: drop this posting's run history so no orphans linger
            conn.execute("DELETE FROM application_runs WHERE application_id=?", (app_id,))
            # Cascade the token usage keyed on this posting's URL (decision 095) so a deleted
            # application's tokens don't re-attach if the same URL is re-discovered later.
            if row and row["source_url"]:
                conn.execute("DELETE FROM usage_events WHERE posting_key=?", (row["source_url"],))
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


# ---------------------------------------------------------------- run history (decision 084)

def record_run(data: dict[str, Any], *, path: str | Path = DEFAULT_DB) -> int:
    """Append one apply run to the history log; returns its new id. Unknown keys ignored.
    `ran_at` is stamped here (ISO datetime). Callers pass at least `application_id`/`outcome`."""
    row = {k: ("" if data.get(k) is None else str(data.get(k))) for k in RUN_FIELDS}
    cols = RUN_FIELDS + ["ran_at"]
    vals = [row[k] for k in RUN_FIELDS] + [_now()]
    placeholders = ", ".join("?" for _ in cols)
    with _connect(path) as conn:
        cur = conn.execute(
            f"INSERT INTO application_runs ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        return int(cur.lastrowid)


def runs_for_application(app_id: int, *, path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """Every run recorded against one posting, newest first."""
    with _connect(path) as conn:
        cur = conn.execute(
            "SELECT * FROM application_runs WHERE application_id=? ORDER BY id DESC", (app_id,)
        )
        return [dict(r) for r in cur.fetchall()]


def run_counts(*, path: str | Path = DEFAULT_DB) -> dict[int, int]:
    """{application_id: number of runs} — lets the Track tab show a per-posting run count
    without loading every run up front (details are fetched lazily when a row is expanded)."""
    with _connect(path) as conn:
        cur = conn.execute(
            "SELECT application_id, COUNT(*) AS n FROM application_runs GROUP BY application_id"
        )
        return {int(r["application_id"]): int(r["n"]) for r in cur.fetchall()}


# ---------------------------------------------------------------- token usage (decision 095)

# Columns a caller may set when recording a Claude call. `id`/`ran_at` are managed here.
USAGE_FIELDS = ["posting_key", "activity", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_creation_tokens", "cost_usd"]
_USAGE_INT = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"}


def record_usage_event(data: dict[str, Any], *, path: str | Path = DEFAULT_DB) -> int:
    """Append one Claude call's token usage to the log; returns its new id (decision 095).
    `ran_at` is stamped here. Called (best-effort) by usage.record after every Claude CLI call."""
    row: dict[str, Any] = {}
    for k in USAGE_FIELDS:
        v = data.get(k)
        if k in _USAGE_INT:
            row[k] = int(v or 0)
        elif k == "cost_usd":
            row[k] = float(v or 0.0)
        else:
            row[k] = "" if v is None else str(v)
    cols = USAGE_FIELDS + ["ran_at"]
    vals = [row[k] for k in USAGE_FIELDS] + [_now()]
    placeholders = ", ".join("?" for _ in cols)
    with _connect(path) as conn:
        cur = conn.execute(
            f"INSERT INTO usage_events ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        return int(cur.lastrowid)


def _usage_zero() -> dict[str, Any]:
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cost_usd": 0.0, "calls": 0,
            "total_tokens": 0, "by_activity": {}}


def _usage_add(acc: dict[str, Any], r: sqlite3.Row) -> None:
    """Fold one usage_events row into an accumulator (with a per-activity sub-breakdown)."""
    for k in _USAGE_INT:
        acc[k] += r[k]
    acc["cost_usd"] += r["cost_usd"]
    acc["calls"] += 1
    acc["total_tokens"] += r["input_tokens"] + r["output_tokens"]
    act = acc["by_activity"].setdefault(
        r["activity"] or "other",
        {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0, "total_tokens": 0})
    act["input_tokens"] += r["input_tokens"]
    act["output_tokens"] += r["output_tokens"]
    act["cost_usd"] += r["cost_usd"]
    act["calls"] += 1
    act["total_tokens"] += r["input_tokens"] + r["output_tokens"]


def usage_by_application(*, path: str | Path = DEFAULT_DB) -> dict[str, dict[str, Any]]:
    """{source_url -> token totals} for every posting that spent Claude tokens (decision 095).
    Each total carries input/output/cache counts, cost, call count, `total_tokens`
    (input+output), and a `by_activity` sub-map (tailoring / form-entry / …). The Track tab
    joins each application row to this by its source_url. Discovery calls (posting_key '') are
    excluded here — see `usage_discovery_summary`."""
    out: dict[str, dict[str, Any]] = {}
    with _connect(path) as conn:
        for r in conn.execute("SELECT * FROM usage_events WHERE posting_key != ''"):
            acc = out.setdefault(r["posting_key"], _usage_zero())
            _usage_add(acc, r)
    return out


def usage_discovery_summary(*, path: str | Path = DEFAULT_DB) -> dict[str, Any]:
    """All-time token spend on discovery-phase Claude calls not tied to one application —
    the batched fit judge and any pre-selection enrichment (decision 095). One aggregate with a
    `by_activity` breakdown, shown on the Track tab separate from the per-application rows."""
    acc = _usage_zero()
    with _connect(path) as conn:
        for r in conn.execute("SELECT * FROM usage_events WHERE posting_key = ''"):
            _usage_add(acc, r)
    return acc


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

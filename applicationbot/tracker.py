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
STATUSES = ["discovered", "tailored", "dry-run", "applied", "failed", "responded"]

# Columns a caller may set/edit. `id`, `created_at`, `updated_at` are managed here.
EDITABLE = [
    "company", "role", "location", "remote", "pay", "portal", "method",
    "source_url", "date_discovered", "date_applied", "status", "resume_path", "notes",
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
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # concurrent runner-writes + UI-reads
    conn.executescript(_SCHEMA)
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
        cur = conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
        return cur.rowcount > 0


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


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="ApplicationBot tracking store (SQLite).")
    p.add_argument("--db", default=str(DEFAULT_DB), help="path to the SQLite DB")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add an application")
    for f in EDITABLE:
        a.add_argument(f"--{f.replace('_', '-')}", default=None)

    sub.add_parser("list", help="list applications")
    sub.add_parser("counts", help="status counts")

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
    elif args.cmd == "delete":
        print("deleted" if delete_application(args.id, path=db) else "not found")
    return 0


if __name__ == "__main__":
    sys.exit(main())

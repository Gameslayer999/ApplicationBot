#!/usr/bin/env python3
"""Move tailored résumé PDFs from purgeable temp files into the stable store (decision 029).

Older dry-runs wrote the tailored PDF to ``$TMPDIR/tailored_*.pdf``, which macOS purges —
so a Track row's ``resume_path`` would eventually dangle. This copies each such file into
``profile/tailored/`` (the managed, git-ignored store) and repoints the row at it.

Idempotent and safe to re-run:
  - rows already pointing inside the managed store are left untouched;
  - rows whose file is gone are reported, not failed on;
  - the destination is keyed on the posting URL, so re-running overwrites, never piles up.

    python scripts/migrate_tailored_pdfs.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path

from applicationbot import resume_store, tracker


def main() -> int:
    db = tracker.DEFAULT_DB
    if not Path(db).exists():
        print(f"No tracker DB at {db} — nothing to migrate.")
        return 0

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, company, role, source_url, resume_path FROM applications "
        "WHERE resume_path != ''"
    ).fetchall()
    con.close()

    migrated = already = missing = 0
    for r in rows:
        src = r["resume_path"]
        if resume_store.is_managed(src):
            already += 1
            continue
        if not Path(src).exists():
            print(f"  row {r['id']}: source PDF gone ({src}) — leaving path as-is")
            missing += 1
            continue
        dest = resume_store.write_pdf(
            Path(src).read_bytes(), r["company"] or "", r["role"] or "", r["source_url"] or ""
        )
        tracker.update_application(r["id"], {"resume_path": dest})
        print(f"  row {r['id']}: {Path(src).name} → {Path(dest).name}")
        migrated += 1

    print(f"\nMigrated {migrated}, already-managed {already}, missing {missing}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

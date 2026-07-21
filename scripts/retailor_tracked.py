#!/usr/bin/env python3
"""Re-tailor already-tracked postings with the CURRENT tailoring code, in place.

Why this exists: a dry run reuses a posting's stored tailored PDF while its stamp matches
(pipeline.run_testing_mode), and the normal loop only ever processes *new* postings
(only_new=True) — so after a tailoring-logic change (prompt or PDF layout, decisions 081/082)
an already-seen posting is never revisited and its résumé looks unchanged. This script
regenerates the tailored PDF for tracked rows whose job description is still in the discovery
cache, overwriting the stored PDF (same path the Track tab links to) and its stamp.

It can only re-tailor rows whose JD is still cached — the JD isn't persisted per PDF, so rows
tailored from a snapshot that has since rolled over are reported as skipped (not silently
ignored). Idempotent: with the same code + JD the stamp matches and the row is skipped unless
--force is given.

Usage:
    python -m scripts.retailor_tracked --company stripe        # just Stripe rows
    python -m scripts.retailor_tracked --id 22                 # one row
    python -m scripts.retailor_tracked --all                   # every re-tailorable row
    python -m scripts.retailor_tracked --all --backend rules   # deterministic, no Claude call
    python -m scripts.retailor_tracked --id 22 --force         # regen even if the stamp matches
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from applicationbot import pdf, pipeline, resume_store  # noqa: E402
from applicationbot.apply_profile import load_profile, resume_with_profile_links  # noqa: E402
from applicationbot.job_description import JobDescription  # noqa: E402
from applicationbot.resume import load_resume  # noqa: E402
from applicationbot.tailor import tailor_resume  # noqa: E402

DB = ROOT / "applications.db"
CACHE = ROOT / "profile" / "discovery_cache.json"


def _cached_jds() -> dict[str, dict]:
    """Map posting URL -> posting dict (with a non-empty JD body) from the discovery cache."""
    if not CACHE.is_file():
        return {}
    data = json.loads(CACHE.read_text())
    out = {}
    for m in data.get("matches", []):
        p = m.get("posting") or {}
        if p.get("url") and p.get("body"):
            out[p["url"]] = p
    return out


def _rows(where_id: int | None, company: str | None) -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    q = ("SELECT id, company, role, source_url, resume_path, status FROM applications "
         "WHERE status IN ('dry-run','tailored','blocked')")
    args: list = []
    if where_id is not None:
        q += " AND id = ?"; args.append(where_id)
    if company:
        q += " AND lower(company) LIKE ?"; args.append(f"%{company.lower()}%")
    rows = [dict(r) for r in conn.execute(q, args)]
    conn.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, help="Re-tailor one tracked row by id.")
    g.add_argument("--company", help="Re-tailor tracked rows whose company matches (substring).")
    g.add_argument("--all", action="store_true", help="Re-tailor every re-tailorable tracked row.")
    ap.add_argument("--backend", default="auto", help="Tailor backend (auto|claude-code|rules).")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate even if the stamp already matches the current code.")
    a = ap.parse_args()

    base = load_resume("profile/resume.yaml")
    try:
        profile = load_profile()
        base_for_pdf = resume_with_profile_links(base, profile)
    except Exception:
        profile, base_for_pdf = None, base

    jds = _cached_jds()
    rows = _rows(a.id, None if a.all else a.company)
    if not rows:
        print("No matching tracked rows.")
        return 1

    done = skipped = 0
    for r in rows:
        label = f"[{r['id']}] {r['company'] or '?'} - {r['role'] or '?'}"
        post = jds.get(r["source_url"])
        if not post:
            print(f"  SKIP {label}: JD no longer in discovery cache — can't re-tailor.")
            skipped += 1
            continue

        jd = JobDescription(body=post["body"],
                            meta={"company": post.get("company", ""),
                                  "role": post.get("title", ""), "url": post.get("url", "")})
        stamp = pipeline.tailor_stamp(base, profile, jd) if profile else None
        dest = resume_store.path_for(r["company"] or post.get("company", ""),
                                     r["role"] or post.get("title", ""), r["source_url"])
        if (not a.force and stamp and dest.is_file()
                and resume_store.read_stamp(dest) == stamp):
            print(f"  OK   {label}: already current (stamp matches) — use --force to regen anyway.")
            skipped += 1
            continue

        res = tailor_resume(base, jd, backend=a.backend)
        data = pdf.render_pdf(base_for_pdf, res.tailored)
        path = resume_store.write_pdf(data, r["company"] or post.get("company", ""),
                                      r["role"] or post.get("title", ""), r["source_url"])
        if stamp:
            resume_store.write_stamp(path, stamp)
        print(f"  DONE {label}: {len(data)} bytes, {pdf.page_count(base_for_pdf, res.tailored)} "
              f"page(s) via {res.backend} -> {Path(path).name}")
        done += 1

    print(f"\nRe-tailored {done}, skipped {skipped}.")
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())

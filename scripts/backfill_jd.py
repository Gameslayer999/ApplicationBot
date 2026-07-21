#!/usr/bin/env python3
"""Backfill the ``<pdf>.jd`` job-description sidecar for already-tracked rows.

Why this exists: the Track tab's "Re-tailor ▶" button only shows when a row has a JD
sidecar beside its tailored PDF (``resume_store.has_jd``, decision 086). Rows tailored
before that feature have no sidecar, so the button never draws for them and they can't
be re-tailored offline — the exact "I can't find where to re-run a dry run with
retailoring" symptom. This script writes the missing sidecar so the existing button
lights up. It does NOT tailor or submit anything.

Where the JD comes from, in order:
  1. the discovery cache (``profile/discovery_cache.json``) — no network, exact bytes; then
  2. a live re-fetch of the row's ``source_url`` via ``enrich.fetch_full_jd`` (json-ld → css
     → LLM cascade, same path discovery uses).
A row whose JD can't be obtained either way is reported as FAILED (posting expired/removed,
or nothing extractable) — never silently skipped.

Idempotent: a row that already has a sidecar is left untouched (the JD is fetched only when
one doesn't already exist). Safe to re-run — a second pass only touches rows still missing one.

Usage:
    python -m scripts.backfill_jd --all                # every tracked row missing a sidecar
    python -m scripts.backfill_jd --company stripe     # just Stripe rows
    python -m scripts.backfill_jd --id 6               # one row
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from applicationbot import enrich, resume_store  # noqa: E402
from applicationbot.job_description import JobDescription  # noqa: E402

DB = ROOT / "applications.db"
CACHE = ROOT / "profile" / "discovery_cache.json"


def _cached_bodies() -> dict[str, dict]:
    """Map posting URL -> posting dict (with a non-empty JD body) from the discovery cache."""
    if not CACHE.is_file():
        return {}
    try:
        data = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}
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


def _jd_for(row: dict, cache: dict[str, dict]) -> tuple[JobDescription | None, str]:
    """Return (JobDescription, source) for a row, or (None, reason). Cache first, then network."""
    url = row.get("source_url") or ""
    post = cache.get(url)
    if post and post.get("body"):
        meta = {"company": row.get("company") or post.get("company", ""),
                "role": row.get("role") or post.get("title", ""), "url": url}
        return JobDescription(body=post["body"], meta=meta), "cache"
    if not url:
        return None, "no source_url to re-fetch"
    res = enrich.fetch_full_jd(url, llm=enrich.claude_llm_extractor)
    if res.ok:
        meta = {"company": row.get("company") or "", "role": row.get("role") or "",
                "url": url, "apply_url": res.apply_url or url}
        return JobDescription(body=res.description, meta=meta), f"fetch:{res.tier}"
    return None, "posting unreachable or no description found"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, help="Backfill one tracked row by id.")
    g.add_argument("--company", help="Backfill tracked rows whose company matches (substring).")
    g.add_argument("--all", action="store_true", help="Backfill every tracked row missing a sidecar.")
    a = ap.parse_args()

    cache = _cached_bodies()
    rows = _rows(a.id, None if a.all else a.company)
    if not rows:
        print("No matching tracked rows.")
        return 1

    wrote = have = failed = 0
    for r in rows:
        label = f"[{r['id']}] {r['company'] or '?'} - {r['role'] or '?'}"
        pdf_path = r.get("resume_path") or ""
        if not pdf_path or not Path(pdf_path).is_file():
            print(f"  FAIL {label}: no tailored PDF on this row — nothing to attach a JD to.")
            failed += 1
            continue
        if resume_store.has_jd(pdf_path):
            print(f"  HAVE {label}: sidecar already exists — left untouched.")
            have += 1
            continue
        jd, src = _jd_for(r, cache)
        if jd is None:
            print(f"  FAIL {label}: {src}.")
            failed += 1
            continue
        resume_store.write_jd(pdf_path, jd)
        if resume_store.has_jd(pdf_path):
            print(f"  DONE {label}: wrote sidecar ({len(jd.body)} chars, via {src}).")
            wrote += 1
        else:
            print(f"  FAIL {label}: sidecar write failed (permissions?).")
            failed += 1

    print(f"\nWrote {wrote}, already had {have}, failed {failed}. "
          f"Rows with a sidecar now show 'Re-tailor ▶' in Track.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Seen-openings ledger (decision 053): show only NEW openings on a preview re-run.

A dry-run / list search re-surfaces the SAME ranked openings every time. Two things cause
it, and they compound: the discovery snapshot cache returns the identical result within its
window (decision 037), and even a `--fresh` search finds the same postings on the boards.
Nothing you only *previewed* is remembered — `skip_seen` (decision 025 follow-up) drops only
postings already in the applications *tracker*, and a preview never writes there. So the whole
ranked list repeats run after run.

This ledger records the canonical URL of every posting a preview surfaces, the first time it
appears. The next preview suppresses already-seen postings and shows only what's NEW. It is
kept deliberately SEPARATE from the applications tracker: a tracker row means "applied / acted
on", while a ledger entry means merely "shown once", so previewing never pollutes your real
application history (or the outcome-calibration stats built from it).

Re-applied fresh each run (like `skip_seen`), so it layers cleanly on top of the snapshot
cache: the cache still holds the FULL ranked result; this filter just hides the rows you've
already seen. Escape hatches: `--all` (CLI) / the "Re-search fresh" button (web) show
everything again, and `python -m applicationbot.discovery_seen clear` resets the ledger.

The ledger holds posting URLs you've been shown (PII: the roles you're targeting), so it lives
under git-ignored ``profile/`` and never leaves the machine (Agent Guideline #12).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .discovery import canonical_url

from .paths import DATA_ROOT
DEFAULT_PATH = DATA_ROOT / "profile" / "discovery_seen.json"

# Bump when the on-disk shape changes so an old ledger is ignored rather than misread.
_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load(path: str | Path | None = None) -> dict[str, str]:
    """The ledger as {canonical_url: first_seen_iso}. Any missing file, wrong schema, or
    read/parse error returns an empty dict (a clean 'nothing seen yet' — never raises)."""
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return {}
    seen = data.get("seen")
    return seen if isinstance(seen, dict) else {}


def seen_urls(path: str | Path | None = None) -> set[str]:
    """Set of canonical URLs already shown — what a preview suppresses."""
    return set(load(path).keys())


def record(urls: Iterable[str], path: str | Path | None = None) -> int:
    """Add each URL's canonical form to the ledger with the current timestamp, iff not already
    present (an existing entry keeps its original first-seen time). Returns how many NEW URLs
    were added. Best-effort: a write failure is swallowed (a lost ledger only costs one extra
    repeat next run, never a crash)."""
    p = Path(path or DEFAULT_PATH)
    seen = load(p)
    now = _now_iso()
    added = 0
    for u in urls:
        key = canonical_url(u) if u else ""
        if not key or key in seen:
            continue
        seen[key] = now
        added += 1
    if added:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"version": _SCHEMA_VERSION, "seen": seen}), encoding="utf-8")
        except Exception:
            pass
    return added


def clear(path: str | Path | None = None) -> bool:
    """Forget every shown opening. Returns True if a ledger file was removed."""
    p = Path(path or DEFAULT_PATH)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Seen-openings ledger — the postings your previews have already shown you."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("count", help="how many openings are remembered as already-shown")
    sub.add_parser("clear", help="forget every shown opening (next preview shows all again)")
    args = ap.parse_args(argv)

    if args.cmd == "count":
        print(f"{len(seen_urls())} opening(s) remembered as already-shown ({DEFAULT_PATH})")
    elif args.cmd == "clear":
        print("cleared" if clear() else "nothing to clear (no ledger yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

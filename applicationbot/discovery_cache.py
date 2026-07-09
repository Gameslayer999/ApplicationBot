"""Snapshot cache for discovery results (decision 037).

Repeated dry-runs re-fetch every board over the network and re-run the Claude fit judge on
the same postings each time — postings you didn't end up applying to (everything below the
top match, or beyond a run cap) get rediscovered and rejudged from scratch. This module
saves the *whole ranked result* of a discovery run to disk so the next run can skip the
board search **and** the Claude judging entirely, as long as:

- the snapshot is younger than the freshness window (`cache_ttl_hours`, default 12h), and
- the résumé, target boards, and gate/matcher filters haven't changed (a fingerprint).

Postings you *did* apply to still drop out on the next run: the caller re-applies the
tracker `skip_seen` filter to the cached matches, so a stale snapshot never re-surfaces a
role that's now in the tracker. Change the résumé/boards, wait out the window, or pass
`force_fresh=True` (CLI `--fresh`) to force a real re-search.

The snapshot holds discovered postings + Claude verdicts (PII: your match notes and the
roles you're targeting), so it lives under git-ignored ``profile/`` and never leaves the
machine (Agent Guideline #12).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .discovery import Posting
from .matching import Match

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "profile" / "discovery_cache.json"

# Bump when the on-disk shape changes so old snapshots are ignored rather than misread.
_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- (de)serialize

def _posting_to_dict(p: Posting) -> dict:
    return dataclasses.asdict(p)


def _posting_from_dict(d: dict) -> Posting:
    # Tolerate a snapshot written by an older/newer build with extra keys.
    fields = {f.name for f in dataclasses.fields(Posting)}
    return Posting(**{k: v for k, v in d.items() if k in fields})


def _match_to_dict(m: Match) -> dict:
    return {
        "posting": _posting_to_dict(m.posting),
        "keyword_score": m.keyword_score,
        "matched_skills": m.matched_skills,
        "qualified": m.qualified,
        "fit_score": m.fit_score,
        "why": m.why,
        "missing": m.missing,
        "judged_by": m.judged_by,
        "dimensions": m.dimensions,
    }


def _match_from_dict(d: dict) -> Match:
    return Match(
        posting=_posting_from_dict(d["posting"]),
        keyword_score=d.get("keyword_score", 0),
        matched_skills=d.get("matched_skills") or [],
        qualified=d.get("qualified"),
        fit_score=d.get("fit_score"),
        why=d.get("why", ""),
        missing=d.get("missing") or [],
        judged_by=d.get("judged_by", "keyword"),
        dimensions=d.get("dimensions") or {},  # absent in pre-043 snapshots
    )


# --------------------------------------------------------------------------- fingerprint

def _resume_fingerprint_parts(resume) -> list:
    """The résumé content that actually drives keyword rank + the Claude judge. A change to
    any of it should invalidate cached verdicts (they'd be judged against a stale résumé)."""
    return [
        resume.summary or "",
        [it for cat in resume.skills for it in cat.items],
        [[e.role or "", e.organization or "", list(e.bullets or [])] for e in resume.experience],
    ]


# Filter fields that change WHICH postings survive gates/matching (so must invalidate the
# cache) — as opposed to downstream thresholds (min_fit) and load-time filters (skip_seen)
# that are re-applied fresh each run and so must NOT invalidate it.
_FILTER_FINGERPRINT_KEYS = (
    "remote_only", "min_salary", "title_exclude", "experience_levels",
    "keywords", "min_skills", "top_n", "max_posting_age_days",
    "adzuna", "early_career",
)


def fingerprint(resume, filters, source_names: list[str], *, use_claude: bool, bridge: bool) -> str:
    """Stable hash over everything that determines the ranked result: the résumé, the exact
    set of sources (board tokens, aggregator config, curated kinds — captured by their
    names), the gate/matcher filters, and the judging flags. Any change ⇒ a cache miss."""
    fdump = filters.model_dump()
    payload = {
        "v": _SCHEMA_VERSION,
        "resume": _resume_fingerprint_parts(resume),
        "sources": sorted(source_names),
        "filters": {k: fdump.get(k) for k in _FILTER_FINGERPRINT_KEYS},
        "use_claude": bool(use_claude),
        "bridge": bool(bridge),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- load / save

@dataclasses.dataclass
class Snapshot:
    matches: list[Match]
    non_fillable: list[Posting]
    discovered: int
    after_gates: int
    bridged: int
    saved_at: str
    age_seconds: float


def _now() -> datetime:
    return datetime.now()


def load(fp: str, *, ttl_hours: float, path: str | Path | None = None) -> Optional[Snapshot]:
    """Return the cached snapshot iff it exists, matches fingerprint `fp`, and is younger
    than `ttl_hours`. Any mismatch, staleness, or read/parse error returns None (a clean
    cache miss — the caller then does a real discovery). Never raises."""
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return None
    if data.get("fingerprint") != fp:
        return None
    saved_at = data.get("saved_at") or ""
    try:
        age = (_now() - datetime.fromisoformat(saved_at)).total_seconds()
    except Exception:
        return None
    if age < 0 or age > ttl_hours * 3600:
        return None
    try:
        matches = [_match_from_dict(m) for m in data.get("matches") or []]
        non_fillable = [_posting_from_dict(p) for p in data.get("non_fillable") or []]
    except Exception:
        return None
    return Snapshot(
        matches=matches,
        non_fillable=non_fillable,
        discovered=int(data.get("discovered", 0)),
        after_gates=int(data.get("after_gates", 0)),
        bridged=int(data.get("bridged", 0)),
        saved_at=saved_at,
        age_seconds=age,
    )


def save(
    fp: str,
    matches: list[Match],
    non_fillable: list[Posting],
    *,
    discovered: int,
    after_gates: int,
    bridged: int,
    path: str | Path | None = None,
) -> None:
    """Write the full ranked result of a discovery run. Best-effort: a write failure is
    swallowed (a missing cache only costs a re-search next time, never a crash)."""
    p = Path(path or DEFAULT_PATH)
    payload = {
        "version": _SCHEMA_VERSION,
        "fingerprint": fp,
        "saved_at": _now().isoformat(timespec="seconds"),
        "discovered": discovered,
        "after_gates": after_gates,
        "bridged": bridged,
        "matches": [_match_to_dict(m) for m in matches],
        "non_fillable": [_posting_to_dict(x) for x in non_fillable],
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def clear(path: str | Path | None = None) -> bool:
    """Delete the snapshot. Returns True if a file was removed."""
    p = Path(path or DEFAULT_PATH)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False

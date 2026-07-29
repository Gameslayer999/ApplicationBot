"""Per-application answer overrides (decision 153) — the answers YOU edited while reviewing.

The "Review before you apply" panel shows the exact values the last dry-run fill produced.
Editing one there cannot touch the form (that browser is long gone), so an edit is recorded
here instead: a per-posting override that the NEXT fill of that posting uses in place of
whatever the resolver would have answered. Every submit path re-fills from scratch (the web
re-apply, the loop's queued Apply, the runner), so what the user typed in Review is what
actually gets submitted.

Stored as ``answers.json`` in that posting's archive dir (decision 043) — git-ignored PII,
keyed by the same company/role/url slug as the ``report.json`` and ``filled.png`` the panel
already reads:

    {"Why do you want to work here?": "…", "Preferred name": "Gabe"}

Keys are field labels exactly as the fill recorded them; lookup is normalised (case and
punctuation folded) so a re-fill still matches when the ATS renders a label slightly
differently. An empty value DELETES that override — the resolver answers the field again.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import archive

FILENAME = "answers.json"


def key(label: str) -> str:
    """Normalised match key for a field label — same folding as apply._norm."""
    return re.sub(r"[^a-z0-9 ]", "", (label or "").lower()).strip()


def path_for(company: str, role: str, source_url: str) -> Path:
    return archive.dir_for(company, role, source_url) / FILENAME


def load(company: str, role: str, source_url: str) -> dict[str, str]:
    """Saved overrides for one posting as ``{label: value}``. Empty when none/unreadable."""
    p = path_for(company, role, source_url)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()}


def save(company: str, role: str, source_url: str, edits: dict) -> dict[str, str]:
    """Merge `edits` into this posting's overrides and return the stored result.

    A blank value removes that label's override (the resolver answers it again). Writing an
    empty result deletes the file, so "no overrides" is never a stale empty dict on disk.
    """
    merged = load(company, role, source_url)
    for label, value in (edits or {}).items():
        label = str(label).strip()
        if not label:
            continue
        value = str(value if value is not None else "").strip()
        if value:
            merged[label] = value
        else:
            merged.pop(label, None)
    p = path_for(company, role, source_url)
    if merged:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    elif p.is_file():
        p.unlink()
    return merged

"""Stable storage for tailored résumé PDFs (decision 029).

Dry-runs used to write the tailored PDF to a throwaway ``$TMPDIR`` file, which macOS
purges — so the ``resume_path`` a Track row points at would silently vanish and you
could no longer see the résumé an application used. This module stores each PDF in a
git-ignored, per-posting file under ``profile/tailored/`` instead.

Growth is bounded three ways so the folder can't creep:
  - **one file per posting** — the filename is derived deterministically from the
    posting URL, so re-running the same job overwrites rather than accumulates;
  - **cascade delete** — ``tracker.delete_application`` removes the row's file (only
    if it lives under this folder — never a user-supplied path);
  - **size cap** — ``prune`` drops the oldest files once the folder passes ``MAX_BYTES``.
Files are ~5 KB each, so the cap is a backstop, not an expected trigger.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .paths import DATA_ROOT
TAILORED_DIR = DATA_ROOT / "profile" / "tailored"

# Backstop cap on the whole folder. At ~5 KB/PDF this is ~20k applications — far past
# the upsert-by-URL count in practice; it only ever trips on pathological reuse.
MAX_BYTES = 100 * 1024 * 1024  # 100 MB


def _slug(text: str, limit: int = 40) -> str:
    """Lowercase, hyphenate, strip to a filesystem-safe stub for readability."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit] or "job"


def path_for(company: str, role: str, source_url: str) -> Path:
    """Deterministic file path for a posting: ``<company>-<role>-<url-hash>.pdf``.

    Keyed on the posting URL (the same dedup key the tracker upserts on) so a re-run
    of the same job resolves to the same file and overwrites it. The company/role
    slug is cosmetic — only the hash guarantees uniqueness/stability.
    """
    digest = hashlib.sha1((source_url or "").encode("utf-8")).hexdigest()[:8]
    name = f"{_slug(company)}-{_slug(role)}-{digest}.pdf"
    return TAILORED_DIR / name


def write_pdf(data: bytes, company: str, role: str, source_url: str) -> str:
    """Write a tailored PDF to its stable per-posting path, prune, return the path."""
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    dest = path_for(company, role, source_url)
    dest.write_bytes(data)
    prune(keep=dest)
    return str(dest)


# --- tailoring stamp -------------------------------------------------------------------
# A tiny sidecar (``<pdf>.stamp``) holding a content hash of the inputs that produced the
# PDF (résumé + profile links + JD). The rescan re-prepare path reuses a PDF instead of
# re-tailoring (a Claude call) when its stamp still matches — robust to the profile file's
# own churn from learned screening answers, which mtime comparison would misread as a change.

def _stamp_path(pdf_path: str | Path) -> Path:
    return Path(str(pdf_path) + ".stamp")


def read_stamp(pdf_path: str | Path) -> str | None:
    """The stamp written beside ``pdf_path``, or None if absent/unreadable."""
    try:
        return _stamp_path(pdf_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_stamp(pdf_path: str | Path, key: str) -> None:
    """Record ``key`` beside ``pdf_path``. Best-effort (a missing stamp only forces a
    re-tailor next time, never a crash)."""
    try:
        _stamp_path(pdf_path).write_text(key, encoding="utf-8")
    except OSError:
        pass


# --- job-description sidecar (decision 086) --------------------------------------------
# A ``<pdf>.jd`` JSON sidecar holding the posting's job description (body + meta) that a PDF
# was tailored to. Kept so a Track "Re-run → re-tailor" can regenerate the résumé offline —
# against the saved JD + the user's *current* base résumé/prompt — without re-scraping the
# posting. Written on every dry-run tailor (reuse or fresh); pruned/deleted with its PDF.

def _jd_path(pdf_path: str | Path) -> Path:
    return Path(str(pdf_path) + ".jd")


def write_jd(pdf_path: str | Path, jd) -> None:
    """Record the JobDescription (body + meta) beside ``pdf_path``. Best-effort."""
    try:
        _jd_path(pdf_path).write_text(
            json.dumps({"body": jd.body, "meta": jd.meta}), encoding="utf-8")
    except OSError:
        pass


def read_jd(pdf_path: str | Path):
    """The JobDescription saved beside ``pdf_path``, or None if absent/unreadable."""
    from .job_description import JobDescription
    try:
        d = json.loads(_jd_path(pdf_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return JobDescription(body=d.get("body", ""), meta=d.get("meta") or {})


def has_jd(pdf_path: str | Path) -> bool:
    """True iff a JD sidecar exists for ``pdf_path`` — i.e. a re-tailor can run offline."""
    return bool(pdf_path) and _jd_path(pdf_path).is_file()


def is_managed(path: str | Path) -> bool:
    """True iff ``path`` lives under ``TAILORED_DIR`` — the guard that keeps cascade
    delete from ever removing a user-supplied résumé outside this folder."""
    try:
        Path(path).resolve().relative_to(TAILORED_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def delete_if_managed(path: str | Path) -> bool:
    """Delete the file only if it's one we manage; no-op otherwise. Best-effort."""
    if not is_managed(path):
        return False
    try:
        Path(path).unlink(missing_ok=True)
        _stamp_path(path).unlink(missing_ok=True)
        _jd_path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def prune(*, max_bytes: int = MAX_BYTES, keep: Path | None = None) -> int:
    """Drop the oldest PDFs (by mtime) until the folder is under ``max_bytes``.

    Returns the number of files removed. ``keep`` (the file just written) is never
    removed. This is the backstop; per-posting overwrite + cascade delete do the real
    bounding."""
    if not TAILORED_DIR.exists():
        return 0
    files = sorted(TAILORED_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed = 0
    keep_resolved = keep.resolve() if keep else None
    for f in files:
        if total <= max_bytes:
            break
        if keep_resolved and f.resolve() == keep_resolved:
            continue
        size = f.stat().st_size
        try:
            f.unlink()
            _stamp_path(f).unlink(missing_ok=True)
            _jd_path(f).unlink(missing_ok=True)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed

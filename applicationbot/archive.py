"""Per-application archive (decision 043): what exactly did we send, against what posting.

The tracker row points at a tailored PDF that gets overwritten on re-runs, and postings go
dead — so once the runner submits autonomously there is no way to reconstruct what a given
application actually contained. This module snapshots each application into a git-ignored
folder under ``profile/applications/`` (``profile/*`` is already in ``.gitignore``):

    <company>-<role>-<url-hash>/          one dir per posting — same key as resume_store
        posting.md      the JD as fetched (title/company/location/pay/url + body)
        resume.pdf      the exact PDF bytes uploaded
        report.json     the fill outcome (fields filled, skipped, submit state, when)
        submitted-<date>/   frozen copy written on a REAL submission — never overwritten

Dry-runs overwrite the dir-root files (latest state, bounded like resume_store); an armed
real submission additionally freezes a dated copy that nothing ever touches again.
Adapted from ai-job-search's ``documents/applications/<company>_<role>/`` archive.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .resume_store import _slug

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "profile" / "applications"


def dir_for(company: str, role: str, source_url: str) -> Path:
    """Deterministic per-posting archive dir — same slug+hash key as the tailored PDF."""
    import hashlib

    digest = hashlib.sha1((source_url or "").encode("utf-8")).hexdigest()[:8]
    return ARCHIVE_DIR / f"{_slug(company)}-{_slug(role)}-{digest}"


def _posting_md(company: str, role: str, meta: dict, jd_text: str) -> str:
    header = [f"# {role} — {company}"]
    for label, key in (("Location", "location"), ("Remote", "remote"), ("Pay", "pay"),
                       ("Source", "source_url")):
        if meta.get(key):
            header.append(f"- {label}: {meta[key]}")
    header.append(f"- Archived: {datetime.now().isoformat(timespec='seconds')}")
    return "\n".join(header) + "\n\n---\n\n" + (jd_text or "(no posting text captured)") + "\n"


def archive_run(company: str, role: str, meta: dict, *, jd_text: str = "",
                pdf_path: str = "", report_data: dict | None = None,
                submitted: bool = False) -> str:
    """Snapshot one apply run. Returns the archive dir path.

    Overwrites the posting/resume/report at the dir root (latest run state); when
    ``submitted``, also freezes them into a ``submitted-<date>`` subdir that is never
    reused — if the name exists (resubmission same day), a ``-2``/``-3`` suffix is added.
    """
    dest = dir_for(company, role, meta.get("source_url", ""))
    dest.mkdir(parents=True, exist_ok=True)

    (dest / "posting.md").write_text(_posting_md(company, role, meta, jd_text),
                                     encoding="utf-8")
    pdf = Path(pdf_path) if pdf_path else None
    if pdf and pdf.is_file():
        (dest / "resume.pdf").write_bytes(pdf.read_bytes())
    if report_data is not None:
        (dest / "report.json").write_text(
            json.dumps(report_data, indent=1, ensure_ascii=False), encoding="utf-8")

    if submitted:
        frozen = dest / f"submitted-{date.today().isoformat()}"
        n = 2
        while frozen.exists():
            frozen = dest / f"submitted-{date.today().isoformat()}-{n}"
            n += 1
        frozen.mkdir(parents=True)
        for name in ("posting.md", "resume.pdf", "report.json"):
            src = dest / name
            if src.is_file():
                (frozen / name).write_bytes(src.read_bytes())
    return str(dest)

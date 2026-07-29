"""The user's own résumé DOCUMENTS, kept as submittable files (decision 152).

`resume_import` parses an uploaded résumé into the catalogue and used to throw the bytes away.
But the file the user wrote is a *real* résumé — proof-read, laid out the way they want, and in
most cases already sent to employers — so when a posting demands essentially only skills that
document already shows, sending it beats sending a machine-tailored PDF. This module keeps each
uploaded PDF plus the text extracted from it, so `pipeline.find_uploaded_match` can score a
posting against it token-free (`ats_requirements` over the stored text — no Claude call).

Layout (git-ignored: `profile/*` is in `.gitignore`):

    profile/uploads/
        <name-slug>-<content-hash>.pdf     the exact bytes the user uploaded
        <...>.pdf.meta                     {"filename", "text", "uploaded_at"}

**PDFs only.** The apply stage uploads `pdf_path` straight into the ATS file field, so a DOCX/TXT
upload could not be submitted as-is; those still merge into the catalogue, they just aren't kept
as reuse candidates. Names are content-hashed, so re-uploading the same file overwrites instead of
accumulating, while a genuinely different résumé becomes its own candidate (the best-covering one
wins at match time). The stored *text* — not a precomputed skill list — is what's kept, because the
skill universe comes from the user's current résumé catalogue and changes as they edit it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .paths import DATA_ROOT
from .resume_store import _slug

UPLOADS_DIR = DATA_ROOT / "profile" / "uploads"


def _meta_path(pdf_path: str | Path) -> Path:
    return Path(str(pdf_path) + ".meta")


def is_pdf(filename: str, data: bytes) -> bool:
    """True iff this upload is a PDF — the only format an ATS file field can take verbatim."""
    return (filename or "").lower().endswith(".pdf") or data[:5] == b"%PDF-"


def path_for(filename: str, data: bytes) -> Path:
    """Deterministic path for an uploaded document: ``<name-slug>-<content-hash>.pdf``."""
    digest = hashlib.sha1(data).hexdigest()[:8]
    stem = _slug(Path(filename or "resume").stem)
    return UPLOADS_DIR / f"{stem}-{digest}.pdf"


def store(filename: str, data: bytes, text: str) -> str | None:
    """Keep an uploaded PDF (plus its extracted `text`) as a reuse candidate; return its path.

    Returns None for a non-PDF upload (nothing kept) or on a write error — keeping the document
    is a bonus on top of the catalogue merge, so a failure here must never fail the import."""
    if not is_pdf(filename, data):
        return None
    try:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest = path_for(filename, data)
        dest.write_bytes(data)
        _meta_path(dest).write_text(json.dumps({
            "filename": filename or dest.name,
            "text": text or "",
            "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
        return str(dest)
    except OSError:
        return None


def all_docs() -> list[tuple[Path, dict]]:
    """Every stored ``(pdf_path, meta)`` pair — the corpus the uploaded-résumé match searches.
    Sorted by path for deterministic tie-breaking; unreadable sidecars are skipped."""
    if not UPLOADS_DIR.exists():
        return []
    out: list[tuple[Path, dict]] = []
    for meta in sorted(UPLOADS_DIR.glob("*.pdf.meta")):
        pdf = meta.with_suffix("")
        if not pdf.is_file():
            continue
        try:
            out.append((pdf, json.loads(meta.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return out


def listing() -> list[dict]:
    """The kept documents as UI rows: file `name` (the delete key), original `filename`, date."""
    return [{"name": p.name, "filename": m.get("filename") or p.name,
             "uploaded_at": m.get("uploaded_at") or ""} for p, m in all_docs()]


def delete(name: str) -> bool:
    """Remove one kept document by file name. Refuses anything outside `UPLOADS_DIR` — `name` is
    a plain file name, never a path, so a traversal attempt can't reach the rest of the profile."""
    if not name or "/" in name or "\\" in name or not name.endswith(".pdf"):
        return False
    target = UPLOADS_DIR / name
    try:
        if not target.is_file():
            return False
        target.unlink()
        _meta_path(target).unlink(missing_ok=True)
        return True
    except OSError:
        return False

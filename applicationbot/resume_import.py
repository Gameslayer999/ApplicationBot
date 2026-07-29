"""Import a résumé document (PDF/DOCX/TXT) into the résumé catalogue.

The Profile page lets the user type their history in by hand or import a LinkedIn export
(`linkedin.py`). This module adds the obvious third path: upload the résumé they already
have. It extracts the document's text, has Claude structure it into the `Resume` shape,
and MERGES new entries into the catalogue — deduping against what's already there, exactly
like the LinkedIn import (existing entries and contact fields are never overwritten). When
there is no résumé yet, the parsed content becomes the first `profile/resume.yaml`.

Parsing needs an LLM (real résumé layouts are too varied for heuristics — decision logged in
DECISIONS.md), so it uses the same Claude engines as tailoring: the Claude Code CLI
(subscription) when available, else the user's Anthropic API key. No key and no CLI → a clear,
actionable error. Text extraction is dependency-light: PDFs via the already-bundled `pypdf`,
DOCX via stdlib `zipfile` (a .docx is a zip of XML), plain text decoded directly.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from . import auth, backends, catalogue, resume_docs
from .models import Education, Experience, Project, Resume, SkillCategory
from .resume import load_resume

# The canonical user résumé the whole pipeline reads. A brand-new user has no résumé yet, so
# an upload must be able to create it; anything that isn't an already-existing profile/ résumé
# imports into this fixed, safe path (never a shipped examples/ file, never a caller-supplied one).
DEFAULT_RESUME = "profile/resume.yaml"

_MAX_TEXT = 60_000  # ~15+ pages of résumé; guards against a pathological upload blowing the prompt


# --------------------------------------------------------------- text extraction

def extract_text(filename: str, data: bytes) -> str:
    """Return the plain text of an uploaded résumé (PDF, DOCX, or text). Raises ValueError with
    an actionable message when the format is unsupported or no text can be read."""
    name = (filename or "").lower()
    is_pdf = name.endswith(".pdf") or data[:5] == b"%PDF-"
    is_docx = name.endswith(".docx") or (data[:2] == b"PK" and b"word/document.xml" in data[:4000] + data[-4000:])

    if is_pdf:
        text = _pdf_text(data)
    elif is_docx:
        text = _docx_text(data)
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ValueError("Unsupported file type. Upload a PDF, DOCX, or plain-text résumé.")

    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        raise ValueError(
            "No text could be read from that file. If it's a scanned/image-only PDF, export a "
            "text-based PDF from your word processor (File → Save as PDF), or paste the résumé "
            "into a .txt file and upload that.")
    return text[:_MAX_TEXT]


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        raise ValueError(f"Could not read that PDF ({type(e).__name__}: {e}). "
                         "Re-export it as a standard PDF and try again.") from e


def _docx_text(data: bytes) -> str:
    # A .docx is a ZIP; the body lives in word/document.xml. Paragraphs are <w:p>, runs of text
    # <w:t>. Join <w:t> contents, break lines on </w:p>, drop the remaining tags. No dependency.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"Could not read that DOCX ({type(e).__name__}: {e}). "
                         "Re-save it as .docx or export to PDF and try again.") from e
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[ /]", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return text


# --------------------------------------------------------------- Claude parse

class _ParsedContact(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    links: list[str] = Field(default_factory=list)


class _ParsedResume(BaseModel):
    """The parse target — same shape as `Resume` but with an all-optional contact, so a résumé
    that omits (say) an email address still validates."""

    contact: _ParsedContact = Field(default_factory=_ParsedContact)
    summary: Optional[str] = None
    skills: list[SkillCategory] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    activities: list[Experience] = Field(default_factory=list, description="Leadership and activities.")
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)


_SYSTEM = """\
You are a résumé parser. You are given the raw text of one candidate's résumé. Extract it \
into the given JSON structure — nothing more.

Hard rules:
- Use ONLY information present in the text. NEVER invent, infer, or embellish organizations, \
roles, titles, dates, degrees, certifications, metrics, links, or skills.
- Copy bullet text faithfully; you may fix obvious OCR/line-wrap breaks, but do not rewrite, \
summarize, or add achievements.
- Sort each entry's fields into: experience (jobs/internships), activities (leadership, clubs, \
volunteering), projects (personal/side/academic builds), education, skills (grouped by the \
candidate's own categories, e.g. Languages/Tools), certifications.
- Dates: use the résumé's own wording (e.g. "May 2024", "Present"). If an entry has no date, \
use "" — do not guess.
- Leave `tailor_note` and `impact` null (they are set later by the app, not by you).
- `contact.links` = profile/portfolio/repo URLs found in the header (LinkedIn, GitHub, site).
"""


def _parse_with_claude(text: str) -> _ParsedResume:
    prompt = (
        "Résumé text to parse:\n\n" + text +
        "\n\nOutput format: respond with ONLY a single JSON object matching the schema — no "
        "explanation, no markdown code fences.")
    schema = _ParsedResume.model_json_schema()

    if backends.claude_code_available():
        raw = backends.run_claude_cli(prompt, system=_SYSTEM, json_schema=schema,
                                      think=False, activity="resume-parse")
    else:
        key = auth.get_api_key()
        if not key:
            raise RuntimeError(
                "Parsing a résumé needs Claude. Install Claude Code and sign in to your "
                "subscription, or connect an Anthropic API key in the account panel — then "
                "upload again. (You can also fill the résumé sections in by hand.)")
        raw = backends.run_anthropic_api(prompt, api_key=key, system=_SYSTEM,
                                         activity="resume-parse")
    try:
        return _ParsedResume.model_validate_json(backends._extract_json(raw))
    except Exception as e:
        raise RuntimeError(f"Claude did not return a valid résumé structure: {e}") from e


# --------------------------------------------------------------- merge

def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def import_resume(path, filename: str, data: bytes) -> dict:
    """Extract, parse, and merge an uploaded résumé into the résumé at `path` (created if it does
    not exist). Returns per-section counts of what was ADDED plus which contact fields were filled
    — new entries only; existing entries and non-blank contact fields are never overwritten."""
    text = extract_text(filename, data)
    parsed = _parse_with_claude(text)

    target = Path(path)
    created = not target.exists()
    if created:
        # First résumé: the parsed document IS the résumé (contact must have at least a name).
        resume = Resume(contact={  # type: ignore[arg-type]
            "name": parsed.contact.name or "",
            "email": parsed.contact.email or "",
            "phone": parsed.contact.phone,
            "location": parsed.contact.location,
            "links": list(parsed.contact.links),
        })
    else:
        resume = load_resume(target)

    added = {"experience": 0, "activities": 0, "projects": 0,
             "education": 0, "skills": 0, "certifications": 0}

    # Contact — fill only blank fields (never overwrite what the user already has).
    contact_filled: list[str] = []
    for fld in ("name", "email", "phone", "location"):
        cur = getattr(resume.contact, fld, None)
        new = getattr(parsed.contact, fld, None)
        if not (cur or "").strip() and (new or "").strip():
            setattr(resume.contact, fld, new.strip())
            contact_filled.append(fld)
    have_links = {_norm(l) for l in resume.contact.links}
    for link in parsed.contact.links:
        if link.strip() and _norm(link) not in have_links:
            have_links.add(_norm(link))
            resume.contact.links.append(link.strip())

    # Experience / activities — dedupe on (organization, role) within their own section.
    def _merge_roles(dest: list[Experience], src: list[Experience], key: str) -> None:
        have = {(_norm(e.organization), _norm(e.role)) for e in dest}
        for e in src:
            k = (_norm(e.organization), _norm(e.role))
            if not e.organization.strip() or not e.role.strip() or k in have:
                continue
            have.add(k)
            dest.append(e)
            added[key] += 1

    _merge_roles(resume.experience, parsed.experience, "experience")
    _merge_roles(resume.activities, parsed.activities, "activities")

    # Projects — dedupe on name.
    have_proj = {_norm(p.name) for p in resume.projects}
    for p in parsed.projects:
        if p.name.strip() and _norm(p.name) not in have_proj:
            have_proj.add(_norm(p.name))
            resume.projects.append(p)
            added["projects"] += 1

    # Education — dedupe on (school, degree).
    have_edu = {(_norm(e.school), _norm(e.degree)) for e in resume.education}
    for e in parsed.education:
        k = (_norm(e.school), _norm(e.degree))
        if not e.school.strip() or k in have_edu:
            continue
        have_edu.add(k)
        resume.education.append(e)
        added["education"] += 1

    # Skills — merge items into matching categories (case-insensitive), deduped across all skills.
    existing_items = {_norm(i) for c in resume.skills for i in c.items}
    for cat in parsed.skills:
        fresh = [i.strip() for i in cat.items if i.strip() and _norm(i) not in existing_items]
        if not fresh:
            continue
        existing_items.update(_norm(i) for i in fresh)
        match = next((c for c in resume.skills if _norm(c.category) == _norm(cat.category)), None)
        if match:
            match.items.extend(fresh)
        else:
            resume.skills.append(SkillCategory(category=cat.category, items=fresh))
        added["skills"] += len(fresh)

    # Certifications — dedupe on the string.
    have_cert = {_norm(c) for c in resume.certifications}
    for c in parsed.certifications:
        if c.strip() and _norm(c) not in have_cert:
            have_cert.add(_norm(c))
            resume.certifications.append(c.strip())
            added["certifications"] += 1

    # Fill an empty summary only (like every other field: never overwrite the user's).
    summary_added = False
    if not (resume.summary or "").strip() and (parsed.summary or "").strip():
        resume.summary = parsed.summary.strip()
        summary_added = True

    target.parent.mkdir(parents=True, exist_ok=True)
    catalogue.save_resume(target, resume)

    # Keep the document itself, not just what we parsed out of it (decision 152): a PDF résumé the
    # user wrote is sent verbatim to postings whose demanded skills it already covers, in
    # preference to any tailored PDF. Best-effort — a storage failure never fails the import.
    kept = resume_docs.store(filename, data, text)
    return {"added": added, "contact_filled": contact_filled,
            "summary_added": summary_added, "created": created,
            "kept_document": bool(kept)}

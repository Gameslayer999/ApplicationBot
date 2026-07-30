"""Import a résumé document (PDF/DOCX/TXT) into the résumé catalogue.

The Profile page lets the user type their history in by hand or import a LinkedIn export
(`linkedin.py`). This module adds the obvious third path: upload the résumé they already
have. It extracts the document's text, has Claude structure it into the `Resume` shape,
and MERGES new entries into the catalogue. Deduping is the hard part: a second résumé spells the
same job differently ("Acme Corp." / "Acme Corporation", "SWE Intern" / "Software Engineer Intern"),
so entries are matched on normalised employer/title tokens plus their month-precise span rather than
exact strings — existing entries are never duplicated and never overwritten, and the caller is told
which entries were matched instead of added. When there is no résumé yet, the parsed content becomes
the first `profile/resume.yaml`.

Parsing needs an LLM (real résumé layouts are too varied for heuristics — decision logged in
DECISIONS.md), so it uses the same Claude engines as tailoring: the Claude Code CLI
(subscription) when available, else the user's Anthropic API key. No key and no CLI → a clear,
actionable error. Text extraction is dependency-light: PDFs via the already-bundled `pypdf`,
DOCX via stdlib `zipfile` (a .docx is a zip of XML), plain text decoded directly.
"""

from __future__ import annotations

import difflib
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


# Two résumé versions of the same real entry rarely spell it identically — "Acme Corp." vs
# "Acme Corporation", "Software Engineer Intern" vs "SWE Intern", "Intern" vs the full title.
# Exact string equality therefore appends a duplicate of a role the profile already has. The
# matcher below compares entries the way a person would: strip punctuation and boilerplate,
# fold the handful of title synonyms résumés actually use, then accept a token-subset or a
# high character-similarity — but refuse the match if the two entries state different years,
# which is what distinguishes a genuine second stint at the same employer from a re-spelling.

_ORG_NOISE = frozenset({"the", "inc", "incorporated", "llc", "llp", "ltd", "limited", "plc",
                        "corp", "corporation", "co", "company", "group", "gmbh", "sa", "ag"})
_ROLE_SYNONYM = {"engineering": "engineer", "developer": "engineer", "dev": "engineer",
                 "swe": "engineer", "internship": "intern",
                 "coop": "intern", "sr": "senior", "jr": "junior", "and": "", "of": "", "the": ""}


def _words(s: Optional[str]) -> list[str]:
    """Lower-cased words with punctuation dropped, and runs of single letters glued back into the
    acronym they came from ("M.I.T." → `mit`, "B.S." → `bs`) so they match the unpunctuated form."""
    words, out = re.sub(r"[^a-z0-9+#]+", " ", (s or "").lower()).split(), []
    for w in words:
        if len(w) == 1 and w.isalpha() and out and out[-1].isalpha() and len(out[-1]) <= 2:
            out[-1] += w
        else:
            out.append(w)
    return out


def org_tokens(s: Optional[str]) -> frozenset[str]:
    return frozenset(w for w in _words(s) if w not in _ORG_NOISE)


def phrase_tokens(s: Optional[str]) -> frozenset[str]:
    """Tokens for a role, degree, project name, or certification, with the title synonyms
    résumés actually vary on folded together and connecting words dropped."""
    return frozenset(t for t in (_ROLE_SYNONYM.get(w, w) for w in _words(s)) if t)


def same_tokens(a: frozenset[str], b: frozenset[str]) -> bool:
    """True when two token sets name the same thing: identical, one contained in the other
    (`Intern` ⊂ `Software Engineer Intern`), or ≥0.87 character-similar (typos, abbreviations)."""
    if not a or not b:
        return False
    if a == b or a <= b or b <= a:
        return True
    return difflib.SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio() >= 0.87


def _year(s: Optional[str]) -> Optional[str]:
    m = re.search(r"(19|20)\d{2}", s or "")
    return m.group(0) if m else None


_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _month_year(s: Optional[str]) -> Optional[tuple[str, int]]:
    """(year, month) for a résumé date, or None when either part is missing ("2024", "Present")."""
    t = (s or "").lower()
    y = _year(t)
    if not y:
        return None
    mo = next((n for m, n in _MONTHS.items() if m in t), None)
    if mo is None:  # numeric forms: "05/2024", "5-1-2024", and ISO "2024-05-01"
        m2 = (re.search(r"(?:19|20)\d{2}\s*[/-]\s*(0?[1-9]|1[0-2])\b", t)
              or re.search(r"\b(0?[1-9]|1[0-2])\s*[/-]\s*(?:\d{1,2}\s*[/-]\s*)?(?:19|20)\d{2}", t))
        mo = int(m2.group(1)) if m2 else None
    return (y, mo) if mo else None


def same_period(a: Experience, b: Experience) -> bool:
    """True when two entries cover the identical month-precise span. Nobody holds two different
    jobs at one employer over exactly the same months, so this catches the same role re-titled
    between résumé versions ("Software Engineer Intern" ↔ "Junior Software Engineer")."""
    sa, sb = _month_year(a.start), _month_year(b.start)
    if not (sa and sb and sa == sb):
        return False
    pa, pb = "present" in _norm(a.end), "present" in _norm(b.end)
    if pa or pb:
        return pa and pb
    ea, eb = _month_year(a.end), _month_year(b.end)
    return bool(ea and eb and ea == eb)


def years_conflict(a: Optional[str], b: Optional[str]) -> bool:
    """True when both entries state a year and the years differ — a second, distinct stint
    (2022 internship vs 2024 return offer), not the same entry spelled differently."""
    ya, yb = _year(a), _year(b)
    return bool(ya and yb and ya != yb)


def find_role(resume: Resume, e: Experience) -> Optional[Experience]:
    """The entry in `resume` that is already this role, or None. Both `experience` and `activities`
    are searched for every candidate: the same internship lands in one section from one import and
    the other from the next, and that is still one role."""
    org, role = org_tokens(e.organization), phrase_tokens(e.role)
    for have in (*resume.experience, *resume.activities):
        if (same_tokens(org, org_tokens(have.organization))
                and (same_tokens(role, phrase_tokens(have.role)) or same_period(e, have))
                and not years_conflict(e.start, have.start)):
            return have
    return None


def find_education(resume: Resume, e: Education) -> Optional[Education]:
    """The entry in `resume` that is already this degree, or None. Same school plus the same degree
    — or a degree either side left blank — is one entry, unless the graduation years disagree."""
    school, degree = org_tokens(e.school), phrase_tokens(e.degree)
    return next((h for h in resume.education
                 if same_tokens(school, org_tokens(h.school))
                 and (same_tokens(degree, phrase_tokens(h.degree))
                      or not h.degree.strip() or not e.degree.strip())
                 and not years_conflict(e.graduation, h.graduation)), None)


def skill_key(s: str) -> str:
    """Comparison key for a skill: punctuation and spacing removed, so "Node.js" does not re-add
    "NodeJS". Deliberately exact rather than fuzzy — "React" and "React Native" are different."""
    return re.sub(r"[^a-z0-9+#]", "", s.lower())


def fill_blanks(existing, new, fields: tuple[str, ...]) -> bool:
    """Copy `fields` from a parsed entry onto the matching existing one, but only where the
    existing value is blank — same rule as contact fields: never overwrite the user's data."""
    filled = False
    for fld in fields:
        cur, val = getattr(existing, fld, None), getattr(new, fld, None)
        if not (cur or "").strip() and (val or "").strip():
            setattr(existing, fld, val.strip())
            filled = True
    return filled


def import_resume(path, filename: str, data: bytes) -> dict:
    """Extract, parse, and merge an uploaded résumé into the résumé at `path` (created if it does
    not exist). Returns per-section counts of what was ADDED, which contact fields were filled, and
    the entries `skipped` (already in the résumé, in any spelling) or `enriched` (matched an
    existing entry and filled blanks in it) — existing content is never overwritten or duplicated."""
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
    skipped: list[str] = []   # entries already in the résumé, named so the user can see what we dropped
    enriched: list[str] = []  # existing entries whose blank fields this upload filled in

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

    # Experience / activities — a role already in the résumé is never appended again, however it
    # is spelled.
    def _merge_roles(dest: list[Experience], src: list[Experience], key: str) -> None:
        for e in src:
            if not e.organization.strip() or not e.role.strip():
                continue
            match = find_role(resume, e)
            if match is not None:
                label = f"{match.organization} — {match.role}"
                (enriched if fill_blanks(match, e, ("location", "start", "end")) else skipped).append(label)
                continue
            dest.append(e)
            added[key] += 1

    _merge_roles(resume.experience, parsed.experience, "experience")
    _merge_roles(resume.activities, parsed.activities, "activities")

    # Projects — dedupe on name.
    for p in parsed.projects:
        if not p.name.strip():
            continue
        name = phrase_tokens(p.name)
        match = next((q for q in resume.projects if same_tokens(name, phrase_tokens(q.name))), None)
        if match is not None:
            (enriched if fill_blanks(match, p, ("tech", "link")) else skipped).append(match.name)
            continue
        resume.projects.append(p)
        added["projects"] += 1

    # Education — same school and degree (or a degree the résumé left blank) is the same entry.
    for e in parsed.education:
        if not e.school.strip():
            continue
        match = find_education(resume, e)
        if match is not None:
            label = f"{match.school} — {match.degree}" if match.degree.strip() else match.school
            (enriched if fill_blanks(match, e, ("degree", "location", "graduation")) else skipped).append(label)
            continue
        resume.education.append(e)
        added["education"] += 1

    # Skills — merge items into matching categories (case-insensitive), deduped across all skills.
    existing_items = {skill_key(i) for c in resume.skills for i in c.items}
    for cat in parsed.skills:
        fresh = [i.strip() for i in cat.items if i.strip() and skill_key(i) not in existing_items]
        if not fresh:
            continue
        existing_items.update(skill_key(i) for i in fresh)
        match = next((c for c in resume.skills if _norm(c.category) == _norm(cat.category)), None)
        if match:
            match.items.extend(fresh)
        else:
            resume.skills.append(SkillCategory(category=cat.category, items=fresh))
        added["skills"] += len(fresh)

    # Certifications — dedupe on the wording, ignoring punctuation and issuer boilerplate.
    for c in parsed.certifications:
        if not c.strip():
            continue
        key = phrase_tokens(c)
        if any(same_tokens(key, phrase_tokens(h)) for h in resume.certifications):
            skipped.append(c.strip())
            continue
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
            "kept_document": bool(kept), "skipped": skipped, "enriched": enriched}

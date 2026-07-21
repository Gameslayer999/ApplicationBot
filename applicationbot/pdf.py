"""Render a tailored résumé to a PDF — pure Python via fpdf2 (no Chromium, no system libs).

Generates a real-text, single-column, ATS-friendly PDF straight from the structured resume
(the same data the HTML/Markdown renderers use), so it parses cleanly in applicant tracking
systems. Contact + section order come from the base resume; content from the tailored one.
"""

from __future__ import annotations

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .models import (
    SECTION_KEYS,
    Contact,
    Education,
    Experience,
    Project,
    Resume,
    SkillCategory,
    TailoredResume,
)

DEFAULT_ORDER = list(SECTION_KEYS)

# Core-font PDFs use latin-1; map common unicode punctuation, replace the rest.
_REPL = {
    "–": "-", "—": "-", "•": "-", "·": "-", "→": "->",
    "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...",
    " ": " ", " ": " ", "​": "",
}

MUTED = (110, 110, 110)
INK = (28, 28, 28)


def _t(s: object) -> str:
    text = str(s or "")
    for k, v in _REPL.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


class _Resume(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="pt", format="letter")
        self.set_margins(34, 34, 34)
        self.set_auto_page_break(True, margin=34)
        self.add_page()

    def _two_col(self, left: str, right: str, lfont, rfont, gap: float = 2):
        """A left value and a right-aligned value on one line (org — location, role · dates)."""
        w = self.epw
        self.set_font(*lfont)
        self.set_text_color(*INK)
        self.cell(w * 0.72, 13, _t(left), new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font(*rfont)
        self.set_text_color(*MUTED)
        self.cell(w * 0.28, 13, _t(right), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*INK)
        self.ln(gap)

    def _bullets(self, bullets: list[str]):
        self.set_font("Helvetica", "", 9.5)
        indent = 12
        for b in bullets:
            x0 = self.l_margin
            self.set_xy(x0, self.get_y())
            self.cell(indent, 12, "-", new_x=XPos.RIGHT, new_y=YPos.TOP)
            self.multi_cell(self.epw - indent, 12, _t(b), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(3)

    def heading(self, title: str):
        self.ln(5)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*INK)
        self.cell(0, 15, _t(title.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        y = self.get_y()
        self.set_draw_color(*INK)
        self.line(self.l_margin, y, self.l_margin + self.epw, y)
        self.ln(5)


def _fit_font(pdf: _Resume, text: str, style: str, size: float, floor: float):
    """Set Helvetica/`style` at the largest size <= `size` (down to `floor`) at which `text`
    fits the content width — so a long name or contact line can never spill past the margins
    (a plain `cell` doesn't wrap or shrink; it just overprints past the page edge)."""
    while size > floor:
        pdf.set_font("Helvetica", style, size)
        if pdf.get_string_width(text) <= pdf.epw:
            return
        size -= 0.5
    pdf.set_font("Helvetica", style, floor)


def _contact(pdf: _Resume, c: Contact):
    _fit_font(pdf, _t(c.name), "B", 20, 12)
    pdf.cell(0, 24, _t(c.name), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    bits = [b for b in (c.location, c.email, c.phone) if b] + list(c.links)
    if bits:
        line = _t("  |  ".join(bits))
        _fit_font(pdf, line, "", 9, 6.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 13, line, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(*INK)


def _experience(pdf: _Resume, e: Experience):
    pdf._two_col(e.organization, e.location or "", ("Helvetica", "B", 10.5), ("Helvetica", "", 9))
    pdf._two_col(e.role, f"{e.start} - {e.end}", ("Helvetica", "I", 9.5), ("Helvetica", "", 9))
    pdf._bullets(e.bullets)


def _project(pdf: _Resume, p: Project):
    pdf._two_col(p.name, p.tech or "", ("Helvetica", "B", 10.5), ("Helvetica", "", 9))
    pdf._bullets(p.bullets)


def _education(pdf: _Resume, e: Education):
    pdf._two_col(e.school, e.location or "", ("Helvetica", "B", 10.5), ("Helvetica", "", 9))
    pdf._two_col(e.degree, e.graduation or "", ("Helvetica", "I", 9.5), ("Helvetica", "", 9))
    pdf._bullets(e.details)


def _skills(pdf: _Resume, skills: list[SkillCategory]):
    for cat in skills:
        pdf.set_font("Helvetica", "B", 9.5)
        label = _t(cat.category + ": ")
        pdf.cell(pdf.get_string_width(label), 13, label, new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.set_font("Helvetica", "", 9.5)
        pdf.multi_cell(0, 13, _t(", ".join(cat.items)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _build(base: Resume, tailored: TailoredResume) -> _Resume:
    """Lay out the tailored résumé and return the FPDF object (for bytes or a page count)."""
    pdf = _Resume()
    _contact(pdf, base.contact)

    for name in (base.section_order or DEFAULT_ORDER):
        if name == "summary" and tailored.summary:
            pdf.heading("Summary")
            pdf.set_font("Helvetica", "", 9.5)
            pdf.multi_cell(0, 12, _t(tailored.summary), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif name == "education" and tailored.education:
            pdf.heading("Education")
            for e in tailored.education:
                _education(pdf, e)
        elif name == "experience" and tailored.experience:
            pdf.heading("Experience")
            for e in tailored.experience:
                _experience(pdf, e)
        elif name == "projects" and tailored.projects:
            pdf.heading("Projects")
            for p in tailored.projects:
                _project(pdf, p)
        elif name == "activities" and tailored.activities:
            pdf.heading("Leadership and Activities")
            for a in tailored.activities:
                _experience(pdf, a)
        elif name == "skills" and tailored.skills:
            pdf.heading("Skills")
            _skills(pdf, tailored.skills)
        elif name == "certifications" and tailored.certifications:
            pdf.heading("Certifications")
            pdf.set_font("Helvetica", "", 9.5)
            pdf.multi_cell(0, 12, _t(", ".join(tailored.certifications)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return pdf


def render_pdf(base: Resume, tailored: TailoredResume) -> bytes:
    """Render the tailored résumé to PDF bytes."""
    return bytes(_build(base, tailored).output())


def page_count(base: Resume, tailored: TailoredResume) -> int:
    """How many pages the résumé actually renders to — measured, not estimated."""
    return _build(base, tailored).page_no()


_MIN_BULLETS = 2  # never trim an entry below this many bullets; drop whole entries instead


def _trim_once(t: TailoredResume, cut_entries: list[str]) -> bool:
    """Remove the least-relevant piece of content: one bullet from the LAST entry (entries are
    ordered most-relevant first) of the least-important section that still has more than
    _MIN_BULLETS; else a whole trailing entry (activities → projects → experience, keeping at
    least one experience). Returns False when nothing safe is left to trim."""
    for entries in (t.activities, t.projects, t.experience):
        for e in reversed(entries):
            if len(e.bullets) > _MIN_BULLETS:
                e.bullets.pop()
                return True
    if t.activities:
        e = t.activities.pop()
        cut_entries.append(f"{e.role} ({e.organization})")
        return True
    if t.projects:
        p = t.projects.pop()
        cut_entries.append(p.name)
        return True
    if len(t.experience) > 1:
        e = t.experience.pop()
        cut_entries.append(f"{e.role} at {e.organization}")
        return True
    return False


def fit_to_pages(base: Resume, tailored: TailoredResume,
                 max_pages: int = 1) -> tuple[TailoredResume, list[str]]:
    """GUARANTEE the rendered PDF fits `max_pages` by measuring it: render, count pages, trim
    the least-relevant content, re-render — zero tokens, deterministic. The count-based budget
    caps are heuristics; long bullets/skills can still spill, and auto page-break would spill
    SILENTLY to page 2. Returns (tailored, user-facing notes saying exactly what was dropped —
    silence would read as \"ignored my input\")."""
    if page_count(base, tailored) <= max_pages:
        return tailored, []
    bullets_cut, cut_entries = 0, []
    while page_count(base, tailored) > max_pages:
        before = len(cut_entries)
        if not _trim_once(tailored, cut_entries):
            return tailored, [
                f"Still over {max_pages} page(s) after trimming all entries/bullets — "
                "shorten the summary, skills, or education details to fit."]
        if len(cut_entries) == before:
            bullets_cut += 1
    dropped = ([f"{bullets_cut} bullet(s)"] if bullets_cut else []) \
        + ([f"{len(cut_entries)} entr{'y' if len(cut_entries) == 1 else 'ies'} "
            f"({', '.join(cut_entries)})"] if cut_entries else [])
    return tailored, [
        f"Trimmed to fit {max_pages} page(s) — dropped the least job-relevant "
        f"{' and '.join(dropped)}. Increase Length to include more."]

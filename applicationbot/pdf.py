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
        self.set_margins(42, 40, 42)
        self.set_auto_page_break(True, margin=40)
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


def _contact(pdf: _Resume, c: Contact):
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 24, _t(c.name), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    bits = [b for b in (c.location, c.email, c.phone) if b] + list(c.links)
    if bits:
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 13, _t("  |  ".join(bits)), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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


def render_pdf(base: Resume, tailored: TailoredResume) -> bytes:
    """Render the tailored résumé to PDF bytes."""
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

    return bytes(pdf.output())

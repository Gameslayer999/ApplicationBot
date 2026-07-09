"""ATS text-layer verification of a generated résumé PDF (decision 043).

An ATS parses the PDF's text layer, not its visual layout — so after every export we
extract that layer (pypdf, pure Python) and verify what a parser would actually see:

1. **Readability problems** — empty/corrupt text layer, or the name / email / phone not
   readable as literal text (this also catches the core-font latin-1 ``?``-mangling of
   non-Western names, a known audit gap).
2. **Keyword coverage** — of the candidate skills the JD asks for (the same token-free
   matching the discovery ranker uses, `relevance.mentions`), which made it into the PDF
   (*covered*) and which the tailoring dropped (*dropped* — in the base résumé but absent
   from the PDF). Genuine gaps — requirements the résumé never had — are the fit judge's
   `missing` list and are reported by the caller, not re-derived here.

Zero tokens, deterministic, adapted from ai-job-search's pdftotext verification step.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from . import relevance
from .models import Resume


@dataclass
class AtsReport:
    """What an ATS parser would see in the PDF."""

    problems: list[str] = field(default_factory=list)  # readability failures — fix before submitting
    covered: list[str] = field(default_factory=list)  # JD-requested skills present in the PDF text
    dropped: list[str] = field(default_factory=list)  # JD-requested, in the base résumé, NOT in the PDF

    @property
    def ok(self) -> bool:
        return not self.problems and not self.dropped

    def notes(self) -> list[str]:
        """User-facing summary lines (UI Principle #3: state the problem and the fix)."""
        out = list(self.problems)
        if self.dropped:
            out.append(
                f"ATS check: the job asks for {', '.join(self.dropped)} — you have "
                "them, but they were trimmed out of this PDF. Increase Length or "
                "re-tailor to include them."
            )
        if self.covered and not self.problems:
            out.append(
                f"ATS check passed: text layer readable; {len(self.covered)} JD "
                f"keyword(s) present ({', '.join(self.covered[:8])}"
                + (", …)" if len(self.covered) > 8 else ")")
            )
        return out


def extract_text(pdf_bytes: bytes) -> str:
    """The PDF's text layer, as an ATS parser would extract it."""
    from pypdf import PdfReader  # lazy — keep import cheap for callers that never verify

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def verify_pdf(pdf_bytes: bytes, base: Resume, jd_text: str | None = None) -> AtsReport:
    """Verify the PDF's text layer and (when a JD is given) its keyword coverage.

    `dropped` compares against the BASE résumé's skills — if the JD asks for a skill the
    candidate has and it isn't in the PDF text, tailoring cut something the ATS will
    screen for.
    """
    report = AtsReport()
    try:
        text = extract_text(pdf_bytes)
    except Exception as e:
        report.problems.append(
            f"ATS check: could not read the PDF text layer ({type(e).__name__}: {e}) — "
            "the file may be corrupt; re-export it."
        )
        return report

    low = text.lower()
    if len(low.strip()) < 40:
        report.problems.append(
            "ATS check: the PDF has no readable text layer — an ATS would see an empty "
            "résumé. Re-export it."
        )
        return report

    c = base.contact
    if c.name and c.name.lower() not in low:
        report.problems.append(
            f"ATS check: the name {c.name!r} is not readable in the PDF text — non-ASCII "
            "characters were replaced at render time (known core-font limitation); an ATS "
            "would file this under the mangled name."
        )
    if c.email and c.email.lower() not in low:
        report.problems.append(
            f"ATS check: the email {c.email!r} is not readable in the PDF text — an ATS "
            "could not contact you. Re-export the PDF."
        )
    if c.phone and _digits(c.phone) and _digits(c.phone) not in _digits(text):
        report.problems.append(
            f"ATS check: the phone number {c.phone!r} is not readable in the PDF text — "
            "an ATS could not contact you. Re-export the PDF."
        )

    if jd_text:
        jd_low = jd_text.lower()
        jd_tok = relevance.tokens(jd_low)
        pdf_tok = relevance.tokens(low)
        for term in relevance.skill_terms(base):
            if not relevance.mentions(term, jd_low, jd_tok):
                continue  # the JD doesn't ask for it — irrelevant to this application
            if relevance.mentions(term, low, pdf_tok):
                report.covered.append(term)
            else:
                report.dropped.append(term)

    return report

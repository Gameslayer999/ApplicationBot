"""Résumé-document upload → parse → merge (resume_import.py).

Text extraction is asserted for real (PDF via fpdf2's own output, DOCX via a hand-built zip);
the Claude parse step is monkeypatched to a fixed structure, so the merge/dedup/create logic is
tested exactly with no live model call."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from applicationbot import resume_import as ri
from applicationbot.models import Education, Experience, Project, SkillCategory
from applicationbot.resume import load_resume
from applicationbot.resume_import import _ParsedContact, _ParsedResume


# ----------------------------------------------------------------- text extraction

def _pdf_bytes(lines: list[str]) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for line in lines:
        pdf.cell(0, 8, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def _docx_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_extract_pdf():
    text = ri.extract_text("resume.pdf", _pdf_bytes(["Jane Doe", "Built REST services"]))
    assert "Jane Doe" in text and "Built REST services" in text


def test_extract_pdf_by_magic_without_extension():
    text = ri.extract_text("upload", _pdf_bytes(["Jane Doe"]))
    assert "Jane Doe" in text


def test_extract_docx():
    text = ri.extract_text("resume.docx", _docx_bytes(["Jane Doe", "Acme Corp"]))
    assert text.splitlines() == ["Jane Doe", "Acme Corp"]


def test_extract_plaintext():
    assert "hello" in ri.extract_text("r.txt", b"hello world")


def test_extract_unsupported_type_raises():
    with pytest.raises(ValueError, match="Unsupported file type"):
        ri.extract_text("photo.png", b"\x89PNG\r\n")


def test_extract_empty_raises():
    with pytest.raises(ValueError, match="No text could be read"):
        ri.extract_text("r.txt", b"   \n  ")


# ----------------------------------------------------------------- merge / create

def _parsed(**kw) -> _ParsedResume:
    base = dict(
        contact=_ParsedContact(name="Jane Doe", email="jane@example.com",
                               location="New York, NY", links=["github.com/janedoe"]),
        summary="Engineer.",
        skills=[SkillCategory(category="Languages", items=["Python", "Go"])],
        experience=[Experience(organization="Acme", role="Engineer", start="2023",
                               end="Present", bullets=["Built REST services"])],
        projects=[Project(name="SideProj", bullets=["A thing"])],
        education=[Education(school="MIT", degree="BS CS", graduation="2023")],
        certifications=["AWS SAA"],
    )
    base.update(kw)
    return _ParsedResume(**base)


@pytest.fixture
def target(tmp_path) -> Path:
    return tmp_path / "profile" / "resume.yaml"


def test_first_upload_creates_resume(monkeypatch, target):
    monkeypatch.setattr(ri, "extract_text", lambda *a: "text")
    monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: _parsed())

    result = ri.import_resume(target, "resume.pdf", b"%PDF-x")

    assert result["created"] is True
    assert result["added"] == {"experience": 1, "activities": 0, "projects": 1,
                               "education": 1, "skills": 2, "certifications": 1}
    r = load_resume(target)
    assert r.contact.name == "Jane Doe" and r.contact.email == "jane@example.com"
    assert [e.organization for e in r.experience] == ["Acme"]
    assert r.summary == "Engineer."


def test_reupload_same_resume_adds_nothing(monkeypatch, target):
    monkeypatch.setattr(ri, "extract_text", lambda *a: "text")
    monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: _parsed())
    ri.import_resume(target, "resume.pdf", b"%PDF-x")

    result = ri.import_resume(target, "resume.pdf", b"%PDF-x")

    assert result["created"] is False
    assert sum(result["added"].values()) == 0
    assert result["contact_filled"] == [] and result["summary_added"] is False


def test_merge_adds_new_entries_only(monkeypatch, target):
    monkeypatch.setattr(ri, "extract_text", lambda *a: "text")
    monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: _parsed())
    ri.import_resume(target, "resume.pdf", b"%PDF-x")

    second = _parsed(
        contact=_ParsedContact(phone="555-1212"),  # fills a blank field only
        experience=[Experience(organization="Beta", role="Intern", start="2022",
                               end="2022", bullets=["x"])],
        skills=[SkillCategory(category="Languages", items=["Python", "Rust"])],  # Rust is new
        projects=[], education=[], certifications=[], summary=None,
    )
    monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: second)
    result = ri.import_resume(target, "resume.pdf", b"%PDF-x")

    assert result["added"]["experience"] == 1 and result["added"]["skills"] == 1
    assert result["contact_filled"] == ["phone"]
    r = load_resume(target)
    assert [e.organization for e in r.experience] == ["Acme", "Beta"]
    assert r.contact.phone == "555-1212"
    langs = next(c for c in r.skills if c.category == "Languages")
    assert langs.items == ["Python", "Go", "Rust"]  # existing kept, only new appended


def test_existing_fields_never_overwritten(monkeypatch, target):
    monkeypatch.setattr(ri, "extract_text", lambda *a: "text")
    monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: _parsed())
    ri.import_resume(target, "resume.pdf", b"%PDF-x")

    # A second parse that reports a DIFFERENT name/email/summary for the same-keyed entries.
    conflicting = _parsed(
        contact=_ParsedContact(name="Impostor", email="evil@example.com"),
        summary="Rewritten.",
    )
    monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: conflicting)
    ri.import_resume(target, "resume.pdf", b"%PDF-x")

    r = load_resume(target)
    assert r.contact.name == "Jane Doe" and r.contact.email == "jane@example.com"
    assert r.summary == "Engineer."


def test_parse_requires_claude(monkeypatch, target):
    monkeypatch.setattr(ri, "extract_text", lambda *a: "text")
    monkeypatch.setattr(ri.backends, "claude_code_available", lambda: False)
    monkeypatch.setattr(ri.auth, "get_api_key", lambda: None)
    with pytest.raises(RuntimeError, match="needs Claude"):
        ri.import_resume(target, "resume.pdf", b"%PDF-x")

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


# ------------------------------------------------- dedupe against what the profile already has

def _seed(monkeypatch, target, **kw):
    """Create the résumé from one parse, then return a function that merges a second parse in."""
    monkeypatch.setattr(ri, "extract_text", lambda *a: "text")
    monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: _parsed(**kw))
    ri.import_resume(target, "resume.pdf", b"%PDF-x")

    def again(**kw2):
        second = dict(projects=[], education=[], certifications=[], skills=[], summary=None)
        second.update(kw2)
        monkeypatch.setattr(ri, "_parse_with_claude", lambda *a: _parsed(**second))
        return ri.import_resume(target, "resume2.pdf", b"%PDF-x")

    return again


@pytest.mark.parametrize("org,role", [
    ("Acme Corp.", "Software Engineer Intern"),   # suffix + longer title
    ("ACME, Inc.", "SWE Intern"),                 # abbreviation
    ("Acme", "Software Engineering Intern"),      # engineer/engineering
    ("Acme", "Intern"),                           # the résumé's shorter wording
])
def test_same_role_spelled_differently_is_not_duplicated(monkeypatch, target, org, role):
    again = _seed(monkeypatch, target, experience=[
        Experience(organization="Acme", role="Software Engineer Intern", start="May 2024",
                   end="Aug 2024", bullets=["Built REST services"])])

    result = again(experience=[Experience(organization=org, role=role, start="May 2024",
                                          end="Aug 2024", bullets=["Built REST services"])])

    assert result["added"]["experience"] == 0
    assert result["skipped"] + result["enriched"] == ["Acme — Software Engineer Intern"]
    assert len(load_resume(target).experience) == 1


def test_same_job_retitled_between_resume_versions_is_not_duplicated(monkeypatch, target):
    # Different title wording, but identical employer and month-precise span = one job.
    again = _seed(monkeypatch, target, experience=[
        Experience(organization="Jaguar Technologies", role="Software Engineer Intern",
                   start="May 2024", end="Apr 2025")])

    result = again(experience=[Experience(organization="Jaguar Technologies",
                                          role="Junior Software Engineer",
                                          start="May 2024", end="Apr 2025")])

    assert result["added"]["experience"] == 0
    assert len(load_resume(target).experience) == 1


def test_different_role_and_span_at_same_employer_is_kept(monkeypatch, target):
    # Same employer, overlapping year, but neither the title nor the span matches.
    again = _seed(monkeypatch, target, experience=[
        Experience(organization="Penn State", role="Research Assistant",
                   start="Sep 2024", end="May 2025")])

    result = again(experience=[Experience(organization="Penn State", role="Teaching Assistant",
                                          start="Sep 2024", end="Dec 2024")])

    assert result["added"]["experience"] == 1
    assert len(load_resume(target).experience) == 2


def test_second_stint_at_same_employer_is_kept(monkeypatch, target):
    # Same org and title, different years = a real second entry, not a re-spelling.
    again = _seed(monkeypatch, target, experience=[
        Experience(organization="Acme", role="Software Engineer Intern", start="May 2023", end="Aug 2023")])

    result = again(experience=[Experience(organization="Acme", role="Software Engineer Intern",
                                          start="May 2024", end="Aug 2024")])

    assert result["added"]["experience"] == 1
    assert [e.start for e in load_resume(target).experience] == ["May 2023", "May 2024"]


def test_role_already_under_activities_is_not_re_added_to_experience(monkeypatch, target):
    again = _seed(monkeypatch, target, experience=[], activities=[
        Experience(organization="Robotics Club", role="Team Lead", start="2023", end="2024")])

    result = again(experience=[Experience(organization="Robotics Club", role="Team Lead",
                                          start="2023", end="2024")])

    assert result["added"]["experience"] == 0
    r = load_resume(target)
    assert r.experience == [] and len(r.activities) == 1


def test_matching_entry_gains_only_its_blank_fields(monkeypatch, target):
    again = _seed(monkeypatch, target, experience=[
        Experience(organization="Acme", role="Engineer", start="", end="Present",
                   bullets=["Built REST services"])])

    result = again(experience=[Experience(organization="Acme", role="Engineer", start="Jan 2023",
                                          end="Dec 2024", location="Austin, TX")])

    assert result["added"]["experience"] == 0 and result["enriched"] == ["Acme — Engineer"]
    e = load_resume(target).experience[0]
    assert (e.start, e.location) == ("Jan 2023", "Austin, TX")
    assert e.end == "Present" and e.bullets == ["Built REST services"]  # non-blank fields untouched


def test_education_and_skill_variants_are_not_duplicated(monkeypatch, target):
    again = _seed(monkeypatch, target,
                  education=[Education(school="MIT", degree="BS Computer Science", graduation="2023")],
                  skills=[SkillCategory(category="Languages", items=["Node.js"])])

    result = again(education=[Education(school="M.I.T.", degree="B.S. Computer Science", graduation="2023")],
                   skills=[SkillCategory(category="Languages", items=["NodeJS", "Rust"])])

    assert result["added"]["education"] == 0 and result["added"]["skills"] == 1
    r = load_resume(target)
    assert len(r.education) == 1
    assert next(c for c in r.skills if c.category == "Languages").items == ["Node.js", "Rust"]


def test_parse_requires_claude(monkeypatch, target):
    monkeypatch.setattr(ri, "extract_text", lambda *a: "text")
    monkeypatch.setattr(ri.backends, "claude_code_available", lambda: False)
    monkeypatch.setattr(ri.auth, "get_api_key", lambda: None)
    with pytest.raises(RuntimeError, match="needs Claude"):
        ri.import_resume(target, "resume.pdf", b"%PDF-x")

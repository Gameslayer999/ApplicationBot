"""LinkedIn-export import (linkedin.py) — the merge into an existing résumé.

The CSVs are built by hand (that is all a LinkedIn export is), so the whole path runs for real
with no network and no model call. The point of these tests is decision 160: an export imported
on top of a résumé must not re-add roles the résumé already holds under different wording.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import yaml

from applicationbot import linkedin
from applicationbot.resume import load_resume


def _zip(**csvs: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in csvs.items():
            z.writestr(f"{name}.csv", text)
    return buf.getvalue()


POSITIONS = ("Company Name,Title,Location,Started On,Finished On,Description\n"
             "{org},{role},{loc},{start},{end},{desc}\n")


@pytest.fixture
def resume_path(tmp_path) -> Path:
    p = tmp_path / "resume.yaml"
    p.write_text(yaml.safe_dump({
        "contact": {"name": "Jane Doe", "email": "jane@example.com"},
        "experience": [{"organization": "Acme", "role": "Software Engineer Intern",
                        "start": "May 2024", "end": "Aug 2024",
                        "bullets": ["Built REST services"]}],
        "education": [{"school": "MIT", "degree": "BS Computer Science", "graduation": "2023"}],
        "skills": [{"category": "Languages", "items": ["Node.js"]}],
    }), encoding="utf-8")
    return p


def test_role_already_on_the_resume_is_not_re_added(resume_path):
    # LinkedIn's legal company name and its own title wording for the job already on file.
    data = _zip(positions=POSITIONS.format(org="Acme Corporation", role="SWE Intern",
                                           loc='"New York, NY"', start="May 2024",
                                           end="Aug 2024", desc="Built REST services"))

    result = linkedin.import_into(resume_path, "export.zip", data)

    assert result["added"]["experience"] == 0
    exp = load_resume(resume_path).experience
    assert len(exp) == 1
    assert exp[0].role == "Software Engineer Intern"          # the user's wording is kept
    assert exp[0].location == "New York, NY"                  # blank field filled from the export
    assert result["enriched"] == ["Acme — Software Engineer Intern"]


def test_genuinely_new_role_is_added(resume_path):
    data = _zip(positions=POSITIONS.format(org="Beta LLC", role="Data Analyst", loc="Remote",
                                           start="Jan 2025", end="Present", desc="Dashboards"))

    result = linkedin.import_into(resume_path, "export.zip", data)

    assert result["added"]["experience"] == 1 and result["skipped"] == []
    assert [e.organization for e in load_resume(resume_path).experience] == ["Acme", "Beta LLC"]


def test_iso_dated_export_matches_a_month_named_entry(resume_path):
    # Same job, LinkedIn's ISO dates and a re-titled role: matched on the identical span.
    data = _zip(positions=POSITIONS.format(org="Acme", role="Junior Software Engineer", loc="",
                                           start="2024-05-01", end="2024-08-31", desc=""))

    result = linkedin.import_into(resume_path, "export.zip", data)

    assert result["added"]["experience"] == 0
    assert len(load_resume(resume_path).experience) == 1


def test_education_and_skill_variants_are_not_duplicated(resume_path):
    data = _zip(education="School Name,Degree Name,End Date\nM.I.T.,B.S. Computer Science,2023\n",
                skills="Name\nNodeJS\nRust\n")

    result = linkedin.import_into(resume_path, "export.zip", data)

    assert result["added"]["education"] == 0 and result["added"]["skills"] == 1
    r = load_resume(resume_path)
    assert len(r.education) == 1
    assert [i for c in r.skills for i in c.items] == ["Node.js", "Rust"]


def test_reimporting_the_same_export_changes_nothing(resume_path):
    data = _zip(positions=POSITIONS.format(org="Beta LLC", role="Data Analyst", loc="Remote",
                                           start="Jan 2025", end="Present", desc="Dashboards"),
                education="School Name,Degree Name,End Date\nMIT,BS Computer Science,2023\n",
                skills="Name\nRust\n")
    linkedin.import_into(resume_path, "export.zip", data)
    before = resume_path.read_text()

    result = linkedin.import_into(resume_path, "export.zip", data)

    assert sum(result["added"].values()) == 0
    assert resume_path.read_text() == before

"""Per-application archive (decision 043): snapshot posting + PDF + report; freeze real
submissions. All writes go to a temp dir — the real profile/ is never touched.

Run:  python -m pytest tests/test_archive.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applicationbot import archive

META = {"company": "Acme", "role": "Platform Engineer", "location": "NYC",
        "pay": "$150k", "source_url": "https://boards.example.com/acme/123"}


@pytest.fixture
def arch(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "applications")
    return archive


def _run(arch, tmp_path, *, submitted=False, jd="We need a platform engineer."):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-fake")
    return Path(arch.archive_run("Acme", "Platform Engineer", META, jd_text=jd,
                                 pdf_path=str(pdf),
                                 report_data={"submitted": submitted, "filled": []},
                                 submitted=submitted))


def test_dry_run_snapshots_posting_pdf_and_report(arch, tmp_path):
    dest = _run(arch, tmp_path)
    assert (dest / "posting.md").read_text().startswith("# Platform Engineer — Acme")
    assert "We need a platform engineer." in (dest / "posting.md").read_text()
    assert "https://boards.example.com/acme/123" in (dest / "posting.md").read_text()
    assert (dest / "resume.pdf").read_bytes() == b"%PDF-fake"
    assert json.loads((dest / "report.json").read_text())["submitted"] is False
    assert not list(dest.glob("submitted-*"))


def test_dry_run_overwrites_same_posting_dir(arch, tmp_path):
    d1 = _run(arch, tmp_path, jd="first fetch")
    d2 = _run(arch, tmp_path, jd="second fetch")
    assert d1 == d2
    assert "second fetch" in (d2 / "posting.md").read_text()
    assert len(list(d2.parent.iterdir())) == 1  # still one dir per posting


def test_submission_freezes_a_dated_copy_never_reused(arch, tmp_path):
    dest = _run(arch, tmp_path, submitted=True)
    frozen = sorted(dest.glob("submitted-*"))
    assert len(frozen) == 1
    assert (frozen[0] / "resume.pdf").read_bytes() == b"%PDF-fake"
    # A same-day resubmission gets its own suffixed dir; the first copy is untouched.
    marker = frozen[0] / "posting.md"
    before = marker.read_text()
    _run(arch, tmp_path, submitted=True, jd="resubmitted with different text")
    frozen2 = sorted(dest.glob("submitted-*"))
    assert len(frozen2) == 2
    assert marker.read_text() == before


def test_missing_pdf_and_empty_jd_still_archive(arch, tmp_path):
    dest = Path(arch.archive_run("Acme", "Platform Engineer", META, jd_text="",
                                 pdf_path=str(tmp_path / "nope.pdf"),
                                 report_data=None, submitted=False))
    assert "(no posting text captured)" in (dest / "posting.md").read_text()
    assert not (dest / "resume.pdf").exists()
    assert not (dest / "report.json").exists()

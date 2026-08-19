"""Exporting the portable setup as one .zip (decision 188).

Two failure modes are worth pinning hard. **A leak**: the export is an allowlist precisely so a
config file added later can't ride along, and `mailbox.yaml` / `safety.yaml` must never be in the
archive — one carries the inbox link, the other the arming switch. **A silent drop**: the user may
keep several résumés and pick between them on the Profile page, so an export that took only
`resume.yaml` would hand them a backup missing work they'd only notice was gone on the new machine.
"""
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from applicationbot import profile_export, web

SAMPLE_RESUME = Path(web.__file__).resolve().parent.parent / "examples" / "sample_resume.yaml"


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    """A profile/ folder holding one of everything — the exportable, the excluded, and the junk."""
    p = tmp_path / "profile"
    (p / "uploads").mkdir(parents=True)
    (p / "application_profile.yaml").write_text("first_name: Jordan\nemail: j@example.com\n")
    (p / "discovery.yaml").write_text("roles: [Software Engineer]\n")
    shutil.copy(SAMPLE_RESUME, p / "resume.yaml")
    shutil.copy(SAMPLE_RESUME, p / "resume-backend.yaml")          # a second résumé to pick between
    (p / "mailbox.yaml").write_text("email: bot@example.com\nhost: imap.example.com\n")
    (p / "safety.yaml").write_text("armed: true\nmax_submissions_per_run: 100\n")
    (p / "notifications.yaml").write_text("ntfy:\n  topic: secret-topic\n")
    (p / "uploads" / "resume-abc123.pdf").write_bytes(b"%PDF-1.4 fake")
    (p / "uploads" / "resume-abc123.pdf.meta").write_text('{"filename": "resume.pdf"}')
    (p / "discovery_cache.json").write_text("{}")                  # regenerable
    (p / "fit_history.jsonl").write_text("{}\n")                   # this machine's record
    (p / "applications").mkdir()
    (p / "applications" / "run.json").write_text("{}")
    return p


def _names(profile_dir: Path) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(profile_export.build_zip(profile_dir))).namelist()


def test_exports_the_setup_and_nothing_else(profile_dir):
    assert sorted(_names(profile_dir)) == sorted([
        "manifest.json",
        "application_profile.yaml",
        "discovery.yaml",
        "resume.yaml",
        "resume-backend.yaml",
        "uploads/resume-abc123.pdf",
        "uploads/resume-abc123.pdf.meta",
    ])


@pytest.mark.parametrize("secret", ["mailbox.yaml", "safety.yaml", "notifications.yaml"])
def test_local_and_credential_files_never_travel(profile_dir, secret):
    blob = profile_export.build_zip(profile_dir)
    assert secret not in _names(profile_dir)
    # Not merely absent from the listing — its contents are nowhere in the archive.
    assert b"imap.example.com" not in blob and b"secret-topic" not in blob
    assert b"armed" not in blob


def test_history_and_caches_are_left_behind(profile_dir):
    names = _names(profile_dir)
    assert not [n for n in names if n.startswith("applications/")]
    assert "discovery_cache.json" not in names and "fit_history.jsonl" not in names


def test_every_resume_is_exported_and_config_yaml_is_not_mistaken_for_one(profile_dir):
    files = {p.name for p in profile_export.resume_files(profile_dir)}
    assert files == {"resume.yaml", "resume-backend.yaml"}


def test_layout_mirrors_profile_so_unzipping_into_it_restores(profile_dir):
    # The whole restore instruction in the UI depends on this: résumés at the top level (where
    # the app reads them), uploads under uploads/. A tidier resumes/ subfolder would break it.
    names = _names(profile_dir)
    assert "resume.yaml" in names
    assert not [n for n in names if n.startswith("resumes/")]
    assert all(n.startswith("uploads/") for n in names if n.endswith((".pdf", ".meta")))


def test_manifest_records_the_version_contents_and_what_was_dropped(profile_dir):
    z = zipfile.ZipFile(io.BytesIO(profile_export.build_zip(profile_dir)))
    m = json.loads(z.read("manifest.json"))
    assert m["format"] == profile_export.FORMAT
    assert m["app_version"] and m["exported_at"]
    assert set(m["contents"]) == set(z.namelist()) - {"manifest.json"}
    # The exclusions are shipped with a reason each, so a backup never silently omits something.
    assert "mailbox.yaml" in m["excluded"] and m["excluded"]["mailbox.yaml"]


def test_a_profile_that_does_not_exist_yet_errors_instead_of_zipping_nothing(tmp_path):
    (tmp_path / "profile").mkdir()
    with pytest.raises(FileNotFoundError) as e:
        profile_export.build_zip(tmp_path / "profile")
    assert "application_profile.yaml" in str(e.value)
    assert "Save profile" in str(e.value)      # states the fix, not just the fault


@pytest.mark.parametrize("exists", [True, False])
def test_export_to_a_directory_uses_the_dated_filename(profile_dir, tmp_path, exists):
    dest = tmp_path / "backups"
    if exists:
        dest.mkdir()
    out = profile_export.export_to(dest, profile_dir)
    assert out.name == profile_export.filename() and out.parent.name == "backups"
    assert zipfile.is_zipfile(out)


def test_export_to_a_named_file_keeps_that_name(profile_dir, tmp_path):
    out = profile_export.export_to(tmp_path / "mine.zip", profile_dir)
    assert out.name == "mine.zip" and zipfile.is_zipfile(out)


# ---- the Profile tab's button ----

def test_the_route_is_served_and_returns_a_zip_attachment():
    body = web.INDEX_HTML
    src = Path(web.__file__).read_text()
    assert 'if path == "/profile/export":' in src
    assert 'application/zip' in src and 'Content-Disposition' in src
    assert 'id="export-profile"' in body and 'fetch("/profile/export")' in body


def test_the_button_says_what_it_downloads_and_how_to_restore_it():
    body = web.INDEX_HTML
    assert "Download profile (.zip)" in body
    assert "unzip the file into <code>profile/</code>" in body
    assert "Left out on purpose:" in body           # the drop is stated (UI Principle #5)
    assert '["s-export","Back up / move"]' in body  # reachable from the section-jump nav

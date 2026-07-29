"""Editing an answer in Review changes what gets submitted (decision 153).

Covers the whole path a user's edit takes: saved against the posting → loaded onto the resolver
→ returned instead of the profile's own answer (even for a field the ATS prefilled) → shown back
in the review panel as the value that will be submitted.
"""
from __future__ import annotations

import json

import pytest

from applicationbot import answer_overrides
from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Resume

POSTING = ("Acme", "Backend Engineer", "https://example.invalid/jobs/7")


@pytest.fixture(autouse=True)
def archive_root(tmp_path, monkeypatch):
    monkeypatch.setattr("applicationbot.archive.ARCHIVE_DIR", tmp_path / "applications")
    return tmp_path


def _resolver(**overrides) -> AnswerResolver:
    return AnswerResolver(
        resume=Resume(contact=Contact(name="Jane Doe", email="jane@example.com")),
        profile=ApplicationProfile(first_name="Jane", email="jane@example.com",
                                   open_to_remote=True),
        overrides=overrides,
    )


def test_save_load_roundtrip_and_blank_clears():
    answer_overrides.save(*POSTING, {"Preferred name": "Janey", "Why us?": "Because."})
    assert answer_overrides.load(*POSTING) == {"Preferred name": "Janey", "Why us?": "Because."}

    # A blank value removes that one override; the rest survive.
    answer_overrides.save(*POSTING, {"Preferred name": "  "})
    assert answer_overrides.load(*POSTING) == {"Why us?": "Because."}

    # Clearing the last one deletes the file rather than leaving an empty dict on disk.
    answer_overrides.save(*POSTING, {"Why us?": ""})
    assert answer_overrides.load(*POSTING) == {}
    assert not answer_overrides.path_for(*POSTING).is_file()


def test_load_survives_a_corrupt_file():
    p = answer_overrides.path_for(*POSTING)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert answer_overrides.load(*POSTING) == {}


def test_edited_answer_beats_the_profile_and_matches_a_reworded_label():
    r = _resolver(**{"Email": "contact-me@example.com", "Are you open to remote?": "No"})
    assert r.resolve("Email") == "contact-me@example.com"
    # Label matching is normalised — the ATS may render it with different case/punctuation.
    assert r.resolve("E-mail *") == "contact-me@example.com"
    # An edit outranks the profile's own answer (open_to_remote=True would have said "Yes").
    assert r.resolve("Are you open to remote?") == "No"
    # Fields with no edit are unaffected.
    assert r.resolve("First name") == "Jane"


def test_freetext_answer_uses_the_edit_without_calling_claude():
    r = _resolver(**{"Why do you want to work here?": "I use your product daily."})
    r.enable_generation = False  # no Claude — the edit alone must answer it
    assert r.freetext_answer("Why do you want to work here?", is_textarea=True) == (
        "I use your product daily.", "resolver")


def test_blank_override_is_not_an_answer():
    r = _resolver(**{"Email": "   "})
    assert r.override_for("Email") is None
    assert r.resolve("Email") == "jane@example.com"


def test_run_apply_loads_the_postings_edits_onto_the_resolver():
    from applicationbot.apply import _load_overrides

    answer_overrides.save(*POSTING, {"Preferred name": "Janey"})
    r = _resolver()
    _load_overrides(r, {"company": "Acme", "role": "Backend Engineer",
                        "source_url": POSTING[2]}, "", "")
    assert r.resolve("Preferred name") == "Janey"

    # No posting identity (a bare CLI run) → nothing to load, and no crash.
    r2 = _resolver()
    _load_overrides(r2, None, "Acme", "Backend Engineer")
    assert r2.overrides == {}


def test_review_panel_shows_the_edit_and_offers_unanswered_fields(monkeypatch, tmp_path):
    """_review_data merges the edits over the recorded fill and exposes unanswered fields as
    editable rows — the panel must show what WILL be submitted, not the superseded original."""
    from applicationbot import archive, web

    app = {"id": 3, "company": "Acme", "role": "Backend Engineer", "source_url": POSTING[2],
           "location": "Remote", "remote": "yes", "pay": "", "portal": "lever",
           "fit_score": 90, "status": "dry-run", "resume_source": "Tailored fresh",
           "resume_path": ""}
    monkeypatch.setattr(web.tracker, "get_application", lambda aid: app if aid == 3 else None)

    adir = archive.dir_for(app["company"], app["role"], app["source_url"])
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "report.json").write_text(json.dumps({
        "when": "2026-07-29T10:00:00",
        "filled": [{"label": "Email", "value": "jane@example.com", "control": "text"},
                   {"label": "Resume", "value": "/tmp/tailored.pdf", "control": "file"}],
        "skipped": ["Why Acme? — no saved answer",
                    "Why Acme? — REQUIRED, not filled (no matching answer or unsupported field)",
                    "[answer bank] 2 questions captured"],
    }), encoding="utf-8")
    answer_overrides.save(*POSTING, {"Email": "contact-me@example.com"})

    r = web._review_data(3)
    assert r["filled"][0] == {"label": "Email", "value": "contact-me@example.com",
                              "control": "text", "edited": True}
    # The résumé upload is never rewritten by an edit — it shows the file name.
    assert r["filled"][1]["value"] == "tailored.pdf"
    # One row per unanswered label (deduped), the bracketed learning line dropped.
    assert r["unanswered"] == [{"label": "Why Acme?", "detail": "no saved answer",
                                "value": "", "edited": False}]

    # Saving through the web layer writes the same store the next fill reads.
    assert web.save_answers(3, {"Why Acme?": "Your latency work."}) == {"ok": True, "saved": 2}
    assert answer_overrides.load(*POSTING)["Why Acme?"] == "Your latency work."
    assert web._review_data(3)["unanswered"][0]["value"] == "Your latency work."

    assert web.save_answers(99, {"a": "b"})["ok"] is False
    assert web.save_answers(3, {})["ok"] is False

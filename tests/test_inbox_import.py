"""Inbox → tracker import (decision 151) — no network, no Claude, no real inbox.

Covers all three stages: the free gate (`classify_message` on forwarded mail), the
match-or-insert writer (`apply_extraction`), and `run_import` end-to-end with the mailbox
reader and the extractor injected. The Claude call itself is stubbed — what is pinned here is
the CONTRACT around it: which emails are worth a token, what a given extraction does to the
tracker, that a re-run is idempotent, and that an import can be undone exactly.

Run:  python -m pytest tests/test_inbox_import.py
"""
from __future__ import annotations

from email.message import EmailMessage

from applicationbot import inbox_import, mailbox, tracker
from applicationbot.mailbox import MailboxConfig

# A Gmail-style forward: the envelope From is the USER, the employer is in the forwarded block.
_FORWARDED_CONFIRMATION = """\
---------- Forwarded message ---------
From: Acme Careers <no-reply@greenhouse.io>
Date: Mon, 6 Jul 2026 09:14:00 -0400
Subject: Thank you for applying to Acme
To: <me@personal.example>

Hi Gabriel,

Thank you for applying to the Backend Engineer role at Acme. Our team is reviewing your
application and will be in touch.

— The Acme Recruiting Team
"""

_FORWARDED_REJECTION = """\
---------- Forwarded message ---------
From: Globex Talent <talent@globex.example>
Date: Fri, 17 Jul 2026 11:02:00 -0400
Subject: Your application to Globex

Unfortunately we have decided to move forward with other candidates for the Data Analyst
position. We appreciate your interest in Globex.
"""

_NEWSLETTER = """\
This week in tech: five stories you missed, plus our favourite gadgets.
Manage your subscription preferences at any time.
"""

_LINKEDIN_ALERT = """\
---------- Forwarded message ---------
From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>
Subject: 12 new jobs for Software Engineer

<a href="https://www.linkedin.com/comm/jobs/view/123">Software Engineer at Initech</a>
"""


def _rec(body: str, *, mid: str, subject: str = "Fwd: update", date: str = "2026-07-20",
         sender: str = "me@personal.example") -> dict:
    return {"message_id": mid, "sender": sender, "subject": subject, "date": date, "body": body}


# ------------------------------------------------------------------- stage 1: the free gate

def test_effective_sender_reads_through_the_forward():
    # The envelope From is the user; the employer is the forwarded header. Getting this wrong
    # makes every imported row look like it came from the user's own address.
    rec = _rec(_FORWARDED_CONFIRMATION, mid="<1>")
    assert inbox_import.effective_sender(rec) == "no-reply@greenhouse.io"
    assert inbox_import.original_subject(rec) == "Thank you for applying to Acme"


def test_subject_falls_back_to_the_envelope_minus_fwd_prefixes():
    rec = _rec("no forwarded headers here", mid="<2>", subject="Fwd: Re: Your application")
    assert inbox_import.original_subject(rec) == "Your application"


def test_gate_admits_ats_sender_and_application_wording():
    bucket, portal = inbox_import.classify_message(_rec(_FORWARDED_CONFIRMATION, mid="<3>"))
    assert (bucket, portal) == ("candidate", "greenhouse")
    # No known ATS domain — admitted on wording alone, with no portal to stamp.
    bucket, portal = inbox_import.classify_message(_rec(_FORWARDED_REJECTION, mid="<4>"))
    assert (bucket, portal) == ("candidate", "")


def test_gate_rejects_a_newsletter_before_spending_a_token():
    bucket, why = inbox_import.classify_message(_rec(_NEWSLETTER, mid="<5>"))
    assert bucket == "skip" and "no application wording" in why


def test_gate_routes_job_alerts_to_discovery_not_the_importer():
    # Alert emails are new OPENINGS (decision 132's EmailAlertSource), not applications —
    # they must never reach the extractor or the tracker.
    bucket, provider = inbox_import.classify_message(_rec(_LINKEDIN_ALERT, mid="<6>"))
    assert (bucket, provider) == ("alert", "linkedin")


# ------------------------------------------------------- stage 3: matching, insert, update

def _ext(**kw) -> dict:
    base = {"is_application": True, "kind": "confirmation", "company": "Acme",
            "role": "Backend Engineer", "confidence": 90}
    return {**base, **kw}


def test_confirmation_with_no_match_creates_a_flagged_row(tmp_path):
    db = tmp_path / "t.db"
    act = inbox_import.apply_extraction(
        _rec(_FORWARDED_CONFIRMATION, mid="<7>"), _ext(date="2026-07-06"),
        portal="greenhouse", path=db)
    assert act["action"] == "created"
    row = tracker.get_application(act["application_id"], path=db)
    assert row["company"] == "Acme" and row["role"] == "Backend Engineer"
    assert row["status"] == "applied" and row["date_applied"] == "2026-07-06"
    assert row["method"] == "email-import" and row["portal"] == "greenhouse"
    assert "[email-import]" in row["notes"] and "no-reply@greenhouse.io" in row["notes"]


def test_outcome_email_updates_the_matching_row_instead_of_duplicating(tmp_path):
    db = tmp_path / "t.db"
    app_id = tracker.add_application(
        {"company": "Acme Inc.", "role": "Backend Engineer, Platform", "status": "applied"}, path=db)
    act = inbox_import.apply_extraction(
        _rec(_FORWARDED_REJECTION, mid="<8>"),
        _ext(kind="rejection", company="Acme", role="Backend Engineer"), path=db)
    # "Acme Inc." ≈ "Acme" (legal suffix dropped); role matches on token overlap.
    assert act["action"] == "updated" and act["application_id"] == app_id
    assert tracker.get_application(app_id, path=db)["status"] == "rejected"
    assert len(tracker.list_applications(path=db)) == 1


def test_status_only_moves_forward(tmp_path):
    db = tmp_path / "t.db"
    app_id = tracker.add_application(
        {"company": "Acme", "role": "Backend Engineer", "status": "interview"}, path=db)
    # A confirmation email read AFTER an interview invite must not reset the row.
    act = inbox_import.apply_extraction(_rec(_FORWARDED_CONFIRMATION, mid="<9>"), _ext(), path=db)
    assert act["action"] == "unchanged"
    assert tracker.get_application(app_id, path=db)["status"] == "interview"
    # …but a rejection after an interview is the normal path and must land.
    act = inbox_import.apply_extraction(
        _rec(_FORWARDED_REJECTION, mid="<10>"), _ext(kind="rejection"), path=db)
    assert act["action"] == "updated"
    assert tracker.get_application(app_id, path=db)["status"] == "rejected"


def test_source_url_matches_a_bot_made_row_across_a_different_company_spelling(tmp_path):
    db = tmp_path / "t.db"
    app_id = tracker.add_application(
        {"company": "Acme Corporation", "role": "SWE", "status": "applied",
         "source_url": "https://boards.greenhouse.io/acme/jobs/42"}, path=db)
    act = inbox_import.apply_extraction(
        _rec(_FORWARDED_REJECTION, mid="<11>"),
        _ext(kind="rejection", company="ACME (US)", role="Software Engineer II",
             url="https://boards.greenhouse.io/acme/jobs/42?utm_source=email"), path=db)
    assert act["action"] == "updated" and act["application_id"] == app_id


def test_low_confidence_and_non_applications_write_nothing(tmp_path):
    db = tmp_path / "t.db"
    rec = _rec(_FORWARDED_CONFIRMATION, mid="<12>")
    assert inbox_import.apply_extraction(rec, _ext(confidence=20), path=db)["action"] == "skipped"
    assert inbox_import.apply_extraction(
        rec, _ext(is_application=False), path=db)["action"] == "skipped"
    assert inbox_import.apply_extraction(rec, _ext(company=""), path=db)["action"] == "skipped"
    assert tracker.list_applications(path=db) == []


def test_rejection_with_no_prior_row_still_records_the_application(tmp_path):
    # An outcome email proves an application was submitted even if we never saw its confirmation —
    # but the email's date is the OUTCOME's date, so date_applied stays blank.
    db = tmp_path / "t.db"
    act = inbox_import.apply_extraction(
        _rec(_FORWARDED_REJECTION, mid="<13>"),
        _ext(kind="rejection", company="Globex", role="Data Analyst", date="2026-07-17"), path=db)
    row = tracker.get_application(act["application_id"], path=db)
    assert act["action"] == "created" and row["status"] == "rejected"
    assert row["date_applied"] == "" and row["date_discovered"] == "2026-07-17"


# ---------------------------------------------------------------- end-to-end orchestration

def _fake_extract(records: list[dict]) -> dict[int, dict]:
    """Stand-in for the Haiku call: label by the forwarded subject line."""
    out = {}
    for i, r in enumerate(records):
        subj = inbox_import.original_subject(r).lower()
        if "thank you for applying" in subj:
            out[i] = _ext(date="2026-07-06")
        elif "your application" in subj:
            out[i] = _ext(kind="rejection", company="Globex", role="Data Analyst",
                          date="2026-07-17")
        else:
            out[i] = _ext(is_application=False, kind="other", confidence=10)
    return out


def _run(tmp_path, records, **kw):
    return inbox_import.run_import(
        MailboxConfig(host="imap.example", email="bot@example"),
        path=tmp_path / "t.db", ledger_path=tmp_path / "ledger.json",
        _fetch=lambda cfg, **k: records, _extract=_fake_extract, **kw)


def test_run_import_creates_updates_and_reports(tmp_path):
    records = [
        _rec(_FORWARDED_CONFIRMATION, mid="<a>", subject="Fwd: Thank you for applying to Acme"),
        _rec(_FORWARDED_REJECTION, mid="<b>", subject="Fwd: Your application to Globex"),
        _rec(_NEWSLETTER, mid="<c>", subject="Fwd: This week in tech"),
        _rec(_LINKEDIN_ALERT, mid="<d>", subject="Fwd: 12 new jobs"),
    ]
    out = _run(tmp_path, records)
    assert out["scanned"] == 4 and out["examined"] == 2
    assert out["not_applications"] == 1            # the newsletter never reached Claude
    assert out["alerts"] == {"linkedin": 1}        # the alert went to discovery's bucket
    assert len(out["created"]) == 2 and out["errors"] == []
    rows = tracker.list_applications(path=tmp_path / "t.db")
    assert {r["company"] for r in rows} == {"Acme", "Globex"}
    assert all(r["method"] == "email-import" for r in rows)
    assert "2 new application(s)" in out["message"]


def test_rerunning_imports_nothing_twice(tmp_path):
    records = [_rec(_FORWARDED_CONFIRMATION, mid="<a>",
                    subject="Fwd: Thank you for applying to Acme")]
    _run(tmp_path, records)
    again = _run(tmp_path, records)
    assert again["already_imported"] == 1 and again["examined"] == 0 and again["created"] == []
    assert len(tracker.list_applications(path=tmp_path / "t.db")) == 1


def test_undo_deletes_created_rows_and_restores_updated_ones(tmp_path):
    db = tmp_path / "t.db"
    app_id = tracker.add_application(
        {"company": "Globex", "role": "Data Analyst", "status": "applied"}, path=db)
    out = _run(tmp_path, [
        _rec(_FORWARDED_CONFIRMATION, mid="<a>", subject="Fwd: Thank you for applying to Acme"),
        _rec(_FORWARDED_REJECTION, mid="<b>", subject="Fwd: Your application to Globex"),
    ])
    assert len(out["created"]) == 1 and out["updated"] == [app_id]
    res = inbox_import.undo_run(out["run_id"], path=db, ledger_path=tmp_path / "ledger.json")
    assert res == {"deleted": 1, "restored": 1, "missing": 0}
    assert tracker.get_application(app_id, path=db)["status"] == "applied"
    assert len(tracker.list_applications(path=db)) == 1
    # Undone messages leave the ledger, so a re-import can pick them up again.
    assert inbox_import.seen_message_ids(path=tmp_path / "ledger.json") == set()


def test_no_linked_inbox_reports_the_fix_and_writes_nothing(tmp_path, monkeypatch):
    # config=None means "use the linked inbox" — with none linked, the run must stop and say so.
    monkeypatch.setattr(mailbox, "load_config", lambda *a, **k: None)
    out = inbox_import.run_import(None, path=tmp_path / "t.db",
                                  ledger_path=tmp_path / "ledger.json",
                                  _fetch=lambda cfg, **k: [], _extract=_fake_extract)
    # UI Principle #3: the message names the blocker AND where to fix it.
    assert "No inbox is linked" in out["message"] and "Profile tab" in out["message"]
    assert out["created"] == [] and not (tmp_path / "t.db").exists()


def test_a_failed_claude_batch_leaves_the_messages_for_the_next_run(tmp_path):
    def boom(records):
        raise RuntimeError("Claude usage limit hit")

    records = [_rec(_FORWARDED_CONFIRMATION, mid="<a>",
                    subject="Fwd: Thank you for applying to Acme")]
    out = inbox_import.run_import(
        MailboxConfig(host="imap.example", email="bot@example"), path=tmp_path / "t.db",
        ledger_path=tmp_path / "ledger.json", _fetch=lambda cfg, **k: records, _extract=boom)
    assert out["created"] == [] and len(out["errors"]) == 1
    assert "usage limit" in out["errors"][0] and "run the import again" in out["errors"][0]
    assert inbox_import.seen_message_ids(path=tmp_path / "ledger.json") == set()


# ------------------------------------------------------------------ the mailbox reader

class _FakeIMAP:
    """Minimal imaplib stand-in: one message, so `fetch_messages` can be driven with no network."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.selected = ""

    def select(self, box):
        self.selected = box

    def search(self, charset, *criteria):
        return "OK", [b"1"]

    def fetch(self, mid, spec):
        return "OK", [(b"1 (RFC822 {n}", self.raw)]

    def logout(self):
        pass


def test_fetch_messages_keeps_headers_that_fetch_alerts_drops():
    msg = EmailMessage()
    msg["From"] = "Me <me@personal.example>"
    msg["Subject"] = "Fwd: Thank you for applying to Acme"
    msg["Message-ID"] = "<abc@mail>"
    msg["Date"] = "Mon, 6 Jul 2026 09:14:00 -0400"
    msg.set_content(_FORWARDED_CONFIRMATION)
    recs = mailbox.fetch_messages(MailboxConfig(host="h", email="e", password="p"),
                                  _connect=lambda cfg: _FakeIMAP(msg.as_bytes()))
    assert len(recs) == 1
    r = recs[0]
    assert r["message_id"] == "<abc@mail>" and r["date"] == "2026-07-06"
    assert r["subject"] == "Fwd: Thank you for applying to Acme"
    assert "no-reply@greenhouse.io" in r["body"]
    # And the gate reads the employer through it, not the forwarder.
    assert inbox_import.classify_message(r) == ("candidate", "greenhouse")


def test_fetch_messages_returns_empty_when_the_inbox_cannot_be_opened():
    def refuse(cfg):
        raise OSError("connection refused")

    assert mailbox.fetch_messages(MailboxConfig(host="h", email="e", password="p"),
                                  _connect=refuse) == []

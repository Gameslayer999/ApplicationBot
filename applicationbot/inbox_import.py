"""Turn application emails in the linked inbox into tracker rows (decision 151).

The tracker only ever knew about applications *this bot* made. Everything applied to by hand —
on LinkedIn, on a company site, before ApplicationBot existed — was invisible, so the funnel and
the outcome calibration (decision 043) were built on a fraction of the real history. This module
closes that: forward those emails into the already-linked bot inbox (mailbox.py) and every
"thank you for applying" becomes a row, every rejection / interview invite / recruiter reply
moves an existing row's status.

Three-stage pipeline, cheapest first (the two-stage pattern of decision 124):

1. **Free gate** (`classify_message`) — a forwarded email's ``From`` is *you*, not the employer,
   so the real sender is dug out of the forwarded header block in the body (`effective_sender`).
   A message is only worth a token if it comes from a known ATS domain **or** carries
   application language. Job-alert emails are recognized here and routed OUT (they are new
   *openings*, discovery's job via `EmailAlertSource`, decision 132) — never sent to Claude.
2. **Haiku extraction** (`extract_batch`) — survivors go to the cheap model in batches, which
   returns company / role / kind / date per message as schema-enforced JSON.
3. **Match-or-insert** (`apply_extraction`) — an extraction that matches an existing application
   (by source URL, else normalized company + role) updates its status; one that matches nothing
   inserts a new row. Status only ever moves FORWARD (`_RANK`) so a stale confirmation email
   can't knock an interview back to `applied`; a rejection wins over anything but an offer.

**Direct write, flagged, undoable.** Rows land in the tracker immediately — this is a local DB
write, nothing outward-facing — carrying ``method="email-import"`` and the source email quoted in
``notes``, so a wrong one is obvious. Every run records what it did in the ledger, and `undo_run`
reverses it exactly: inserted rows are deleted, updated rows are restored to their previous status.

The ledger (`profile/inbox_import_seen.json`, git-ignored — it names the companies you applied to,
Guideline #12) also makes re-scanning idempotent: a Message-ID already processed is never charged
for or written twice.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .paths import DATA_ROOT

LEDGER_PATH = DATA_ROOT / "profile" / "inbox_import_seen.json"
_LEDGER_VERSION = 1

MODEL = "haiku"  # extraction is a short read-and-label task — the cheap model is enough
BATCH_SIZE = 5  # emails per Claude call; each is truncated to _BODY_CHARS
_BODY_CHARS = 2000

# ATS / recruiting-mail domains. A message from one of these is application-related on sender
# alone, even when its wording is unusual. The value is the portal name stamped on a new row.
_ATS_DOMAINS: dict[str, str] = {
    "greenhouse.io": "greenhouse", "lever.co": "lever", "ashbyhq.com": "ashby",
    "myworkday.com": "workday", "workday.com": "workday", "icims.com": "icims",
    "smartrecruiters.com": "smartrecruiters", "successfactors.com": "successfactors",
    "taleo.net": "taleo", "workable.com": "workable", "jobvite.com": "jobvite",
    "bamboohr.com": "bamboohr", "breezy.hr": "breezy", "recruitee.com": "recruitee",
    "teamtailor.com": "teamtailor", "paylocity.com": "paylocity", "dayforcehcm.com": "dayforce",
    "jazz.co": "jazzhr", "applytojob.com": "jazzhr", "hire.lever.co": "lever",
}

# Application language. Deliberately broad — a false positive costs one cheap Claude call, which
# then rejects it (`is_application: false`); a false negative silently loses an application.
_APPLICATION_MARKERS = re.compile(
    r"thank(?:s| you)[^.]{0,40}\bapply|thank(?:s| you)[^.]{0,40}\bapplication|"
    r"\bapplication (?:was |has been |is )?(?:received|submitted|complete|under review)|"
    r"\b(?:we|I) received your application|\bapplied (?:to|for)\b|"
    r"\byour application (?:to|for|at|with)\b|\bapplication for the\b|"
    r"\bmoving forward\b|\bnot (?:be )?(?:moving|proceeding|selecting)\b|"
    r"\bunfortunately\b[^.]{0,60}\b(?:position|role|application|candidate)|"
    r"\bother candidates\b|\bpursue other\b|\bnot to move forward\b|"
    r"\binterview\b|\bschedule (?:a |some )?(?:time|call|chat)\b|"
    r"\bnext steps?\b|\brecruit(?:er|ing team)\b|\bhiring team\b|\bjob offer\b|\boffer letter\b",
    re.I)

# A forwarded message's original headers, as every mail client writes them into the body.
_FWD_FROM = re.compile(r"^\s*(?:>\s*)?(?:From|De|Von|Van|Da)\s*:\s*(.+)$", re.M)
_FWD_SUBJECT = re.compile(r"^\s*(?:>\s*)?(?:Subject|Asunto|Betreff|Onderwerp|Oggetto)\s*:\s*(.+)$", re.M)
_FWD_DATE = re.compile(r"^\s*(?:>\s*)?(?:Date|Sent|Fecha|Datum|Data)\s*:\s*(.+)$", re.M)
_EMAIL_IN = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

# What each extracted `kind` means for the row's status.
_KIND_STATUS = {
    "confirmation": "applied", "rejection": "rejected", "interview": "interview",
    "offer": "offer", "reply": "responded",
}
# Lifecycle order, so an import only ever moves a row FORWARD (see `_should_update`).
_RANK = {"discovered": 0, "tailored": 1, "dry-run": 2, "blocked": 2, "failed": 2,
         "applied": 3, "no-response": 3, "responded": 4, "interview": 5, "offer": 7,
         "rejected": 6}

_SYSTEM = (
    "You read emails a job applicant received and label each one. For EACH email decide whether "
    "it concerns a job application THIS PERSON submitted, and if so extract the employer, the "
    "role title, and what the email is: confirmation (the application was received), rejection "
    "(they are not proceeding), interview (an interview or screening call is offered/scheduled), "
    "offer (a job offer), or reply (a recruiter replied, anything else that is not the four "
    "above). Emails that are job ALERTS, newsletters, job-board digests, marketing, or account/"
    "security notices are NOT applications — set is_application false for those. The email may be "
    "a forwarded copy: read the forwarded headers inside the body for the true sender and date. "
    "Never invent a company or role; leave a field empty when the email does not state it."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "is_application": {"type": "boolean"},
                    "kind": {"type": "string",
                             "enum": ["confirmation", "rejection", "interview", "offer", "reply", "other"]},
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "location": {"type": "string"},
                    "date": {"type": "string"},
                    "url": {"type": "string"},
                    "confidence": {"type": "integer"},
                },
                "required": ["index", "is_application", "kind", "company", "role", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

# Below this the extraction is too unsure to write a row from — counted and reported, not written.
MIN_CONFIDENCE = 60


# ------------------------------------------------------------------ stage 1: the free gate

def effective_sender(record: dict) -> str:
    """The address the message really came FROM. For a forwarded email that is the original
    sender in the body's forwarded header block — the envelope ``From`` is whoever forwarded it
    (usually the user), which would make every import look like it came from themselves."""
    body = record.get("body") or ""
    for line in _FWD_FROM.findall(body)[:3]:  # a forward chain repeats the header; take the first
        m = _EMAIL_IN.search(line)
        if m:
            return m.group(0).lower()
    m = _EMAIL_IN.search(record.get("sender") or "")
    return m.group(0).lower() if m else (record.get("sender") or "").strip().lower()


def original_subject(record: dict) -> str:
    """The forwarded message's own Subject if the body carries one, else the envelope Subject
    minus any ``Fwd:`` prefixes."""
    for line in _FWD_SUBJECT.findall(record.get("body") or "")[:3]:
        s = line.strip()
        if s:
            return s
    return re.sub(r"^(?:(?:fwd?|re|tr|wg|aw)\s*:\s*)+", "", (record.get("subject") or ""),
                  flags=re.I).strip()


def portal_for(sender: str, body: str = "") -> str:
    """The ATS this message came from ('' if none recognized), from the sender domain first and
    any ATS link in the body second — a confirmation is often sent from the employer's own domain
    but still links back to the ATS."""
    hay = sender.lower()
    for domain, portal in _ATS_DOMAINS.items():
        if domain in hay:
            return portal
    hay = (body or "").lower()
    for domain, portal in _ATS_DOMAINS.items():
        if domain in hay:
            return portal
    return ""


def alert_provider_key(sender: str) -> str:
    """The job-alert provider this message is from ('' if none) — those are new openings for
    discovery's `EmailAlertSource` (decision 132), not applications, so they skip the importer."""
    from .discovery import _BUILTIN_ALERT_PROVIDERS

    low = (sender or "").lower()
    for key, prov in _BUILTIN_ALERT_PROVIDERS.items():
        if prov.sender.lower() in low:
            return key
    return ""


def classify_message(record: dict) -> tuple[str, str]:
    """The free gate. Returns ``(bucket, detail)`` where bucket is:
      • ``"alert"``   — a job-alert email (detail = provider key); discovery's, not ours.
      • ``"candidate"`` — worth a Claude call (detail = the ATS portal, or '').
      • ``"skip"``    — no application signal at all (detail = why).
    Costs nothing: sender domains + wording only."""
    sender = effective_sender(record)
    alert = alert_provider_key(sender)
    if alert:
        return "alert", alert
    portal = portal_for(sender, record.get("body") or "")
    text = f"{original_subject(record)}\n{(record.get('body') or '')[:6000]}"
    if portal:
        return "candidate", portal
    if _APPLICATION_MARKERS.search(text):
        return "candidate", ""
    return "skip", "no application wording or known ATS sender"


# ------------------------------------------------------------- stage 2: Haiku extraction

def _block(i: int, record: dict) -> str:
    return (f"=== EMAIL {i} ===\n"
            f"From: {effective_sender(record)}\n"
            f"Subject: {original_subject(record)}\n"
            f"Received: {record.get('date') or 'unknown'}\n\n"
            f"{(record.get('body') or '')[:_BODY_CHARS]}")


def extract_batch(records: list[dict], *, timeout: int = 120) -> dict[int, dict]:
    """Label a batch of gated-in emails with the cheap model. Returns {index -> extraction};
    an email the reply skipped is absent. Raises the `backends` Claude exceptions on failure."""
    from .backends import _extract_json, run_claude_cli

    if not records:
        return {}
    prompt = ("\n\n".join(_block(i, r) for i, r in enumerate(records))
              + f"\n\nLabel all {len(records)} email(s) now.")
    text = run_claude_cli(prompt, model=MODEL, think=False, timeout=timeout, system=_SYSTEM,
                          json_schema=_SCHEMA, activity="inbox-import")
    data = json.loads(_extract_json(text))
    out: dict[int, dict] = {}
    for r in data.get("results") or []:
        try:
            idx = int(r.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(records):
            out[idx] = r
    return out


# ----------------------------------------------------------- stage 3: match, insert, update

_COMPANY_NOISE = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|"
                            r"holdings|group|technologies|technology|labs)\b")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _norm_company(name: str) -> str:
    """A company name reduced to its identity: lowercase, punctuation and legal suffixes gone."""
    s = _NON_WORD.sub(" ", (name or "").lower())
    return " ".join(w for w in _COMPANY_NOISE.sub(" ", s).split() if w)


def _role_tokens(role: str) -> set[str]:
    """Role words that carry meaning, for the overlap test in `find_match`."""
    stop = {"the", "a", "an", "of", "and", "for", "to", "in", "at", "with", "our", "job",
            "position", "role", "opening", "opportunity", "i", "ii", "iii"}
    return {w for w in _NON_WORD.sub(" ", (role or "").lower()).split() if w not in stop}


def _roles_match(a: str, b: str) -> bool:
    """True if two role titles plausibly name the same job. Exact after normalization, one
    contained in the other, or ≥60% token overlap ('Software Engineer, Backend' ≈ 'Backend
    Software Engineer'). An empty role on either side is not a match — the caller decides."""
    ta, tb = _role_tokens(a), _role_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / max(len(ta), len(tb)) >= 0.6


def find_match(rows: list[dict], company: str, role: str, url: str = "") -> Optional[dict]:
    """The tracker row this email is about, or None. Source URL wins outright; otherwise the
    company must match and the role must match, EXCEPT that a company with exactly one row is
    matched on company alone (a rejection email often names no role). `rows` is newest-first, so
    the most recent qualifying application wins."""
    if url:
        for r in rows:
            if r.get("source_url") and r["source_url"].split("?", 1)[0] == url.split("?", 1)[0]:
                return r
    key = _norm_company(company)
    if not key:
        return None
    same = [r for r in rows if _norm_company(r.get("company", "")) == key]
    if not same:
        return None
    for r in same:
        if _roles_match(role, r.get("role", "")):
            return r
    return same[0] if len(same) == 1 and not role else None


def _should_update(current: str, new: str) -> bool:
    """Whether an imported status may overwrite the row's current one. Status moves forward only:
    a confirmation email read after an interview invite must not reset the row to `applied`. A
    rejection is allowed over anything but an accepted offer — being rejected after interviewing
    is the normal path."""
    if current == new:
        return False
    return _RANK.get(new, 0) > _RANK.get(current, 0)


def _note(record: dict, extraction: dict) -> str:
    """The provenance line stamped on every imported row — which email produced it."""
    when = extraction.get("date") or record.get("date") or ""
    return (f"[email-import] {original_subject(record) or '(no subject)'} — from "
            f"{effective_sender(record)}{' on ' + when if when else ''}")


def apply_extraction(record: dict, extraction: dict, *, portal: str = "",
                     path: str | Path | None = None) -> dict:
    """Write one extraction to the tracker. Returns an action record:
    ``{"action": created|updated|unchanged|skipped, "application_id": id|None, "status": …,
    "prev_status": …, "reason": …}``. `action=skipped` never touches the DB."""
    from . import tracker

    db = path or tracker.DEFAULT_DB
    if not extraction.get("is_application"):
        return {"action": "skipped", "application_id": None, "reason": "not an application email"}
    kind = (extraction.get("kind") or "other").lower()
    status = _KIND_STATUS.get(kind)
    if not status:
        return {"action": "skipped", "application_id": None, "reason": f"kind={kind}"}
    try:
        confidence = int(extraction.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    company, role = (extraction.get("company") or "").strip(), (extraction.get("role") or "").strip()
    if confidence < MIN_CONFIDENCE:
        return {"action": "skipped", "application_id": None,
                "reason": f"confidence {confidence} below {MIN_CONFIDENCE}"}
    if not company:
        return {"action": "skipped", "application_id": None, "reason": "no employer named"}

    when = (extraction.get("date") or record.get("date") or "").strip()
    url = (extraction.get("url") or "").strip()
    rows = tracker.list_applications(path=db)
    match = find_match(rows, company, role, url)
    if match is not None:
        prev = match.get("status", "")
        if not _should_update(prev, status):
            return {"action": "unchanged", "application_id": int(match["id"]), "status": prev,
                    "prev_status": prev,
                    "reason": f"row already {prev}; email says {status}"}
        changes: dict[str, Any] = {
            "status": status,
            "notes": ((match.get("notes") or "") + ("\n" if match.get("notes") else "")
                      + _note(record, extraction)).strip(),
        }
        if status == "applied" and when and not match.get("date_applied"):
            changes["date_applied"] = when
        tracker.update_application(int(match["id"]), changes, path=db)
        return {"action": "updated", "application_id": int(match["id"]), "status": status,
                "prev_status": prev, "reason": f"{prev} → {status}"}

    row: dict[str, Any] = {
        "company": company, "role": role or "(role not stated)",
        "location": (extraction.get("location") or "").strip(),
        "portal": portal or portal_for(effective_sender(record), record.get("body") or ""),
        "method": "email-import", "source_url": url, "status": status,
        "notes": _note(record, extraction),
    }
    # An outcome email proves an application was submitted even when we never saw the
    # confirmation, so date the submission from the email only for a confirmation; for a
    # rejection/interview the email's date is the OUTCOME's date, not the application's.
    if when:
        row["date_discovered"] = when
        if status == "applied":
            row["date_applied"] = when
    app_id = tracker.add_application(row, path=db)
    return {"action": "created", "application_id": app_id, "status": status, "prev_status": "",
            "reason": f"new row from {kind} email"}


# ------------------------------------------------------------------------------ the ledger

def _load_ledger(path: str | Path = LEDGER_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {"version": _LEDGER_VERSION, "messages": {}, "runs": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version": _LEDGER_VERSION, "messages": {}, "runs": {}}
    if data.get("version") != _LEDGER_VERSION:
        return {"version": _LEDGER_VERSION, "messages": {}, "runs": {}}
    data.setdefault("messages", {})
    data.setdefault("runs", {})
    return data


def _save_ledger(data: dict, path: str | Path = LEDGER_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def seen_message_ids(*, path: str | Path = LEDGER_PATH) -> set[str]:
    """Message-IDs already processed — never re-read, re-charged, or re-written."""
    return set(_load_ledger(path).get("messages", {}))


# --------------------------------------------------------------------------- orchestration

def _summary() -> dict:
    return {"scanned": 0, "already_imported": 0, "not_applications": 0, "examined": 0,
            "created": [], "updated": [], "unchanged": 0, "skipped": 0,
            "alerts": {}, "alerts_enabled": False, "errors": [], "run_id": "",
            "message": ""}


def run_import(config=None, *, limit: int = 50, newer_than_days: int = 30,
               path: str | Path | None = None, ledger_path: str | Path = LEDGER_PATH,
               _fetch=None, _extract=None) -> dict:
    """Scan the linked inbox and fold every application email into the tracker (decision 151).

    Returns the summary dict `_summary` describes: what was scanned, what was created/updated
    (ids), what was skipped and why, plus the job-alert emails found (`alerts`) and whether
    discovery's alert source is switched on to use them. Idempotent — a Message-ID in the ledger
    is skipped before any token is spent. `config` defaults to the linked mailbox; `_fetch` /
    `_extract` are injected in tests so nothing touches the network or Claude.
    """
    from . import mailbox, tracker

    db = path or tracker.DEFAULT_DB
    out = _summary()
    cfg = config if config is not None else mailbox.load_config()
    if cfg is None:
        out["errors"].append(
            "No inbox is linked, so there is nothing to import. Link one on the Profile tab "
            "(Connect with Google, or an IMAP app password), then run the import again.")
        out["message"] = out["errors"][0]
        return out

    fetch = _fetch or mailbox.fetch_messages
    try:
        records = fetch(cfg, limit=limit, newer_than_days=newer_than_days)
    except Exception as e:  # fetch_messages itself never raises; a stub in a test might
        out["errors"].append(f"Could not read the inbox: {type(e).__name__}: {e}")
        out["message"] = out["errors"][0]
        return out
    out["scanned"] = len(records)

    ledger = _load_ledger(ledger_path)
    seen = ledger["messages"]
    candidates: list[tuple[dict, str]] = []  # (record, portal)
    for rec in records:
        mid = rec.get("message_id") or ""
        if mid and mid in seen:
            out["already_imported"] += 1
            continue
        bucket, detail = classify_message(rec)
        if bucket == "alert":
            out["alerts"][detail] = out["alerts"].get(detail, 0) + 1
        elif bucket == "candidate":
            candidates.append((rec, detail))
        else:
            out["not_applications"] += 1
    out["examined"] = len(candidates)

    try:
        from .filters import load_filters
        f = load_filters()
        out["alerts_enabled"] = bool(f.email_alerts.enabled and f.email_alerts.providers)
    except Exception:
        out["alerts_enabled"] = False

    extract = _extract or extract_batch
    run_id = datetime.now().isoformat(timespec="seconds")
    actions: list[dict] = []
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        try:
            results = extract([r for r, _ in batch])
        except Exception as e:
            out["errors"].append(
                f"Claude could not label {len(batch)} email(s): {type(e).__name__}: {e}. "
                "They were left unprocessed — run the import again to retry them.")
            continue
        for i, (rec, portal) in enumerate(batch):
            ext = results.get(i)
            if ext is None:
                out["skipped"] += 1
                continue
            try:
                act = apply_extraction(rec, ext, portal=portal, path=db)
            except Exception as e:
                out["errors"].append(
                    f"Could not record '{original_subject(rec)}': {type(e).__name__}: {e}")
                continue
            if act["action"] == "created":
                out["created"].append(act["application_id"])
            elif act["action"] == "updated":
                out["updated"].append(act["application_id"])
            elif act["action"] == "unchanged":
                out["unchanged"] += 1
            else:
                out["skipped"] += 1
            mid = rec.get("message_id") or ""
            if mid:
                seen[mid] = {"at": run_id, "run": run_id, "action": act["action"],
                             "application_id": act.get("application_id"),
                             "prev_status": act.get("prev_status", ""),
                             "subject": original_subject(rec)}
            actions.append({**act, "subject": original_subject(rec)})

    if actions:
        ledger["runs"][run_id] = [a for a in actions if a["action"] in ("created", "updated")]
        out["run_id"] = run_id
    _save_ledger(ledger, ledger_path)
    out["message"] = summarize(out)
    return out


def summarize(out: dict) -> str:
    """One precise sentence about what the run did (Guideline #11) — the message the UI shows."""
    if out["errors"] and not (out["created"] or out["updated"]):
        return out["errors"][0]
    bits = [f"scanned {out['scanned']} email(s)"]
    if out["already_imported"]:
        bits.append(f"{out['already_imported']} already imported")
    bits.append(f"{len(out['created'])} new application(s)")
    bits.append(f"{len(out['updated'])} status update(s)")
    if out["unchanged"]:
        bits.append(f"{out['unchanged']} already up to date")
    if out["alerts"]:
        n = sum(out["alerts"].values())
        who = ", ".join(sorted(out["alerts"]))
        bits.append(f"{n} job-alert email(s) from {who}"
                    + ("" if out["alerts_enabled"] else " — not in use yet"))
    if out["errors"]:
        bits.append(f"{len(out['errors'])} error(s)")
    return ", ".join(bits) + "."


def undo_run(run_id: str, *, path: str | Path | None = None,
             ledger_path: str | Path = LEDGER_PATH) -> dict:
    """Reverse one import exactly: delete the rows it inserted, restore the status of the rows it
    updated. Returns {"deleted": n, "restored": n, "missing": n}. The run's messages leave the
    ledger too, so a re-import can pick them up again."""
    from . import tracker

    db = path or tracker.DEFAULT_DB
    ledger = _load_ledger(ledger_path)
    actions = ledger.get("runs", {}).get(run_id) or []
    res = {"deleted": 0, "restored": 0, "missing": 0}
    for act in actions:
        app_id = act.get("application_id")
        if not app_id:
            continue
        if act["action"] == "created":
            res["deleted" if tracker.delete_application(int(app_id), path=db) else "missing"] += 1
        elif act["action"] == "updated" and act.get("prev_status"):
            ok = tracker.update_application(int(app_id), {"status": act["prev_status"]}, path=db)
            res["restored" if ok else "missing"] += 1
    ledger["runs"].pop(run_id, None)
    ledger["messages"] = {k: v for k, v in ledger.get("messages", {}).items()
                          if v.get("run") != run_id}
    _save_ledger(ledger, ledger_path)
    return res


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Import job-application emails from the linked inbox into the tracker. "
        "Forward confirmations, rejections, and interview invites from any address to the linked "
        "bot inbox; each becomes a tracker row (or moves an existing row's status).")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="scan the inbox and import (default)")
    run.add_argument("--limit", type=int, default=50, help="newest N messages to scan")
    run.add_argument("--days", type=int, default=30, help="only messages from the last N days")
    run.add_argument("--db", default=None, help="tracker DB path")
    sub.add_parser("status", help="how many messages the ledger has already imported")
    un = sub.add_parser("undo", help="reverse an import run by its id")
    un.add_argument("run_id")
    un.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "status":
        led = _load_ledger()
        print(f"{len(led.get('messages', {}))} message(s) imported; "
              f"{len(led.get('runs', {}))} undoable run(s): "
              + (", ".join(sorted(led.get('runs', {}))) or "none"))
        return 0
    if args.cmd == "undo":
        r = undo_run(args.run_id, path=args.db)
        print(f"deleted {r['deleted']} row(s), restored {r['restored']} status(es), "
              f"{r['missing']} already gone.")
        return 0
    out = run_import(limit=getattr(args, "limit", 50), newer_than_days=getattr(args, "days", 30),
                     path=getattr(args, "db", None))
    print(out["message"])
    for e in out["errors"]:
        print(f"  ! {e}")
    if out["run_id"]:
        print(f"  undo with:  python -m applicationbot.inbox_import undo {out['run_id']}")
    return 1 if out["errors"] and not (out["created"] or out["updated"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read the bot's inbox over IMAP to complete a portal's email verification (decision 053).

Account-gated portals (Workday) email a verification **link or code** right after account
creation. The settled Workday design uses a dedicated **bot-owned email**; this module finds the
most recent verification message there so account creation stays hands-off.

**Linking the inbox.** Two ways, both storing the secret in the **OS keychain** (never on disk,
Guideline #12) with only non-secret fields in git-ignored `profile/mailbox.yaml`:
  • **Gmail one-click** (decision 065) — OAuth "Sign in with Google". `connect_gmail` runs the
    loopback consent flow and, on success, stores the refresh token + client secret in the keychain
    and email/client_id/`auth: oauth` in the yaml. Reads use the Gmail REST API with the **read-only**
    scope (least privilege, Guideline #5) — no app password, no IMAP host to enter.
  • **IMAP app-password** (decision 057) — any provider: password → keychain, host/email/port → yaml.
`load_config` prefers a stored link, then falls back to the **environment** for headless use
(`MAILBOX_IMAP_HOST` / `MAILBOX_EMAIL` / `MAILBOX_PASSWORD` / `MAILBOX_IMAP_PORT`). Link from the
Profile tab in the web UI, or the CLI: `python -m applicationbot.mailbox connect-gmail
--client-id … --client-secret …` (Gmail) / `link --email bot@example.com` (IMAP); also
`status` / `test` / `unlink`.

`extract_verification` (the link/code parser) is pure and fully tested; the IMAP connection is
injected (`_connect`) so `fetch_verification`/`wait_for_verification`/`test_connection` run against
a fake with no network. The one thing no unit test can cover — a real inbox receiving a real
Workday email — is the flagged live step for this brick.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .paths import DATA_ROOT

# A verification link (prefer one that looks like the portal's) or a 6–8 digit code.
_LINK_RE = re.compile(r"""https?://[^\s"'<>)]+""", re.IGNORECASE)
_CODE_RE = re.compile(r"\b(\d{6,8})\b")
_LINK_HINTS = ("verify", "verification", "activate", "confirm", "myworkdayjobs", "workday")

# Where a linked account is stored: the PASSWORD goes in the OS keychain (never on disk —
# Guideline #12), and only host/email/port land in this git-ignored file (profile/ is ignored).
_LINK_PATH = DATA_ROOT / "profile" / "mailbox.yaml"
_KEYRING_SERVICE = "applicationbot-mailbox"

# Gmail OAuth (decision 065): the true one-click connect. We read only the verification emails, so
# we ask for the read-only Gmail scope (least privilege, Guideline #5) — NOT the full mail.google.com
# scope IMAP-over-OAuth would force. Reads go through the Gmail REST API with a Bearer token, so
# `imap.gmail.com` here is only a display label. The refresh token + client secret live in the
# keychain; the (non-secret) client_id sits in the git-ignored yaml so a reconnect is one click.
_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_IMAP_HOST = "imap.gmail.com"  # display label only; OAuth reads use the REST API
_GMAIL_OAUTH_SERVICE = "applicationbot-gmail-oauth"
_GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Common IMAP hosts, so the UI can suggest one from the email domain (best-effort convenience).
_KNOWN_IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
    "office365.com": "outlook.office365.com", "yahoo.com": "imap.mail.yahoo.com",
    "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "fastmail.com": "imap.fastmail.com",
}


@dataclass
class MailboxConfig:
    # The three secret fields are `repr=False` (decision 075): they stay fully usable in code but
    # never render in a repr/str, so a traceback, log line, or pytest assertion diff carrying a
    # config cannot print a live credential (Guideline #5). `link_status()` is the safe view.
    host: str
    email: str
    password: str = field(default="", repr=False)
    port: int = 993
    source: str = ""  # "linked" (keychain+file) | "env" — for status display only
    auth: str = "password"  # "password" (IMAP app-password/env) | "oauth" (Gmail read-only)
    refresh_token: str = field(default="", repr=False)  # oauth: mints access tokens
    client_id: str = ""      # oauth: Google Cloud "Desktop app" client (non-secret)
    client_secret: str = field(default="", repr=False)  # oauth: paired secret (keychain)


def suggest_host(email: str) -> str:
    """A best-effort IMAP host guessed from the email domain, or '' if unknown."""
    domain = (email or "").split("@")[-1].strip().lower()
    return _KNOWN_IMAP_HOSTS.get(domain, "")


def _keyring():
    import keyring

    return keyring


def _env_config(env) -> Optional[MailboxConfig]:
    host, email, pw = env.get("MAILBOX_IMAP_HOST"), env.get("MAILBOX_EMAIL"), env.get("MAILBOX_PASSWORD")
    if not (host and email and pw):
        return None
    try:
        port = int(env.get("MAILBOX_IMAP_PORT", 993))
    except ValueError:
        port = 993
    return MailboxConfig(host=host, email=email, password=pw, port=port, source="env")


def _verify_saved(backend, service: str, key: str, value: str, what: str) -> None:
    """Read a just-written secret back, and raise an actionable RuntimeError if it isn't there.

    A keychain write can silently no-op — a locked login keychain, or `keyring` falling back to a
    backend that stores nothing. Without this read-back the link file gets written anyway and the
    inbox reads as connected until the next run, when the secret turns out to be missing."""
    try:
        stored = backend.get_password(service, key)
    except Exception as e:
        raise RuntimeError(
            f"Saved the {what} for {key} but could not read it back from the OS keychain: "
            f"{type(e).__name__}: {e}. Unlock your login keychain in Keychain Access, then "
            "connect again.") from e
    if stored != value:
        raise RuntimeError(
            f"The {what} for {key} did not persist to the OS keychain — nothing came back after "
            "writing it. Unlock your login keychain in Keychain Access, then connect again.")


def save_link(host: str, email: str, password: str, port: int = 993, *, backend=None,
              path: str | Path = _LINK_PATH) -> None:
    """Link the bot inbox: password → OS keychain, host/email/port → the git-ignored file.
    Raises if the password did not persist to the keychain — the file is written only after the
    secret is proven stored, so a link is never recorded without its password."""
    import yaml

    kr = backend or _keyring()
    kr.set_password(_KEYRING_SERVICE, email, password)
    _verify_saved(kr, _KEYRING_SERVICE, email, password, "app password")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"host": host, "email": email, "port": int(port)}, sort_keys=False),
                 encoding="utf-8")


def save_gmail_link(email: str, refresh_token: str, client_id: str, client_secret: str, *,
                    backend=None, path: str | Path = _LINK_PATH) -> None:
    """Link Gmail via OAuth: refresh token + client secret → keychain; email/client_id/auth flag →
    the git-ignored yaml. host/port are the Gmail defaults (display only — reads use the REST API).
    Raises if the keychain write didn't stick (same read-back guarantee as `save_link`)."""
    import json
    import yaml

    kr = backend or _keyring()
    blob = json.dumps({"refresh_token": refresh_token, "client_secret": client_secret})
    kr.set_password(_GMAIL_OAUTH_SERVICE, email, blob)
    _verify_saved(kr, _GMAIL_OAUTH_SERVICE, email, blob, "Google authorization")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(
        {"host": _GMAIL_IMAP_HOST, "email": email, "port": 993, "auth": "oauth",
         "client_id": client_id}, sort_keys=False), encoding="utf-8")


def load_link(*, backend=None, path: str | Path = _LINK_PATH) -> Optional[MailboxConfig]:
    """The linked account (file + keychain secret), or None if not linked / secret missing.
    Handles both the OAuth (Gmail) and password (IMAP app-password) link formats."""
    import json
    import yaml

    p = Path(path)
    if not p.exists():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    host, email = data.get("host"), data.get("email")
    if not (host and email):
        return None
    if data.get("auth") == "oauth":
        try:
            raw = (backend or _keyring()).get_password(_GMAIL_OAUTH_SERVICE, email)
        except Exception:
            raw = None
        try:
            blob = json.loads(raw) if raw else {}
        except Exception:
            blob = {}
        if not blob.get("refresh_token"):
            return None
        return MailboxConfig(host=host, email=email, port=int(data.get("port", 993)),
                             source="linked", auth="oauth", refresh_token=blob["refresh_token"],
                             client_id=data.get("client_id", ""),
                             client_secret=blob.get("client_secret", ""))
    try:
        pw = (backend or _keyring()).get_password(_KEYRING_SERVICE, email)
    except Exception:
        pw = None
    if not pw:
        return None
    return MailboxConfig(host=host, email=email, password=pw, port=int(data.get("port", 993)),
                         source="linked")


def link_problem(*, backend=None, path: str | Path = _LINK_PATH) -> str:
    """'' normally. If the link file records an account but its keychain secret can't be read, an
    actionable message naming the account and the fix — the one way a link looks saved on disk yet
    fails to load (a cleared or locked login keychain). Without this the UI would just say "not
    connected", hiding that only the secret is gone (UI Principle #3)."""
    import yaml

    p = Path(path)
    if not p.exists():
        return ""
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return f"{p} is not readable as YAML — click Disconnect, then connect the inbox again."
    email = data.get("email")
    if not email or load_link(backend=backend, path=path) is not None:
        return ""
    if data.get("auth") == "oauth":
        return (f"{email} is linked but its Google authorization is missing from your OS keychain — "
                "click Connect with Google to re-authorize.")
    return (f"{email} is linked but its app password is missing from your OS keychain — re-enter "
            "the 16-character app password below and click Connect.")


def link_status(*, backend=None, path: str | Path = _LINK_PATH, env=None) -> dict:
    """Non-secret status for the UI: {linked, host, email, port, source, auth, problem}. Never
    returns the password. `linked` is True if either a stored link OR the env vars provide a full
    config; `problem` explains a link whose keychain secret went missing."""
    cfg = load_link(backend=backend, path=path) or _env_config(env if env is not None else os.environ)
    if cfg is None:
        return {"linked": False, "host": "", "email": "", "port": 993, "source": "", "auth": "",
                "problem": link_problem(backend=backend, path=path)}
    return {"linked": True, "host": cfg.host, "email": cfg.email, "port": cfg.port,
            "source": cfg.source, "auth": cfg.auth, "problem": ""}


def gmail_client_id(*, path: str | Path = _LINK_PATH) -> str:
    """The stored (non-secret) Gmail OAuth client_id, so a reconnect can pre-fill it. '' if none."""
    import yaml

    p = Path(path)
    if not p.exists():
        return ""
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    return data.get("client_id", "") if data.get("auth") == "oauth" else ""


def clear_link(*, backend=None, path: str | Path = _LINK_PATH) -> bool:
    """Unlink: remove the keychain secret (password or OAuth) and the file. True if a link existed."""
    import yaml

    p = Path(path)
    if not p.exists():
        return False
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    email = data.get("email")
    service = _GMAIL_OAUTH_SERVICE if data.get("auth") == "oauth" else _KEYRING_SERVICE
    if email:
        try:
            (backend or _keyring()).delete_password(service, email)
        except Exception:
            pass
    p.unlink()
    return True


def load_config(env=None, *, backend=None, path: str | Path = _LINK_PATH) -> Optional[MailboxConfig]:
    """The mailbox config to use: a stored **link** (keychain) first, then the **environment**
    (headless). None if neither is set (callers degrade to 'verify the email manually')."""
    linked = load_link(backend=backend, path=path)
    if linked is not None:
        return linked
    return _env_config(env if env is not None else os.environ)


def _gmail_access_token(config: MailboxConfig) -> str:
    """Mint a fresh short-lived access token from the stored refresh token. Raises on failure."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=None, refresh_token=config.refresh_token, token_uri=_GMAIL_TOKEN_URI,
        client_id=config.client_id, client_secret=config.client_secret, scopes=[_GMAIL_SCOPE])
    creds.refresh(Request())
    return creds.token


def _gmail_get(access_token: str, path: str, params: dict | None = None) -> dict:
    """One authenticated GET against the Gmail REST API. Returns the parsed JSON body."""
    import json
    import urllib.parse
    import urllib.request

    url = f"{_GMAIL_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gmail_test(config: MailboxConfig, *, _token=None, _get=None) -> "tuple[bool, str]":
    """Prove the OAuth link works by refreshing the token and reading the account profile.
    Returns an actionable (ok, message) (UI Principle #3)."""
    token = _token or _gmail_access_token
    get = _get or _gmail_get
    try:
        access = token(config)
    except Exception as e:
        return False, (f"Could not refresh Gmail access for {config.email}: {type(e).__name__}: {e}. "
                       "Reconnect Gmail — the authorization may have been revoked or expired.")
    try:
        prof = get(access, "/profile")
    except Exception as e:
        return False, f"Signed in but could not read the Gmail profile: {type(e).__name__}: {e}."
    return True, f"Connected to Gmail as {prof.get('emailAddress', config.email)} (read-only)."


def test_connection(config: MailboxConfig, *, _connect=None) -> "tuple[bool, str]":
    """Prove the linked mailbox works. OAuth (Gmail) refreshes the token + reads the profile;
    IMAP does a login + INBOX select. Returns (ok, message), user-facing and actionable (#3)."""
    if config.auth == "oauth":
        return _gmail_test(config)
    connect = _connect or _connect_imap
    try:
        m = connect(config)
    except Exception as e:
        return False, (f"Could not sign in to {config.host} as {config.email}: {type(e).__name__}: {e}. "
                       "Check the IMAP host, the email, and that the password is an app password "
                       "(not your normal login) if the provider requires one.")
    try:
        m.select("INBOX")
    except Exception as e:
        return False, f"Signed in but could not open INBOX: {type(e).__name__}: {e}."
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return True, f"Connected to {config.host} as {config.email}."


def extract_verification(body: str, *, hints=_LINK_HINTS, prefer_code: bool = False) -> str:
    """The verification link (preferring one whose URL mentions a hint word) or, failing that, a
    6–8 digit code, from an email body. '' if neither is present.

    `prefer_code` flips the order for senders whose email is a **code** sign-in but still carries
    verify-ish links (MyGreenhouse, decision 182): take the digits, never the link."""
    if not body:
        return ""
    if prefer_code:
        m = _CODE_RE.search(body)
        if m:
            return m.group(1)
    links = _LINK_RE.findall(body)
    for link in links:
        low = link.lower()
        if any(h in low for h in hints):
            return link.rstrip(".,)")
    m = _CODE_RE.search(body)
    if m:
        return m.group(1)
    return links[0].rstrip(".,)") if links else ""


def _body_text(msg) -> str:
    """Best-effort plaintext of an email.message.Message: prefer text/plain parts, fall back to
    HTML stripped to text."""
    from .discovery import html_to_text

    plain, html = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            (plain if ctype == "text/plain" else html).append(text)
    else:
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
        except Exception:
            text = ""
        (html if msg.get_content_type() == "text/html" else plain).append(text)
    if plain and "".join(plain).strip():
        return "\n".join(plain)
    return html_to_text("\n".join(html))


def _connect_imap(config: MailboxConfig):
    import imaplib

    m = imaplib.IMAP4_SSL(config.host, config.port)
    m.login(config.email, config.password)
    return m


def _body_html(msg) -> str:
    """Best-effort HTML (or plaintext) body of an email.message.Message. Prefers text/html so a
    job-alert parser can read the posting <a href=…> links (unlike `_body_text`, which strips the
    hrefs out); falls back to text/plain when there is no HTML part."""
    html, plain = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (html if ctype == "text/html" else plain).append(text)
    if html and "".join(html).strip():
        return "\n".join(html)
    return "\n".join(plain)


def _gmail_fetch_alerts(config: MailboxConfig, *, sender_contains: str, limit: int = 25,
                        _token=None, _get=None) -> list[str]:
    """OAuth read: newest-first HTML/plaintext bodies (up to `limit`) of Gmail messages whose From
    matches `sender_contains`. Sibling of `_gmail_fetch_verification` that returns whole bodies.
    Never raises (returns [] on error)."""
    import base64
    import email as email_mod

    token = _token or _gmail_access_token
    get = _get or _gmail_get
    try:
        access = token(config)
        q = f"from:{sender_contains}" if sender_contains else ""
        listing = get(access, "/messages", {"q": q, "maxResults": max(1, limit)})
    except Exception:
        return []
    out: list[str] = []
    for meta in listing.get("messages", []):  # already newest-first from the API
        if len(out) >= limit:
            break
        try:
            full = get(access, f"/messages/{meta['id']}", {"format": "raw"})
            raw = base64.urlsafe_b64decode(full["raw"])
            msg = email_mod.message_from_bytes(raw)
        except Exception:
            continue
        body = _body_html(msg)
        if body.strip():
            out.append(body)
    return out


def fetch_alerts(config: MailboxConfig, *, sender_contains: str, limit: int = 25,
                 mailbox: str = "INBOX", _connect=_connect_imap) -> list[str]:
    """Newest-first, up to `limit`, the HTML (or plaintext) bodies of inbox messages whose From
    matches `sender_contains` — the raw material a job-alert parser turns into leads (decision 132).
    Sibling of `fetch_verification`: same sender-filtered, newest-first scan, but returns whole
    bodies instead of a single verification token. OAuth reads via the Gmail REST API; a
    password/env link reads via IMAP. Never raises (returns [] on any error)."""
    if config.auth == "oauth":
        return _gmail_fetch_alerts(config, sender_contains=sender_contains, limit=limit)
    import email as email_mod

    try:
        m = _connect(config)
    except Exception:
        return []
    out: list[str] = []
    try:
        m.select(mailbox)
        typ, data = m.search(None, "ALL")
        ids = (data[0].split() if data and data[0] else [])
        for mid in reversed(ids):  # newest last in IMAP sequence → iterate reversed
            if len(out) >= limit:
                break
            try:
                typ, msg_data = m.fetch(mid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email_mod.message_from_bytes(raw)
            except Exception:
                continue
            frm = (msg.get("From") or "").lower()
            if sender_contains and sender_contains.lower() not in frm:
                continue
            body = _body_html(msg)
            if body.strip():
                out.append(body)
        return out
    except Exception:
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _decode_header(raw: str) -> str:
    """An RFC-2047 encoded header (`=?utf-8?B?…?=`) as plain text; the raw value on failure."""
    from email.header import decode_header, make_header

    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def _message_record(msg) -> dict:
    """One parsed email as the inbox importer consumes it: {message_id, sender, subject, date,
    body}. `date` is the ISO date from the Date header ('' if unparseable); `body` is plaintext
    (HTML stripped) — a forwarded message's original From/Subject/Date live inside it."""
    from email.utils import parsedate_to_datetime

    date_iso = ""
    if msg.get("Date"):
        try:
            date_iso = parsedate_to_datetime(msg["Date"]).date().isoformat()
        except Exception:
            date_iso = ""
    return {
        "message_id": (msg.get("Message-ID") or "").strip(),
        "sender": _decode_header(msg.get("From") or ""),
        "subject": _decode_header(msg.get("Subject") or ""),
        "date": date_iso,
        "body": _body_text(msg),
    }


def _gmail_fetch_messages(config: MailboxConfig, *, limit: int, newer_than_days: int,
                          _token=None, _get=None) -> list[dict]:
    """OAuth read: newest-first message records (see `_message_record`) from the Gmail account,
    limited to the last `newer_than_days` days. Never raises (returns [] on error)."""
    import base64
    import email as email_mod

    token = _token or _gmail_access_token
    get = _get or _gmail_get
    try:
        access = token(config)
        q = f"newer_than:{newer_than_days}d" if newer_than_days else ""
        listing = get(access, "/messages", {"q": q, "maxResults": max(1, limit)})
    except Exception:
        return []
    out: list[dict] = []
    for meta in listing.get("messages", []):  # already newest-first from the API
        if len(out) >= limit:
            break
        try:
            full = get(access, f"/messages/{meta['id']}", {"format": "raw"})
            msg = email_mod.message_from_bytes(base64.urlsafe_b64decode(full["raw"]))
        except Exception:
            continue
        rec = _message_record(msg)
        if not rec["message_id"]:  # no stable id → the importer could not dedup it
            rec["message_id"] = f"gmail:{meta.get('id', '')}"
        out.append(rec)
    return out


def fetch_messages(config: MailboxConfig, *, limit: int = 50, newer_than_days: int = 30,
                   mailbox: str = "INBOX", _connect=_connect_imap) -> list[dict]:
    """Newest-first, up to `limit`, every inbox message from the last `newer_than_days` days as
    a record dict (`_message_record`) — the raw material the inbox importer turns into tracker
    rows (decision 151). Unlike `fetch_alerts` this keeps the HEADERS and does not filter by
    sender: a forwarded email's From is the forwarder, not the employer, so sender filtering
    would drop exactly the messages we want. OAuth reads via the Gmail REST API; a
    password/env link reads via IMAP. Never raises (returns [] on any error)."""
    if config.auth == "oauth":
        return _gmail_fetch_messages(config, limit=limit, newer_than_days=newer_than_days)
    import email as email_mod
    from datetime import date as _date, timedelta

    try:
        m = _connect(config)
    except Exception:
        return []
    out: list[dict] = []
    try:
        m.select(mailbox)
        if newer_than_days:
            since = (_date.today() - timedelta(days=newer_than_days)).strftime("%d-%b-%Y")
            typ, data = m.search(None, "SINCE", since)
        else:
            typ, data = m.search(None, "ALL")
        ids = (data[0].split() if data and data[0] else [])
        for mid in reversed(ids):  # newest last in IMAP sequence → iterate reversed
            if len(out) >= limit:
                break
            try:
                typ, msg_data = m.fetch(mid, "(RFC822)")
                msg = email_mod.message_from_bytes(msg_data[0][1])
            except Exception:
                continue
            rec = _message_record(msg)
            if not rec["message_id"]:
                rec["message_id"] = f"imap:{config.email}:{mid.decode() if isinstance(mid, bytes) else mid}"
            out.append(rec)
        return out
    except Exception:
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _sent_epoch(msg) -> Optional[float]:
    """When an email.message.Message was sent, as a POSIX timestamp — None if its Date header is
    missing or unparseable."""
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(msg.get("Date") or "")
        if dt is None:
            return None
        if dt.tzinfo is None:
            # RFC 2822 "-0000" (an unknown offset) parses to a NAIVE datetime, and .timestamp()
            # would then read it as local time — shifting the send time by the machine's UTC
            # offset and letting a stale code look fresh. Those headers are UTC in practice.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _stale(sent: Optional[float], since_epoch: Optional[float]) -> bool:
    """True when a message must be skipped as older than the code we're waiting for. With no
    `since_epoch` nothing is stale (Workday's account-creation flow, decision 053). With one, a
    message of unknown age is treated as stale: a login code is only ever valid if it arrived
    AFTER we asked for it, and replaying a previous code fails the sign-in silently (decision 182)."""
    if since_epoch is None:
        return False
    return sent is None or sent < since_epoch


def _gmail_fetch_verification(config: MailboxConfig, *, sender_contains: str = "workday",
                              since_epoch: Optional[float] = None, prefer_code: bool = False,
                              _token=None, _get=None) -> str:
    """OAuth read: newest-first, return the verification link/code from the most recent Gmail
    message whose From matches `sender_contains`. '' if none. Never raises (returns '' on error)."""
    import base64
    import email as email_mod

    token = _token or _gmail_access_token
    get = _get or _gmail_get
    try:
        access = token(config)
        q = f"from:{sender_contains}" if sender_contains else ""
        listing = get(access, "/messages", {"q": q, "maxResults": 20})
    except Exception:
        return ""
    for meta in listing.get("messages", []):  # already newest-first from the API
        try:
            full = get(access, f"/messages/{meta['id']}", {"format": "raw"})
            raw = base64.urlsafe_b64decode(full["raw"])
            msg = email_mod.message_from_bytes(raw)
        except Exception:
            continue
        # Gmail's own internalDate (ms) is authoritative for arrival time; fall back to the header.
        try:
            sent = float(full["internalDate"]) / 1000.0
        except Exception:
            sent = _sent_epoch(msg)
        if _stale(sent, since_epoch):
            continue
        v = extract_verification(_body_text(msg), prefer_code=prefer_code)
        if v:
            return v
    return ""


def fetch_verification(config: MailboxConfig, *, sender_contains: str = "workday",
                       mailbox: str = "INBOX", since_epoch: Optional[float] = None,
                       prefer_code: bool = False, _connect=_connect_imap) -> str:
    """One pass, newest-first: the verification link/code from the most recent message whose From
    matches `sender_contains`. '' if none. Never raises. OAuth reads via the Gmail REST API; a
    password/env link reads via IMAP.

    `since_epoch` (POSIX seconds) skips anything sent earlier — pass the moment you triggered the
    send so a previous code can never be replayed. `prefer_code` takes the digits over a link."""
    import email as email_mod

    if config.auth == "oauth":
        return _gmail_fetch_verification(config, sender_contains=sender_contains,
                                         since_epoch=since_epoch, prefer_code=prefer_code)
    try:
        m = _connect(config)
    except Exception:
        return ""
    try:
        m.select(mailbox)
        typ, data = m.search(None, "ALL")
        ids = (data[0].split() if data and data[0] else [])
        for mid in reversed(ids):  # newest last in IMAP sequence → iterate reversed
            try:
                typ, msg_data = m.fetch(mid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email_mod.message_from_bytes(raw)
            except Exception:
                continue
            frm = (msg.get("From") or "").lower()
            if sender_contains and sender_contains.lower() not in frm:
                continue
            if _stale(_sent_epoch(msg), since_epoch):
                continue
            v = extract_verification(_body_text(msg), prefer_code=prefer_code)
            if v:
                return v
        return ""
    except Exception:
        return ""
    finally:
        try:
            m.logout()
        except Exception:
            pass


def wait_for_verification(config: MailboxConfig, *, sender_contains: str = "workday",
                          timeout: int = 120, poll: int = 5, since_epoch: Optional[float] = None,
                          prefer_code: bool = False, _connect=_connect_imap,
                          _sleep=time.sleep, _fetch=None) -> str:
    """Poll the inbox until a matching verification link/code arrives or `timeout` elapses.
    Returns '' on timeout. `since_epoch`/`prefer_code` pass through to `fetch_verification`.
    `_fetch`/`_sleep` injectable for tests."""
    fetch = _fetch or (lambda: fetch_verification(config, sender_contains=sender_contains,
                                                  since_epoch=since_epoch, prefer_code=prefer_code,
                                                  _connect=_connect))
    waited = 0
    while True:
        v = fetch()
        if v:
            return v
        if waited >= timeout:
            return ""
        _sleep(poll)
        waited += poll


def _client_config(client_id: str, client_secret: str) -> dict:
    """The installed-app (loopback) client config google-auth-oauthlib expects."""
    return {"installed": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": _GMAIL_TOKEN_URI,
        "redirect_uris": ["http://localhost"]}}


def run_gmail_oauth(client_id: str, client_secret: str, *, open_browser: bool = True,
                    port: int = 0, _flow=None) -> "tuple[str, str]":
    """Run the one-click 'Sign in with Google' loopback flow: open the consent screen in the
    browser, catch the redirect on a temporary local port, and return (email, refresh_token).
    Raises on denial/timeout; the refresh_token is '' if Google returned none (see the caller)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = _flow or InstalledAppFlow.from_client_config(
        _client_config(client_id, client_secret), scopes=[_GMAIL_SCOPE])
    # access_type=offline + prompt=consent forces a refresh token on every run (Google omits it on
    # re-consent otherwise) so a reconnect always yields a token we can persist.
    creds = flow.run_local_server(port=port, open_browser=open_browser, access_type="offline",
                                  prompt="consent")
    email = ""
    try:
        email = _gmail_get(creds.token, "/profile").get("emailAddress", "")
    except Exception:
        pass
    return email, (creds.refresh_token or "")


def connect_gmail(client_id: str, client_secret: str, *, open_browser: bool = True,
                  backend=None, path: str | Path = _LINK_PATH, _run=None) -> "tuple[bool, str]":
    """End-to-end one-click connect: run the OAuth flow, verify it reads, and persist the link.
    Nothing is saved unless a refresh token comes back AND a test read succeeds (mirrors the
    link-before-save rule of decision 057). Returns an actionable (ok, message)."""
    run = _run or run_gmail_oauth
    try:
        email, refresh_token = run(client_id, client_secret, open_browser=open_browser)
    except Exception as e:
        return False, (f"Gmail authorization did not complete: {type(e).__name__}: {e}. "
                       "Re-run Connect Gmail and approve the read-only access on Google's screen.")
    if not email or not refresh_token:
        return False, ("Google did not return a reusable token. Make sure the Google Cloud project "
                       "is set to 'In production' (not 'Testing'), then Connect Gmail again.")
    cfg = MailboxConfig(host=_GMAIL_IMAP_HOST, email=email, auth="oauth",
                        refresh_token=refresh_token, client_id=client_id, client_secret=client_secret)
    ok, msg = test_connection(cfg)
    if not ok:
        return False, msg
    try:
        save_gmail_link(email, refresh_token, client_id, client_secret, backend=backend, path=path)
    except Exception as e:
        return False, f"Google approved the access but it could not be saved: {e}"
    return True, f"Connected {email} — Gmail read-only access stored in your OS keychain."


def main(argv=None) -> int:
    import argparse
    import getpass

    ap = argparse.ArgumentParser(
        description="Link the bot email inbox used for portal (Workday) email verification. "
        "The password is stored in the OS keychain; host/email/port in git-ignored profile/mailbox.yaml.")
    sub = ap.add_subparsers(dest="cmd")
    lk = sub.add_parser("link", help="Link an inbox and verify it connects.")
    lk.add_argument("--email", required=True)
    lk.add_argument("--host", default="", help="IMAP host; guessed from the email domain if omitted.")
    lk.add_argument("--port", type=int, default=993)
    lk.add_argument("--password", default="", help="App password; omit to be prompted (not echoed).")
    lk.add_argument("--no-test", action="store_true", help="Skip the connection test before saving.")
    cg = sub.add_parser("connect-gmail", help="One-click Gmail connect via OAuth (opens a browser).")
    cg.add_argument("--client-id", required=True, help="Google Cloud 'Desktop app' OAuth client id.")
    cg.add_argument("--client-secret", required=True, help="Paired client secret.")
    cg.add_argument("--no-browser", action="store_true",
                    help="Print the consent URL instead of opening a browser (headless).")
    sub.add_parser("status", help="Show whether an inbox is linked (no password).")
    sub.add_parser("test", help="Test the currently linked/env inbox connection.")
    sub.add_parser("unlink", help="Remove the linked inbox (keychain + file).")
    args = ap.parse_args(argv)

    if args.cmd == "connect-gmail":
        ok, msg = connect_gmail(args.client_id, args.client_secret, open_browser=not args.no_browser)
        print(msg)
        return 0 if ok else 1

    if args.cmd == "link":
        host = args.host or suggest_host(args.email)
        if not host:
            print(f"Could not guess the IMAP host for {args.email} — pass --host (e.g. imap.gmail.com).")
            return 1
        password = args.password or getpass.getpass("App password (input hidden): ")
        cfg = MailboxConfig(host=host, email=args.email, password=password, port=args.port)
        if not args.no_test:
            ok, msg = test_connection(cfg)
            print(msg)
            if not ok:
                return 1
        try:
            save_link(host, args.email, password, args.port)
        except Exception as e:
            print(f"Not linked — {e}")
            return 1
        print(f"Linked {args.email} ({host}:{args.port}). Password stored in the OS keychain.")
        return 0
    if args.cmd == "test":
        cfg = load_config()
        if cfg is None:
            print("No inbox linked. Run:  python -m applicationbot.mailbox link --email bot@example.com")
            return 1
        ok, msg = test_connection(cfg)
        print(msg)
        return 0 if ok else 1
    if args.cmd == "unlink":
        print("Unlinked." if clear_link() else "Nothing was linked.")
        return 0
    s = link_status()  # default: status
    if s["linked"]:
        how = "Gmail OAuth (read-only)" if s.get("auth") == "oauth" else f"{s['host']}:{s['port']}"
        print(f"Linked: {s['email']} via {how} ({s['source']}).")
    elif s.get("problem"):
        print(s["problem"])
    else:
        print("No inbox linked. Gmail one-click:  python -m applicationbot.mailbox connect-gmail "
              "--client-id … --client-secret …\n"
              "Or IMAP app-password:  python -m applicationbot.mailbox link --email bot@example.com")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

# A verification link (prefer one that looks like the portal's) or a 6–8 digit code.
_LINK_RE = re.compile(r"""https?://[^\s"'<>)]+""", re.IGNORECASE)
_CODE_RE = re.compile(r"\b(\d{6,8})\b")
_LINK_HINTS = ("verify", "verification", "activate", "confirm", "myworkdayjobs", "workday")

# Where a linked account is stored: the PASSWORD goes in the OS keychain (never on disk —
# Guideline #12), and only host/email/port land in this git-ignored file (profile/ is ignored).
_LINK_PATH = Path("profile/mailbox.yaml")
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


def save_link(host: str, email: str, password: str, port: int = 993, *, backend=None,
              path: str | Path = _LINK_PATH) -> None:
    """Link the bot inbox: password → OS keychain, host/email/port → the git-ignored file."""
    import yaml

    (backend or _keyring()).set_password(_KEYRING_SERVICE, email, password)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"host": host, "email": email, "port": int(port)}, sort_keys=False),
                 encoding="utf-8")


def save_gmail_link(email: str, refresh_token: str, client_id: str, client_secret: str, *,
                    backend=None, path: str | Path = _LINK_PATH) -> None:
    """Link Gmail via OAuth: refresh token + client secret → keychain; email/client_id/auth flag →
    the git-ignored yaml. host/port are the Gmail defaults (display only — reads use the REST API)."""
    import json
    import yaml

    (backend or _keyring()).set_password(
        _GMAIL_OAUTH_SERVICE, email,
        json.dumps({"refresh_token": refresh_token, "client_secret": client_secret}))
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


def link_status(*, backend=None, path: str | Path = _LINK_PATH, env=None) -> dict:
    """Non-secret status for the UI: {linked, host, email, port, source}. Never returns the
    password. `linked` is True if either a stored link OR the env vars provide a full config."""
    cfg = load_link(backend=backend, path=path) or _env_config(env if env is not None else os.environ)
    if cfg is None:
        return {"linked": False, "host": "", "email": "", "port": 993, "source": "", "auth": ""}
    return {"linked": True, "host": cfg.host, "email": cfg.email, "port": cfg.port,
            "source": cfg.source, "auth": cfg.auth}


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


def extract_verification(body: str, *, hints=_LINK_HINTS) -> str:
    """The verification link (preferring one whose URL mentions a hint word) or, failing that, a
    6–8 digit code, from an email body. '' if neither is present."""
    if not body:
        return ""
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


def _gmail_fetch_verification(config: MailboxConfig, *, sender_contains: str = "workday",
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
        v = extract_verification(_body_text(msg))
        if v:
            return v
    return ""


def fetch_verification(config: MailboxConfig, *, sender_contains: str = "workday",
                       mailbox: str = "INBOX", _connect=_connect_imap) -> str:
    """One pass, newest-first: the verification link/code from the most recent message whose From
    matches `sender_contains`. '' if none. Never raises. OAuth reads via the Gmail REST API; a
    password/env link reads via IMAP."""
    import email as email_mod

    if config.auth == "oauth":
        return _gmail_fetch_verification(config, sender_contains=sender_contains)
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
            v = extract_verification(_body_text(msg))
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
                          timeout: int = 120, poll: int = 5, _connect=_connect_imap,
                          _sleep=time.sleep, _fetch=None) -> str:
    """Poll the inbox until a matching verification link/code arrives or `timeout` elapses.
    Returns '' on timeout. `_fetch`/`_sleep` injectable for tests."""
    fetch = _fetch or (lambda: fetch_verification(config, sender_contains=sender_contains, _connect=_connect))
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
    save_gmail_link(email, refresh_token, client_id, client_secret, backend=backend, path=path)
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
        save_link(host, args.email, password, args.port)
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
    else:
        print("No inbox linked. Gmail one-click:  python -m applicationbot.mailbox connect-gmail "
              "--client-id … --client-secret …\n"
              "Or IMAP app-password:  python -m applicationbot.mailbox link --email bot@example.com")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

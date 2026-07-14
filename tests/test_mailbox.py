"""Mailbox (IMAP verification reader) tests (decision 053) — no network, fake IMAP.

`extract_verification` prefers a portal-looking link, else a numeric code; `fetch_verification`
pulls the newest matching-sender message from a fake IMAP; `wait_for_verification` polls until one
arrives. The only unverifiable-in-tests path (a real inbox) is the flagged live step.

Run:  python -m tests.test_mailbox   (also pytest-compatible)
"""
from __future__ import annotations

from email.message import EmailMessage

from applicationbot import mailbox
from applicationbot.mailbox import MailboxConfig


def test_extract_prefers_hinted_link():
    body = ("Welcome! Confirm here: https://acme.wd1.myworkdayjobs.com/verify?token=abc123 "
            "or ignore. Unrelated https://example.com/help")
    assert mailbox.extract_verification(body) == "https://acme.wd1.myworkdayjobs.com/verify?token=abc123"


def test_extract_falls_back_to_code():
    assert mailbox.extract_verification("Your Workday verification code is 483920. Enter it soon.") == "483920"


def test_extract_trailing_punctuation_stripped():
    assert mailbox.extract_verification("Verify: https://x.com/activate/9.") == "https://x.com/activate/9"


def test_extract_empty():
    assert mailbox.extract_verification("no link, no code here") == ""


def test_load_config_needs_all_three(monkeypatch):
    assert mailbox.load_config({"MAILBOX_IMAP_HOST": "imap.x.com"}) is None
    cfg = mailbox.load_config({"MAILBOX_IMAP_HOST": "imap.x.com", "MAILBOX_EMAIL": "bot@x.com",
                               "MAILBOX_PASSWORD": "pw", "MAILBOX_IMAP_PORT": "1993"})
    assert cfg and cfg.host == "imap.x.com" and cfg.email == "bot@x.com" and cfg.port == 1993


def _email(frm: str, body: str) -> bytes:
    m = EmailMessage()
    m["From"] = frm
    m["Subject"] = "Verify your account"
    m.set_content(body)
    return m.as_bytes()


class _FakeIMAP:
    def __init__(self, messages):  # messages: list[bytes], oldest→newest
        self._msgs = messages

    def select(self, mailbox):  # noqa: A002 - imaplib name
        return ("OK", [b""])

    def search(self, charset, criteria):
        ids = " ".join(str(i + 1) for i in range(len(self._msgs))).encode()
        return ("OK", [ids])

    def fetch(self, mid, spec):
        raw = self._msgs[int(mid) - 1]
        return ("OK", [(b"1 (RFC822)", raw)])

    def logout(self):
        return ("BYE", [b""])


_CFG = MailboxConfig("imap.x.com", "bot@x.com", "pw")


def test_fetch_verification_newest_matching_sender():
    msgs = [
        _email("noreply@other.com", "code 111111"),                 # wrong sender
        _email("workday@myworkday.com", "code 222222"),             # older workday
        _email("Workday <no-reply@myworkdayjobs.com>", "code 333333"),  # newest workday
    ]
    got = mailbox.fetch_verification(_CFG, _connect=lambda cfg: _FakeIMAP(msgs))
    assert got == "333333"  # newest workday message wins, other-sender ignored


def test_fetch_verification_none_when_no_match():
    msgs = [_email("noreply@other.com", "code 111111")]
    assert mailbox.fetch_verification(_CFG, _connect=lambda cfg: _FakeIMAP(msgs)) == ""


def test_fetch_verification_swallows_connect_error():
    def boom(cfg):
        raise OSError("connection refused")
    assert mailbox.fetch_verification(_CFG, _connect=boom) == ""


def test_wait_for_verification_polls_until_present():
    seq = ["", "", "742199"]
    calls = {"n": 0}

    def fake_fetch():
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v

    got = mailbox.wait_for_verification(_CFG, timeout=100, poll=5, _sleep=lambda s: None, _fetch=fake_fetch)
    assert got == "742199" and calls["n"] == 3


def test_wait_for_verification_times_out():
    got = mailbox.wait_for_verification(_CFG, timeout=10, poll=5, _sleep=lambda s: None, _fetch=lambda: "")
    assert got == ""


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def _link_path():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "mailbox.yaml"


def test_suggest_host_from_domain():
    assert mailbox.suggest_host("bot@gmail.com") == "imap.gmail.com"
    assert mailbox.suggest_host("bot@outlook.com") == "outlook.office365.com"
    assert mailbox.suggest_host("bot@unknown-corp.com") == ""


def test_save_load_link_password_in_keychain_only():
    kr, p = _FakeKeyring(), _link_path()
    mailbox.save_link("imap.gmail.com", "bot@x.com", "app-pw-123", 993, backend=kr, path=p)
    # the file on disk holds host/email/port but NOT the password
    text = p.read_text()
    assert "app-pw-123" not in text and "bot@x.com" in text and "imap.gmail.com" in text
    cfg = mailbox.load_link(backend=kr, path=p)
    assert cfg and cfg.password == "app-pw-123" and cfg.source == "linked"


def test_load_config_prefers_link_over_env():
    kr, p = _FakeKeyring(), _link_path()
    mailbox.save_link("imap.gmail.com", "bot@x.com", "linkpw", backend=kr, path=p)
    env = {"MAILBOX_IMAP_HOST": "imap.env.com", "MAILBOX_EMAIL": "env@x.com", "MAILBOX_PASSWORD": "envpw"}
    cfg = mailbox.load_config(env, backend=kr, path=p)
    assert cfg.source == "linked" and cfg.email == "bot@x.com"
    # with no link, env is the fallback
    cfg2 = mailbox.load_config(env, backend=kr, path=_link_path())
    assert cfg2.source == "env" and cfg2.email == "env@x.com"


def test_link_status_and_clear():
    kr, p = _FakeKeyring(), _link_path()
    assert mailbox.link_status(backend=kr, path=p, env={})["linked"] is False
    mailbox.save_link("imap.gmail.com", "bot@x.com", "pw", backend=kr, path=p)
    st = mailbox.link_status(backend=kr, path=p, env={})
    assert st == {"linked": True, "host": "imap.gmail.com", "email": "bot@x.com", "port": 993,
                  "source": "linked", "auth": "password"}
    assert "password" not in st  # never exposed
    assert mailbox.clear_link(backend=kr, path=p) is True
    assert mailbox.load_link(backend=kr, path=p) is None
    assert kr.get_password(mailbox._KEYRING_SERVICE, "bot@x.com") is None  # keychain cleared


# --- Gmail OAuth (decision 065): read-only, no IMAP; refresh token + secret in the keychain --------

def _raw_gmail_msg(frm: str, body: str) -> str:
    import base64
    return base64.urlsafe_b64encode(_email(frm, body)).decode()


def _fake_gmail_get(listing, raw_by_id):
    """A stand-in for mailbox._gmail_get: /messages returns `listing`; /messages/<id> returns raw."""
    def get(access, path, params=None):
        if path == "/messages":
            return listing
        if path == "/profile":
            return {"emailAddress": "bot@gmail.com"}
        mid = path.rsplit("/", 1)[-1]
        return {"raw": raw_by_id[mid]}
    return get


def test_gmail_save_load_link_secret_in_keychain_only():
    kr, p = _FakeKeyring(), _link_path()
    mailbox.save_gmail_link("bot@gmail.com", "rtok-123", "cid.apps", "csecret-xyz", backend=kr, path=p)
    text = p.read_text()
    # yaml holds email/client_id/auth but NEITHER secret (refresh token or client secret)
    assert "rtok-123" not in text and "csecret-xyz" not in text
    assert "bot@gmail.com" in text and "cid.apps" in text and "oauth" in text
    cfg = mailbox.load_link(backend=kr, path=p)
    assert cfg and cfg.auth == "oauth" and cfg.refresh_token == "rtok-123"
    assert cfg.client_id == "cid.apps" and cfg.client_secret == "csecret-xyz" and cfg.source == "linked"
    assert cfg.password == ""


def test_gmail_link_status_client_id_and_clear():
    kr, p = _FakeKeyring(), _link_path()
    mailbox.save_gmail_link("bot@gmail.com", "rtok", "cid.apps", "csec", backend=kr, path=p)
    st = mailbox.link_status(backend=kr, path=p, env={})
    assert st["linked"] and st["auth"] == "oauth" and st["email"] == "bot@gmail.com"
    assert "refresh_token" not in st and "client_secret" not in st  # no secret leaks
    assert mailbox.gmail_client_id(path=p) == "cid.apps"  # non-secret, for one-click reconnect
    assert mailbox.clear_link(backend=kr, path=p) is True
    assert kr.get_password(mailbox._GMAIL_OAUTH_SERVICE, "bot@gmail.com") is None
    assert mailbox.gmail_client_id(path=p) == ""


def test_gmail_fetch_verification_reads_newest_matching():
    cfg = MailboxConfig(host="imap.gmail.com", email="bot@gmail.com", auth="oauth",
                        refresh_token="r", client_id="c", client_secret="s")
    listing = {"messages": [{"id": "new"}, {"id": "old"}]}  # API returns newest-first
    raw = {"new": _raw_gmail_msg("Workday <no-reply@workday.com>",
                                 "Confirm: https://acme.myworkdayjobs.com/verify?token=zzz"),
           "old": _raw_gmail_msg("Workday", "code 111111")}
    v = mailbox._gmail_fetch_verification(cfg, _token=lambda c: "access-tok",
                                          _get=_fake_gmail_get(listing, raw))
    assert v == "https://acme.myworkdayjobs.com/verify?token=zzz"


def test_gmail_fetch_verification_empty_on_no_messages():
    cfg = MailboxConfig(host="imap.gmail.com", email="b@gmail.com", auth="oauth", refresh_token="r")
    v = mailbox._gmail_fetch_verification(cfg, _token=lambda c: "t",
                                          _get=_fake_gmail_get({"messages": []}, {}))
    assert v == ""


def test_gmail_test_ok_and_failure():
    cfg = MailboxConfig(host="imap.gmail.com", email="bot@gmail.com", auth="oauth", refresh_token="r")
    ok, msg = mailbox._gmail_test(cfg, _token=lambda c: "t",
                                  _get=lambda a, p, q=None: {"emailAddress": "bot@gmail.com"})
    assert ok and "bot@gmail.com" in msg and "read-only" in msg

    def boom(c):
        raise RuntimeError("invalid_grant")
    ok2, msg2 = mailbox._gmail_test(cfg, _token=boom)
    assert not ok2 and "invalid_grant" in msg2 and "Reconnect" in msg2


def test_test_connection_and_fetch_route_to_oauth(monkeypatch):
    cfg = MailboxConfig(host="imap.gmail.com", email="bot@gmail.com", auth="oauth", refresh_token="r")
    monkeypatch.setattr(mailbox, "_gmail_access_token", lambda c: "tok")
    monkeypatch.setattr(mailbox, "_gmail_get",
                        _fake_gmail_get({"messages": [{"id": "m"}]},
                                        {"m": _raw_gmail_msg("workday", "code 654321")}))
    ok, _ = mailbox.test_connection(cfg)  # must NOT touch imaplib
    assert ok
    assert mailbox.fetch_verification(cfg) == "654321"


def test_connect_gmail_saves_on_success(monkeypatch):
    kr, p = _FakeKeyring(), _link_path()
    monkeypatch.setattr(mailbox, "_gmail_access_token", lambda c: "tok")
    monkeypatch.setattr(mailbox, "_gmail_get", lambda a, path, q=None: {"emailAddress": "bot@gmail.com"})
    ok, msg = mailbox.connect_gmail(
        "cid.apps", "csec", backend=kr, path=p,
        _run=lambda cid, csec, open_browser=True: ("bot@gmail.com", "rtok-live"))
    assert ok and "bot@gmail.com" in msg
    cfg = mailbox.load_link(backend=kr, path=p)
    assert cfg.auth == "oauth" and cfg.refresh_token == "rtok-live"


def test_connect_gmail_rejects_missing_refresh_token(monkeypatch):
    kr, p = _FakeKeyring(), _link_path()
    ok, msg = mailbox.connect_gmail(
        "cid", "csec", backend=kr, path=p,
        _run=lambda cid, csec, open_browser=True: ("bot@gmail.com", ""))  # Google gave no token
    assert not ok and "production" in msg.lower()
    assert not p.exists()  # nothing persisted


def test_connect_gmail_does_not_save_when_test_read_fails(monkeypatch):
    kr, p = _FakeKeyring(), _link_path()

    def boom(c):
        raise RuntimeError("revoked")
    monkeypatch.setattr(mailbox, "_gmail_access_token", boom)
    ok, msg = mailbox.connect_gmail(
        "cid", "csec", backend=kr, path=p,
        _run=lambda cid, csec, open_browser=True: ("bot@gmail.com", "rtok"))
    assert not ok
    assert mailbox.load_link(backend=kr, path=p) is None  # link-before-save: nothing stored


class _OKImap:
    def select(self, mbox):
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


def test_test_connection_ok_and_failure():
    cfg = MailboxConfig("imap.x.com", "bot@x.com", "pw")
    ok, msg = mailbox.test_connection(cfg, _connect=lambda c: _OKImap())
    assert ok is True and "Connected" in msg

    def boom(c):
        raise OSError("AUTHENTICATIONFAILED")
    ok2, msg2 = mailbox.test_connection(cfg, _connect=boom)
    assert ok2 is False and "app password" in msg2  # actionable message


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        kw = {"monkeypatch": None} if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount] else {}
        fn(**kw)
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

"""Credential-store tests (decision 050) — in-memory keyring fake, no real keychain, temp index.

Verifies passwords round-trip through the (injected) keychain, the git-ignored index lists
tenants without touching secrets, tenant keys derive from a URL, and delete clears both.

Run:  python -m tests.test_credentials   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot import credentials
from applicationbot.credentials import Account


class _FakeKeyring:
    """Stands in for the keyring module: a dict keyed by (service, username)."""
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


def _idx():
    return str(Path(tempfile.mkdtemp()) / "workday_accounts.json")


def test_tenant_of_from_url_and_host():
    assert credentials.tenant_of("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/1") == "acme.wd1.myworkdayjobs.com"
    assert credentials.tenant_of("Acme.WD1.myworkdayjobs.com") == "acme.wd1.myworkdayjobs.com"
    assert credentials.tenant_of("acme.wd1.myworkdayjobs.com/careers") == "acme.wd1.myworkdayjobs.com"


def test_save_get_roundtrip():
    kr, idx = _FakeKeyring(), _idx()
    acct = Account("acme.wd1.myworkdayjobs.com", "bot@example.com", "s3cret!")
    credentials.save_account(acct, backend=kr, index_path=idx)
    got = credentials.get_account("https://acme.wd1.myworkdayjobs.com/job/1", backend=kr, index_path=idx)
    assert got and got.email == "bot@example.com" and got.password == "s3cret!"
    assert credentials.has_account("acme.wd1.myworkdayjobs.com", backend=kr, index_path=idx)


def test_password_not_in_index_file():
    kr, idx = _FakeKeyring(), _idx()
    credentials.save_account(Account("acme.wd1.myworkdayjobs.com", "bot@x.com", "TOPSECRET"),
                             backend=kr, index_path=idx)
    text = Path(idx).read_text()
    assert "TOPSECRET" not in text and "bot@x.com" in text  # only email in the plaintext index


def test_list_accounts_is_secret_free():
    kr, idx = _FakeKeyring(), _idx()
    credentials.save_account(Account("a.myworkdayjobs.com", "a@x.com", "pw1"), backend=kr, index_path=idx)
    credentials.save_account(Account("b.myworkdayjobs.com", "b@x.com", "pw2"), backend=kr, index_path=idx)
    rows = credentials.list_accounts(idx)
    assert rows == [("a.myworkdayjobs.com", "a@x.com"), ("b.myworkdayjobs.com", "b@x.com")]


def test_missing_account_is_none():
    kr, idx = _FakeKeyring(), _idx()
    assert credentials.get_account("nope.myworkdayjobs.com", backend=kr, index_path=idx) is None
    assert not credentials.has_account("nope.myworkdayjobs.com", backend=kr, index_path=idx)


def test_delete_clears_index_and_keychain():
    kr, idx = _FakeKeyring(), _idx()
    credentials.save_account(Account("acme.myworkdayjobs.com", "a@x.com", "pw"), backend=kr, index_path=idx)
    assert credentials.delete_account("acme.myworkdayjobs.com", backend=kr, index_path=idx) is True
    assert credentials.list_accounts(idx) == []
    assert credentials.get_account("acme.myworkdayjobs.com", backend=kr, index_path=idx) is None
    # deleting an unknown tenant reports False, doesn't crash
    assert credentials.delete_account("ghost.myworkdayjobs.com", backend=kr, index_path=idx) is False


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

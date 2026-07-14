"""MyGreenhouse password → OS keychain migration (decision 060).

Verifies the password never lands in YAML or a `GET /profile` payload, round-trips through the
(injected) keychain, the legacy plaintext value auto-migrates out of the YAML on load, and the
autofill read path falls back to a not-yet-migrated plaintext value. In-memory keyring fake,
temp files — no real keychain.

Run:  python -m pytest tests/test_greenhouse_creds.py -q
"""
from __future__ import annotations

import pytest
import yaml

from applicationbot import apply_profile
from applicationbot.apply_profile import ApplicationProfile


class _FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture
def fake_kr(monkeypatch):
    kr = _FakeKeyring()
    monkeypatch.setattr(apply_profile, "_gh_keyring", lambda: kr)
    return kr


# --------------------------------------------------------------------- keychain round-trip

def test_password_round_trips_through_the_keychain(fake_kr):
    apply_profile.set_greenhouse_password("hunter2")
    assert apply_profile.get_greenhouse_password() == "hunter2"
    assert fake_kr.store[(apply_profile._GH_SERVICE, apply_profile._GH_ACCOUNT)] == "hunter2"
    apply_profile.set_greenhouse_password("")  # clear
    assert apply_profile.get_greenhouse_password() == ""


def test_linked_requires_email_and_stored_password(fake_kr):
    prof = ApplicationProfile(greenhouse_email="me@x.com")
    assert apply_profile.greenhouse_linked(prof) is False
    apply_profile.set_greenhouse_password("pw")
    assert apply_profile.greenhouse_linked(prof) is True
    assert apply_profile.greenhouse_linked(ApplicationProfile()) is False  # no email


def test_credentials_prefers_keychain_then_falls_back_to_legacy_plaintext(fake_kr):
    prof = ApplicationProfile(greenhouse_email="me@x.com", greenhouse_password="legacy")
    # Keychain empty → fall back to the not-yet-migrated plaintext so autofill still works.
    assert apply_profile.greenhouse_credentials(prof) == ("me@x.com", "legacy")
    apply_profile.set_greenhouse_password("fromchain")
    assert apply_profile.greenhouse_credentials(prof) == ("me@x.com", "fromchain")  # keychain wins


# --------------------------------------------------------------------- never in YAML

def test_save_profile_never_writes_the_password(tmp_path, fake_kr):
    path = tmp_path / "application_profile.yaml"
    apply_profile.save_profile(
        ApplicationProfile(greenhouse_email="me@x.com", greenhouse_password="secret"), path)
    on_disk = yaml.safe_load(path.read_text())
    assert "greenhouse_password" not in on_disk
    assert on_disk["greenhouse_email"] == "me@x.com"


# --------------------------------------------------------------------- one-time migration

def test_load_profile_migrates_legacy_plaintext_out_of_yaml(tmp_path, fake_kr):
    path = tmp_path / "application_profile.yaml"
    path.write_text("greenhouse_email: me@x.com\ngreenhouse_password: oldsecret\n")

    prof = apply_profile.load_profile(path)

    # Moved into the keychain, blanked in memory, and scrubbed from the file.
    assert apply_profile.get_greenhouse_password() == "oldsecret"
    assert prof.greenhouse_password == ""
    assert "greenhouse_password" not in yaml.safe_load(path.read_text())
    # Idempotent: a second load doesn't error and leaves the keychain intact.
    apply_profile.load_profile(path)
    assert apply_profile.get_greenhouse_password() == "oldsecret"


def test_load_profile_no_migration_when_no_plaintext(tmp_path, fake_kr):
    path = tmp_path / "application_profile.yaml"
    path.write_text("greenhouse_email: me@x.com\n")
    apply_profile.load_profile(path)
    assert apply_profile.get_greenhouse_password() == ""  # nothing to migrate

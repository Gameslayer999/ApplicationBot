"""MyGreenhouse Quick Apply: opt-in gating and the retired password (decision 182).

Greenhouse replaced its password sign-in with an emailed security code, so there is no password
to store and the login depends on the linked inbox. These tests pin the three things that keeps
honest: Quick Apply is OFF unless the user turns it on, every blocked state names its own fix
(UI principle #3), and the password decision 060 put in the keychain/YAML is deleted rather than
left lying around (Guideline #12). In-memory keyring fake, temp files — no real keychain.

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


class _Mailbox:
    """Stand-in for mailbox.MailboxConfig — only `.email` is read here."""
    def __init__(self, email):
        self.email = email


@pytest.fixture
def fake_kr(monkeypatch):
    kr = _FakeKeyring()
    monkeypatch.setattr(apply_profile, "_gh_keyring", lambda: kr)
    monkeypatch.setattr(apply_profile, "_GH_PW_CLEARED", False)  # the once-per-process latch
    return kr


# --------------------------------------------------------------------- opt-in gating

def test_quick_apply_is_off_by_default():
    prof = ApplicationProfile(greenhouse_email="me@x.com")
    assert prof.greenhouse_quick_apply is False
    problem = apply_profile.greenhouse_quick_apply_problem(prof, config=_Mailbox("me@x.com"))
    assert "off" in problem.lower() and "Native autofill logins" in problem


def test_ready_when_on_and_the_account_email_is_the_linked_inbox():
    prof = ApplicationProfile(greenhouse_quick_apply=True, greenhouse_email="Me@X.com")
    assert apply_profile.greenhouse_quick_apply_problem(prof, config=_Mailbox("me@x.com")) == ""


def test_on_without_an_email_says_where_to_add_it():
    prof = ApplicationProfile(greenhouse_quick_apply=True)
    problem = apply_profile.greenhouse_quick_apply_problem(prof, config=_Mailbox("me@x.com"))
    assert "no account email" in problem and "Native autofill logins" in problem


def test_on_without_a_linked_inbox_points_at_settings(monkeypatch):
    monkeypatch.setattr("applicationbot.mailbox.load_config", lambda *a, **k: None)
    prof = ApplicationProfile(greenhouse_quick_apply=True, greenhouse_email="me@x.com")
    problem = apply_profile.greenhouse_quick_apply_problem(prof)
    assert "security code" in problem and "Linked inbox" in problem


def test_mismatched_addresses_name_both_sides_and_both_fixes():
    # The code goes to the MyGreenhouse account; reading a DIFFERENT inbox would wait forever.
    prof = ApplicationProfile(greenhouse_quick_apply=True, greenhouse_email="me@personal.com")
    problem = apply_profile.greenhouse_quick_apply_problem(prof, config=_Mailbox("bot@gmail.com"))
    assert "me@personal.com" in problem and "bot@gmail.com" in problem


# --------------------------------------------------------------------- the retired password

def test_load_profile_deletes_the_dead_password_from_keychain_and_yaml(tmp_path, fake_kr):
    path = tmp_path / "application_profile.yaml"
    path.write_text("greenhouse_email: me@x.com\ngreenhouse_password: oldsecret\n")
    fake_kr.store[(apply_profile._GH_SERVICE, apply_profile._GH_ACCOUNT)] = "oldsecret"

    prof = apply_profile.load_profile(path)

    assert prof.greenhouse_email == "me@x.com"          # the non-secret half survives
    assert not hasattr(prof, "greenhouse_password")     # field is gone from the model
    assert fake_kr.store == {}                          # keychain entry deleted
    assert "greenhouse_password" not in yaml.safe_load(path.read_text())


def test_save_profile_writes_the_opt_in_and_no_password(tmp_path, fake_kr):
    path = tmp_path / "application_profile.yaml"
    apply_profile.save_profile(
        ApplicationProfile(greenhouse_quick_apply=True, greenhouse_email="me@x.com"), path)
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk["greenhouse_quick_apply"] is True
    assert on_disk["greenhouse_email"] == "me@x.com"
    assert "greenhouse_password" not in on_disk


def test_apply_stage_never_touches_the_page_when_quick_apply_is_off():
    """Off is the default, so this runs on EVERY Greenhouse posting — it must cost nothing and
    say nothing. `_Explodes` fails the test if any browser call is attempted."""
    from applicationbot.apply import ApplyReport, _greenhouse_native_autofill

    class _Explodes:
        def __getattr__(self, name):
            raise AssertionError(f"touched the browser ({name}) with Quick Apply off")

    report = ApplyReport(url="https://boards.greenhouse.io/acme/jobs/1")
    _greenhouse_native_autofill(_Explodes(), _Explodes(), ApplicationProfile(), report)
    assert report.errors == [] and report.native_autofill is None


def test_apply_stage_reports_the_blocker_when_on_but_unusable(monkeypatch):
    from applicationbot.apply import ApplyReport, _greenhouse_native_autofill

    monkeypatch.setattr("applicationbot.mailbox.load_config", lambda *a, **k: None)

    class _Explodes:
        def __getattr__(self, name):
            raise AssertionError(f"touched the browser ({name}) before the setup check")

    prof = ApplicationProfile(greenhouse_quick_apply=True, greenhouse_email="me@x.com")
    report = ApplyReport(url="https://boards.greenhouse.io/acme/jobs/1")
    _greenhouse_native_autofill(_Explodes(), _Explodes(), prof, report)
    assert len(report.errors) == 1
    assert "Linked inbox" in report.errors[0] and "Filling the form without it" in report.errors[0]


# --------------------------------------------------------------------- the emailed-code sign-in

class _Loc:
    """A Playwright-locator stand-in: records fills/clicks, reports itself as one visible node."""
    def __init__(self, log, label, count=1):
        self.log, self.label, self._count = log, label, count

    @property
    def first(self):
        return self

    def nth(self, i):
        return _Loc(self.log, f"{self.label}[{i}]")

    def count(self):
        return self._count

    def is_visible(self):
        return True

    def fill(self, value, timeout=None):
        self.log.append(("fill", self.label, value))

    def click(self, timeout=None):
        self.log.append(("click", self.label))


class _Popup:
    def __init__(self, log):
        self.log = log

    def get_by_label(self, pat):
        return _Loc(self.log, "email-field")

    def get_by_role(self, role, name=None):
        return _Loc(self.log, "button")

    def locator(self, sel):
        return _Loc(self.log, "code-field")  # one field for the whole code

    def wait_for_load_state(self, state):
        pass

    def wait_for_event(self, event, timeout=None):
        pass

    def close(self):
        self.log.append(("close-popup",))


class _Page:
    def __init__(self, log):
        self.log = log

    def get_by_role(self, role, name=None):
        return _Loc(self.log, "quick-apply-button")

    def wait_for_timeout(self, ms):
        pass


class _Ctx:
    def __init__(self, popup):
        self.popup = popup

    def expect_page(self, timeout=None):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            class _Info:
                pass
            info = _Info()
            yield info
            info.value = self.popup
        return _cm()


def test_signs_in_with_a_fresh_emailed_code(monkeypatch):
    from applicationbot.apply import ApplyReport, _greenhouse_native_autofill

    log, seen = [], {}
    monkeypatch.setattr("applicationbot.mailbox.load_config", lambda *a, **k: _Mailbox("me@x.com"))

    def fake_wait(config, **kw):
        seen.update(kw)
        return "654321"

    monkeypatch.setattr("applicationbot.mailbox.wait_for_verification", fake_wait)

    popup = _Popup(log)
    prof = ApplicationProfile(greenhouse_quick_apply=True, greenhouse_email="me@x.com")
    report = ApplyReport(url="https://boards.greenhouse.io/acme/jobs/1")
    _greenhouse_native_autofill(_Page(log), _Ctx(popup), prof, report)

    assert report.errors == []
    assert report.native_autofill == "greenhouse: MyGreenhouse (Quick Apply)"
    assert ("fill", "email-field", "me@x.com") in log
    assert ("fill", "code-field", "654321") in log
    assert ("close-popup",) not in log  # a signed-in popup is left for Greenhouse to close
    # The code is read from Greenhouse only, must be newer than the request, and is taken as
    # digits rather than as a link in the same email.
    assert seen["sender_contains"] == "greenhouse" and seen["prefer_code"] is True
    assert seen["since_epoch"] is not None


def test_a_code_that_never_arrives_falls_back_and_closes_the_popup(monkeypatch):
    from applicationbot.apply import ApplyReport, _greenhouse_native_autofill

    log = []
    monkeypatch.setattr("applicationbot.mailbox.load_config", lambda *a, **k: _Mailbox("me@x.com"))
    monkeypatch.setattr("applicationbot.mailbox.wait_for_verification", lambda config, **kw: "")

    prof = ApplicationProfile(greenhouse_quick_apply=True, greenhouse_email="me@x.com")
    report = ApplyReport(url="https://boards.greenhouse.io/acme/jobs/1")
    _greenhouse_native_autofill(_Page(log), _Ctx(_Popup(log)), prof, report, verify_wait=30)

    assert report.native_autofill is None            # fall back to our own fill
    assert ("close-popup",) in log                   # no half-finished sign-in tab left behind
    assert "never arrived in me@x.com within 30s" in report.errors[0]


def test_cleanup_is_idempotent_and_survives_a_broken_keychain(tmp_path, monkeypatch):
    path = tmp_path / "application_profile.yaml"
    path.write_text("greenhouse_email: me@x.com\n")

    def boom():
        raise RuntimeError("no keychain here")

    monkeypatch.setattr(apply_profile, "_gh_keyring", boom)
    monkeypatch.setattr(apply_profile, "_GH_PW_CLEARED", False)
    assert apply_profile.load_profile(path).greenhouse_email == "me@x.com"  # load still works
    assert apply_profile.load_profile(path).greenhouse_email == "me@x.com"  # and again

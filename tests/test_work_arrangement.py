"""Work-arrangement preference resolution (decision: commutability-aware remote answers).

The reported bug: with only a global `open_to_remote=True` boolean, the resolver signalled
"I'd work remotely" for a posting whose office the applicant is within commuting range of and
would rather commute to. These tests pin the new `work_arrangement` setting + Claude-judged
commutability, without spawning the CLI (it's patched out).

Run:  python -m pytest tests/test_work_arrangement.py -q
"""
from __future__ import annotations

from contextlib import contextmanager

from applicationbot import backends
from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Resume


@contextmanager
def _fake_claude(reply):
    """Patch backends.run_claude_cli to return `reply` (None = raise, i.e. CLI unavailable).
    Yields the prompts sent so a test can assert Claude was (or wasn't) consulted."""
    calls: list[str] = []

    def fake(prompt, **kw):
        calls.append(prompt)
        if reply is None:
            raise RuntimeError("claude CLI unavailable")
        return reply

    real = backends.run_claude_cli
    backends.run_claude_cli = fake
    try:
        yield calls
    finally:
        backends.run_claude_cli = real


def _resume():
    return Resume(contact=Contact(name="Jamie Rivera", email="j@x.com", location="Edison, NJ"))


def _resolver(**profile_kw):
    prof = ApplicationProfile(location="Edison, NJ", **profile_kw)
    return AnswerResolver(resume=_resume(), profile=prof, enable_generation=True,
                          jd="We are hiring in our New York, NY office. Hybrid, 3 days/week.")


# --------------------------------------------------------------- bare willingness unchanged

def test_bare_open_to_remote_still_yes():
    """A yes/no "Are you open to remote work?" stays answered from open_to_remote — the
    applicant IS open, whatever their office preference (the chosen behaviour)."""
    r = _resolver(open_to_remote=True, work_arrangement="in_office_if_commutable")
    with _fake_claude('{"commutable": true}'):  # not consulted for a bare yes/no
        assert r.resolve("Are you open to working remotely?") == "Yes"


# --------------------------------------------------------------- explicit preferences

def test_always_in_office_prefers_onsite():
    r = _resolver(work_arrangement="in_office")
    assert r.resolve("What is your preferred work arrangement?") == "On-site"


def test_always_remote_prefers_remote():
    r = _resolver(work_arrangement="remote")
    assert r.resolve("Preferred work arrangement?") == "Remote"


def test_hybrid_preference():
    r = _resolver(work_arrangement="hybrid")
    assert r.resolve("Do you prefer remote, hybrid, or on-site?") == "Hybrid"


def test_no_preference_falls_back_to_open_to_remote():
    """work_arrangement unset → arrangement questions fall through to the legacy yes/no."""
    r = _resolver(open_to_remote=True)
    assert r.resolve("Do you prefer to work remotely?") == "Yes"


# --------------------------------------------------------------- commutability (Claude-judged)

def test_commutable_office_prefers_onsite():
    r = _resolver(work_arrangement="in_office_if_commutable", max_commute_miles=35)
    with _fake_claude('{"commutable": true}') as calls:
        assert r.resolve("Preferred work arrangement?") == "On-site"
    assert calls and "Edison, NJ" in calls[0] and "35 miles" in calls[0]


def test_non_commutable_office_prefers_remote():
    r = _resolver(work_arrangement="in_office_if_commutable")
    with _fake_claude('{"commutable": false}'):
        assert r.resolve("Preferred work arrangement?") == "Remote"


def test_commutability_judged_once_and_cached():
    r = _resolver(work_arrangement="in_office_if_commutable")
    with _fake_claude('{"commutable": true}') as calls:
        r.resolve("Preferred work arrangement?")
        r.resolve("Which work model do you prefer?")
        r.option_hints("Preferred work arrangement?")
    assert len(calls) == 1  # one posting → one commutability call, then cached


def test_unknown_commutability_falls_through():
    """CLI unavailable → commutability None → the preference doesn't force an answer; the
    arrangement question falls through to the bare open_to_remote yes/no instead of guessing."""
    r = _resolver(work_arrangement="in_office_if_commutable", open_to_remote=True)
    with _fake_claude(None):
        assert r.resolve("Do you prefer to work remotely?") == "Yes"


# --------------------------------------------------------------- office-location dropdown fix

def test_office_prefs_no_remote_for_in_office_preference():
    """The reported bug: empty preferred_locations + open_to_remote ranked Remote first even for
    a commutable office. With an in-office preference, Remote is never offered for the office
    dropdown; the home city is the fallback."""
    r = _resolver(open_to_remote=True, work_arrangement="in_office_if_commutable")
    with _fake_claude('{"commutable": true}'):
        prefs = r._office_prefs()
    assert "Remote" not in prefs
    assert prefs and prefs[0] == "Edison, NJ"


def test_office_prefs_remote_first_when_remote_preference():
    r = _resolver(open_to_remote=True, work_arrangement="remote")
    prefs = r._office_prefs()
    assert prefs[0] == "Remote"


def test_office_prefs_legacy_behaviour_no_preference():
    """No preference set → unchanged legacy behaviour: Remote appended when open_to_remote."""
    r = _resolver(open_to_remote=True)
    prefs = r._office_prefs()
    assert "Remote" in prefs


# --------------------------------------------------------------- hints map onto form wording

def test_arrangement_hints_for_onsite():
    r = _resolver(work_arrangement="in_office")
    hints = r.option_hints("Preferred work arrangement?")
    assert hints and "On-site" in hints and any("office" in h.lower() for h in hints)


if __name__ == "__main__":
    import sys
    ok = True
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok  {name}")
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"FAIL {name}: {e}")
    sys.exit(0 if ok else 1)

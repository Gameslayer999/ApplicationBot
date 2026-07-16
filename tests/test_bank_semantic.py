"""Semantic answer-bank matching (decision 036) — a banked answer is reused for any
rewording of its question, not only the literal phrasing it was saved under. No subprocess,
no network: the Claude CLI is patched out.

Run:  python -m tests.test_bank_semantic   (also pytest-compatible)
"""
from __future__ import annotations

from contextlib import contextmanager

from applicationbot import answer_bank, backends
from applicationbot.answer_bank import match_banked_question
from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import QA, ApplicationProfile
from applicationbot.models import Contact, Resume


@contextmanager
def _fake_claude(reply: str | None):
    """Patch backends.run_claude_cli to return `reply` (None = raise, i.e. CLI unavailable).
    Yields the list of prompts sent, so tests can assert whether Claude was consulted."""
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


BANK = [("Are you willing to travel up to 25% of the time?", "Yes"),
        ("How many years of experience do you have with Python?", "4")]


def test_match_parses_index():
    with _fake_claude('{"match": 1}'):
        assert match_banked_question("Years of Python experience?", BANK) == 1


def test_match_no_match_and_malformed_reply_reject():
    with _fake_claude('{"match": -1}'):
        assert match_banked_question("Do you have a security clearance?", BANK) is None
    with _fake_claude("not json at all"):
        assert match_banked_question("Do you have a security clearance?", BANK) is None


def test_match_out_of_range_and_cli_failure():
    with _fake_claude('{"match": 7}'):
        assert match_banked_question("Years of Python experience?", BANK) is None
    with _fake_claude(None):
        assert match_banked_question("Years of Python experience?", BANK) is None


def test_match_guards_skip_claude():
    # Company-specific / demographic / empty-bank questions never reach Claude.
    for q, bank in [("Why do you want to work here?", BANK),
                    ("What is your gender?", BANK),
                    ("Years of Python experience?", [])]:
        with _fake_claude('{"match": 0}') as calls:
            assert match_banked_question(q, bank) is None
            assert calls == []


def _resolver(**custom) -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    profile = ApplicationProfile(**custom)
    return AnswerResolver(resume=resume, profile=profile, enable_generation=True)


def test_resolver_reuses_banked_answer_and_learns_alias():
    r = _resolver(custom_answers=[QA(question=BANK[0][0], answer="Yes"),
                                  QA(question=BANK[1][0], answer="4")])
    # classify_question is asked first (replies none), then the bank match (replies 1).
    with _fake_claude('{"type": "none"}') as calls:
        # Force the bank-match call to reply match=1 by patching per-call.
        replies = iter(['{"type": "none"}', '{"match": 1}'])
        backends.run_claude_cli = lambda p, **kw: (calls.append(p) or next(replies))
        got = r.resolve_semantic("What is your total Python experience, in years?")
    assert got == "4"
    assert len(calls) == 2  # classify, then bank match
    # The new phrasing is cached as an alias carrying the same answer.
    assert any(qa.question == "What is your total Python experience, in years?"
               and qa.answer == "4" for qa in r.learned)


def test_resolver_mapped_entry_answers_live_from_profile():
    # A banked entry with maps_to has a blank answer; the reused value must come from the
    # CURRENT profile field, and the learned alias must carry maps_to (not a stale copy).
    r = _resolver(open_to_remote=True,
                  custom_answers=[QA(question="Willing to work from our office 3 days a week?",
                                     answer="", maps_to="open_to_remote")])
    replies = iter(['{"type": "none"}', '{"match": 0}'])
    with _fake_claude("unused"):
        backends.run_claude_cli = lambda p, **kw: next(replies)
        got = r.resolve_semantic("Are you comfortable with a hybrid schedule?")
    assert got == "Yes"
    alias = r.learned[-1]
    assert alias.maps_to == "open_to_remote" and alias.answer == ""


def test_freetext_consults_bank_before_drafting():
    # A short text field (not open-ended) used to be skippable only; now a reworded banked
    # answer fills it — and no draft is generated.
    r = _resolver(custom_answers=[QA(question=BANK[1][0], answer="4")])
    with _fake_claude('{"match": 0}'):
        ans, source = r.freetext_answer("Python — years of experience")
    assert (ans, source) == ("4", "resolver")


def test_freetext_no_match_still_captures():
    r = _resolver(custom_answers=[QA(question=BANK[0][0], answer="Yes")])
    with _fake_claude('{"match": -1}'):
        ans, source = r.freetext_answer("Do you hold a driver's license?")
    assert (ans, source) == (None, "")


def _main() -> int:
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                fails += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""Determinism gates for the autofill learning loop — no subprocess, no network.

Covers: `valid_mapping` (the write-time gate that keeps a wrong Claude classification out of
the answer bank), `remember_answers` enforcing it at the persistence layer, `learn_option`
refusing generic boolean dropdown aliases, and the schema-constrained JSON replies of
`classify_question` / `pick_dropdown_option`.

Run:  python -m tests.test_determinism_gates   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path

from applicationbot import answer_bank, backends
from applicationbot.answer_bank import classify_question, pick_dropdown_option, valid_mapping
from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import QA, ApplicationProfile, load_profile, remember_answers, save_profile
from applicationbot.models import Contact, Resume


@contextmanager
def _fake_claude(reply: str | None):
    """Patch backends.run_claude_cli to return `reply` (None = raise). Yields the prompts sent."""
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


# ------------------------------------------------------------------ valid_mapping

def test_valid_mapping_accepts_a_normal_semantic_match():
    assert valid_mapping("Willing to work from our office 3 days a week?", "open_to_remote")


def test_valid_mapping_rejects_pollution_vectors():
    # Enumerated-answer questions (the SpaceX incident), demographic, company-specific,
    # garbage-length questions, and unknown type keys must all be refused.
    assert not valid_mapping("SpaceX Employment History", "work_authorized")
    assert not valid_mapping("Do you have an active security clearance?", "us_citizen")
    assert not valid_mapping("What is your gender identity?", "location")
    assert not valid_mapping("Why do you want to work here?", "how_heard")
    assert not valid_mapping("yes", "country")
    assert not valid_mapping("Where are you located?", "not_a_real_type")


# ------------------------------------------------------------------ remember_answers gate

def _tmp_profile() -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "application_profile.yaml"
    save_profile(ApplicationProfile(), p)
    return p


def test_remember_answers_refuses_invalid_mapping_but_keeps_answer_text():
    p = _tmp_profile()
    added = remember_answers([
        QA(question="Do you have an active security clearance?",
           answer="Never held a clearance", maps_to="us_citizen", generated=True),
    ], p)
    assert added == 1
    got = load_profile(p).custom_answers[0]
    assert got.maps_to == ""  # the invalid mapping is dropped at write time…
    assert got.answer == "Never held a clearance"  # …the answer text survives


def test_remember_answers_drops_mapping_only_entries_with_invalid_mapping():
    p = _tmp_profile()
    # A mapping-only entry (blank answer) whose mapping is invalid has no content left — skip it.
    added = remember_answers([
        QA(question="SpaceX Employment History", answer="", maps_to="work_authorized", generated=True),
    ], p)
    assert added == 0
    assert load_profile(p).custom_answers == []


def test_remember_answers_keeps_valid_mapping_and_rejects_garbage_questions():
    p = _tmp_profile()
    added = remember_answers([
        QA(question="Comfortable with a hybrid schedule?", answer="", maps_to="open_to_remote"),
        QA(question="yes", answer="yes"),  # garbage capture
    ], p)
    assert added == 1
    assert load_profile(p).custom_answers[0].maps_to == "open_to_remote"


# ------------------------------------------------------------------ learn_option gate

def _resolver() -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    return AnswerResolver(resume=resume, profile=ApplicationProfile())


def test_learn_option_refuses_generic_boolean_values():
    # Aliases are keyed by value alone — a "yes" alias learned on one question would become a
    # match candidate for EVERY future Yes/No dropdown, so booleans are never learned.
    r = _resolver()
    r.learn_option("Yes", "I am authorized to work in the United States for any employer")
    r.learn_option("no", "I have never held a clearance")
    assert r.learned_options == {}


def test_learn_option_still_learns_real_values():
    r = _resolver()
    r.learn_option("The Pennsylvania State University", "Pennsylvania State University-Main Campus")
    assert r.learned_option_hints("the  pennsylvania state university") == [
        "Pennsylvania State University-Main Campus"]


# ------------------------------------------------------------------ schema-constrained replies

def test_classify_question_parses_schema_reply_and_rejects_unknown():
    with _fake_claude('{"type": "open_to_remote"}'):
        assert classify_question("OK working onsite 3 days a week?") == "open_to_remote"
    with _fake_claude('{"type": "none"}'):
        assert classify_question("Do you own a car?") is None
    with _fake_claude("not json"):
        assert classify_question("Do you own a car?") is None
    with _fake_claude(None):
        assert classify_question("Do you own a car?") is None


def test_classify_question_guards_never_reach_claude():
    for q in ("Why do you want to work here?", "What is your gender?",
              "Do you have an active security clearance?"):
        with _fake_claude('{"type": "location"}') as calls:
            assert classify_question(q) is None
            assert calls == []


def test_numeric_fact_questions_are_never_open_ended():
    # A salary/GPA figure is a fact the applicant must own — drafting one fabricates data
    # (live AppLovin dry-run 2026-07-09 drafted a salary grounded in nothing).
    q = ("Please provide your annual gross salary expectation (in local currency and "
         "numerical format without special characters)")
    assert not answer_bank.is_open_ended(q)
    assert not answer_bank.is_open_ended(q, is_textarea=True)  # textarea doesn't override
    assert not answer_bank.is_open_ended("Please provide your cumulative GPA", is_textarea=True)
    assert answer_bank.is_open_ended("Please describe your experience with Python and Go")


def test_salary_rule_with_no_data_falls_through_to_bank():
    # An empty desired_salary must not short-circuit resolve(): a USER-entered banked answer
    # to the same question still wins (the early `return None` used to skip the bank).
    q = "What is your annual salary expectation?"
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    profile = ApplicationProfile(custom_answers=[QA(question=q, answer="132000")])
    r = AnswerResolver(resume=resume, profile=profile)
    assert r.resolve(q) == "132000"
    # With data present the salary machinery still wins over the bank.
    r2 = AnswerResolver(resume=resume, profile=profile.model_copy(
        update={"desired_salary": "140000"}))
    assert r2.resolve(q) == "140000"


def test_freetext_never_drafts_a_salary_question():
    q = "Please provide your annual gross salary expectation for this role"
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    r = AnswerResolver(resume=resume, profile=ApplicationProfile(), enable_generation=True)
    with _fake_claude('{"match": -1}'):  # bank-match declines; drafting must not follow
        ans, source = r.freetext_answer(q, is_textarea=True)
    assert (ans, source) == (None, "")  # captured for the user, no fabricated figure


def test_pick_dropdown_option_parses_choice_and_keeps_token_guard():
    opts = ["Harvard University", "Pennsylvania State University-Main Campus", "Stanford University"]
    with _fake_claude('{"choice": 1}'):
        assert pick_dropdown_option("School", "The Pennsylvania State University", opts) \
            == "Pennsylvania State University-Main Campus"
    with _fake_claude('{"choice": -1}'):
        assert pick_dropdown_option("School", "The Pennsylvania State University", opts) is None
    # Token guard: an unrelated pick is rejected even if Claude returns a valid index.
    with _fake_claude('{"choice": 0}'):
        assert pick_dropdown_option("School", "The Pennsylvania State University", opts) is None
    # Boolean exemption: "Yes" may map to a descriptive option sharing no token.
    with _fake_claude('{"choice": 0}'):
        assert pick_dropdown_option(
            "Are you legally authorized to work in the United States?", "Yes",
            ["I am authorized to work in the United States for any employer",
             "I am not authorized to work in the United States"]) \
            == "I am authorized to work in the United States for any employer"


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

"""Required unmapped free-text fields get a weak-model Claude draft (this session's change).

A required field must be filled to submit, whatever its phrasing — so `freetext_answer(required=True)`
drafts even a short, non-"open-ended" question, EXCEPT numeric-fact/demographic questions we must
never fabricate. Drafts use the weak DRAFT_MODEL by default. Pure/stubbed — no browser, zero tokens.
"""
from __future__ import annotations

from applicationbot import answer_bank, backends
from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Resume


def _resolver() -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    profile = ApplicationProfile(first_name="Test", last_name="User", email="t@example.com")
    return AnswerResolver(resume=resume, profile=profile, enable_generation=True,
                          company="WHOOP", jd="Build wearable health tech.")


# A short, unusually-phrased custom question — NOT open-ended by the phrase heuristic.
SHORT_Q = "Why WHOOP?"


def test_is_draftable_required_gates():
    assert answer_bank.is_draftable_required(SHORT_Q)
    assert answer_bank.is_draftable_required("What's your favorite programming language?")
    # Never fabricate numeric facts or demographics — these stay for the user.
    assert not answer_bank.is_draftable_required("What is your expected salary?")
    assert not answer_bank.is_draftable_required("What is your GPA?")
    assert not answer_bank.is_draftable_required("What is your gender?")
    # Years of experience is a numeric fact the applicant owns: never guessed from the résumé
    # (a new grad with 0 full-time years was mis-answered "1-5" by counting internships/projects).
    assert not answer_bank.is_draftable_required(
        "How many years of experience full time, industry do you have as a software engineer? "
        "(not counting internships or school work)")
    assert not answer_bank.is_draftable_required("")


def test_required_short_question_gets_drafted(monkeypatch):
    monkeypatch.setattr(answer_bank, "generate_answer", lambda *a, **k: "Because their mission fits.")
    r = _resolver()
    # "Why WHOOP?" is a company-specific motivational prompt: now drafted whether or not it's
    # required (an OPTIONAL "Why <company>?" left blank was the Ramp bug, 2026-07-14).
    assert r.freetext_answer(SHORT_Q, is_textarea=False, required=False) == (
        "Because their mission fits.", "generated")
    assert r.freetext_answer(SHORT_Q, is_textarea=False, required=True) == (
        "Because their mission fits.", "generated")
    # A genuinely arbitrary short field (not open-ended, not company-specific) is still left for
    # the user when optional, and only drafted when REQUIRED (decision 067).
    assert r.freetext_answer("Favorite color?", is_textarea=False, required=False) == (None, "")
    assert r.freetext_answer("Favorite color?", is_textarea=False, required=True) == (
        "Because their mission fits.", "generated")


def test_required_numeric_fact_never_drafted(monkeypatch):
    monkeypatch.setattr(answer_bank, "generate_answer",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not draft")))
    r = _resolver()
    assert r.freetext_answer("What is your expected salary?", required=True) == (None, "")


def test_generate_answer_defaults_to_weak_model(monkeypatch):
    seen = {}

    def fake_cli(prompt, **kw):
        seen["model"] = kw.get("model")
        return "drafted answer"

    monkeypatch.setattr(backends, "run_claude_cli", fake_cli)
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    answer_bank.generate_answer("Describe your experience.", resume)
    assert seen["model"] == answer_bank.DRAFT_MODEL == "haiku"

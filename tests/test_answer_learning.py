"""Answers edited in Review are LEARNED, not just applied to that posting (decision 155).

An edit the user makes while reviewing an application is their own answer to that question, so
the next posting that asks it must use it instead of leaving the field blank again or repeating
the answer they replaced. This covers the bank write itself (`apply_profile.upsert_answers`),
the policy gate (`answer_bank.is_reusable_answer`), the bucketing web does on a save, and the
end-to-end effect: after the edit, the resolver answers that question on a DIFFERENT posting.
"""
from __future__ import annotations

import pytest

from applicationbot import answer_bank, apply_profile, web
from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import QA, ApplicationProfile, load_profile, save_profile
from applicationbot.models import Contact, Resume

RESUME = Resume(contact=Contact(name="Jane Doe", email="jane@example.com"))


@pytest.fixture
def profile_path(tmp_path, monkeypatch):
    """A temp apply-profile that the module default (and so web) writes to."""
    p = tmp_path / "application_profile.yaml"
    monkeypatch.setattr(apply_profile, "DEFAULT_PATH", str(p))
    monkeypatch.setattr(web.apply_profile, "DEFAULT_PATH", str(p))
    save_profile(ApplicationProfile(first_name="Jane", email="jane@example.com",
                                    open_to_remote=True), p)
    return p


# ------------------------------------------------------------------ the bank write

def test_upsert_fills_the_blank_entry_autofill_captured(profile_path):
    """The common case: autofill got stuck, banked the question blank, the user answers it in
    Review — the SAME entry is filled in, not duplicated."""
    profile = load_profile(profile_path)
    profile.custom_answers.append(QA(question="How many years of Python do you have?",
                                     answer="", seen_count=3, input_kind="text"))
    save_profile(profile, profile_path)

    assert apply_profile.upsert_answers(
        {"How many years of Python do you have?": "Four years."}, profile_path) == 1
    qa = load_profile(profile_path).custom_answers
    assert len(qa) == 1
    assert qa[0].answer == "Four years."
    assert qa[0].seen_count == 3        # the capture history is kept
    assert qa[0].generated is False     # user-authored, not a Claude draft


def test_upsert_overwrites_a_rejected_answer_and_its_mapping(profile_path):
    """A user edit is ground truth: it replaces a Claude draft and clears a `maps_to` mapping —
    re-deriving the mapped answer would reproduce exactly the answer they just rejected."""
    profile = load_profile(profile_path)
    profile.custom_answers.append(QA(question="Are you able to work from our NYC office?",
                                     answer="", maps_to="open_to_remote", generated=True))
    save_profile(profile, profile_path)

    apply_profile.upsert_answers({"are you able to work from our NYC OFFICE?": "Two days a week."},
                                 profile_path)
    qa = load_profile(profile_path).custom_answers
    assert len(qa) == 1, "matched case-insensitively — no duplicate entry"
    assert (qa[0].answer, qa[0].maps_to, qa[0].generated) == ("Two days a week.", "", False)


def test_upsert_ignores_blanks_and_no_ops(profile_path):
    """Clearing a review edit drops that posting's override only — it never erases the bank.
    Re-saving an unchanged answer writes nothing."""
    apply_profile.upsert_answers({"Do you have a driver's license?": "Yes"}, profile_path)
    assert apply_profile.upsert_answers({"Do you have a driver's license?": ""}, profile_path) == 0
    assert apply_profile.upsert_answers({"Do you have a driver's license?": "Yes"}, profile_path) == 0
    assert load_profile(profile_path).custom_answers[0].answer == "Yes"


# ------------------------------------------------------------------ the policy gate

@pytest.mark.parametrize("question", [
    "Why do you want to work at Acme?",   # company-specific — wrong at the next employer
    "Why Acme?",
    "What is your gender?",               # EEO — owned by the structured profile fields
    "Are you Hispanic or Latino?",
    "Veteran status",
    "yes",                                # garbage capture
])
def test_not_reusable(question):
    assert answer_bank.is_reusable_answer(question) is False


@pytest.mark.parametrize("question", [
    "How many years of Python do you have?",
    "Are you willing to travel up to 25% of the time?",
    "Describe your experience with distributed systems.",
])
def test_reusable(question):
    assert answer_bank.is_reusable_answer(question) is True


# ------------------------------------------------------------------ what a save learns

def _learn(edits, profile_path, monkeypatch):
    monkeypatch.setattr(web, "load_resume", lambda _p: RESUME)
    return web._learn_reviewed_answers(edits)


def test_review_save_buckets_each_edit(profile_path, monkeypatch):
    out = _learn({
        "How many years of Python do you have?": "Four years.",  # learnable
        "Why Acme?": "Your latency work.",                       # company-specific
        "Gender": "Woman",                                       # EEO
        "Email": "contact-me@example.com",                       # a profile rule answers this
    }, profile_path, monkeypatch)

    assert out == {"learned": 1, "profile_owned": ["Email"], "posting_only": 2}
    banked = {qa.question: qa.answer for qa in load_profile(profile_path).custom_answers}
    assert banked == {"How many years of Python do you have?": "Four years."}


def test_review_save_corrects_a_wrong_banked_answer(profile_path, monkeypatch):
    """A banked answer the user overwrites IS relearned — that is the "wrong next time" case,
    and unlike a profile-owned label the bank is what answers it."""
    profile = load_profile(profile_path)
    profile.custom_answers.append(QA(question="Are you willing to travel?", answer="No"))
    save_profile(profile, profile_path)

    out = _learn({"Are you willing to travel?": "Yes, up to 25%."}, profile_path, monkeypatch)
    assert out["learned"] == 1 and out["profile_owned"] == []
    assert load_profile(profile_path).custom_answers[0].answer == "Yes, up to 25%."


def test_learned_answer_is_used_on_a_different_posting(profile_path, monkeypatch):
    """The point of all of it: after the edit, a DIFFERENT posting asking the same question is
    answered — with no per-posting override in play."""
    question = "How many years of Python do you have?"
    resolver = AnswerResolver(resume=RESUME, profile=load_profile(profile_path))
    assert resolver.resolve(question) is None  # today: unanswered, parked for the user

    _learn({question: "Four years."}, profile_path, monkeypatch)

    fresh = AnswerResolver(resume=RESUME, profile=load_profile(profile_path))
    assert fresh.resolve(question) == "Four years."
    assert fresh.resolve("Why Acme?") is None  # the company-specific edit stayed per-posting

"""A question is only 'unanswerable' after Claude has looked for its answer in the applicant's
own data (decision 187). No subprocess, no network: the Claude CLI is patched out.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

from applicationbot import answer_bank, backends
from applicationbot.apply import AnswerResolver, PendingDecisions, _norm, _resolve_pending
from applicationbot.apply_profile import QA, ApplicationProfile
from applicationbot.models import Contact, Education, Resume


@contextmanager
def _fake_claude(reply):
    """Patch the CLI to return `reply` (None ⇒ unavailable). Yields the prompts sent."""
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


def _answers(*vals):
    return json.dumps({"answers": list(vals)})


RESUME = Resume(
    contact=Contact(name="Ada Lovelace", email="ada@example.com"),
    summary="Backend engineer.",
    education=[Education(school="Penn State", degree="BS Computer Science", dates="2023 – 2027")],
)


def _profile(**kw):
    base = dict(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                phone="555-0100", street_address="12 Analytical Way", postal_code="08820",
                location="Edison, NJ", work_authorized=True, requires_sponsorship=False,
                earliest_start_date="June 2027", veteran_status="I am not a protected veteran",
                disability_status="No", race_ethnicity="Decline to self-identify")
    base.update(kw)
    return ApplicationProfile(**base)


def _resolver(**kw):
    return AnswerResolver(resume=RESUME, profile=_profile(**kw), enable_generation=True)


# ------------------------------------------------------------------ derive_answers

def test_an_answer_the_data_settles_is_returned():
    with _fake_claude(_answers("Yes")) as calls:
        out = answer_bank.derive_answers(
            ["Will this be your final internship before graduating?"], "RÉSUMÉ: graduates 2027")
    assert out == {"Will this be your final internship before graduating?": "Yes"}
    assert "graduates 2027" in calls[0]


def test_a_question_the_data_does_not_settle_comes_back_unanswered():
    """An empty answer must stay empty — the field goes to the user, it is not guessed."""
    with _fake_claude(_answers("", "No")):
        out = answer_bank.derive_answers(["Do you have upcoming offer deadlines?",
                                          "Are you authorized to work in the US?"], "FACTS")
    assert out == {"Are you authorized to work in the US?": "No"}


def test_placeholder_non_answers_are_not_typed_into_the_form():
    for junk in ("N/A", "unknown", "Not specified.", "none", "TBD", "  "):
        with _fake_claude(_answers(junk)):
            assert answer_bank.derive_answers(["What is your notice period?"], "FACTS") == {}


def test_an_essay_length_reply_is_refused_because_that_is_not_extraction():
    with _fake_claude(_answers("x" * 401)):
        assert answer_bank.derive_answers(["Why are you a fit?"], "FACTS") == {}


def test_demographic_questions_are_never_derived():
    """Only the applicant may declare these — inferring one would put words in their mouth."""
    with _fake_claude(_answers("Yes")) as calls:
        out = answer_bank.derive_answers(["Are you a protected veteran?",
                                          "Do you have a disability?"], "FACTS")
    assert out == {} and calls == []          # not even asked


def test_company_specific_questions_are_left_to_drafting():
    with _fake_claude(_answers("...")) as calls:
        assert answer_bank.derive_answers(["Why do you want to work here?"], "FACTS") == {}
        assert calls == []


def test_all_questions_go_in_one_batched_call():
    qs = ["Q one?", "Q two?", "Q three?"]
    with _fake_claude(_answers("a", "", "c")) as calls:
        out = answer_bank.derive_answers(qs, "FACTS")
    assert len(calls) == 1 and out == {"Q one?": "a", "Q three?": "c"}


def test_surrounding_form_text_is_sent_so_a_generic_label_can_be_placed():
    with _fake_claude(_answers("June 2027")) as calls:
        answer_bank.derive_answers(["Date"], "FACTS",
                                   contexts={"Date": "Anticipated graduation"})
    assert "Anticipated graduation" in calls[0]


def test_a_malformed_or_short_reply_answers_nothing():
    for bad in ('{"answers": ["a", "b"]}', "not json", '{"other": []}'):
        with _fake_claude(bad):
            assert answer_bank.derive_answers(["Only one?"], "FACTS") == {}


def test_no_claude_means_no_answers_not_an_error():
    with _fake_claude(None):
        assert answer_bank.derive_answers(["Anything?"], "FACTS") == {}


def test_nothing_is_asked_when_there_is_no_data_to_read():
    with _fake_claude(_answers("Yes")) as calls:
        assert answer_bank.derive_answers(["Anything?"], "  ") == {}
        assert calls == []


# ------------------------------------------------------------------ what leaves the machine

def test_the_facts_block_carries_the_resume_and_the_stated_profile_facts():
    facts = _resolver().data_facts()
    assert "Ada Lovelace" in facts and "Penn State" in facts
    assert "Earliest start date: June 2027" in facts
    assert "Authorized to work: Yes" in facts and "Requires visa sponsorship: No" in facts


def test_contact_details_and_eeo_answers_are_not_sent(monkeypatch):
    """Guideline #5: the derive stage needs facts to reason over, not the applicant's address —
    and EEO self-identification is theirs to declare, never ours to infer."""
    facts = _resolver().data_facts()
    for secret in ("555-0100", "12 Analytical Way", "08820",
                   "protected veteran", "Decline to self-identify"):
        assert secret not in facts


def test_an_unset_tri_state_field_reads_as_absent_not_as_no():
    facts = _resolver(work_authorized=None, requires_sponsorship=None).data_facts()
    assert "Authorized to work" not in facts and "Requires visa sponsorship" not in facts


def test_already_given_answers_are_part_of_the_data():
    r = _resolver()
    r.profile.custom_answers.append(QA(question="Do you hold a security clearance?", answer="No"))
    assert "security clearance" in r.data_facts()


def test_the_facts_block_is_built_once_per_run():
    r = _resolver()
    first = r.data_facts()
    r.profile.first_name = "CHANGED"
    assert r.data_facts() is first


# ------------------------------------------------------------------ the batched fill stage

def _pending(*labels):
    p = PendingDecisions()
    for label in labels:
        p.defer_question(label)
    return p


def test_a_derived_answer_resolves_the_field_in_round_two():
    """The whole point: the question is answered instead of reaching the user as unanswerable."""
    r = _resolver()
    q = "Will this be your final internship before graduating?"
    # classify → no structured type; bank-match → nothing banked; derive → "Yes"
    with _fake_claude(json.dumps({"types": [None], "matches": [-1], "answers": ["Yes"]})):
        _resolve_pending(r, _pending(q))
    assert r.resolve(q) == "Yes"
    assert r.derived == {_norm(q): "Yes"}


def test_a_question_the_data_cannot_answer_still_reaches_the_user():
    r = _resolver()
    q = "Do you have any upcoming offer deadlines?"
    with _fake_claude(json.dumps({"types": [None], "matches": [-1], "answers": [""]})):
        _resolve_pending(r, _pending(q))
    assert r.resolve(q) is None and r.derived == {}


def test_the_derive_call_only_sees_what_the_earlier_stages_could_not_answer():
    r = _resolver()
    r.profile.custom_answers.append(QA(question="Are you authorized to work in the US?",
                                       answer="Yes"))
    banked, novel = "Are you authorized to work in the US?", "Which campus do you attend?"
    with _fake_claude(json.dumps({"types": [None, None], "matches": [-1, -1],
                                  "answers": ["Penn State"]})) as calls:
        _resolve_pending(r, _pending(banked, novel))
    derive_prompt = [c for c in calls if "THE APPLICANT'S DATA" in c]
    assert len(derive_prompt) == 1
    asked = derive_prompt[0].split("FORM QUESTIONS:", 1)[1]
    assert novel in asked and banked not in asked   # the banked one was already answered
    assert banked in derive_prompt[0]               # it IS in the data block, as known context


def test_no_derive_call_is_made_when_every_question_is_already_answered():
    r = _resolver()
    q = "Are you authorized to work in the US?"
    r.profile.custom_answers.append(QA(question=q, answer="Yes"))
    with _fake_claude(json.dumps({"types": [None], "matches": [-1], "answers": []})) as calls:
        _resolve_pending(r, _pending(q))
    assert not [c for c in calls if "THE APPLICANT'S DATA" in c]


def test_a_derived_answer_is_reported_as_derived_not_as_the_users_own():
    from applicationbot.apply import ApplyReport, FilledField, _mark_derived
    r = _resolver()
    r.derived[_norm("Final internship?")] = "Yes"
    report = ApplyReport(url="u")
    report.filled = [FilledField("Final internship?", "Yes"),
                     FilledField("Email", "ada@example.com"),
                     FilledField("Why us?", "Because…", source="generated")]
    _mark_derived(r, report)
    assert [f.source for f in report.filled] == ["derived", "resolver", "generated"]
    assert "AI-derived from your data" in report.summary()


def test_the_report_says_the_last_chance_look_happened_even_when_it_found_nothing():
    """Otherwise "no saved answer" is ambiguous: did the data lack it, or did nobody look?"""
    from applicationbot.apply import ApplyReport, _mark_derived
    r = _resolver()
    r.derive_asked = 3
    r.derived[_norm("Final internship?")] = "Yes"
    report = ApplyReport(url="u")
    _mark_derived(r, report)
    assert report.notes == ["Re-read your résumé and profile for 3 question(s) no rule or saved "
                            "answer covered — answered 1, left 2 for you."]


def test_no_note_when_every_question_was_answered_before_the_derive_stage():
    from applicationbot.apply import ApplyReport, _mark_derived
    report = ApplyReport(url="u")
    _mark_derived(_resolver(), report)
    assert report.notes == []


def test_the_derive_stage_counts_what_it_was_asked(monkeypatch):
    r = _resolver()
    with _fake_claude(json.dumps({"types": [None, None], "matches": [-1, -1], "answers": ["", ""]})):
        _resolve_pending(r, _pending("Question one?", "Question two?"))
    assert r.derive_asked == 2 and r.derived == {}


# ------------------------------------------------------------------ the blank-placeholder trap

def test_a_derived_answer_beats_the_blank_placeholder_captured_for_the_user():
    """A question the bot could not answer before is already banked BLANK (captured so the user
    fills it once). resolve() reads the first match, so without dropping that placeholder the
    fresh answer is ignored and the field reports "no saved answer" it had just answered."""
    r = _resolver()
    q = "Portfolio URL"
    r.profile.custom_answers.append(QA(question=q, answer=""))      # captured on an earlier run
    with _fake_claude(json.dumps({"types": [None], "matches": [-1],
                                  "answers": ["https://gabriel.dev"]})):
        _resolve_pending(r, _pending(q))
    assert r.resolve(q) == "https://gabriel.dev"


def test_a_banked_match_also_beats_a_blank_placeholder():
    """Same trap on the bank-match stage — a reworded saved answer was being shadowed too."""
    r = _resolver()
    asked, saved = "Do you need a visa sponsored?", "Will you require visa sponsorship?"
    r.profile.custom_answers.append(QA(question=saved, answer="No"))
    r.profile.custom_answers.append(QA(question=asked, answer=""))   # blank placeholder
    with _fake_claude(json.dumps({"types": [None], "matches": [0], "answers": []})):
        _resolve_pending(r, _pending(asked))
    assert r.resolve(asked) == "No"


def test_injecting_leaves_a_real_saved_answer_alone():
    """Only BLANK placeholders are dropped — a real answer the user wrote is never removed."""
    from applicationbot.apply import _inject_answer
    r = _resolver()
    r.profile.custom_answers.append(QA(question="Tell us about a project", answer="Mine, in full"))
    _inject_answer(r, "Tell us about a project", answer="derived version")
    kept = [qa.answer for qa in r.profile.custom_answers
            if qa.question == "Tell us about a project"]
    assert "Mine, in full" in kept
    assert r.resolve("Tell us about a project") == "Mine, in full"   # the user's answer wins


def test_a_derived_answer_never_outranks_a_profile_field_or_a_saved_answer():
    r = _resolver()
    r.derived[_norm("Are you authorized to work in the US?")] = "No"
    r.derived[_norm("Tell us about a project")] = "derived version"
    r.profile.custom_answers.append(QA(question="Tell us about a project", answer="Mine, in full"))
    assert r.resolve("Are you authorized to work in the US?") == "Yes"   # the profile field
    assert r.resolve("Tell us about a project") == "Mine, in full"       # the saved answer


def test_a_derived_answer_fills_a_question_whose_profile_field_is_empty():
    """The live gap: "Portfolio URL" maps to `portfolio_url`, and an empty one makes the rule give
    up before the bank is ever consulted — so the fallback sits outside the rule chain."""
    r = _resolver(portfolio_url="")
    assert r.resolve("Portfolio URL") is None
    r.derived[_norm("Portfolio URL")] = "https://gabriel.dev"
    assert r.resolve("Portfolio URL") == "https://gabriel.dev"


def test_a_set_profile_field_still_wins_over_the_derived_map():
    r = _resolver(portfolio_url="https://real.example")
    r.derived[_norm("Portfolio URL")] = "https://guessed.example"
    assert r.resolve("Portfolio URL") == "https://real.example"


def test_a_saved_answer_is_used_when_its_profile_field_is_empty():
    """Found live: "What is your anticipated start date?" was banked with a real answer, but the
    start-date rule returns None when `earliest_start_date` is unset and never reached the bank,
    so the user's own answer was reported as "no saved answer"."""
    r = _resolver(earliest_start_date="")
    q = "What is your anticipated start date? (Month/Year)"
    r.profile.custom_answers.append(QA(question=q, answer="September 2026"))
    assert r.resolve(q) == "September 2026"


def test_the_profile_field_still_wins_over_a_stale_saved_answer():
    r = _resolver(earliest_start_date="June 2027")
    q = "What is your anticipated start date? (Month/Year)"
    r.profile.custom_answers.append(QA(question=q, answer="September 2026"))
    assert r.resolve(q) == "June 2027"

"""Spoken-language profile field (decision 158) — resolver rules + a real checkbox-group fill.

"Language Skill(s) (Check all that apply)" was captured blank on every Palantir run: no résumé
field carries spoken languages, so the resolver had nothing to answer it with. These tests pin
that the profile field now answers it, that a per-language proficiency question resolves to the
stored level, and — the failure mode worth guarding — that a PROGRAMMING-language question is
never answered from it.

The fill test drives fixtures/apply_forms/multi_select.html in headless Chromium (zero tokens).

Run:  python -m tests.test_languages   (also pytest-compatible; the fill test needs chromium)
"""
from __future__ import annotations

from pathlib import Path

from applicationbot.answer_bank import valid_mapping
from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import ApplicationProfile, Language
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "multi_select.html").as_uri()

QUESTION = "Language Skill(s) (Check all that apply)"


def _resolver(*langs: Language) -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    profile = ApplicationProfile(first_name="Test", last_name="User", email="t@example.com",
                                 languages=list(langs))
    return AnswerResolver(resume=resume, profile=profile, enable_generation=False)


ENGLISH = Language(name="English", proficiency="Native")
SPANISH = Language(name="Spanish", proficiency="Conversational")


# ------------------------------------------------------------------ resolver rules

def test_language_question_answers_with_every_language():
    # "; "-joined: the multi-answer format a checkbox group splits on to tick each option.
    assert _resolver(ENGLISH, SPANISH).resolve(QUESTION) == "English; Spanish"


def test_language_question_phrasings_all_resolve():
    r = _resolver(ENGLISH, SPANISH)
    for label in ("What languages do you speak?", "Languages", "Language(s) spoken",
                  "Which languages are you comfortable working in?"):
        assert r.resolve(label) == "English; Spanish", label


def test_proficiency_question_answers_for_the_language_it_names():
    r = _resolver(ENGLISH, SPANISH)
    assert r.resolve("How proficient are you in Spanish?") == "Conversational"
    assert r.resolve("English proficiency") == "Native"


def test_generic_proficiency_question_uses_the_primary_language():
    # No language named — answered for the first-listed (most proficient) one.
    assert _resolver(ENGLISH, SPANISH).resolve("Language proficiency level") == "Native"


def test_proficiency_for_a_language_we_do_not_have_is_left_to_the_user():
    # Naming a language that isn't ours must never claim a level for it — and the generic
    # primary-language fallback is off, since the question names a specific language.
    assert _resolver(ENGLISH).resolve("How fluent are you in German?") is None


def test_no_languages_stored_resolves_to_nothing():
    assert _resolver().resolve(QUESTION) is None


def test_programming_language_questions_are_never_answered_from_spoken_languages():
    r = _resolver(ENGLISH, SPANISH)
    for label in ("Which programming languages are you proficient in?",
                  "What coding languages do you know?",
                  "Rate your proficiency with our tech stack",
                  "Which languages/frameworks have you used in production?"):
        assert r.resolve(label) is None, label


def test_valid_mapping_refuses_banking_a_code_question_as_languages():
    assert valid_mapping("What languages do you speak?", "languages")
    assert not valid_mapping("Which programming languages do you know?", "languages")


def test_maps_to_languages_answers_live_from_the_profile():
    assert _resolver(ENGLISH, SPANISH).answer_for_type("languages") == "English; Spanish"


# ------------------------------------------------------------------ real form fill

def _drive(fn):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(FIXTURE)
        try:
            return fn(page)
        finally:
            browser.close()


def test_profile_languages_check_every_matching_box():
    """The end-to-end point: with the profile field set and NO banked answer, the check-all-that-
    apply group is filled — the exact question that was skipped as "no saved answer" before."""
    def run(page):
        report = ApplyReport(url=FIXTURE, ats="fixture")
        _fill_page(page, _resolver(ENGLISH, SPANISH), report, done=set())
        assert page.locator('input[value="eng"]').is_checked()
        assert page.locator('input[value="spa"]').is_checked()
        assert not page.locator('input[value="fra"]').is_checked()
        assert not page.locator('input[value="deu"]').is_checked()
        assert {f.value for f in report.filled if f.label == QUESTION} == \
            {"English (ENG)", "Spanish (SPA)"}
        assert not [s for s in report.skipped if s.startswith(QUESTION)], report.skipped
    _drive(run)


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
                print(f"  FAIL {name}")
                traceback.print_exc()
    print("ok" if not fails else f"{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""Resolver regression corpus — the determinism lock on the deterministic answering layer.

Drives `AnswerResolver.resolve()` (and `option_hints()` where pinned) over every label in
fixtures/resolver_corpus.yaml — real question labels from live dry-run sweeps — and asserts
the exact answer. The keyword rules are order- and substring-sensitive; without this, a rule
edit can silently flip an answer on a form we already fill correctly. No tokens, no browser.

If a deliberate behavior change flips a case, update the corpus IN THE SAME commit and say why.

Run:  python -m tests.test_resolver_corpus   (also pytest-compatible)
"""
from __future__ import annotations

from pathlib import Path

import yaml

from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import QA, ApplicationProfile
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "fixtures" / "resolver_corpus.yaml"

# The synthetic applicant the corpus expectations are written against (pairs with
# examples/sample_resume.yaml — Jordan Avery, NOT a real person). Changing a value here
# invalidates the corpus; regenerate it deliberately if you must.
PROFILE = ApplicationProfile(
    first_name="Jordan", last_name="Avery", email="jordan.avery@example.com",
    phone="(555) 010-4477", location="Austin, TX", country="United States",
    linkedin_url="https://linkedin.com/in/jordanavery-example",
    github_url="https://github.com/jordanavery-example",
    portfolio_url="https://jordanavery.example.com",
    work_authorized=True, requires_sponsorship=False, us_citizen=True,
    willing_to_relocate=False, open_to_remote=True,
    preferred_locations=["New York, NY", "Remote"],
    desired_salary="120000", earliest_start_date="2026-08-01", years_experience="6",
    gender="Male", race_ethnicity="White (Not Hispanic or Latino)",
    veteran_status="I am not a protected veteran",
    disability_status="No, I don't have a disability",
    custom_answers=[QA(question="Are you willing to travel up to 25% of the time?", answer="Yes")],
)


def _cases() -> list[dict]:
    return yaml.safe_load(CORPUS.read_text(encoding="utf-8"))["cases"]


def _resolver(case: dict) -> AnswerResolver:
    return AnswerResolver(
        resume=load_resume(str(REPO / "examples" / "sample_resume.yaml")),
        profile=PROFILE, company=case.get("company"), pay=case.get("pay"))


def test_corpus_answers_are_pinned():
    failures = []
    for case in _cases():
        got = _resolver(case).resolve(case["label"])
        if got != case["expect"]:
            failures.append(f"  {case.get('id') or case['label']!r}: "
                            f"expected {case['expect']!r}, got {got!r}")
    assert not failures, "resolver answers changed:\n" + "\n".join(failures)


def test_corpus_option_hints_are_pinned():
    failures = []
    for case in _cases():
        if "hints" not in case:
            continue
        got = _resolver(case).option_hints(case["label"])
        if got != case["hints"]:
            failures.append(f"  {case['label']!r}: expected {case['hints']!r}, got {got!r}")
    assert not failures, "option hints changed:\n" + "\n".join(failures)


def test_corpus_is_meaningfully_large():
    # A gutted corpus can't guard anything — fail loudly if cases disappear.
    assert len(_cases()) >= 60


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

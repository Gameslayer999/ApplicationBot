"""Two-pass batched fill (decision 041) — local fixture, headless Chromium, zero tokens.

Round 1 fills the deterministic fields and DEFERS the four unresolvable ones; the batch step
must make EXACTLY 3 stubbed Claude calls (classify, bank-match, dropdown picks — never one per
field); round 2 fills everything from the injected results. Also verifies generation-off stays
a single pass with zero calls, and that a failed batch degrades to plain captures.

Run:  python -m tests.test_two_pass_fill   (also pytest-compatible; needs chromium installed)
"""
from __future__ import annotations

import json
from pathlib import Path

from applicationbot import backends
from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import QA, ApplicationProfile
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "two_pass.html").as_uri()

ONSITE = "Are you comfortable working onsite three days each week?"
TRAVEL = "How much travel can you commit to annually?"
WORKAUTH = "Work Authorization Status"
IMMIG = "Will your employment require us to file immigration paperwork on your behalf?"
WA_POSITIVE = "Permitted to accept employment without restriction"


def _resolver(generation: bool = True) -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    profile = ApplicationProfile(
        first_name="Test", last_name="User", email="t@example.com",
        work_authorized=True, requires_sponsorship=False, open_to_remote=True,
        custom_answers=[QA(question="Are you willing to travel up to 25% of the time?",
                           answer="Yes")])
    return AnswerResolver(resume=resume, profile=profile, enable_generation=generation)


class _BatchClaude:
    """Stub run_claude_cli that answers the three BATCH prompts by shape and records calls."""

    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, prompt, **kw):
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("claude CLI unavailable")
        if "Map EACH job-application question" in prompt:
            # Deferral order: onsite (text), travel (text), immig (radio group).
            return json.dumps({"types": ["open_to_remote", "none", "requires_sponsorship"]})
        if "SAVED PAIRS" in prompt:
            return json.dumps({"matches": [0]})  # travel → the banked 25%-travel answer
        if "DROPDOWN 0" in prompt:
            return json.dumps({"choices": [0]})  # work auth "Yes" → the positive option
        raise AssertionError(f"unexpected (non-batch?) prompt: {prompt[:120]}")


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


def _run_page(page, resolver):
    report = ApplyReport(url=FIXTURE, ats="fixture")
    _fill_page(page, resolver, report, done=set())
    return report


def test_two_pass_fills_all_deferred_fields_in_three_calls():
    def run(page):
        r = _resolver()
        stub = _BatchClaude()
        real = backends.run_claude_cli
        backends.run_claude_cli = stub
        try:
            report = _run_page(page, r)
        finally:
            backends.run_claude_cli = real

        filled = {f.label: f for f in report.filled}
        assert filled["First Name"].value == "Test"          # deterministic, round 1
        assert filled[ONSITE].value == "Yes"                 # classify → open_to_remote
        assert filled[TRAVEL].value == "Yes"                 # bank-match → banked travel answer
        assert filled[WORKAUTH].value == WA_POSITIVE         # batched dropdown pick
        assert filled[WORKAUTH].source == "option:claude"
        assert filled[IMMIG].value == "No"                   # classify → requires_sponsorship
        assert page.locator("#wa").evaluate("el => el.dataset.committed") == WA_POSITIVE
        assert page.locator('input[name="immig"][value="no"]').is_checked()
        # Exactly one batched call per decision kind — never one per field.
        assert len(stub.calls) == 3, [c[:60] for c in stub.calls]
        # Nothing we answered is also reported as needing attention.
        assert not any(lbl in s for s in report.skipped
                       for lbl in (ONSITE, TRAVEL, WORKAUTH, IMMIG)), report.skipped
        # The adjudications are learned for persistence (write-gated later).
        assert {qa.maps_to for qa in r.learned} == {"open_to_remote", "requires_sponsorship", ""}
    _drive(run)


def test_generation_off_stays_single_pass_with_zero_calls():
    def run(page):
        r = _resolver(generation=False)
        stub = _BatchClaude()
        real = backends.run_claude_cli
        backends.run_claude_cli = stub
        try:
            report = _run_page(page, r)
        finally:
            backends.run_claude_cli = real
        assert stub.calls == []
        filled = {f.label for f in report.filled}
        assert "First Name" in filled
        # The four undecidable fields are captured for the user, exactly as before.
        for lbl in (ONSITE, TRAVEL, WORKAUTH, IMMIG):
            assert lbl not in filled
        for lbl in (ONSITE, TRAVEL, IMMIG):
            assert lbl in report.captured, report.captured.keys()
    _drive(run)


def test_batch_failure_degrades_to_captures():
    def run(page):
        r = _resolver()
        stub = _BatchClaude(fail=True)
        real = backends.run_claude_cli
        backends.run_claude_cli = stub
        try:
            report = _run_page(page, r)
        finally:
            backends.run_claude_cli = real
        filled = {f.label for f in report.filled}
        assert "First Name" in filled
        # Batch died → round 2 captures the deferred fields; no per-field retry storm.
        for lbl in (ONSITE, TRAVEL, IMMIG):
            assert lbl in report.captured, report.captured.keys()
        assert len(stub.calls) <= 3
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
                print(f"FAIL  {name}")
                traceback.print_exc()
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())

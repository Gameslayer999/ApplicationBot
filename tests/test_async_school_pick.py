"""Async searchable school picker prefers the MAIN campus in round 2 (decision 080).

A searchable react-select's OPEN list (first schools alphabetically) never contains the applicant's
school, so the round-1 batch declines it and marks the label picks_done. Before the fix, picks_done
also suppressed Phase 2b — the article-stripped typeahead + Claude pick that prefers the primary
campus — so round 2 fell through to the substring fallback, which takes the first fuzzy match and
here lands on "…- Schuylkill Campus". Phase 2b is now exempt from picks_done, so the main campus
wins. Verifies the full two-pass `_fill_page`, not a helper in isolation.

Run:  python -m tests.test_async_school_pick   (also pytest-compatible; needs chromium)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from applicationbot import backends
from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Education, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "async_school_picker.html").as_uri()
MAIN = "Pennsylvania State University"
BRANCH = "Pennsylvania State University - Schuylkill Campus"


def _resolver() -> AnswerResolver:
    resume = Resume(
        contact=Contact(name="Test User", email="t@example.com"),
        education=[Education(school="The Pennsylvania State University",
                             degree="Bachelor of Science in Computer Science")])
    return AnswerResolver(resume=resume, profile=ApplicationProfile(), enable_generation=True)


class _Claude:
    """Batch declines from the open (alphabetical) list; the per-query Phase 2b pick chooses the
    MAIN campus by exact name, never the branch listed first."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, prompt, **kw):
        self.calls.append(prompt)
        if "DROPDOWN 0" in prompt:                       # round-1 batch over the open list
            return json.dumps({"choices": [-1]})          # no alphabetical default fits Penn State
        if prompt.startswith("A job-application dropdown"):  # Phase 2b per-query pick
            opts = re.findall(r"^\s*(\d+)\.\s*(.+)$", prompt, re.M)
            for num, text in opts:
                if text.strip() == MAIN:                  # prefer the primary campus, not the branch
                    return json.dumps({"choice": int(num)})
            return json.dumps({"choice": -1})
        raise AssertionError(f"unexpected prompt: {prompt[:100]}")


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


def test_round2_typeahead_picks_main_campus_after_batch_declines():
    def run(page):
        r = _resolver()
        stub = _Claude()
        real = backends.run_claude_cli
        backends.run_claude_cli = stub
        try:
            report = ApplyReport(url=FIXTURE, ats="fixture")
            _fill_page(page, r, report, done=set())
        finally:
            backends.run_claude_cli = real

        school = [f for f in report.filled if f.label == "School"]
        assert len(school) == 1, [(f.label, f.value, f.source) for f in report.filled]
        f = school[0]
        # The MAIN campus, chosen by Claude — not the branch a first-fuzzy-match substring grabs.
        assert f.value == MAIN, f
        assert f.value != BRANCH, f
        assert f.source == "option:claude", f
        assert page.locator("#school--0").evaluate("el => el.dataset.committed || ''") == MAIN
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

"""react-select aria-hidden requiredInput mirror must not hijack its dropdown (decision 079).

Greenhouse renders each react-select as two inputs sharing one label: the real combobox and an
aria-hidden `requiredInput` shadow whose empty `type` reads as free text. When the résumé value
doesn't literally match an option, the combobox DEFERS its pick to the batch and returns without
claiming the label; before the fix the loop then reached the mirror, .fill()'d it as plain text,
and marked the label "done" — so round 2 never recommitted the real selection and the field
submitted empty (a SpaceX 'School' dry run showed 'Select…'). The fix skips aria-hidden inputs.

Run:  python -m tests.test_required_input_mirror   (also pytest-compatible; needs chromium)
"""
from __future__ import annotations

import json
from pathlib import Path

from applicationbot import backends
from applicationbot.apply import AnswerResolver, ApplyReport, _fill_page
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Education, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "react_select_required_mirror.html").as_uri()
MAIN_CAMPUS = "Pennsylvania State University-Main Campus"


def _resolver(generation: bool = True) -> AnswerResolver:
    resume = Resume(
        contact=Contact(name="Test User", email="t@example.com"),
        education=[Education(school="The Pennsylvania State University",
                             degree="Bachelor of Science in Computer Science")])
    return AnswerResolver(resume=resume, profile=ApplicationProfile(),
                          enable_generation=generation)


class _PickMainCampus:
    """Stub run_claude_cli: the batched dropdown pick chooses the Main Campus option; records
    the calls so we can assert the pick happened via the combobox, not a text fabrication."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, prompt, **kw):
        self.calls.append(prompt)
        if "DROPDOWN 0" in prompt:
            # options are numbered 0-based in DOM order; Main Campus is index 1.
            return json.dumps({"choices": [1]})
        raise AssertionError(f"unexpected (non-dropdown) batch prompt: {prompt[:120]}")


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


def test_mirror_skipped_dropdown_commits_the_real_selection():
    def run(page):
        r = _resolver()
        stub = _PickMainCampus()
        real = backends.run_claude_cli
        backends.run_claude_cli = stub
        try:
            report = ApplyReport(url=FIXTURE, ats="fixture")
            _fill_page(page, r, report, done=set())
        finally:
            backends.run_claude_cli = real

        school = [f for f in report.filled if f.label == "School"]
        assert len(school) == 1, [(f.label, f.source) for f in report.filled]
        f = school[0]
        # Committed through the real dropdown — NOT the plain-text mirror .fill() (source
        # 'resolver', control 'text') that left the widget on 'Select…'.
        assert f.control == "combobox", f
        assert f.source == "option:claude", f
        assert f.value == MAIN_CAMPUS, f
        # The selection is actually committed on the visible combobox input.
        assert page.locator("#school--0").evaluate("el => el.dataset.committed || ''") == MAIN_CAMPUS
        # The aria-hidden mirror was never typed into.
        assert page.locator(".requiredInput").input_value() == ""
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

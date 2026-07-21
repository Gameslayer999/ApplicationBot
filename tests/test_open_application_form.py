"""Regression test for the SPA-timing bug in `_open_application_form`.

Ashby (and other SPA ATS) mount the "Apply for this Job" control *after* domcontentloaded,
and the real application form lives on a separate route (<posting>/application). The old code
tried to reveal the form with a single click BEFORE the poll loop — firing before the button
existed, so it never navigated and the loop watched an empty posting page until it timed out.

These fakes model that exactly: the Apply button appears only from the 2nd poll onward, and the
form fields appear only after the button is clicked. The fix (retry the reveal-click inside the
poll loop) must therefore load the form; a one-shot pre-loop click must not.
"""
import re

from applicationbot.apply import _open_application_form, ApplyReport


class _FakeLocator:
    def __init__(self, page, present):
        self._page = page
        self._present = present  # callable -> bool: is the Apply button on the page yet?

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._present() else 0

    def is_visible(self):
        return self._present()

    def click(self, timeout=0):
        self._page.clicked = True


class _FakeFrame:
    def __init__(self, page):
        self._page = page
        self.url = "https://jobs.ashbyhq.com/Ramp/abc"

    def evaluate(self, _js):
        return self._page.field_count()

    def wait_for_selector(self, *a, **k):
        return None


class _FakePage:
    """Poll count drives the timeline: button mounts on poll 2; fields mount once clicked."""
    def __init__(self):
        self.polls = 0
        self.clicked = False
        self.main_frame = _FakeFrame(self)
        self.frames = [self.main_frame]
        self.url = "https://jobs.ashbyhq.com/Ramp/abc"

    def _button_present(self):
        return self.polls >= 2  # SPA hasn't mounted the Apply control on the first pass

    def field_count(self):
        return 12 if self.clicked else 0  # form fields only exist after navigating via Apply

    def get_by_role(self, role, name=None):
        return _FakeLocator(self, self._button_present)

    def wait_for_timeout(self, _ms):
        self.polls += 1


def test_reveal_click_is_retried_until_button_mounts():
    page = _FakePage()
    report = ApplyReport(url=page.url, ats="ashby")
    loaded, frame, ats = _open_application_form(page, "ashby", report, timeout_ms=25000)
    assert loaded is True, f"form should load once the late Apply button is clicked; errors={report.errors}"
    assert page.clicked is True
    assert not report.errors


class _FakeRevealPage(_FakePage):
    """Models SmartRecruiters: the form is gated behind a control whose accessible name is
    `button_label` (e.g. "I'm interested", not "Apply"). `get_by_role` honors the name regex the
    caller passes, so a locator is "present" only when that regex matches the label — exactly how
    Playwright resolves it. Proves `_REVEAL_CONTROL` clicks the real reveal button."""
    def __init__(self, button_label):
        super().__init__()
        self.button_label = button_label

    def _label_present(self, name):
        return name is not None and bool(name.search(self.button_label))

    def get_by_role(self, role, name=None):
        return _FakeLocator(self, lambda: self._label_present(name))


def test_smartrecruiters_interested_button_reveals_form():
    """The reveal control says "I'm interested", not "Apply" — the old `\\bapply\\b`-only match
    never clicked it and the form timed out. `_REVEAL_CONTROL` must click it and load the form."""
    page = _FakeRevealPage("I'm interested")
    report = ApplyReport(url="https://jobs.smartrecruiters.com/Consultadd4/87644936", ats="generic")
    loaded, _, _ = _open_application_form(page, "generic", report, timeout_ms=25000)
    assert loaded is True, f"'I'm interested' should reveal the form; errors={report.errors}"
    assert page.clicked is True
    assert not report.errors


def test_not_interested_button_is_never_clicked():
    """A "Not interested" dismiss control must never be treated as the reveal — the negative
    lookbehind excludes it, so the form never loads via that button and the run times out."""
    page = _FakeRevealPage("Not interested")
    report = ApplyReport(url="https://jobs.smartrecruiters.com/X/1", ats="generic")
    loaded, _, _ = _open_application_form(page, "generic", report, timeout_ms=200)
    assert loaded is False
    assert page.clicked is False, "must not click a 'Not interested' button"


def test_form_already_present_needs_no_click():
    """When fields exist immediately, the reveal-click must not fire."""
    page = _FakePage()
    page.clicked = True  # fields already present (non-SPA / embedded form) — but we didn't click
    page.clicked = False
    # Simulate: fields present without a click by overriding field_count.
    page.field_count = lambda: 12
    report = ApplyReport(url=page.url, ats="generic")
    loaded, _, _ = _open_application_form(page, "generic", report, timeout_ms=25000)
    assert loaded is True
    assert page.clicked is False, "should not click Apply when a form is already rendered"

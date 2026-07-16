"""Combobox fill flow — local react-select-shaped fixture, headless Chromium, zero tokens.

Verifies the decide-with-menu-CLOSED restructure: a Claude option pick (stubbed) happens only
while the menu is shut, the chosen option is recommitted by exact text, the mapping is learned
(except generic booleans), and every fill reports its matched tier (literal/hint/claude).

Run:  python -m tests.test_combobox_fill   (also pytest-compatible; needs chromium installed)
"""
from __future__ import annotations

from pathlib import Path

from applicationbot import answer_bank
from applicationbot.apply import AnswerResolver, _fill_combobox
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Resume

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (REPO / "fixtures" / "apply_forms" / "combobox.html").as_uri()


def _resolver(generation: bool = True) -> AnswerResolver:
    resume = Resume(contact=Contact(name="Test User", email="t@example.com"))
    return AnswerResolver(resume=resume, profile=ApplicationProfile(),
                          enable_generation=generation)


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


def _committed(page, input_id: str) -> str:
    return page.locator(f"#{input_id}").evaluate("el => el.dataset.committed || ''")


def _stub_pick(reply, menu_state: list | None = None, page=None, menu_id: str = ""):
    """Replace answer_bank.pick_dropdown_option with a canned reply; optionally record whether
    the fixture's menu was open at decide time (it must NOT be)."""
    def fake(label, value, options, **kw):
        if menu_state is not None:
            menu_state.append(page.locator(f"#{menu_id}").evaluate("el => el.hidden"))
        return reply(options) if callable(reply) else reply
    real = answer_bank.pick_dropdown_option
    answer_bank.pick_dropdown_option = fake
    return lambda: setattr(answer_bank, "pick_dropdown_option", real)


def test_literal_and_hint_tiers_no_claude():
    def run(page):
        r = _resolver(generation=False)
        got = _fill_combobox(page, page.locator("#cb1"), "United States", resolver=r, label="Country")
        assert got == ("United States", "literal")
        assert _committed(page, "cb1") == "United States"
        # Hints tier: the value itself matches nothing, a ranked hint does.
        got2 = _fill_combobox(page, page.locator("#cb2"), "Definitely",
                              hints=["for any employer"], resolver=r, label="Work auth")
        assert got2 == ("I am authorized to work in the United States for any employer", "hint")
    _drive(run)


def test_claude_pick_decides_with_menu_closed_and_recommits():
    def run(page):
        r = _resolver()
        menu_open_at_decide: list = []
        restore = _stub_pick(lambda opts: "Pennsylvania State University-Main Campus",
                             menu_state=menu_open_at_decide, page=page, menu_id="menu-cb3")
        try:
            got = _fill_combobox(page, page.locator("#cb3"), "The Pennsylvania State University",
                                 resolver=r, label="School")
        finally:
            restore()
        assert got == ("Pennsylvania State University-Main Campus", "claude")
        assert _committed(page, "cb3") == "Pennsylvania State University-Main Campus"
        assert menu_open_at_decide == [True]  # hidden=True — the menu was CLOSED while deciding
        # The vetted pick is learned for instant matching next time.
        assert r.learned_option_hints("the pennsylvania state university") \
            == ["Pennsylvania State University-Main Campus"]
    _drive(run)


def test_claude_boolean_pick_fills_but_is_never_learned():
    def run(page):
        r = _resolver()
        restore = _stub_pick("I am authorized to work in the United States for any employer")
        try:
            got = _fill_combobox(page, page.locator("#cb2"), "Yes", resolver=r, label="Work auth")
        finally:
            restore()
        assert got == ("I am authorized to work in the United States for any employer", "claude")
        assert r.learned_options == {}  # value-keyed "yes" alias would leak into every Yes/No dropdown
    _drive(run)


def test_claude_declines_leaves_field_unfilled_and_clean():
    def run(page):
        r = _resolver()
        restore = _stub_pick(None)
        try:
            got = _fill_combobox(page, page.locator("#cb1"), "Fantasyland", resolver=r, label="Country")
        finally:
            restore()
        assert got is None
        assert _committed(page, "cb1") == ""  # nothing committed…
        assert page.locator("#cb1").input_value() == ""  # …and no typed text left behind
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

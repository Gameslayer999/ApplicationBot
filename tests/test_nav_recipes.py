"""Agentic nav fallback + nav-recipe tests (decision 076).

Root cause under test: a real dry-run against jobs.smartrecruiters.com filled 0 fields and reported
"Application form did not load" — the reveal control says "I'm interested", not "Apply", so the form
was never opened. Two layers fix it and both are covered here:

  1. deterministic — "I'm interested" is now a known reveal control (and "Not interested" is not),
     driven headless against a committed PII-free fixture of the real page.
  2. agentic + learned — for wording we can't know in advance ("Join our team"), an armed Claude
     worker opens the form ONCE; we distil a host-keyed recipe by DIFFING the DOM (what navigated,
     what vanished) and replay it forever after with no agent. The full learn-once → replay loop is
     asserted with a fake agent, so no Claude/CDP is needed.

Run:  python -m pytest tests/test_nav_recipes.py   (browser tests need chromium)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from applicationbot import nav_recipes
from applicationbot.apply import (ApplyReport, _bot_wall_evidence, _distil_nav,
                                  _open_application_form, detect_ats, nav_agentic_enabled,
                                  run_agent_nav)
from applicationbot.nav_recipes import NavRecipe

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "apply_forms"


def _store() -> str:
    return str(Path(tempfile.mkdtemp()) / "nav_recipes.json")


@pytest.fixture(scope="module")
def site():
    """Serve the fixtures over loopback HTTP, not file://. Recipes are keyed by HOST, and a
    file:// URI has none — so only a real http origin exercises the store the way a posting does."""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def posting(site):
    return f"{site}/smartrecruiters_posting.html"


@pytest.fixture
def unknown(site):
    return f"{site}/unknown_reveal.html"


@pytest.fixture
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


# --- store ---------------------------------------------------------------

def test_host_key_collapses_postings_to_one_site():
    """The recipe key is the host, so learning ONE posting unblocks every posting on that site —
    the whole point of "future applications through a similar site are not blocked"."""
    a = nav_recipes.host_of("https://jobs.smartrecruiters.com/Consultadd4/87644936")
    b = nav_recipes.host_of("https://jobs.smartrecruiters.com/OtherCo/112233?utm=x")
    assert a == b == "jobs.smartrecruiters.com"
    assert nav_recipes.host_of("https://WWW.Example.com:443/x") == "example.com"


def test_store_roundtrip_and_merge_does_not_clobber():
    p = _store()
    nav_recipes.save_recipe(NavRecipe("jobs.example.com", url_suffix="/apply"), path=p)
    nav_recipes.save_recipe(NavRecipe("jobs.example.com", url_suffix="/WRONG",
                                      reveal_labels=["Join our team"]), path=p)
    r = nav_recipes.get_recipe("https://jobs.example.com/co/1", path=p)
    assert r.url_suffix == "/apply", "a known-good suffix must not be clobbered by re-learning"
    assert r.reveal_labels == ["Join our team"]


def test_store_is_pii_free_and_bounded():
    """A committed, shared library must never absorb applicant data or page chrome."""
    p = _store()
    nav_recipes.save_recipe(NavRecipe("h.com", reveal_labels=["Apply", "Apply", "x" * 99] +
                                      [f"L{i}" for i in range(9)]), path=p)
    raw = Path(p).read_text()
    assert "x" * 99 not in raw, "over-long labels must be dropped"
    labels = nav_recipes.get_recipe("https://h.com/j", path=p).reveal_labels
    assert labels.count("Apply") == 1 and len(labels) <= 4
    assert "@" not in raw and "resume" not in raw.lower()


def test_empty_recipe_is_never_saved():
    p = _store()
    nav_recipes.save_recipe(NavRecipe("h.com"), path=p)
    assert nav_recipes.load_recipes(p) == {}


def test_malformed_store_degrades_to_no_recipe():
    p = _store()
    Path(p).write_text("{ not json")
    assert nav_recipes.load_recipes(p) == {}


# --- gating --------------------------------------------------------------

def test_nav_agentic_is_off_by_default_and_opt_in(tmp_path):
    """Learning spends Claude tokens, so it is opt-in — mirroring workday_agentic (dec. 061/063)."""
    assert nav_agentic_enabled(tmp_path / "missing.yaml") is False
    off = tmp_path / "off.yaml"
    off.write_text("dry_run: true\n")
    assert nav_agentic_enabled(off) is False
    on = tmp_path / "on.yaml"
    on.write_text("nav_agentic: true\n")
    assert nav_agentic_enabled(on) is True


# --- layer 1: the deterministic fix -------------------------------------

def test_smartrecruiters_is_detected():
    assert detect_ats("https://jobs.smartrecruiters.com/Consultadd4/87644936") == "smartrecruiters"


def test_im_interested_opens_the_form(browser, posting):
    """The exact regression: the real posting's reveal says "I'm interested"."""
    page = browser.new_page()
    page.goto(posting)
    report = ApplyReport(url=posting, ats="smartrecruiters")
    loaded, frame, _ = _open_application_form(page, "smartrecruiters", report, timeout_ms=8000,
                                              replay=False)
    assert loaded is True, f"errors={report.errors}"
    assert "smartrecruiters_apply" in page.url, "must have navigated to the form, not the decoy"
    assert not report.errors


def test_unknown_wording_still_fails_deterministically(browser, unknown):
    """Bounds the cheap fix honestly: "Join our team" is NOT guessable, which is why the agentic
    fallback exists. If this ever passes, the fallback test below is no longer proving anything."""
    page = browser.new_page()
    page.goto(unknown)
    report = ApplyReport(url=unknown, ats="generic")
    loaded, _, _ = _open_application_form(page, "generic", report, timeout_ms=3000, replay=False)
    assert loaded is False
    assert any("did not load" in e for e in report.errors)


# --- layer 2: distillation ----------------------------------------------

def test_distil_prefers_url_suffix_when_the_agent_navigated():
    class _P:
        url = "https://jobs.example.com/co/123/apply"
        def evaluate(self, _js): return []
    r = _distil_nav("https://jobs.example.com/co/123", ["Apply now"], _P())
    assert r.host == "jobs.example.com"
    assert r.url_suffix == "/apply"


def test_distil_ignores_unrelated_controls_that_vanished():
    """A cookie banner disappearing must never become a recipe we replay on every posting."""
    class _P:
        url = "https://h.com/j/1"
        def evaluate(self, _js): return []
    r = _distil_nav("https://h.com/j/1", ["Accept cookies", "Close", "Join our team"], _P())
    assert r.reveal_labels == ["Join our team"]
    assert r.url_suffix == ""


def test_distil_yields_nothing_when_the_route_is_opaque():
    """An agent that reached the form via an unreducible route (cross-domain redirect, modal with
    no vanishing control) must produce NO recipe rather than a wrong one."""
    class _P:
        url = "https://someportal.com/totally/different"
        def evaluate(self, _js): return ["Join our team"]
    assert _distil_nav("https://h.com/j/1", ["Join our team"], _P()).is_empty()


# --- layer 2: the learn-once → replay-forever loop -----------------------

def test_agent_learns_unknown_site_once_then_replay_needs_no_agent(browser, unknown):
    """The headline property, end to end on a real browser with a FAKE agent (no Claude, no CDP):
    the deterministic path fails on "Join our team"; the armed agent opens the form once; the route
    is distilled into a host-keyed recipe; and a SECOND posting on that host then opens purely by
    replay — with the agent asserted to have run exactly ONCE."""
    store = _store()
    runs = []

    def fake_agent(page, fields, prompt, *, cdp_port, model, report):
        """Stands in for Claude+Playwright-MCP: does what a nav worker does — clicks until the
        form is up. Asserts the worker is told navigation-only, never to fill."""
        runs.append(prompt)
        assert "Do NOT fill in ANY field" in prompt
        page.click("#dismiss")   # incidental chrome change — must not be learned
        page.click("#reveal")
        page.wait_for_load_state("domcontentloaded")

    page = browser.new_page()
    page.goto(unknown)
    report = ApplyReport(url=unknown, ats="generic")
    ok = run_agent_nav(page, unknown, report, cdp_port=1234, recipe_path=store, _spawn=fake_agent)

    assert ok is True, f"agent should have opened the form; errors={report.errors}"
    assert len(runs) == 1
    learned = nav_recipes.get_recipe(unknown, path=store)
    assert learned is not None and "Join our team" in learned.reveal_labels
    assert "Accept cookies" not in learned.reveal_labels
    assert any("Learned nav recipe" in n for n in report.notes)

    # --- a later posting on the same host: replay only, agent must NOT run again ---
    page2 = browser.new_page()
    page2.goto(unknown)
    report2 = ApplyReport(url=unknown, ats="generic")
    loaded, _, _ = _open_application_form(page2, "generic", report2, timeout_ms=8000,
                                          url_hint=unknown, recipe_path=store)
    assert loaded is True, f"the learned recipe must open the form with no agent; errors={report2.errors}"
    assert len(runs) == 1, "replay must not spawn the agent again — agentic use trends to 0"
    assert "unknown_reveal_form" in page2.url


def test_failed_agent_records_actionable_error_and_no_recipe(browser, unknown):
    """A worker that can't reach the form must leave no recipe and say what to do (UI principle 3)."""
    store = _store()

    def dead_agent(page, fields, prompt, *, cdp_port, model, report):
        raise RuntimeError("claude agent exited 1")

    page = browser.new_page()
    page.goto(unknown)
    report = ApplyReport(url=unknown, ats="generic")
    ok = run_agent_nav(page, unknown, report, cdp_port=1, recipe_path=store, _spawn=dead_agent)
    assert ok is False
    assert nav_recipes.load_recipes(store) == {}
    assert any("claude agent exited 1" in e for e in report.errors)


# --- bot walls are not missing forms ------------------------------------

def test_bot_wall_is_detected_through_the_iframe(browser, site):
    """Live-drive finding, reproduced: the real SmartRecruiters posting answered our reveal with a
    403 whose "Access is temporarily restricted / Automated (bot) activity" wall is rendered by a
    vendor INSIDE AN IFRAME while the host page's body is empty. A main-frame-only scan missed it
    and the run misreported a REFUSAL as "form did not load"."""
    page = browser.new_page()
    page.goto(f"{site}/bot_wall.html")
    assert (page.inner_text("body") or "").strip() == "", "fixture must model the empty host page"
    assert _bot_wall_evidence(page) == "access is temporarily restricted"


def test_bot_wall_vendor_frame_alone_is_evidence():
    """DataDome/PerimeterX/Cloudflare serve the wall from their own host; the frame's presence is
    the block, even before any text renders."""
    class _F:
        url = "https://geo.captcha-delivery.com/captcha/?initialCid=abc"
        def inner_text(self, _s): return ""
    class _P:
        frames = [_F()]
    assert _bot_wall_evidence(_P()) == "captcha-delivery.com"


def test_bot_wall_never_spends_a_claude_call(site, tmp_path, monkeypatch):  # run_apply owns its browser
    """Guideline #4, driven through the REAL run_apply branch (not a stand-in): a site that has
    refused us must NOT be handed to the agentic nav fallback — the agent drives the same browser
    from the same IP and hits the identical wall, and aiming an agent at a bot wall is evasion.
    The run must report the refusal, spend no Claude call, and submit nothing."""
    import tempfile as _tf

    from applicationbot import apply as apply_mod
    from applicationbot.apply import AnswerResolver, run_apply
    from applicationbot.apply_profile import ApplicationProfile
    from applicationbot.resume import load_resume

    monkeypatch.setattr(apply_mod, "nav_agentic_enabled", lambda *a, **k: True)  # armed
    spawned = []
    monkeypatch.setattr(apply_mod, "run_agent_nav", lambda *a, **k: spawned.append(1) or True)

    resolver = AnswerResolver(resume=load_resume(str(REPO / "examples" / "sample_resume.yaml")),
                              profile=ApplicationProfile(), enable_generation=False)
    pdf = Path(_tf.mkdtemp()) / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    report = run_apply(f"{site}/bot_wall.html", str(pdf), resolver, headed=False, pause=False,
                       slow_mo=0, learn=False, record=False, timeout_ms=3000,
                       screenshot=str(Path(_tf.mkdtemp()) / "shot.png"), gate=None)

    assert spawned == [], "the agentic fallback must never fire at a bot wall"
    assert report.submitted is False and not report.filled
    assert any("blocked automated access" in e for e in report.errors), report.errors
    assert not any("did not load within" in e for e in report.errors), \
        "a refusal must not also be misreported as a missing form"
    assert not any("nav_agentic: true" in e for e in report.errors), \
        "must not advise arming the agent against a site that refused us"


def test_committed_library_rejects_one_machine_hosts():
    """Surfaced by a live drive: the agent learned host "127.0.0.1" from a local fixture and would
    have written it to the SHARED, COMMITTED library, where it is meaningless to every clone. The
    default store filters non-public hosts; a custom store (tests/dev) is never filtered."""
    assert nav_recipes.is_shareable_host("jobs.smartrecruiters.com") is True
    for junk in ("127.0.0.1", "localhost", "192.168.1.9", "10.0.0.4", "dev.local", "intranet"):
        assert nav_recipes.is_shareable_host(junk) is False, junk

    nav_recipes.save_recipe(NavRecipe("127.0.0.1", reveal_labels=["Join our team"]))
    assert nav_recipes.get_recipe("http://127.0.0.1:8000/j") is None, \
        "a loopback recipe must never reach the committed library"

    p = _store()  # an explicit store is the caller's own — loopback is fine there
    nav_recipes.save_recipe(NavRecipe("127.0.0.1", reveal_labels=["Join our team"]), path=p)
    assert nav_recipes.get_recipe("http://127.0.0.1:8000/j", path=p) is not None

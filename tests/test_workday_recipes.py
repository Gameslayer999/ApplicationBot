"""Workday M2 recipe-backbone tests (decision 061) — store + unrecognized detection + replay.

All offline: the store round-trips a PII-free recipe (selectors + labels only); `unrecognized_fields`
finds a tenant's custom questions (skipping known + already-filled fields) headless on a fixture;
`replay_recipe` re-fills them deterministically via the resolver — no Claude. The agentic worker
that *produces* a recipe (Claude over CDP) is the next brick and the flagged live step.

Run:  python -m tests.test_workday_recipes   (also pytest-compatible; some tests need chromium)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot import workday, workday_recipes
from applicationbot.apply import AnswerResolver, ApplyReport
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.resume import load_resume
from applicationbot.workday_recipes import Recipe, RecipeField

REPO = Path(__file__).resolve().parent.parent
CUSTOM = (REPO / "fixtures" / "apply_forms" / "workday_custom.html").as_uri()
SAMPLE = str(REPO / "examples" / "sample_resume.yaml")


def _store():
    return str(Path(tempfile.mkdtemp()) / "recipes.json")


def _resolver(**profile_kw):
    return AnswerResolver(resume=load_resume(SAMPLE),
                          profile=ApplicationProfile(**profile_kw), enable_generation=False)


# --- store ---------------------------------------------------------------

def test_store_roundtrip_and_pii_free():
    p = _store()
    workday_recipes.save_recipe(Recipe("sig1", [
        RecipeField("customGithub", "text", "GitHub profile URL"),
        RecipeField("referralSource", "dropdown", "How did you hear about us?"),
    ]), path=p)
    text = Path(p).read_text()
    assert "http" not in text and "@" not in text  # only selectors + questions, no answers/PII
    r = workday_recipes.get_recipe("sig1", path=p)
    assert r and len(r.fields) == 2 and r.fields[0].automation_id == "customGithub"
    assert workday_recipes.get_recipe("nope", path=p) is None


def test_store_merge_dedupes_by_automation_id():
    p = _store()
    workday_recipes.save_recipe(Recipe("s", [RecipeField("a", "text", "A")]), path=p)
    workday_recipes.save_recipe(Recipe("s", [RecipeField("a", "text", "A"),
                                             RecipeField("b", "text", "B")]), path=p)
    r = workday_recipes.get_recipe("s", path=p)
    assert [f.automation_id for f in r.fields] == ["a", "b"]  # 'a' not duplicated


def test_committed_library_present_and_valid():
    # ships with the repo as an (initially empty) dict — never crashes load
    assert isinstance(workday_recipes.load_recipes(), dict)


# --- detection + replay (headless) ---------------------------------------

def test_unrecognized_fields_finds_custom_skips_known_and_filled():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(CUSTOM, wait_until="domcontentloaded")
        fields = workday.unrecognized_fields(page)
        b.close()
    ids = {f["automation_id"] for f in fields}
    assert ids == {"customGithub", "customWhy", "referralSource"}
    assert "legalNameSection_firstName" not in ids   # known → excluded
    assert "customPrefilled" not in ids               # already filled → excluded
    byid = {f["automation_id"]: f for f in fields}
    assert byid["customWhy"]["control"] == "text" and "Why do you want" in byid["customWhy"]["question"]
    assert byid["referralSource"]["control"] == "dropdown"


def test_replay_recipe_fills_resolvable_skips_unanswerable():
    from playwright.sync_api import sync_playwright

    recipe = Recipe("sig", [
        RecipeField("customGithub", "text", "GitHub profile URL"),      # resolves → github_url
        RecipeField("customWhy", "text", "Why do you want to work here?"),  # open-ended → skipped
    ])
    resolver = _resolver(github_url="https://github.com/ada")
    report = ApplyReport(url=CUSTOM, ats="workday")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(CUSTOM, wait_until="domcontentloaded")
        n = workday.replay_recipe(page, recipe, resolver, report)
        gh = page.locator("[data-automation-id='customGithub'] input").input_value()
        why = page.locator("[data-automation-id='customWhy'] textarea").input_value()
        b.close()
    assert n == 1                                   # only the resolvable field filled
    assert gh == "https://github.com/ada" and why == ""
    assert [f.source for f in report.filled] == ["workday-recipe"]


# --- agentic fallback: distillation by DOM diff (fake agent, no Claude/CDP) ---

def test_agent_prompt_and_argv_shape():
    fields = [{"automation_id": "customWhy", "control": "text", "question": "Why join us?"}]
    resolver = _resolver(github_url="https://github.com/ada", years_experience="7")
    prompt = workday.agent_prompt(fields, resolver.resume, resolver.profile)
    assert "Why join us?" in prompt and "customWhy" in prompt
    assert "Do NOT click Next" in prompt and "NEVER invent" in prompt   # navigation + HARD RULES
    assert "github.com/ada" in prompt                                    # applicant facts injected
    argv = workday._agent_argv("/tmp/mcp.json", "claude-sonnet-5")
    assert argv[0] == "claude" and "--output-format" in argv and "stream-json" in argv
    cfg = workday._agent_mcp_config(9333)
    assert "cdp-endpoint=http://localhost:9333" in cfg["mcpServers"]["playwright"]["args"][1]


def test_run_agent_fill_distills_only_what_the_agent_filled():
    from playwright.sync_api import sync_playwright

    def fake_agent(page, fields, prompt, *, cdp_port, model, report):
        # the "agent" fills two of the three custom fields; leaves referralSource alone
        page.fill("[data-automation-id='customGithub'] input", "https://github.com/x")
        page.fill("[data-automation-id='customWhy'] textarea", "Because I love it.")

    resolver = _resolver()
    report = ApplyReport(url=CUSTOM, ats="workday")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(CUSTOM, wait_until="domcontentloaded")
        learned = workday.run_agent_fill(page, resolver.resume, resolver.profile, report, _spawn=fake_agent)
        b.close()
    ids = {rf.automation_id for rf in learned}
    assert ids == {"customGithub", "customWhy"}           # diff: exactly what went empty→filled
    assert "referralSource" not in ids                     # agent didn't fill it → not learned
    assert all(f.source == "workday-agent" for f in report.filled)


def test_learn_once_then_replay_no_agent():
    from playwright.sync_api import sync_playwright

    store = _store()
    resolver = _resolver(github_url="https://github.com/ada")

    def fake_agent(page, fields, prompt, *, cdp_port, model, report):
        page.fill("[data-automation-id='customGithub'] input", "anything")  # agent handles this one

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        # 1) learn: agent fills customGithub; distill + persist the recipe
        p1 = b.new_page(); p1.goto(CUSTOM, wait_until="domcontentloaded")
        rep1 = ApplyReport(url=CUSTOM, ats="workday")
        learned = workday.run_agent_fill(p1, resolver.resume, resolver.profile, rep1, _spawn=fake_agent)
        sig = workday._page_signature(p1)
        workday_recipes.save_recipe(Recipe(sig, learned), path=store)
        p1.close()

        # 2) replay on a FRESH load of the same page — deterministic, no agent
        p2 = b.new_page(); p2.goto(CUSTOM, wait_until="domcontentloaded")
        recipe = workday_recipes.get_recipe(sig, path=store)
        rep2 = ApplyReport(url=CUSTOM, ats="workday")
        n = workday.replay_recipe(p2, recipe, resolver, rep2)
        val = p2.locator("[data-automation-id='customGithub'] input").input_value()
        b.close()

    assert recipe is not None and [f.automation_id for f in recipe.fields] == ["customGithub"]
    assert n == 1 and val == "https://github.com/ada"   # replayed from the recipe, re-resolved per user
    assert [f.source for f in rep2.filled] == ["workday-recipe"]


# --- M2 part 2: wired into fill_wizard (replay → agentic fallback → persist), gated ---

def test_agentic_enabled_off_by_default():
    d = Path(tempfile.mkdtemp())
    assert workday.agentic_enabled(d / "nope.yaml") is False           # missing file → off
    (d / "off.yaml").write_text("armed: true\n")
    assert workday.agentic_enabled(d / "off.yaml") is False            # key absent → off
    (d / "on.yaml").write_text("workday_agentic: true\n")
    assert workday.agentic_enabled(d / "on.yaml") is True              # opt-in


def test_fill_wizard_learns_then_replays_without_agent():
    from playwright.sync_api import sync_playwright

    store = _store()
    resolver = _resolver(github_url="https://github.com/ada")
    calls = {"agent": 0}

    def fake_agent(page, fields, prompt, *, cdp_port, model, report):
        calls["agent"] += 1
        page.fill("[data-automation-id='customGithub'] input", "agent-typed")  # learns customGithub

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        # run 1: agentic ON, empty store → agent fills + a recipe is persisted
        p1 = b.new_page(); p1.goto(CUSTOM, wait_until="domcontentloaded")
        rep1 = ApplyReport(url=CUSTOM, ats="workday")
        workday.fill_wizard(p1, resolver.resume, resolver.profile, rep1, resolver=resolver,
                            agentic=True, store_path=store, _agent_spawn=fake_agent)
        p1.close()

        # run 2: agentic OFF — the learned recipe replays customGithub deterministically, no agent
        p2 = b.new_page(); p2.goto(CUSTOM, wait_until="domcontentloaded")
        rep2 = ApplyReport(url=CUSTOM, ats="workday")
        workday.fill_wizard(p2, resolver.resume, resolver.profile, rep2, resolver=resolver,
                            agentic=False, store_path=store, _agent_spawn=fake_agent)
        gh = p2.locator("[data-automation-id='customGithub'] input").input_value()
        b.close()

    assert calls["agent"] == 1                       # agent ran ONCE (run 1), never in run 2
    assert any(f.source == "workday-agent" for f in rep1.filled)
    assert gh == "https://github.com/ada"            # run 2 replayed it, re-resolved for this user
    assert [f.source for f in rep2.filled if f.label == "customGithub"] == ["workday-recipe"]


def test_fill_wizard_without_resolver_is_pure_m1():
    # No resolver ⇒ M2 path is skipped entirely (M1 behaviour unchanged, Guideline #7).
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page(); page.goto(CUSTOM, wait_until="domcontentloaded")
        rep = ApplyReport(url=CUSTOM, ats="workday")
        workday.fill_wizard(page, load_resume(SAMPLE), ApplicationProfile(), rep)  # no resolver kwarg
        b.close()
    assert not any(f.source in ("workday-recipe", "workday-agent") for f in rep.filled)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

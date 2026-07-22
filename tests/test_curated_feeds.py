"""Curated GitHub job boards (DECISIONS.md #073, #074): dropping in a feed by URL, and
resolving Workday/SmartRecruiters listings that the curated filter used to discard."""

from __future__ import annotations

import pytest

from applicationbot import discovery, filters
from applicationbot.discovery import CuratedListSource, DiscoveryError
from applicationbot.filters import DiscoveryFilters, EarlyCareerConfig
from applicationbot.models import Contact, Experience, Resume, SkillCategory


def _resume() -> Resume:
    return Resume(
        contact=Contact(name="Test", email="t@example.com"),
        skills=[SkillCategory(category="Languages", items=["python", "go"])],
        experience=[Experience(organization="Acme", role="Software Engineer",
                               start="Jun 2024", end="Aug 2024", bullets=[])],
    )


def _listing(**kw) -> dict:
    e = {"title": "Software Engineer", "url": "https://boards.greenhouse.io/acme/jobs/1",
         "company_name": "Acme", "active": True, "locations": ["NYC"]}
    e.update(kw)
    return e


@pytest.fixture
def feeds(monkeypatch):
    """Stub the network: {url: payload}. Returns the dict for the test to populate."""
    payloads: dict[str, object] = {}
    monkeypatch.setattr(discovery, "fetch_json", lambda url, **kw: payloads[url])
    monkeypatch.setattr(discovery, "_resolve_jd", lambda url, ats, cache: f"JD for {url}")
    return payloads


# --------------------------------------------------------------- drop-in feeds


def test_drop_in_feed_by_url_flows_through(feeds):
    """A GitHub board the code has never heard of, added by URL alone, yields Postings."""
    url = "https://raw.githubusercontent.com/who/ever/main/listings.json"
    feeds[url] = [_listing(title="Backend Engineer", url="https://jobs.lever.co/x/abcd1234")]
    out = CuratedListSource(_resume(), kinds=(), feeds={"who/ever": url}).fetch()
    assert [(p.company, p.title, p.ats) for p in out] == [("Acme", "Backend Engineer", "lever")]
    assert out[0].body == "JD for https://jobs.lever.co/x/abcd1234"


def test_builtin_and_dropped_in_feeds_merge_and_dedup(feeds):
    """Built-ins and a custom feed rank jointly; the same URL from two feeds survives once."""
    dup = "https://boards.greenhouse.io/acme/jobs/1"
    feeds[discovery._BUILTIN_FEEDS["new-grad"]] = [_listing(url=dup)]
    feeds["https://raw.githubusercontent.com/who/ever/main/listings.json"] = [
        _listing(url=dup), _listing(title="Other", url="https://jobs.ashbyhq.com/x/1234abcd")
    ]
    src = CuratedListSource(
        _resume(), kinds=("new-grad",),
        feeds={"who/ever": "https://raw.githubusercontent.com/who/ever/main/listings.json"},
    )
    urls = [p.url for p in src.fetch()]
    assert sorted(urls) == ["https://boards.greenhouse.io/acme/jobs/1",
                            "https://jobs.ashbyhq.com/x/1234abcd"]


def test_max_resolve_is_a_global_budget_across_feeds(feeds):
    """max_resolve caps the whole run, not each feed — adding boards must not multiply cost."""
    # Distinct titles so the repost-dedup pass (decision 123) never collapses these — this test
    # is about the max_resolve budget, not dedup.
    feeds[discovery._BUILTIN_FEEDS["new-grad"]] = [
        _listing(title=f"SWE gh {i}", url=f"https://boards.greenhouse.io/a/jobs/{i}") for i in range(5)
    ]
    feeds["https://raw.githubusercontent.com/who/ever/main/listings.json"] = [
        _listing(title=f"SWE lv {i}", url=f"https://jobs.lever.co/b/{i:08x}") for i in range(5)
    ]
    src = CuratedListSource(
        _resume(), kinds=("new-grad",), max_resolve=3,
        feeds={"who/ever": "https://raw.githubusercontent.com/who/ever/main/listings.json"},
    )
    assert len(src.fetch()) == 3


def test_inactive_and_unfillable_listings_are_skipped(feeds):
    feeds[discovery._BUILTIN_FEEDS["new-grad"]] = [
        _listing(url="https://boards.greenhouse.io/a/jobs/1", active=False),
        _listing(url="https://jobs.apple.com/x/1"),  # ats 'other' — can't resolve or fill
        _listing(url="https://boards.greenhouse.io/a/jobs/2"),
    ]
    out = CuratedListSource(_resume(), kinds=("new-grad",)).fetch()
    assert [p.url for p in out] == ["https://boards.greenhouse.io/a/jobs/2"]


# --------------------------------------------------- actionable errors on a bad feed


def test_feed_returning_non_list_names_the_feed_and_the_fix(feeds):
    feeds["https://example.com/oops"] = {"message": "Not Found"}
    src = CuratedListSource(_resume(), kinds=(), feeds={"typo": "https://example.com/oops"})
    with pytest.raises(DiscoveryError) as e:
        src.fetch()
    assert "typo" in str(e.value) and "https://example.com/oops" in str(e.value)
    assert "listings.json" in str(e.value)  # states the fix, not just the symptom


def test_feed_with_wrong_schema_names_the_missing_keys(feeds):
    feeds["https://example.com/wrong"] = [{"foo": "bar"}]
    src = CuratedListSource(_resume(), kinds=(), feeds={"wrong": "https://example.com/wrong"})
    with pytest.raises(DiscoveryError) as e:
        src.fetch()
    assert "missing" in str(e.value) and "company_name" in str(e.value)


# ------------------------------------------------------------------ config plumbing


def test_bare_url_string_is_named_after_its_repo():
    cfg = EarlyCareerConfig(feeds=[
        "https://raw.githubusercontent.com/vanshb03/New-Grad-2026/dev/.github/scripts/listings.json"
    ])
    assert cfg.feeds[0].name == "vanshb03/New-Grad-2026"
    assert cfg.feeds[0].url.endswith("listings.json")


def test_bare_builtin_name_resolves_to_its_url():
    cfg = EarlyCareerConfig(feeds=["intern"])
    assert cfg.feeds[0].url == discovery._BUILTIN_FEEDS["intern"]


def test_unknown_builtin_name_is_rejected_with_the_valid_options():
    with pytest.raises(ValueError) as e:
        EarlyCareerConfig(feeds=["nope"])
    assert "new-grad" in str(e.value)  # tells the user what IS valid


def test_build_sources_passes_feeds_through_to_the_source():
    f = DiscoveryFilters(early_career=EarlyCareerConfig(
        enabled=True, kinds=["new-grad"],
        feeds=["https://raw.githubusercontent.com/who/ever/main/listings.json"],
    ))
    src = next(s for s in filters.build_sources(f, resume=_resume())
               if isinstance(s, CuratedListSource))
    assert src.feeds["who/ever"] == "https://raw.githubusercontent.com/who/ever/main/listings.json"
    assert "new-grad" in src.feeds  # built-in still present
    # name drives the discovery-cache fingerprint: adding a feed must invalidate it
    assert src.name == "curated:new-grad,who/ever"


# ------------------------------------------------------- workday / smartrecruiters unlock


def test_workday_and_smartrecruiters_now_survive_the_curated_filter(feeds):
    # Distinct titles so repost-dedup (decision 123) keeps both — this test is about ATS survival.
    feeds[discovery._BUILTIN_FEEDS["new-grad"]] = [
        _listing(title="SWE Workday", url="https://kla.wd1.myworkdayjobs.com/Search/job/Ann-Arbor-MI/Eng_1"),
        _listing(title="SWE SmartRecruiters", url="https://jobs.smartrecruiters.com/Acme/743999"),
    ]
    out = CuratedListSource(_resume(), kinds=("new-grad",)).fetch()
    assert sorted(p.ats for p in out) == ["smartrecruiters", "workday"]


def test_workday_jd_resolves_via_the_enrichment_cascade(monkeypatch):
    """_resolve_workday_jd delegates to enrich.fetch_full_jd with no llm= (free tiers only)."""
    from applicationbot import enrich

    seen = {}

    def fake(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return enrich.EnrichResult(description="Full Workday JD", tier="json-ld")

    monkeypatch.setattr(enrich, "fetch_full_jd", fake)
    url = "https://kla.wd1.myworkdayjobs.com/Search/job/Ann-Arbor-MI/Eng_1"
    assert discovery._resolve_workday_jd(url) == "Full Workday JD"
    assert seen["url"] == url
    assert "llm" not in seen["kw"]  # must not pay for an LLM call


def test_resolve_jd_routes_workday_and_swallows_failures(monkeypatch):
    monkeypatch.setattr(discovery, "_resolve_workday_jd", lambda url: "WD")
    assert discovery._resolve_jd("https://x.myworkdayjobs.com/a/job/b", "workday", {}) == "WD"

    def boom(url):
        raise DiscoveryError("network down")

    monkeypatch.setattr(discovery, "_resolve_workday_jd", boom)
    # a resolver failure degrades to a title-only body, it does not kill the run
    assert discovery._resolve_jd("https://x.myworkdayjobs.com/a/job/b", "workday", {}) == ""

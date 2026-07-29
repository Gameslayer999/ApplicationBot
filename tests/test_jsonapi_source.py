"""Declarative JSON-API aggregator source (DECISIONS.md #136).

JsonApiSource turns a spec (endpoint template + JSON list-path + field map + pagination) into a
list[Posting]. Network is faked by monkeypatching discovery.fetch_json, so these assert the
mapping/pagination/query logic exactly, no live calls."""
from __future__ import annotations

import pytest

from applicationbot import discovery
from applicationbot.discovery import AggregatorSpec, JsonApiSource


def _spec(**kw):
    base = dict(
        name="remotive",
        endpoint="https://x.test/api?search={q}",
        list_path="jobs",
        field_map={"title": "title", "company": "company_name", "body": "description",
                   "apply_url": "url", "location": "candidate_required_location"},
    )
    base.update(kw)
    return AggregatorSpec(**base)


def test_maps_list_path_and_fields(monkeypatch):
    monkeypatch.setattr(discovery, "fetch_json", lambda url: {"jobs": [
        {"title": "Backend Engineer", "company_name": "Acme", "description": "<p>Build things</p>",
         "url": "https://acme.example/apply/1", "candidate_required_location": "Worldwide"},
    ]})
    posts = JsonApiSource(_spec(), keywords=["engineer"]).fetch()
    assert len(posts) == 1
    p = posts[0]
    assert (p.title, p.company, p.ats) == ("Backend Engineer", "Acme", "jsonapi")
    assert p.apply_url == "https://acme.example/apply/1" and p.url == p.apply_url
    assert p.body == "Build things"  # html stripped
    assert p.location == "Worldwide"
    assert p.extra["snippet_only"] is True and p.extra["platform"] == "remotive"


def test_dotted_source_path_and_root_array(monkeypatch):
    # list_path "" → the response itself is the array; a dotted map reaches into nested company obj.
    monkeypatch.setattr(discovery, "fetch_json", lambda url: [
        {"position": "SRE", "company": {"name": "Globex"}, "apply": "https://g.example/1"},
    ])
    spec = _spec(list_path="", query_required=False, endpoint="https://x.test/api",
                 field_map={"title": "position", "company": "company.name", "apply_url": "apply"})
    posts = JsonApiSource(spec).fetch()
    assert (posts[0].title, posts[0].company, posts[0].apply_url) == ("SRE", "Globex", "https://g.example/1")


def test_query_substitution_runs_once_per_keyword(monkeypatch):
    seen_urls = []

    def fake(url):
        seen_urls.append(url)
        return {"jobs": [{"title": "T", "company_name": "C", "url": "https://e/" + url[-1]}]}

    monkeypatch.setattr(discovery, "fetch_json", fake)
    JsonApiSource(_spec(), keywords=["python", "rust"]).fetch()
    assert any("search=python" in u for u in seen_urls) and any("search=rust" in u for u in seen_urls)


def test_offset_pagination_walks_pages(monkeypatch):
    def fake(url):
        off = int(dict(pair.split("=") for pair in url.split("?")[1].split("&")).get("offset", "0"))
        if off == 0:
            return {"jobs": [{"title": f"J{i}", "company_name": "C", "url": f"https://e/{i}"} for i in range(2)]}
        if off == 2:
            return {"jobs": [{"title": "J2", "company_name": "C", "url": "https://e/2"}]}
        return {"jobs": []}

    monkeypatch.setattr(discovery, "fetch_json", fake)
    spec = _spec(endpoint="https://x.test/api?q={q}&limit={limit}&offset={offset}",
                 paginate="offset", page_size=2, query_required=True)
    posts = JsonApiSource(spec, keywords=["x"]).fetch()
    assert [p.title for p in posts] == ["J0", "J1", "J2"]  # page 2 (short) stops the walk


def test_skips_items_missing_title_or_apply_and_dedups(monkeypatch):
    monkeypatch.setattr(discovery, "fetch_json", lambda url: {"jobs": [
        {"title": "", "company_name": "C", "url": "https://e/1"},          # no title → skip
        {"title": "Has title", "company_name": "C", "url": ""},            # no apply → skip
        {"title": "Good", "company_name": "C", "url": "https://e/2"},
        {"title": "Dup", "company_name": "C", "url": "https://e/2"},       # same url → dedup
    ]})
    posts = JsonApiSource(_spec(query_required=False, endpoint="https://x.test/api"),).fetch()
    assert [p.title for p in posts] == ["Good"]


def test_max_results_caps_output(monkeypatch):
    monkeypatch.setattr(discovery, "fetch_json", lambda url: {"jobs": [
        {"title": f"J{i}", "company_name": "C", "url": f"https://e/{i}"} for i in range(50)]})
    posts = JsonApiSource(_spec(max_results=5, query_required=False, endpoint="https://x.test/api")).fetch()
    assert len(posts) == 5


def test_from_dict_ignores_unknown_keys():
    spec = AggregatorSpec.from_dict({"name": "muse", "endpoint": "https://x/{q}", "bogus": 1})
    assert spec.name == "muse" and not hasattr(spec, "bogus")


def test_jsonapi_ats_is_bridge_eligible():
    # One registry entry makes every declarative source ride the aggregator→ATS bridge + fillability.
    assert discovery.JSONAPI_ATS in discovery._AGGREGATOR_ATS


def test_build_sources_includes_enabled_json_aggregator(monkeypatch):
    from applicationbot import filters as filters_mod
    monkeypatch.setattr(filters_mod, "load_aggregator_specs",
                        lambda *a, **k: {"remotive": _spec(query_required=False)})
    f = filters_mod.DiscoveryFilters(json_aggregators=["remotive"])
    names = [s.name for s in filters_mod.build_sources(f)]  # no résumé; spec is query-less
    assert "jsonapi:remotive" in names
    # A name with no matching registry spec is silently skipped (never crashes build_sources).
    f2 = filters_mod.DiscoveryFilters(json_aggregators=["does-not-exist"])
    assert not [s for s in filters_mod.build_sources(f2) if s.name.startswith("jsonapi:")]


def test_load_aggregator_specs_reads_registry(tmp_path):
    reg = tmp_path / "aggregator_specs.json"
    reg.write_text('{"specs": [{"name": "muse", "endpoint": "https://x/{q}", "list_path": "results",'
                   ' "field_map": {"title": "name"}}, {"name": "", "endpoint": "skip-me"}]}')
    specs = discovery.load_aggregator_specs(str(reg))
    assert set(specs) == {"muse"}  # nameless entry dropped
    assert specs["muse"].endpoint == "https://x/{q}" and specs["muse"].list_path == "results"


"""Phase-2 spec staging in source_scout (DECISIONS.md #136): validate a declarative JSON-API spec
against a live-faked fetch, upsert into the committed registry, enable by name in discovery.yaml.
Network is faked by monkeypatching discovery.fetch_json."""
from __future__ import annotations

import pytest

from applicationbot import discovery, filters as filters_mod, source_scout as ss

GOOD = {"name": "remotive", "endpoint": "https://x.test/api?search={q}", "list_path": "jobs",
        "field_map": {"title": "title", "company": "company_name", "apply_url": "url"}}


def _one_job(url):
    return {"jobs": [{"title": "Backend Engineer", "company_name": "Acme", "url": "https://acme/1"}]}


def test_validate_spec_rejects_incomplete_field_map():
    bad = {**GOOD, "field_map": {"title": "title"}}  # no company, no url/apply_url
    res = ss.validate_spec(bad)
    assert not res.validated and "company" in res.error and "url" in res.error


def test_validate_spec_reports_zero_postings(monkeypatch):
    monkeypatch.setattr(discovery, "fetch_json", lambda url: {"jobs": []})
    res = ss.validate_spec(GOOD, keywords=["engineer"])
    assert not res.validated and "0 usable postings" in res.error


def test_validate_spec_passes_live(monkeypatch):
    monkeypatch.setattr(discovery, "fetch_json", _one_job)
    res = ss.validate_spec(GOOD, keywords=["engineer"])
    assert res.validated and res.n_postings == 1 and res.sample_title == "Backend Engineer"


def test_stage_spec_upserts_registry_only_when_valid(tmp_path, monkeypatch):
    reg = str(tmp_path / "aggregator_specs.json")
    monkeypatch.setattr(discovery, "fetch_json", _one_job)
    res = ss.stage_spec(GOOD, keywords=["engineer"], path=reg)
    assert res.validated
    specs = ss.load_registry_specs(reg)
    assert [s["name"] for s in specs] == ["remotive"]
    assert specs[0]["n_postings"] == 1 and specs[0]["sample_title"] == "Backend Engineer"  # stamped

    # Re-staging the same name upserts (no duplicate); an invalid spec is never written.
    ss.stage_spec({**GOOD, "list_path": "jobs"}, keywords=["x"], path=reg)
    assert len(ss.load_registry_specs(reg)) == 1
    monkeypatch.setattr(discovery, "fetch_json", lambda url: {"jobs": []})
    ss.stage_spec({**GOOD, "name": "dead"}, keywords=["x"], path=reg)
    assert {s["name"] for s in ss.load_registry_specs(reg)} == {"remotive"}


def test_enable_json_aggregator_appends_and_is_idempotent(tmp_path, monkeypatch):
    import yaml
    # enable_json_aggregator reads the registry via load_registry_specs() (default path); point it
    # at a fixed in-memory registry so the test never touches the real committed file.
    monkeypatch.setattr(ss, "load_registry_specs", lambda *a, **k: [{"name": "remotive"}])

    fpath = tmp_path / "discovery.yaml"
    fpath.write_text(yaml.safe_dump({"json_aggregators": []}))
    assert ss.enable_json_aggregator("remotive", filters_path=str(fpath)) is True
    assert ss.enable_json_aggregator("remotive", filters_path=str(fpath)) is False  # already enabled
    assert ss.enable_json_aggregator("not-in-registry", filters_path=str(fpath)) is False
    assert filters_mod.load_filters(str(fpath)).json_aggregators == ["remotive"]

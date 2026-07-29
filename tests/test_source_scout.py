"""Tests for source_scout — the deterministic half of auto source discovery (DECISIONS.md #134).

Covers URL→board extraction across every supported ATS and its URL shapes, dedup vs configured
boards, the committed-candidates store round-trip, and the accept→discovery.yaml merge. The live
ATS probe (`validate_board`) is network and is exercised via `scout(..., validate=False)`."""

from pathlib import Path

import pytest

from applicationbot import source_scout as ss
from applicationbot.filters import Board, DiscoveryFilters


@pytest.mark.parametrize(
    "url,expected",
    [
        # Greenhouse — board, job-boards host, and the embedded widget (?for=)
        ("https://boards.greenhouse.io/stripe", ("greenhouse", "stripe")),
        ("https://job-boards.greenhouse.io/airbnb/jobs/12345", ("greenhouse", "airbnb")),
        ("https://boards.greenhouse.io/embed/job_app?for=databricks&token=9", ("greenhouse", "databricks")),
        # Lever
        ("https://jobs.lever.co/netflix/abc-123-def", ("lever", "netflix")),
        # Ashby
        ("https://jobs.ashbyhq.com/openai/some-uuid-here", ("ashby", "openai")),
        # SmartRecruiters
        ("https://jobs.smartrecruiters.com/Visa/743999", ("smartrecruiters", "Visa")),
        # Workable — path form and subdomain form
        ("https://apply.workable.com/mlabs/j/ABC123/", ("workable", "mlabs")),
        ("https://bunq.workable.com/", ("workable", "bunq")),
        # Recruitee — subdomain is the token
        ("https://acme.recruitee.com/o/senior-engineer", ("recruitee", "acme")),
        # bare host (no scheme) still parses
        ("boards.greenhouse.io/notion", ("greenhouse", "notion")),
    ],
)
def test_extract_board_supported(url, expected):
    assert ss.extract_board(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://www.linkedin.com/jobs/view/123",  # excluded consumer board
        "https://www.indeed.com/viewjob?jk=abc",
        "https://example.com/careers",  # unknown ATS
        "https://boards.greenhouse.io/embed",  # routing word only, no token
    ],
)
def test_extract_board_rejects_unsupported(url):
    assert ss.extract_board(url) is None


def test_extract_boards_dedups_case_insensitive_and_preserves_order():
    urls = [
        "https://boards.greenhouse.io/Stripe",
        "https://boards.greenhouse.io/stripe/jobs/9",  # same board, different case+path
        "https://jobs.lever.co/netflix",
    ]
    assert ss.extract_boards(urls) == [("greenhouse", "Stripe"), ("lever", "netflix")]


def test_scout_skips_already_configured_boards(tmp_path):
    filters = DiscoveryFilters(boards=[Board(ats="greenhouse", token="stripe")])
    store = tmp_path / "cands.json"
    res = ss.scout(
        ["https://boards.greenhouse.io/stripe", "https://jobs.lever.co/netflix"],
        filters, validate=False, store_path=str(store),
    )
    assert res.skipped_known == 1  # stripe already configured
    assert [c.key for c in res.proposed] == [("lever", "netflix")]
    assert res.n_new == 1


def test_candidates_store_roundtrip_and_merge(tmp_path):
    store = tmp_path / "cands.json"
    a = ss.Candidate(ats="lever", token="netflix", provenance="web-search", validated=True, n_postings=5)
    ss.save_candidates([a], str(store))
    loaded = ss.load_candidates(str(store))
    assert len(loaded) == 1 and loaded[0].key == ("lever", "netflix")

    # Re-finding a candidate refreshes its result but keeps original provenance; adds no duplicate.
    refound = ss.Candidate(ats="lever", token="Netflix", provenance="discovery-harvest",
                           validated=True, n_postings=9)
    merged, n_new = ss.merge_new(loaded, [refound])
    assert n_new == 0
    assert len(merged) == 1
    assert merged[0].n_postings == 9  # refreshed
    assert merged[0].provenance == "web-search"  # original kept


def test_candidates_file_has_no_pii_comment_and_is_committable(tmp_path):
    store = tmp_path / "cands.json"
    ss.save_candidates([ss.Candidate(ats="ashby", token="openai", validated=True)], str(store))
    doc = store.read_text()
    assert "public company ATS slugs" in doc  # self-documenting non-PII header
    assert "candidates" in doc


def test_merge_into_filters_appends_and_is_idempotent(tmp_path):
    import yaml
    fpath = tmp_path / "discovery.yaml"
    fpath.write_text(yaml.safe_dump({"boards": [{"ats": "greenhouse", "token": "stripe"}]}))

    assert ss.merge_into_filters("lever", "netflix", filters_path=str(fpath)) is True
    assert ss.merge_into_filters("lever", "netflix", filters_path=str(fpath)) is False  # already there

    from applicationbot.filters import load_filters
    boards = {(b.ats, b.token) for b in load_filters(str(fpath)).boards}
    assert ("lever", "netflix") in boards and ("greenhouse", "stripe") in boards

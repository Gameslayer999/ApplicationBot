"""Seen-openings ledger tests (decision 053) — stubbed network + Claude, no tokens.

Covers the module round-trip (record/dedup/canonicalize/clear) and the pipeline wiring:
`only_new` hides openings a previous run already showed (live AND cache-hit paths), records
what it surfaces, and stays a no-op when off so the runner is unaffected.

Run:  python -m tests.test_discovery_seen   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from applicationbot import discovery_cache, discovery_seen, pipeline
from applicationbot.discovery import Posting
from applicationbot.filters import Board, DiscoveryFilters
from applicationbot.matching import Match


def _resume(summary="backend engineer"):
    return SimpleNamespace(
        summary=summary,
        skills=[SimpleNamespace(items=["python", "sql"])],
        experience=[SimpleNamespace(role="Software Engineer", organization="Acme",
                                    bullets=["built things"], start="2020", end="2023")],
    )


def _posting(n=1):
    return Posting(company=f"Co{n}", title="SWE", body="python sql backend",
                   url=f"https://boards.greenhouse.io/co{n}/jobs/{n}", ats="greenhouse")


def _filters(**kw):
    base = dict(boards=[Board(ats="greenhouse", token="co")], skip_seen=False,
                min_skills=1, cache_ttl_hours=12)
    base.update(kw)
    return DiscoveryFilters(**base)


class _Env:
    """Patch the network/Claude edges of discover_and_match and point the cache, fit-learning,
    and seen-ledger files at a temp dir. Counts how many live board searches ran."""

    def __init__(self, tmp: Path, postings):
        self.tmp = tmp
        self.postings = postings
        self.searches = 0
        self._orig = {}

    def __enter__(self):
        import applicationbot.backends as backends
        import applicationbot.fit_learning as fit_learning

        def fake_discover(sources):
            self.searches += 1
            return list(self.postings), []

        def fake_match(resume, postings, **kw):
            return [Match(posting=p, keyword_score=3, matched_skills=["python"],
                          fit_score=80, qualified=True, judged_by="claude")
                    for p in postings], []

        self._orig = {
            "discover": pipeline.discover, "match": pipeline.match,
            "avail": backends.claude_code_available,
            "cache_path": discovery_cache.DEFAULT_PATH, "seen_path": discovery_seen.DEFAULT_PATH,
            "fit_path": fit_learning.DEFAULT_PATH, "runs_path": fit_learning.RUNS_PATH,
        }
        pipeline.discover = fake_discover
        pipeline.match = fake_match
        backends.claude_code_available = lambda: True
        discovery_cache.DEFAULT_PATH = self.tmp / "discovery_cache.json"
        discovery_seen.DEFAULT_PATH = self.tmp / "discovery_seen.json"
        fit_learning.DEFAULT_PATH = self.tmp / "fit_history.jsonl"
        fit_learning.RUNS_PATH = self.tmp / "fit_runs.jsonl"
        self._backends, self._fit_learning = backends, fit_learning
        return self

    def __exit__(self, *a):
        pipeline.discover = self._orig["discover"]
        pipeline.match = self._orig["match"]
        self._backends.claude_code_available = self._orig["avail"]
        discovery_cache.DEFAULT_PATH = self._orig["cache_path"]
        discovery_seen.DEFAULT_PATH = self._orig["seen_path"]
        self._fit_learning.DEFAULT_PATH = self._orig["fit_path"]
        self._fit_learning.RUNS_PATH = self._orig["runs_path"]


def _run(resume, filters, **kw):
    return pipeline.discover_and_match(resume, filters, **kw)


# --------------------------------------------------------------------------- module unit tests

def test_module_record_dedup_canonicalize_clear():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seen.json"
        assert discovery_seen.seen_urls(path) == set()
        # Two spellings of the same posting (tracking query) canonicalize to one entry.
        added = discovery_seen.record(
            ["https://boards.greenhouse.io/co1/jobs/1?utm_source=x",
             "https://boards.greenhouse.io/co1/jobs/1"], path)
        assert added == 1
        assert discovery_seen.record(["https://boards.greenhouse.io/co1/jobs/1"], path) == 0
        assert discovery_seen.record([""], path) == 0  # empty URL ignored
        assert len(discovery_seen.seen_urls(path)) == 1
        assert discovery_seen.clear(path) is True
        assert discovery_seen.clear(path) is False and discovery_seen.seen_urls(path) == set()


def test_bad_or_missing_ledger_reads_empty():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seen.json"
        assert discovery_seen.load(path) == {}          # missing
        path.write_text("{not json", encoding="utf-8")
        assert discovery_seen.load(path) == {}          # unparseable
        path.write_text('{"version": 999, "seen": {"x": "t"}}', encoding="utf-8")
        assert discovery_seen.load(path) == {}          # wrong schema


# --------------------------------------------------------------------------- pipeline wiring

def test_only_new_hides_already_shown_on_rerun():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1), _posting(2)]) as env:
        r, f = _resume(), _filters()
        first = _run(r, f, only_new=True)
        assert len(first.matches) == 2 and first.skipped_shown == 0
        # Second run reuses the cache (no new search) and hides both already-shown openings.
        second = _run(r, f, only_new=True)
        assert second.from_cache and env.searches == 1
        assert second.matches == [] and second.skipped_shown == 2


def test_only_new_surfaces_just_the_new_posting():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1)]) as env:
        r, f = _resume(), _filters()
        _run(r, f, only_new=True)                       # Co1 shown + recorded
        env.postings = [_posting(1), _posting(2)]        # a new opening appears
        nxt = _run(r, f, only_new=True, force_fresh=True)  # re-search to pick it up
        assert [m.posting.company for m in nxt.matches] == ["Co2"]
        assert nxt.skipped_shown == 1


def test_show_all_ignores_ledger_and_does_not_record():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1), _posting(2)]) as env:
        r, f = _resume(), _filters()
        _run(r, f, only_new=True)                        # records Co1, Co2
        # only_new=False (the --all / "Re-search fresh" path) shows everything, records nothing.
        allrun = _run(r, f, only_new=False)
        assert len(allrun.matches) == 2 and allrun.skipped_shown == 0
        # Ledger unchanged: a later only_new run still hides exactly the original two.
        again = _run(r, f, only_new=True)
        assert again.matches == [] and again.skipped_shown == 2


def test_off_by_default_writes_no_ledger():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1)]) as env:
        r, f = _resume(), _filters()
        res = _run(r, f)                                 # only_new defaults False (runner path)
        assert res.skipped_shown == 0
        assert not discovery_seen.DEFAULT_PATH.exists(), "no ledger written when only_new is off"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

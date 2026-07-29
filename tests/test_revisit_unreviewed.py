"""Bring back never-reviewed applications (decision 149) — stubbed network + Claude, no tokens.

An application the loop prepared but the user never opened used to be buried twice over: the
tracker skip (`skip_seen`) dropped its posting because a row existed, and the seen-openings
ledger dropped it because it had already been judged. Together that meant a posting prepared
while nobody was watching never came up again.

Covers the tracker half (`mark_reviewed` / `unreviewed_source_urls`) and the pipeline wiring:
an unreviewed prepared posting is exempt from BOTH filters, opening its review re-buries it,
and `revisit=False` (the loop's later hunt passes) restores the strict only-new behaviour.

Run:  python -m tests.test_revisit_unreviewed   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from applicationbot import discovery_cache, discovery_seen, fit_learning, pipeline, tracker
from applicationbot.discovery import Posting
from applicationbot.filters import Board, DiscoveryFilters
from applicationbot.matching import Match

URL = "https://boards.greenhouse.io/co1/jobs/1"


def _resume():
    return SimpleNamespace(
        summary="backend engineer",
        skills=[SimpleNamespace(items=["python", "sql"])],
        experience=[SimpleNamespace(role="Software Engineer", organization="Acme",
                                    bullets=["built things"], start="2020", end="2023")],
    )


def _posting():
    return Posting(company="Co1", title="SWE", body="python sql backend", url=URL,
                   ats="greenhouse")


def _filters(**kw):
    base = dict(boards=[Board(ats="greenhouse", token="co")], skip_seen=True,
                min_skills=1, cache_ttl_hours=0)
    base.update(kw)
    return DiscoveryFilters(**base)


class _Env:
    """Point the tracker DB, discovery cache, seen ledger and fit history at a temp dir, and
    stub the board search + Claude judge so a run costs nothing.

    The tracker's readers take `path` as a DEFAULT ARGUMENT, bound at import — rebinding
    `tracker.DEFAULT_DB` alone would leave the pipeline reading the real applications.db. So
    the two readers the pipeline calls are wrapped to pass the temp DB explicitly; the real
    implementations still run, they just read the throwaway file. `self.db` is the path every
    test writes through."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db = tmp / "applications.db"
        self._orig = {}

    def __enter__(self):
        import applicationbot.backends as backends

        def fake_discover(sources):
            return [_posting()], []

        def fake_match(resume, postings, **kw):
            return [Match(posting=p, keyword_score=3, matched_skills=["python"],
                          fit_score=80, qualified=True, judged_by="claude")
                    for p in postings], []

        self._orig = {
            "discover": pipeline.discover, "match": pipeline.match,
            "avail": backends.claude_code_available, "db": tracker.DEFAULT_DB,
            "seen_urls": tracker.seen_source_urls, "unreviewed": tracker.unreviewed_source_urls,
            "cache_path": discovery_cache.DEFAULT_PATH, "seen_path": discovery_seen.DEFAULT_PATH,
            "fit_path": fit_learning.DEFAULT_PATH, "runs_path": fit_learning.RUNS_PATH,
        }
        real_seen, real_unreviewed = tracker.seen_source_urls, tracker.unreviewed_source_urls
        tracker.seen_source_urls = lambda **kw: real_seen(**{**kw, "path": self.db})
        tracker.unreviewed_source_urls = lambda **kw: real_unreviewed(**{**kw, "path": self.db})
        pipeline.discover = fake_discover
        pipeline.match = fake_match
        backends.claude_code_available = lambda: True
        tracker.DEFAULT_DB = self.db
        discovery_cache.DEFAULT_PATH = self.tmp / "discovery_cache.json"
        discovery_seen.DEFAULT_PATH = self.tmp / "discovery_seen.json"
        fit_learning.DEFAULT_PATH = self.tmp / "fit_history.jsonl"
        fit_learning.RUNS_PATH = self.tmp / "fit_runs.jsonl"
        self._backends = backends
        return self

    def __exit__(self, *a):
        pipeline.discover = self._orig["discover"]
        pipeline.match = self._orig["match"]
        self._backends.claude_code_available = self._orig["avail"]
        tracker.DEFAULT_DB = self._orig["db"]
        tracker.seen_source_urls = self._orig["seen_urls"]
        tracker.unreviewed_source_urls = self._orig["unreviewed"]
        discovery_cache.DEFAULT_PATH = self._orig["cache_path"]
        discovery_seen.DEFAULT_PATH = self._orig["seen_path"]
        fit_learning.DEFAULT_PATH = self._orig["fit_path"]
        fit_learning.RUNS_PATH = self._orig["runs_path"]


def _prepared(env, status="dry-run", url=URL):
    """A row as the loop's prepare step leaves it: prepared, never submitted, never reviewed."""
    return tracker.add_application(
        {"company": "Co1", "role": "SWE", "source_url": url, "status": status}, path=env.db)


# --------------------------------------------------------------------------- tracker unit tests

def test_unreviewed_lists_only_prepared_and_unseen():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d)) as env:
        prepared = _prepared(env)
        _prepared(env, status="applied", url="https://boards.greenhouse.io/co2/jobs/2")
        assert tracker.unreviewed_source_urls() == {URL}, "only the prepared, unreviewed row"

        assert tracker.mark_reviewed(prepared, path=env.db) is True   # this call stamped it
        assert tracker.mark_reviewed(prepared, path=env.db) is False  # already reviewed
        assert tracker.unreviewed_source_urls() == set()
        assert tracker.get_application(prepared, path=env.db)["reviewed_at"] != ""


# --------------------------------------------------------------------------- pipeline wiring

def test_unreviewed_posting_comes_back_past_both_filters():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d)) as env:
        app_id = _prepared(env)
        # The ledger already holds it (an earlier only_new run judged it) AND the tracker skip
        # would drop it — both suppressions are live.
        discovery_seen.record([URL])
        assert URL in tracker.seen_source_urls()

        res = pipeline.discover_and_match(_resume(), _filters(), only_new=True)
        assert [m.posting.url for m in res.matches] == [URL], "never-reviewed ⇒ brought back"
        assert res.skipped_seen == 0 and res.skipped_shown == 0
        assert res.funnel["revisited"] == 1, "the breakdown names why it is back"

        # The user opens its review panel: it is now reviewed, so both filters bury it again.
        tracker.mark_reviewed(app_id, path=env.db)
        res2 = pipeline.discover_and_match(_resume(), _filters(), only_new=True)
        assert res2.matches == [], "reviewed ⇒ suppressed exactly as before"
        assert res2.skipped_seen == 1
        assert "revisited" not in res2.funnel


def test_revisit_off_keeps_strict_only_new():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d)) as env:
        _prepared(env)
        discovery_seen.record([URL])
        # The loop's later hunt passes pass revisit=False so the same unreviewed postings are
        # not re-judged on every pass of one run.
        res = pipeline.discover_and_match(_resume(), _filters(), only_new=True, revisit=False)
        assert res.matches == [] and res.skipped_seen == 1


def test_applied_posting_never_comes_back():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d)) as env:
        _prepared(env, status="applied")   # submitted — reviewed or not, it stays suppressed
        res = pipeline.discover_and_match(_resume(), _filters(), only_new=True)
        assert res.matches == [] and res.skipped_seen == 1


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all revisit-unreviewed tests passed")

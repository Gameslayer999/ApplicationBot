"""Discovery snapshot cache tests (decision 036) — stubbed network + Claude, no tokens.

Verifies the cache skips the board search on a fresh reuse, that `--fresh`/TTL/fingerprint
changes force a real re-search, and that a role applied to since the snapshot still drops
out of a cache hit via skip_seen.

Run:  python -m tests.test_discovery_cache   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from applicationbot import discovery_cache, pipeline
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
    """Patch the network/Claude edges of discover_and_match and point the cache at a temp
    file. Counts how many live board searches actually ran."""

    def __init__(self, tmp: Path, postings):
        self.tmp = tmp
        self.postings = postings
        self.searches = 0
        self._orig = {}

    def __enter__(self):
        import applicationbot.backends as backends

        def fake_discover(sources):
            self.searches += 1
            return list(self.postings), []

        def fake_match(resume, postings, **kw):
            # One Claude-judged match per posting (fit 80), preserving input order.
            return [Match(posting=p, keyword_score=3, matched_skills=["python"],
                          fit_score=80, qualified=True, judged_by="claude")
                    for p in postings], []

        self._orig["discover"] = pipeline.discover
        self._orig["match"] = pipeline.match
        self._orig["avail"] = backends.claude_code_available
        self._orig["path"] = discovery_cache.DEFAULT_PATH
        pipeline.discover = fake_discover
        pipeline.match = fake_match
        backends.claude_code_available = lambda: True
        discovery_cache.DEFAULT_PATH = self.tmp / "discovery_cache.json"
        self._backends = backends
        return self

    def __exit__(self, *a):
        pipeline.discover = self._orig["discover"]
        pipeline.match = self._orig["match"]
        self._backends.claude_code_available = self._orig["avail"]
        discovery_cache.DEFAULT_PATH = self._orig["path"]


def _run(resume, filters, **kw):
    return pipeline.discover_and_match(resume, filters, **kw)


def test_fresh_run_saves_and_second_run_reuses():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1), _posting(2)]) as env:
        r, f = _resume(), _filters()
        first = _run(r, f)
        assert env.searches == 1 and not first.from_cache and len(first.matches) == 2
        second = _run(r, f)
        assert env.searches == 1, "second run must not hit the network"
        assert second.from_cache and len(second.matches) == 2
        assert second.cache_age_seconds is not None


def test_force_fresh_bypasses_cache():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1)]) as env:
        r, f = _resume(), _filters()
        _run(r, f)
        _run(r, f, force_fresh=True)
        assert env.searches == 2, "--fresh must re-search even with a fresh snapshot"


def test_ttl_zero_disables_cache():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1)]) as env:
        r, f = _resume(), _filters(cache_ttl_hours=0)
        _run(r, f)
        _run(r, f)
        assert env.searches == 2, "cache_ttl_hours=0 disables reuse"


def test_resume_change_invalidates_cache():
    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1)]) as env:
        f = _filters()
        _run(_resume("backend engineer"), f)
        _run(_resume("nurse practitioner"), f)  # different résumé → different fingerprint
        assert env.searches == 2, "a changed résumé must invalidate the snapshot"


def test_skip_seen_prunes_cached_matches(monkeypatch=None):
    import applicationbot.pipeline as pl

    with tempfile.TemporaryDirectory() as d, _Env(Path(d), [_posting(1), _posting(2)]) as env:
        r = _resume()
        # First run with skip_seen off populates a 2-match snapshot.
        _run(r, _filters(skip_seen=False))
        # Now pretend Co1 is in the tracker; a skip_seen cache hit must drop it.
        orig = pl._seen_canonical_urls
        from applicationbot.discovery import canonical_url
        pl._seen_canonical_urls = lambda filters: {canonical_url(_posting(1).url)}
        try:
            hit = _run(r, _filters(skip_seen=True))
        finally:
            pl._seen_canonical_urls = orig
        assert hit.from_cache and env.searches == 1
        assert hit.skipped_seen == 1 and len(hit.matches) == 1
        assert hit.matches[0].posting.company == "Co2"


def test_module_roundtrip_and_staleness():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "c.json"
        m = [Match(posting=_posting(1), keyword_score=3, matched_skills=["python"],
                   fit_score=90, qualified=True, judged_by="claude")]
        discovery_cache.save("fp1", m, [], discovered=5, after_gates=3, bridged=0, path=path)
        good = discovery_cache.load("fp1", ttl_hours=12, path=path)
        assert good is not None and len(good.matches) == 1 and good.discovered == 5
        assert good.matches[0].fit_score == 90 and good.matches[0].posting.company == "Co1"
        assert discovery_cache.load("other-fp", ttl_hours=12, path=path) is None
        assert discovery_cache.load("fp1", ttl_hours=0, path=path) is None  # instantly stale


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

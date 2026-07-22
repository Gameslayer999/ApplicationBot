"""Discovery-robustness tests (2026-07-06 audit): request pacing, retry/backoff on
transient HTTP failures, canonical-URL dedup, and the opt-in staleness gate.

No network: urllib.request.urlopen and discovery._sleep are monkeypatched.

Run:  python -m tests.test_discovery_robustness   (also pytest-compatible)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from applicationbot import discovery
from applicationbot.discovery import (
    DiscoveryError,
    Posting,
    Source,
    canonical_url,
    discover,
    fetch_json,
)
from applicationbot.filters import DiscoveryFilters, apply_gates


# ---------------------------------------------------------------------------
# fakes / patch helpers
# ---------------------------------------------------------------------------

class _Resp:
    """Minimal stand-in for urlopen's response context manager."""

    def __init__(self, payload) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(url: str, code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"status {code}", headers or {}, None)


class _Patched:
    """Monkeypatch urlopen with a scripted sequence of results (an Exception instance is
    raised; anything else is returned), and record every _sleep() without sleeping."""

    def __init__(self, sequence) -> None:
        self._sequence = list(sequence)
        self.calls = 0
        self.sleeps: list[float] = []

    def __enter__(self):
        self._orig_urlopen = urllib.request.urlopen
        self._orig_sleep = discovery._sleep
        discovery._last_request_at.clear()

        def fake_urlopen(req, **kwargs):
            self.calls += 1
            result = self._sequence.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        urllib.request.urlopen = fake_urlopen
        discovery._sleep = self.sleeps.append
        return self

    def __exit__(self, *args):
        urllib.request.urlopen = self._orig_urlopen
        discovery._sleep = self._orig_sleep
        discovery._last_request_at.clear()
        return False


# ---------------------------------------------------------------------------
# fetch_json: retry / backoff
# ---------------------------------------------------------------------------

def test_fetch_json_retries_429_and_honors_retry_after():
    url = "https://api.example.com/jobs"
    with _Patched([_http_error(url, 429, {"Retry-After": "7"}), _Resp({"ok": True})]) as p:
        assert fetch_json(url) == {"ok": True}
        assert p.calls == 2
        assert 7 in p.sleeps  # Retry-After honored, not the default 1s backoff


def test_fetch_json_caps_retry_after_at_30s():
    url = "https://api.example.com/jobs"
    with _Patched([_http_error(url, 429, {"Retry-After": "999"}), _Resp({"ok": 1})]) as p:
        fetch_json(url)
        assert 30 in p.sleeps and 999 not in p.sleeps


def test_fetch_json_500_exhausts_retries_then_raises():
    url = "https://api.example.com/jobs"
    errs = [_http_error(url, 500) for _ in range(3)]
    with _Patched(errs) as p:
        try:
            fetch_json(url)
            raise AssertionError("expected DiscoveryError")
        except DiscoveryError as e:
            assert "HTTP 500" in str(e) and "(after 3 attempts)" in str(e)
        assert p.calls == 3
        assert 1.0 in p.sleeps and 3.0 in p.sleeps  # backoff schedule used


def test_fetch_json_404_fails_immediately_no_retry():
    url = "https://api.example.com/jobs"
    with _Patched([_http_error(url, 404)]) as p:
        try:
            fetch_json(url)
            raise AssertionError("expected DiscoveryError")
        except DiscoveryError as e:
            assert "HTTP 404" in str(e) and "attempts" not in str(e)
        assert p.calls == 1
        assert not p.sleeps  # first request never paces, and no backoff


def test_fetch_json_urlerror_retries_then_succeeds():
    url = "https://api.example.com/jobs"
    with _Patched([urllib.error.URLError("conn reset"), _Resp([1, 2])]) as p:
        assert fetch_json(url) == [1, 2]
        assert p.calls == 2 and 1.0 in p.sleeps


# ---------------------------------------------------------------------------
# _polite_wait: per-host pacing
# ---------------------------------------------------------------------------

def test_polite_wait_paces_same_host_only_within_interval():
    orig_sleep = discovery._sleep
    sleeps: list[float] = []
    discovery._sleep = sleeps.append
    discovery._last_request_at.clear()
    try:
        discovery._polite_wait("a.example.com")  # first request: no wait
        assert sleeps == []
        discovery._polite_wait("a.example.com")  # immediate repeat: waits the remainder
        assert len(sleeps) == 1 and 0 < sleeps[0] <= discovery._MIN_REQUEST_INTERVAL_S
        discovery._polite_wait("b.example.com")  # different host: independent, no wait
        assert len(sleeps) == 1
        # same host but the interval has already passed: no wait
        discovery._last_request_at["a.example.com"] = time.monotonic() - 1.0
        discovery._polite_wait("a.example.com")
        assert len(sleeps) == 1
    finally:
        discovery._sleep = orig_sleep
        discovery._last_request_at.clear()


# ---------------------------------------------------------------------------
# canonical_url
# ---------------------------------------------------------------------------

def test_canonical_url_rules():
    # utm_* / source / ref / src stripped; trailing slash and host case normalized
    assert (
        canonical_url("HTTPS://Boards.Greenhouse.io/acme/jobs/123/?utm_source=li&ref=x&src=y&source=z")
        == "https://boards.greenhouse.io/acme/jobs/123"
    )
    # job-identifying query params kept, tracking params dropped
    assert (
        canonical_url("https://acme.com/careers?utm_campaign=q2&gh_jid=456")
        == "https://acme.com/careers?gh_jid=456"
    )
    # fragment always dropped
    assert canonical_url("https://x.com/a#apply") == "https://x.com/a"
    # path case is preserved (job slugs are case-sensitive)
    assert canonical_url("https://x.com/Jobs/A") == "https://x.com/Jobs/A"


def test_discover_dedups_two_url_spellings():
    def _posting(url: str) -> Posting:
        return Posting(company="Acme", title="SWE", body="jd", url=url, ats="greenhouse")

    class _Fake(Source):
        def __init__(self, name: str, url: str) -> None:
            self.name, self._url = name, url

        def fetch(self) -> list[Posting]:
            return [_posting(self._url)]

    s1 = _Fake("one", "https://Boards.Greenhouse.io/acme/jobs/1/?utm_source=li")
    s2 = _Fake("two", "https://boards.greenhouse.io/acme/jobs/1")
    postings, errors = discover([s1, s2])
    assert errors == []
    assert len(postings) == 1


# ---------------------------------------------------------------------------
# staleness gate (max_posting_age_days)
# ---------------------------------------------------------------------------

def _aged_posting(updated_at, title="SWE") -> Posting:
    # Distinct titles so the repost-dedup pass never collapses these — this fixture isolates
    # the staleness gate.
    return Posting(company="Acme", title=title, body="jd",
                   url="https://x.com/j/1", ats="greenhouse", updated_at=updated_at)


def test_staleness_gate_off_by_default_keeps_old_postings():
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    kept = apply_gates([_aged_posting(old)], DiscoveryFilters())
    assert len(kept) == 1


def test_staleness_gate_drops_old_keeps_missing_unparseable_and_recent():
    f = DiscoveryFilters(max_posting_age_days=30)
    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(days=60)).isoformat()
    old_zulu = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_ms = str(int((now - timedelta(days=2)).timestamp() * 1000))  # Lever epoch-ms
    postings = [
        _aged_posting(old_iso, "SWE 1"),        # dropped
        _aged_posting(old_zulu, "SWE 2"),       # dropped ('Z' suffix parses)
        _aged_posting("", "SWE 3"),             # kept: missing date passes
        _aged_posting("last Tuesday", "SWE 4"), # kept: unparseable passes
        _aged_posting(recent_ms, "SWE 5"),      # kept: recent ms-epoch parses
    ]
    kept = apply_gates(postings, f)
    assert [p.updated_at for p in kept] == ["", "last Tuesday", recent_ms]


# ---------------------------------------------------------------------------
# company_exclude gate + repost dedup
# ---------------------------------------------------------------------------

def _p(company, title, url):
    return Posting(company=company, title=title, body="jd", url=url, ats="greenhouse")


def test_company_exclude_drops_by_company_substring():
    f = DiscoveryFilters(company_exclude=["Consultadd", "DellFor"])
    postings = [
        _p("Consultadd Public Serv", "Python Developer", "https://x/1"),   # dropped
        _p("DellFor Technologies", "Java Developer", "https://x/2"),       # dropped
        _p("Dexcom", "Software Engineer", "https://x/3"),                  # kept
    ]
    stats = {}
    kept = apply_gates(postings, f, stats=stats)
    assert [p.company for p in kept] == ["Dexcom"]
    assert stats["gate_company"] == 2


def test_dedup_collapses_same_company_title_reposts_with_distinct_urls():
    # A staffing agency posts one role three times under DISTINCT urls — dedup keeps the first.
    postings = [
        _p("Consultadd", "Python Developer", "https://x/a"),
        _p("Consultadd", "python  developer", "https://x/b"),  # same after normalization
        _p("Consultadd", "Python-Developer", "https://x/c"),   # same after normalization
        _p("Consultadd", "Data Engineer", "https://x/d"),      # different title — kept
        _p("Other Co", "Python Developer", "https://x/e"),     # different company — kept
    ]
    stats = {}
    kept = apply_gates(postings, DiscoveryFilters(), stats=stats)
    assert [p.url for p in kept] == ["https://x/a", "https://x/d", "https://x/e"]
    assert stats["gate_duplicate"] == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} discovery-robustness test(s) passed.")

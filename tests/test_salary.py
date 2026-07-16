"""Market salary estimation tests (decision 039) — stubbed Claude + Adzuna, no tokens/network.

Covers the cross-check policy (agree→mean, diverge→lower, single-source), the per-(title,
location) cache (reuse within TTL, recompute when stale, no source call on a hit), the
"extremely wrong" band-validation that invalidates a stale cached estimate, and the resolver
precedence advertised-band > market-estimate > stored desired_salary.

Run:  python -m tests.test_salary   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from applicationbot import salary
from applicationbot.apply import AnswerResolver
from applicationbot.apply_profile import ApplicationProfile
from applicationbot.models import Contact, Resume


def _cache_path() -> Path:
    return Path(tempfile.mkdtemp()) / "salary_cache.json"


def _stub_sources(monkey_claude=None, monkey_adzuna=None):
    """Return (claude_calls, adzuna_calls) counters after monkeypatching the two sources onto
    fixed return values (or a callable). Restores nothing — call inside a test that owns them."""
    calls = {"claude": 0, "adzuna": 0}

    def claude(*a, **k):
        calls["claude"] += 1
        return monkey_claude

    def adzuna(*a, **k):
        calls["adzuna"] += 1
        return monkey_adzuna

    salary._claude_estimate = claude
    salary._adzuna_estimate = adzuna
    return calls


# --------------------------------------------------------------------------- cross-check

def test_reconcile_policy():
    assert salary.reconcile(150000, 160000)[0] == 155000       # agree → mean
    assert salary.reconcile(180000, 140000)[0] == 140000       # diverge >20% → lower
    assert salary.reconcile(150000, None)[0] == 150000         # claude only
    assert salary.reconcile(None, 145000)[0] == 145000         # adzuna only
    assert salary.reconcile(None, None) is None                # nothing


# --------------------------------------------------------------------------- advertised band

def test_advertised_band_parsing():
    assert salary.advertised_band(None, "CA Base Pay Range is $124,000 - $186,000 USD") == (124000, 186000)
    assert salary.advertised_band("$120K – $180K") == (120000, 180000)
    assert salary.advertised_band("$100,000 to $140,000") == (100000, 140000)
    assert salary.advertised_band("$40 - $60 per hour") is None   # hourly excluded (<1000)
    assert salary.advertised_band("great team, competitive pay") is None
    # structured compensation preferred over the JD body (first arg wins)
    assert salary.advertised_band("$150,000 - $170,000", "$90,000 - $100,000") == (150000, 170000)


# --------------------------------------------------------------------------- cache

def test_estimate_caches_and_reuses():
    path = _cache_path()
    calls = _stub_sources(monkey_claude=150000, monkey_adzuna=160000)
    v1 = salary.estimate("Senior Software Engineer", "Austin, TX", "6 years", cache_path=path)
    assert v1 == 155000 and calls == {"claude": 1, "adzuna": 1}
    # Second call for the same role hits the cache — no source call.
    v2 = salary.estimate("Senior Software Engineer", "Austin, TX", "6 years", cache_path=path)
    assert v2 == 155000 and calls == {"claude": 1, "adzuna": 1}


def test_estimate_recomputes_when_stale():
    path = _cache_path()
    calls = _stub_sources(monkey_claude=150000, monkey_adzuna=160000)
    salary.estimate("SWE", "Remote", cache_path=path)
    assert calls["claude"] == 1
    # Age the cached entry past the TTL, then estimate again → recompute.
    import json
    data = json.loads(path.read_text())
    key = next(iter(data))
    data[key]["computed_at"] = (datetime.now() - timedelta(days=salary.TTL_DAYS + 1)).isoformat(timespec="seconds")
    path.write_text(json.dumps(data))
    salary.estimate("SWE", "Remote", cache_path=path)
    assert calls["claude"] == 2  # recomputed


def test_estimate_none_when_no_source():
    path = _cache_path()
    _stub_sources(monkey_claude=None, monkey_adzuna=None)
    assert salary.estimate("Nowhere Job", "Nowhere", cache_path=path) is None
    assert not path.exists()  # nothing cached when nothing was produced


# --------------------------------------------------------------------------- band validation

def test_validate_invalidates_extremely_wrong_estimate():
    path = _cache_path()
    _stub_sources(monkey_claude=80000, monkey_adzuna=80000)
    salary.estimate("SWE", "SF", cache_path=path)          # caches 80000
    # A real posting later advertises $150k-$190k — 80000 is >40% below the floor → invalidated.
    assert salary.validate_against_band("SWE", "SF", (150000, 190000), cache_path=path) is True
    import json
    assert json.loads(path.read_text()) == {}


def test_validate_keeps_in_range_estimate():
    path = _cache_path()
    _stub_sources(monkey_claude=160000, monkey_adzuna=160000)
    salary.estimate("SWE", "SF", cache_path=path)          # caches 160000
    # A real band of $150k-$190k contains 160000 → kept.
    assert salary.validate_against_band("SWE", "SF", (150000, 190000), cache_path=path) is False


def test_validate_noop_without_entry():
    path = _cache_path()
    assert salary.validate_against_band("Unknown", "Nowhere", (100000, 150000), cache_path=path) is False


# --------------------------------------------------------------------------- resolver precedence

def _resolver(**kw):
    resume = Resume(contact=Contact(name="Jane Doe", email="j@example.com"))
    prof = ApplicationProfile(desired_salary="85000")
    return AnswerResolver(resume=resume, profile=prof, **kw)


def test_resolver_prefers_band_then_market_then_stored():
    # 1. Advertised band → midpoint, ignoring both market estimate and stored figure.
    r = _resolver(pay="$124,000 - $186,000", market_salary="150000")
    assert r._salary_expectation() == "155000"
    # 2. No band → the injected market estimate, not the stored figure.
    r = _resolver(market_salary="150000")
    assert r._salary_expectation() == "150000"
    # 3. No band and no estimate → the stored desired_salary.
    r = _resolver()
    assert r._salary_expectation() == "85000"
    # Routed through the real question label too.
    r = _resolver(market_salary="150000")
    assert r.resolve("Expected salary") == "150000"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

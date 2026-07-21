"""Market salary estimation for the salary-expectation field (decision 039).

Layered on top of decision 038:
  * When a posting **advertises** a pay band, the Apply resolver fills its midpoint
    (decision 038) — `advertised_band()` lives here so both the resolver and the pipeline
    parse a band the same way.
  * When a posting advertises **nothing**, `estimate()` supplies a dynamic fallback in place
    of the static profile figure: a market estimate for (title, location, seniority) that
    Claude and Adzuna cross-check, saved per (title, location) in git-ignored
    ``profile/salary_cache.json`` and reused until it goes stale (TTL) or a real advertised
    band later shows the cached number is extremely off (`validate_against_band()`).

Cross-check policy (decision 039): both sources agreeing within 20% → their mean; a larger
disagreement → the **lower** of the two (never over-ask on a shaky estimate), with the
divergence recorded on the cache entry.

Best-effort throughout: any source (Claude, Adzuna, cache I/O) that fails is skipped, and if
nothing is available `estimate()` returns None so the caller falls back to the profile's
stored ``desired_salary``. A usable cache hit makes **no** network or Claude call. Adzuna is
optional — without ``ADZUNA_APP_ID``/``ADZUNA_APP_KEY`` the estimate degrades to Claude-only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .paths import DATA_ROOT
DEFAULT_CACHE_PATH = DATA_ROOT / "profile" / "salary_cache.json"

TTL_DAYS = 30.0            # a cached estimate is refreshed after this many days
AGREE_TOLERANCE = 0.20    # ≤20% apart = the two sources "agree"; wider = take the lower
BAND_SLACK = 0.40         # a cached value >40% outside a real advertised band is "extremely wrong"


# --------------------------------------------------------------------------- advertised band

# A posting's advertised annual pay band, e.g. "$124,000 - $186,000", "$120K–$180K",
# "$124,000 to $186,000". Two dollar-figures joined by a dash/"to" — specific enough to pick
# out of prose without matching stray numbers. 'K' notation handled; hourly bands ("$40-$60")
# are excluded by the ≥ 1000 floor in advertised_band().
_PAY_RANGE = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(?:-|–|—|to)\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?"
)


def _pay_figure(num: str, k: Optional[str]) -> int:
    return int(float(num.replace(",", "")) * (1000 if k else 1))


def advertised_band(*texts: Optional[str]) -> Optional[tuple[int, int]]:
    """Best-effort (low, high) annual pay band advertised by a posting, parsed from the given
    texts in order (typically the structured compensation string first, then the JD body).
    Returns None when no two-figure '$X - $Y' band (both ≥ 1000, so hourly rates are ignored)
    is found."""
    for text in texts:
        if not text:
            continue
        m = _PAY_RANGE.search(text)
        if not m:
            continue
        lo = _pay_figure(m.group(1), m.group(2))
        hi = _pay_figure(m.group(3), m.group(4))
        if lo >= 1000 and hi >= lo:
            return (lo, hi)
    return None


# --------------------------------------------------------------------------- cross-check

def reconcile(claude: Optional[int], adzuna: Optional[int]) -> Optional[tuple[int, str]]:
    """Combine the two source estimates into one figure per decision 039. Returns
    (value, note) or None if neither source produced a number.
      * both, within AGREE_TOLERANCE → their mean ("agree")
      * both, further apart          → the lower ("diverge …")
      * exactly one                  → that one ("<source> only")"""
    if claude and adzuna:
        spread = abs(claude - adzuna) / max(claude, adzuna)
        if spread <= AGREE_TOLERANCE:
            return (claude + adzuna) // 2, f"agree ({claude}/{adzuna}, {spread:.0%})"
        return min(claude, adzuna), f"diverge {spread:.0%} ({claude}/{adzuna}) — took lower"
    if claude:
        return claude, "claude only"
    if adzuna:
        return adzuna, "adzuna only"
    return None


# --------------------------------------------------------------------------- sources

def _claude_estimate(title: str, location: str, years: str, *,
                     model: Optional[str] = None) -> Optional[int]:
    """Median of Claude's estimated market base-salary range for this role. Best-effort:
    None if the CLI is unavailable or the reply doesn't parse."""
    from . import backends  # lazy: don't pull in the CLI plumbing unless we estimate

    yrs = f" with {years} of experience" if years else ""
    prompt = (
        "Estimate the current market BASE salary range (annual, USD) for this role. "
        "Use typical market pay for the title, seniority, and location — not a single "
        "company. Return ONLY a JSON object of integers: "
        '{"low": <number>, "high": <number>}.\n\n'
        f"TITLE: {title!r}\nLOCATION: {location or 'United States (national)'!r}\n"
        f"CANDIDATE{yrs}."
    )
    try:
        raw = backends.run_claude_cli(prompt, model=model, think=False, timeout=60, activity="salary")
        obj = json.loads(backends._extract_json(raw))
        lo, hi = int(obj["low"]), int(obj["high"])
    except Exception:
        return None
    if lo >= 1000 and hi >= lo:
        return (lo + hi) // 2
    return None


def _adzuna_estimate(title: str, location: str, *, app_id: str, app_key: str,
                     country: str = "us") -> Optional[int]:
    """Adzuna's mean advertised salary for this title+location (the top-level ``mean`` the
    search endpoint returns for the query). Best-effort: None without keys or on any failure."""
    if not (app_id and app_key and title):
        return None
    from urllib.parse import urlencode

    from .discovery import fetch_json

    params = {
        "app_id": app_id, "app_key": app_key,
        "results_per_page": 1, "content-type": "application/json",
        "what": title,
    }
    if location:
        params["where"] = location
    url = f"https://api.adzuna.com/v1/api/jobs/{country.lower()}/search/1?{urlencode(params)}"
    try:
        data = fetch_json(url)
    except Exception:  # best-effort: any network/parse failure → no Adzuna figure
        return None
    mean = data.get("mean") if isinstance(data, dict) else None
    try:
        val = int(float(mean))
    except (TypeError, ValueError):
        return None
    return val if val >= 1000 else None


# --------------------------------------------------------------------------- cache

def _now() -> datetime:
    return datetime.now()


def _key(title: str, location: str) -> str:
    norm = lambda s: " ".join((s or "").lower().split())
    return f"{norm(title)}|{norm(location)}"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _store(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # a cache write failure must never break an application run


def estimate(title: str, location: str, years: str = "", *,
             model: Optional[str] = None, app_id: str = "", app_key: str = "",
             cache_path: str | Path | None = None) -> Optional[int]:
    """The dynamic salary figure for (title, location), used only when a posting advertises no
    band. Returns a cached value when one is younger than TTL_DAYS; otherwise cross-checks
    Claude + Adzuna (`reconcile`), caches the result, and returns it. None when no source
    produced a figure (caller then falls back to the stored desired_salary)."""
    if not title:
        return None
    path = Path(cache_path or DEFAULT_CACHE_PATH)
    cache = _load(path)
    key = _key(title, location)
    entry = cache.get(key)
    if entry:
        try:
            age_days = (_now() - datetime.fromisoformat(entry["computed_at"])).days
        except (KeyError, ValueError):
            age_days = TTL_DAYS + 1  # unparseable timestamp → treat as stale
        if age_days < TTL_DAYS and isinstance(entry.get("value"), int):
            return entry["value"]

    claude = _claude_estimate(title, location, years, model=model)
    adzuna = _adzuna_estimate(title, location, app_id=app_id, app_key=app_key)
    result = reconcile(claude, adzuna)
    if result is None:
        return None
    value, note = result
    cache[key] = {
        "value": value, "claude": claude, "adzuna": adzuna,
        "note": note, "computed_at": _now().isoformat(timespec="seconds"),
    }
    _store(path, cache)
    return value


def validate_against_band(title: str, location: str, band: tuple[int, int], *,
                          cache_path: str | Path | None = None) -> bool:
    """Cross-check a cached estimate against a posting's REAL advertised band. If a cached
    value for (title, location) sits more than BAND_SLACK outside the band, it is "extremely
    wrong" — drop it so the next no-band posting recomputes. Returns True if an entry was
    invalidated. No-op when there's no cache entry (the common case)."""
    path = Path(cache_path or DEFAULT_CACHE_PATH)
    cache = _load(path)
    key = _key(title, location)
    entry = cache.get(key)
    if not entry or not isinstance(entry.get("value"), int):
        return False
    lo, hi = band
    value = entry["value"]
    if lo * (1 - BAND_SLACK) <= value <= hi * (1 + BAND_SLACK):
        return False
    del cache[key]
    _store(path, cache)
    return True

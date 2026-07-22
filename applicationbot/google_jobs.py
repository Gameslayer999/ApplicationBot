"""Google Jobs discovery — a minimal, proxy-free scraper for the Google Jobs vertical.

Vendored and adapted from JobSpy (https://github.com/speedyapply/JobSpy, MIT license) — only the
Google Jobs slice, deliberately NOT the LinkedIn/Indeed/Glassdoor scrapers (decision: we ship the
approved source, not a bot-evasion stack — Agent Guideline #4). Differences from upstream:

- No proxies, no TLS-fingerprint spoofing, no rotating user-agents. One honest browser UA, low
  request volume, and it backs off on HTTP 429 instead of routing around the limit.
- No pandas / JobPost model. `search()` returns plain dicts the caller maps onto our `Posting`.

Google exposes the jobs vertical at google.com/search?udm=8; results and the pagination cursor are
embedded in the page as opaque JSON keyed by internal ids (e.g. "520084652"). That keying is
Google's and can change without notice — this is best-effort scraping of a ToS-gray endpoint, so a
parse miss returns [] rather than raising (never aborts a discovery run).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

_SEARCH_URL = "https://www.google.com/search"
_CALLBACK_URL = "https://www.google.com/async/callback:550"
_JOBS_PER_PAGE = 10
_PAGE_DELAY_S = 1.0  # polite pause between pagination calls (single IP, no proxies)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_HEADERS_INITIAL = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.google.com/",
    "user-agent": _UA,
}
_HEADERS_JOBS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.google.com/",
    "user-agent": _UA,
}
# Opaque pagination token Google's async endpoint requires (from JobSpy). Static; tied to a Google
# build string — if pagination stops working, refresh this from a live google.com/search?udm=8 page.
_ASYNC_PARAM = "_fmt:prog,_id:fc_5FwaZ86OKsfdwN4P4La3yA4_2"


class GoogleJobsError(RuntimeError):
    """Google returned an error or a shape we couldn't parse."""


def _find_job_info(node) -> Optional[list]:
    """Walk the nested JSON to the list keyed by Google's job-array id."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "520084652" and isinstance(value, list):
                return value
            found = _find_job_info(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_job_info(item)
            if found:
                return found
    return None


def _find_job_info_initial_page(html_text: str) -> list:
    """Extract the job arrays embedded in the first search page's HTML."""
    pattern = '520084652":(' + r"\[.*?\]\s*])\s*}\s*]\s*]\s*]\s*]\s*]"
    results = []
    for match in re.finditer(pattern, html_text):
        try:
            results.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return results


def _parse_job(job_info: list, seen: set) -> Optional[dict]:
    """Map one Google job array to a plain dict; None if malformed or already seen."""
    try:
        job_url = job_info[3][0][0] if job_info[3] and job_info[3][0] else None
    except (IndexError, TypeError):
        return None
    if not job_url or job_url in seen:
        return None
    seen.add(job_url)

    title = job_info[0] if len(job_info) > 0 else ""
    company = job_info[1] if len(job_info) > 1 else ""
    location = job_info[2] if len(job_info) > 2 else ""
    description = job_info[19] if len(job_info) > 19 and isinstance(job_info[19], str) else ""

    posted_at = None
    days_ago_str = job_info[12] if len(job_info) > 12 else None
    if isinstance(days_ago_str, str):
        m = re.search(r"\d+", days_ago_str)
        if m:
            posted_at = (datetime.now(timezone.utc) - timedelta(days=int(m.group()))).isoformat()

    low = description.lower()
    return {
        "title": (title or "").strip(),
        "company": (company or "").strip() or "Unknown",
        "location": (location or "").strip(),
        "url": job_url,
        "description": description,
        "date_posted": posted_at,
        "is_remote": ("remote" in low or "wfh" in low) or None,
    }


def _parse_next_page(text: str, seen: set) -> tuple[list[dict], Optional[str]]:
    """Parse a pagination-callback response: (jobs, next_cursor)."""
    try:
        start = text.index("[[[")
        end = text.rindex("]]]") + 3
    except ValueError:
        return [], None
    try:
        parsed = json.loads(text[start:end])[0]
    except (json.JSONDecodeError, IndexError):
        return [], None

    m = re.search(r'data-async-fc="([^"]+)"', text)
    next_cursor = m.group(1) if m else None

    jobs: list[dict] = []
    for array in parsed:
        try:
            _, job_data = array
        except (ValueError, TypeError):
            continue
        if not isinstance(job_data, str) or not job_data.startswith("[[["):
            continue
        try:
            job_d = json.loads(job_data)
        except json.JSONDecodeError:
            continue
        info = _find_job_info(job_d)
        if info:
            job = _parse_job(info, seen)
            if job:
                jobs.append(job)
    return jobs, next_cursor


def _build_query(search_term: str, *, location: str, is_remote: bool, hours_old: int,
                 google_search_term: str) -> str:
    if google_search_term:
        return google_search_term
    q = f"{search_term} jobs"
    if location:
        q += f" near {location}"
    if hours_old:
        if hours_old <= 24:
            q += " since yesterday"
        elif hours_old <= 72:
            q += " in the last 3 days"
        elif hours_old <= 168:
            q += " in the last week"
        else:
            q += " in the last month"
    if is_remote:
        q += " remote"
    return q


def search(
    search_term: str,
    *,
    location: str = "",
    is_remote: bool = False,
    hours_old: int = 0,
    results_wanted: int = 40,
    google_search_term: str = "",
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """Search the Google Jobs vertical and return up to `results_wanted` job dicts.

    Proxy-free and low-volume: one query, cursor-paginated, ~1s between pages, and it stops on
    HTTP 429 (rate-limited) rather than working around it. Raises GoogleJobsError on a hard HTTP
    failure of the first request; a parse miss just yields fewer/no jobs."""
    results_wanted = max(1, min(results_wanted, 300))
    sess = session or requests.Session()
    seen: set = set()

    query = _build_query(search_term, location=location, is_remote=is_remote,
                         hours_old=hours_old, google_search_term=google_search_term)
    try:
        resp = sess.get(_SEARCH_URL, headers=_HEADERS_INITIAL,
                        params={"q": query, "udm": "8"}, timeout=20)
    except requests.RequestException as e:
        raise GoogleJobsError(f"Google request failed: {e}") from e
    if resp.status_code == 429:
        raise GoogleJobsError("Google rate-limited the request (HTTP 429); try again later.")
    if resp.status_code != 200:
        raise GoogleJobsError(f"Google returned HTTP {resp.status_code}.")

    m = re.search(r'<div jsname="Yust4d"[^>]+data-async-fc="([^"]+)"', resp.text)
    cursor = m.group(1) if m else None
    jobs: list[dict] = []
    for info in _find_job_info_initial_page(resp.text):
        job = _parse_job(info, seen)
        if job:
            jobs.append(job)

    # Google now renders the Jobs vertical client-side: a plain HTTP GET gets a JS shell with none
    # of the embedded job JSON (no cursor, no `520084652` arrays) this keyless scraper reads. Fail
    # loudly and actionably (UI Principle #3) instead of returning a misleading "0 jobs".
    if not jobs and not cursor:
        raise GoogleJobsError(
            "Google returned a JavaScript-only page with no readable job data — the keyless "
            "scraper can't parse current Google Jobs (results are rendered client-side). This "
            "source needs a headless-browser render path; use Adzuna or another aggregator for now."
        )

    while cursor and len(seen) < results_wanted:
        time.sleep(_PAGE_DELAY_S)
        try:
            r = sess.get(_CALLBACK_URL, headers=_HEADERS_JOBS,
                         params={"fc": cursor, "fcv": "3", "async": _ASYNC_PARAM}, timeout=20)
        except requests.RequestException:
            break
        if r.status_code == 429 or r.status_code != 200:
            break
        page_jobs, cursor = _parse_next_page(r.text, seen)
        if not page_jobs:
            break
        jobs.extend(page_jobs)

    return jobs[:results_wanted]

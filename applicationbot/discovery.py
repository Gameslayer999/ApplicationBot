"""Discover job postings (Stage 2) from public sources, normalized to the fixture shape.

Discovery is **qualification-driven, not company-driven** (DECISIONS.md #025): the pipeline
finds roles that fit the user, rather than making the user maintain a company list. This
module is the *source* layer — the pluggable set of places postings come from — mirroring
the pluggable-backends design (DECISIONS.md #008).

Sources implemented here are public, no-auth ATS job-board APIs. Greenhouse/Lever/Ashby are
the SAME ATSs the Apply stage natively drives (DECISIONS.md #016/#017); SmartRecruiters and
Recruitee are *additional, distinct* form systems added to exercise the Apply autofill on as
many ATS layouts as possible (DECISIONS.md #030) — a discovered posting flows straight
through Tailor → Apply on whatever system it lives on:

- Greenhouse     : GET  boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
- Lever          : GET  api.lever.co/v0/postings/{slug}?mode=json
- Ashby          : GET  api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
- SmartRecruiters: GET  api.smartrecruiters.com/v1/companies/{company}/postings (+ /{id} detail)
- Recruitee      : GET  {company}.recruitee.com/api/offers/
- Workable       : POST apply.workable.com/api/v3/accounts/{account}/jobs (+ v2 /{shortcode} detail)

All return the full job-description text with no scraping and no ToS grey area
(Agent Guideline #4). Broad aggregator sources (Adzuna, remote feeds) slot in behind the
same `Source` interface later. Every source yields `Posting`s; `Posting.to_job_description()`
emits the exact Markdown + YAML front-matter shape the fixtures use, so the Tailor/Apply
pipeline needs no changes when real discovery replaces fixtures.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Any, Optional

import yaml

from .job_description import JobDescription

_UA = "ApplicationBot/0.1 (personal job search; contact: local user)"
_TIMEOUT = 25

# SmartRecruiters and Workable list postings without the JD body, so we fetch each posting's
# detail (an N+1). Cap how many we pull per company to keep the run polite and bounded.
_DETAIL_MAX_POSTINGS = 100


# ---------------------------------------------------------------------------
# HTTP + HTML helpers (stdlib only; certifi used for CA certs if present)
# ---------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    """A verifying SSL context. Prefer certifi's CA bundle (present transitively via
    Playwright) since some Python installs ship without a usable system store; fall back
    to the system default. Never disables verification."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()

# --- Politeness pacing (Agent Guideline #4): min gap between requests to the same host. ---
_MIN_REQUEST_INTERVAL_S = 0.5
_last_request_at: dict[str, float] = {}  # host -> time.monotonic() of last request
_sleep = time.sleep  # module-level so tests can monkeypatch sleeping away

# Retry policy for transient HTTP failures (429/5xx/network): 2 retries with backoff.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 3.0)  # sleep before attempt 2, attempt 3
_RETRY_AFTER_CAP_S = 30


def _polite_wait(host: str) -> None:
    """Sleep just enough that consecutive requests to `host` are >= _MIN_REQUEST_INTERVAL_S
    apart. Per-host: different hosts never wait on each other."""
    last = _last_request_at.get(host)
    if last is not None:
        wait = _MIN_REQUEST_INTERVAL_S - (time.monotonic() - last)
        if wait > 0:
            _sleep(wait)
    _last_request_at[host] = time.monotonic()


def _retry_delay(e: urllib.error.HTTPError, attempt: int) -> float:
    """Backoff before the next attempt; for 429/503 honor an integer Retry-After header
    (capped at _RETRY_AFTER_CAP_S)."""
    delay = _RETRY_BACKOFF_S[attempt - 1]
    if e.code in (429, 503):
        ra = str((e.headers.get("Retry-After") if e.headers is not None else "") or "").strip()
        if ra.isdigit():
            delay = min(int(ra), _RETRY_AFTER_CAP_S)
    return delay


def fetch_text(url: str, *, method: str = "GET", body: Any = None,
               accept: str = "application/json") -> str:
    """Fetch a URL and return the decoded response body. Defaults to GET; pass a `body`
    (dict) to POST it as JSON (Workable's careers API is POST). Rate-limited per host
    (_polite_wait) and retried on transient failures (429/5xx/network, up to 3 attempts
    with backoff); other HTTP errors fail immediately. Raises DiscoveryError with a precise
    message on failure (Agent Guideline #11) so a single bad source never crashes a run."""
    headers = {"User-Agent": _UA, "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    host = urllib.parse.urlsplit(url).netloc
    exhausted = f" (after {_RETRY_ATTEMPTS} attempts)"
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        _polite_wait(host)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_SSL_CTX) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code != 429 and not 500 <= e.code < 600:  # non-transient: fail immediately
                raise DiscoveryError(f"HTTP {e.code} for {url}") from e
            if attempt < _RETRY_ATTEMPTS:
                _sleep(_retry_delay(e, attempt))
                continue
            raise DiscoveryError(f"HTTP {e.code} for {url}{exhausted}") from e
        except urllib.error.URLError as e:
            if attempt < _RETRY_ATTEMPTS:
                _sleep(_RETRY_BACKOFF_S[attempt - 1])
                continue
            raise DiscoveryError(f"network error for {url}: {e.reason}{exhausted}") from e
        except Exception as e:  # timeouts, ssl, etc.
            if attempt < _RETRY_ATTEMPTS:
                _sleep(_RETRY_BACKOFF_S[attempt - 1])
                continue
            raise DiscoveryError(f"request failed for {url}: {type(e).__name__}: {e}{exhausted}") from e
    raise DiscoveryError(f"request failed for {url}{exhausted}")  # unreachable (loop returns/raises)


def fetch_json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    """Fetch a URL and parse JSON (fetch semantics per `fetch_text`). Raises DiscoveryError
    on a non-JSON response so a single bad source never crashes a whole discovery run."""
    raw = fetch_text(url, method=method, body=body, accept="application/json")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise DiscoveryError(f"non-JSON response from {url}: {e}") from e


# Query keys that carry the job identity on some hosts (Greenhouse embeds, Lever, generic
# career pages) — kept by canonical_url; everything else in the query is tracking noise.
_QUERY_KEEP_SUBSTRINGS = ("gh_jid", "lever", "job", "id", "token")
_QUERY_DROP_KEYS = {"source", "ref", "src"}


def canonical_url(u: str) -> str:
    """Canonical form of a posting URL for dedup: lowercase scheme+host, trailing slash
    stripped from the path, fragment dropped, and the query reduced to job-identifying
    params only (keys containing gh_jid/lever/job/id/token) — utm_*, source, ref, src and
    all other tracking params are dropped. Two spellings of the same posting compare equal."""
    if not u:
        return u
    parts = urllib.parse.urlsplit(u)
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    kept = []
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_") or kl in _QUERY_DROP_KEYS:
            continue
        if any(s in kl for s in _QUERY_KEEP_SUBSTRINGS):
            kept.append((k, v))
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


class DiscoveryError(Exception):
    """A source failed to fetch/parse. Surfaced per-source; does not abort the run."""


class _TextExtractor(HTMLParser):
    """Turn posting HTML into readable plaintext: block tags become newlines, <li> becomes
    a '- ' bullet. Good enough for a JD body Claude will read (not pixel-faithful)."""

    _BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "section"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        import re

        out = "".join(self._parts)
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r"\n[ \t]+", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    p.feed(html)
    return p.text()


# ---------------------------------------------------------------------------
# Normalized posting
# ---------------------------------------------------------------------------

@dataclass
class Posting:
    """One job posting, normalized across sources. `to_job_description()` renders it into
    the fixture JD shape so the rest of the pipeline is source-agnostic."""

    company: str
    title: str
    body: str
    url: str
    ats: str  # "greenhouse" | "lever" | "ashby" | "adzuna" | ...
    location: str = ""
    compensation: str = ""
    remote: Optional[bool] = None
    apply_url: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def front_matter(self) -> dict[str, Any]:
        m: dict[str, Any] = {
            "source_url": self.url,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "ats": self.ats,
        }
        if self.compensation:
            m["compensation"] = self.compensation
        if self.remote is not None:
            m["remote"] = self.remote
        if self.apply_url:
            m["apply_url"] = self.apply_url
        m["date_captured"] = date.today().isoformat()
        return m

    def to_markdown(self) -> str:
        """The fixture on-disk format: YAML front matter + verbatim body."""
        front = yaml.safe_dump(self.front_matter(), sort_keys=False, allow_unicode=True)
        return f"---\n{front}---\n\n{self.body.strip()}\n"

    def to_job_description(self) -> JobDescription:
        """In-memory JobDescription (same object the fixtures parse to)."""
        return JobDescription(body=self.body.strip(), meta=self.front_matter(), source_path=self.url)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class Source:
    """A place postings come from. `fetch()` returns normalized postings or raises
    DiscoveryError. Subclasses set `name` for logging."""

    name = "source"

    def fetch(self) -> list[Posting]:  # pragma: no cover - interface
        raise NotImplementedError


class GreenhouseSource(Source):
    def __init__(self, token: str) -> None:
        self.token = token.strip().strip("/")
        self.name = f"greenhouse:{self.token}"

    def fetch(self) -> list[Posting]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs?content=true"
        data = fetch_json(url)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        out: list[Posting] = []
        for j in jobs:
            import html as _html

            body = html_to_text(_html.unescape(j.get("content", "") or ""))
            loc = (j.get("location") or {}).get("name", "") if isinstance(j.get("location"), dict) else ""
            out.append(
                Posting(
                    company=j.get("company_name") or self.token,
                    title=j.get("title", "").strip(),
                    body=body,
                    url=j.get("absolute_url", ""),
                    ats="greenhouse",
                    location=loc,
                    apply_url=j.get("absolute_url", ""),
                    updated_at=j.get("updated_at", ""),
                )
            )
        return out


class LeverSource(Source):
    def __init__(self, slug: str) -> None:
        self.slug = slug.strip().strip("/")
        self.name = f"lever:{self.slug}"

    def fetch(self) -> list[Posting]:
        url = f"https://api.lever.co/v0/postings/{self.slug}?mode=json"
        data = fetch_json(url)
        if not isinstance(data, list):
            raise DiscoveryError(f"unexpected Lever payload for {self.slug}: {type(data).__name__}")
        out: list[Posting] = []
        for j in data:
            cats = j.get("categories") or {}
            # Full body = opening + each list section (header + bullets) + closing.
            sections = [j.get("descriptionPlain", "") or ""]
            for lst in j.get("lists", []) or []:
                header = (lst.get("text") or "").strip()
                bullets = html_to_text(lst.get("content", "") or "")
                sections.append(f"{header}\n{bullets}".strip())
            sections.append(j.get("additionalPlain", "") or "")
            body = "\n\n".join(s for s in sections if s.strip())
            sal = j.get("salaryRange") or {}
            comp = ""
            if sal.get("min") and sal.get("max"):
                cur = sal.get("currency", "USD")
                comp = f"{sal['min']}-{sal['max']} {cur}".strip()
            wp = (j.get("workplaceType") or "").lower()
            out.append(
                Posting(
                    company=self.slug,
                    title=j.get("text", "").strip(),
                    body=body,
                    url=j.get("hostedUrl", ""),
                    ats="lever",
                    location=cats.get("location", "") or "",
                    compensation=comp,
                    remote=True if wp == "remote" else (False if wp else None),
                    apply_url=j.get("applyUrl", ""),
                    updated_at=str(j.get("createdAt", "")),
                )
            )
        return out


class AshbySource(Source):
    def __init__(self, org: str) -> None:
        self.org = org.strip().strip("/")
        self.name = f"ashby:{self.org}"

    def fetch(self) -> list[Posting]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{self.org}?includeCompensation=true"
        data = fetch_json(url)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        out: list[Posting] = []
        for j in jobs:
            if j.get("isListed") is False:
                continue
            comp = ((j.get("compensation") or {}).get("compensationTierSummary") or "") if isinstance(
                j.get("compensation"), dict
            ) else ""
            body = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "") or "")
            out.append(
                Posting(
                    company=self.org,
                    title=j.get("title", "").strip(),
                    body=body,
                    url=j.get("jobUrl", ""),
                    ats="ashby",
                    location=j.get("location", "") or "",
                    compensation=comp,
                    remote=j.get("isRemote"),
                    apply_url=j.get("applyUrl", ""),
                    updated_at=j.get("publishedAt", ""),
                )
            )
        return out


class AdzunaSource(Source):
    """Broad aggregator (many boards, ~19 countries). Free but needs a free app_id/app_key
    (developer.adzuna.com). Unlike the ATS sources this is a *search* — queried with keywords
    derived from the profile — and returns a **snippet**, not the full JD (`redirect_url`
    links to the full posting). Snippet is enough for the keyword pre-filter; the Claude
    judge/tailor work on the snippet (degraded vs. the full-text ATS sources). Skipped
    gracefully when no key is configured."""

    def __init__(
        self,
        app_id: str,
        app_key: str,
        *,
        what: str = "",
        whats: list[str] | None = None,
        where: str = "",
        country: str = "us",
        results_per_page: int = 50,
        max_pages: int = 1,
        salary_min: int = 0,
        max_days_old: int = 0,
        sort_by: str = "",
    ) -> None:
        self.app_id = app_id
        self.app_key = app_key
        # Run each query as its OWN focused `what` search (e.g. "Backend Engineer", "Python") and
        # merge+dedup, instead of one broad `what_or` blob — each query returns results ranked for
        # that term, widening real coverage of the candidate's profile. `whats` (the focused list)
        # takes precedence; `what` stays for single-query back-compat.
        self.whats = [q for q in (whats if whats is not None else [what]) if q]
        self.where = where
        self.country = country.lower()
        self.results_per_page = results_per_page
        self.max_pages = max_pages
        self.salary_min = salary_min
        self.max_days_old = max_days_old  # server-side recency gate (Adzuna `max_days_old`); 0 = off
        self.sort_by = sort_by  # e.g. "date" for freshest-first when paginating; "" = Adzuna default
        self.name = f"adzuna:{country}:{','.join(self.whats)[:40]}"

    def fetch(self) -> list[Posting]:
        if not (self.app_id and self.app_key):
            raise DiscoveryError("Adzuna not configured (missing app_id/app_key)")
        from urllib.parse import urlencode

        out: list[Posting] = []
        seen: set[str] = set()  # dedup across queries (the same job can match several terms)
        for query in (self.whats or [""]):
            for page in range(1, self.max_pages + 1):
                params = {
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    "results_per_page": self.results_per_page,
                    "content-type": "application/json",
                }
                if query:
                    params["what"] = query
                if self.where:
                    params["where"] = self.where
                if self.salary_min:
                    params["salary_min"] = self.salary_min
                if self.max_days_old:
                    params["max_days_old"] = self.max_days_old
                if self.sort_by:
                    params["sort_by"] = self.sort_by
                url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/{page}?{urlencode(params)}"
                data = fetch_json(url)
                results = data.get("results", []) if isinstance(data, dict) else []
                for j in results:
                    redirect = j.get("redirect_url", "")
                    if redirect and redirect in seen:
                        continue
                    seen.add(redirect)
                    comp = ""
                    lo, hi = j.get("salary_min"), j.get("salary_max")
                    if lo and hi:
                        comp = f"{int(lo):,}-{int(hi):,}"
                    elif lo:
                        comp = f"from {int(lo):,}"
                    out.append(
                        Posting(
                            company=(j.get("company") or {}).get("display_name", "") or "Unknown",
                            title=j.get("title", "").strip(),
                            body=html_to_text(j.get("description", "") or ""),
                            url=redirect,
                            ats="adzuna",
                            location=(j.get("location") or {}).get("display_name", "") or "",
                            compensation=comp,
                            remote=None,
                            apply_url=redirect,
                            updated_at=j.get("created", ""),
                            extra={"snippet_only": True},
                        )
                    )
                if len(results) < self.results_per_page:
                    break  # last page for this query
        return out


class GoogleJobsSource(Source):
    """Broad aggregator via the Google Jobs vertical (google_jobs.py, vendored from JobSpy, MIT).
    Like Adzuna it's a *search* — driven by profile-derived queries — but keyless: no app_id, no
    scraping-evasion stack (proxy-free, one honest UA, backs off on 429). Runs each of `whats` as
    its own focused Google query and merges+dedups. Tagged `ats="google"` so it flows through the
    aggregator bridge → the bridge resolves each hit to its real ATS (Greenhouse/Lever/…) for a
    fillable apply, and marks the unresolved ones the same as any other aggregator hit."""

    def __init__(
        self,
        whats: list[str],
        *,
        location: str = "",
        is_remote: bool = False,
        max_days_old: int = 0,
        results_wanted: int = 40,
    ) -> None:
        self.whats = [q for q in whats if q]
        self.location = location
        self.is_remote = is_remote
        self.hours_old = max_days_old * 24 if max_days_old else 0
        self.results_wanted = results_wanted
        self.name = f"google:{','.join(self.whats)[:40]}"

    def fetch(self) -> list[Posting]:
        from . import google_jobs

        out: list[Posting] = []
        seen: set[str] = set()
        errors: list[str] = []
        for query in self.whats:
            try:
                rows = google_jobs.search(
                    query, location=self.location, is_remote=self.is_remote,
                    hours_old=self.hours_old, results_wanted=self.results_wanted,
                )
            except google_jobs.GoogleJobsError as e:
                errors.append(str(e))
                continue
            for r in rows:
                url = r.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                out.append(
                    Posting(
                        company=r.get("company", "") or "Unknown",
                        title=r.get("title", "").strip(),
                        body=r.get("description", "") or "",
                        url=url,
                        ats="google",
                        location=r.get("location", "") or "",
                        compensation="",
                        remote=r.get("is_remote"),
                        apply_url=url,
                        updated_at=r.get("date_posted") or "",
                        extra={"snippet_only": True},
                    )
                )
        # If every query failed (e.g. Google rate-limited the whole run), surface it rather than a
        # silent empty result (Agent Guideline #11) — discover() records source errors.
        if errors and not out:
            raise DiscoveryError("; ".join(errors[:2]))
        return out


def _annualize(amount, period: str) -> Optional[int]:
    """Best-effort annualize a pay figure so the salary gate compares like-for-like (a monthly
    2000-4000 must not read as a 4000/yr salary). Returns None if unparseable."""
    try:
        n = int(float(amount))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    mult = {"hourly": 2080, "weekly": 52, "monthly": 12, "yearly": 1, "annually": 1}.get(
        (period or "").strip().lower(), 1
    )
    return n * mult


class HimalayasSource(Source):
    """Keyless remote-job aggregator (himalayas.app) — public JSON, no signup. Remote by
    construction. Full description + structured salary/seniority; `applicationLink` is the apply
    URL. Tagged `ats="himalayas"` so it rides the aggregator bridge/fillability path like Adzuna."""

    def __init__(self, max_results: int = 100) -> None:
        self.max_results = max_results
        self.name = "himalayas"

    def fetch(self) -> list[Posting]:
        out: list[Posting] = []
        page_size = 50
        offset = 0
        while len(out) < self.max_results:
            data = fetch_json(f"https://himalayas.app/jobs/api?limit={page_size}&offset={offset}")
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            if not jobs:
                break
            for j in jobs:
                lo = _annualize(j.get("minSalary"), j.get("salaryPeriod"))
                hi = _annualize(j.get("maxSalary"), j.get("salaryPeriod"))
                cur = j.get("currency") or ""
                comp = f"{cur} {lo:,}-{hi:,}".strip() if lo and hi else (f"{cur} from {lo:,}".strip() if lo else "")
                locs = j.get("locationRestrictions") or []
                out.append(
                    Posting(
                        company=j.get("companyName", "") or "Unknown",
                        title=(j.get("title", "") or "").strip(),
                        body=html_to_text(j.get("description", "") or "") or (j.get("excerpt", "") or ""),
                        url=j.get("applicationLink", "") or "",
                        ats="himalayas",
                        location=", ".join(locs) if locs else "Remote",
                        compensation=comp,
                        remote=True,
                        apply_url=j.get("applicationLink", "") or "",
                        updated_at=j.get("pubDate", "") or "",
                        extra={"snippet_only": True},
                    )
                )
            offset += page_size
            if len(jobs) < page_size:
                break
        return out[: self.max_results]


class RemoteOKSource(Source):
    """Keyless remote-job aggregator (remoteok.com) — public JSON list, no signup. Filter by
    `?tag=` (profile-derived single-word skill tags); results merged + deduped. Element 0 of the
    response is a legal/attribution notice (skipped). Their terms require a backlink, so the
    posting URL is the remoteok.com listing (which is that backlink). Tagged `ats="remoteok"`."""

    def __init__(self, tags: list[str] | None = None, max_results: int = 100) -> None:
        self.tags = [t for t in (tags or []) if t] or [""]  # [""] = one tagless "recent" query
        self.max_results = max_results
        self.name = "remoteok"

    def fetch(self) -> list[Posting]:
        from urllib.parse import quote

        out: list[Posting] = []
        seen: set = set()
        for tag in self.tags:
            url = "https://remoteok.com/api" + (f"?tag={quote(tag)}" if tag else "")
            data = fetch_json(url)
            if not isinstance(data, list):
                continue
            for j in data:
                if not isinstance(j, dict) or "position" not in j:
                    continue  # element 0 (legal notice) and any malformed entries
                jid = j.get("id") or j.get("slug")
                if jid in seen:
                    continue
                seen.add(jid)
                lo, hi = j.get("salary_min"), j.get("salary_max")
                comp = ""
                try:
                    lo, hi = int(lo or 0), int(hi or 0)
                    if lo and hi:
                        comp = f"{lo:,}-{hi:,}"
                    elif lo:
                        comp = f"from {lo:,}"
                except (TypeError, ValueError):
                    pass
                out.append(
                    Posting(
                        company=j.get("company", "") or "Unknown",
                        title=(j.get("position", "") or "").strip(),
                        body=html_to_text(j.get("description", "") or ""),
                        url=j.get("url", "") or j.get("apply_url", ""),  # remoteok listing = required backlink
                        ats="remoteok",
                        location=j.get("location", "") or "Remote",
                        compensation=comp,
                        remote=True,
                        apply_url=j.get("apply_url", "") or j.get("url", ""),
                        updated_at=j.get("date", "") or "",
                        extra={"snippet_only": True, "tags": j.get("tags")},
                    )
                )
                if len(out) >= self.max_results:
                    return out
        return out


class SmartRecruitersSource(Source):
    """Public, no-auth SmartRecruiters postings API — a DIFFERENT ATS/form system than
    Greenhouse/Lever/Ashby, so the Apply stage gets exercised on new forms. The list
    endpoint omits the JD body, so we fetch each posting's detail for the full description
    plus the real jobs.smartrecruiters.com apply URL. Bounded by `_DETAIL_MAX_POSTINGS`
    (an N+1 of one detail request per kept posting) to stay polite.

        list   : GET api.smartrecruiters.com/v1/companies/{company}/postings?limit&offset
        detail : GET api.smartrecruiters.com/v1/companies/{company}/postings/{id}
    """

    # jobAd sections in the order SmartRecruiters presents them; unknown ones append after.
    _SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")

    def __init__(self, company: str) -> None:
        self.company = company.strip().strip("/")
        self.name = f"smartrecruiters:{self.company}"

    def fetch(self) -> list[Posting]:
        base = f"https://api.smartrecruiters.com/v1/companies/{self.company}/postings"
        listed: list[dict] = []
        offset, limit = 0, 100
        while len(listed) < _DETAIL_MAX_POSTINGS:
            data = fetch_json(f"{base}?limit={limit}&offset={offset}")
            content = data.get("content", []) if isinstance(data, dict) else []
            if not content:
                break
            listed.extend(content)
            if len(content) < limit:
                break
            offset += limit
        out: list[Posting] = []
        for p in listed[:_DETAIL_MAX_POSTINGS]:
            pid = p.get("id")
            if not pid:
                continue
            try:
                det = fetch_json(f"{base}/{pid}")  # full JD + real apply URL
            except DiscoveryError:
                det = p  # detail failed — keep the list entry (title/location, no body)
            out.append(self._to_posting(det))
        return out

    def _to_posting(self, j: dict) -> Posting:
        loc = j.get("location") or {}
        loc_str = loc.get("fullLocation") or ", ".join(
            x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x
        )
        remote = loc.get("remote")
        sections = ((j.get("jobAd") or {}).get("sections")) or {}
        apply_url = j.get("applyUrl") or j.get("postingUrl") or ""
        return Posting(
            company=(j.get("company") or {}).get("name") or self.company,
            title=(j.get("name") or "").strip(),
            body=self._body_from_sections(sections),
            url=j.get("postingUrl") or apply_url,
            ats="smartrecruiters",
            location=loc_str,
            remote=remote if isinstance(remote, bool) else None,
            apply_url=apply_url,
            updated_at=str(j.get("releasedDate", "")),
        )

    def _body_from_sections(self, sections: dict) -> str:
        keys = list(self._SECTION_ORDER) + [k for k in sections if k not in self._SECTION_ORDER]
        parts: list[str] = []
        for key in keys:
            sec = sections.get(key)
            if not isinstance(sec, dict):
                continue
            title = (sec.get("title") or "").strip()
            text = html_to_text(sec.get("text", "") or "")
            if text:
                parts.append(f"{title}\n{text}".strip() if title else text)
        return "\n\n".join(parts)


class RecruiteeSource(Source):
    """Public, no-auth Recruitee careers API — another distinct ATS/form system. One call
    returns every published offer with the full JD inline (description + requirements) and
    the direct careers apply URL.

        GET https://{company}.recruitee.com/api/offers/
    """

    def __init__(self, company: str) -> None:
        self.company = company.strip().strip("/")
        self.name = f"recruitee:{self.company}"

    def fetch(self) -> list[Posting]:
        data = fetch_json(f"https://{self.company}.recruitee.com/api/offers/")
        offers = data.get("offers", []) if isinstance(data, dict) else []
        out: list[Posting] = []
        for o in offers:
            status = o.get("status")
            if status and status != "published":
                continue
            desc = html_to_text(o.get("description", "") or "")
            reqs = html_to_text(o.get("requirements", "") or "")
            body = "\n\n".join(x for x in [desc, reqs] if x)
            apply_url = o.get("careers_apply_url") or o.get("careers_url") or ""
            comp = o.get("salary") if isinstance(o.get("salary"), str) else ""
            out.append(
                Posting(
                    company=o.get("company_name") or self.company,
                    title=(o.get("title") or o.get("position") or "").strip(),
                    body=body,
                    url=o.get("careers_url") or apply_url,
                    ats="recruitee",
                    location=o.get("location") or ", ".join(
                        x for x in [o.get("city"), o.get("country")] if x
                    ),
                    compensation=comp,
                    remote=True if o.get("remote") else (False if o.get("on_site") else None),
                    apply_url=apply_url,
                    updated_at=str(o.get("published_at") or o.get("created_at") or ""),
                )
            )
        return out


class WorkableSource(Source):
    """Public, no-auth Workable careers API — another distinct ATS/form system. The list
    endpoint (POST, token-paginated 10 at a time) omits the JD body, so we fetch each
    posting's detail for the full description; bounded by `_DETAIL_MAX_POSTINGS` (an N+1).
    `account` is the slug in the careers URL apply.workable.com/{account}/.

        list   : POST apply.workable.com/api/v3/accounts/{account}/jobs  {} (+ {"token": nextPage})
        detail : GET  apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}   (note: v2)
    """

    def __init__(self, account: str) -> None:
        self.account = account.strip().strip("/")
        self.name = f"workable:{self.account}"

    def fetch(self) -> list[Posting]:
        list_url = f"https://apply.workable.com/api/v3/accounts/{self.account}/jobs"
        listed: list[dict] = []
        token: Optional[str] = None
        while len(listed) < _DETAIL_MAX_POSTINGS:
            data = fetch_json(list_url, method="POST", body=({"token": token} if token else {}))
            results = data.get("results", []) if isinstance(data, dict) else []
            if not results:
                break
            listed.extend(results)
            token = data.get("nextPage") if isinstance(data, dict) else None
            if not token:
                break
        out: list[Posting] = []
        for p in listed[:_DETAIL_MAX_POSTINGS]:
            sc = p.get("shortcode")
            if not sc:
                continue
            try:
                det = fetch_json(  # v2 detail carries the full JD (description/requirements/benefits)
                    f"https://apply.workable.com/api/v2/accounts/{self.account}/jobs/{sc}"
                )
            except DiscoveryError:
                det = p  # detail failed — keep the list entry (title/location, no body)
            out.append(self._to_posting(det, sc))
        return out

    def _to_posting(self, j: dict, shortcode: str) -> Posting:
        loc = j.get("location") or {}
        loc_str = ", ".join(
            x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
        )
        workplace = (j.get("workplace") or "").lower()
        remote = True if (j.get("remote") or workplace == "remote") else (False if workplace else None)
        body = "\n\n".join(
            html_to_text(j.get(k, "") or "")
            for k in ("description", "requirements", "benefits")
            if j.get(k)
        )
        apply_url = f"https://apply.workable.com/{self.account}/j/{shortcode}/"
        return Posting(
            company=self.account,
            title=(j.get("title") or "").strip(),
            body=body,
            url=apply_url,
            ats="workable",
            location=loc_str,
            remote=remote,
            apply_url=apply_url,
            updated_at=str(j.get("published", "")),
        )


class CareerSiteSource(Source):
    """Discover from career pages that publish schema.org `JobPosting` structured data —
    the enrichment cascade (DECISIONS.md #047, adapted from ApplyPilot): JSON-LD first, then
    CSS/DOM, then an optional Claude fallback. Give it career/posting URLs; each page is
    fetched once and every JobPosting on it becomes a Posting (a listing page may yield
    several; a single posting page yields one). Reads only published structured data
    (Agent Guideline #4). No link-crawling in v1 — point it at posting pages or listing pages
    that embed JobPosting JSON-LD. `llm` (optional) turns on the tier-3 Claude fallback for
    pages with neither JSON-LD nor a recognizable description block; off by default (free,
    offline). `stats` counts which tier resolved each page for a "% saved" log line."""

    def __init__(self, urls: list[str], *, llm=None) -> None:
        self.urls = [u.strip() for u in urls if u and u.strip()]
        self.llm = llm
        self.name = f"career-sites:{len(self.urls)}"
        self.stats: dict[str, int] = {"json-ld": 0, "css": 0, "llm": 0, "empty": 0}

    def fetch(self) -> list[Posting]:
        from . import enrich

        out: list[Posting] = []
        for url in self.urls:
            try:
                html = fetch_text(url, accept="text/html,application/xhtml+xml,*/*")
            except DiscoveryError:
                self.stats["empty"] += 1
                continue
            results = [enrich.jobposting_to_result(n) for n in enrich.extract_jobpostings_from_jsonld(html)]
            results = [r for r in results if r.ok]
            if not results:  # no JSON-LD JobPosting → single-result CSS/LLM cascade for the page
                single = enrich.enrich_from_html(html, url=url, llm=self.llm)
                results = [single] if single.ok else []
            if not results:
                self.stats["empty"] += 1
                continue
            for r in results:
                self.stats[r.tier] = self.stats.get(r.tier, 0) + 1
                apply_url = r.apply_url or url
                out.append(Posting(
                    company=r.company,
                    title=r.title,
                    body=r.description,
                    url=apply_url,
                    ats=detect_ats_from_url(apply_url),
                    location=r.location,
                    compensation=r.compensation,
                    remote=r.remote,
                    apply_url=apply_url,
                    updated_at=r.date_posted,
                    extra={"enriched": r.tier},
                ))
        return out


# --------------------------------------------------------------------------- curated feeds
#
# Community-maintained, daily-updated JSON lists of EARLY-CAREER roles (new-grad + internships)
# — early-career by construction, so no senior roles to filter out (DECISIONS.md #031). The
# lists are URL-only (a title + an application link, no JD text), and ~63% of active links point
# at an ATS we can both fetch a full JD from AND fill (`_CURATED_ATS`). We rank listings by
# title-relevance to the résumé, resolve the FULL JD for the top-K via the linked ATS, and emit
# normal full-JD Postings so the matcher/apply pipeline is unchanged. Personal-use only (public
# job links; the lists carry no explicit redistribution license).
#
# NOTE (measured 2026-07-15, DECISIONS.md #073): the built-in feeds carry ~3.4k active postings,
# ~2.2k of them in `_CURATED_ATS` — but `max_resolve` (default 40) is what actually caps a run.
# Adding feeds widens a pool that is already ~50x oversubscribed; `max_resolve` is the real knob.

_BUILTIN_FEEDS = {
    "new-grad": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "intern": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
}
# Any GitHub repo publishing the SimplifyJobs `listings.json` schema can be dropped in via
# `early_career.feeds` with no code change (DECISIONS.md #073) — that schema is the de-facto
# standard for these boards, and every field below is read with a `.get()` default, so feeds
# carrying only a subset (e.g. no `category`/`degrees`) still work.
_FEED_REQUIRED_KEYS = ("title", "url", "company_name", "active")

# ATSs a curated listing may point at and still flow all the way through: we can resolve a full
# JD from the URL AND fill the form in Apply. greenhouse/lever/ashby/smartrecruiters resolve via
# their public JSON APIs; workday resolves via the shared enrichment cascade (DECISIONS.md #074).
_CURATED_ATS = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters")
# Tokens that signal LEVEL not role — excluded from title-relevance so "intern" alone doesn't
# rank a "Marketing Intern" as relevant to a software résumé.
_LEVEL_TOKENS = {"intern", "internship", "junior", "senior", "staff", "lead", "i", "ii", "iii",
                 "co", "op", "new", "grad", "graduate", "entry", "level", "of", "the", "and", "a"}
_WORD_RE = re.compile(r"[a-z0-9+#.]+")


def detect_ats_from_url(url: str) -> str:
    """Identify the ATS from an application URL (routes curated-list links)."""
    u = (url or "").lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    if "recruitee.com" in u:
        return "recruitee"
    if "workable.com" in u:
        return "workable"
    if "myworkdayjobs.com" in u or "workday" in u:
        return "workday"
    return "other"


def _resolve_greenhouse_jd(url: str) -> str:
    m = re.search(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)", url)
    if not m:
        return ""
    import html as _html

    token, job_id = m.group(1), m.group(2)
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")
    return html_to_text(_html.unescape((data or {}).get("content", "") or ""))


def _resolve_lever_jd(url: str) -> str:
    m = re.search(r"lever\.co/([^/?#]+)/([0-9a-f-]{8,})", url)
    if not m:
        return ""
    j = fetch_json(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
    if not isinstance(j, dict):
        return ""
    sections = [j.get("descriptionPlain", "") or ""]
    for lst in j.get("lists", []) or []:
        sections.append(f"{(lst.get('text') or '').strip()}\n{html_to_text(lst.get('content', '') or '')}".strip())
    sections.append(j.get("additionalPlain", "") or "")
    return "\n\n".join(s for s in sections if s.strip())


def _resolve_ashby_jd(url: str, cache: dict) -> str:
    m = re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-f-]{8,})", url)
    if not m:
        return ""
    org, uuid = m.group(1), m.group(2)
    if org not in cache:  # Ashby has no per-job public endpoint — fetch the board once, index it
        try:
            data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
            cache[org] = {j.get("jobUrl", ""): j for j in (data.get("jobs", []) if isinstance(data, dict) else [])}
        except DiscoveryError:
            cache[org] = {}
    j = next((v for k, v in cache[org].items() if uuid in k), None)
    if not j:
        return ""
    return j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "") or "")


def _resolve_smartrecruiters_jd(url: str) -> str:
    m = re.search(r"smartrecruiters\.com/([^/?#]+)/(\d+)", url)
    if not m:
        return ""
    company, pid = m.group(1), m.group(2)
    det = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{pid}")
    sections = ((det.get("jobAd") or {}).get("sections")) or {} if isinstance(det, dict) else {}
    return SmartRecruitersSource(company)._body_from_sections(sections)


def _resolve_workable_jd(url: str) -> str:
    m = re.search(r"apply\.workable\.com/([^/?#]+)/j/([^/?#]+)", url) or re.search(
        r"([^/.]+)\.workable\.com/j/([^/?#]+)", url
    )
    if not m:
        return ""
    account, sc = m.group(1).strip("/"), m.group(2).strip("/")
    det = fetch_json(f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{sc}")
    if not isinstance(det, dict):
        return ""
    return "\n\n".join(
        html_to_text(det.get(k, "") or "")
        for k in ("description", "requirements", "benefits")
        if det.get(k)
    )


def _resolve_recruitee_jd(url: str) -> str:
    m = re.search(r"([^/.]+)\.recruitee\.com/o/([^/?#]+)", url)
    if not m:
        return ""
    company, slug = m.group(1), m.group(2)
    data = fetch_json(f"https://{company}.recruitee.com/api/offers/{slug}")
    o = data.get("offer") if isinstance(data, dict) else None
    if not isinstance(o, dict):
        return ""
    desc = html_to_text(o.get("description", "") or "")
    reqs = html_to_text(o.get("requirements", "") or "")
    return "\n\n".join(x for x in [desc, reqs] if x)


def _resolve_workday_jd(url: str) -> str:
    """Workday renders the visible page client-side but still ships schema.org JSON-LD in the
    initial HTML, so the shared enrichment cascade resolves it with a plain GET — no browser and
    no LLM call (no `llm=`, so it stops at the free JSON-LD/CSS tiers)."""
    from . import enrich  # local: enrich imports from this module

    return enrich.fetch_full_jd(url).description


def _resolve_jd(url: str, ats: str, ashby_cache: dict) -> str:
    """Fetch one job's full JD via its ATS. Returns '' on any failure (the caller keeps a
    title-only body so the posting still flows through, just judged on less)."""
    try:
        if ats == "greenhouse":
            return _resolve_greenhouse_jd(url)
        if ats == "lever":
            return _resolve_lever_jd(url)
        if ats == "ashby":
            return _resolve_ashby_jd(url, ashby_cache)
        if ats == "workday":
            return _resolve_workday_jd(url)
        if ats == "smartrecruiters":
            return _resolve_smartrecruiters_jd(url)
        if ats == "workable":
            return _resolve_workable_jd(url)
        if ats == "recruitee":
            return _resolve_recruitee_jd(url)
    except Exception:
        return ""
    return ""


def _title_relevance(resume, title: str, category: str = "") -> int:
    """Cheap score of how relevant a listing's TITLE is to the candidate — used to pick which
    URL-only listings to resolve+judge. Overlap of title/category tokens with the résumé's role
    words (weighted) + skills, excluding generic level words so 'intern' alone isn't a match."""
    toks = {t for t in _WORD_RE.findall(f"{title} {category}".lower()) if t not in _LEVEL_TOKENS}
    role_words: set[str] = set()
    for exp in resume.experience:
        role_words |= {t for t in _WORD_RE.findall((exp.role or "").lower()) if t not in _LEVEL_TOKENS}
    skills = {it.lower() for cat in resume.skills for it in cat.items}
    return len(toks & role_words) * 2 + len(toks & skills)


class CuratedListSource(Source):
    """Early-career discovery from GitHub job-board JSON feeds (DECISIONS.md #031, #073). Ships
    with the SimplifyJobs new-grad/internship lists; any repo publishing the same `listings.json`
    schema can be added via `feeds` with no code change. Keeps `active` roles whose apply link is
    a resolvable+fillable ATS (`_CURATED_ATS`), ranks them by title-relevance to the résumé across
    ALL feeds jointly, resolves the full JD for the top `max_resolve`, and emits full-JD Postings.
    Personal-use only (public job links)."""

    def __init__(self, resume, kinds=("new-grad", "intern"), max_resolve: int = 40,
                 feeds: dict[str, str] | None = None, filter_spam: bool = True,
                 title_exclude: list[str] | None = None,
                 company_exclude: list[str] | None = None) -> None:
        self.resume = resume
        self.kinds = tuple(kinds)
        self.max_resolve = max_resolve
        self.filter_spam = filter_spam
        self.title_exclude = [t.lower() for t in (title_exclude or [])]
        self.company_exclude = [c.lower() for c in (company_exclude or [])]
        # Built-ins named by `kinds`, plus any dropped-in {name: url}. Ranking is global across
        # the merged set, so max_resolve stays a whole-run budget however many feeds are added.
        self.feeds = {k: _BUILTIN_FEEDS[k] for k in self.kinds if k in _BUILTIN_FEEDS}
        self.feeds.update(feeds or {})
        self.name = "curated:" + ",".join(sorted(self.feeds))

    def _listings(self, name: str, url: str) -> list:
        """Fetch one feed and validate it looks like a listings.json board. Raises DiscoveryError
        naming the feed and the fix — a dropped-in URL that 404s or points at the wrong file is a
        config error the user must see, not something to silently skip (Agent Guideline #11)."""
        data = fetch_json(url)
        if not isinstance(data, list):
            raise DiscoveryError(
                f"feed '{name}' ({url}) returned {type(data).__name__}, expected a JSON array of "
                f"listings. Check the URL points at a raw listings.json (raw.githubusercontent.com"
                f"/<owner>/<repo>/<branch>/.github/scripts/listings.json), not the repo page."
            )
        first = next((e for e in data if isinstance(e, dict)), None)
        missing = [k for k in _FEED_REQUIRED_KEYS if first is not None and k not in first]
        if first is None or missing:
            raise DiscoveryError(
                f"feed '{name}' ({url}) is not a SimplifyJobs-schema job board"
                + (f" — entries are missing {', '.join(missing)}." if missing else " — it is empty.")
                + f" Required keys: {', '.join(_FEED_REQUIRED_KEYS)}. Remove it from"
                f" early_career.feeds in profile/discovery.yaml, or point it at a compatible feed."
            )
        return data

    def fetch(self) -> list[Posting]:
        import html as _html

        # lazy imports: filters imports discovery (avoid an import cycle)
        from .filters import _norm_title, is_staffing_spam

        def clean(s: str) -> str:
            return _html.unescape(s or "").strip()

        seen: set[str] = set()
        seen_key: set[tuple[str, str]] = set()  # (company, norm-title) — collapse reposts
        listings: list[tuple[dict, str]] = []
        for name, feed in self.feeds.items():
            for e in self._listings(name, feed):
                if not isinstance(e, dict) or not e.get("active"):
                    continue
                url = e.get("url", "")
                ats = detect_ats_from_url(url)
                if ats not in _CURATED_ATS or not url or url in seen:
                    continue
                # Apply the cheap title/company/spam gates + repost dedup HERE — BEFORE the
                # title-relevance sort below — so a staffing repost (keyword-stuffed OR a clean-
                # titled Consultadd/DellFor dupe) never wins a max_resolve JD-fetch slot over a
                # real role (decision 122 open item). apply_gates repeats these for other sources.
                title = clean(e.get("title", ""))
                company = clean(e.get("company_name", ""))
                tl, cl = title.lower(), company.lower()
                if self.title_exclude and any(x in tl for x in self.title_exclude):
                    continue
                if self.company_exclude and any(x in cl for x in self.company_exclude):
                    continue
                if self.filter_spam and is_staffing_spam(title):
                    continue
                key = (cl.strip(), _norm_title(title))
                if key != ("", "") and key in seen_key:
                    continue
                seen.add(url)
                seen_key.add(key)
                listings.append((e, ats))

        listings.sort(
            key=lambda ea: _title_relevance(self.resume, clean(ea[0].get("title", "")),
                                            clean(ea[0].get("category", ""))),
            reverse=True,
        )
        ashby_cache: dict = {}
        out: list[Posting] = []
        for e, ats in listings[: self.max_resolve]:
            title = clean(e.get("title", ""))
            url = e.get("url", "")
            locs = e.get("locations") or []
            location = ", ".join(str(x) for x in locs[:2]) if isinstance(locs, list) else str(locs)
            body = _resolve_jd(url, ats, ashby_cache)
            if not body:  # resolution failed — keep a minimal body so it still flows (degraded)
                body = (f"{title}. Early-career role. Category: {clean(e.get('category', ''))}. "
                        f"Degrees: {', '.join(e.get('degrees') or [])}.")
            out.append(Posting(
                company=clean(e.get("company_name", "")) or ats,
                title=title, body=body, url=url, ats=ats,
                location=location, apply_url=url,
                extra={"curated": True, "sponsorship": e.get("sponsorship"),
                       "category": clean(e.get("category", ""))},
            ))
        return out


# Map an ATS name to its source constructor, for building sources from config.
ATS_SOURCES = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "smartrecruiters": SmartRecruitersSource,
    "recruitee": RecruiteeSource,
    "workable": WorkableSource,
}


def build_source(ats: str, token: str) -> Source:
    ats = ats.strip().lower()
    if ats not in ATS_SOURCES:
        raise DiscoveryError(f"unknown ATS '{ats}'. Known: {', '.join(ATS_SOURCES)}")
    return ATS_SOURCES[ats](token)


def discover(sources: list[Source]) -> tuple[list[Posting], list[str]]:
    """Fan out across sources, collect postings, dedup by canonical URL (so the same job
    reached via two URL spellings survives only once). Returns (postings, errors);
    a failing source is recorded as an error string, never aborting the run."""
    seen: set[str] = set()
    postings: list[Posting] = []
    errors: list[str] = []
    for src in sources:
        try:
            for p in src.fetch():
                key = canonical_url(p.url) if p.url else f"{p.company}|{p.title}"
                if key in seen:
                    continue
                seen.add(key)
                postings.append(p)
        except DiscoveryError as e:
            errors.append(f"{src.name}: {e}")
        except Exception as e:  # defensive: a malformed field shouldn't kill the run
            errors.append(f"{src.name}: unexpected {type(e).__name__}: {e}")
    return postings, errors


# ---------------------------------------------------------------------------
# Aggregator → ATS bridge (DECISIONS.md #032)
#
# Aggregators (Adzuna/Jooble) return a snippet + an apply link that redirects through their
# OWN domain, so you can't tell the real ATS from the API response. This bridge follows that
# redirect, and when it lands on an ATS the Apply stage can fill, rewrites the posting's
# `ats` + `apply_url` so the aggregator hit flows straight into auto-apply — and, for the ATSs
# with a public JD API, upgrades the snippet body to the full JD via the existing resolvers.
# ---------------------------------------------------------------------------

# Aggregators whose apply links redirect through their own domain (need resolving to find the ATS).
_AGGREGATOR_ATS = {"adzuna", "jooble", "google", "himalayas", "remoteok"}
# Aggregators whose apply link sits behind a BROWSER-ONLY gate we can't resolve server-side, so
# the real ATS is unknowable at bridge time. Adzuna's www.adzuna.com/land/ page 403s every
# non-browser client (CloudFront WAF — even real Chromium from a blocked IP) AND its "Apply for
# this job" button is a real navigation, not an HTTP 30x. Rather than hammer a blocked endpoint
# 60x/run, these are deferred to Apply: the real Chromium browser opens the land URL and
# `apply._open_application_form` clicks the "Apply for this job" control (already matched by its
# `\bapply\b` reveal regex), then re-derives the true ATS from the resulting form frame.
# (DECISIONS.md #120.)
_BROWSER_GATED_ATS = {"adzuna"}
_BRIDGE_MAX = 60  # cap redirect resolutions per run (one network call each) — polite + bounded


def resolve_redirect(url: str) -> str:
    """Follow HTTP redirects to the final destination URL. Aggregators link through their own
    domain; this returns the real posting URL so `detect_ats_from_url` can classify it. Tries a
    cheap HEAD, falls back to GET, and returns the original URL on any failure. urllib follows
    the 30x chain — JS/meta-refresh interstitials (some Jooble pages) are not followed."""
    if not url:
        return url
    host = urllib.parse.urlsplit(url).netloc
    for method in ("HEAD", "GET"):
        try:
            _polite_wait(host)
            req = urllib.request.Request(url, method=method, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_SSL_CTX) as r:
                return r.geturl()
        except Exception:
            continue
    return url


def bridge_aggregator_postings(postings, *, limit: int = _BRIDGE_MAX, upgrade_jd: bool = True, on_progress=None):
    """Turn aggregator hits into auto-apply candidates. For each posting whose `ats` is an
    aggregator, resolve its redirect and — when it lands on a recognized ATS — rewrite `ats`
    + `apply_url` so it flows into Apply, recording the original ats in `extra['bridged_from']`
    and whether we have a dedicated adapter in `extra['auto_applyable']`. When `upgrade_jd` and
    the ATS exposes a public JD API (Greenhouse/Lever/Ashby/SmartRecruiters/Workable/Recruitee),
    replace the aggregator's *snippet* body with the full JD. Left untouched otherwise. Mutates and
    returns (postings, n_bridged); bounded by `limit` redirect resolutions to stay polite.

    Browser-gated aggregators (`_BROWSER_GATED_ATS`, e.g. Adzuna) are NOT resolved here — their
    gate 403s any server-side client — but are left flowing to Apply (ats/apply_url unchanged,
    tagged `extra['browser_gated']`) so the real browser clicks through the gate at apply time.
    They make no network call, so they don't consume the `limit` resolution budget."""
    for p in (p for p in postings if p.ats in _BROWSER_GATED_ATS):
        p.extra["bridged_from"] = p.ats
        p.extra["browser_gated"] = True  # Apply resolves the real ATS by clicking the gate in-browser
        # `auto_applyable` left unset on purpose: we have no dedicated adapter (the ATS is unknown
        # until apply time), but `_is_fillable` keeps it in the funnel via `ats in _AGGREGATOR_ATS`.
    to_bridge = [p for p in postings if p.ats in _AGGREGATOR_ATS and p.ats not in _BROWSER_GATED_ATS]
    ashby_cache: dict = {}
    bridged = 0
    for i, p in enumerate(to_bridge[:limit]):
        final = resolve_redirect(p.apply_url or p.url)
        ats = detect_ats_from_url(final)
        if ats not in ("other", ""):
            p.extra["bridged_from"] = p.ats
            # Do we have a dedicated Apply adapter? The six public-API ATSs, plus Workday
            # (deterministic adapter, decision 059 — M1 dry-run).
            p.extra["auto_applyable"] = ats in ATS_SOURCES or ats == "workday"
            p.ats = ats
            p.apply_url = final
            if upgrade_jd:
                full = _resolve_jd(final, ats, ashby_cache)  # full JD for GH/Lever/Ashby, else ''
                if full and len(full) > len(p.body):
                    p.body = full
                    p.extra["jd_upgraded"] = True
            bridged += 1
        else:
            p.extra["auto_applyable"] = False
        if on_progress:
            on_progress(i + 1, min(len(to_bridge), limit))
    return postings, bridged

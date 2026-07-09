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


def fetch_json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    """Fetch a URL and parse JSON. Defaults to GET; pass a `body` (dict) to POST it as JSON
    (Workable's careers API is POST). Rate-limited per host (_polite_wait) and retried on
    transient failures (429/5xx/network, up to 3 attempts with backoff); other HTTP errors
    fail immediately. Raises DiscoveryError with a precise message on failure
    (Agent Guideline #11) so a single bad source never crashes a whole discovery run."""
    headers = {"User-Agent": _UA, "Accept": "application/json"}
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
                raw = r.read().decode("utf-8", errors="replace")
            break
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
        where: str = "",
        country: str = "us",
        results_per_page: int = 50,
        max_pages: int = 1,
        salary_min: int = 0,
    ) -> None:
        self.app_id = app_id
        self.app_key = app_key
        self.what = what
        self.where = where
        self.country = country.lower()
        self.results_per_page = results_per_page
        self.max_pages = max_pages
        self.salary_min = salary_min
        self.name = f"adzuna:{country}:{what[:30]}"

    def fetch(self) -> list[Posting]:
        if not (self.app_id and self.app_key):
            raise DiscoveryError("Adzuna not configured (missing app_id/app_key)")
        from urllib.parse import urlencode

        out: list[Posting] = []
        for page in range(1, self.max_pages + 1):
            params = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": self.results_per_page,
                "content-type": "application/json",
            }
            if self.what:
                params["what_or"] = self.what  # match ANY of the space-separated terms
            if self.where:
                params["where"] = self.where
            if self.salary_min:
                params["salary_min"] = self.salary_min
            url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/{page}?{urlencode(params)}"
            data = fetch_json(url)
            results = data.get("results", []) if isinstance(data, dict) else []
            for j in results:
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
                        url=j.get("redirect_url", ""),
                        ats="adzuna",
                        location=(j.get("location") or {}).get("display_name", "") or "",
                        compensation=comp,
                        remote=None,
                        apply_url=j.get("redirect_url", ""),
                        updated_at=j.get("created", ""),
                        extra={"snippet_only": True},
                    )
                )
            if len(results) < self.results_per_page:
                break  # last page
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


# --------------------------------------------------------------------------- curated feeds
#
# Community-maintained, daily-updated JSON lists of EARLY-CAREER roles (new-grad + internships)
# — early-career by construction, so no senior roles to filter out (DECISIONS.md #031). The
# lists are URL-only (a title + an application link, no JD text), and ~40% of active links point
# at Greenhouse/Lever/Ashby — ATSs we can both fetch a full JD from AND fill. We rank listings by
# title-relevance to the résumé, resolve the FULL JD for the top-K via the linked ATS, and emit
# normal full-JD Postings so the matcher/apply pipeline is unchanged. Personal-use only (public
# job links; the lists carry no explicit redistribution license).

_SIMPLIFY_FEEDS = {
    "new-grad": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "intern": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
}
_CURATED_ATS = ("greenhouse", "lever", "ashby")  # resolvable full JD AND fillable in Apply
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
    """Early-career discovery from the SimplifyJobs new-grad/internship JSON feeds
    (DECISIONS.md #031). Keeps `active` roles whose apply link is a resolvable+fillable ATS
    (Greenhouse/Lever/Ashby), ranks them by title-relevance to the résumé, resolves the full JD
    for the top `max_resolve`, and emits full-JD Postings. Personal-use only (public job links)."""

    def __init__(self, resume, kinds=("new-grad", "intern"), max_resolve: int = 40) -> None:
        self.resume = resume
        self.kinds = tuple(kinds)
        self.max_resolve = max_resolve
        self.name = "curated:" + ",".join(self.kinds)

    def fetch(self) -> list[Posting]:
        import html as _html

        def clean(s: str) -> str:
            return _html.unescape(s or "").strip()

        seen: set[str] = set()
        listings: list[tuple[dict, str]] = []
        for kind in self.kinds:
            feed = _SIMPLIFY_FEEDS.get(kind)
            if not feed:
                continue
            data = fetch_json(feed)
            for e in data if isinstance(data, list) else []:
                if not e.get("active"):
                    continue
                url = e.get("url", "")
                ats = detect_ats_from_url(url)
                if ats not in _CURATED_ATS or not url or url in seen:
                    continue
                seen.add(url)
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
_AGGREGATOR_ATS = {"adzuna", "jooble"}
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
    returns (postings, n_bridged); bounded by `limit` redirect resolutions to stay polite."""
    to_bridge = [p for p in postings if p.ats in _AGGREGATOR_ATS]
    ashby_cache: dict = {}
    bridged = 0
    for i, p in enumerate(to_bridge[:limit]):
        final = resolve_redirect(p.apply_url or p.url)
        ats = detect_ats_from_url(final)
        if ats not in ("other", ""):
            p.extra["bridged_from"] = p.ats
            p.extra["auto_applyable"] = ats in ATS_SOURCES  # do we have a dedicated adapter?
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

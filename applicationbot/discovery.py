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

- Greenhouse     : GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
- Lever          : GET api.lever.co/v0/postings/{slug}?mode=json
- Ashby          : GET api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
- SmartRecruiters: GET api.smartrecruiters.com/v1/companies/{company}/postings (+ /{id} detail)
- Recruitee      : GET {company}.recruitee.com/api/offers/

All return the full job-description text with no scraping and no ToS grey area
(Agent Guideline #4). Broad aggregator sources (Adzuna, remote feeds) slot in behind the
same `Source` interface later. Every source yields `Posting`s; `Posting.to_job_description()`
emits the exact Markdown + YAML front-matter shape the fixtures use, so the Tailor/Apply
pipeline needs no changes when real discovery replaces fixtures.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Any, Optional

import yaml

from .job_description import JobDescription

_UA = "ApplicationBot/0.1 (personal job search; contact: local user)"
_TIMEOUT = 25

# SmartRecruiters lists postings without the JD body, so we fetch each posting's detail
# (an N+1). Cap how many we pull per company to keep the run polite and bounded.
_SR_MAX_POSTINGS = 100


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


def fetch_json(url: str) -> Any:
    """GET a URL and parse JSON. Raises DiscoveryError with a precise message on failure
    (Agent Guideline #11) so a single bad source never crashes a whole discovery run."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_SSL_CTX) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise DiscoveryError(f"HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise DiscoveryError(f"network error for {url}: {e.reason}") from e
    except Exception as e:  # timeouts, ssl, etc.
        raise DiscoveryError(f"request failed for {url}: {type(e).__name__}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise DiscoveryError(f"non-JSON response from {url}: {e}") from e


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
    plus the real jobs.smartrecruiters.com apply URL. Bounded by `_SR_MAX_POSTINGS`
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
        while len(listed) < _SR_MAX_POSTINGS:
            data = fetch_json(f"{base}?limit={limit}&offset={offset}")
            content = data.get("content", []) if isinstance(data, dict) else []
            if not content:
                break
            listed.extend(content)
            if len(content) < limit:
                break
            offset += limit
        out: list[Posting] = []
        for p in listed[:_SR_MAX_POSTINGS]:
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


# Map an ATS name to its source constructor, for building sources from config.
ATS_SOURCES = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "smartrecruiters": SmartRecruitersSource,
    "recruitee": RecruiteeSource,
}


def build_source(ats: str, token: str) -> Source:
    ats = ats.strip().lower()
    if ats not in ATS_SOURCES:
        raise DiscoveryError(f"unknown ATS '{ats}'. Known: {', '.join(ATS_SOURCES)}")
    return ATS_SOURCES[ats](token)


def discover(sources: list[Source]) -> tuple[list[Posting], list[str]]:
    """Fan out across sources, collect postings, dedup by URL. Returns (postings, errors);
    a failing source is recorded as an error string, never aborting the run."""
    seen: set[str] = set()
    postings: list[Posting] = []
    errors: list[str] = []
    for src in sources:
        try:
            for p in src.fetch():
                key = p.url or f"{p.company}|{p.title}"
                if key in seen:
                    continue
                seen.add(key)
                postings.append(p)
        except DiscoveryError as e:
            errors.append(f"{src.name}: {e}")
        except Exception as e:  # defensive: a malformed field shouldn't kill the run
            errors.append(f"{src.name}: unexpected {type(e).__name__}: {e}")
    return postings, errors

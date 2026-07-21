"""Extract a full job description from a posting's HTML page — a three-tier cascade
adopted from ApplyPilot (DECISIONS.md #047). Try the cheapest, most reliable method first
and only escalate on failure:

  1. JSON-LD  — parse ``<script type="application/ld+json">`` blocks and pull the schema.org
                ``JobPosting`` (description, apply URL, title, company, salary). This is
                published structured data — the same data Google for Jobs indexes — so
                reading it is ToS-clean (Agent Guideline #4) and needs no LLM.
  2. CSS/DOM  — when a page carries no JSON-LD, take the text of the element whose id/class
                names it a description (``#job-description``, ``.description``, ``<article>``…)
                and the apply link from an ``apply``-ish ``<a>``.
  3. LLM      — last resort for novel layouts: hand the cleaned, capped page text to Claude
                and ask for ``{description, apply_url}``. Optional — only runs if a caller
                passes an ``llm`` callable — so the cascade is free and offline by default.

The point of the cascade is cost: on a real run the great majority of pages resolve at
tier 1/2, so the LLM is rarely called (ApplyPilot reports ~95% saved). Reusable across the
Discover stage: it powers `discovery.CareerSiteSource` and can backfill a full JD wherever
an ATS-specific resolver comes up empty.
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Optional

from .discovery import DiscoveryError, fetch_text, html_to_text

_MIN_DESCRIPTION_CHARS = 50   # a JD shorter than this is treated as "not really found"
_LLM_HTML_CAP = 30_000        # chars of page text handed to the LLM tier (ApplyPilot's cap)

# The tier-3 extractor: (page_text, url) -> {"description": str, "apply_url": str} | None.
LLMExtractor = Callable[[str, str], Optional[dict]]


@dataclass
class EnrichResult:
    description: str = ""
    apply_url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    compensation: str = ""
    date_posted: str = ""
    remote: Optional[bool] = None
    tier: str = ""  # "json-ld" | "css" | "llm" | "" (nothing found)

    @property
    def ok(self) -> bool:
        return len(self.description) >= _MIN_DESCRIPTION_CHARS


# ---------------------------------------------------------------------------
# Tier 1 — JSON-LD (schema.org JobPosting)
# ---------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _iter_jsonld_objects(html: str):
    """Yield every JSON object embedded in the page's ld+json blocks — recursing into nested
    dicts/lists and @graph arrays, tolerating per-block parse errors."""
    for block in _JSONLD_RE.findall(html or ""):
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)


def _is_jobposting(node: dict) -> bool:
    t = node.get("@type")
    if isinstance(t, list):
        return any(isinstance(x, str) and x.lower().endswith("jobposting") for x in t)
    return isinstance(t, str) and t.lower().endswith("jobposting")


def extract_jobpostings_from_jsonld(html: str) -> list[dict]:
    """Every schema.org JobPosting object embedded in the page (several on a listing page,
    one on a posting page, empty if none)."""
    return [n for n in _iter_jsonld_objects(html) if _is_jobposting(n)]


def _org_name(node: dict) -> str:
    org = node.get("hiringOrganization")
    if isinstance(org, dict):
        return str(org.get("name") or "").strip()
    return org.strip() if isinstance(org, str) else ""


def _location_str(node: dict) -> str:
    loc = node.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = []
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                v = addr.get(key)
                parts.append(v.get("name") if isinstance(v, dict) else v)
            return ", ".join(p for p in parts if isinstance(p, str) and p)
    return ""


def _salary_str(node: dict) -> str:
    bs = node.get("baseSalary")
    if not isinstance(bs, dict):
        return ""
    cur = bs.get("currency") or ""
    val = bs.get("value")
    if isinstance(val, dict):
        lo, hi = val.get("minValue"), val.get("maxValue")
        if lo and hi:
            return f"{lo}-{hi} {cur}".strip()
        if val.get("value"):
            return f"{val.get('value')} {cur}".strip()
    return ""


def _apply_url_from_jsonld(node: dict) -> str:
    """schema.org's `url` is the canonical posting URL; some feeds put a direct apply link in
    `directApply` (a URL string, though the spec also allows a bool) or `applicationContact`."""
    da = node.get("directApply")
    if isinstance(da, str) and da.startswith("http"):
        return da
    ac = node.get("applicationContact")
    if isinstance(ac, dict) and isinstance(ac.get("url"), str):
        return ac["url"]
    return node.get("url") if isinstance(node.get("url"), str) else ""


def _remote_from_jsonld(node: dict) -> Optional[bool]:
    jlt = node.get("jobLocationType")
    return True if isinstance(jlt, str) and "telecommute" in jlt.lower() else None


def jobposting_to_result(node: dict) -> EnrichResult:
    """Normalize one schema.org JobPosting dict into an EnrichResult."""
    desc = html_to_text(_html.unescape(node.get("description", "") or ""))
    return EnrichResult(
        description=desc,
        apply_url=_apply_url_from_jsonld(node),
        title=str(node.get("title") or "").strip(),
        company=_org_name(node),
        location=_location_str(node),
        compensation=_salary_str(node),
        date_posted=str(node.get("datePosted") or "").strip(),
        remote=_remote_from_jsonld(node),
        tier="json-ld",
    )


# ---------------------------------------------------------------------------
# Tier 2 — CSS/DOM patterns
# ---------------------------------------------------------------------------

_DESC_ID_CLASS_HINTS = ("job-description", "job_description", "jobdescription",
                        "job-details", "jobdetails", "job-post", "posting", "description")
_DESC_TAGS_FALLBACK = ("article", "main")
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
              "param", "source", "track", "wbr"}
_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "section"}


def _clean_ws(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class _DescExtractor(HTMLParser):
    """Capture the text of the element whose id/class names it a job description (plus
    ``<article>``/``<main>`` as a fallback); the longest such block wins. Tracks a stack of
    open non-void tags so nested markup closes the capture at the right element, and skips
    ``<script>``/``<style>`` data. Robust to messy real-world HTML (tolerant pop)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []   # open non-void, non-skipped tag names
        self._cap_at: Optional[int] = None  # stack length when the current capture began
        self._skip = 0                # >0 while inside a script/style/etc. subtree
        self._buf: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag in _VOID_TAGS:
            if self._cap_at is not None and tag == "br":
                self._buf.append("\n")
            return
        start = False
        if self._cap_at is None:
            a = {k: (v or "") for k, v in attrs}
            idc = f"{a.get('id', '')} {a.get('class', '')}".lower()
            start = any(h in idc for h in _DESC_ID_CLASS_HINTS) or tag in _DESC_TAGS_FALLBACK
        self._stack.append(tag)
        if self._cap_at is not None:
            if tag == "li":
                self._buf.append("\n- ")
            elif tag in _BLOCK_TAGS:
                self._buf.append("\n")
        elif start:
            self._cap_at = len(self._stack)
            self._buf = []

    def handle_startendtag(self, tag: str, attrs) -> None:  # <br/>, <img/>, …
        if self._cap_at is not None and tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in _VOID_TAGS or not self._stack:
            return
        self._stack.pop()
        if self._cap_at is not None and len(self._stack) < self._cap_at:
            self.blocks.append(_clean_ws("".join(self._buf)))
            self._cap_at = None

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and self._cap_at is not None:
            self._buf.append(data)

    def best(self) -> str:
        return max(self.blocks, key=len) if self.blocks else ""


_APPLY_A_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


def extract_description_css(html: str) -> str:
    p = _DescExtractor()
    p.feed(html or "")
    return p.best()


def extract_apply_url_css(html: str, base_url: str = "") -> str:
    """First ``<a>`` whose href or link text mentions 'apply', resolved against base_url."""
    from urllib.parse import urljoin

    for href, inner in _APPLY_A_RE.findall(html or ""):
        if "apply" in f"{href} {inner}".lower():
            return urljoin(base_url, href) if base_url else href
    return ""


# ---------------------------------------------------------------------------
# Tier 3 — LLM (optional, Claude Code CLI)
# ---------------------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _strip_scripts(html: str) -> str:
    return _SCRIPT_STYLE_RE.sub(" ", html or "")


def claude_llm_extractor(text: str, url: str) -> Optional[dict]:
    """Tier-3 extractor backed by the Claude Code CLI (subscription billing, DECISIONS.md
    #034). Returns ``{"description", "apply_url"}`` or None on any failure. Pass this as
    ``llm=`` to opt into the LLM tier — it is never called unless a caller opts in."""
    from .backends import ClaudeUnavailableError, run_claude_cli

    schema = {
        "type": "object",
        "properties": {"description": {"type": "string"}, "apply_url": {"type": "string"}},
        "required": ["description"],
        "additionalProperties": False,
    }
    prompt = (
        "Extract the job posting from this web page. Return the FULL job-description text "
        "(every section: responsibilities, requirements, qualifications, benefits) verbatim, "
        "and the application URL if one is present.\n\n"
        f"Page URL: {url}\n\nPage text:\n{text}"
    )
    try:
        out = run_claude_cli(
            prompt, think=False, json_schema=schema, activity="enrichment",
            system="You extract structured job-posting data from web pages. Return only the requested JSON.",
        )
    except ClaudeUnavailableError:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------------------

def enrich_from_html(html: str, *, url: str = "", llm: Optional[LLMExtractor] = None) -> EnrichResult:
    """Run the cascade on already-fetched page HTML and return the best single result
    (`.tier` names which method won; `.ok` is False if nothing produced a usable
    description). For pages that may list several postings, use
    `extract_jobpostings_from_jsonld` directly."""
    for node in extract_jobpostings_from_jsonld(html):
        res = jobposting_to_result(node)
        if not res.apply_url and url:
            res.apply_url = url
        if res.ok:
            return res
    desc = extract_description_css(html)
    if len(desc) >= _MIN_DESCRIPTION_CHARS:
        return EnrichResult(description=desc, apply_url=extract_apply_url_css(html, url) or url, tier="css")
    if llm is not None:
        text = html_to_text(_strip_scripts(html))[:_LLM_HTML_CAP]
        try:
            data = llm(text, url)
        except Exception:
            data = None
        if isinstance(data, dict):
            desc = html_to_text(_html.unescape(data.get("description", "") or ""))
            if len(desc) >= _MIN_DESCRIPTION_CHARS:
                return EnrichResult(description=desc, apply_url=data.get("apply_url") or url, tier="llm")
    return EnrichResult()


def fetch_full_jd(url: str, *, llm: Optional[LLMExtractor] = None) -> EnrichResult:
    """Fetch `url` and run the enrichment cascade. Returns an empty (`.ok == False`) result
    if the page can't be fetched or no description is found."""
    try:
        html = fetch_text(url, accept="text/html,application/xhtml+xml,*/*")
    except DiscoveryError:
        return EnrichResult()
    return enrich_from_html(html, url=url, llm=llm)

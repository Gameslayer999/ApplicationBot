"""Source scout — turn career-page URLs into validated, ready-to-wire discovery sources.

This is the deterministic half of the "auto-discover new job sources" feature (DECISIONS.md
#134). A periodic Claude cloud routine does the fuzzy part — WebSearch across an allowlist of
the ATS domains we already support, plus harvesting links out of a normal discovery run — and
hands the resulting career URLs here. This module does the parts that must be exact and testable:

  1. `extract_board(url)`  — classify a URL's ATS and pull its board token (the company slug).
  2. `validate_board`      — probe the live public ATS API; keep only tokens that return postings.
  3. dedup vs the boards already in `profile/discovery.yaml` (never re-propose what you run).
  4. persist survivors to the COMMITTED candidates file (`data/source_candidates.json`) — the
     transport that carries cloud-found candidates back to the local app (git pull), holding
     only public company ATS identifiers + coarse provenance, never résumé/profile PII.
  5. `merge_into_filters` — the one-click "accept": append an accepted candidate to `boards`.

Scope guard (NEXT_STEPS "don't overload ATS_SOURCES"): the board flow only ever proposes boards
for ATSs already in `discovery.ATS_SOURCES` — companies on Greenhouse/Lever/Ashby/SmartRecruiters/
Recruitee/Workable. It adds NO new source *type* and asserts NO new autofill adapter; a candidate
is just "another company on a board we already read + already know how to apply to."

Phase 2 (decision 136) adds a parallel flow for whole new aggregator *platforms* — but as
declarative DATA, not code: `validate_spec` / `stage_spec` / `enable_json_aggregator` take an
`AggregatorSpec` (endpoint + field map), verify it against the live API, and stage it in the
committed registry for one-click enable. A platform that a spec can't express (HTML-only, odd
auth) is still the escape hatch: a hand-written `Source` adapter via PR.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .discovery import (
    AGGREGATOR_SPECS_PATH,
    ATS_SOURCES,
    AggregatorSpec,
    DiscoveryError,
    JsonApiSource,
    Posting,
    build_source,
)

# The committed candidates file: cloud→local transport + the app's "new sources found" queue.
CANDIDATES_PATH = "data/source_candidates.json"

# Only public company slugs on ATSs we already support are ever written here — no PII. This
# allowlist is exactly `discovery.ATS_SOURCES`; keeping it derived means a newly supported ATS
# becomes scoutable automatically and an unsupported one can never sneak into config.
SUPPORTED_ATS = tuple(ATS_SOURCES)

# Path segments that are never a company token (ATS UI/routing words), so we don't propose
# "greenhouse:embed" or "workable:j" from a deep link.
_NON_TOKEN_SEGMENTS = {"embed", "j", "jobs", "careers", "o", "en", "en-us"}


@dataclass
class Candidate:
    """One proposed discovery source: a company on an already-supported ATS. Serialized to the
    committed candidates file. `validated` gates whether the app may offer it for one-click add."""

    ats: str
    token: str
    provenance: str = ""  # coarse, non-PII: "discovery-harvest" | "web-search" | free text
    validated: bool = False
    n_postings: int = 0
    sample_title: str = ""
    sample_company: str = ""
    error: str = ""  # why validation failed, if it did (actionable per Guideline #11)

    @property
    def key(self) -> tuple[str, str]:
        return (self.ats, self.token.lower())


# --------------------------------------------------------------------------- #
# 1. URL → (ats, token)
# --------------------------------------------------------------------------- #

def _first_path_token(path: str) -> str:
    """First meaningful path segment (skips ATS routing words like 'embed'/'jobs')."""
    for seg in path.split("/"):
        seg = seg.strip()
        if seg and seg.lower() not in _NON_TOKEN_SEGMENTS:
            return seg
    return ""


def extract_board(url: str) -> tuple[str, str] | None:
    """Classify `url`'s ATS and extract its board token (company slug), or None if the URL is not
    a supported-ATS careers link. Handles both path-style boards (greenhouse/lever/ashby/
    smartrecruiters/workable: `host/{token}/...`) and subdomain-style boards (recruitee:
    `{token}.recruitee.com`). Token is returned exactly as it appears — the ATS APIs are
    case-sensitive for some slugs (e.g. 'Visa')."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url if "//" in url else f"https://{url}")
    host = parts.netloc.lower()
    path = parts.path or ""
    qs = urllib.parse.parse_qs(parts.query or "")

    if "greenhouse.io" in host:
        # Embedded application widget: token is the ?for= param, not a path segment.
        if "for" in qs and qs["for"]:
            return ("greenhouse", qs["for"][0])
        tok = _first_path_token(path)
        return ("greenhouse", tok) if tok else None
    if "lever.co" in host:
        tok = _first_path_token(path)
        return ("lever", tok) if tok else None
    if "ashbyhq.com" in host:
        tok = _first_path_token(path)
        return ("ashby", tok) if tok else None
    if "smartrecruiters.com" in host:
        tok = _first_path_token(path)
        return ("smartrecruiters", tok) if tok else None
    if "workable.com" in host:
        # apply.workable.com/{token}/... (path) or {token}.workable.com (subdomain)
        tok = _first_path_token(path)
        if tok:
            return ("workable", tok)
        sub = host.split(".workable.com")[0]
        if sub and sub not in ("apply", "www", "jobs", "careers"):
            return ("workable", sub)
        return None
    if "recruitee.com" in host:
        sub = host.split(".recruitee.com")[0]
        if sub and sub not in ("www", "api", "jobs", "careers"):
            return ("recruitee", sub)
        return None
    return None


def extract_boards(urls: list[str]) -> list[tuple[str, str]]:
    """Extract every supported-ATS board from a list of URLs, deduped (case-insensitive on token),
    order-preserving. Non-supported URLs are silently skipped."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for u in urls:
        b = extract_board(u)
        if not b:
            continue
        k = (b[0], b[1].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(b)
    return out


def harvest_boards_from_postings(postings: list[Posting]) -> list[tuple[str, str]]:
    """Pull board tokens out of an already-run discovery pass. Every posting's apply/url has, by
    the time the aggregator→ATS bridge is done, been rewritten to its real ATS URL — so this
    turns transient aggregator/curated hits into durable per-company boards for free, no new
    scraping. Prefers `apply_url` (post-bridge), falls back to `url`."""
    urls: list[str] = []
    for p in postings:
        urls.append(getattr(p, "apply_url", "") or "")
        urls.append(getattr(p, "url", "") or "")
    return extract_boards(urls)


# --------------------------------------------------------------------------- #
# 2. Validation — a token only counts if the live ATS API returns postings
# --------------------------------------------------------------------------- #

def validate_board(ats: str, token: str) -> Candidate:
    """Probe the live public ATS API for `(ats, token)` and return a Candidate carrying the result.
    A board is `validated` only if the API returns ≥1 posting — a dead/misspelled slug returns an
    empty list or errors and is kept UNvalidated (with the exact error) so the app never proposes
    a source that yields nothing."""
    c = Candidate(ats=ats, token=token)
    try:
        postings = build_source(ats, token).fetch()
    except DiscoveryError as e:
        c.error = str(e)
        return c
    except Exception as e:  # defensive: a malformed field shouldn't crash the scout
        c.error = f"unexpected {type(e).__name__}: {e}"
        return c
    c.n_postings = len(postings)
    if postings:
        c.validated = True
        c.sample_title = (postings[0].title or "").strip()
        c.sample_company = (postings[0].company or "").strip()
    else:
        c.error = "board returned 0 postings (dead, private, or wrong slug)"
    return c


# --------------------------------------------------------------------------- #
# 3–4. Dedup + the committed candidates store
# --------------------------------------------------------------------------- #

def known_boards(boards) -> set[tuple[str, str]]:
    """The `(ats, token.lower())` set already configured in `filters.boards` — never re-propose
    a source the user already runs. Accepts a list of `filters.Board` (or any obj with .ats/.token)."""
    return {(b.ats.strip().lower(), b.token.strip().lower()) for b in boards}


def load_candidates(path: str | Path = CANDIDATES_PATH) -> list[Candidate]:
    p = Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text() or "{}")
    return [Candidate(**c) for c in raw.get("candidates", [])]


def save_candidates(cands: list[Candidate], path: str | Path = CANDIDATES_PATH) -> None:
    """Write the candidates file. Only serializable Candidate fields are stored — no PII. Sorted
    for a stable, reviewable git diff each run."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(cands, key=lambda c: (c.ats, c.token.lower()))
    doc = {
        "_comment": (
            "ApplicationBot source-scout candidates — public company ATS slugs proposed for "
            "discovery, NOT résumé/profile data. Committed on purpose: it is how the cloud "
            "routine hands new sources back to the local app. Accept via the settings UI or "
            "`python -m applicationbot.source_scout --accept <ats>:<token>`."
        ),
        "candidates": [asdict(c) for c in ordered],
    }
    p.write_text(json.dumps(doc, indent=2) + "\n")


def merge_new(existing: list[Candidate], found: list[Candidate]) -> tuple[list[Candidate], int]:
    """Merge freshly-found candidates into the stored list, keyed by (ats, token). A re-found
    candidate refreshes its validation result but keeps its original provenance. Returns
    (merged, n_new)."""
    by_key = {c.key: c for c in existing}
    n_new = 0
    for c in found:
        prior = by_key.get(c.key)
        if prior is None:
            by_key[c.key] = c
            n_new += 1
        else:
            prov = prior.provenance or c.provenance
            by_key[c.key] = c
            by_key[c.key].provenance = prov
    return list(by_key.values()), n_new


# --------------------------------------------------------------------------- #
# Phase 2 — declarative JSON-API aggregator specs (DECISIONS.md #136)
# --------------------------------------------------------------------------- #

# A spec is usable only if its field_map names a title, a company, and somewhere to apply.
_SPEC_REQUIRED_FIELDS = ("title", "company")


@dataclass
class SpecResult:
    """Validation result for a declarative aggregator spec (the JSON-API analog of Candidate)."""

    name: str
    validated: bool = False
    n_postings: int = 0
    sample_title: str = ""
    sample_company: str = ""
    error: str = ""


def validate_spec(spec_dict: dict, keywords: list[str] | None = None) -> SpecResult:
    """Check a candidate JSON-API spec: its field_map must name title/company/apply, and a live
    fetch (with `keywords` substituted into `{q}`) must return ≥1 usable posting. Never raises —
    a bad endpoint/shape comes back as `validated=False` with the exact reason (Guideline #11)."""
    name = (spec_dict.get("name") or "").strip()
    if not name or not (spec_dict.get("endpoint") or "").strip():
        return SpecResult(name, error="a spec needs a non-empty name and endpoint")
    fm = spec_dict.get("field_map") or {}
    missing = [f for f in _SPEC_REQUIRED_FIELDS if f not in fm]
    if "url" not in fm and "apply_url" not in fm:
        missing.append("url or apply_url")
    if missing:
        return SpecResult(name, error="field_map is missing: " + ", ".join(missing))
    try:
        posts = JsonApiSource(AggregatorSpec.from_dict(spec_dict), keywords=keywords).fetch()
    except DiscoveryError as e:
        return SpecResult(name, error=str(e))
    except Exception as e:  # defensive: a malformed field shouldn't crash the scout
        return SpecResult(name, error=f"unexpected {type(e).__name__}: {e}")
    if not posts:
        return SpecResult(name, error="endpoint returned 0 usable postings "
                                      "(check list_path / field_map / endpoint)")
    return SpecResult(name, validated=True, n_postings=len(posts),
                      sample_title=(posts[0].title or "").strip(),
                      sample_company=(posts[0].company or "").strip())


def load_registry_specs(path: str = AGGREGATOR_SPECS_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return (json.loads(p.read_text() or "{}") or {}).get("specs", [])


def save_registry_specs(specs: list[dict], path: str = AGGREGATOR_SPECS_PATH) -> None:
    """Write the committed spec registry, sorted by name for a stable, reviewable diff."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(specs, key=lambda s: s.get("name", ""))
    doc = {
        "_comment": (
            "ApplicationBot declarative JSON-API aggregator registry — validated specs (endpoint + "
            "field map) for public job APIs. Shareable, non-PII. The source-scout routine appends "
            "here; enable a spec by adding its name to profile/discovery.yaml `json_aggregators`."
        ),
        "specs": ordered,
    }
    p.write_text(json.dumps(doc, indent=2) + "\n")


def stage_spec(spec_dict: dict, keywords: list[str] | None = None,
               path: str = AGGREGATOR_SPECS_PATH) -> SpecResult:
    """Validate a candidate spec and, if it passes, upsert it into the committed registry (keyed by
    name), stamping the validation sample so the UI can show "N roles, e.g. …" without re-probing.
    Returns the SpecResult either way; an invalid spec is never written."""
    res = validate_spec(spec_dict, keywords)
    if not res.validated:
        return res
    stamped = {**spec_dict, "name": res.name, "n_postings": res.n_postings,
               "sample_title": res.sample_title, "sample_company": res.sample_company}
    by_name = {s.get("name"): s for s in load_registry_specs(path)}
    by_name[res.name] = stamped
    save_registry_specs(list(by_name.values()), path)
    return res


def enable_json_aggregator(name: str, filters_path=None) -> bool:
    """Append `name` to `json_aggregators` in discovery.yaml (the one-click "Add" for a spec).
    Idempotent: True if added, False if already enabled or not in the registry."""
    from . import filters as filters_mod

    name = name.strip()
    if name not in {s.get("name") for s in load_registry_specs()}:
        return False
    path = filters_path or filters_mod.DEFAULT_PATH
    f = filters_mod.load_filters(path)
    if name in f.json_aggregators:
        return False
    f.json_aggregators.append(name)
    filters_mod.save_filters(f, path)
    return True


def enable_contrib_source(name: str, filters_path=None) -> bool:
    """Append `name` to `contrib_sources` in discovery.yaml — the one-click "Add" for a merged
    drop-in adapter (decision 139). Idempotent: True if added, False if already enabled or not a
    loaded adapter."""
    from . import filters as filters_mod
    from .sources_contrib import load_contrib_sources

    name = name.strip()
    if name not in load_contrib_sources():
        return False
    path = filters_path or filters_mod.DEFAULT_PATH
    f = filters_mod.load_filters(path)
    if name in f.contrib_sources:
        return False
    f.contrib_sources.append(name)
    filters_mod.save_filters(f, path)
    return True


# --------------------------------------------------------------------------- #
# 5. Accept — wire a validated candidate into discovery config (one-click action)
# --------------------------------------------------------------------------- #

def merge_into_filters(ats: str, token: str, filters_path=None) -> bool:
    """Append `(ats, token)` to `boards` in `profile/discovery.yaml` and save. Idempotent: returns
    False (no write) if the board is already configured, True if it was added. This is the
    deterministic backing for the settings UI's one-click 'Add source'."""
    from . import filters as filters_mod

    path = filters_path or filters_mod.DEFAULT_PATH
    f = filters_mod.load_filters(path)
    key = (ats.strip().lower(), token.strip().lower())
    if key in known_boards(f.boards):
        return False
    f.boards.append(filters_mod.Board(ats=ats.strip().lower(), token=token.strip()))
    filters_mod.save_filters(f, path)
    return True


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

@dataclass
class ScoutResult:
    proposed: list[Candidate] = field(default_factory=list)  # validated + new (net of known/stored)
    rejected: list[Candidate] = field(default_factory=list)  # extracted but failed validation
    skipped_known: int = 0  # boards already in filters.boards
    n_new: int = 0  # net-new added to the candidates file this run


def scout(urls, filters, *, provenance="web-search", validate=True,
          store_path=CANDIDATES_PATH) -> ScoutResult:
    """Turn career URLs into validated, deduped, persisted candidates.

    Steps: extract boards → drop any already in `filters.boards` → validate each against the live
    ATS API → merge validated survivors into the committed candidates file. `filters` is a
    `DiscoveryFilters` (used only for its `boards`, for dedup). Set `validate=False` to skip the
    network probe (extraction/dedup only — used by tests)."""
    known = known_boards(filters.boards)
    result = ScoutResult()
    boards = extract_boards(urls)
    to_check: list[tuple[str, str]] = []
    for ats, token in boards:
        if (ats, token.lower()) in known:
            result.skipped_known += 1
            continue
        to_check.append((ats, token))

    validated: list[Candidate] = []
    for ats, token in to_check:
        c = validate_board(ats, token) if validate else Candidate(ats=ats, token=token, validated=True)
        c.provenance = provenance
        if c.validated:
            validated.append(c)
            result.proposed.append(c)
        else:
            result.rejected.append(c)

    stored = load_candidates(store_path)
    merged, n_new = merge_new(stored, validated)
    save_candidates(merged, store_path)
    result.n_new = n_new
    return result


def _main(argv=None) -> int:
    import argparse

    from . import filters as filters_mod

    ap = argparse.ArgumentParser(
        prog="python -m applicationbot.source_scout",
        description="Scout new job-source boards (companies on already-supported ATSs) and stage "
                    "them in the committed candidates file for one-click add.",
    )
    ap.add_argument("--url", action="append", default=[], metavar="URL",
                    help="a career-page URL to extract+validate (repeatable)")
    ap.add_argument("--from-discovery", action="store_true",
                    help="run a normal discovery pass and harvest boards out of its postings")
    ap.add_argument("--accept", metavar="ATS:TOKEN",
                    help="wire a candidate into profile/discovery.yaml, e.g. greenhouse:stripe")
    ap.add_argument("--list", action="store_true", help="print the current candidates file")
    ap.add_argument("--no-validate", action="store_true", help="skip the live ATS probe")
    ap.add_argument("--add-spec", metavar="JSON",
                    help="validate a declarative JSON-API aggregator spec (a JSON object with name/"
                         "endpoint/list_path/field_map/…) and, if it returns postings, stage it in "
                         "the registry (decision 136)")
    ap.add_argument("--keyword", action="append", default=[], metavar="KW",
                    help="a search keyword to substitute into a spec's {q} when validating (repeatable)")
    ap.add_argument("--list-specs", action="store_true", help="print the aggregator-spec registry")
    args = ap.parse_args(argv)

    if args.add_spec:
        try:
            spec_dict = json.loads(args.add_spec)
        except json.JSONDecodeError as e:
            print(f"--add-spec is not valid JSON: {e}")
            return 2
        res = stage_spec(spec_dict, keywords=args.keyword or None)
        if res.validated:
            print(f"staged spec {res.name!r}: {res.n_postings} postings "
                  f"(e.g. {res.sample_title!r}) — enable it in Discovery settings or "
                  f"add \"{res.name}\" to json_aggregators")
            return 0
        print(f"rejected spec {res.name!r}: {res.error}")
        return 1

    if args.list_specs:
        for s in load_registry_specs():
            n = s.get("n_postings", "?")
            print(f"  {s.get('name')}  [{n} postings]  {s.get('endpoint','')}")
        return 0

    if args.accept:
        if ":" not in args.accept:
            print("--accept expects ATS:TOKEN, e.g. greenhouse:stripe")
            return 2
        ats, token = args.accept.split(":", 1)
        added = merge_into_filters(ats, token)
        print(f"{'added' if added else 'already configured'}: {ats}:{token}")
        return 0

    if args.list:
        for c in load_candidates():
            mark = "✓" if c.validated else "✗"
            detail = f"{c.n_postings} postings" if c.validated else c.error
            print(f"  {mark} {c.ats}:{c.token}  [{c.provenance}]  {detail}")
        return 0

    f = filters_mod.load_filters()
    urls = list(args.url)
    if args.from_discovery:
        # Run a normal (Claude-free) discovery pass and harvest the real ATS boards its postings
        # resolved to — turning transient aggregator/curated hits into durable per-company boards.
        from . import apply_profile, pipeline
        from .resume import load_resume

        resume = load_resume("profile/resume.yaml")
        profile = apply_profile.load_profile()
        res_pipe = pipeline.discover_and_match(resume, f, profile=profile, use_claude=False)
        postings = [m.posting for m in res_pipe.matches]
        harvested = harvest_boards_from_postings(postings)

        result = ScoutResult()
        known = known_boards(f.boards)
        found: list[Candidate] = []
        for ats, token in harvested:
            if (ats, token.lower()) in known:
                result.skipped_known += 1
                continue
            c = validate_board(ats, token)
            c.provenance = "discovery-harvest"
            (found if c.validated else result.rejected).append(c)
        result.proposed = found
        merged, n_new = merge_new(load_candidates(), found)
        save_candidates(merged)
        result.n_new = n_new
        _print_result(result)
        return 0

    if not urls:
        ap.print_help()
        return 0
    res = scout(urls, f, validate=not args.no_validate)
    _print_result(res)
    return 0


def _print_result(res: ScoutResult) -> None:
    print(f"proposed {len(res.proposed)} validated source(s), {res.n_new} new to the candidates file")
    for c in res.proposed:
        print(f"  ✓ {c.ats}:{c.token}  ({c.n_postings} postings, e.g. {c.sample_title!r})")
    if res.rejected:
        print(f"rejected {len(res.rejected)} (failed live validation):")
        for c in res.rejected:
            print(f"  ✗ {c.ats}:{c.token}  {c.error}")
    if res.skipped_known:
        print(f"skipped {res.skipped_known} already-configured board(s)")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

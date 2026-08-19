"""End-to-end discovery pipeline (Stages 2→4), qualification-driven (DECISIONS.md #025).

    discover  →  gate  →  qualification-match  →  [testing mode] tailor → PDF → dry-run apply

Two modes:

- **List (default):** discover postings from the configured sources, apply the coarse
  gates, rank against the user's qualifications (keyword pre-filter → Claude judge), and
  print the ranked matches. No browser, fast to iterate.

- **Testing mode (`--apply-first`):** everything above, then take the **single top match**
  and run the full loop on it — tailor the résumé, export a PDF, and launch a **dry-run,
  headed** apply you watch fill live (never submits; Agent Guideline #3). This is the
  "watch one job go end-to-end before turning on the autonomous runner" mode the user asked
  for. The autonomous many-postings runner builds on this same core.

Run:
    python -m applicationbot.pipeline                 # list qualified matches
    python -m applicationbot.pipeline --apply-first   # + watch the top match fill (dry-run)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import backends
from . import resume_store
from .apply_profile import ApplicationProfile, load_profile, resume_with_profile_links
from .discovery import Source, discover
from .filters import DiscoveryFilters, apply_gates, build_sources, load_filters
from .matching import Match, match
from .models import Resume
from .resume import load_resume


@dataclass
class PipelineResult:
    matches: list[Match]
    discovered: int
    after_gates: int
    errors: list[str]
    skipped_seen: int = 0  # postings dropped because they're already in the tracker
    skipped_shown: int = 0  # postings hidden because a previous preview already showed them (decision 053)
    bridged: int = 0  # aggregator hits resolved to a fillable ATS (decision 032)
    non_fillable: list = None  # postings on portals Apply can't fill (decision 035 gate)
    from_cache: bool = False  # matches came from the discovery snapshot, not a live search (decision 037)
    cache_age_seconds: float | None = None  # age of the reused snapshot, when from_cache
    funnel: dict = None  # per-stage drop breakdown for the diagnostic (decision: diagnose-first). Empty on cache hits.

    def __post_init__(self):
        if self.non_fillable is None:
            self.non_fillable = []
        if self.funnel is None:
            self.funnel = {}


def _is_fillable(p) -> bool:
    """Can the Apply stage drive this posting's form? True for `discovery.FILLABLE_ATS` — the six
    public-API ATSs plus Workday (the deterministic adapter, decision 059 — M1 dry-run) and
    Jobvite/BambooHR (open forms, decision 168) — and for aggregator hits not yet bridge-resolved
    (which redirect to one of them or get marked auto_applyable=False by the bridge).
    `discovery.ACCOUNT_GATED_ATS` (iCIMS / Taleo / Avature) and unresolved links are not."""
    from .discovery import _AGGREGATOR_ATS, FILLABLE_ATS
    if p.extra.get("auto_applyable") is False:
        return False
    return p.ats in FILLABLE_ATS or p.ats in _AGGREGATOR_ATS


def _revisit_canonical_urls(revisit: bool = True) -> set:
    """Canonicalized URLs of applications prepared but NEVER REVIEWED (decision 149). These are
    exempt from both suppression filters — the tracker skip and the seen-openings ledger — so a
    posting the loop prepared while the user was away is brought up by the next search instead
    of being buried forever. Empty when `revisit` is off or the tracker can't be read."""
    if not revisit:
        return set()
    from . import tracker
    from .discovery import canonical_url
    try:
        return {canonical_url(u) for u in tracker.unreviewed_source_urls()}
    except Exception:
        return set()


def _seen_canonical_urls(filters: DiscoveryFilters) -> set:
    """Canonicalized URLs of postings already in the tracker (empty when skip_seen is off or
    the tracker can't be read). Re-computed on every run — including cache hits — so a role
    applied to since a snapshot was saved never re-surfaces from stale cache."""
    if not filters.skip_seen:
        return set()
    from . import tracker
    from .discovery import canonical_url
    try:
        seen = tracker.seen_source_urls()  # tracker stores raw URLs
    except Exception:
        return set()
    return {canonical_url(u) for u in seen}


def _record_shown(matches: list[Match], only_new: bool) -> None:
    """Record the openings this run actually put in front of the user, so the next `only_new`
    run hides them. Only JUDGED matches are recorded (decision 146): a keyword-only match never
    got a fit verdict, and recording it would hide it forever without Claude ever having scored
    it — the judge only scores `top_n` per run, so everything past that cut used to be burned
    unseen. Leaving them unrecorded is what lets the next run judge the next-best slice."""
    if not only_new:
        return
    from . import discovery_seen
    discovery_seen.record(m.posting.url for m in matches if m.fit_score is not None)


def _hide_already_shown(matches: list[Match], only_new: bool, revisit: bool = True) -> tuple[list[Match], int]:
    """The seen-openings ledger (decision 053), cache-hit path: drop matches a previous run
    already surfaced, then record the judged survivors so the NEXT run hides them too.
    Returns (matches_to_show, n_hidden). A no-op (and never records) when `only_new` is False,
    so the runner and other non-preview callers keep their exact current behaviour."""
    if not only_new:
        return matches, 0
    from . import discovery_seen
    from .discovery import canonical_url
    seen = discovery_seen.seen_urls() - _revisit_canonical_urls(revisit)
    hidden = 0
    if seen:
        before = len(matches)
        matches = [m for m in matches if canonical_url(m.posting.url) not in seen]
        hidden = before - len(matches)
    _record_shown(matches, only_new)
    return matches, hidden


def _drop_already_shown(postings: list, only_new: bool, revisit: bool = True) -> tuple[list, int]:
    """The ledger applied BEFORE the judge (decision 146), live-search path: postings a previous
    run already judged are dropped here, so they never consume this run's scarce `top_n` judge
    slots. That is what makes a repeat search productive — each pass judges the next-best
    postings instead of re-scoring the same ones and reporting "nothing new".
    Returns (postings_to_match, n_hidden). Must run AFTER bridging, since the ledger holds the
    post-bridge (real ATS) URLs."""
    if not only_new:
        return postings, 0
    from . import discovery_seen
    from .discovery import canonical_url
    seen = discovery_seen.seen_urls() - _revisit_canonical_urls(revisit)
    if not seen:
        return postings, 0
    kept = [p for p in postings if canonical_url(p.url) not in seen]
    return kept, len(postings) - len(kept)


def discover_and_match(
    resume: Resume,
    filters: DiscoveryFilters,
    *,
    profile: ApplicationProfile | None = None,
    extra_sources: list[Source] | None = None,
    use_claude: bool = True,
    bridge: bool = True,
    cache: bool = True,
    force_fresh: bool = False,
    only_new: bool = False,
    revisit: bool = True,
    on_progress=None,
) -> PipelineResult:
    """The reusable core: discover → gate → skip-already-seen → bridge → qualification-match.
    `on_progress(done, total)` reports Claude-judging progress for a UI. `bridge` resolves
    aggregator (Adzuna/Jooble) redirect links to their real ATS so those hits become
    auto-applyable (a no-op when no aggregator postings are present).

    Caching (decision 037): unless `force_fresh` or `cache=False`, a discovery snapshot
    younger than `filters.cache_ttl_hours` (and matching the résumé/boards/filters
    fingerprint) is reused verbatim — skipping the board search AND the Claude judge. The
    only per-run work on a cache hit is re-applying `skip_seen`, so a role you've since
    applied to still drops out. A live run saves its result as the next snapshot.

    `only_new` (decision 053): for preview/list runs, hide openings a previous preview already
    showed (the seen-openings ledger) and record what's surfaced, so each run shows only NEW
    postings. Layered on top of the cache (which still holds the full ranked result) and
    `skip_seen`; off by default so the autonomous runner is unaffected. On a live search the
    hide runs BEFORE the judge and only JUDGED postings are recorded (decision 146), so each
    repeat search scores the next-best unjudged slice instead of re-scoring the same `top_n`.

    `revisit` (decision 149, on by default): postings whose prepared application the user never
    reviewed are exempted from BOTH suppression filters, so an application prepared while nobody
    was watching is brought up again by the next search. Callers that search repeatedly inside one
    run (the loop's goal-mode hunt) pass `revisit=False` after their first pass so the same
    unreviewed postings aren't re-judged every pass."""
    from . import backends
    from . import discovery_cache

    sources = build_sources(filters, resume, profile) + list(extra_sources or [])
    if not sources:
        return PipelineResult([], 0, 0, ["No sources configured. Add boards to profile/discovery.yaml."])

    # `match()` only judges when the CLI is actually present; fold that into the fingerprint
    # so a keyword-only snapshot (Claude absent) is never reused once Claude is available.
    effective_claude = use_claude and backends.claude_code_available()
    fp = discovery_cache.fingerprint(
        resume, filters, [s.name for s in sources], use_claude=effective_claude, bridge=bridge,
    )

    if cache and not force_fresh and filters.cache_ttl_hours and not extra_sources:
        snap = discovery_cache.load(fp, ttl_hours=filters.cache_ttl_hours)
        if snap is not None:
            from .discovery import canonical_url
            # Never-reviewed applications are exempt from the tracker skip (decision 149).
            seen_canon = _seen_canonical_urls(filters) - _revisit_canonical_urls(revisit)
            matches = snap.matches
            skipped = 0
            if seen_canon:
                before = len(matches)
                matches = [m for m in matches if canonical_url(m.posting.url) not in seen_canon]
                skipped = before - len(matches)
            matches, skipped_shown = _hide_already_shown(matches, only_new, revisit)
            return PipelineResult(
                matches=matches,
                discovered=snap.discovered,
                after_gates=snap.after_gates,
                errors=[],
                skipped_seen=skipped,
                skipped_shown=skipped_shown,
                bridged=snap.bridged,
                non_fillable=list(snap.non_fillable),
                from_cache=True,
                cache_age_seconds=snap.age_seconds,
            )

    postings, errors = discover(sources)
    discovered = len(postings)
    # Per-stage funnel breakdown (diagnose-first): record how many postings survive each stage
    # and which gate dropped how many, so "lots found, few through" points at a specific stage.
    funnel: dict = {"discovered": discovered,
                    "min_skills": filters.min_skills, "top_n": filters.top_n}
    gate_stats: dict = {}
    postings = apply_gates(postings, filters, stats=gate_stats)
    funnel.update(gate_stats)
    funnel["after_gates"] = len(postings)

    # Skip postings already in the tracker so we don't keep re-surfacing/re-applying to the
    # same roles (keyed on the posting URL, which is what the Apply stage records).
    skipped_seen = 0
    seen_canon = _seen_canonical_urls(filters) - _revisit_canonical_urls(revisit)
    if seen_canon:
        from .discovery import canonical_url
        before = len(postings)
        postings = [p for p in postings if canonical_url(p.url) not in seen_canon]
        skipped_seen = before - len(postings)
    funnel["skipped_seen"] = skipped_seen
    funnel["after_seen"] = len(postings)

    # Bridge aggregator hits (Adzuna/Jooble) to their real ATS before matching, so the matcher
    # ranks them on the full JD and Apply lands on the fillable form (decision 032). No-op when
    # no aggregator postings are present, so it adds zero latency to ATS-only runs.
    bridged = 0
    if bridge:
        from .discovery import bridge_aggregator_postings
        postings, bridged = bridge_aggregator_postings(postings)

    # Fillability gate (decision 035): postings on portals Apply can't drive (Workday/iCIMS/
    # unresolved aggregator links) never reach the matcher — no Claude judge tokens spent on
    # them, no dead apply runs. They're returned separately for a future manual queue.
    non_fillable = [p for p in postings if not _is_fillable(p)]
    if non_fillable:
        postings = [p for p in postings if _is_fillable(p)]
    funnel["non_fillable"] = len(non_fillable)

    # Hide openings a previous only_new run already judged BEFORE the judge picks its top_n
    # (decision 146), so a repeat search spends its judge slots on postings that have never been
    # scored. No-op unless only_new.
    postings, skipped_shown = _drop_already_shown(postings, only_new, revisit)
    funnel["already_shown"] = skipped_shown
    funnel["into_matcher"] = len(postings)
    # How many of the survivors are here only because their prepared application was never
    # reviewed (decision 149) — reported so "why is this back?" is answered on the breakdown.
    revisit_canon = _revisit_canonical_urls(revisit)
    if revisit_canon:
        from .discovery import canonical_url
        funnel["revisited"] = sum(1 for p in postings if canonical_url(p.url) in revisit_canon)

    # Steer which top_n postings the judge scores toward past winners (decision 046). Built
    # from the accumulated fit history; a no-op until enough postings have been judged.
    from . import fit_learning
    predictor = fit_learning.predictor()

    matches, match_errors = match(
        resume, postings, top_n=filters.top_n, use_claude=use_claude,
        min_skills=filters.min_skills, on_progress=on_progress, predictor=predictor,
        prerank_n=filters.prerank_n,
    )
    # Keyword pre-filter dropped everything scoring < min_skills; of the survivors, only the
    # top_n get a Claude fit_score (the rest stay keyword-only and can never clear min_fit).
    funnel["keyword_dropped"] = max(0, len(postings) - len(matches))
    funnel["matched"] = len(matches)
    funnel["judged"] = sum(1 for m in matches if m.fit_score is not None)

    # Record this run's judged verdicts so the next run's predictor learns from them
    # (decision 046). Best-effort; judged-only (keyword-only matches carry no fit signal).
    fit_learning.append(m for m in matches if m.fit_score is not None)
    # Also log a one-line run summary for the UI's improvement trend (best/mean fit, how many
    # cleared). Uses the configured min_fit so "cleared" means the same across runs.
    fit_learning.record_run(matches, min_fit=filters.min_fit)

    # Save this live result as the next run's snapshot (decision 037). Only cache the coarse
    # after-gates count and the ranked matches — enough to replay the run without touching the
    # network or Claude. Skipped when `extra_sources` are injected (the fingerprint doesn't
    # capture ad-hoc sources, so caching them could serve a mismatched result).
    # On an only_new run this snapshot holds THIS pass's slice, not every posting the boards
    # returned (the already-judged ones were dropped before matching — decision 146), so a
    # later "re-check without re-scoring" replays the most recent pass.
    if cache and filters.cache_ttl_hours and not extra_sources:
        discovery_cache.save(
            fp, matches, non_fillable,
            discovered=discovered, after_gates=len(postings), bridged=bridged,
        )

    # Record what this run surfaced, AFTER caching so the snapshot keeps the full ranked result
    # (decision 053). Judged matches only (decision 146) — see `_record_shown`.
    _record_shown(matches, only_new)

    return PipelineResult(
        matches=matches,
        discovered=discovered,
        after_gates=len(postings),
        errors=errors + match_errors,
        skipped_seen=skipped_seen,
        skipped_shown=skipped_shown,
        bridged=bridged,
        non_fillable=non_fillable,
        funnel=funnel,
    )


def cached_matches(
    resume: Resume,
    filters: DiscoveryFilters,
    *,
    profile: ApplicationProfile | None = None,
    use_claude: bool = True,
    bridge: bool = True,
) -> list[Match]:
    """The freshest discovery snapshot's full ranked matches — postings + cached Claude fit
    scores (decision 037) — with NO board re-search and NO Claude re-judge. Unlike a normal
    `discover_and_match` cache hit, neither `skip_seen` nor the seen-openings ledger is
    applied, so postings already prepared/applied ARE included: this is for re-preparing an
    already-scored set while reusing its scores (a fit score rarely changes run to run).

    Returns `[]` when no fresh, fingerprint-matching snapshot exists (caching disabled, stale,
    or the résumé/boards/filters changed) — the caller then has nothing cached to re-prepare."""
    if not filters.cache_ttl_hours:
        return []
    sources = build_sources(filters, resume, profile)
    if not sources:
        return []
    from . import discovery_cache
    effective_claude = use_claude and backends.claude_code_available()
    fp = discovery_cache.fingerprint(
        resume, filters, [s.name for s in sources], use_claude=effective_claude, bridge=bridge,
    )
    snap = discovery_cache.load(fp, ttl_hours=filters.cache_ttl_hours)
    return list(snap.matches) if snap else []


def _fmt_match(i: int, m: Match) -> str:
    p = m.posting
    if m.judged_by == "claude":
        head = f"fit {m.fit_score:>3}/100 {'✓ qualified' if m.qualified else '✗ not qualified'}"
    else:
        head = f"kw {m.keyword_score:>2} (unjudged)"
    line = f"{i:>2}. [{head}] {p.company} — {p.title}"
    meta = " · ".join(x for x in [p.location, ("remote" if p.remote else ""), p.compensation] if x)
    out = [line]
    if meta:
        out.append(f"       {meta}")
    if m.dimensions:
        out.append("       " + " · ".join(f"{k} {v}" for k, v in m.dimensions.items()))
    if m.why:
        out.append(f"       why: {m.why}")
    if m.missing:
        out.append(f"       missing: {'; '.join(m.missing[:3])}")
    out.append(f"       {p.url}")
    return "\n".join(out)


def effective_min_fit(filters: DiscoveryFilters) -> tuple[int, str | None]:
    """The min_fit to actually use: the configured value, auto-RAISED when recorded
    outcomes prove a band below it is dead (decision 043 follow-up). Returns
    (value, user-facing note when raised — callers must surface it, silence would read as
    the config being ignored). The user stays in control: the `calibrate_min_fit` filter
    turns this off, and an explicit --min-fit override wins at the call site. Best-effort:
    any tracker problem keeps the configured value."""
    if not filters.calibrate_min_fit:
        return filters.min_fit, None
    from . import tracker
    try:
        rec = tracker.recommended_min_fit(filters.min_fit)
    except Exception:
        return filters.min_fit, None
    if rec is None:
        return filters.min_fit, None
    value, reason = rec
    return value, (f"min_fit raised {filters.min_fit}→{value} by outcome calibration "
                   f"({reason}). Set min_fit ≥ {value} in the Discover settings to make "
                   "this permanent, or turn off its calibration toggle to keep "
                   f"{filters.min_fit}.")


def _print_funnel(res: "PipelineResult", filters: DiscoveryFilters) -> None:
    """Print the per-stage discovery→match funnel with the drop at each stage, so it's obvious
    WHERE postings are lost (diagnose-first). No-op on cache hits (funnel isn't recomputed)."""
    f = res.funnel
    if not f:
        return
    rows: list[tuple[str, int, int]] = []  # (label, remaining, dropped-at-this-stage)

    def add(label: str, remaining: int, dropped: int) -> None:
        rows.append((label, remaining, dropped))

    disc = f.get("discovered", 0)
    add("discovered", disc, 0)
    # Coarse gates, itemized — only the ones that actually dropped something.
    gate_labels = {
        "gate_remote": "remote_only", "gate_title": "title_exclude",
        "gate_company": "company_exclude", "gate_spam": "staffing spam",
        "gate_level": "experience_levels", "gate_salary": "min_salary", "gate_stale": "stale",
        "gate_duplicate": "duplicate reposts",
    }
    for key, label in gate_labels.items():
        if f.get(key):
            add(f"  ✗ {label}", -1, f[key])
    add("after gates", f.get("after_gates", disc), disc - f.get("after_gates", disc))
    if f.get("skipped_seen"):
        add("after skip-seen", f.get("after_seen", 0), f["skipped_seen"])
    if f.get("non_fillable"):
        add("after fillability", f.get("into_matcher", 0), f["non_fillable"])
    add(f"matched (≥{filters.min_skills} skills)", f.get("matched", 0), f.get("keyword_dropped", 0))
    add(f"judged by Claude (top {filters.top_n})", f.get("judged", 0),
        max(0, f.get("matched", 0) - f.get("judged", 0)))

    print("\nSearch funnel (where postings are lost):")
    for label, remaining, dropped in rows:
        drop = f"  −{dropped}" if dropped else ""
        count = "" if remaining < 0 else f"{remaining:>5}"
        print(f"  {count:>5}  {label}{drop}")


def _print_diagnosis(filters: DiscoveryFilters) -> None:
    """Print the fit-learning diagnosis + recommendations (decision 046), best-effort."""
    from . import fit_learning
    try:
        a = fit_learning.analysis(min_fit=filters.min_fit,
                                   current_levels=filters.experience_levels)
    except Exception:
        return
    if a.n_judged == 0:
        return
    print("\nWhat past runs have taught the search:")
    for line in a.lines():
        print("  " + line)
    hist = fit_learning.runs(limit=10)
    if len(hist) >= 2:
        first, last = hist[0]["best_fit"], hist[-1]["best_fit"]
        trend = "▲ improving" if last > first else ("▼ down" if last < first else "▬ flat")
        spark = " → ".join(str(r["best_fit"]) for r in hist)
        print(f"  trend: best fit {first}→{last} over {len(hist)} runs ({trend}); {spark}")


def pick_top(matches: list[Match], *, min_fit: int) -> Match | None:
    """The single match to run in testing mode: the top-ranked one meeting `min_fit`.
    Matches are already sorted best-first (Claude-judged float above keyword-only)."""
    for m in matches:
        if m.fit_score is not None and m.fit_score >= min_fit:
            return m
    # If Claude judged any posting, respect the threshold — return None rather than silently
    # applying to a below-bar match (that bypass is why a 45/100 role got picked at min_fit=50).
    if any(m.fit_score is not None for m in matches):
        return None
    # No Claude judgments at all (e.g. CLI absent) — fall back to the top keyword match.
    return matches[0] if matches else None


def _tailoring_logic_fingerprint() -> str:
    """SHA1 over the SOURCE of every module that determines a tailored PDF's content — the prompt
    and reconstruction (`backends`), catalogue selection (`catalogue`), the length budget
    (`length`), the tailor orchestration (`tailor`), and the PDF renderer (`pdf`). Any edit to any
    of them changes this hash, so the reuse-stamp invalidates automatically on ANY tailoring
    change — no hand-maintained version to forget to bump (the footgun a single `LAYOUT_VERSION`
    int was). Pinned once at import to the code actually running: a source edit takes effect only
    after the process restarts, and the fingerprint then reflects the new code, so a restart + a
    re-prepare (rescan) always re-tailors seen postings with the new logic."""
    import hashlib
    from pathlib import Path

    from . import catalogue, length, pdf, tailor
    h = hashlib.sha1()
    for mod in (backends, catalogue, length, pdf, tailor):
        try:
            h.update(Path(mod.__file__).read_bytes())
        except OSError:
            h.update(b"?")  # unreadable source → distinct-but-stable marker, never a crash
    return h.hexdigest()


# Computed once per process, against the loaded (running) tailoring code — see the docstring.
_TAILORING_LOGIC = _tailoring_logic_fingerprint()


def tailor_stamp(resume: Resume, profile: ApplicationProfile, jd) -> str:
    """A content hash of everything that determines a posting's tailored PDF: the résumé, the
    profile links flowed onto the header (LinkedIn/GitHub/portfolio), the JD the résumé is
    tailored to, AND the tailoring logic itself — a fingerprint of every tailoring module's
    source (`_tailoring_logic_fingerprint`). Including the logic means ANY tailoring change (prompt,
    selection, length budget, reconstruction, or PDF layout) invalidates cached PDFs automatically,
    so a re-prepare re-tailors instead of silently reusing a PDF built by the old code. Stamped
    beside the PDF so a re-prepare can reuse it when nothing that affects it changed — deliberately
    ignores the rest of the profile (e.g. learned screening answers), which the fill re-reads fresh
    but never change the PDF."""
    import hashlib
    import json

    payload = {
        "resume": resume.model_dump(),
        "links": [profile.linkedin_url, profile.github_url, profile.portfolio_url],
        "jd": jd.body or "",
        "logic": _TAILORING_LOGIC,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def tailor_base_stamp(resume: Resume, profile: ApplicationProfile) -> str:
    """The JD-independent half of `tailor_stamp`: the résumé, header links, and tailoring-logic
    fingerprint — everything a tailored PDF depends on EXCEPT the specific JD. Two postings share a
    base stamp iff a résumé tailored for one is built from the exact same inputs + code as one
    tailored for the other. It is the precondition for cross-posting reuse (decision 142): a
    different base résumé, header links, or tailoring code yields a different base stamp, so a PDF
    built by old inputs is never reused across that change."""
    import hashlib
    import json

    payload = {
        "resume": resume.model_dump(),
        "links": [profile.linkedin_url, profile.github_url, profile.portfolio_url],
        "logic": _TAILORING_LOGIC,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class ReuseHit:
    """A previously-tailored PDF whose posting is close enough to reuse for a new one."""

    path: str
    score: float  # 0..1 demanded-skill Jaccard with the new posting
    label: str  # the source posting ("Company — Role"), for the honest reuse note


def find_reusable(resume: Resume, profile: ApplicationProfile, jd, *,
                  threshold: float | None = None, exclude_path: str | None = None) -> "ReuseHit | None":
    """The best previously-tailored PDF from a DIFFERENT posting whose demanded-skill profile is
    'close enough' to `jd` to reuse verbatim, or None (decision 142). 'Close enough' = identical
    base stamp (same résumé / header links / tailoring code) AND demanded-skill Jaccard ≥ threshold
    with a matching knockout profile. Token-free: it reuses the `ats_requirements` extraction the
    tailoring loop already runs, so deciding to skip a Claude call never itself costs one.

    `exclude_path` (this posting's own artifact) is skipped — same-posting reuse is owned by the
    exact-stamp mechanism (dry-run) and decision 069's armed re-tailor; this is strictly *cross*-
    posting reuse."""
    from . import reuse

    thr = reuse.DEFAULT_THRESHOLD if threshold is None else threshold
    if thr <= 0:
        return None
    base = tailor_base_stamp(resume, profile)
    target = reuse.signature(resume, jd)
    skip = _resolve(exclude_path)
    best: ReuseHit | None = None
    for pdf_path, sig in resume_store.all_sigs():
        if sig.get("base_stamp") != base or not pdf_path.is_file() or _resolve(pdf_path) == skip:
            continue
        score = reuse.similarity(target, reuse.JdSignature.from_dict(sig.get("signature") or {}))
        if score >= thr and (best is None or score > best.score):
            best = ReuseHit(str(pdf_path), score, str(sig.get("label") or "a near-identical posting"))
    return best


@dataclass
class UploadHit:
    """A résumé document the user uploaded themselves, good enough to send to a posting as-is."""

    path: str
    score: float  # 0..1 share of the posting's demanded skills the document already shows
    label: str  # the original file name, for the honest provenance note


def find_uploaded_match(resume: Resume, jd, *, threshold: float | None = None) -> "UploadHit | None":
    """The user's own uploaded résumé that best covers what `jd` demands, or None (decision 152).

    A real résumé the user wrote and (usually) already submitted outranks anything we tailored, so
    when one covers ≥ threshold of the posting's demanded skills it is sent verbatim and no
    tailoring runs — see `run_testing_mode`, where this is checked before both reuse paths.
    Token-free: the document's text was extracted at upload time, and scoring it reuses the same
    `ats_requirements` extraction the tailoring loop already runs.

    A posting that demands none of the candidate's skills (usually a JD we failed to scrape) never
    matches — with nothing to cover, `coverage` is trivially 1.0, which is not evidence of fit."""
    from . import ats_requirements, resume_docs, reuse

    thr = reuse.DEFAULT_THRESHOLD if threshold is None else threshold
    if thr <= 0:
        return None
    demanded = reuse.signature(resume, jd).keywords
    if not demanded:
        return None
    best: UploadHit | None = None
    for path, meta in resume_docs.all_docs():
        present = frozenset(
            k.strip().lower()
            for k in ats_requirements.extract(resume, meta.get("text") or "").keywords
            if k.strip())
        score = reuse.coverage(demanded, present)
        if score >= thr and (best is None or score > best.score):
            best = UploadHit(str(path), score, str(meta.get("filename") or path.name))
    return best


def _resolve(path) -> str | None:
    """Absolute, symlink-normalized path string for identity comparison, or None."""
    if not path:
        return None
    try:
        from pathlib import Path
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def tailor_and_render(resume: Resume, profile: ApplicationProfile, jd, company: str, role: str,
                      url: str, *, backend: str = "auto", status_cb=None, on_result=None) -> str:
    """Tailor `resume` to `jd`, render the PDF, write it to the per-posting path with its reuse
    stamp, run the ATS text-layer check, and return the PDF path. This is the tailor+render half
    of `run_testing_mode`, extracted so the Track "Re-run → re-tailor" can regenerate a résumé
    from the saved JD without re-scraping (decision 086). Does NOT write the JD sidecar — the
    caller owns that (run_testing_mode stores it; a re-tailor already has it).

    `on_result(TailorResult)` hands the structured tailoring to the caller as well — the web UI
    renders it (preview + drift warnings) for a tailor-only dry run. Additive: the return value
    is still the PDF path."""
    from . import usage
    from .ats_check import verify_pdf
    from .pdf import render_pdf
    from .tailor import tailor_resume

    def say(step, message):
        print(message)
        if status_cb is not None:
            status_cb(step, message)

    say("tailor", f"▶ Tailoring résumé for: {company} — {role}")
    # Attribute this posting's tailoring tokens to its application row (decision 095). The
    # Claude call inside is tagged activity="tailoring" by the backend.
    with usage.for_posting(url):
        result = tailor_resume(resume, jd, backend=backend)
    if on_result is not None:
        on_result(result)
    print(f"  tailored via {result.backend}" + (f" — {'; '.join(result.warnings)}" if result.warnings else ""))
    for note in result.tailored.relevance_notes:
        print(f"  note: {note}")

    say("pdf", "▶ Exporting tailored résumé to PDF…")
    pdf_resume = resume_with_profile_links(resume, profile)
    pdf_bytes = render_pdf(pdf_resume, result.tailored)
    pdf_path = resume_store.write_pdf(pdf_bytes, company, role, url)
    resume_store.write_stamp(pdf_path, tailor_stamp(resume, profile, jd))
    # Reuse signature (decision 142): the demanded-skill fingerprint + base stamp, so a later
    # posting close enough to this one can reuse this PDF instead of re-tailoring.
    from . import reuse
    resume_store.write_sig(pdf_path, {
        "base_stamp": tailor_base_stamp(resume, profile),
        "signature": reuse.signature(resume, jd).to_dict(),
        "label": f"{company} — {role}",
        "source_url": url,
    })
    print(f"  résumé PDF → {pdf_path}")

    for note in verify_pdf(pdf_bytes, pdf_resume, jd.body or None).notes():
        say("pdf", f"  {note}")
    return pdf_path


def untailored_pdf(resume: Resume, profile: ApplicationProfile, jd, company: str, role: str,
                   url: str) -> tuple[str, str]:
    """The user's own résumé as a PDF for one posting, with NO tailoring and NO Claude call
    (decision 174) — the "Apply without tailoring" path. Returns ``(pdf_path, resume_source)``.

    Prefers the user's best-covering **uploaded** résumé document when they have one (a real file
    they wrote beats anything we render), else renders their **base** résumé verbatim: every
    section, entry, and bullet exactly as stored, in the stored order. Unlike
    `find_uploaded_match`, no coverage threshold applies — the user asked for their résumé as it
    stands, so the decision is theirs, not the matcher's.

    The tailoring sidecars are cleared beside the written PDF because these bytes are not a tailor
    of the current inputs: a later dry-run must not reuse them as this posting's tailored résumé,
    and no other posting may pull them in as a cross-posting reuse source."""
    from pathlib import Path

    from . import resume_docs, reuse
    from .models import TailoredResume
    from .pdf import render_pdf

    docs = resume_docs.all_docs()
    # A threshold just above zero, not zero: `find_uploaded_match` treats `<= 0` as "matching
    # disabled" and returns None. This keeps its ranking (best coverage wins) while accepting any
    # non-zero coverage, which is the point of the no-threshold behaviour described above.
    hit = find_uploaded_match(resume, jd, threshold=1e-9) if docs else None
    if hit is None and docs:
        # No skill signal to rank on (a JD we failed to scrape, or a résumé/JD with no overlap):
        # `all_docs` is path-sorted, so the first is a deterministic choice rather than an
        # arbitrary one. Its file name goes in the provenance label either way.
        path, meta = docs[0]
        hit = UploadHit(str(path), 0.0, str(meta.get("filename") or path.name))

    if hit is not None:
        pdf_path = resume_store.write_pdf(Path(hit.path).read_bytes(), company, role, url)
        source = reuse.uploaded_asis_label(hit.label)
    else:
        verbatim = TailoredResume(
            summary=resume.summary, skills=resume.skills, experience=resume.experience,
            projects=resume.projects, activities=resume.activities,
            education=resume.education, certifications=resume.certifications,
            relevance_notes=["Sent untailored at your request — nothing was selected, reordered, "
                             "or reworded from your base résumé."])
        pdf_bytes = render_pdf(resume_with_profile_links(resume, profile), verbatim)
        pdf_path = resume_store.write_pdf(pdf_bytes, company, role, url)
        source = reuse.UNTAILORED
    resume_store.clear_tailor_sidecars(pdf_path)
    return pdf_path, source


def loop_policy(filters_obj) -> dict:
    """The loop's résumé policy for this run (decision 178), read once at start from
    `profile/discovery.yaml` — the values the "Loop settings" popup writes. Unknown or missing
    values fall back to the pre-178 behaviour (tailor, reuse when the skills match).

    This is the ONLY thing that decides the résumé for an application the loop prepares by itself
    (decision 180) — there is no per-run override, so what the popup says is what the run does."""
    mode = str(getattr(filters_obj, "tailor_mode", "smart") or "smart").lower()
    if mode not in ("smart", "always", "under", "never"):
        mode = "smart"
    try:
        below = int(getattr(filters_obj, "tailor_below_fit", 70))
    except (TypeError, ValueError):
        below = 70
    try:
        thr = float(getattr(filters_obj, "reuse_threshold", 0.9))
    except (TypeError, ValueError):
        thr = 0.9
    return {"mode": mode, "below": below, "reuse_threshold": thr}


def tailor_choice(policy: dict, fit, force_retailor: bool = False) -> tuple[bool, bool]:
    """`(tailor?, force a fresh tailor?)` for ONE posting under the loop's résumé policy:
      - ``never``  → send the résumé as-is, no Claude call.
      - ``always`` → re-tailor from scratch, ignoring every reuse path.
      - ``under``  → tailor only what needs it: below the fit threshold it is tailored; at or
        above it the résumé already fits, so it is sent as-is. An UNSCORED posting is tailored —
        "we don't know" must not silently become "send it untailored".
      - ``smart``  → tailor, letting the reuse paths skip the Claude call when they can."""
    mode = policy["mode"]
    if mode == "never":
        return False, False
    if mode == "always":
        return True, True
    if mode == "under":
        return (fit is None or fit < policy["below"]), False
    return True, bool(force_retailor)


def run_testing_mode(
    resume: Resume,
    match_obj: Match,
    resume_yaml: str,
    profile_path: str,
    *,
    backend: str = "auto",
    headed: bool = True,
    slow_mo: int = 350,
    pause: bool = True,
    status_cb=None,
    hold=None,
    on_filled=None,
    gate=None,
    force_retailor: bool = False,
    tailor: bool = True,
    reuse_threshold: float | None = None,
):
    """Tailor → PDF → apply for ONE posting, watched live. Dry-run (never submits) unless an
    armed SafetyGate is passed (decision 035). Returns the ApplyReport. `status_cb(step,
    message)` receives progress (in addition to printing) so a UI can surface it; `hold` (a
    threading.Event) replaces the terminal review pause for web runs; `on_filled(report)`
    fires the moment filling finishes, before the hold.

    On a **dry run** (no armed gate), if the posting's existing tailored PDF was made from the
    same inputs — its stamp still matches (decision 069 follow-up) — the tailor (a Claude call)
    and PDF render are skipped and that PDF is reused; the fill still runs. A real armed submit
    always re-tailors, so an actual submission never rides on a reused artifact. `force_retailor`
    overrides the reuse and regenerates the résumé even when the stamp matches (the user's
    "re-tailor anyway" escape hatch).

    `tailor=False` (decision 174) skips tailoring entirely — no Claude call and no reuse scan —
    and sends the user's own résumé verbatim via `untailored_pdf`. It is the user's explicit
    "apply without tailoring" choice, so it wins over every other résumé path including
    `force_retailor`.

    `reuse_threshold` (decision 178) overrides how similar two postings' demanded skills must be
    before an earlier tailored résumé is reused instead of a fresh Claude call; None keeps
    `reuse.DEFAULT_THRESHOLD`, and 0 disables cross-posting reuse."""
    from .apply import AnswerResolver, run_apply

    def say(step, message):
        print(message)
        if status_cb is not None:
            status_cb(step, message)

    p = match_obj.posting
    jd = p.to_job_description()
    # Flow the apply-profile links (LinkedIn/GitHub/portfolio) onto the résumé header when it has
    # none, so the submitted PDF carries them (they're stored once, in the apply profile).
    profile = load_profile(profile_path)
    stamp = tailor_stamp(resume, profile, jd)

    # Reuse the existing tailored PDF when this is a dry run and nothing that affects it changed
    # (its stamp still matches). The path is deterministic per posting (decision 029), so we
    # check the stamp beside it directly — no tracker lookup, no Claude tailor, no re-render. A
    # real armed submit re-tailors so it never rides on a reused artifact.
    dry_run = gate is None or not getattr(gate, "armed", False)
    reuse_path = resume_store.path_for(p.company, p.title, p.url)
    # Résumé precedence (decision 152): the user's OWN uploaded résumé outranks every tailored PDF,
    # so it is checked first and short-circuits both reuse scans. `force_retailor` still wins.
    upload_hit = None if (force_retailor or not tailor) else find_uploaded_match(resume, jd)
    reuse_hit = (None if (force_retailor or not tailor or upload_hit is not None)
                 else find_reusable(resume, profile, jd, exclude_path=str(reuse_path),
                                    threshold=reuse_threshold))
    from . import reuse
    if not tailor:
        # The user turned tailoring off for this application (decision 174): send their résumé
        # exactly as it stands. No Claude call, no reuse scan, nothing rewritten or dropped.
        pdf_path, resume_source = untailored_pdf(resume, profile, jd, p.company, p.title, p.url)
        say("tailor", f"▶ No tailoring — {resume_source}")
    elif force_retailor:
        pdf_path = tailor_and_render(resume, profile, jd, p.company, p.title, p.url,
                                     backend=backend, status_cb=status_cb)
        resume_source = reuse.FRESH
    elif upload_hit is not None:
        # The user's own résumé document already covers what this posting screens on — send that
        # real, human-written file instead of a machine-tailored one. Copied into the per-posting
        # slot so Track/archive point at stable bytes; the tailoring sidecars are cleared because
        # these bytes are not a tailor of the current inputs.
        from pathlib import Path
        pdf_path = resume_store.write_pdf(Path(upload_hit.path).read_bytes(), p.company, p.title, p.url)
        resume_store.clear_tailor_sidecars(pdf_path)
        resume_source = reuse.uploaded_reuse_label(upload_hit.label, upload_hit.score)
        say("tailor", f"▶ Sending your uploaded résumé ({upload_hit.label}) as-is — it already "
                      f"covers {upload_hit.score:.0%} of the skills this posting demands, so no "
                      "tailoring was needed. Use 'Re-tailor' to generate a tailored résumé instead.")
    elif dry_run and reuse_path.is_file() and resume_store.read_stamp(reuse_path) == stamp:
        # Exact same-posting reuse (decision 069 follow-up): this posting's own PDF, unchanged
        # since the last dry-run. Dry-run only — an armed submit always re-tailors here.
        pdf_path = str(reuse_path)
        resume_source = reuse.exact_reuse_label()
        say("tailor", f"▶ Reusing tailored résumé (unchanged since last dry-run): {p.company} — {p.title}")
    elif reuse_hit is not None:
        # Cross-posting reuse (decision 142): a DIFFERENT posting we already tailored for demands
        # essentially the same skills, so its PDF fits this one — copy it and skip the Claude call.
        # Applies on armed submits too (user's choice); `force_retailor` is the escape hatch above.
        from pathlib import Path
        pdf_path = resume_store.write_pdf(Path(reuse_hit.path).read_bytes(), p.company, p.title, p.url)
        resume_store.write_stamp(pdf_path, stamp)
        # Carry the SOURCE's content signature (not this JD's) so a later posting compares against
        # the résumé's TRUE skill profile — keeps reuse chains from drifting off the original.
        resume_store.write_sig(pdf_path, {**(resume_store.read_sig(reuse_hit.path) or {}), "source_url": p.url})
        resume_source = reuse.similar_reuse_label(reuse_hit.label, reuse_hit.score)
        say("tailor", f"▶ Reused a résumé tailored to a near-identical posting "
                      f"({reuse_hit.label}, {reuse_hit.score:.0%} skill-demand match) — skipped "
                      f"re-tailoring to save tokens. Use 'Re-tailor' to force a fresh pass.")
    else:
        resume_source = reuse.FRESH
        # Stable, git-ignored, per-posting path (decision 029) — not $TMPDIR, which macOS
        # purges out from under the Track row's resume_path. The ATS text-layer check (decision
        # 043) runs inside the helper.
        pdf_path = tailor_and_render(resume, profile, jd, p.company, p.title, p.url,
                                     backend=backend, status_cb=status_cb)
    # Save the JD beside the PDF (both branches) so a later Track "Re-run → re-tailor" can
    # regenerate offline against it (decision 086).
    resume_store.write_jd(pdf_path, jd)

    apply_url = p.apply_url or p.url
    say("apply", f"▶ DRY-RUN apply (watch it fill; never submits): {apply_url}")
    generate = backends.claude_code_available()
    # Salary-expectation fallback (decision 039): if the posting advertises a band, the resolver
    # fills its midpoint (decision 038) and we opportunistically re-validate any cached estimate
    # for this role against that real band; otherwise pre-compute the dynamic market estimate
    # (Claude + Adzuna, cached) so the resolver never falls back to the static desired_salary.
    from . import salary, usage
    band = salary.advertised_band(p.compensation or None, jd.body or None)
    market = None
    if band:
        salary.validate_against_band(p.title, p.location, band)
    else:
        say("apply", "  no pay band advertised — resolving market salary estimate…")
        # Attribute the market-estimate Claude call to this posting (decision 095); it's tagged
        # activity="salary" by salary.estimate.
        with usage.for_posting(p.url):
            market = salary.estimate(
                p.title, p.location, profile.years_experience,
                app_id=os.environ.get("ADZUNA_APP_ID", ""),
                app_key=os.environ.get("ADZUNA_APP_KEY", ""),
            )
        say("apply", f"  salary expectation → {market:,} (market estimate)" if market is not None
            else f"  salary expectation → {profile.desired_salary or 'unset'} (stored; no estimate available)")
    resolver = AnswerResolver(
        resume=load_resume(resume_yaml),
        profile=profile,
        enable_generation=generate,
        company=p.company or None,
        jd=jd.body or None,
        pay=p.compensation or None,
        market_salary=str(market) if market is not None else None,
    )
    # Basic info for the Track record comes from the discovered posting (reliable), keyed on
    # the posting URL for dedup — not scraped from the ATS form page.
    meta = {
        "company": p.company, "role": p.title, "location": p.location,
        "remote": ("remote" if p.remote else ("on-site" if p.remote is False else "")),
        "pay": p.compensation, "source_url": p.url,
        # Which résumé this run used — freshly tailored vs reused (decision 144) — persisted onto
        # the Track row and surfaced on the report for the review panel / loop notification.
        "resume_source": resume_source,
        # The judge's verdict at apply time — the calibration report correlates it
        # with outcomes (decision 043).
        "fit_score": match_obj.fit_score,
        # Not a tracker column: the posting text, snapshotted by the per-application
        # archive (decision 043) so a dead posting stays reconstructable.
        "jd_body": jd.body or "",
    }
    return run_apply(
        apply_url, pdf_path, resolver,
        headed=headed, pause=pause, slow_mo=slow_mo,
        profile_path=profile_path, hold=hold, on_filled=on_filled, meta=meta, gate=gate,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Qualification-driven job discovery: find postings that fit you, "
        "then (--apply-first) watch the top match go tailor → PDF → dry-run apply."
    )
    parser.add_argument("--resume", default="profile/resume.yaml", help="Résumé YAML.")
    parser.add_argument("--profile", default="profile/application_profile.yaml", help="Apply-profile YAML.")
    parser.add_argument("--filters", default="profile/discovery.yaml", help="Discovery filters YAML.")
    parser.add_argument("--no-claude", action="store_true",
                        help="Rank by keyword only; skip the Claude fit judge (fast, offline).")
    parser.add_argument("--limit", type=int, default=20, help="How many ranked matches to print.")
    parser.add_argument("--apply-first", action="store_true",
                        help="TESTING MODE: after ranking, run the full tailor→PDF→dry-run "
                        "apply loop on the single top match (headed, never submits).")
    parser.add_argument("--min-fit", type=int, default=None,
                        help="Testing mode: minimum Claude fit score (0-100) to pick a match. "
                        "Defaults to min_fit in your discovery filters.")
    parser.add_argument("--backend", default="auto", choices=["auto", "claude-code", "rules"],
                        help="Tailoring backend for testing mode.")
    parser.add_argument("--headless", action="store_true", help="Testing mode: no visible browser.")
    parser.add_argument("--no-pause", action="store_true",
                        help="Testing mode: don't leave the browser open for review at the end.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run even if profile/safety.yaml is armed.")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore the cached discovery snapshot and re-search every board "
                        "(re-judges with Claude). Default reuses a snapshot younger than "
                        "cache_ttl_hours in your filters.")
    parser.add_argument("--all", dest="show_all", action="store_true",
                        help="Show every match, including openings a previous run already showed "
                        "you. Default lists only NEW openings since your last run (decision 053).")
    parser.add_argument("--reset-seen", action="store_true",
                        help="Forget which openings were already shown, then run — every match "
                        "counts as new again.")
    args = parser.parse_args(argv)

    if args.reset_seen:
        from . import discovery_seen
        print("Reset seen-openings ledger." if discovery_seen.clear()
              else "Seen-openings ledger was already empty.")

    resume = load_resume(args.resume)
    filters = load_filters(args.filters)
    try:
        profile = load_profile(args.profile)
    except Exception:
        profile = ApplicationProfile()

    if not filters.boards and not (filters.adzuna.app_id or os.environ.get("ADZUNA_APP_ID")):
        print("No target boards configured in", args.filters)
        print("Add some, e.g.:\n  boards:\n    - {ats: greenhouse, token: stripe}\n"
              "    - {ats: lever, token: cin7}\n    - {ats: ashby, token: Ramp}")
        return 1

    use_claude = not args.no_claude
    if use_claude and not backends.claude_code_available():
        print("Note: Claude Code CLI not found — ranking by keyword only. Sign in with `claude` to judge fit.\n")

    if args.fresh:
        print(f"Discovering fresh from {len(filters.boards)} board(s)…")
    else:
        print(f"Discovering from {len(filters.boards)} board(s) (reusing a fresh cache if present)…")
    res = discover_and_match(resume, filters, profile=profile, use_claude=use_claude,
                             force_fresh=args.fresh, only_new=not args.show_all)
    seen_note = f" (skipped {res.skipped_seen} already in tracker)" if res.skipped_seen else ""
    shown_note = (f" (hid {res.skipped_shown} already shown — pass --all to see them)"
                  if res.skipped_shown else "")
    bridge_note = f" (bridged {res.bridged} aggregator hit(s) to a fillable ATS)" if res.bridged else ""
    manual_note = (f" (set aside {len(res.non_fillable)} on portals ApplicationBot can't fill yet"
                   " — e.g. Workday/iCIMS)" if res.non_fillable else "")
    if res.from_cache:
        mins = int((res.cache_age_seconds or 0) // 60)
        age = f"{mins} min ago" if mins < 90 else f"{mins // 60}h ago"
        print(f"→ Reused cached discovery (saved {age}; no board search, no Claude judging — "
              "pass --fresh to re-search).")
    print(f"Discovered {res.discovered} postings → {res.after_gates} after gates{seen_note}{bridge_note}{manual_note} → "
          f"{len(res.matches)} matched ≥{filters.min_skills} skill(s){shown_note}.")
    for e in res.errors:
        print(f"  ! {e}")

    _print_funnel(res, filters)

    if not res.matches and res.skipped_shown:
        print("\nEvery match this run was already shown to you. Pass --all to see them again, "
              "or --reset-seen to start over.")

    print(f"\nTop {min(args.limit, len(res.matches))} qualification matches:\n")
    for i, m in enumerate(res.matches[:args.limit], 1):
        print(_fmt_match(i, m))

    # What the feedback loop has learned so far, and what it recommends (decision 046).
    _print_diagnosis(filters)

    if not args.apply_first:
        if res.matches:
            print("\n(Run again with --apply-first to watch the top match go end-to-end in dry-run.)")
        return 0

    if args.min_fit is not None:
        min_fit = args.min_fit  # explicit override — calibration never second-guesses it
    else:
        min_fit, calib_note = effective_min_fit(filters)
        if calib_note:
            print(f"\n→ {calib_note}")
    top = pick_top(res.matches, min_fit=min_fit)
    if top is None:
        print(f"\nNo match met min-fit {min_fit}; nothing to apply to.")
        return 1

    # Safety switch (decision 035): armed state comes from profile/safety.yaml; the KILL
    # file halts submission; --dry-run overrides both to disarmed.
    from .safety import load_gate
    gate = None if args.dry_run else load_gate()
    if gate is not None and gate.armed:
        print("\n⚠ ARMED (profile/safety.yaml) — this run WILL SUBMIT if all required fields "
              "resolve. Create profile/KILL or pass --dry-run to stop.")

    run_testing_mode(
        resume, top, args.resume, args.profile,
        backend=args.backend, headed=not args.headless, pause=not args.no_pause,
        gate=gate,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

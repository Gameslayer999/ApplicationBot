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

    def __post_init__(self):
        if self.non_fillable is None:
            self.non_fillable = []


def _is_fillable(p) -> bool:
    """Can the Apply stage drive this posting's form? True for the six public-API ATSs, for
    **Workday** (the deterministic adapter, decision 059 — M1 dry-run), and for aggregator hits
    not yet bridge-resolved (which redirect to one of them or get marked auto_applyable=False by
    the bridge). iCIMS / unresolved links are not."""
    from .discovery import _AGGREGATOR_ATS, ATS_SOURCES
    if p.extra.get("auto_applyable") is False:
        return False
    return p.ats in ATS_SOURCES or p.ats in _AGGREGATOR_ATS or p.ats == "workday"


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


def _hide_already_shown(matches: list[Match], only_new: bool) -> tuple[list[Match], int]:
    """The seen-openings ledger (decision 053): when `only_new`, drop matches a previous
    preview already surfaced, then record the survivors so the NEXT preview hides them too.
    Returns (matches_to_show, n_hidden). A no-op (and never records) when `only_new` is False,
    so the runner and other non-preview callers keep their exact current behaviour."""
    if not only_new:
        return matches, 0
    from . import discovery_seen
    from .discovery import canonical_url
    seen = discovery_seen.seen_urls()
    hidden = 0
    if seen:
        before = len(matches)
        matches = [m for m in matches if canonical_url(m.posting.url) not in seen]
        hidden = before - len(matches)
    discovery_seen.record(m.posting.url for m in matches)
    return matches, hidden


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
    `skip_seen`; off by default so the autonomous runner is unaffected."""
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
            seen_canon = _seen_canonical_urls(filters)
            matches = snap.matches
            skipped = 0
            if seen_canon:
                before = len(matches)
                matches = [m for m in matches if canonical_url(m.posting.url) not in seen_canon]
                skipped = before - len(matches)
            matches, skipped_shown = _hide_already_shown(matches, only_new)
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
    postings = apply_gates(postings, filters)

    # Skip postings already in the tracker so we don't keep re-surfacing/re-applying to the
    # same roles (keyed on the posting URL, which is what the Apply stage records).
    skipped_seen = 0
    seen_canon = _seen_canonical_urls(filters)
    if seen_canon:
        from .discovery import canonical_url
        before = len(postings)
        postings = [p for p in postings if canonical_url(p.url) not in seen_canon]
        skipped_seen = before - len(postings)

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

    # Steer which top_n postings the judge scores toward past winners (decision 046). Built
    # from the accumulated fit history; a no-op until enough postings have been judged.
    from . import fit_learning
    predictor = fit_learning.predictor()

    matches, match_errors = match(
        resume, postings, top_n=filters.top_n, use_claude=use_claude,
        min_skills=filters.min_skills, on_progress=on_progress, predictor=predictor,
    )

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
    if cache and filters.cache_ttl_hours and not extra_sources:
        discovery_cache.save(
            fp, matches, non_fillable,
            discovered=discovered, after_gates=len(postings), bridged=bridged,
        )

    # Hide openings a previous preview already showed, AFTER caching the full result above so
    # the snapshot keeps everything (decision 053). No-op unless only_new.
    matches, skipped_shown = _hide_already_shown(matches, only_new)

    return PipelineResult(
        matches=matches,
        discovered=discovered,
        after_gates=len(postings),
        errors=errors + match_errors,
        skipped_seen=skipped_seen,
        skipped_shown=skipped_shown,
        bridged=bridged,
        non_fillable=non_fillable,
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


def tailor_stamp(resume: Resume, profile: ApplicationProfile, jd) -> str:
    """A content hash of everything that determines a posting's tailored PDF: the résumé, the
    profile links flowed onto the header (LinkedIn/GitHub/portfolio), the JD the résumé is
    tailored to, AND the tailoring logic itself — the Claude prompt (`backends.SYSTEM_PROMPT`)
    and the PDF layout (`pdf.LAYOUT_VERSION`). Including the logic means a prompt or layout
    change invalidates cached PDFs automatically, so a dry run re-tailors instead of silently
    reusing a PDF built by the old prompt/layout. Stamped beside the PDF so a re-prepare can
    reuse it when nothing that affects it changed — deliberately ignores the rest of the profile
    (e.g. learned screening answers), which the fill re-reads fresh but never change the PDF."""
    import hashlib
    import json

    from . import pdf as _pdf
    payload = {
        "resume": resume.model_dump(),
        "links": [profile.linkedin_url, profile.github_url, profile.portfolio_url],
        "jd": jd.body or "",
        "prompt": backends.SYSTEM_PROMPT,
        "layout": _pdf.LAYOUT_VERSION,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


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
    "re-tailor anyway" escape hatch)."""
    from .apply import AnswerResolver, run_apply
    from .pdf import render_pdf
    from .tailor import tailor_resume

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
    if not force_retailor and dry_run and reuse_path.is_file() and resume_store.read_stamp(reuse_path) == stamp:
        pdf_path = str(reuse_path)
        say("tailor", f"▶ Reusing tailored résumé (unchanged since last dry-run): {p.company} — {p.title}")
    else:
        say("tailor", f"▶ Tailoring résumé for: {p.company} — {p.title}")
        result = tailor_resume(resume, jd, backend=backend)
        print(f"  tailored via {result.backend}" + (f" — {'; '.join(result.warnings)}" if result.warnings else ""))
        for note in result.tailored.relevance_notes:
            print(f"  note: {note}")

        say("pdf", "▶ Exporting tailored résumé to PDF…")
        # Stable, git-ignored, per-posting path (decision 029) — not $TMPDIR, which macOS
        # purges out from under the Track row's resume_path.
        pdf_resume = resume_with_profile_links(resume, profile)
        pdf_bytes = render_pdf(pdf_resume, result.tailored)
        pdf_path = resume_store.write_pdf(pdf_bytes, p.company, p.title, p.url)
        resume_store.write_stamp(pdf_path, stamp)
        print(f"  résumé PDF → {pdf_path}")

        # ATS text-layer check (decision 043): what an ATS parser would actually see in this
        # PDF — readability of name/email/phone + which JD-requested skills survived tailoring.
        from .ats_check import verify_pdf
        for note in verify_pdf(pdf_bytes, pdf_resume, jd.body or None).notes():
            say("pdf", f"  {note}")

    apply_url = p.apply_url or p.url
    say("apply", f"▶ DRY-RUN apply (watch it fill; never submits): {apply_url}")
    generate = backends.claude_code_available()
    # Salary-expectation fallback (decision 039): if the posting advertises a band, the resolver
    # fills its midpoint (decision 038) and we opportunistically re-validate any cached estimate
    # for this role against that real band; otherwise pre-compute the dynamic market estimate
    # (Claude + Adzuna, cached) so the resolver never falls back to the static desired_salary.
    from . import salary
    band = salary.advertised_band(p.compensation or None, jd.body or None)
    market = None
    if band:
        salary.validate_against_band(p.title, p.location, band)
    else:
        say("apply", "  no pay band advertised — resolving market salary estimate…")
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

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
    bridged: int = 0  # aggregator hits resolved to a fillable ATS (decision 032)


def discover_and_match(
    resume: Resume,
    filters: DiscoveryFilters,
    *,
    profile: ApplicationProfile | None = None,
    extra_sources: list[Source] | None = None,
    use_claude: bool = True,
    bridge: bool = True,
    on_progress=None,
) -> PipelineResult:
    """The reusable core: discover → gate → skip-already-seen → bridge → qualification-match.
    No side effects. `on_progress(done, total)` reports Claude-judging progress for a UI.
    `bridge` resolves aggregator (Adzuna/Jooble) redirect links to their real ATS so those
    hits become auto-applyable (a no-op when no aggregator postings are present)."""
    sources = build_sources(filters, resume, profile) + list(extra_sources or [])
    if not sources:
        return PipelineResult([], 0, 0, ["No sources configured. Add boards to profile/discovery.yaml."])

    postings, errors = discover(sources)
    discovered = len(postings)
    postings = apply_gates(postings, filters)

    # Skip postings already in the tracker so we don't keep re-surfacing/re-applying to the
    # same roles (keyed on the posting URL, which is what the Apply stage records).
    skipped_seen = 0
    if filters.skip_seen:
        from . import tracker
        try:
            seen = tracker.seen_source_urls()
        except Exception:
            seen = set()
        if seen:
            before = len(postings)
            postings = [p for p in postings if p.url not in seen]
            skipped_seen = before - len(postings)

    # Bridge aggregator hits (Adzuna/Jooble) to their real ATS before matching, so the matcher
    # ranks them on the full JD and Apply lands on the fillable form (decision 032). No-op when
    # no aggregator postings are present, so it adds zero latency to ATS-only runs.
    bridged = 0
    if bridge:
        from .discovery import bridge_aggregator_postings
        postings, bridged = bridge_aggregator_postings(postings)

    matches, match_errors = match(
        resume, postings, top_n=filters.top_n, use_claude=use_claude,
        min_skills=filters.min_skills, on_progress=on_progress,
    )
    return PipelineResult(
        matches=matches,
        discovered=discovered,
        after_gates=len(postings),
        errors=errors + match_errors,
        skipped_seen=skipped_seen,
        bridged=bridged,
    )


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
    if m.why:
        out.append(f"       why: {m.why}")
    if m.missing:
        out.append(f"       missing: {'; '.join(m.missing[:3])}")
    out.append(f"       {p.url}")
    return "\n".join(out)


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
):
    """Tailor → PDF → dry-run apply for ONE posting, watched live. Never submits. Returns the
    ApplyReport. `status_cb(step, message)` receives progress (in addition to printing) so a UI
    can surface it; `hold` (a threading.Event) replaces the terminal review pause for web runs;
    `on_filled(report)` fires the moment filling finishes, before the hold."""
    from .apply import AnswerResolver, run_apply
    from .pdf import render_pdf
    from .tailor import tailor_resume

    def say(step, message):
        print(message)
        if status_cb is not None:
            status_cb(step, message)

    p = match_obj.posting
    jd = p.to_job_description()

    say("tailor", f"▶ Tailoring résumé for: {p.company} — {p.title}")
    result = tailor_resume(resume, jd, backend=backend)
    print(f"  tailored via {result.backend}" + (f" — {'; '.join(result.warnings)}" if result.warnings else ""))
    for note in result.tailored.relevance_notes:
        print(f"  note: {note}")

    say("pdf", "▶ Exporting tailored résumé to PDF…")
    # Stable, git-ignored, per-posting path (decision 029) — not $TMPDIR, which macOS
    # purges out from under the Track row's resume_path.
    # Flow the apply-profile links (LinkedIn/GitHub/portfolio) onto the résumé header when it has
    # none, so the submitted PDF carries them (they're stored once, in the apply profile).
    profile = load_profile(profile_path)
    pdf_resume = resume_with_profile_links(resume, profile)
    pdf_path = resume_store.write_pdf(render_pdf(pdf_resume, result.tailored), p.company, p.title, p.url)
    print(f"  résumé PDF → {pdf_path}")

    apply_url = p.apply_url or p.url
    say("apply", f"▶ DRY-RUN apply (watch it fill; never submits): {apply_url}")
    generate = backends.claude_code_available()
    resolver = AnswerResolver(
        resume=load_resume(resume_yaml),
        profile=profile,
        enable_generation=generate,
        company=p.company or None,
        jd=jd.body or None,
    )
    # Basic info for the Track record comes from the discovered posting (reliable), keyed on
    # the posting URL for dedup — not scraped from the ATS form page.
    meta = {
        "company": p.company, "role": p.title, "location": p.location,
        "remote": ("remote" if p.remote else ("on-site" if p.remote is False else "")),
        "pay": p.compensation, "source_url": p.url,
    }
    return run_apply(
        apply_url, pdf_path, resolver,
        headed=headed, pause=pause, slow_mo=slow_mo,
        profile_path=profile_path, hold=hold, on_filled=on_filled, meta=meta,
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
    args = parser.parse_args(argv)

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

    print(f"Discovering from {len(filters.boards)} board(s)…")
    res = discover_and_match(resume, filters, profile=profile, use_claude=use_claude)
    seen_note = f" (skipped {res.skipped_seen} already in tracker)" if res.skipped_seen else ""
    bridge_note = f" (bridged {res.bridged} aggregator hit(s) to a fillable ATS)" if res.bridged else ""
    print(f"Discovered {res.discovered} postings → {res.after_gates} after gates{seen_note}{bridge_note} → "
          f"{len(res.matches)} matched ≥{filters.min_skills} skill(s).")
    for e in res.errors:
        print(f"  ! {e}")

    print(f"\nTop {min(args.limit, len(res.matches))} qualification matches:\n")
    for i, m in enumerate(res.matches[:args.limit], 1):
        print(_fmt_match(i, m))

    if not args.apply_first:
        if res.matches:
            print("\n(Run again with --apply-first to watch the top match go end-to-end in dry-run.)")
        return 0

    min_fit = args.min_fit if args.min_fit is not None else filters.min_fit
    top = pick_top(res.matches, min_fit=min_fit)
    if top is None:
        print(f"\nNo match met min-fit {min_fit}; nothing to apply to.")
        return 1
    run_testing_mode(
        resume, top, args.resume, args.profile,
        backend=args.backend, headed=not args.headless, pause=not args.no_pause,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

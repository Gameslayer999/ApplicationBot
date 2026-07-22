"""Autonomous runner (decision 035): apply to EVERY cleared match, not just the top one.

Dry-run by default (Guideline #3): each application is tailored, filled, and recorded but
never submitted unless profile/safety.yaml arms the run. Blockers are recorded outcomes,
not prompts (decision 016's exception-queue model): an application that can't be completed
is logged to the tracker and the loop moves on. The kill file (profile/KILL) stops the
loop between applications AND blocks any submit mid-application (checked again by the
SafetyGate immediately before every click).

Run:
    python -m applicationbot.runner              # dry-run the whole cleared queue, headless
    python -m applicationbot.runner --max 5      # bound this run to 5 applications
    python -m applicationbot.runner --headed     # watch it work
    python -m applicationbot.runner --continuous # poll forever: cycle, wait --interval min, repeat
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .backends import ClaudeAuthError, ClaudeRateLimitError, ClaudeUnavailableError
from .matching import Match
from .safety import SafetyGate


@dataclass
class Outcome:
    company: str
    role: str
    url: str
    fit: Optional[int]
    result: str  # submitted | unconfirmed | blocked | dry-run | failed
    detail: str = ""


@dataclass
class RunnerResult:
    outcomes: list[Outcome] = field(default_factory=list)
    stopped_reason: str = ""  # queue exhausted / kill switch / cap / max / Claude failure

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for o in self.outcomes:
            c[o.result] = c.get(o.result, 0) + 1
        return c

    def summary(self) -> str:
        parts = [f"{n} {k}" for k, n in sorted(self.counts().items())]
        return (f"Runner finished — {len(self.outcomes)} application(s): "
                + (", ".join(parts) if parts else "none") + f". Stopped: {self.stopped_reason}")


def cleared_queue(matches: list[Match], min_fit: int) -> list[Match]:
    """Only Claude-judged matches at/above the fit bar. The runner NEVER auto-applies on
    keyword rank alone — an unjudged queue (Claude absent) yields an empty queue rather
    than applying blind (closes the min_fit bypass for autonomous runs)."""
    return [m for m in matches if m.fit_score is not None and m.fit_score >= min_fit]


def _wait_for_reset(seconds: int, gate: SafetyGate, _sleep) -> bool:
    """Sleep `seconds` in ≤30s chunks, polling the kill file between chunks.
    Returns True if the full wait completed, False if the kill file appeared."""
    remaining = seconds
    while remaining > 0:
        if gate.kill_file.exists():
            return False
        chunk = min(30, remaining)
        _sleep(chunk)
        remaining -= chunk
    return not gate.kill_file.exists()


def run_queue(
    queue: list[Match],
    apply_one,
    gate: SafetyGate,
    *,
    max_applications: Optional[int] = None,
    say=None,
    rate_limit_wait_s: int = 900,
    max_rate_limit_waits: int = 3,
    _sleep=time.sleep,
) -> RunnerResult:
    """The autonomous loop. `apply_one(match) -> ApplyReport` does one application
    (tailor → PDF → fill → maybe submit); this function owns ordering, quotas, the
    kill switch, and failure isolation. Injected `apply_one` keeps it fully testable
    without a browser or a Claude call.

    A Claude usage-cap/rate-limit error pauses the run for `rate_limit_wait_s`
    (kill-file-abortable) then retries the SAME match, at most `max_rate_limit_waits`
    times per run. `_sleep` is injectable for tests."""
    say = say or (lambda msg: print(msg))
    out = RunnerResult()
    rate_waits = 0

    for m in queue:
        if gate.kill_file.exists():
            out.stopped_reason = f"kill switch — {gate.kill_file} exists; run halted"
            break
        if max_applications is not None and len(out.outcomes) >= max_applications:
            out.stopped_reason = f"per-run limit reached ({max_applications})"
            break
        if gate.armed and gate.submitted_this_run >= gate.max_submissions_per_run:
            out.stopped_reason = (f"submission cap reached ({gate.max_submissions_per_run}) — "
                                  "remaining queue left for the next run")
            break

        p = m.posting
        say(f"[{len(out.outcomes) + 1}/{len(queue)}] {p.company} — {p.title} (fit {m.fit_score})")
        report = None
        while True:  # retries the same match after a usage-limit wait
            try:
                report = apply_one(m)
                break
            except ClaudeRateLimitError as e:
                # Usage cap / rate limit: pause-and-resume — no Outcome is recorded
                # unless the retried match finally succeeds or fails.
                rate_waits += 1
                if rate_waits > max_rate_limit_waits:
                    out.stopped_reason = (
                        f"Claude usage limit persisted through {max_rate_limit_waits} "
                        f"wait(s) — stopping; remaining queue left for the next run. Wait "
                        f"for your usage window to reset, then rerun "
                        f"`python -m applicationbot.runner`. Detail: {e}")
                    break
                mins = max(1, round(rate_limit_wait_s / 60))
                say(f"  Claude usage limit hit — waiting {mins} min "
                    f"({rate_waits}/{max_rate_limit_waits}) then retrying this "
                    f"application. Create {gate.kill_file} to stop now.")
                if not _wait_for_reset(rate_limit_wait_s, gate, _sleep):
                    out.stopped_reason = (f"kill switch — {gate.kill_file} appeared during "
                                          "the usage-limit wait; run halted")
                    break
            except ClaudeAuthError as e:
                msg = f"{type(e).__name__}: {e}"
                out.outcomes.append(Outcome(p.company, p.title, p.url, m.fit_score, "failed", msg))
                say(f"  ✗ failed: {msg}")
                out.stopped_reason = ("Claude sign-in required — run `claude` in a terminal "
                                      f"and use /login, then rerun the runner. Detail: {e}")
                break
            except ClaudeUnavailableError as e:
                msg = f"{type(e).__name__}: {e}"
                out.outcomes.append(Outcome(p.company, p.title, p.url, m.fit_score, "failed", msg))
                say(f"  ✗ failed: {msg}")
                out.stopped_reason = f"Claude call failed — stopping the queue: {msg}"
                break
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                out.outcomes.append(Outcome(p.company, p.title, p.url, m.fit_score, "failed", msg))
                say(f"  ✗ failed: {msg}")
                # A dead Claude CLI raised outside backends still fails every subsequent
                # tailor — keep the substring fallback stop for those RuntimeErrors.
                if "claude" in msg.lower():
                    out.stopped_reason = f"Claude call failed — stopping the queue: {msg}"
                break
        if out.stopped_reason:
            break
        if report is None:
            continue  # isolated failure — move on to the next match

        if report.submitted and report.submit_state == "submitted":
            result, detail = "submitted", report.confirmation
        elif report.submit_state in ("unconfirmed", "blocked"):
            result = report.submit_state
            detail = report.confirmation or "; ".join(report.blockers)
        else:
            result = "dry-run"
            detail = f"{len(report.filled)} filled, {len(report.skipped)} need attention"
        out.outcomes.append(Outcome(p.company, p.title, p.url, m.fit_score, result, detail))
        say(f"  → {result}: {detail}")

    if not out.stopped_reason:
        out.stopped_reason = "queue exhausted"
    return out


def _report_parked(say=print) -> None:
    """After a cycle, name the applications parked on a user-resolvable block (decision 049)
    so a blocked fill is a one-step fix, not a lost run. Best-effort: a tracker/DB hiccup
    never breaks the run."""
    try:
        from . import parking, tracker
        parked = tracker.parked_applications()
    except Exception:
        return
    if not parked:
        return
    # Header stays neutral and each line carries its OWN verb: not every parked application is
    # waiting on the user — a bot-walled one (decision 077) is waiting on the site, and telling
    # the user to "resolve" it would send them looking for a fix that doesn't exist (UI Principle #4).
    say(f"\n{len(parked)} application(s) parked, not submitted — open the Discover tab:")
    for a in parked[:10]:
        d = parking.describe(a.get("blocked_kind", ""), a.get("blocked_detail", ""))
        who = f"{a['company']} — {a['role']}".strip(" —") or a.get("source_url", "?")
        verb = f" → {d['action']}" if d["action"] else ""
        say(f"  - {who}: {d['label']}{verb}" + (f" ({d['detail']})" if d["detail"] else ""))
    if len(parked) > 10:
        say(f"  … and {len(parked) - 10} more.")


def continuous_loop(run_cycle, gate: SafetyGate, *, interval_s: int,
                    say=None, _sleep=time.sleep) -> str:
    """Poll forever: run one cycle, wait `interval_s` (kill-file-abortable), repeat. `run_cycle()`
    returns 'ok'|'empty'|'stop'; a 'stop' (fatal, e.g. Claude sign-in) ends the loop immediately —
    waiting won't fix it. Returns why it ended: 'stop' or 'kill'. Injected `run_cycle`/`_sleep`
    keep it testable without network, browser, or real waiting."""
    say = say or (lambda msg: print(msg))
    cycle = 0
    while not gate.kill_file.exists():
        cycle += 1
        say(f"\n=== Cycle {cycle} ===")
        if run_cycle() == "stop":
            return "stop"
        say(f"Cycle {cycle} done — waiting {max(1, round(interval_s / 60))} min. "
            f"Create {gate.kill_file} to stop.")
        if not _wait_for_reset(interval_s, gate, _sleep):
            return "kill"
    return "kill"


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    from . import backends
    from .apply_profile import ApplicationProfile, load_profile
    from .filters import load_filters
    from .pipeline import discover_and_match, run_testing_mode
    from .resume import load_resume
    from .safety import load_gate

    parser = argparse.ArgumentParser(
        description="Autonomous runner: discover → judge → tailor → fill EVERY cleared match "
        "(dry-run unless profile/safety.yaml is armed)."
    )
    parser.add_argument("--resume", default="profile/resume.yaml")
    parser.add_argument("--profile", default="profile/application_profile.yaml")
    parser.add_argument("--filters", default="profile/discovery.yaml")
    parser.add_argument("--backend", default="auto", choices=["auto", "claude-code", "rules"])
    parser.add_argument("--min-fit", type=int, default=None,
                        help="Minimum Claude fit score; defaults to min_fit in your filters.")
    parser.add_argument("--max", type=int, default=None, help="Cap applications this run.")
    parser.add_argument("--headed", action="store_true", help="Watch the browser work.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run even if profile/safety.yaml is armed.")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore the cached discovery snapshot and re-search every board.")
    parser.add_argument("--continuous", action="store_true",
                        help="Keep polling for new matching postings: run a cycle, wait "
                        "--interval minutes, repeat. Stop with Ctrl-C or by creating profile/KILL.")
    parser.add_argument("--interval", type=int, default=30,
                        help="Minutes to wait between cycles in --continuous mode (default 30). "
                        "Pair with --fresh to re-search boards every cycle; otherwise cycles "
                        "reuse the discovery cache until its TTL expires.")
    args = parser.parse_args(argv)

    if not backends.claude_code_available():
        print("The runner needs the Claude fit judge — it never auto-applies on keyword rank "
              "alone. Sign in with `claude`, or use `python -m applicationbot.pipeline` to list "
              "keyword matches.")
        return 1

    resume = load_resume(args.resume)
    filters = load_filters(args.filters)
    try:
        profile = load_profile(args.profile)
    except Exception:
        profile = ApplicationProfile()

    gate = load_gate()
    if args.dry_run:
        gate.armed = False
    print("⚠ ARMED — cleared applications WILL BE SUBMITTED (cap "
          f"{gate.max_submissions_per_run}/run). Create profile/KILL to halt."
          if gate.armed else "Dry-run — filling and recording, never submitting "
          "(arm in profile/safety.yaml).")

    def apply_one(m: Match):
        return run_testing_mode(
            resume, m, args.resume, args.profile,
            backend=args.backend, headed=args.headed,
            slow_mo=350 if args.headed else 0, pause=False, gate=gate,
        )

    def run_cycle() -> str:
        """One discover → judge → apply pass. Returns 'ok' (applied to the cleared queue),
        'empty' (nothing cleared the bar), or 'stop' (fatal — Claude sign-in required, which
        won't fix itself by waiting)."""
        print(f"Discovering from {len(filters.boards)} board(s)"
              + ("…" if args.fresh else " (reusing a fresh cache if present)…"))
        res = discover_and_match(resume, filters, profile=profile, use_claude=True,
                                 force_fresh=args.fresh)
        if res.from_cache:
            mins = int((res.cache_age_seconds or 0) // 60)
            age = f"{mins} min ago" if mins < 90 else f"{mins // 60}h ago"
            print(f"→ Reused cached discovery (saved {age}; no board search, no Claude judging — "
                  "pass --fresh to re-search).")
        for e in res.errors:
            print(f"  ! {e}")

        if args.min_fit is not None:
            min_fit = args.min_fit  # explicit override — calibration never second-guesses it
        else:
            from .pipeline import effective_min_fit
            min_fit, calib_note = effective_min_fit(filters)
            if calib_note:
                print(f"→ {calib_note}")
        queue = cleared_queue(res.matches, min_fit)
        manual = f" ({len(res.non_fillable)} set aside on non-fillable portals)" if res.non_fillable else ""
        print(f"{res.discovered} discovered → {len(res.matches)} matched{manual} → "
              f"{len(queue)} cleared min-fit {min_fit}.")
        from .pipeline import _print_funnel
        _print_funnel(res, filters)
        if not queue:
            best = max((m.fit_score for m in res.matches if m.fit_score is not None), default=None)
            print("Nothing cleared the bar."
                  + (f" Best fit this run: {best}." if best is not None else "")
                  + f" Lower min_fit (now {min_fit}) or broaden boards in {args.filters}.")
            return "empty"

        result = run_queue(queue, apply_one, gate, max_applications=args.max)
        print("\n" + result.summary())
        for o in result.outcomes:
            print(f"  - {o.result:11} {o.company} — {o.role} (fit {o.fit}) {o.detail}")
        _report_parked()
        return "stop" if "sign-in required" in result.stopped_reason.lower() else "ok"

    if not args.continuous:
        return 1 if run_cycle() == "empty" else 0

    print(f"Continuous mode — a cycle then a {args.interval} min wait, repeating. "
          f"Create {gate.kill_file} or press Ctrl-C to stop.")
    try:
        ended = continuous_loop(run_cycle, gate, interval_s=args.interval * 60)
    except KeyboardInterrupt:
        print("\nStopped (Ctrl-C).")
        return 0
    if ended == "stop":
        print("Stopping continuous run — fix the above, then rerun.")
        return 1
    print(f"Kill switch — {gate.kill_file} exists; stopping continuous run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

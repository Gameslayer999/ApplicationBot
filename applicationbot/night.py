"""Unattended night session (decision 185) — the one command an agent runs and does not come back.

The product already applies with no human in the loop (Guideline #3, decision 176). What was
missing was a session an *agent* — Claude, Hermes, cron — can start at 11pm against a target
("100 applications tonight") and read the results of in the morning without a human ever being
asked anything. This module is that session driver:

    python -m applicationbot.night --goal 100 --until 07:00 --arm

It owns four things `runner.py` does not:

  * **A goal that survives cycles.** `runner --max` caps ONE cycle; a night keeps discovering,
    judging, filling and submitting across as many cycles as it takes until `--goal` submissions
    land, the `--until` deadline passes, the kill switch appears, or the breaker trips.
  * **Nothing is ever asked.** Every application that a human would normally resolve — expired
    login, captcha, an unanswerable required question — is recorded with its parking reason and
    skipped (decision 049's exception-queue model), never waited on. Headless, no pause, no
    per-application confirmation.
  * **A circuit breaker.** An unattended run must not burn 100 attempts on one broken selector.
    N consecutive failed applications, or N of the SAME failure kind, ends the night with the
    reason recorded — that is a bug report, not a night's work.
  * **A machine-readable record.** Every cycle and every application is appended to
    `nights/<session>/events.jsonl`; the run ends with `summary.json` (what an agent branches on)
    and `report.md` (what a human reads). `python -m applicationbot.night review` turns that into
    findings, and the exit code says how the night ended without parsing any prose.

Safety is unchanged (Guideline #3). A submit still needs `armed: true`, an absent `profile/KILL`,
and room under the per-run cap, checked immediately before every click. `--arm` writes that arming
for the user who asked for it and puts the switch back exactly as it found it when the night ends,
so an armed night cannot bleed into tomorrow. Without `--arm` and without a pre-armed
`profile/safety.yaml`, the night runs as a dry run and says so.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .paths import DATA_ROOT

NIGHTS_ROOT = DATA_ROOT / "nights"

# Exit codes: an agent branches on these instead of reading the log.
EXIT_GOAL = 0        # the goal was reached (or a no-goal run finished its window cleanly)
EXIT_DEADLINE = 3    # the --until deadline arrived first — partial night, nothing wrong
EXIT_KILL = 4        # profile/KILL appeared — the user (or a script) halted submission
EXIT_BREAKER = 5     # systemic failure — the night stopped itself; read report.md
EXIT_PREFLIGHT = 6   # never started: something is not set up (each check names its fix)
EXIT_FATAL = 7       # a mid-run stop that waiting cannot fix (Claude sign-in, dead browser)

_EXIT_FOR_STOP = {
    "goal_reached": EXIT_GOAL,
    "deadline": EXIT_DEADLINE,
    "kill": EXIT_KILL,
    "breaker": EXIT_BREAKER,
    "fatal": EXIT_FATAL,
}

# Consecutive failed discovery passes (a board, a parser, the network) before the night gives up.
# Below this it backs off and re-searches: a transient board outage must not end a run.
MAX_DISCOVERY_FAILURES = 5

# Outcomes `runner.Outcome.result` can carry. Only "submitted" counts toward the goal; "dry-run"
# is a prepared application (a good outcome in a dry-run night); the rest are failure kinds the
# breaker watches.
_GOOD = {"submitted", "dry-run"}


# ------------------------------------------------------------------ deadline / breaker (pure)

def parse_deadline(spec: str, now: float) -> float:
    """Absolute epoch for `--until`. Accepts a wall-clock time (`07:00`, `7:00am` — the NEXT time
    it is that o'clock, tomorrow if today's already passed) or a duration (`8h`, `90m`, `45s`).
    Raises ValueError with the accepted forms on anything else."""
    s = spec.strip().lower()
    if s.endswith(("h", "m", "s")) and s[:-1].replace(".", "", 1).isdigit():
        mult = {"h": 3600, "m": 60, "s": 1}[s[-1]]
        return now + float(s[:-1]) * mult
    ampm = ""
    for suffix in ("am", "pm"):
        if s.endswith(suffix):
            ampm, s = suffix, s[: -len(suffix)].strip()
            break
    parts = s.split(":")
    if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
        hour, minute = int(parts[0]), int(parts[1])
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            base = datetime.fromtimestamp(now)
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target.timestamp() <= now:
                target += timedelta(days=1)
            return target.timestamp()
    raise ValueError(f"--until {spec!r} is not a time or a duration. Use 07:00, 7:00am, 8h, or 90m.")


@dataclass
class Breaker:
    """Stops a night that has stopped working. Two independent trips, both cheap to reason about:
    `max_consecutive` failed applications in a row (the browser died, the network is gone), or
    `max_same_kind` failures sharing one kind (one ATS or one selector is broken — every further
    attempt is the same bug again). A good outcome resets only the consecutive streak; the
    per-kind tallies are the night's evidence and never reset."""

    max_consecutive: int = 5
    max_same_kind: int = 10
    consecutive: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    reason: str = ""

    def record(self, kind: Optional[str]) -> None:
        """`kind=None` for a good outcome; otherwise the failure kind (`blocked:login`,
        `failed:TimeoutError`, …)."""
        if kind is None:
            self.consecutive = 0
            return
        self.consecutive += 1
        n = self.kinds[kind] = self.kinds.get(kind, 0) + 1
        if not self.reason and self.consecutive >= self.max_consecutive:
            self.reason = (f"{self.consecutive} applications failed in a row (last: {kind}) — "
                           "stopping rather than spending the night on a broken run")
        elif not self.reason and n >= self.max_same_kind:
            self.reason = (f"{n} applications hit the same failure ({kind}) — stopping; that is "
                           "one bug repeating, not a night's work")

    @property
    def tripped(self) -> bool:
        return bool(self.reason)


def outcome_kind(result: str, detail: str = "") -> Optional[str]:
    """The breaker/taxonomy key for one application outcome. `None` for a good outcome, so a
    productive night never trips anything. A failure is keyed by its result AND its cause, so
    "every Workday login expired" and "every fill timed out" are different kinds."""
    if result in _GOOD:
        return None
    cause = (detail or "").split(":")[0].strip()
    cause = " ".join(cause.split()[:4])  # the head of the reason, not a whole sentence
    return f"{result}:{cause}" if cause else result


# ------------------------------------------------------------------ the session record

@dataclass
class CycleReport:
    """What one discover→judge→apply cycle did. `status` is `ok` (applications were attempted),
    `empty` (nothing cleared the fit bar — wait and re-search) or `stop` (fatal; waiting will not
    fix it). `outcomes` are dicts, not Outcome objects, so the journal writes them verbatim."""
    status: str
    outcomes: list[dict] = field(default_factory=list)
    detail: str = ""

    @property
    def submitted(self) -> int:
        return sum(1 for o in self.outcomes if o.get("result") == "submitted")


@dataclass
class NightResult:
    goal: Optional[int]
    submitted: int = 0
    cycles: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)
    stop_reason: str = ""
    stop_detail: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def exit_code(self) -> int:
        return _EXIT_FOR_STOP.get(self.stop_reason, EXIT_FATAL)

    def summary(self) -> dict:
        return {
            "goal": self.goal,
            "submitted": self.submitted,
            "cycles": self.cycles,
            "counts": dict(sorted(self.counts.items())),
            "failure_kinds": dict(sorted(self.kinds.items(), key=lambda kv: -kv[1])),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")
            if self.started_at else "",
            "ended_at": datetime.fromtimestamp(self.ended_at).isoformat(timespec="seconds")
            if self.ended_at else "",
            "duration_minutes": round((self.ended_at - self.started_at) / 60, 1)
            if self.ended_at and self.started_at else 0.0,
            "exit_code": self.exit_code,
        }


@dataclass
class Journal:
    """Append-only JSONL for one night, plus the files written at the end. Every write is
    best-effort-safe: a full disk must not take the night down, so a failed write is dropped
    rather than raised (the run's own state, not the file, is what decides the outcome)."""
    dir: Path

    def __post_init__(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    def event(self, kind: str, **payload) -> None:
        rec = {"at": datetime.now().isoformat(timespec="seconds"), "event": kind, **payload}
        try:
            with self.events_path.open("a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    def write(self, name: str, text: str) -> Path:
        p = self.dir / name
        try:
            p.write_text(text)
        except OSError:
            pass
        return p

    def read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        for line in self.events_path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def session_dir(root: Path = NIGHTS_ROOT, *, now: Optional[float] = None) -> Path:
    """`nights/2026-08-18-2300` — one directory per night, sortable, no collisions inside a minute
    (a second run in the same minute gets a `-2` suffix rather than appending to the first)."""
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d-%H%M")
    p = root / stamp
    n = 2
    while p.exists():
        p = root / f"{stamp}-{n}"
        n += 1
    return p


# ------------------------------------------------------------------ the loop (pure, injected)

def run_night(
    cycle: Callable[[], CycleReport],
    *,
    goal: Optional[int],
    deadline_at: float,
    journal: Journal,
    kill_file: Path,
    wait: Callable[[int], bool],
    breaker: Optional[Breaker] = None,
    idle_s: int = 1200,
    max_idle_s: int = 7200,
    now: Callable[[], float] = time.time,
) -> NightResult:
    """Run cycles until the goal, the deadline, the kill file, the breaker, or a fatal stop.

    Everything external is injected, so this is unit-testable with no browser, network, or real
    waiting: `cycle()` does one discover→judge→apply pass and returns a `CycleReport`;
    `wait(seconds)` idles between fruitless cycles and returns False if it was cut short (kill
    file, deadline) — the caller owns how that sleep stays responsive; `now()` is the clock.

    A cycle that produces no applications (`empty`, or `ok` with nothing attempted) means the
    boards had nothing new: the night backs off — `idle_s` doubling up to `max_idle_s` — and
    searches again rather than ending, because a goal the user set is a commitment, not a hint
    (the same rule decision 146 gave the in-app loop). Any productive cycle resets the backoff.

    The goal is checked BEFORE each cycle and again after it, so a cycle that overshoots
    (submitted more than the goal needed) still ends the night immediately."""
    breaker = breaker or Breaker()
    res = NightResult(goal=goal, started_at=now())
    journal.event("session_start", goal=goal,
                  deadline=datetime.fromtimestamp(deadline_at).isoformat(timespec="seconds"))
    idle = idle_s

    while True:
        if kill_file.exists():
            res.stop_reason, res.stop_detail = "kill", f"{kill_file} exists — submission halted"
            break
        if now() >= deadline_at:
            res.stop_reason, res.stop_detail = "deadline", "the --until deadline arrived"
            break
        if goal is not None and res.submitted >= goal:
            res.stop_reason, res.stop_detail = "goal_reached", f"{res.submitted}/{goal} submitted"
            break

        res.cycles += 1
        journal.event("cycle_start", cycle=res.cycles, submitted_so_far=res.submitted)
        report = cycle()
        for o in report.outcomes:
            result = o.get("result", "failed")
            res.counts[result] = res.counts.get(result, 0) + 1
            kind = outcome_kind(result, o.get("detail", ""))
            if kind:
                res.kinds[kind] = res.kinds.get(kind, 0) + 1
            breaker.record(kind)
            journal.event("application", cycle=res.cycles, **o)
        res.submitted += report.submitted
        journal.event("cycle_end", cycle=res.cycles, status=report.status,
                      applications=len(report.outcomes), submitted=report.submitted,
                      detail=report.detail)

        if breaker.tripped:
            res.stop_reason, res.stop_detail = "breaker", breaker.reason
            break
        if report.status == "stop":
            res.stop_reason, res.stop_detail = "fatal", report.detail or "a fatal stop"
            break
        if goal is not None and res.submitted >= goal:
            res.stop_reason, res.stop_detail = "goal_reached", f"{res.submitted}/{goal} submitted"
            break

        if report.outcomes:
            idle = idle_s  # productive cycle — search again immediately, no backoff
            continue
        journal.event("idle", cycle=res.cycles, seconds=idle,
                      reason="no posting cleared the fit bar — backing off, then re-searching")
        if not wait(idle):
            continue  # cut short by the kill file or the deadline — the loop head decides why
        idle = min(idle * 2, max_idle_s)

    res.ended_at = now()
    journal.event("session_end", **res.summary())
    return res


# ------------------------------------------------------------------ preflight

@dataclass
class Preflight:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True


def preflight(*, goal: Optional[int], armed: bool, cap: int, kill_file: Path,
              deadline_at: float, dry_run: bool, now: float,
              checks: Optional[list] = None) -> list[Preflight]:
    """Everything that would otherwise be discovered at 3am, checked before the first cycle.

    `checks` are `doctor.Check`s (injected for tests); the night adds what only it cares about:
    the kill file, a submission budget that can actually reach the goal, and a deadline in the
    future. A failed REQUIRED check means the night never starts — each one names its own fix,
    because nobody will be awake to work it out."""
    out: list[Preflight] = []
    if checks is None:
        from .doctor import run_checks
        checks = run_checks()
    for c in checks:
        out.append(Preflight(c.name, c.ok, c.detail, c.fix, c.required))

    if kill_file.exists():
        out.append(Preflight("Kill switch", False, f"{kill_file} exists — every submit is blocked",
                             f"Delete {kill_file} to let the night submit."))
    else:
        out.append(Preflight("Kill switch", True, f"clear ({kill_file} absent)"))

    if dry_run:
        out.append(Preflight("Submission budget", True,
                             "dry-run night — filling and recording, submitting nothing",
                             required=False))
    elif not armed:
        out.append(Preflight("Submission budget", False,
                             "not armed — the night would fill and record but submit nothing",
                             "Re-run with --arm (arms for this night and disarms afterwards), or "
                             "set `armed: true` in profile/safety.yaml."))
    elif goal is not None and cap < goal:
        out.append(Preflight("Submission budget", False,
                             f"cap {cap}/run is below the goal of {goal} — the night would stop "
                             f"at {cap}",
                             f"Re-run with --arm (which sets the cap to the goal) or set "
                             f"`max_submissions_per_run: {goal}` in profile/safety.yaml."))
    else:
        out.append(Preflight("Submission budget", True,
                             f"ARMED — up to {cap} real submissions this run"
                             + (f", goal {goal}" if goal else "")))

    if deadline_at <= now:
        out.append(Preflight("Deadline", False,
                             f"--until is in the past ({datetime.fromtimestamp(deadline_at):%H:%M})",
                             "Give a future time (07:00) or a duration (8h)."))
    else:
        hours = (deadline_at - now) / 3600
        out.append(Preflight("Deadline", True,
                             f"{datetime.fromtimestamp(deadline_at):%a %H:%M} — {hours:.1f}h from now"))
    return out


def preflight_failures(checks: list[Preflight]) -> list[Preflight]:
    return [c for c in checks if not c.ok and c.required]


# ------------------------------------------------------------------ the report a human reads

def describe_resume_policy(policy: dict) -> str:
    """One line naming the résumé this night sends, for the console, `summary.json` and the report.
    An unattended run must say which résumé went out: "the one I wrote" and "one Claude wrote that
    I have never read" are not interchangeable (decision 186)."""
    mode = (policy or {}).get("mode", "smart")
    if mode == "never":
        return "yours as it stands — no tailoring, no Claude call per application"
    if mode == "always":
        return "freshly tailored per posting (no reuse)"
    if mode == "under":
        return (f"tailored only when the fit is under {policy.get('below', 70)}; sent as-is at or "
                "above it")
    return "tailored per posting, reusing an earlier tailor when the skills match"


def render_report(result: NightResult, *, events: list[dict], session: Path,
                  armed: bool, resume_note: str = "") -> str:
    """`report.md` — what happened, in the order someone at breakfast wants it: the number, why it
    stopped, what failed and how often, then every application. `review.md` (night_review.py) is
    the judgement; this is the record."""
    s = result.summary()
    apps = [e for e in events if e.get("event") == "application"]
    lines = [
        f"# Night run — {s['started_at'] or '?'}",
        "",
        f"**{result.submitted} submitted**"
        + (f" of a {result.goal} goal" if result.goal else "")
        + f" · {s['duration_minutes']} min · {result.cycles} cycle(s)"
        + ("" if armed else " · DRY RUN, nothing was submitted"),
        "",
        f"Stopped: **{result.stop_reason}** — {result.stop_detail}",
        "",
        f"Résumé sent: {resume_note}" if resume_note else "",
        "",
        "## Outcomes",
        "",
        "| outcome | n |",
        "|---|---|",
    ]
    for k, n in sorted(result.counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {n} |")
    if not result.counts:
        lines.append("| (no applications attempted) | 0 |")

    if result.kinds:
        lines += ["", "## What went wrong", "",
                  "| failure | n |", "|---|---|"]
        for k, n in sorted(result.kinds.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {k} | {n} |")

    lines += ["", f"## Applications ({len(apps)})", ""]
    if apps:
        lines += ["| outcome | company | role | fit | detail |", "|---|---|---|---|---|"]
        for a in apps:
            detail = str(a.get("detail", "")).replace("|", "\\|")[:120]
            lines.append(f"| {a.get('result','')} | {a.get('company','')} | {a.get('role','')} "
                         f"| {a.get('fit','')} | {detail} |")
    else:
        lines.append("Nothing was attempted — no posting cleared the fit bar this night.")

    lines += ["", "---", "",
              f"Machine-readable: `{session / 'summary.json'}` · events: "
              f"`{session / 'events.jsonl'}`",
              "Self-review: `python -m applicationbot.night review` "
              f"(reads this session and the ones before it)."]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI

def _say(msg: str) -> None:
    print(msg, flush=True)


def _responsive_wait(seconds: int, *, kill_file: Path, deadline_at: float,
                     _sleep=time.sleep, now=time.time) -> bool:
    """Sleep in ≤30s chunks so the kill file and the deadline are noticed within half a minute.
    Returns False if the wait was cut short (either of those), True if it completed."""
    remaining = seconds
    while remaining > 0:
        if kill_file.exists() or now() >= deadline_at:
            return False
        chunk = min(30, remaining)
        _sleep(chunk)
        remaining -= chunk
    return True


def _build_cycle(args, gate, journal: Journal):
    """Wire one real discover→judge→apply cycle. Everything here is the existing pipeline — the
    night adds no second submit path (the gate, the fill, and the tracker rows are `runner.py`'s
    and `pipeline.py`'s, unchanged), it just reports the cycle as data instead of prose."""
    from . import backends  # noqa: F401  (import cost paid once, before the first cycle)
    from .apply_profile import ApplicationProfile, load_profile
    from .backends import ClaudeAuthError
    from .filters import load_filters
    from .pipeline import (discover_and_match, effective_min_fit, loop_policy, run_testing_mode,
                           tailor_choice)
    from .resume import load_resume
    from .runner import cleared_queue, run_queue

    resume = load_resume(args.resume)
    filters = load_filters(args.filters)
    try:
        profile = load_profile(args.profile)
    except Exception:
        profile = ApplicationProfile()

    # The night uses the SAME résumé policy as the in-app loop (decision 178's ⚙ Loop settings,
    # saved in discovery.yaml) so both decide a posting's résumé identically. `--no-tailor` is the
    # one override: it forces `never` for this run only, and writes nothing back.
    policy = dict(loop_policy(filters))
    if args.no_tailor:
        policy["mode"] = "never"

    def apply_one(m):
        tailor, force = tailor_choice(policy, m.fit_score)
        return run_testing_mode(
            resume, m, args.resume, args.profile,
            backend=args.backend, headed=args.headed,
            slow_mo=350 if args.headed else 0, pause=False, gate=gate,
            tailor=tailor, force_retailor=force, reuse_threshold=policy["reuse_threshold"],
        )

    state = {"cycles": 0, "discovery_failures": 0}

    def cycle() -> CycleReport:
        # Only the FIRST cycle revisits (decision 149: postings prepared but never reviewed come
        # back so they aren't buried). After that a night must NOT revisit: there is no human
        # reviewing anything at 3am, so every application this night prepares stays "unreviewed"
        # forever and would be re-discovered, re-tailored and re-attempted every cycle instead of
        # the night moving on to new postings. Verified on a real dry run before this line existed:
        # cycles 1 and 2 both prepared the same Palantir posting.
        state["cycles"] += 1
        try:
            res = discover_and_match(resume, filters, profile=profile, use_claude=True,
                                     force_fresh=args.fresh, revisit=state["cycles"] == 1)
        except ClaudeAuthError as e:
            # Nobody can sign in at 3am and waiting will not fix it.
            return CycleReport("stop", detail=f"Claude sign-in required: {e}")
        except Exception as e:  # noqa: BLE001 — a board, a parser or the network, not our bug
            state["discovery_failures"] += 1
            msg = f"{type(e).__name__}: {e}"
            journal.event("discovery_error", detail=msg, consecutive=state["discovery_failures"])
            _say(f"  ! discovery failed ({state['discovery_failures']}): {msg}")
            if state["discovery_failures"] >= MAX_DISCOVERY_FAILURES:
                return CycleReport("stop", detail=(
                    f"discovery failed {state['discovery_failures']} times in a row — last: {msg}"))
            return CycleReport("empty", detail=f"discovery failed: {msg}")
        state["discovery_failures"] = 0
        for e in res.errors:
            journal.event("discovery_error", detail=str(e))
        min_fit = args.min_fit if args.min_fit is not None else effective_min_fit(filters)[0]
        queue = cleared_queue(res.matches, min_fit)
        journal.event("discovery", cycle=state["cycles"], discovered=res.discovered,
                      matched=len(res.matches), cleared=len(queue), min_fit=min_fit,
                      from_cache=bool(res.from_cache), revisit=state["cycles"] == 1)
        _say(f"  {res.discovered} discovered → {len(res.matches)} matched → "
             f"{len(queue)} cleared min-fit {min_fit}")
        if not queue:
            return CycleReport("empty", detail=f"nothing cleared min-fit {min_fit}")

        rr = run_queue(queue, apply_one, gate, max_applications=args.max_per_cycle, say=_say)
        outcomes = [{"result": o.result, "company": o.company, "role": o.role, "url": o.url,
                     "fit": o.fit, "detail": o.detail} for o in rr.outcomes]
        fatal = "sign-in required" in rr.stopped_reason.lower()
        return CycleReport("stop" if fatal else "ok", outcomes, rr.stopped_reason)

    cycle.policy = policy   # so the caller can say which résumé went out, without re-reading filters
    return cycle


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    from . import safety

    ap = argparse.ArgumentParser(
        prog="python -m applicationbot.night",
        description="Run an unattended session toward a submission goal and write a "
                    "machine-readable record of it. Nothing is ever asked of a human.")
    sub = ap.add_subparsers(dest="command")
    rev = sub.add_parser("review", help="Review the last night's work (see night_review.py).")
    rev.add_argument("--session", default="", help="Session directory; default the newest.")
    rev.add_argument("--history", type=int, default=5, help="How many earlier nights to trend against.")

    ap.add_argument("--goal", type=int, default=None,
                    help="Submissions to land tonight. Omit (or use --dangerously-unlimited) to "
                         "run until the deadline.")
    ap.add_argument("--until", default="8h",
                    help="When to stop: a wall-clock time (07:00, 7:00am) or a duration (8h, 90m). "
                         "Default 8h.")
    ap.add_argument("--arm", action="store_true",
                    help="Arm real submissions for THIS night and set the per-run cap to the goal, "
                         "then put profile/safety.yaml back exactly as it was when the night ends. "
                         "Only use it when the user has asked for a real run.")
    ap.add_argument("--dangerously-unlimited", action="store_true",
                    help="With --arm: no submission cap — keep applying until the deadline, the "
                         "kill switch, the breaker, or the Claude usage limit. There is no ceiling "
                         "on how many applications this sends.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Force a dry run even if profile/safety.yaml is armed: fill, record, "
                         "submit nothing. The rehearsal that proves the plumbing before a real night.")
    ap.add_argument("--max-per-cycle", type=int, default=None,
                    help="Cap applications attempted in one cycle (default: the whole cleared queue).")
    ap.add_argument("--interval", type=int, default=20,
                    help="Minutes to idle after a cycle that found nothing, doubling up to 2h "
                         "(default 20).")
    ap.add_argument("--max-consecutive-failures", type=int, default=5,
                    help="Stop the night after this many failed applications in a row (default 5).")
    ap.add_argument("--max-same-failure", type=int, default=10,
                    help="Stop the night after this many failures of one kind (default 10).")
    ap.add_argument("--min-fit", type=int, default=None,
                    help="Minimum Claude fit score; defaults to min_fit in your filters.")
    ap.add_argument("--no-tailor", action="store_true",
                    help="Send YOUR résumé exactly as it stands — no Claude tailoring call per "
                         "application, and no résumé you have never read goes out. Overrides the "
                         "saved ⚙ Loop settings résumé policy for this run only. Saves the biggest "
                         "per-application token cost; discovery and the fit judge still use Claude.")
    ap.add_argument("--fresh", action="store_true",
                    help="Re-search every board each cycle instead of reusing the discovery cache.")
    ap.add_argument("--headed", action="store_true", help="Show the browser (debugging only).")
    ap.add_argument("--resume", default="profile/resume.yaml")
    ap.add_argument("--profile", default="profile/application_profile.yaml")
    ap.add_argument("--filters", default="profile/discovery.yaml")
    ap.add_argument("--backend", default="auto", choices=["auto", "claude-code", "rules"])
    ap.add_argument("--session-dir", default="",
                    help="Where to write this night's record (default nights/<timestamp>/).")
    ap.add_argument("--preflight-only", action="store_true",
                    help="Run the checks, print them as JSON, and exit without applying.")
    args = ap.parse_args(argv)

    if args.command == "review":
        from .night_review import main as review_main
        return review_main(["--session", args.session, "--history", str(args.history)])

    now = time.time()
    try:
        deadline_at = parse_deadline(args.until, now)
    except ValueError as e:
        _say(str(e))
        return EXIT_PREFLIGHT

    goal = args.goal
    if args.dangerously_unlimited and goal is None:
        goal = None  # explicit: run to the deadline

    session = Path(args.session_dir) if args.session_dir else session_dir(now=now)

    # Arm (only when the user asked for it), remembering what to put back.
    restore: Optional[dict] = None
    if args.arm and not args.dry_run:
        cap = 10 ** 6 if args.dangerously_unlimited else max(goal or 1, 1)
        restore = safety.save_arming(True, cap)
        _say(f"⚠ ARMED for this night — cap {cap}/run. profile/safety.yaml goes back to "
             f"armed={restore['armed']}, cap {restore['max_submissions_per_run']} when it ends. "
             f"Create profile/KILL to halt immediately.")

    gate = safety.load_gate()
    if args.dry_run:
        gate.armed = False

    checks = preflight(goal=goal, armed=gate.armed, cap=gate.max_submissions_per_run,
                       kill_file=gate.kill_file, deadline_at=deadline_at,
                       dry_run=args.dry_run, now=now)
    for c in checks:
        _say(f"{'✓' if c.ok else ('✗' if c.required else '⚠')} {c.name}: {c.detail}"
             + (f"\n    → {c.fix}" if not c.ok and c.fix else ""))
    failures = preflight_failures(checks)

    # The session directory is only created for a night that actually starts — a preflight that
    # fails leaves no empty record behind to confuse tomorrow's review.
    if args.preflight_only or failures:
        if restore is not None:
            safety.save_arming(restore["armed"], restore["max_submissions_per_run"])
        # No session path here: nothing was created, and naming a directory that will not exist
        # would send an agent looking for a record of a night that never ran.
        payload = {"ok": not failures, "checks": [c.__dict__ for c in checks]}
        _say("PREFLIGHT " + json.dumps(payload))
        if failures:
            _say(f"\nNot starting — {len(failures)} check(s) failed above. Each names its fix.")
        return EXIT_PREFLIGHT if failures else 0

    journal = Journal(session)
    journal.event("preflight", checks=[c.__dict__ for c in checks])
    _say(f"\nNight session → {session}\n"
         f"Goal: {goal if goal else 'run to the deadline'} · until "
         f"{datetime.fromtimestamp(deadline_at):%a %H:%M} · "
         + ("submitting for real" if gate.armed else "DRY RUN — nothing will be submitted"))

    breaker = Breaker(max_consecutive=args.max_consecutive_failures,
                      max_same_kind=args.max_same_failure)
    cycle = _build_cycle(args, gate, journal)
    resume_note = describe_resume_policy(getattr(cycle, "policy", {}))
    _say(f"Résumé: {resume_note}")
    journal.event("resume_policy", policy=getattr(cycle, "policy", {}), note=resume_note)

    def wait(seconds: int) -> bool:
        _say(f"  nothing new — waiting {max(1, round(seconds / 60))} min, then re-searching.")
        return _responsive_wait(seconds, kill_file=gate.kill_file, deadline_at=deadline_at)

    try:
        result = run_night(cycle, goal=goal, deadline_at=deadline_at, journal=journal,
                           kill_file=gate.kill_file, wait=wait, breaker=breaker,
                           idle_s=args.interval * 60)
    except KeyboardInterrupt:
        result = NightResult(goal=goal, started_at=now, ended_at=time.time(),
                             stop_reason="kill", stop_detail="interrupted at the keyboard")
        journal.event("session_end", **result.summary())
    except Exception as e:  # noqa: BLE001 — an unattended run must never end with no record
        result = NightResult(goal=goal, started_at=now, ended_at=time.time(),
                             stop_reason="fatal",
                             stop_detail=f"the night crashed — {type(e).__name__}: {e}")
        journal.event("session_end", **result.summary())
        _say(f"\nThe night crashed: {type(e).__name__}: {e}")
    finally:
        if restore is not None:
            safety.save_arming(restore["armed"], restore["max_submissions_per_run"])
            _say(f"Disarmed — profile/safety.yaml restored to armed={restore['armed']}, "
                 f"cap {restore['max_submissions_per_run']}.")

    summary = result.summary()
    summary["session"] = str(session)
    summary["armed"] = bool(gate.armed)
    summary["resume_policy"] = getattr(cycle, "policy", {}).get("mode", "smart")
    summary["resume_note"] = resume_note
    journal.write("summary.json", json.dumps(summary, indent=2) + "\n")
    journal.write("report.md", render_report(result, events=journal.read_events(),
                                             session=session, armed=gate.armed,
                                             resume_note=resume_note))
    _say(f"\n{result.submitted} submitted"
         + (f"/{goal}" if goal else "")
         + f" · stopped: {result.stop_reason} — {result.stop_detail}")
    _say(f"Report: {session / 'report.md'} · Summary: {session / 'summary.json'}")
    _say("NIGHT_SUMMARY " + json.dumps(summary))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

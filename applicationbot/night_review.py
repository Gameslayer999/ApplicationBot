"""The morning after — a night reviews its own work (decision 185).

An unattended run that only reports what it *believes* happened is not trustworthy: the number it
prints is the number of times it clicked submit, which is not the same as the number of
applications that actually landed. This module is the audit an agent runs before it tells anyone
the night went well, and it answers exactly three questions the user asked for:

  1. **Were the submissions real?** Every application the night counted as `submitted` is checked
     against the tracker: is there a row, is its status a submitted one, is `date_applied` set?
     Anything that clicked submit without a confirmation (`unconfirmed`) is called out as a
     POSSIBLE FALSE SUCCESS rather than quietly counted.
  2. **What failed, and is it getting worse?** Every non-submit is grouped by failure kind and by
     ATS/portal, and compared against the previous nights' `summary.json` files, so a regression
     ("Workday logins started failing tonight") reads differently from a standing gap.
  3. **Is the fit judge calibrated?** The existing outcome calibration (decision 043) says whether
     the roles that cleared `min_fit` are the ones that get replies, and whether the bar should move.

The output is `findings.json` — a ranked backlog, biggest blocker first, each item naming the
evidence and the file to change — and `review.md` for a human. The findings are what the next
night gets fixed from; the agent contract for acting on them is in docs/AGENT_NIGHT_RUN.md.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .night import NIGHTS_ROOT, outcome_kind

# Tracker statuses that mean the application really went in (mirrors tracker._SUBMITTED, which is
# private; kept as a literal so a tracker refactor surfaces here as a test failure, not silence).
SUBMITTED_STATUSES = {"applied", "responded", "interview", "offer", "rejected", "no-response"}

# Where each failure kind is fixed. The taxonomy is only useful if it points at a file.
_FIX_HINTS = {
    "blocked:needs_answer": ("An application stalled on a question with no stored answer. Add it to "
                             "the answer bank (`applicationbot/answer_bank.py`) or Profile → Answers "
                             "so the next night fills it unattended."),
    "blocked:login": ("The portal wanted an account. Check the stored credentials "
                      "(`applicationbot/credentials.py`) and the bot inbox link "
                      "(`python -m applicationbot.mailbox status`) — email verification is how an "
                      "unattended run gets past these."),
    "blocked:captcha": ("A human-verification wall. Not fixable in code (Guideline #4) — the right "
                        "response is to stop queueing that source, not to evade it."),
    "unconfirmed": ("Submit was clicked but no confirmation was detected. Either the ATS's success "
                    "page is unrecognised (extend the confirmation matcher in "
                    "`applicationbot/apply.py`) or the submit did not go through."),
    "failed:TimeoutError": ("The form never reached a fillable state in time. Check the reveal/nav "
                            "path for that ATS (`_open_application_form` in "
                            "`applicationbot/apply.py`, `applicationbot/nav_recipes.py`)."),
}
_GENERIC_HINT = ("Reproduce with a single headless dry run: `python -m applicationbot.apply <url> "
                 "--headless --no-pause --no-record --dry-run`, then fix the specific failure.")


@dataclass
class Finding:
    """One ranked backlog item. `impact` is how many of the night's applications it actually cost
    (what a human reads); `priority` is what the list sorts on, so a class of problem can outrank a
    same-sized one — a submission the system was WRONG about beats one it knows it missed."""
    id: str
    title: str
    impact: int
    evidence: str
    fix: str
    kind: str = "failure"          # failure | verification | calibration
    priority: int = 0
    examples: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.priority:
            self.priority = self.impact


# ------------------------------------------------------------------ reading sessions

def list_sessions(root: Path = NIGHTS_ROOT, *, limit: int = 6) -> list[Path]:
    """Session directories, newest first. Only directories that actually finished a night (they
    have a `summary.json`) count — a crashed session is not a data point for a trend."""
    if not root.exists():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and (p / "summary.json").exists()]
    return sorted(dirs, key=lambda p: p.name, reverse=True)[:limit]


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def load_events(session: Path) -> list[dict]:
    p = session / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ------------------------------------------------------------------ 1. were the submissions real?

def verify_submissions(applications: list[dict], lookup: Callable[[str], Optional[dict]]) -> dict:
    """Check each claimed submission against the tracker. `lookup(url)` returns the tracker row for
    a source URL (or None). Returns counts plus the specific applications that do not hold up:

      * `unconfirmed` — the submit click landed but no confirmation was seen (possible false success)
      * `not_recorded` — counted as submitted, but the tracker has no row for it at all
      * `wrong_status` — a row exists but its status is not a submitted one (so the Track tab and
        the night's number disagree, and the tracker is the one the user reads)
    """
    confirmed, unconfirmed, not_recorded, wrong_status = [], [], [], []
    for a in applications:
        result, url = a.get("result"), a.get("url", "")
        label = f"{a.get('company', '')} — {a.get('role', '')}".strip(" —") or url
        if result == "unconfirmed":
            unconfirmed.append({"label": label, "url": url, "detail": a.get("detail", "")})
            continue
        if result != "submitted":
            continue
        row = lookup(url) if url else None
        if not row:
            not_recorded.append({"label": label, "url": url, "detail": "no tracker row"})
        elif row.get("status") not in SUBMITTED_STATUSES:
            wrong_status.append({"label": label, "url": url,
                                 "detail": f"tracker says '{row.get('status')}'"})
        elif not row.get("date_applied"):
            wrong_status.append({"label": label, "url": url,
                                 "detail": "tracker row has no date_applied"})
        else:
            confirmed.append({"label": label, "url": url})
    return {
        "claimed": sum(1 for a in applications if a.get("result") == "submitted"),
        "confirmed": len(confirmed),
        "unconfirmed": unconfirmed,
        "not_recorded": not_recorded,
        "wrong_status": wrong_status,
        "trustworthy": not (unconfirmed or not_recorded or wrong_status),
    }


# ------------------------------------------------------------------ 2. what failed, and the trend

def taxonomy(applications: list[dict], lookup: Callable[[str], Optional[dict]]) -> dict:
    """Group every non-submit by failure kind and by portal. Portal comes from the tracker row (the
    night's outcome doesn't carry it), so "every Workday attempt failed" is visible even when the
    kinds differ."""
    kinds: dict[str, int] = {}
    portals: dict[str, dict[str, int]] = {}
    examples: dict[str, list[str]] = {}
    for a in applications:
        kind = outcome_kind(a.get("result", ""), a.get("detail", ""))
        if not kind:
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
        examples.setdefault(kind, [])
        if len(examples[kind]) < 3:
            label = f"{a.get('company', '')} — {a.get('role', '')}".strip(" —") or a.get("url", "")
            examples[kind].append(label)
        row = lookup(a.get("url", "")) if a.get("url") else None
        portal = (row or {}).get("portal") or "unknown"
        bucket = portals.setdefault(portal, {"attempts": 0, "failures": 0})
        bucket["failures"] += 1
    for a in applications:
        row = lookup(a.get("url", "")) if a.get("url") else None
        portal = (row or {}).get("portal") or "unknown"
        portals.setdefault(portal, {"attempts": 0, "failures": 0})["attempts"] += 1
    return {"kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
            "examples": examples,
            "portals": dict(sorted(portals.items(), key=lambda kv: -kv[1]["failures"]))}


def trend(kinds: dict[str, int], previous: list[dict]) -> dict:
    """How tonight's failure kinds compare with the average of the previous nights' `summary.json`
    `failure_kinds`. A kind absent from every earlier night is flagged NEW — that is a regression,
    and it reads differently from a gap that has been there all along."""
    if not previous:
        return {k: {"tonight": n, "before": None, "verdict": "no history"} for k, n in kinds.items()}
    out = {}
    seen_keys = set(kinds)
    for p in previous:
        seen_keys |= set(p.get("failure_kinds", {}))
    for k in seen_keys:
        tonight = kinds.get(k, 0)
        befores = [p.get("failure_kinds", {}).get(k, 0) for p in previous]
        avg = sum(befores) / len(befores)
        if tonight and all(b == 0 for b in befores):
            verdict = "NEW tonight"
        elif tonight > avg:
            verdict = "worse"
        elif tonight < avg:
            verdict = "better"
        else:
            verdict = "unchanged"
        out[k] = {"tonight": tonight, "before": round(avg, 1), "verdict": verdict}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["tonight"]))


# ------------------------------------------------------------------ 3. is the judge calibrated?

def calibration_finding(report: dict, current_min_fit: Optional[int],
                        recommendation: Optional[tuple]) -> Optional[Finding]:
    """Turn the existing outcome calibration (decision 043) into a finding, but only when there is
    enough resolved history to mean anything — a bar moved on three data points is noise."""
    if not report:
        return None
    bands = report.get("bands", [])
    resolved = sum(b.get("positive", 0) + b.get("negative", 0) for b in bands)
    if recommendation:
        new_fit, why = recommendation
        return Finding(
            id="calibration",
            title=f"Move min_fit {current_min_fit} → {new_fit}",
            impact=resolved,
            evidence=why,
            fix="Set `min_fit` in profile/discovery.yaml (the night reads it every cycle).",
            kind="calibration")
    if resolved < 5:
        return None
    return Finding(id="calibration", title=f"min_fit {current_min_fit} looks right",
                   impact=0, evidence=f"{resolved} resolved application(s) across the fit bands "
                                      "show no reason to move the bar.",
                   fix="No change.", kind="calibration")


# ------------------------------------------------------------------ the ranked backlog

def build_findings(verification: dict, tax: dict, tr: dict,
                   calibration: Optional[Finding]) -> list[Finding]:
    """Everything worth fixing, biggest cost to the night first. Verification problems outrank
    failure kinds of the same size: a submission the system got WRONG about is worse than one it
    knows it missed."""
    out: list[Finding] = []

    n_bad = len(verification.get("not_recorded", [])) + len(verification.get("wrong_status", []))
    if n_bad:
        rows = verification["not_recorded"] + verification["wrong_status"]
        out.append(Finding(
            id="submissions-not-recorded",
            title=f"{n_bad} submission(s) the tracker does not confirm",
            impact=n_bad, priority=n_bad + 1000,   # always outranks a same-size failure kind
            evidence="; ".join(f"{r['label']} ({r['detail']})" for r in rows[:5]),
            fix="The night's count and the Track tab disagree — trust the tracker. Check the "
                "tracker write in `pipeline.run_testing_mode` / `applicationbot/apply.py` for "
                "these postings before believing tonight's number.",
            kind="verification",
            examples=[r["url"] for r in rows[:5]]))

    unconf = verification.get("unconfirmed", [])
    if unconf:
        out.append(Finding(
            id="unconfirmed-submits",
            title=f"{len(unconf)} submit(s) with no confirmation — possible false success",
            impact=len(unconf), priority=len(unconf) + 1000,
            evidence="; ".join(f"{r['label']}" for r in unconf[:5]),
            fix=_FIX_HINTS["unconfirmed"],
            kind="verification",
            examples=[r["url"] for r in unconf[:5]]))

    for kind, n in tax.get("kinds", {}).items():
        t = tr.get(kind, {})
        verdict = t.get("verdict", "")
        out.append(Finding(
            id=f"failure:{kind}",
            title=f"{n} × {kind}" + (f" ({verdict})" if verdict and verdict != "unchanged" else ""),
            impact=n,
            evidence=("was " + str(t.get("before")) + "/night before; " if t.get("before") is not None
                      else "") + "e.g. " + ", ".join(tax.get("examples", {}).get(kind, [])[:3]),
            fix=_FIX_HINTS.get(kind, _GENERIC_HINT),
            kind="failure"))

    for portal, counts in tax.get("portals", {}).items():
        attempts, failures = counts["attempts"], counts["failures"]
        if attempts >= 3 and failures == attempts:
            out.append(Finding(
                id=f"portal:{portal}",
                title=f"every {portal} attempt failed ({failures}/{attempts})",
                impact=failures, priority=failures + 500,  # a wholly-broken ATS outranks scattered failures
                evidence=f"{failures} of {attempts} applications on {portal} failed tonight.",
                fix=f"Drive one {portal} posting by hand headless and fix the specific step; until "
                    f"then consider dropping {portal} from the boards in profile/discovery.yaml so "
                    "the night does not spend cycles on it.",
                kind="failure"))

    if calibration:
        out.append(calibration)
    return sorted(out, key=lambda f: -f.priority)


# ------------------------------------------------------------------ the review a human reads

def render_review(summary: dict, verification: dict, tax: dict, tr: dict,
                  findings: list[Finding], *, session: Path, reviewed: int = 0) -> str:
    claimed = verification.get("claimed", 0)
    lines = [
        f"# Night review — {summary.get('started_at', '?')}",
        "",
        f"The night reported **{summary.get('submitted', 0)} submitted** "
        f"(goal {summary.get('goal')}) and stopped on **{summary.get('stop_reason', '?')}** — "
        f"{summary.get('stop_detail', '')}",
        "",
        "## 1. Were the submissions real?",
        "",
    ]
    if claimed == 0 and verification.get("trustworthy"):
        # Saying "all 0 submissions confirmed" would read as a clean night. Nothing was sent.
        lines.append(f"**Nothing was submitted.** {reviewed} application(s) were attempted; none "
                     "claimed a submission — that is expected for a dry-run night, and a bug for "
                     "an armed one.")
    elif verification.get("trustworthy"):
        lines.append(f"Yes — all {claimed} claimed submission(s) have a tracker row with a "
                     "submitted status and a date applied.")
    else:
        lines.append(f"**No — the reported number overstates what landed.** {claimed} claimed, "
                     f"{verification.get('confirmed', 0)} fully confirmed.")
        for key, label in (("unconfirmed", "clicked submit, no confirmation seen"),
                           ("not_recorded", "no tracker row"),
                           ("wrong_status", "tracker disagrees")):
            rows = verification.get(key, [])
            if rows:
                lines += ["", f"- **{len(rows)} {label}:**"] + [
                    f"  - {r['label']} — {r.get('detail', '')}" for r in rows[:10]]

    lines += ["", "## 2. What failed, and is it getting worse?", ""]
    if tax.get("kinds"):
        lines += ["| failure | tonight | before | trend |", "|---|---|---|---|"]
        for kind, n in tax["kinds"].items():
            t = tr.get(kind, {})
            lines.append(f"| {kind} | {n} | {t.get('before', '—')} | {t.get('verdict', '—')} |")
        lines += ["", "| portal | attempts | failures |", "|---|---|---|"]
        for portal, c in tax.get("portals", {}).items():
            lines.append(f"| {portal} | {c['attempts']} | {c['failures']} |")
    else:
        lines.append("Nothing failed tonight.")

    cal = next((f for f in findings if f.kind == "calibration"), None)
    lines += ["", "## 3. Is the fit judge calibrated?", "",
              (f"{cal.title} — {cal.evidence}" if cal else
               "Not enough resolved applications yet to judge the bar (needs replies or "
               "rejections against submitted applications).")]

    lines += ["", "## Backlog — fix these before the next night", ""]
    if findings:
        for i, f in enumerate(findings, 1):
            lines += [f"{i}. **{f.title}** (cost {f.impact} application(s))",
                      f"   - Evidence: {f.evidence}", f"   - Fix: {f.fix}"]
    else:
        lines.append("Nothing to fix — the night ran clean.")

    lines += ["", "---", "",
              f"Machine-readable: `{session / 'findings.json'}` · night record: "
              f"`{session / 'summary.json'}`"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ the whole review

def review_session(session: Path, *, lookup: Callable[[str], Optional[dict]],
                   previous: Optional[list[dict]] = None,
                   calibration_report: Optional[dict] = None,
                   min_fit: Optional[int] = None,
                   recommendation: Optional[tuple] = None) -> dict:
    """Review one night. Everything external — the tracker, the calibration report, the earlier
    nights — is injected, so this is testable against a handful of dicts."""
    summary = load_json(session / "summary.json")
    apps = [e for e in load_events(session) if e.get("event") == "application"]
    verification = verify_submissions(apps, lookup)
    tax = taxonomy(apps, lookup)
    tr = trend(tax["kinds"], previous or [])
    cal = calibration_finding(calibration_report or {}, min_fit, recommendation)
    findings = build_findings(verification, tax, tr, cal)
    return {
        "session": str(session),
        "summary": summary,
        "applications_reviewed": len(apps),
        "verification": verification,
        "taxonomy": tax,
        "trend": tr,
        "findings": [asdict(f) for f in findings],
        "review_md": render_review(summary, verification, tax, tr, findings, session=session,
                                   reviewed=len(apps)),
    }


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m applicationbot.night review",
        description="Audit a night's work: were the submissions real, what failed and is it "
                    "getting worse, and is the fit judge calibrated.")
    ap.add_argument("--session", default="", help="Session directory (default: the newest).")
    ap.add_argument("--history", type=int, default=5,
                    help="How many earlier nights to trend against (default 5).")
    args = ap.parse_args(argv)

    sessions = list_sessions()
    if args.session:
        session = Path(args.session)
        previous = [load_json(p / "summary.json") for p in sessions if p != session]
    elif sessions:
        session, previous = sessions[0], [load_json(p / "summary.json") for p in sessions[1:]]
    else:
        print(f"No night sessions found under {NIGHTS_ROOT}. Run one first: "
              "`python -m applicationbot.night --goal 10 --dry-run`.")
        return 1
    if not (session / "summary.json").exists():
        print(f"{session} has no summary.json — it is not a finished night session.")
        return 1
    previous = previous[: args.history]

    from . import tracker
    from .filters import load_filters

    def lookup(url: str):
        try:
            return tracker.find_by_source_url(url)
        except Exception:
            return None

    try:
        cal_report = tracker.calibration_report()
    except Exception:
        cal_report = {}
    try:
        min_fit = load_filters("profile/discovery.yaml").min_fit
        recommendation = tracker.recommended_min_fit(min_fit)
    except Exception:
        min_fit, recommendation = None, None

    out = review_session(session, lookup=lookup, previous=previous,
                         calibration_report=cal_report, min_fit=min_fit,
                         recommendation=recommendation)
    (session / "findings.json").write_text(
        json.dumps({k: v for k, v in out.items() if k != "review_md"}, indent=2) + "\n")
    (session / "review.md").write_text(out["review_md"])
    print(out["review_md"])
    print(f"Findings: {session / 'findings.json'} · Review: {session / 'review.md'}")
    print("REVIEW_SUMMARY " + json.dumps({
        "session": str(session),
        "claimed": out["verification"]["claimed"],
        "confirmed": out["verification"]["confirmed"],
        "trustworthy": out["verification"]["trustworthy"],
        "findings": [f["title"] for f in out["findings"]],
    }))
    # Exit 2 when the night's own number cannot be trusted — an agent must not report a clean
    # night on the strength of a count the tracker disagrees with.
    return 0 if out["verification"]["trustworthy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

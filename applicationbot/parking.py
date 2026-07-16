"""Park & resume blocked applications (AutoApply-AI survey #1).

The autonomous runner used to treat a blocked application as a dead end: it recorded the
block as an outcome and moved on, and nothing ever came back to it. This module turns a
block into a *resumable* state. It classifies WHY an application stalled into a small set
of user-actionable kinds, so the Apply/runner path can PARK it (a durable tracker row,
status='blocked', with the reason) and the UI can show a one-click "Resolve" card that
deep-links to the fix (UI Principle #2).

Resume is deterministic and dependency-free (unlike AutoApply-AI's Redis `BLPOP`/`RPUSH`
worker rendezvous): once the user resolves the block (answers the question, stores the
login), re-running Apply on the same posting URL drives the same deterministic fill again —
now getting past the field that stalled it. No browser-state serialization, no new service.

`classify(report)` is a pure function over an `ApplyReport`; it returns None when there is
nothing for the user to act on (a clean dry-run, or a genuine site failure we can't fix by
answering).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid importing apply.py (pulls Playwright) just for a type hint
    from .apply import ApplyReport

# The user-actionable kinds. `resolve` is the UI deep-link target the "Resolve" card sends
# the user to; `resumable` marks whether acting on it and re-running can get further.
NEEDS_ANSWER = "needs_answer"   # required question(s) had no answer → Profile "Needs your answer"
FORM_REJECTED = "form_rejected"  # the form rejected the submit → review answers
LOGIN = "login"                 # the site needs a sign-in / verification first → credentials
CAPTCHA = "captcha"             # a CAPTCHA stands in the way → solve in the browser
BOT_WALL = "bot_wall"           # the site REFUSED us as a bot → nothing to fix; retry later
SITE_ERROR = "site_error"       # genuine failure (no submit button, click crashed) — NOT resumable


@dataclass
class ParkReason:
    kind: str          # one of the constants above
    summary: str       # one human line naming what is blocked (Guideline #11)
    resolve: str       # UI deep-link target: "profile-answers" | "credentials" | ""
    resumable: bool    # True = user can act then re-run; False = a genuine site failure
    detail: str = ""   # specifics (field names, error text) for the Resolve card


_LOGIN_MARKERS = ("log in", "login", "sign in", "signin", "log-in", "sign-in",
                  "authenticate", "verify your", "verification link", "create an account")


def required_missing(report: "ApplyReport") -> list[str]:
    """The names of REQUIRED fields this run could not answer, from both the armed
    pre-submit gate (`blockers`) and the dry-run required-field scan (`skipped`),
    de-duplicated in first-seen order."""
    names: list[str] = []
    for b in report.blockers:
        b = b.strip()
        if b.lower().startswith("unresolved required field"):
            _, _, rest = b.partition(":")
            names += [n.strip() for n in rest.split(";")]
    for s in report.skipped:
        if "REQUIRED, not filled" in s:
            names.append(s.split(" — ")[0].strip())
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def classify(report: "ApplyReport") -> Optional[ParkReason]:
    """Why this application stalled, as a resumable ParkReason, or None if there is nothing
    for the user to act on. Order reflects specificity: a login/CAPTCHA wall gates the whole
    form, so it wins over individual missing fields."""
    text = " ".join(report.blockers + report.errors).lower()

    # FIRST — and read from a structured flag, never from the error prose. The site refused us, so
    # the form was never served: this run proves nothing about the posting and is worth retrying
    # later, unchanged. It must outrank CAPTCHA: the wall's own vendor host is
    # "captcha-delivery.com", so the `"captcha" in text` scan below would otherwise mis-park an IP
    # block as a puzzle the user could "solve in the open browser" — which does not exist here.
    if getattr(report, "bot_wall", ""):
        return ParkReason(
            BOT_WALL,
            "The site refused us as automated traffic — the application form was never shown. "
            "Nothing to fix on your side; try again later or from a different network.",
            "", True, f"blocked by {report.bot_wall}")

    if "captcha" in text:
        return ParkReason(
            CAPTCHA,
            "A CAPTCHA is blocking this application — solve it in the open browser, then re-run.",
            "", True, text.strip())

    if any(m in text for m in _LOGIN_MARKERS):
        return ParkReason(
            LOGIN,
            "The site needs you to sign in or verify before applying — store the login, then re-run.",
            "credentials", True, text.strip())

    missing = required_missing(report)
    if missing:
        shown = ", ".join(missing[:3]) + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
        return ParkReason(
            NEEDS_ANSWER,
            f"{len(missing)} required question(s) had no answer: {shown}. Answer them, then re-run.",
            "profile-answers", True, "; ".join(missing))

    if any("form rejected the submit" in b.lower() for b in report.blockers):
        return ParkReason(
            FORM_REJECTED,
            "The form rejected the submission — review your answers, then re-run.",
            "profile-answers", True,
            "; ".join(b for b in report.blockers if "form rejected" in b.lower()))

    if report.submit_state == "blocked":
        # No submit button, submit click crashed, etc. — a genuine site failure the user
        # can't fix by answering. Parked as a record, but not marked resumable.
        return ParkReason(
            SITE_ERROR,
            "; ".join(report.blockers) or "The application could not be completed.",
            "", False, "; ".join(report.blockers))

    return None


# Display metadata for a parked row, keyed by kind: (headline, the action button's verb,
# the UI deep-link target, resumable). Used by the "Resolve" cards, which have only the
# stored `blocked_kind`/`blocked_detail` — not a live report to re-classify.
_KIND_DISPLAY = {
    NEEDS_ANSWER: ("Needs your answers", "Answer the questions", "profile-answers", True),
    FORM_REJECTED: ("The form rejected the submission", "Review your answers", "profile-answers", True),
    LOGIN: ("Sign-in required first", "Store the login", "credentials", True),
    CAPTCHA: ("A CAPTCHA is in the way", "Solve it in the browser", "", True),
    # Resumable, but by TIME rather than by the user: the fix is to re-run later, so the card's
    # verb is "Try again" and it deep-links nowhere — there is no setting that unblocks this.
    BOT_WALL: ("The site blocked automated access", "Try again", "", True),
    SITE_ERROR: ("Couldn't be completed", "", "", False),
}


def describe(kind: str, detail: str = "") -> dict:
    """Display fields for a parked application's Resolve card, from its stored kind/detail.
    `resolve` is the UI deep-link target ("profile-answers" / "credentials" / ""); `resumable`
    says whether re-running after the fix can get further."""
    label, action, resolve, resumable = _KIND_DISPLAY.get(kind, ("Blocked", "", "", False))
    return {"kind": kind, "label": label, "action": action,
            "resolve": resolve, "resumable": resumable, "detail": detail}

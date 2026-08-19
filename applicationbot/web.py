"""A tiny local web UI for reviewing tailored resumes.

Zero dependencies (stdlib `http.server`), bound to 127.0.0.1. Pick a resume, pick a job
(a fixture or a pasted posting), pick a backend, and see the tailored resume rendered in
the browser alongside the relevance notes, factual-drift warnings, and which engine ran.

Run:
    python -m applicationbot.web            # http://127.0.0.1:8000
    python -m applicationbot.web --port 9000

The endpoints only read files from the repo's `profile/`, `examples/`, and
`fixtures/job_descriptions/` folders (allow-listed), so the page can't be used to read
arbitrary files off disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import apply_profile, auth, catalogue, filters, impact, linkedin, safety, tracker
from .job_description import JobDescription, load_job_description
from .backends import DEFAULT_QUALITY
from .length import LengthBudget
from .models import Resume, TailoredResume
from .pdf import render_pdf
from .render import render_html, render_markdown
from .resume import load_resume
from .tailor import tailor_resume
from .paths import DATA_ROOT

REPO_ROOT = Path(__file__).resolve().parent.parent

# User-saved job fixtures (added from the Track tab). These live under the writable data root
# (`profile/*` is git-ignored, and in the packaged app DATA_ROOT is the per-user support dir), so
# they never touch the read-only shipped `fixtures/` bundle and are never committed. They show up
# in the Review-tab fixture picker alongside the shipped example postings.
USER_FIXTURES_DIR = DATA_ROOT / "profile" / "job_fixtures"

# Dev auto-reload (set by `scripts/dev_reload.py`, i.e. `run.sh --dev`). When on, the page polls
# /dev/reload-token; the token is this process's boot time, so a supervisor restart after a code
# edit changes it and the browser reloads itself. Off (and inert) in normal runs.
_DEV = os.environ.get("APPLICATIONBOT_DEV") == "1"
_BOOT_TOKEN = str(time.time())
_DEV_REFRESH_SCRIPT = """
<script>
/* Dev auto-reload: when the server restarts after a code change its boot token changes — reload
   so edits show without a manual refresh. Only injected when APPLICATIONBOT_DEV=1. */
(function(){
  let token = null;
  setInterval(async () => {
    try {
      const t = await (await fetch("/dev/reload-token", {cache:"no-store"})).text();
      if (token === null) { token = t; return; }
      if (t !== token) location.reload();
    } catch (e) { /* server is mid-restart; ignore and retry */ }
  }, 1000);
})();
</script>
"""


# --------------------------------------------------------------------------- test run
# A single "Find & fill one application (dry-run)" run at a time. The worker thread runs the
# discover → match → tailor → PDF → dry-run apply pipeline; the page polls /test-run/status,
# and a "Finish" button releases the browser (POST /test-run/close) instead of the terminal
# pause. Never submits (Agent Guideline #3).

_TEST_LOCK = threading.Lock()
_TEST_STATE: dict = {"phase": "idle"}  # idle|running|filled|done|error
_TEST_HOLD = threading.Event()


def _test_reset() -> dict:
    return {
        "phase": "running", "step": "start",
        "message": "Starting…", "elapsed_note": "",
        "scanned": 0, "matched": 0, "judged": 0, "judged_total": 0, "funnel": {},
        "from_cache": False, "cache_age_min": None, "can_research": False,
        "chosen": None, "report": None, "tailored": None, "mode": "apply", "errors": [],
    }


def _set(**kw) -> None:
    with _TEST_LOCK:
        _TEST_STATE.update(kw)


# Every posting Claude has scored this session, keyed by URL → its Match (decision 174). The
# search breakdown's Apply / Apply anyway buttons send back a URL; preparing it needs the Match
# (posting + JD + fit score), which the row dicts don't carry. Bounded so a long-running server
# can't grow it without limit; `_match_for_url` falls back to the discovery snapshot on a miss.
_JUDGED_MATCHES: "dict[str, object]" = {}
_JUDGED_LOCK = threading.Lock()
_JUDGED_CAP = 400


def _judged_rows(matches, min_fit: int) -> list[dict]:
    """Every Claude-judged posting of a search — accepted AND denied, ranked best-first — as the
    rows the search-breakdown UI shows, so the user can see what the boards returned and why each
    was rejected. Shared by the test run and the auto-apply loop (decision 149).

    Also indexes each scored Match by posting URL, so an Apply click on any row it renders can
    prepare that posting without re-searching or re-judging (decision 174)."""
    with _JUDGED_LOCK:
        for m in matches:
            if m.fit_score is None:
                continue
            _JUDGED_MATCHES.pop(m.posting.url, None)   # re-insert so eviction drops the oldest
            _JUDGED_MATCHES[m.posting.url] = m
        while len(_JUDGED_MATCHES) > _JUDGED_CAP:
            _JUDGED_MATCHES.pop(next(iter(_JUDGED_MATCHES)))
    return [{
        "company": m.posting.company, "title": m.posting.title,
        "location": m.posting.location, "compensation": m.posting.compensation,
        "url": m.posting.url, "ats": m.posting.ats,
        "fit_score": m.fit_score, "qualified": m.qualified,
        "dimensions": m.dimensions or None,
        "why": m.why, "missing": (m.missing or [])[:3],
        "cleared": (m.fit_score is not None and m.fit_score >= min_fit),
    } for m in matches if m.fit_score is not None]


def _test_worker(force_fresh: bool = False, mode: str = "apply") -> None:
    """Run the testing-mode pipeline in the background, updating _TEST_STATE.
    `force_fresh` bypasses the discovery snapshot cache and re-searches every board.
    `mode="tailor"` stops after the résumé is tailored and exported (no browser, no form fill) —
    the "just the tailoring step" dry run (decision 163); `mode="apply"` runs the whole thing."""
    from . import backends, pipeline
    from .filters import load_filters

    try:
        resume = load_resume("profile/resume.yaml")
        filters = load_filters()
        try:
            profile = apply_profile.load_profile()
        except Exception:
            profile = None

        if not filters.boards and not filters.adzuna.app_id:
            _set(phase="error", errors=["No discovery sources configured. Add a broad aggregator or a target company in the Discover tab."])
            return

        use_claude = backends.claude_code_available()
        _set(step="discover", message=("Re-searching every source (ignoring cache)…" if force_fresh
                                       else "Discovering postings from your sources…"))

        def on_judge(done, total):
            _set(step="match", judged=done, judged_total=total,
                 message=f"Judging fit with Claude — {done}/{total} postings…")

        # Show only openings not surfaced by a previous run (decision 053), so re-running
        # doesn't keep listing the same postings. "Re-search fresh" (force_fresh) shows
        # everything again — the user explicitly asked to see the full board result.
        res = pipeline.discover_and_match(resume, filters, profile=profile,
                                          use_claude=use_claude, on_progress=on_judge,
                                          force_fresh=force_fresh, only_new=not force_fresh)
        # Outcome calibration can raise min_fit above a proven-dead fit band (decision 043
        # follow-up); the note is shown with the judged list so the cutoff is never a mystery.
        min_fit, calib_note = pipeline.effective_min_fit(filters)
        # Surface every Claude-judged posting — accepted AND denied — so the user can see what
        # the searches return and why each is rejected (ranked best-first).
        judged = _judged_rows(res.matches, min_fit)
        cache_age_min = int((res.cache_age_seconds or 0) // 60) if res.from_cache else None
        _set(scanned=res.discovered, matched=len(res.matches), errors=res.errors,
             skipped_seen=res.skipped_seen, skipped_shown=res.skipped_shown, judged=judged,
             min_fit=min_fit, calib_note=calib_note, from_cache=res.from_cache,
             cache_age_min=cache_age_min, funnel=res.funnel)
        if not res.matches:
            extra = ["No new postings matched your qualifications."]
            if res.skipped_shown:
                extra.append(f"({res.skipped_shown} opening(s) already shown in an earlier run "
                             "were hidden — use “Re-search fresh” to see them all again.)")
            if res.skipped_seen:
                extra.append(f"({res.skipped_seen} already in your tracker were skipped.)")
            _set(phase="error", errors=(res.errors or []) + extra, can_research=True)
            return

        top = pipeline.pick_top(res.matches, min_fit=min_fit)
        if top is None:
            best = max((m.fit_score for m in res.matches if m.fit_score is not None), default=None)
            best_txt = f" Best fit this run was {best}/100." if best is not None else ""
            _set(phase="error", can_research=True, errors=[
                f"No match reached your minimum fit of {min_fit}/100, so nothing was "
                f"applied to.{best_txt} See the judged postings below for why. To find a match: "
                "re-search fresh below, lower “Minimum fit score”, raise “How many top matches "
                "Claude judges”, set “Experience levels” to your level (so senior roles are "
                "filtered out before judging), or add boards that better fit your résumé — the "
                "last four in Discovery settings."])
            return
        p = top.posting
        chosen = {
            "company": p.company, "title": p.title, "location": p.location,
            "compensation": p.compensation, "url": p.url, "ats": p.ats,
            "fit_score": top.fit_score, "qualified": top.qualified,
            "dimensions": top.dimensions or None,
            "why": top.why, "missing": top.missing,
            "judged_by": top.judged_by,
        }
        _set(chosen=chosen, message=f"Best match: {p.company} — {p.title}. Tailoring…")

        def status_cb(step, message):
            _set(step=step, message=message.lstrip("▶ ").strip())

        if mode == "tailor":
            # Tailor-only: tailor + export the PDF for the best match and stop. No browser opens
            # and nothing is filled, so there is no application to record — this is the "show me
            # the résumé this job would get" pass. The PDF is still written to the posting's
            # reusable path with its stamp, so a later apply run can reuse it.
            prof = profile or apply_profile.ApplicationProfile()
            held: dict = {}
            pdf = pipeline.tailor_and_render(
                resume, prof, p.to_job_description(), p.company, p.title, p.url,
                backend="auto", status_cb=status_cb,
                on_result=lambda r: held.update(result=r))
            r = held.get("result")
            _set(phase="done", step="done",
                 message=f"Tailored your résumé for {p.company} — {p.title}. Nothing was filled "
                         "or submitted.",
                 tailored={
                     "pdf": str(pdf), "company": p.company, "title": p.title, "url": p.url,
                     "html": render_html(apply_profile.resume_with_profile_links(resume, prof),
                                         r.tailored) if r else "",
                     "notes": (r.tailored.relevance_notes if r else []),
                     "warnings": (r.warnings if r else []),
                     "backend": (r.backend if r else ""),
                 })
            return

        def on_filled(report):
            _set(phase="filled", step="review",
                 message="Filled — review the browser window. Nothing was submitted.",
                 report={"summary": report.summary(), "submitted": report.submitted,
                         "url": report.url, "screenshot": report.screenshot})

        _TEST_HOLD.clear()
        report = pipeline.run_testing_mode(
            resume, top, "profile/resume.yaml", apply_profile.DEFAULT_PATH,
            backend="auto", headed=True, pause=True,
            status_cb=status_cb, hold=_TEST_HOLD, on_filled=on_filled,
        )
        done_msg = ("Done — you submitted this application manually; it's recorded as Applied in Track."
                    if report.submitted else
                    "Done — browser closed. A dry-run row was recorded in Track.")
        _set(phase="done", step="done", message=done_msg,
             report={"summary": report.summary(), "submitted": report.submitted,
                     "url": report.url, "screenshot": report.screenshot})
    except Exception as e:
        _set(phase="error", errors=[f"{type(e).__name__}: {e}"])


def start_test_run(force_fresh: bool = False, mode: str = "apply") -> dict:
    if _loop_running():
        return {"ok": False, "error": "The auto-apply loop is running (it owns the browser). "
                "Stop the loop first, or use its Apply buttons."}
    with _TEST_LOCK:
        if _TEST_STATE.get("phase") == "running":
            return {"ok": False, "error": "A test run is already in progress."}
        _TEST_STATE.clear()
        _TEST_STATE.update(_test_reset())
        _TEST_STATE["mode"] = mode
    threading.Thread(target=_test_worker,
                     kwargs={"force_fresh": force_fresh, "mode": mode}, daemon=True).start()
    return {"ok": True}


def _match_for_url(url: str):
    """The judged Match for one posting URL, or None (decision 174). Served from the in-memory
    index every judged search fills; on a miss (the server restarted since that search) it falls
    back to scanning the freshest discovery snapshot, which carries the same cached fit scores."""
    with _JUDGED_LOCK:
        hit = _JUDGED_MATCHES.get(url)
    if hit is not None:
        return hit
    from . import pipeline
    from .filters import load_filters
    try:
        profile = apply_profile.load_profile()
    except Exception:
        profile = None
    for m in pipeline.cached_matches(load_resume("profile/resume.yaml"), load_filters(),
                                     profile=profile):
        if m.posting.url == url:
            return m
    return None


def _mark_ready(row, company: str, title: str, fit, notifier, *, notify_ready: bool = True) -> str:
    """Register one just-prepared application in the "Ready to apply" queue and fire the
    human-in-the-loop notification (decision 135). Returns "ready" when it newly became ready,
    "blocked" when the fill stopped on something needing the user, "" otherwise.

    Shared by the loop's own `prepare_one` and an Apply click on the search breakdown
    (decision 174), so both land in the same list with the same notification.

    `notify_ready=False` (apply mode, decision 176) still registers it but skips the "ready for
    your approval" push — the caller submits it seconds later, so asking the user to go review
    and submit it would be a lie. The blocked notification is never suppressed: that one is
    genuinely waiting on the user."""
    from . import notifications

    who = f"{company} — {title}".strip(" —")
    app_id = row["id"] if row else None
    newly_ready = False
    with _LOOP_LOCK:
        # A clean dry-run row is "ready to apply"; a blocked one goes to the parked panel
        # instead (parking.py), so it never shows as ready.
        if row and row.get("status") == "dry-run" and row["id"] not in _LOOP_STATE["ready_ids"]:
            _LOOP_STATE["ready_ids"].append(row["id"])
            newly_ready = True
    if newly_ready:
        if not notify_ready:
            return "ready"
        # Tell the user up front whether this one rode a fresh tailor or a reused résumé
        # (decision 144), so "reused" is never a surprise discovered only after applying.
        src = (row or {}).get("resume_source", "")
        src_line = f" Résumé: {src}." if src else ""
        _record_and_push(notifier, notifications.Notification(
            event=notifications.APPROVAL_NEEDED,
            title="Ready to apply",
            body=f"{who} (fit {fit}) is ready.{src_line} Open ApplicationBot → "
                 f"Notifications to review and submit.",
            link="/#notifications"), app_id)
        return "ready"
    if row and row.get("status") == "blocked":
        detail = row.get("blocked_detail") or row.get("blocked_kind") or "needs your input"
        _record_and_push(notifier, notifications.Notification(
            event=notifications.INTERVENTION_NEEDED,
            title="Application needs you",
            body=f"{who} is blocked: {detail}. Open ApplicationBot → Notifications on your "
                 f"Mac to resolve it.",
            link="/#notifications", urgent=True), app_id)
        return "blocked"
    return ""


def _prepared_msg(outcome: str, who: str, row, live: bool = False) -> str:
    """What to tell the user after preparing one posting they clicked Apply on — where the
    application went and what they do next (UI Principle #3/#5). `live=True` means the caller
    submits it immediately (decision 176), so it says that instead of asking for a click."""
    if outcome == "ready":
        src = (row or {}).get("resume_source", "")
        if live:
            return (f"{who} is filled — submitting it now."
                    + (f" Résumé: {src}" if src else ""))
        return (f"{who} is ready to submit — review it under “Ready to apply” below (also in "
                f"Notifications) and click Apply there to really send it."
                + (f" Résumé: {src}" if src else ""))
    if outcome == "blocked":
        detail = (row or {}).get("blocked_detail") or (row or {}).get("blocked_kind") or "needs your input"
        return (f"{who} filled but stopped: {detail}. Resolve it under “Blocked — needs you” "
                "in Notifications, then submit from there.")
    return (f"{who} was filled as a dry-run but isn't waiting for approval — its tracker row is "
            "not a fresh dry-run (it may already have been submitted). Check it in Track.")


def _judged_prepare(url: str, tailor: bool, status_cb=None,
                    live: bool = False) -> "tuple[str, dict | None]":
    """Prepare ONE posting the user clicked Apply / Apply anyway on in the search breakdown
    (decision 174) — including one Claude scored BELOW min_fit, which the automatic queue drops.

    Does exactly what the loop's `prepare_one` does: tailor (unless the user turned tailoring
    off), fill the form headless as a DRY RUN, record the tracker row, and hand it to the
    "Ready to apply" queue. It never submits — `gate=None`. `live=True` (an apply-mode loop,
    decision 176) only changes what the user is TOLD and suppresses the redundant "ready for
    your approval" push; the submit itself is the caller's, through the same armed one-shot
    gate every other submit runs under (Agent Guideline #3).

    Runs on the calling thread; `start_judged_prepare` picks that thread (its own, or the loop's
    when the loop owns the browser) and passes the `status_cb(step, message)` that reports
    progress through that thread's status. Returns `(message for the user, tracker row)`."""
    from . import notifications, pipeline

    m = _match_for_url(url)
    if m is None:
        raise LookupError(
            "That posting's scored details are gone — the app restarted, or the saved search "
            "expired. Search again with “Find & fill one (dry-run)” or the auto-apply loop, "
            "then click Apply on the new results.")
    p = m.posting
    who = f"{p.company} — {p.title}".strip(" —")
    pipeline.run_testing_mode(
        load_resume("profile/resume.yaml"), m, "profile/resume.yaml", apply_profile.DEFAULT_PATH,
        backend="auto", headed=False, slow_mo=0, pause=False, gate=None, tailor=tailor,
        status_cb=status_cb)
    row = tracker.find_by_source_url(p.url)
    outcome = _mark_ready(row, p.company, p.title, m.fit_score, notifications.build_notifier(),
                          notify_ready=not live)
    return _prepared_msg(outcome, who, row, live=live), row


def _prepare_reset() -> dict:
    """A fresh `_TEST_STATE` for an Apply click on the search breakdown, KEEPING the breakdown
    itself (judged list, cutoff, funnel, counts). Resetting those would erase the list the user
    just clicked in — they'd watch their own results vanish as the application is prepared.

    Reads `_TEST_STATE` directly, like `_test_reset`: call it with `_TEST_LOCK` held (`_TEST_LOCK`
    is a plain Lock, so taking it again here would deadlock its caller)."""
    keep = {k: _TEST_STATE.get(k) for k in
            ("judged", "min_fit", "calib_note", "funnel", "scanned", "matched",
             "skipped_seen", "skipped_shown")}
    state = _test_reset()
    state.update({k: v for k, v in keep.items() if v is not None})
    return state


def _judged_prepare_worker(url: str, tailor: bool) -> None:
    """`_judged_prepare` on its own thread, reporting into the run status panel — the panel directly
    above the search breakdown the user clicked in — and then SUBMITTING it on that same thread
    (decision 177): the button says "Apply", so the click applies, exactly as it does while an
    apply-mode loop is running. The user confirmed the submit in the UI before the request was sent;
    the armed one-shot gate, the KILL file and the pre-submit required-field check are unchanged. A
    fill that came out blocked is never submitted — it parks and waits, as it always did."""
    try:
        with _JUDGED_LOCK:
            hit = _JUDGED_MATCHES.get(url)
        p = getattr(hit, "posting", None)
        if p is not None:
            _set(chosen={"company": p.company, "title": p.title, "location": p.location,
                         "compensation": p.compensation, "url": p.url, "ats": p.ats,
                         "fit_score": hit.fit_score, "qualified": hit.qualified,
                         "dimensions": hit.dimensions or None, "why": hit.why,
                         "missing": hit.missing},
                 message=(f"Preparing {p.company} — {p.title}"
                          + ("…" if tailor else " with your résumé as-is (no tailoring)…")))
        message, row = _judged_prepare(url, tailor, live=True, status_cb=lambda step, msg: _set(
            step=step, message=msg.lstrip("▶ ").strip()))
        with _LOOP_LOCK:
            ready = bool(row and row["id"] in _LOOP_STATE["ready_ids"])
        if ready:
            _set(step="apply", message=message)
            message, _ok = _armed_submit(
                row["id"], lambda msg, current=None: _set(step="apply", message=msg))
        _set(phase="done", step="done", message=message)
    except Exception as e:
        _set(phase="error", errors=[str(e) if isinstance(e, LookupError) else f"{type(e).__name__}: {e}"])


def _reapply_gate(arm: bool):
    """The SafetyGate a re-apply runs under. `arm=True` → a per-click armed gate (decision 058):
    armed for exactly ONE submission, independent of profile/safety.yaml, but the global KILL file
    still halts it (checked in `may_submit`). `arm=False` → None, so run_apply stays a dry-run."""
    if not arm:
        return None
    from .safety import DEFAULT_KILL, SafetyGate
    return SafetyGate(armed=True, max_submissions_per_run=1, kill_file=DEFAULT_KILL)


def _reapply_worker(app_id: int, *, arm: bool = False, retailor: bool = False) -> None:
    """Resume a parked application (decision 049): re-drive the DETERMINISTIC fill on the same
    posting URL with the stored tailored PDF, now that the user has resolved the block (answered
    the question, stored the login). No re-discovery — the answer/profile change is all that's
    new, so the same form fills further.

    `retailor=True` (decision 086) first regenerates the résumé from the posting's SAVED job
    description (`resume_store.read_jd`) + the user's current base résumé/prompt/layout, then fills
    with the fresh PDF — the Track "Re-run → re-tailor" choice. `retailor=False` reuses the stored
    PDF as-is (the default, fast, no Claude call).

    `arm=False` (default) → DRY-RUN: fills, records, never submits. `arm=True` (decision 058) →
    a per-click armed submit: a one-shot `SafetyGate(armed=True, cap 1)` is passed to run_apply so
    THIS one application is really submitted — independent of profile/safety.yaml (the user
    confirmed this specific submit in the UI). The KILL file still halts it, and run_apply's
    pre-submit gate still blocks a submit while any REQUIRED field is unresolved, so an unresolved
    block records `blocked` instead of submitting."""
    from . import backends
    from .apply import AnswerResolver, run_apply

    try:
        app = tracker.get_application(app_id)
        if not app:
            _set(phase="error", errors=["That application is no longer in the tracker."])
            return
        url = (app.get("source_url") or "").strip()
        pdf = (app.get("resume_path") or "").strip()
        if not url:
            _set(phase="error", errors=[
                "This application has no source URL to re-apply to. Run a fresh dry-run instead."])
            return
        if not pdf or not Path(pdf).is_file():
            _set(phase="error", errors=[
                "The tailored résumé PDF for this application is gone — run a fresh dry-run for "
                "this posting from Discovery settings instead of re-applying."])
            return

        company, role = app.get("company", ""), app.get("role", "")

        if retailor:
            # Re-tailor from the SAVED job description (no re-scrape) + the user's current résumé.
            from . import pipeline, resume_store
            jd = resume_store.read_jd(pdf)
            if jd is None:
                _set(phase="error", errors=[
                    "This posting has no saved job description, so it can't be re-tailored "
                    "(it predates that feature). Run a fresh dry-run for it from Discovery, or "
                    "re-run reusing the stored résumé instead."])
                return
            _set(step="tailor", message=f"Re-tailoring résumé for {company} — {role}…".strip(" —"),
                 chosen={"company": company, "title": role, "url": url})
            pdf = pipeline.tailor_and_render(
                load_resume("profile/resume.yaml"), apply_profile.load_profile(), jd,
                company, role, url, status_cb=lambda step, message: _set(step=step, message=message))

        verb = "Submitting" if arm else "Re-applying"
        _set(step="apply", message=f"{verb} to {company} — {role}…".strip(" —"),
             chosen={"company": company, "title": role, "url": url})

        resolver = AnswerResolver(
            resume=load_resume("profile/resume.yaml"),
            profile=apply_profile.load_profile(),
            enable_generation=backends.claude_code_available(),
        )
        gate = _reapply_gate(arm)

        def on_filled(report):
            _set(phase="filled", step="review",
                 message=("Filled — submitting…" if arm else
                          "Re-filled — review the browser window. Nothing was submitted."),
                 report={"summary": report.summary(), "submitted": report.submitted,
                         "url": report.url, "screenshot": report.screenshot})

        _TEST_HOLD.clear()
        from . import reuse
        report = run_apply(
            url, pdf, resolver, headed=True, pause=True,
            meta={"company": company, "role": role, "source_url": url,
                  "fit_score": app.get("fit_score") or None,
                  # Re-tailor regenerated the résumé; otherwise this run reuses the stored PDF
                  # as-is — record which (decision 144).
                  "resume_source": reuse.FRESH if retailor else reuse.stored_reuse_label()},
            hold=_TEST_HOLD, on_filled=on_filled, gate=gate,
        )
        from . import parking
        still = parking.classify(report)
        if report.submitted and report.submit_state == "submitted":
            done_msg = f"Submitted to {company} — {report.confirmation or 'confirmation seen'}.".strip()
        elif arm and report.submit_state in ("unconfirmed", "blocked"):
            done_msg = (f"Not submitted — {report.submit_state}: "
                        + (report.confirmation or "; ".join(report.blockers) or "see the browser"))
        elif still and still.resumable:
            done_msg = f"Re-filled — still blocked: {still.summary}"
        elif still:
            done_msg = f"Re-filled — {still.summary}"
        else:
            done_msg = "Re-filled cleanly — the block is cleared. It's ready for the runner to submit."
        _set(phase="done", step="done", message=done_msg,
             report={"summary": report.summary(), "submitted": report.submitted,
                     "url": report.url, "screenshot": report.screenshot})
    except Exception as e:
        _set(phase="error", errors=[f"{type(e).__name__}: {e}"])


def start_reapply(app_id: int, *, arm: bool = False, retailor: bool = False) -> dict:
    if _loop_running():
        return {"ok": False, "error": "The auto-apply loop is running (it owns the browser). "
                "Use its Apply buttons, or stop the loop first."}
    with _TEST_LOCK:
        if _TEST_STATE.get("phase") == "running":
            return {"ok": False, "error": "A run is already in progress — let it finish first."}
        _TEST_STATE.clear()
        _TEST_STATE.update(_test_reset())
    threading.Thread(target=_reapply_worker,
                     kwargs={"app_id": app_id, "arm": arm, "retailor": retailor},
                     daemon=True).start()
    return {"ok": True}


def _retailor_pdf(app: dict, status_cb=None) -> str:
    """Re-tailor ONE prepared application's résumé from its SAVED job description (decision 180)
    and return the new PDF's path, recorded on the tracker row.

    The tailoring control for a single application lives in its review panel, so this is what that
    button runs: the user is looking at one application and decides *this* one deserves a résumé
    written for it (or a fresh pass over the one it has). No re-scrape and no re-judge — the JD was
    stored beside the résumé when the application was prepared. Raises LookupError when there is no
    saved JD, which is the one case that cannot be fixed from here."""
    from . import pipeline, resume_store

    pdf = (app.get("resume_path") or "").strip()
    jd = resume_store.read_jd(pdf) if pdf else None
    if jd is None:
        raise LookupError(
            "This application has no saved job description, so its résumé can't be re-tailored "
            "(it predates that being stored). Apply to the posting again from Discover to tailor "
            "it fresh.")
    company, role = app.get("company", ""), app.get("role", "")
    new_pdf = pipeline.tailor_and_render(
        load_resume("profile/resume.yaml"), apply_profile.load_profile(), jd,
        company, role, (app.get("source_url") or "").strip(), status_cb=status_cb)
    from . import reuse
    tracker.update_application(app["id"], {"resume_path": new_pdf, "resume_source": reuse.FRESH})
    return new_pdf


def _rescan_worker(app_id: int, retailor: bool = False) -> None:
    """Re-read one posting's application form and refresh what the review panel shows about it
    (decision 164): every question, its control type and options, whether the form marks it
    REQUIRED, and the answer the bot now produces for it.

    `retailor=True` (decision 180) re-tailors the résumé from the saved JD first, then re-fills
    with it — the review panel's "Re-tailor résumé" button. Still headless, still `gate=None`.

    A HEADLESS dry-run re-fill — no browser window, no pause, and `gate=None`, so it can never
    submit. It is the same fill the loop's prepare step runs, so it rewrites this posting's
    `report.json` archive (the panel's only source) with current data. Answers the user edited in
    the panel are loaded by `run_apply` itself, so a rescan keeps their edits instead of reverting
    to the bot's originals. Postings change their forms, and reports written before a feature
    landed lack its data — this is how the user refreshes both without opening a browser."""
    from . import backends, reuse
    from .apply import AnswerResolver, run_apply

    try:
        app = tracker.get_application(app_id)
        if not app:
            _set(phase="error", errors=["That application is no longer in the tracker."])
            return
        url = (app.get("source_url") or "").strip()
        pdf = (app.get("resume_path") or "").strip()
        company, role = app.get("company", ""), app.get("role", "")
        who = f"{company} — {role}".strip(" —")
        if not url:
            _set(phase="error", errors=[
                "This application has no source URL, so its form can't be re-read. Run a fresh "
                "dry-run for the posting from Discover instead."])
            return
        if not pdf or not Path(pdf).is_file():
            _set(phase="error", errors=[
                f"The tailored résumé PDF for {who} is gone, and the form can't be filled without "
                "it. Run a fresh dry-run for this posting from Discover instead."])
            return
        source = app.get("resume_source", "") or reuse.stored_reuse_label()
        if retailor:
            _set(step="tailor", message=f"Re-tailoring your résumé for {who}…",
                 chosen={"company": company, "title": role, "url": url})
            pdf = _retailor_pdf(app, status_cb=lambda step, message: _set(step=step, message=message))
            source = reuse.FRESH
        _set(step="apply", message=f"Re-reading the application form for {who}…",
             chosen={"company": company, "title": role, "url": url})
        resolver = AnswerResolver(
            resume=load_resume("profile/resume.yaml"),
            profile=apply_profile.load_profile(),
            enable_generation=backends.claude_code_available(),
        )
        report = run_apply(
            url, pdf, resolver, headed=False, pause=False,
            meta={"company": company, "role": role, "source_url": url,
                  "fit_score": app.get("fit_score") or None,
                  # A plain rescan never re-tailors — it reuses the stored PDF (decision 144); a
                  # re-tailor above has already replaced it with a fresh one (decision 180).
                  "resume_source": source},
            gate=None,
        )
        needed = len([s for s in report.skipped if not str(s).startswith("[")])
        _set(phase="done", step="done",
             message=((f"Re-tailored your résumé for {who} and re-filled the form: "
                       if retailor else f"Rescanned {who}: ")
                      + f"{len(report.filled)} answer(s) ready, "
                      f"{needed} still need attention. Nothing was submitted."),
             report={"summary": report.summary(), "submitted": report.submitted,
                     "url": report.url, "screenshot": report.screenshot})
    except LookupError as e:
        _set(phase="error", errors=[str(e)])
    except Exception as e:
        _set(phase="error", errors=[f"{type(e).__name__}: {e}"])


def start_rescan(app_id: int, retailor: bool = False) -> dict:
    """Run the headless rescan now (the loop is idle, so this thread owns the browser slot).
    `retailor=True` re-tailors the résumé first (decision 180)."""
    if _loop_running():
        return {"ok": False, "error": "The auto-apply loop is running (it owns the browser). "
                "Stop the loop first, then rescan."}
    with _TEST_LOCK:
        if _TEST_STATE.get("phase") == "running":
            return {"ok": False, "error": "A run is already in progress — let it finish first."}
        _TEST_STATE.clear()
        _TEST_STATE.update(_test_reset())
    threading.Thread(target=_rescan_worker,
                     kwargs={"app_id": app_id, "retailor": retailor}, daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------------------- auto-apply loop
# The "prepare-then-prompt" mode (decision 069): discover as many matches as possible, prepare
# each cleared one (tailor → PDF → headless dry-run fill) into a "Ready to apply" queue, and let
# the user submit each with one click. The loop core lives in autoloop.py (pure, tested); this is
# the web glue — one worker thread that OWNS the single browser slot for its lifetime, so the
# test-run / re-apply buttons are refused while it runs (they'd fight for the browser). User
# Apply clicks are enqueued and drained by the loop thread itself, keeping everything serialized.

_LOOP_LOCK = threading.Lock()
_LOOP_STATE: dict = {"running": False, "phase": "idle", "message": "", "prepared": 0,
                     "ready_ids": [], "current": None, "goal": None, "maintain": False,
                     "watch": False, "watch_interval": 30,
                     "show_browser": False,
                     # Last search's breakdown, same shape the test run reports (decision 149),
                     # so the loop shows WHERE its postings went instead of a bare "searching…".
                     "funnel": {}, "judged": [], "min_fit": None, "scanned": 0, "matched": 0,
                     "cleared": 0, "searches": 0, "from_cache": False}
_LOOP_STOP = threading.Event()
_LOOP_SUBMITS: list[int] = []  # app-ids the user clicked "Apply" on, awaiting the loop thread
# The subset of _LOOP_SUBMITS the user asked to WATCH being submitted (decision 179) — the same
# armed submit, but in a visible browser that stays open on the confirmation page.
_LOOP_WATCH_SUBMITS: set[int] = set()
_LOOP_WATCHES: list[int] = []  # app-ids the user clicked "Watch the autofill" on, awaiting the thread
_LOOP_RESCANS: list[int] = []  # app-ids the user clicked "Rescan questions" on, awaiting the thread
# The subset of _LOOP_RESCANS the user asked to RE-TAILOR first (decision 180) — same refresh job,
# with a new résumé written for that posting before the form is re-filled.
_LOOP_RETAILORS: set[int] = set()
# (posting URL, tailor?) pairs the user clicked "Apply"/"Apply anyway" on in the search breakdown
# (decision 174) — postings, not tracker rows: nothing has been prepared for them yet.
_LOOP_PREPARES: list[tuple] = []
_LOOP_WATCH_HOLD = threading.Event()  # set to release an in-progress watch (window close or Stop)

# Goal mode keeps hunting when a pass finds nothing new (decision 146). How long it idles before
# the n-th consecutive empty pass — short at first (a fresh board search may well turn something
# up), then escalating to a 30-min ceiling so a long hunt doesn't hammer the boards (Guideline #4).
_HUNT_BACKOFF_SECONDS = (60, 120, 300, 900, 1800)


def _hunt_backoff(n: int) -> int:
    return _HUNT_BACKOFF_SECONDS[min(max(n, 1), len(_HUNT_BACKOFF_SECONDS)) - 1]


def _ready_cards(ready_ids, *, log=None, path=None) -> list[dict]:
    """Applications prepared and waiting for the user's OK, as review cards (decision 183).

    Unioned from the in-memory loop queue (`ready_ids`) AND the still-`dry-run` applications named
    by `approval_needed` log rows, so one prepared earlier stays reviewable/submittable after the
    in-memory queue was cleared — by a server restart, or by starting the next loop run (which
    resets `ready_ids`). Loop-queue ones first, then the restored ones, deduped.

    Shared by the Notifications action center and the Discover loop panel so both list exactly the
    same ready work; `log` lets a caller that already read the notification log pass it in."""
    kw = {"path": path} if path is not None else {}
    if log is None:
        log = tracker.list_notifications(limit=100, **kw)
    ids = list(ready_ids) + [n["application_id"] for n in log
                             if n["event"] == "approval_needed" and n.get("application_id")]
    ready, seen = [], set()
    for aid in ids:
        if aid in seen:
            continue
        seen.add(aid)
        a = tracker.get_application(aid, **kw)
        if a and a.get("status") == "dry-run":
            ready.append({"id": aid, "company": a["company"], "role": a["role"],
                          "fit": a.get("fit_score"), "portal": a["portal"], "url": a["source_url"],
                          "resume_source": a.get("resume_source", "")})
    return ready


def _build_inbox(ready_ids, *, path=None) -> dict:
    """The Notifications action center payload (decisions 138 + 145). Everything needing the user
    now — applications ready to submit + blocked ones needing a fix — as ACTION cards, with the
    durable notification LOG below. Pure over the tracker so it's unit-testable.

    `ready` comes from `_ready_cards` (see there for how it survives a restart). Each log row is
    tagged `actionable` — whether its application is currently shown as a card above — so the feed
    can hide those and be the record of PAST notifications only, never a duplicate. `count`
    (ready + parked) drives the nav badge."""
    from . import parking
    kw = {"path": path} if path is not None else {}
    log = tracker.list_notifications(limit=100, **kw)
    ready = _ready_cards(ready_ids, log=log, **kw)
    parked = []
    for a in tracker.parked_applications(**kw):
        d = parking.describe(a.get("blocked_kind", ""), a.get("blocked_detail", ""))
        parked.append({"id": a["id"], "company": a["company"], "role": a["role"],
                       "portal": a["portal"], "source_url": a["source_url"],
                       "status": a["status"], **d})
    actionable_ids = {a["id"] for a in ready} | {a["id"] for a in parked}
    for n in log:
        aid = n.get("application_id")
        app = tracker.get_application(aid, **kw) if aid else None
        n["app_status"] = app.get("status") if app else None
        n["actionable"] = aid in actionable_ids
    return {"ready": ready, "parked": parked, "count": len(ready) + len(parked),
            "notifications": log, "unread": tracker.unread_notification_count(**kw)}


def _record_and_push(notifier, note, app_id: int | None = None) -> None:
    """Record every fired notification in the durable log (so the Notifications tab shows it and
    it survives the app being submitted/cleared or the loop resetting), THEN push it via the
    configured transports (desktop/ntfy). Gated by the event toggle — a disabled event neither
    logs nor pushes. Recording is best-effort: a DB hiccup must never break the loop (decision 145)."""
    if not notifier.cfg.event_enabled(note.event):
        return
    try:
        tracker.add_notification(
            note.event, note.title, note.body, link=note.link, application_id=app_id,
            urgent=note.urgent, channels=",".join(c.name for c in notifier.channels))
    except Exception:
        pass  # a logging failure must not stop the loop or suppress the push
    notifier.notify(note)


def _notify_config_from_payload(d: dict):
    """Build a NotifyConfig from the settings-panel payload. Accepts the same nested shape the
    GET returns (``{desktop, ntfy:{enabled,topic,server}, events:{...}}``) so the round-trip is
    symmetric."""
    from . import notifications as notif
    ntfy = d.get("ntfy") or {}
    events = d.get("events") or {}
    return notif.NotifyConfig(
        desktop=bool(d.get("desktop", True)),
        ntfy_enabled=bool(ntfy.get("enabled", False)),
        ntfy_topic=str(ntfy.get("topic", "") or "").strip(),
        ntfy_server=str(ntfy.get("server") or "https://ntfy.sh").strip(),
        approval_needed=bool(events.get(notif.APPROVAL_NEEDED, True)),
        intervention_needed=bool(events.get(notif.INTERVENTION_NEEDED, True)),
    )


# The loop's résumé policy lives in pipeline.py so the CLI night run (decision 186) and this
# server decide a posting's résumé the same way; these names are the pre-move call sites.
from .pipeline import loop_policy as _loop_policy, tailor_choice as _tailor_choice  # noqa: E402


def _loop_reset() -> dict:
    return {"running": True, "phase": "starting", "message": "Starting…",
            "prepared": 0, "submitted": 0, "ready_ids": [], "current": None, "goal": None,
            "maintain": False, "dry_run": False, "watch": False, "watch_interval": 30,
            "show_browser": False, "cap": 0, "cap_hit": False,
            "funnel": {}, "judged": [], "min_fit": None, "scanned": 0, "matched": 0,
            "cleared": 0, "searches": 0, "from_cache": False}


def _loop_set(**kw) -> None:
    with _LOOP_LOCK:
        _LOOP_STATE.update(kw)


def _loop_running() -> bool:
    with _LOOP_LOCK:
        return bool(_LOOP_STATE.get("running"))


def _armed_submit(app_id: int, note, *, headed: bool = False, hold=None) -> tuple[str, bool]:
    """Armed one-shot submit of ONE prepared application on the CALLING thread (the thread that
    owns the browser). Reuses the per-click armed SafetyGate (decision 058): armed for exactly one
    submission, independent of profile/safety.yaml, still halted by the KILL file and the
    pre-submit required-field gate. A block/unconfirmed reports that outcome — never a silent
    submit.

    Headless by default — nothing to see, fastest per application. `headed=True` (decision 179)
    runs the SAME submit in a visible browser so the user can watch the form fill and the Submit
    click happen; `hold` (a threading.Event, which the CALLER must clear before passing) additionally
    leaves that window open on the confirmation page until the user closes it or the event is set.
    `headed` changes only what the user sees: the gate, the fill and the recording are identical.

    `note(message, current=None)` streams progress into whichever panel the caller owns — the loop
    status for the loop thread, the run panel for an Apply click while the loop is idle. Returns
    `(final message, submitted?)`; the caller decides what to count."""
    from . import backends
    from .apply import AnswerResolver, run_apply

    app = tracker.get_application(app_id)
    if not app:
        return "That application is no longer in the tracker.", False
    url = (app.get("source_url") or "").strip()
    pdf = (app.get("resume_path") or "").strip()
    company, role = app.get("company", ""), app.get("role", "")
    who = f"{company} — {role}".strip(" —")
    if not url or not pdf or not Path(pdf).is_file():
        return f"Can't submit {who}: its URL or tailored PDF is missing.", False
    note(f"Submitting to {who}…" + (
        " A browser opened — watch it fill and click Submit."
        if headed and hold is not None else
        " A browser opened — watch it fill and click Submit; it closes itself when done."
        if headed else ""),
         {"company": company, "role": role, "fit": app.get("fit_score")})
    resolver = AnswerResolver(
        resume=load_resume("profile/resume.yaml"),
        profile=apply_profile.load_profile(),
        enable_generation=backends.claude_code_available(),
    )
    report = run_apply(
        url, pdf, resolver, headed=headed, pause=(hold is not None), hold=hold,
        meta={"company": company, "role": role, "source_url": url,
              "fit_score": app.get("fit_score") or None,
              # Submit reuses the prepared PDF as-is — carry its provenance (decision 144).
              "resume_source": app.get("resume_source", "")},
        gate=_reapply_gate(True))
    ok = bool(report.submitted and report.submit_state == "submitted")
    if ok:
        msg = f"Submitted to {who} — {report.confirmation or 'confirmation seen'}."
    elif report.submit_state in ("unconfirmed", "blocked"):
        msg = (f"{who}: not submitted ({report.submit_state}) — "
               + (report.confirmation or "; ".join(report.blockers) or "see the tracker"))
    else:
        msg = f"{who}: filled but not submitted (the arm did not take)."
    # Drop it from the ready list whatever the outcome — a submitted row is no longer 'dry-run',
    # and a re-blocked one moves to the parked panel; either way it shouldn't sit in "ready".
    with _LOOP_LOCK:
        if app_id in _LOOP_STATE["ready_ids"]:
            _LOOP_STATE["ready_ids"].remove(app_id)
    return msg, ok


def _loop_submit(app_id: int) -> None:
    """`_armed_submit` on the loop thread, reporting into the loop status and counting the run's
    submissions — but never past the run's submission cap (decision 178).

    The cap is a ceiling on one run, not a pause: hitting it stops the loop rather than letting it
    keep preparing applications it is no longer allowed to send. Whatever is already prepared stays
    under "Ready to apply" for the user.

    Visible submits (decision 179): the browser is shown when the run was started with "Show the
    browser while it applies" (`show_browser`), or when THIS application is one the user clicked
    "Watch it apply" on. The watched one also HOLDS its window open on the confirmation page (the
    user asked to see it; `_LOOP_WATCH_HOLD` and a manual window close both release it, so a Stop
    is never stuck behind it) — a whole run of visible submits would stall on every window, so
    `show_browser` alone shows the fill and closes itself."""
    with _LOOP_LOCK:
        cap = int(_LOOP_STATE.get("cap") or 0)
        sent = int(_LOOP_STATE.get("submitted", 0))
        watched = app_id in _LOOP_WATCH_SUBMITS
        _LOOP_WATCH_SUBMITS.discard(app_id)
        headed = watched or bool(_LOOP_STATE.get("show_browser"))
    if cap and sent >= cap:
        _loop_set(cap_hit=True, message=(
            f"Submission cap reached — {sent} application(s) submitted this run. This one and "
            f"anything else prepared are waiting under “Ready to apply”. Raise the cap in Loop "
            f"settings to send more."))
        _LOOP_STOP.set()
        return

    def note(message, current=None):
        _loop_set(phase="submitting", current=current, message=message)

    # Arm the release event for this one watched window — but never when a Stop has already landed:
    # clearing it there would re-open a hold the Stop just released, leaving a window nothing closes.
    if watched and not _LOOP_STOP.is_set():
        _LOOP_WATCH_HOLD.clear()
    msg, ok = _armed_submit(app_id, note, headed=headed,
                            hold=_LOOP_WATCH_HOLD if watched else None)
    if ok:
        with _LOOP_LOCK:
            _LOOP_STATE["submitted"] = _LOOP_STATE.get("submitted", 0) + 1
    _loop_set(message=msg + (" Back to preparing." if watched else ""))


def _loop_take_submits() -> list[int]:
    with _LOOP_LOCK:
        ids = list(_LOOP_SUBMITS)
        _LOOP_SUBMITS.clear()
    return ids


def _loop_watch(app_id: int) -> None:
    """Visible DRY-RUN of one prepared application on the loop thread, so the user can watch the
    autofill before signing off (decision 125). Never submits (gate=None). The browser opens and
    stays up until the user closes it (or Stop sets _LOOP_WATCH_HOLD); the loop resumes preparing
    afterward. The re-fill also refreshes this application's archived screenshot + field capture,
    so the review panel reflects what the user just watched."""
    from . import backends
    from .apply import AnswerResolver, run_apply

    app = tracker.get_application(app_id)
    if not app:
        _loop_set(message="That application is no longer in the tracker.")
        return
    url = (app.get("source_url") or "").strip()
    pdf = (app.get("resume_path") or "").strip()
    company, role = app.get("company", ""), app.get("role", "")
    who = f"{company} — {role}".strip(" —")
    if not url or not pdf or not Path(pdf).is_file():
        _loop_set(message=f"Can't watch {who}: its URL or tailored PDF is missing.")
        return
    _loop_set(phase="watching",
              current={"company": company, "role": role, "fit": app.get("fit_score")},
              message=f"Watching {who} fill — a browser opened; close it when you're done and the "
                      "loop resumes. Nothing is submitted.")
    resolver = AnswerResolver(
        resume=load_resume("profile/resume.yaml"),
        profile=apply_profile.load_profile(),
        enable_generation=backends.claude_code_available(),
    )
    _LOOP_WATCH_HOLD.clear()
    run_apply(
        url, pdf, resolver, headed=True, pause=True,
        meta={"company": company, "role": role, "source_url": url,
              "fit_score": app.get("fit_score") or None,
              # Visible dry-run reuses the prepared PDF — carry its provenance (decision 144).
              "resume_source": app.get("resume_source", "")},
        hold=_LOOP_WATCH_HOLD, gate=None)
    _loop_set(message=f"Done watching {who}. Back to preparing.")


def _loop_take_watches() -> list[int]:
    with _LOOP_LOCK:
        ids = list(_LOOP_WATCHES)
        _LOOP_WATCHES.clear()
    return ids


def _loop_rescan(app_id: int) -> None:
    """Re-read one prepared application's form on the loop thread (decision 164) — the same
    headless dry-run `_rescan_worker` runs, routed here because the loop owns the browser while
    it's running. Never submits; no window opens.

    An application the user clicked "Re-tailor résumé" on (decision 180) is tagged in
    `_LOOP_RETAILORS`: its résumé is regenerated from the saved JD first, and the re-fill uses the
    new PDF. Same queue, because it is the same job — refresh this one application's review."""
    from . import backends, reuse
    from .apply import AnswerResolver, run_apply

    with _LOOP_LOCK:
        retailor = app_id in _LOOP_RETAILORS
        _LOOP_RETAILORS.discard(app_id)
    app = tracker.get_application(app_id)
    if not app:
        _loop_set(message="That application is no longer in the tracker.")
        return
    url = (app.get("source_url") or "").strip()
    pdf = (app.get("resume_path") or "").strip()
    company, role = app.get("company", ""), app.get("role", "")
    who = f"{company} — {role}".strip(" —")
    if not url or not pdf or not Path(pdf).is_file():
        _loop_set(message=f"Can't rescan {who}: its URL or tailored PDF is missing.")
        return
    source = app.get("resume_source", "") or reuse.stored_reuse_label()
    if retailor:
        _loop_set(phase="rescanning",
                  current={"company": company, "role": role, "fit": app.get("fit_score")},
                  message=f"Re-tailoring your résumé for {who} — nothing is submitted.")
        try:
            pdf = _retailor_pdf(app)
        except LookupError as e:
            _loop_set(message=f"{e} Back to preparing.")
            return
        source = reuse.FRESH
    _loop_set(phase="rescanning",
              current={"company": company, "role": role, "fit": app.get("fit_score")},
              message=f"Re-reading the application form for {who} — no browser opens and "
                      "nothing is submitted.")
    resolver = AnswerResolver(
        resume=load_resume("profile/resume.yaml"),
        profile=apply_profile.load_profile(),
        enable_generation=backends.claude_code_available(),
    )
    report = run_apply(
        url, pdf, resolver, headed=False, pause=False,
        meta={"company": company, "role": role, "source_url": url,
              "fit_score": app.get("fit_score") or None,
              "resume_source": source},
        gate=None)
    needed = len([s for s in report.skipped if not str(s).startswith("[")])
    _loop_set(message=(("Re-tailored your résumé and re-filled " if retailor else "Rescanned ")
                       + f"{who}: {len(report.filled)} answer(s) ready, {needed} still "
                       "need attention. Back to preparing."))


def _loop_take_rescans() -> list[int]:
    with _LOOP_LOCK:
        ids = list(_LOOP_RESCANS)
        _LOOP_RESCANS.clear()
    return ids


def _loop_take_prepares() -> list[tuple]:
    with _LOOP_LOCK:
        reqs = list(_LOOP_PREPARES)
        _LOOP_PREPARES.clear()
    return reqs


def _loop_worker(rescan: bool = False, force_retailor: bool = False,
                 goal: int | None = None, maintain: bool = False,
                 watch: bool = False, watch_interval_min: int = 30,
                 dry_run: bool = False) -> None:
    from . import autoloop, backends, notifications, pipeline
    from .filters import load_filters
    from .runner import cleared_queue

    try:
        resume = load_resume("profile/resume.yaml")
        filters = load_filters()
        # Push "ready for your approval" / "needs your input" to the user wherever they are
        # (decision 135). Best-effort: a channel failing is swallowed so it never disrupts the
        # loop — the user proves a channel works with the Notifications "Send test" button.
        notifier = notifications.build_notifier()
        try:
            profile = apply_profile.load_profile()
        except Exception:
            profile = None

        if not backends.claude_code_available():
            _loop_set(running=False, phase="error", message=(
                "Sign in to Claude first — the loop needs the fit judge and won't auto-apply on "
                "keyword rank alone. Run `claude` in a terminal, then /login."))
            return
        if not filters.boards and not (filters.adzuna.app_id or os.environ.get("ADZUNA_APP_ID")):
            _loop_set(running=False, phase="error", message=(
                "No discovery sources in Discovery settings. Add a broad aggregator or a target company, then start the loop."))
            return

        min_fit, _ = pipeline.effective_min_fit(filters)
        # The Loop settings popup's values (decision 178), read ONCE here so a run's behaviour is
        # the one the user set before starting it — editing them mid-run never changes a run in
        # flight. The résumé policy comes from discovery.yaml, the submission cap from
        # safety.yaml's existing `max_submissions_per_run` (inert in a dry run: nothing is sent).
        policy = _loop_policy(filters)
        cap = 0 if dry_run else int(safety.load_gate().max_submissions_per_run or 0)
        _loop_set(cap=cap, cap_hit=False)

        # rescan (user opt-in): re-prepare postings that were already scored, REUSING their
        # cached fit scores (decision 037) — no board re-search, no Claude re-judge (a fit
        # score rarely changes between runs). Computed once up front from the freshest
        # snapshot; served as a single bounded batch so it re-prepares the set once, then
        # reports caught-up. If nothing is cached, bail with an actionable message rather than
        # silently doing nothing (UI principle #3).
        # The best Claude fit score seen this run (even below the bar), so a "nothing to
        # prepare" outcome can name the real reason + fix instead of implying the cache is
        # empty (UI Principle #3). Updated by discover_batch / seeded from the cache on rescan.
        best_seen = {"fit": None}

        def _note_best(matches) -> None:
            fits = [m.fit_score for m in matches if m.fit_score is not None]
            if fits:
                top = max(fits)
                if best_seen["fit"] is None or top > best_seen["fit"]:
                    best_seen["fit"] = top

        def _below_bar_msg(prefix: str) -> str:
            """Message for 'matches exist, but none clear min_fit' — the real reason, with the
            one-click fix (lower min_fit in Discovery settings)."""
            best = best_seen["fit"]
            return (f"{prefix} the best fit Claude scored was {best}, below your min_fit of "
                    f"{min_fit}. Lower min_fit in Discovery settings (try {max(1, best)}) to let "
                    "these through, or broaden your boards to surface stronger matches." if best is not None
                    else f"{prefix} nothing has been scored yet — start a normal loop to search and judge.")

        rescan_pool: list = []
        if rescan:
            cached = pipeline.cached_matches(resume, filters, profile=profile)
            _note_best(cached)
            rescan_pool = cleared_queue(cached, min_fit)
            # Re-check has no funnel (nothing was searched), but it still has a judged list —
            # show it so the breakdown isn't blank in this mode either. Best-effort, like every
            # other breakdown write: describing the pool must never block re-preparing it.
            try:
                _loop_set(judged=_judged_rows(cached, min_fit), min_fit=min_fit,
                          matched=len(cached), cleared=len(rescan_pool), from_cache=True)
            except Exception:
                pass
            if not rescan_pool:
                # Distinguish an empty cache from a full cache where nothing clears the bar —
                # the old message claimed "nothing scored" in BOTH cases, sending users to
                # re-run a loop that can't help when the real problem is the min_fit threshold.
                if not cached:
                    msg = ("Nothing recently scored to re-prepare. Start a normal auto-apply "
                           "loop first (it scores and caches matches); then re-check to "
                           "re-prepare them without re-scoring, while the cache is fresh.")
                else:
                    msg = _below_bar_msg(f"Re-prepared nothing: {len(cached)} match(es) are cached, but")
                _loop_set(running=False, phase="caught_up", current=None, message=msg)
                return

        served = {"done": False}
        passes = {"n": 0, "judged": 0}
        prepared_urls: set[str] = set()   # postings this run already prepared — never re-serve

        def _report_scan(res, min_fit_now, batch) -> None:
            """Publish this search's breakdown to the loop status — the same funnel + judged list
            the one-shot test run shows (decision 149), so a running loop reports WHERE its
            postings went instead of only "searching…". Best-effort: this is a display path, and
            failing to describe a search must never stop the loop from preparing it."""
            try:
                _loop_set(funnel=getattr(res, "funnel", {}) or {},
                          judged=_judged_rows(res.matches, min_fit_now), min_fit=min_fit_now,
                          scanned=getattr(res, "discovered", 0), matched=len(res.matches),
                          from_cache=bool(getattr(res, "from_cache", False)),
                          searches=passes["n"], cleared=len(batch))
            except Exception:
                pass

        def discover_batch():
            if rescan:
                # One-shot: serve the pre-scored pool once, then empty ⇒ caught up.
                if served["done"]:
                    return []
                served["done"] = True
                return rescan_pool
            # only_new=True (decision 053/056): each search returns ONLY postings not judged
            # before, so no posting is ever re-judged — the loop spends judge tokens only on
            # genuinely new openings. Since decision 146 the ledger is applied before the judge
            # and records judged postings only, so each pass scores the next-best unjudged slice.
            # Watch mode, and every pass after the first (the goal-mode hunt), re-search the
            # boards fresh (force_fresh) — replaying the cached snapshot would just re-serve
            # postings the ledger already hides.
            passes["n"] += 1
            fresh = watch or passes["n"] > 1
            # Bring back never-reviewed applications (decision 149) on the FIRST pass only: the
            # user gets another chance at what was prepared while they were away, but a
            # multi-pass hunt doesn't re-judge those same postings on every pass.
            revisit = passes["n"] == 1
            res = pipeline.discover_and_match(resume, filters, profile=profile, use_claude=True,
                                              only_new=True, force_fresh=fresh, revisit=revisit)
            if not fresh and res.from_cache and not res.matches:
                # The cached snapshot holds nothing this run hasn't already judged. Go live now
                # rather than reporting "no new matches" off a stale snapshot.
                res = pipeline.discover_and_match(resume, filters, profile=profile,
                                                  use_claude=True, only_new=True, force_fresh=True,
                                                  revisit=revisit)
            for e in res.errors:
                _loop_set(message=f"discovery note: {e}")
            _note_best(res.matches)
            passes["judged"] = sum(1 for m in res.matches if m.fit_score is not None)
            # Never re-serve a posting this run already prepared — a re-surfaced unreviewed one
            # would otherwise be prepared again on every pass without moving the ready count.
            batch = [m for m in cleared_queue(res.matches, min_fit)
                     if m.posting.url not in prepared_urls]
            _report_scan(res, min_fit, batch)
            return batch

        def prepare_one(m):
            """Prepare one match. Returns its application id when the fill came out clean, so the
            loop submits it immediately in apply mode (decision 176); None when it blocked, which
            keeps a half-filled application from being sent."""
            p = m.posting
            _loop_set(phase="preparing",
                      current={"company": p.company, "role": p.title, "fit": m.fit_score},
                      message=f"Preparing {p.company} — {p.title} (fit {m.fit_score})…")
            # Dry-run prepare (gate=None): run_testing_mode reuses the already-tailored PDF when
            # the résumé/profile haven't changed (stamp match) — re-fill only, no Claude
            # re-tailor. This is what makes a rescan of unchanged postings spend zero tokens.
            # The tailoring policy (decision 178) decides per posting whether to tailor at all and
            # whether to force a fresh tailor; force_retailor stays the escape hatch.
            tailor, force = _tailor_choice(policy, m.fit_score, force_retailor)
            pipeline.run_testing_mode(
                resume, m, "profile/resume.yaml", apply_profile.DEFAULT_PATH,
                backend="auto", headed=False, slow_mo=0, pause=False, gate=None,
                force_retailor=force, tailor=tailor, reuse_threshold=policy["reuse_threshold"])
            prepared_urls.add(p.url)
            row = tracker.find_by_source_url(p.url)
            with _LOOP_LOCK:
                _LOOP_STATE["prepared"] += 1
            # Queue it for review and push the human-in-the-loop moment (decision 135): a fresh
            # app awaiting the user's approval, or a blocked one needing intervention. In apply
            # mode there is no approval to ask for — the loop submits it next — so the
            # "ready for you" push is suppressed; a blocked one still notifies.
            outcome = _mark_ready(row, p.company, p.title, m.fit_score, notifier,
                                  notify_ready=dry_run)
            return row["id"] if (row and outcome == "ready") else None

        def prepare_requested(req):
            """Prepare one posting the user clicked Apply / Apply anyway on in the loop's search
            breakdown (decision 174), on this thread because it owns the browser. Same dry-run
            prepare as `prepare_one`, but driven by a URL the user picked — so it also serves
            postings below min_fit, which `discover_batch` never yields. Returns its application
            id (or None) so apply mode submits it like any other prepared application."""
            url, tailor = req
            try:
                message, row = _judged_prepare(
                    url, tailor, live=not dry_run,
                    status_cb=lambda step, msg: _loop_set(message=msg.lstrip("▶ ").strip()))
            except Exception as e:
                _loop_set(message=(str(e) if isinstance(e, LookupError)
                                   else f"Couldn't prepare that posting — {type(e).__name__}: {e}"))
                return None
            prepared_urls.add(url)
            with _LOOP_LOCK:
                _LOOP_STATE["prepared"] += 1
                ready = row and row["id"] in _LOOP_STATE["ready_ids"]
            _loop_set(message=message)
            return row["id"] if ready else None

        def ready_count() -> int:
            """What the goal counts. In dry-run mode that is the applications waiting for the
            user; in apply mode a prepared application is submitted at once and leaves the ready
            list, so the goal counts what was actually SENT plus anything still waiting (a
            blocked or failed submit) — otherwise the count could never reach the goal."""
            with _LOOP_LOCK:
                n = len(_LOOP_STATE["ready_ids"])
                return n if dry_run else n + _LOOP_STATE.get("submitted", 0)

        def on_event(kind, payload=None):
            if kind == "searching":
                _loop_set(phase="searching", current=None,
                          message="Searching every board for new matches…")
            elif kind == "caught_up":
                if watch:
                    n = ready_count()
                    _loop_set(phase="watching", current=None, message=(
                        f"Watching — no new matches right now; re-checking every "
                        f"{watch_interval_min} min. {n} ready for you to review. Stop anytime."))
                elif goal is not None and ready_count() < goal:
                    pass  # short of the goal — the "hunting" event below reports it with the plan
                else:
                    _loop_set(phase="caught_up",
                              message="Caught up — no new matches to prepare.")
            elif kind == "hunting":
                # Goal not met and this pass surfaced nothing new. Say exactly what the pass did,
                # why nothing cleared, and when the next one runs (UI Principle #3/#5).
                n = payload if isinstance(payload, int) else 1
                mins = _hunt_backoff(n) // 60
                judged = passes["judged"]
                best = best_seen["fit"]
                why = (f"judged {judged} posting(s), best fit {best} vs your min_fit of {min_fit}"
                       if judged and best is not None else
                       "the boards returned nothing that hasn't already been judged")
                _loop_set(phase="hunting", current=None, message=(
                    f"{ready_count()} of {goal} ready — pass {n} found no new matches ({why}). "
                    f"Searching again in {mins} min. Stop anytime, or lower min_fit / add boards "
                    f"in Discovery settings to widen the net."))
            elif kind == "goal_reached" and maintain:
                # Holding at the goal in maintain mode: idle until the user applies to some.
                n = payload if isinstance(payload, int) else ready_count()
                _loop_set(phase="holding", current=None, message=(
                    f"Holding at your goal of {goal} ready — apply to some and the loop will "
                    f"refill to keep {goal} ready. Stop anytime."))

        reason = autoloop.auto_apply_loop(
            discover_batch, prepare_one, _loop_take_submits, _loop_submit,
            _LOOP_STOP.is_set, on_event=on_event,
            ready_count=ready_count, goal=goal, maintain=maintain,
            # A stop-responsive idle for maintain mode: waits up to 2s, returns at once on Stop.
            wait=lambda: _LOOP_STOP.wait(2.0),
            take_watch_requests=_loop_take_watches, watch_one=_loop_watch,
            # "Rescan questions" clicks (decision 164) — headless re-reads of one posting's form,
            # served on this thread for the same reason watches are: it owns the browser.
            take_rescan_requests=_loop_take_rescans, rescan_one=_loop_rescan,
            # "Apply"/"Apply anyway" clicks on the search breakdown (decision 174) — prepare one
            # user-picked posting, again on this thread because it owns the browser.
            take_prepare_requests=_loop_take_prepares, prepare_requested_one=prepare_requested,
            # Watch mode: keep re-checking the boards on an interval instead of stopping when
            # caught up. The wait is stop-responsive (_LOOP_STOP.wait returns at once on Stop).
            watch=watch, watch_wait=lambda: _LOOP_STOP.wait(max(1, watch_interval_min) * 60),
            # Goal mode (decision 146): an empty pass while short of the goal backs off and
            # searches again instead of ending the run. Stop-responsive, so Stop ends it at once.
            hunt_wait=lambda n: _LOOP_STOP.wait(_hunt_backoff(n)),
            # Apply mode (decision 176) — the default: submit each application as soon as it is
            # prepared. The "dry run" switch turns this off and the loop only prepares.
            apply_immediately=not dry_run)

        ready_n = ready_count()
        # Report what the run actually DID in the mode it ran in (UI Principle #3): apply mode
        # counts what was submitted (plus anything left waiting — a blocked fill, a refused
        # submit); dry-run mode counts what is prepared and waiting for the user's click.
        with _LOOP_LOCK:
            sent_n = _LOOP_STATE.get("submitted", 0)
            waiting_n = len(_LOOP_STATE["ready_ids"])
            cap_hit = bool(_LOOP_STATE.get("cap_hit"))
        if dry_run:
            did = f"{ready_n} application(s) ready for you to apply"
        else:
            did = (f"{sent_n} application(s) submitted"
                   + (f", {waiting_n} still waiting for you" if waiting_n else ""))
        if cap_hit:
            # The cap is why this run ended — say that, and where to raise it, instead of the
            # generic "Loop stopped" a Stop would print (UI Principle #3).
            _loop_set(running=False, phase="stopped", current=None, message=(
                f"Stopped at your submission cap of {cap} — {did}. Raise the cap in Loop settings "
                "and start again to send more."))
        elif reason == "goal_reached":
            _loop_set(running=False, phase="goal_reached", current=None, message=(
                f"Reached your goal — {did}. "
                + ("Apply to them below, or start the loop again to prepare more."
                   if dry_run else "Start the loop again to apply to more.")))
        elif reason == "caught_up":
            if ready_n == 0 and best_seen["fit"] is not None and best_seen["fit"] < min_fit:
                # Postings WERE found and judged — none just cleared min_fit. Say that, with the
                # fix, instead of "no new matches" (which reads as "the boards are empty").
                _loop_set(running=False, phase="caught_up", current=None,
                          message=_below_bar_msg("No applications were prepared:"))
            else:
                short = (f" (fewer than your goal of {goal} — the boards had no more new matches)"
                         if goal is not None and ready_n < goal else "")
                _loop_set(running=False, phase="caught_up", current=None, message=(
                    f"Caught up — no new matches. {did}{short}. "
                    "Start the loop again later to re-search."))
        else:
            _loop_set(running=False, phase="stopped", current=None,
                      message=f"Loop stopped. {did}.")
    except Exception as e:
        _loop_set(running=False, phase="error", message=f"{type(e).__name__}: {e}")


def start_loop(rescan: bool = False, force_retailor: bool = False,
               goal: int | None = None, maintain: bool = False,
               watch: bool = False, watch_interval: int = 30,
               dry_run: bool = False, show_browser: bool = False) -> dict:
    # Apply mode is the default (decision 176): the loop submits each application it prepares.
    # `dry_run=True` is the switch that turns submission off — it prepares everything and holds
    # each one in "Ready to apply" for a per-application click, the pre-176 behaviour.
    # Goal mode (decision 121): prepare until `goal` applications are ready to review/submit.
    # None/0/negative ⇒ no target (run boards to exhaustion, the pre-goal behaviour).
    if goal is not None and goal <= 0:
        goal = None
    if goal is None:
        maintain = False  # "keep topping up" only means something with a target
    if not dry_run:
        # "Keep topping up as you apply" is a dry-run idea: in apply mode the loop applies to
        # them itself, so the count never drops back below the goal and maintain could only
        # spin. Reaching the goal ends the run instead.
        maintain = False
    # Watch mode (decision 143): keep re-checking the boards on an interval and holding each new
    # match for review — never submits on its own. A one-shot rescan can't also "keep watching".
    if rescan:
        watch = False
    watch_interval = max(1, int(watch_interval or 30))
    # "Show the browser while it applies" (decision 179) shows each SUBMIT. A dry run submits
    # nothing, so there would be nothing to show — don't claim a window that never opens.
    if dry_run:
        show_browser = False
    with _LOOP_LOCK:
        if _LOOP_STATE.get("running"):
            return {"ok": False, "error": "The auto-apply loop is already running."}
        _LOOP_STOP.clear()
        _LOOP_SUBMITS.clear()
        _LOOP_WATCH_SUBMITS.clear()
        _LOOP_WATCHES.clear()
        _LOOP_RESCANS.clear()
        _LOOP_RETAILORS.clear()
        _LOOP_PREPARES.clear()
        _LOOP_WATCH_HOLD.clear()
        _LOOP_STATE.clear()
        _LOOP_STATE.update(_loop_reset())
        _LOOP_STATE["goal"] = goal
        _LOOP_STATE["maintain"] = maintain
        _LOOP_STATE["dry_run"] = dry_run
        _LOOP_STATE["watch"] = watch
        _LOOP_STATE["watch_interval"] = watch_interval
        _LOOP_STATE["show_browser"] = show_browser
    threading.Thread(target=_loop_worker,
                     args=(rescan, force_retailor, goal, maintain, watch, watch_interval,
                           dry_run),
                     daemon=True).start()
    return {"ok": True}


def loop_settings() -> dict:
    """Everything the "Loop settings" popup edits (decision 178), read from where it actually
    lives: the résumé policy + fit cutoff from `profile/discovery.yaml`, the submission cap from
    `profile/safety.yaml`. `effective_min_fit` is reported alongside the configured one so the
    popup can say when outcome calibration is raising the bar above what is typed there."""
    from . import pipeline

    f = filters.load_filters()
    policy = _loop_policy(f)
    effective, note = pipeline.effective_min_fit(f)
    return {"ok": True,
            "min_fit": f.min_fit, "effective_min_fit": effective, "calib_note": note,
            "tailor_mode": policy["mode"], "tailor_below_fit": policy["below"],
            "reuse_threshold": policy["reuse_threshold"],
            "max_submissions_per_run": safety.load_gate().max_submissions_per_run}


def save_loop_settings(d: dict) -> dict:
    """Save the popup. Load-modify-save on each file so nothing else in them is touched — the
    discovery filters keep every board and gate, and safety.yaml keeps `armed` (a cap edit must
    never arm or disarm the system). Values are clamped to what the pipeline can actually use, and
    the saved values are returned so the popup shows what really landed."""
    f = filters.load_filters()
    if "min_fit" in d:
        f.min_fit = max(0, min(100, int(d["min_fit"])))
    if "tailor_mode" in d:
        mode = str(d["tailor_mode"] or "smart").lower()
        if mode not in ("smart", "always", "under", "never"):
            return {"ok": False, "error": f"Unknown tailoring mode {mode!r}."}
        f.tailor_mode = mode
    if "tailor_below_fit" in d:
        f.tailor_below_fit = max(0, min(100, int(d["tailor_below_fit"])))
    if "reuse_threshold" in d:
        f.reuse_threshold = max(0.0, min(1.0, float(d["reuse_threshold"])))
    filters.save_filters(f)
    if "max_submissions_per_run" in d:
        safety.save_max_submissions(int(d["max_submissions_per_run"]))
    return loop_settings()


def stop_loop() -> dict:
    if not _loop_running():
        return {"ok": True, "already": True}
    _LOOP_STOP.set()
    _LOOP_WATCH_HOLD.set()  # release an in-progress watch so a Stop isn't blocked behind it
    _loop_set(message="Stopping after the current step finishes…")
    return {"ok": True}


def _mark_reviewed(app_id: int) -> None:
    """Stamp that the user has now SEEN this application (decision 149) — opening its review
    panel, or clicking Apply/Watch on it. Until that happens discovery keeps re-surfacing the
    posting. Best-effort: a DB hiccup must never block a review or a submit."""
    try:
        tracker.mark_reviewed(app_id)
    except Exception:
        pass


def queue_submit(app_id: int) -> dict:
    """Apply to one prepared application. While the loop runs, enqueue it for the loop thread
    (which owns the browser); otherwise submit directly via the per-click armed re-apply."""
    _mark_reviewed(app_id)
    with _LOOP_LOCK:
        running = bool(_LOOP_STATE.get("running"))
        if running and app_id not in _LOOP_SUBMITS:
            _LOOP_SUBMITS.append(app_id)
    if running:
        return {"ok": True, "queued": True}
    return start_reapply(app_id, arm=True)


def queue_watch_submit(app_id: int) -> dict:
    """Watch one prepared application be SUBMITTED FOR REAL (decision 179) — the same armed
    one-shot submit `queue_submit` runs, but in a visible browser that stays open on the
    confirmation page so the user sees the send happen with their own eyes. The UI confirms this
    is irreversible before calling; the KILL file and the pre-submit required-field check still
    apply.

    While the loop runs it is queued for the loop thread (which owns the browser) and tagged in
    `_LOOP_WATCH_SUBMITS` so that thread runs it headed. With the loop idle, the per-click armed
    re-apply already runs headed and pauses on the result, which is exactly this."""
    _mark_reviewed(app_id)
    with _LOOP_LOCK:
        running = bool(_LOOP_STATE.get("running"))
        if running:
            if app_id not in _LOOP_SUBMITS:
                _LOOP_SUBMITS.append(app_id)
            _LOOP_WATCH_SUBMITS.add(app_id)
    if running:
        return {"ok": True, "queued": True}
    return start_reapply(app_id, arm=True)


def queue_watch(app_id: int) -> dict:
    """Watch one prepared application autofill — a VISIBLE dry-run that never submits. While the
    loop runs, enqueue it for the loop thread (which owns the browser) so it's serialized with
    preparation; otherwise run it directly via the visible re-apply (dry-run, arm=False)."""
    _mark_reviewed(app_id)
    with _LOOP_LOCK:
        running = bool(_LOOP_STATE.get("running"))
        if running and app_id not in _LOOP_WATCHES:
            _LOOP_WATCHES.append(app_id)
    if running:
        return {"ok": True, "queued": True}
    return start_reapply(app_id, arm=False)


def queue_rescan(app_id: int, retailor: bool = False) -> dict:
    """Re-read one posting's application form — a HEADLESS dry-run that never submits and opens
    no window (decision 164). While the loop runs, enqueue it for the loop thread (which owns the
    browser); otherwise run it here. Either way the panel sees it finish by the archived report's
    timestamp changing.

    `retailor=True` (decision 180) writes this one application a fresh résumé from its saved job
    description before the re-fill — the review panel's per-application tailoring control."""
    _mark_reviewed(app_id)
    with _LOOP_LOCK:
        running = bool(_LOOP_STATE.get("running"))
        if running and app_id not in _LOOP_RESCANS:
            _LOOP_RESCANS.append(app_id)
        if running and retailor:
            _LOOP_RETAILORS.add(app_id)
    if running:
        return {"ok": True, "queued": True}
    return start_rescan(app_id, retailor=retailor)


def queue_prepare(url: str, tailor: bool = True) -> dict:
    """"Apply" / "Apply anyway" on a posting in the search breakdown (decision 174): tailor (or
    not), fill the form, and SUBMIT it (decision 177) — the button says Apply, so the click
    applies. The submit is confirmed in the UI before this is called.

    Below-bar postings are the whole point — the automatic queue drops anything under min_fit, so
    this is the only path to an application the user judged worth trying anyway. Nothing about
    min_fit changes: the threshold still governs what runs *automatically*.

    While the loop runs, enqueue it for the loop thread (which owns the browser) — there it follows
    the loop's own mode, so a loop in **dry run** prepares it and holds it under "Ready to apply".
    Otherwise run it here on its own thread, reporting into the run panel above the breakdown."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "That posting has no URL to apply to."}
    with _LOOP_LOCK:
        running = bool(_LOOP_STATE.get("running"))
        if running and not any(u == url for u, _ in _LOOP_PREPARES):
            _LOOP_PREPARES.append((url, bool(tailor)))
    if running:
        return {"ok": True, "queued": True}
    with _TEST_LOCK:
        if _TEST_STATE.get("phase") == "running":
            return {"ok": False, "error": "A run is already in progress — let it finish first."}
        state = _prepare_reset()
        _TEST_STATE.clear()
        _TEST_STATE.update(state)
    threading.Thread(target=_judged_prepare_worker, args=(url, bool(tailor)), daemon=True).start()
    return {"ok": True}


def _merge_checkbox_groups(filled: list[dict]) -> list[dict]:
    """Fold a check-all-that-apply group's per-option rows into ONE answer row.

    `_fill_checkboxes` reports each checked option separately (label repeated, control
    "checkbox"), which the panel would show as the same question several times — and each
    edit box would save to the same label, so only the last would survive. Joined with "; ",
    the exact format `_fill_checkboxes` splits when it re-fills the form."""
    out: list[dict] = []
    at: dict[str, int] = {}
    for f in filled:
        label = f.get("label", "")
        if f.get("control") != "checkbox" or label not in at:
            if f.get("control") == "checkbox":
                at[label] = len(out)
            out.append(f)
            continue
        row = out[at[label]]
        parts = [p for p in str(row.get("value", "")).split("; ") if p]
        v = str(f.get("value", ""))
        if v and v not in parts:
            parts.append(v)
        row["value"] = "; ".join(parts)
    # A wizard can put the SAME question on two pages, and each page now fills it (decision 169).
    # Identical rows are one answer as far as the user is concerned — showing it twice would give
    # them two edit boxes writing to one key. Fields that genuinely differ carry a " #n" key and
    # so are never folded here.
    seen: set = set()
    deduped: list[dict] = []
    for f in out:
        sig = (f.get("label", ""), f.get("value", ""), f.get("control", ""))
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(f)
    return deduped


# --- Answer-shape checks (decision 166) -------------------------------------------------------
#
# The failure these catch is NOT a blank field — it's an answer that is well-formed and plausible
# but belongs to a different question, which reads as "filled correctly" in a screenshot and in
# the review table. A bare "Date" field answered "I'm available immediately…" is the case that
# prompted this. Each rule fires only when the question's expected shape is unambiguous, so a
# flag always means "look at this", never "the bot was unsure" (that's the unanswered table).
_DATE_SHAPE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d", re.I)
_YES_NO_A = re.compile(r"^(yes|no|y|n|true|false|n/?a)\b", re.I)
_YES_NO_Q = re.compile(
    r"^(are|do|does|did|have|has|had|is|was|were|will|would|can|could|may|must|should)\b")
# A question that opens like a Yes/No but genuinely wants prose or a value.
_WANTS_PROSE = ("why", "explain", "describe", "tell us", "tell me", "how many", "how much",
                "which", "what", "who", "where", "when", "list", "if so", "if yes", "share",
                "example", "highlight", "elaborate", "walk us", "anything else", "provide")
# Free-text controls only: an answer picked from the form's OWN option list can't be off-shape.
_FREE_CONTROLS = ("", "text", "textarea", "date")


def _answer_flag(label: str, value: str, control: str = "") -> str:
    """Why `value` looks wrong FOR `label`, phrased as what to check, or "" when it looks right."""
    from .apply import _is_todays_date_q, _norm  # lazy: keeps the web import light

    n, v = _norm(label), (value or "").strip()
    if not n or not v or control == "file":
        return ""
    digits = sum(c.isdigit() for c in v)
    # Date fields. Only SHORT, date-owning labels ("Date", "Date of birth", "Graduation date") —
    # a long question that merely mentions dates may legitimately be answered in prose. Start-date
    # and availability questions are excluded for the same reason ("Immediately" is a real answer).
    is_date_q = _is_todays_date_q(n) or (
        len(n) <= 40 and re.search(r"\bdates?\b", n)
        and not any(t in n for t in ("start", "available", "availability", "notice")))
    # A date answer is a date and little else: the sentence that stole this field ("I'm available
    # immediately, with confirmed graduation in May 2027.") does contain a month and a year, so
    # containing a date isn't enough — it has to be one.
    if is_date_q and (len(v) > 30 or not _DATE_SHAPE.search(v)):
        return ("This field asks for a date and the answer isn't one — a form's bare “Date” is "
                "the day you apply.")
    # "gpa" as a whole word only — as a substring it hits a URL in the question text
    # ("blo(g.pa)lantir.com" normalises to "blogpalantircom").
    if (any(t in n for t in ("how many", "number of", "years of experience"))
            or re.search(r"\bgpa\b", n)) and not digits:
        return "This field asks for a number and the answer doesn't contain one."
    if "email" in n and "@" not in v:
        return "This field asks for an email address and the answer isn't one."
    if any(t in n for t in ("phone", "mobile number", "cell")) and digits < 7:
        return "This field asks for a phone number and the answer doesn't look like one."
    if any(t in n for t in ("linkedin", "github", "website", "portfolio", "url")) \
            and not re.search(r"https?://|www\.|\.[a-z]{2,}/", v, re.I):
        return "This field asks for a link and the answer isn't a URL."
    # A Yes/No question answered with a sentence — the shape of an answer written for a different
    # question. Free-text controls only, and only when the question doesn't invite prose.
    if control in _FREE_CONTROLS and _YES_NO_Q.match(n) and len(v) > 25 \
            and not _YES_NO_A.match(v) and not any(t in n for t in _WANTS_PROSE):
        return "This reads as a Yes/No question but the answer is a sentence."
    # The inverse: a question asking WHICH or WHY, answered with a bare "Yes" — the shape a
    # yes/no profile field produces when it is asked a question it doesn't answer. A live
    # Palantir fill answered "Which of these roles resonates the most … and why?" with "Yes".
    if control in _FREE_CONTROLS and _YES_NO_A.fullmatch(v) and not _YES_NO_Q.match(n) \
            and any(t in n for t in ("which", "why", "how many", "how much", "describe",
                                     "explain", "tell us", "tell me")):
        return f"This question asks for a choice or an explanation — the answer is just “{v}”."
    return ""


def _review_data(app_id: int) -> dict | None:
    """Everything the "Review before you apply" panel shows for one prepared application, joined
    by app id: the posting metadata from the tracker, and the exact fill outcome (field values
    the bot will submit, plus the JD it tailored against) from the per-application archive
    (decision 043) that the headless dry-run already wrote. Returns None if the id is unknown.
    The résumé PDF and the filled-form screenshot are large, so they are NOT inlined here — the
    panel fetches them lazily from /track/resume and /track/screenshot by the same id."""
    a = tracker.get_application(app_id)
    if not a:
        return None
    from . import archive
    adir = archive.dir_for(a["company"], a["role"], a["source_url"])
    filled: list[dict] = []
    skipped: list[str] = []
    captured: dict = {}
    required: dict = {}
    context: dict = {}
    when = ""
    rj = adir / "report.json"
    if rj.is_file():
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
            filled = data.get("filled", []) or []
            captured = data.get("captured", {}) or {}
            required = data.get("required", {}) or {}
            context = data.get("context", {}) or {}
            # A check-all-that-apply group is reported one row per CHECKED option; the panel edits
            # the question once, so fold them into a single "A; B" answer (the format the fill splits).
            filled = _merge_checkbox_groups(filled)
            # A file-upload answer's value is the local PDF path we upload — show just the file
            # name in the preview (the full path is noise; the résumé button opens the file).
            for f in filled:
                v = str(f.get("value", ""))
                if v.startswith("/") and Path(v).suffix.lower() in (".pdf", ".doc", ".docx"):
                    f["value"] = Path(v).name
            # report.skipped mixes genuinely-unanswered fields with a bracketed "[answer bank]"
            # learning-summary line (apply.py) — drop the diagnostic so the panel's "needs
            # attention" list is only real unanswered fields.
            skipped = [s for s in (data.get("skipped", []) or []) if not str(s).startswith("[")]
            when = data.get("when", "") or ""
        except (ValueError, OSError):
            pass
    jd_body = ""
    pm = adir / "posting.md"
    if pm.is_file():
        try:
            # posting.md is "<header>\n\n---\n\n<JD body>" (archive._posting_md); show the body.
            jd_body = pm.read_text(encoding="utf-8").split("\n\n---\n\n", 1)[-1].strip()
            if jd_body == "(no posting text captured)":  # archive placeholder — treat as no JD
                jd_body = ""
        except OSError:
            pass
    # Answers the user edited here previously (decision 153) override what the fill produced —
    # show what WILL be submitted, not the superseded original, and flag each edited row.
    from . import answer_overrides
    edits = answer_overrides.load(a["company"], a["role"], a["source_url"])
    by_key = {answer_overrides.key(k): v for k, v in edits.items()}
    for f in filled:
        v = by_key.get(answer_overrides.key(f.get("label", "")))
        if v is not None and f.get("control") != "file":
            f["value"], f["edited"] = v, True
    # Unanswered fields as editable rows (label + why it was skipped), so the user can answer a
    # blocking field right here instead of only reading that it's blocked (UI Principle #2).
    # skipped strings are "<label> — <reason>"; dedupe by label, keeping the first reason.
    unanswered: list[dict] = []
    seen_labels: set[str] = set()
    filled_keys = {answer_overrides.key(f.get("label", "")) for f in filled}
    for s in skipped:
        label, _, detail = str(s).partition(" — ")
        k = answer_overrides.key(label)
        if not k or k in seen_labels or k in filled_keys:
            continue
        seen_labels.add(k)
        unanswered.append({"label": label.strip(), "detail": detail.strip(),
                           "value": by_key.get(k, ""), "edited": k in by_key})
    # The control each answer came from, so the panel recreates it (checkbox group → checkboxes).
    for row in filled + unanswered:
        meta = captured.get(row.get("label", "")) or {}
        if meta.get("options"):
            row["kind"], row["options"] = meta.get("kind", ""), meta["options"]
    # Required vs optional per question (decision 164), so the panel says what must be answered
    # before a submit will go through. Matched on the same normalised key as the edits, since a
    # form renders the same label with different whitespace/glyphs in different places. A question
    # the sweep never saw is left unmarked (absent), NOT reported as optional.
    req = {answer_overrides.key(k): bool(v) for k, v in required.items() if str(k).strip()}
    for row in filled + unanswered:
        flag = req.get(answer_overrides.key(row.get("label", "")))
        if flag is not None:
            row["required"] = flag
    for row in unanswered:
        # `_flag_missing_required` writes the reason itself — trust it over the sweep.
        if "REQUIRED" in (row.get("detail") or ""):
            row["required"] = True
    # What a generic label was read FROM (decision 167): the heading above the field, the
    # sentence before it, the field it follows. A "Date" box is only reviewable if the user can
    # see WHICH date the form was asking for.
    ctx = {answer_overrides.key(k): v for k, v in context.items() if str(k).strip()}
    for row in filled + unanswered:
        around = ctx.get(answer_overrides.key(row.get("label", "")))
        if around:
            row["context"] = around
    # Answers whose SHAPE doesn't fit their question (decision 166) — the wrong-context fills a
    # filled-looking form hides. Computed after the edits above, so it judges what will actually
    # be submitted, not a value the user already corrected.
    for row in filled + unanswered:
        flag = _answer_flag(row.get("label", ""), row.get("value", ""),
                            row.get("control", "") or row.get("kind", ""))
        if flag:
            row["flag"] = flag
    rp = a.get("resume_path", "")
    has_resume = bool(rp) and Path(rp).suffix.lower() == ".pdf" and Path(rp).is_file()
    return {
        "id": app_id,
        "posting": {
            "company": a["company"], "role": a["role"], "location": a.get("location", ""),
            "remote": a.get("remote", ""), "pay": a.get("pay", ""), "portal": a.get("portal", ""),
            "url": a.get("source_url", ""), "fit": a.get("fit_score"), "status": a.get("status", ""),
            "resume_source": a.get("resume_source", ""),  # freshly tailored vs reused (decision 144)
        },
        "jd": jd_body,
        "filled": filled,
        "skipped": skipped,
        "unanswered": unanswered,
        "when": when,
        # Did this fill record required/optional at all? Reports written before decision 164 (and
        # Workday runs, which fill through their own driver) carry none — the panel says so and
        # offers a rescan instead of silently showing every question as unmarked.
        "required_known": bool(req),
        "has_resume": has_resume,
        "has_screenshot": (adir / "filled.png").is_file(),
    }


def save_answers(app_id: int, answers: dict) -> dict:
    """Persist the answers the user edited in one application's review panel (decision 153).

    Stored against the posting, not the form: the next fill of this application — the dry-run
    re-fill, "Watch it fill", and the real submit alike — resolves these labels to these values.
    A blank value clears that edit, so the bot answers the field from the profile again.

    Reusable answers are ALSO taught to the shared answer bank (decision 155), so a question the
    user fixed here is answered the same way on every future posting instead of coming back
    blank or wrong. See `_learn_reviewed_answers` for which edits qualify."""
    a = tracker.get_application(app_id)
    if not a:
        return {"ok": False, "error": "That application is no longer in the tracker."}
    if not isinstance(answers, dict) or not answers:
        return {"ok": False, "error": "No answers were sent to save."}
    from . import answer_overrides
    try:
        saved = answer_overrides.save(a["company"], a["role"], a["source_url"], answers)
    except OSError as e:
        return {"ok": False, "error": f"Could not write the edited answers: {e}"}
    out = {"ok": True, "saved": len(saved)}
    try:
        out.update(_learn_reviewed_answers(answers, _captured_controls(a)))
    except (OSError, ValueError) as e:
        # The posting override IS saved (this application will submit the edits); only the
        # cross-application learning failed — say exactly that instead of a false success.
        out.update({"learned": 0, "posting_only": 0, "profile_owned": [],
                    "learn_error": f"Saved for this posting, but could not add the answers to "
                                   f"your answer bank: {e}"})
    return out


def _captured_controls(a: dict) -> dict:
    """The {kind, options} the last fill recorded per question for this posting, from its archived
    report — so an answer learned here keeps the control it was asked in. {} if unavailable."""
    from . import archive
    rj = archive.dir_for(a.get("company", ""), a.get("role", ""), a.get("source_url", "")) / "report.json"
    try:
        return json.loads(rj.read_text(encoding="utf-8")).get("captured", {}) or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _learn_reviewed_answers(edits: dict, controls: dict | None = None) -> dict:
    """Teach the shared answer bank the answers the user edited in a review panel, so the NEXT
    posting asking the same question is answered with what they typed instead of being left
    blank or repeating an answer they rejected (decision 155). Each edit lands in one bucket:

      learned      — a reusable question: banked, overwriting the blank entry autofill captured
                     or the answer the user replaced. Future applications reuse it.
      profile_owned— a structured profile rule already answers this label (email, work
                     authorization, start date…), and those rules outrank the bank — banking it
                     would change nothing, so the edit stays per-posting and the user is told
                     where the answer actually lives.
      posting_only — company-specific ("Why us?") or demographic/EEO: never shared across
                     employers, so it stays on this posting alone.

    Returns those buckets (counts, plus the profile-owned labels to name in the UI). `controls`
    carries each question's form control ({kind, options}) into the bank, so a check-all-that-apply
    answer learned here is editable as checkboxes in the profile, not as a text box.
    """
    from . import answer_bank

    learned: dict[str, str] = {}
    profile_owned: list[str] = []
    posting_only = 0
    resolver = None
    try:
        from .apply import AnswerResolver
        resolver = AnswerResolver(resume=load_resume("profile/resume.yaml"),
                                  profile=apply_profile.load_profile())
    except Exception:
        resolver = None  # no résumé/profile yet — bank every reusable edit rather than none
    for label, value in (edits or {}).items():
        label, value = str(label).strip(), str(value if value is not None else "").strip()
        if not (label and value):
            continue  # a cleared edit only drops this posting's override
        if not answer_bank.is_reusable_answer(label):
            posting_only += 1
        elif resolver is not None and resolver.banked_qa(label) is None \
                and resolver.resolve(label) is not None:
            profile_owned.append(label)
        else:
            learned[label] = value
    written = apply_profile.upsert_answers(learned, meta=controls) if learned else 0
    return {"learned": written, "profile_owned": profile_owned[:3], "posting_only": posting_only}


def test_aggregators(data: dict | None) -> dict:
    """Live-probe every broad aggregator the user has configured (Adzuna, early-career feeds,
    Google Jobs, Himalayas, RemoteOK) and report per source whether it's reachable and how many
    postings a quick sample returned — or the exact error. Uses the settings passed from the
    editor (so the user can test before saving), falling back to the saved config. Company ATS
    boards are excluded: this tests only the aggregators. Expensive knobs (page/query/resolve
    counts) are clamped low so the probe stays fast — it validates keys/connectivity/results,
    not full volume."""
    from .discovery import (AdzunaSource, CuratedListSource, DiscoveryError,
                            GoogleJobsSource, HimalayasSource, RemoteOKSource)

    try:
        if data:
            # Merge the editor's values OVER the saved config, so aggregators the form doesn't
            # expose (e.g. Google Jobs) keep their saved settings and are still probed.
            base = filters.load_filters().model_dump()
            base.update(data)
            f = filters.DiscoveryFilters.model_validate(base)
        else:
            f = filters.load_filters()
    except Exception as e:
        return {"error": f"invalid discovery settings: {type(e).__name__}: {e}"}
    # Aggregators only, quick sample: never poll per-company boards here, and cap the expensive
    # breadth knobs so a test returns in seconds instead of running a full discovery.
    f = f.model_copy(deep=True)
    f.boards = []
    f.career_sites = []
    f.adzuna.max_queries = min(f.adzuna.max_queries, 1)
    f.adzuna.max_pages = min(f.adzuna.max_pages, 1)
    f.google.max_queries = min(f.google.max_queries, 1)
    f.google.results_wanted = min(f.google.results_wanted, 10)
    f.remote_boards.max_results = min(f.remote_boards.max_results, 25)
    f.early_career.max_resolve = min(f.early_career.max_resolve, 5)

    try:
        resume = load_resume("profile/resume.yaml")
    except Exception:
        resume = None
    try:
        profile = apply_profile.load_profile()
    except Exception:
        profile = None

    AGG = (AdzunaSource, GoogleJobsSource, HimalayasSource, RemoteOKSource, CuratedListSource)
    sources = [s for s in filters.build_sources(f, resume, profile) if isinstance(s, AGG)]
    results = []
    for src in sources:
        try:
            posts = src.fetch()
            results.append({"name": src.name, "ok": True, "count": len(posts),
                            "sample": (posts[0].title if posts else "")})
        except DiscoveryError as e:
            results.append({"name": src.name, "ok": False, "error": str(e)})
        except Exception as e:  # defensive: a malformed field shouldn't 500 the test
            results.append({"name": src.name, "ok": False,
                            "error": f"unexpected {type(e).__name__}: {e}"})
    return {"ok": True, "results": results, "resume": resume is not None}


# Config files that live in profile/ beside the résumés. `list_resumes` decides what is a résumé
# by trying to LOAD each file, which is normally enough — but a listed file also becomes a WRITE
# target for /resume/update, so anything wrongly listed gets a résumé written over it. That is not
# hypothetical: a test that stubbed `load_resume` to succeed for any path made every config file
# validate, and the save posted a résumé over the user's real `application_profile.yaml`,
# destroying it (decision 159). Names are cheap, and no résumé is ever called these.
_NOT_RESUMES = frozenset({
    "application_profile.yaml", "discovery.yaml", "mailbox.yaml", "safety.yaml",
    "notifications.yaml",
})


def list_resumes() -> list[dict[str, str]]:
    # The apply profile and discovery filters live alongside résumés in profile/ but are not
    # résumés — exclude them so they never show up as a selectable resume (they fail to load
    # as a Resume, which broke the Profile page). Keep this in sync with the config modules.
    # Include only files that actually validate as a Resume. Config files (application_profile,
    # discovery filters, mailbox link, safety) live alongside résumés but are not résumés;
    # loading one as a Resume crashes the Profile page with a pydantic ValidationError. An earlier
    # name-based blacklist drifted out of sync as new config files were added — validating instead
    # skips any non-résumé file automatically.
    out = []
    for folder in ("profile", "examples"):
        for p in sorted((REPO_ROOT / folder).glob("*.yaml")):
            if p.name in _NOT_RESUMES:
                continue
            try:
                load_resume(p)
            except Exception:
                continue
            out.append({"path": str(p.relative_to(REPO_ROOT)), "label": f"{folder}/{p.name}"})
    return out


def list_fixtures() -> list[dict[str, str]]:
    """Job postings selectable in Discover's "a posting I paste" dry run: the shipped example
    fixtures plus any the user
    saved from the Track tab. A shipped fixture's `path` is relative to REPO_ROOT; a saved one's
    is relative to DATA_ROOT (they resolve in different roots — see `_fixture_path`). User-saved
    ones are labeled "· saved" and listed first so they're easy to find."""
    saved = []
    if USER_FIXTURES_DIR.is_dir():
        for p in sorted(USER_FIXTURES_DIR.glob("*.md")):
            saved.append({"path": f"profile/job_fixtures/{p.name}", "label": _saved_label(p)})
    shipped = []
    for p in sorted((REPO_ROOT / "fixtures" / "job_descriptions").glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        shipped.append({"path": str(p.relative_to(REPO_ROOT)), "label": p.name})
    return saved + shipped


def _saved_label(p: Path) -> str:
    """A friendly picker label for a saved fixture — "Company — Title · saved" from its front
    matter, falling back to the filename if it can't be read (the hash-suffixed stem is ugly)."""
    try:
        jd = load_job_description(p)
        bits = [b for b in (jd.meta.get("company"), jd.meta.get("title")) if b]
        if bits:
            return " — ".join(str(b) for b in bits) + " · saved"
    except Exception:
        pass
    return f"{p.stem} · saved"


def _fixture_path(token: str) -> Path:
    """Resolve an allow-listed fixture token to its file. Saved fixtures live under DATA_ROOT,
    shipped ones under REPO_ROOT; both must be a member of `list_fixtures()` (so the token can't
    read an arbitrary file off disk)."""
    if token not in {f["path"] for f in list_fixtures()}:
        raise ValueError(f"Not an allowed fixture: {token!r}")
    root = DATA_ROOT if token.startswith("profile/job_fixtures/") else REPO_ROOT
    return root / token


def save_job_fixture(*, title: str, company: str, body: str, source_url: str = "") -> dict[str, str]:
    """Write a job posting to the user's saved-fixtures folder as a load_job_description-compatible
    Markdown file (YAML front matter + body). The filename keys on company/title + a short hash of
    the source URL (or body), so re-saving the same posting overwrites in place rather than
    duplicating. Returns {"path": <token>, "label": ...} for the new fixture."""
    import hashlib
    from datetime import date

    from .resume_store import _slug

    body = (body or "").strip()
    if not body:
        raise ValueError("No job description text to save.")
    title = (title or "").strip() or "Saved posting"
    company = (company or "").strip()
    USER_FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1((source_url or body).encode("utf-8")).hexdigest()[:8]
    stem = "-".join(s for s in (_slug(company), _slug(title), digest) if s)
    path = USER_FIXTURES_DIR / f"{stem}.md"
    front = f"company: {company}\ntitle: {title}\n"
    if source_url:
        front += f"source_url: {source_url}\n"
    front += f"saved_from: tracker\ndate_captured: {date.today().isoformat()}\n"
    path.write_text(f"---\n{front}---\n\n{body}\n", encoding="utf-8")
    return {"path": f"profile/job_fixtures/{path.name}", "label": _saved_label(path)}


def add_fixture_from_application(payload: dict) -> dict:
    """Save a Track-tab application's posting to the user's fixtures list. Pulls the JD, company,
    and role from the application's archived posting.md (decision 043) by id; falls back to any
    title/company/body passed directly. Returns the new fixture plus the refreshed fixture list so
    the Review-tab picker can add it without a page reload."""
    body = (payload.get("body") or "").strip()
    title = (payload.get("title") or "").strip()
    company = (payload.get("company") or "").strip()
    source_url = (payload.get("source_url") or "").strip()
    app_id = payload.get("id")
    if app_id is not None:
        rv = _review_data(int(app_id))
        if not rv:
            raise ValueError(f"No such application: {app_id!r}")
        body = body or (rv.get("jd") or "")
        p = rv.get("posting") or {}
        title = title or p.get("role", "")
        company = company or p.get("company", "")
        source_url = source_url or p.get("url", "")
    if not body:
        raise ValueError(
            "This application has no saved job description to add. Re-run it as a dry-run first "
            "so its posting is captured, then try again.")
    fixture = save_job_fixture(title=title, company=company, body=body, source_url=source_url)
    return {"ok": True, "fixture": fixture, "fixtures": list_fixtures()}


def _allowlisted(rel_path: str, allowed: list[dict[str, str]]) -> Path:
    """Resolve `rel_path` only if it is one of the discovered, allow-listed files."""
    if rel_path not in {a["path"] for a in allowed}:
        raise ValueError(f"Not an allowed path: {rel_path!r}")
    return REPO_ROOT / rel_path


def _same_origin(handler) -> bool:
    """True if a state-changing request looks same-origin (the localhost UI). The `do_POST`
    origin guard (decision 062) uses it to reject a drive-by cross-site POST — a page on another
    site the user has open must not drive this server. A missing Origin/Referer (many same-origin
    fetches omit it; non-browser clients send none) passes. A present Origin passes if it is a
    loopback host, or if its host matches the `Host` the client addressed — so the guard is
    correct whatever the server is bound to (`--host` LAN IP or name), not just 127.0.0.1. A
    browser sets Origin itself, so a remote attacker page cannot forge it to a loopback value."""
    origin = handler.headers.get("Origin") or handler.headers.get("Referer") or ""
    if not origin:
        return True
    try:
        origin_host = (urlparse(origin).hostname or "").lower()
    except Exception:
        return False
    if origin_host in ("127.0.0.1", "localhost", "::1"):
        return True
    host_header = (handler.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return bool(origin_host) and origin_host == host_header


def do_tailor(payload: dict) -> dict:
    resume_path = _allowlisted(payload["resume"], list_resumes())
    resume = load_resume(resume_path)

    job = payload["job"]
    if job.get("mode") == "custom":
        body = (job.get("body") or "").strip()
        if not body:
            raise ValueError("Paste a job description first.")
        jd = JobDescription(
            body=body,
            meta={
                "title": job.get("title") or "Custom posting",
                "company": job.get("company") or "",
            },
        )
    else:
        jd = load_job_description(_fixture_path(job["fixture"]))

    pages = float(payload.get("pages") or 1.0)
    line_chars = int(payload.get("line_chars") or 100)
    result = tailor_resume(
        resume, jd, backend=payload.get("backend", "auto"),
        budget=LengthBudget(pages=pages, line_chars=line_chars),
        quality=payload.get("quality") or DEFAULT_QUALITY,
    )
    # Show the applicant's links (LinkedIn/GitHub/portfolio) from the apply profile when the
    # résumé header itself has none, so the preview/PDF match what gets submitted.
    rl = apply_profile.resume_with_profile_links(resume, apply_profile.load_profile())
    return {
        "backend": result.backend,
        "pages": result.pages,
        "title": jd.title,
        "company": jd.company,
        "html": render_html(rl, result.tailored),
        "markdown": render_markdown(rl, result.tailored),
        "tailored": result.tailored.model_dump(),
        "notes": result.tailored.relevance_notes,
        "warnings": result.warnings,
    }


def _has_any_application() -> bool:
    """True once the user has run at least one dry-run (any tracked application row)."""
    try:
        from . import tracker

        return sum(tracker.status_counts().values()) > 0
    except Exception:
        return False


# First-run walkthrough steps (decision: in-app skippable checklist). Each step reuses a
# `doctor` readiness check for its ok/detail/fix, and adds a UI `action` telling the front-end
# exactly where to send the user to complete it (UI Principle #2: one click to the fix).
# `required` is defined here, not taken from doctor: the pipeline can tailor with the free
# `rules` engine, so Claude sign-in is optional; Chromium/profile/résumé/filters are needed to
# discover and apply end-to-end.
def _setup_status() -> dict:
    from . import __version__, doctor

    # Call doctor's per-check helpers directly (same package) so the walkthrough and the
    # `doctor` CLI stay one source of truth for what "ready" means.
    checks = {
        "profile": (doctor._check_profile(doctor._PROFILE), "Add your details", True,
                    {"view": "profile"}),
        "resume": (doctor._check_resume(doctor._RESUME), "Add your résumé", True,
                   {"view": "profile"}),
        "discovery": (doctor._check_discovery(doctor._FILTERS), "Choose what jobs to find", True,
                      {"view": "discover", "scroll": "disc-settings"}),
        "playwright": (doctor._check_playwright(), "Install the apply browser", True,
                       {"cmd": "playwright install chromium"}),
        "claude": (doctor._check_claude(), "Connect Claude for best tailoring", False,
                   {"scroll": "account", "flash": "account"}),
    }
    order = ["profile", "resume", "discovery", "playwright", "claude"]
    steps = []
    for key in order:
        chk, title, required, action = checks[key]
        steps.append({"key": key, "title": title, "ok": chk.ok, "required": required,
                      "detail": chk.detail, "fix": chk.fix, "action": action})
    # A synthetic final step: has the user actually watched one dry-run run end-to-end?
    steps.append({
        "key": "dryrun", "title": "Run your first dry-run", "ok": _has_any_application(),
        "required": False,
        "detail": "the pipeline finds, tailors, and fills one application without submitting",
        "fix": "", "action": {"view": "discover", "scroll": "test-run", "flash": "test-run"},
    })
    required_ok = all(s["ok"] for s in steps if s["required"])
    return {"version": __version__, "ready": required_ok, "steps": steps}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _link_base(self) -> str:
        """The address this server is reachable at, for notification click-through back into
        the app. Uses the bound port so a --port override is honored."""
        host, port = self.server.server_address[0], self.server.server_address[1]
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/auth/status":
            self._json(200, auth.status())
            return
        if path == "/setup/status":
            self._json(200, _setup_status())
            return
        if path == "/dev/reload-token":  # dev auto-reload heartbeat (changes on every restart)
            self._send(200, _BOOT_TOKEN.encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path == "/resume":
            try:
                rel = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                resume_path = _allowlisted(rel, list_resumes())
                resume = load_resume(resume_path)
                self._json(200, {"resume": resume.model_dump()})
            except Exception as e:
                self._json(400, {"error": f"{type(e).__name__}: {e}"})
            return
        if path == "/resume/uploads":
            # Résumé documents the user uploaded and we kept (decision 152). These are sent to
            # postings as-is when they already cover the demanded skills, so the user must be able
            # to see exactly which files are in play and remove any of them.
            from . import resume_docs
            self._json(200, {"docs": resume_docs.listing()})
            return
        if path == "/profile":
            # MyGreenhouse Quick Apply carries no secret any more (decision 182) — it needs the
            # linked inbox to read Greenhouse's emailed security code. Ship the exact blocker
            # alongside the profile so the Profile tab can state it and point at the fix.
            prof = apply_profile.load_profile()
            d = prof.model_dump()
            d["greenhouse_problem"] = apply_profile.greenhouse_quick_apply_problem(prof)
            self._json(200, {"profile": d})
            return
        if path == "/profile/export":
            # The portable setup as one .zip (decision 188) — apply profile, filters, every
            # résumé, and the kept résumé PDFs. Errors come back as JSON so the button can show
            # the exact blocker inline (UI Principle #3) rather than downloading a broken file.
            from . import profile_export
            try:
                body = profile_export.build_zip()
            except Exception as e:
                self._json(400, {"error": str(e)})
                return
            self._send(200, body, "application/zip",
                       {"Content-Disposition": f'attachment; filename="{profile_export.filename()}"'})
            return
        if path == "/mailbox":
            # Bot-email link status for the Profile panel (decisions 057, 065). Never returns any
            # secret — the password / OAuth token live in the OS keychain. `client_id` is non-secret
            # and returned so a Gmail reconnect can pre-fill it (one click).
            from . import mailbox
            self._json(200, {**mailbox.link_status(), "client_id": mailbox.gmail_client_id()})
            return
        if path == "/discovery":
            self._json(200, {
                "filters": filters.load_filters().model_dump(),
                "levels": filters.EXPERIENCE_LEVELS,
            })
            return
        if path == "/notifications":
            # Push-notification settings (decision 135). No secret to hide — an ntfy topic is a
            # user-chosen capability string, returned so the panel can show it.
            from . import notifications as notif
            self._json(200, {"config": notif.load_config().to_dict(),
                             "desktop_click": notif.desktop_click_status()})
            return
        if path == "/fit-insights":
            # What the discovery feedback loop has learned + recommends (decision 046).
            from . import fit_learning
            f = filters.load_filters()
            recs = fit_learning.load()
            a = fit_learning.analyze(recs, min_fit=f.min_fit, current_levels=f.experience_levels)
            self._json(200, {
                "n_judged": a.n_judged,
                "lines": a.lines() if a.n_judged else [],
                "recommendations": [
                    {"kind": r.kind, "message": r.message, "field": r.field, "value": r.value}
                    for r in a.recommendations
                ],
                # How well the deterministic pre-score tracks real fit for this résumé (decision
                # 052/055) — bands + a one-line read.
                "prescore": fit_learning.prescore_insight(recs),
                # Per-run trend so the UI can chart results improving over time (decision 046).
                # Return the full lifetime; the UI defaults to showing all and can window it down.
                "runs": fit_learning.runs(),
            })
            return
        if path == "/sources":
            # A live "where & how" view of every source feeding discovery (decision 032):
            # target boards grouped by ATS, the optional Adzuna aggregator + how it's
            # configured, early-career feeds, and the aggregator→ATS bridge.
            from .discovery import ATS_SOURCES
            from . import mailbox

            f = filters.load_filters()
            adz = f.adzuna
            adz_cfg = bool(adz.app_id and adz.app_key)
            adz_env = bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))
            boards_by_ats: dict[str, list[str]] = {}
            for b in f.boards:
                boards_by_ats.setdefault(b.ats, []).append(b.token)
            self._json(200, {
                "boards_by_ats": boards_by_ats,
                "fillable_ats": list(ATS_SOURCES),
                "aggregator": {
                    "active": adz_cfg or adz_env,
                    "via": ("your key" if adz_cfg else ("environment variables" if adz_env else None)),
                    "country": adz.country,
                },
                "early_career": {
                    "enabled": f.early_career.enabled,
                    "kinds": f.early_career.kinds,
                },
                "remote_boards": {
                    "himalayas": f.remote_boards.himalayas,
                    "remoteok": f.remote_boards.remoteok,
                },
                "email_alerts": {
                    "enabled": f.email_alerts.enabled,
                    "providers": f.email_alerts.providers,
                    "linked": mailbox.link_status().get("linked", False),
                },
                "google": {"enabled": f.google.enabled},
                "json_aggregators": list(f.json_aggregators),
                "contrib_sources": list(f.contrib_sources),
                "bridge": {"enabled": True, "upgrade_ats": list(ATS_SOURCES)},
            })
            return
        if path == "/candidates":
            # Discovery-source candidates staged by the source-scout routine (decision 135):
            # companies on an ATS we already support, verified live. Surface only VALIDATED ones
            # not already configured, so the panel is a clean one-click add-list (no dead tokens,
            # no re-proposing a board you already run).
            from . import source_scout

            f = filters.load_filters()
            known = source_scout.known_boards(f.boards)
            out = [
                {"ats": c.ats, "token": c.token, "provenance": c.provenance,
                 "n_postings": c.n_postings, "sample_title": c.sample_title,
                 "sample_company": c.sample_company}
                for c in source_scout.load_candidates()
                if c.validated and c.key not in known
            ]
            # Declarative JSON-API aggregator specs (decision 135): registry entries not yet enabled
            # in json_aggregators. All registry specs are pre-validated (stage_spec gates on that).
            enabled = set(f.json_aggregators)
            specs = [
                {"name": s.get("name"), "endpoint": s.get("endpoint", ""),
                 "n_postings": s.get("n_postings", 0), "sample_title": s.get("sample_title", "")}
                for s in source_scout.load_registry_specs()
                if s.get("name") and s.get("name") not in enabled
            ]
            # Bespoke drop-in adapters (decision 139): merged sources_contrib modules not yet enabled.
            from .sources_contrib import load_contrib_sources

            on = set(f.contrib_sources)
            contrib = [{"name": n, "description": getattr(m, "DESCRIPTION", "")}
                       for n, m in load_contrib_sources().items() if n not in on]
            self._json(200, {"candidates": out, "specs": specs, "contrib": contrib})
            return
        if path == "/track":
            q = parse_qs(urlparse(self.path).query)
            apps = tracker.list_applications(
                status=(q.get("status", [""])[0] or None),
                search=(q.get("search", [""])[0] or None),
            )
            # Attach each posting's run count so the Track tab can show "N runs" without loading
            # every run up front (the runs themselves are fetched lazily on expand, /track/runs).
            # `has_jd` gates the "Re-tailor" re-run option — true only when a saved JD lets it run
            # offline (decision 086); postings that predate the JD sidecar show reuse-only.
            from . import resume_store, parking
            rc = tracker.run_counts()
            # Claude token spend per posting, keyed by source URL (decision 095) — attached so the
            # Track table can show a per-application Tokens column that expands to the in/out split
            # and the per-activity breakdown (tailoring / form-entry / …).
            usage_by = tracker.usage_by_application()
            for a in apps:
                a["run_count"] = rc.get(a["id"], 0)
                a["has_jd"] = resume_store.has_jd(a.get("resume_path", ""))
                a["tokens"] = usage_by.get((a.get("source_url") or "").strip())
                # A short "what blocked" line for the feed card, reusing the parking
                # labels/details that drive the Resolve cards (single source of truth).
                if a.get("status") == "blocked" and a.get("blocked_kind"):
                    d = parking.describe(a["blocked_kind"], a.get("blocked_detail", ""))
                    a["blocker"] = d["label"]
                    a["blocker_detail"] = d["detail"]
            self._json(200, {
                "applications": apps,
                "counts": tracker.status_counts(),
                "funnel": tracker.funnel_report(),
                "statuses": tracker.STATUSES,
                "fields": tracker.EDITABLE,
                # Batched-judge / discovery Claude spend not tied to one application (user's
                # choice: shown as one separate aggregate, never divided across rows).
                "usage_discovery": tracker.usage_discovery_summary(),
            })
            return
        if path == "/track/runs":
            # The run history for one posting (decision 084), newest first — fetched lazily when
            # the user expands a Track row. Id comes from our own DB (localhost-only server).
            q = parse_qs(urlparse(self.path).query)
            try:
                aid = int(q.get("id", ["0"])[0])
            except (ValueError, TypeError):
                aid = 0
            self._json(200, {"runs": tracker.runs_for_application(aid) if aid else []})
            return
        if path == "/parked":
            # Applications parked on a user-resolvable block (parking.py) + display metadata
            # for the Resolve cards (headline, action verb, deep-link target, resumable).
            from . import parking
            out = []
            for a in tracker.parked_applications():
                d = parking.describe(a.get("blocked_kind", ""), a.get("blocked_detail", ""))
                out.append({
                    "id": a["id"], "company": a["company"], "role": a["role"],
                    "portal": a["portal"], "source_url": a["source_url"],
                    "status": a["status"], **d,
                })
            self._json(200, {"parked": out})
            return
        if path == "/test-run/resume":
            rp = ((_TEST_STATE.get("tailored") or {}).get("pdf") or "")
            f = Path(rp) if rp else None
            if not f or f.suffix.lower() != ".pdf" or not f.is_file():
                self._json(404, {"error": "No tailored résumé yet. Run a dry-run with "
                                          "“Tailor the résumé only” first."})
                return
            self._send(200, f.read_bytes(), "application/pdf",
                       {"Content-Disposition": 'inline; filename="' + f.name + '"',
                        "Cache-Control": "no-store"})
            return
        if path == "/track/resume":
            # Stream the tailored PDF a Track row used, so the Track tab can link to it
            # (decision 029). Serves only an existing .pdf the row points at; the path
            # comes from our own DB (localhost-only server), never from the request.
            q = parse_qs(urlparse(self.path).query)
            try:
                app = tracker.get_application(int(q.get("id", ["0"])[0]))
            except (ValueError, TypeError):
                app = None
            rp = (app or {}).get("resume_path", "")
            f = Path(rp) if rp else None
            if not f or f.suffix.lower() != ".pdf" or not f.is_file():
                self._json(404, {"error":
                    "No stored résumé for this application. It records one only after a "
                    "dry-run/apply that tailored a PDF."})
                return
            # no-store: the URL is keyed on the row id and stable, but the file it points at is
            # overwritten in place when a posting is re-tailored (same path, new bytes). Without
            # this the browser's PDF viewer serves the cached old résumé at the unchanged URL, so
            # a re-tailor looks like it did nothing (the exact symptom this endpoint must avoid).
            self._send(200, f.read_bytes(), "application/pdf",
                       {"Content-Disposition": 'inline; filename="' + f.name + '"',
                        "Cache-Control": "no-store"})
            return
        if path == "/track/screenshot":
            # The filled-form screenshot the dry-run captured for this application (decision 043),
            # stored per-application at profile/applications/<key>/filled.png. Served so the review
            # panel can show the form as the site rendered it. Path comes from our own DB.
            q = parse_qs(urlparse(self.path).query)
            try:
                app = tracker.get_application(int(q.get("id", ["0"])[0]))
            except (ValueError, TypeError):
                app = None
            if not app:
                self._json(404, {"error": "No such application."})
                return
            from . import archive
            shot = archive.dir_for(app["company"], app["role"], app["source_url"]) / "filled.png"
            if not shot.is_file():
                self._json(404, {"error":
                    "No filled-form screenshot for this application yet. It's captured on the "
                    "next dry-run/preparation of this posting."})
                return
            # no-store: same rationale as the résumé — the file is overwritten in place on each
            # re-fill of the same posting, so a cached image would show a stale form.
            self._send(200, shot.read_bytes(), "image/png", {"Cache-Control": "no-store"})
            return
        if path == "/track/review":
            # Preview one prepared application before signing off (decision 125): the exact field
            # values the bot will submit + the JD it tailored against, joined by app id from the
            # dry-run's archive. Read-only; the id comes from our own DB (localhost-only server).
            q = parse_qs(urlparse(self.path).query)
            try:
                app_id = int(q.get("id", ["0"])[0])
                rev = _review_data(app_id)
            except (ValueError, TypeError):
                rev = None
            if rev is None:
                self._json(404, {"error": "No such application."})
                return
            # Opening this panel IS the review (decision 149): stamp it so discovery stops
            # re-surfacing this posting.
            _mark_reviewed(app_id)
            self._json(200, rev)
            return
        if path == "/test-run/status":
            with _TEST_LOCK:
                self._json(200, dict(_TEST_STATE))
            return
        if path == "/loop/status":
            # Auto-apply loop state + the "Ready to apply" list (decision 069). The ready list is
            # resolved live from the tracker so a row that has since been submitted (status left
            # 'dry-run') or edited drops out automatically, and is the SAME durable list the
            # Notifications tab shows (decision 183) — an application prepared in an earlier run,
            # or before a restart, stays reviewable and submittable here instead of vanishing.
            # `ready_run` is the subset this run prepared, so goal progress still counts this run.
            with _LOOP_LOCK:
                st = dict(_LOOP_STATE)
                ready_ids = list(st.pop("ready_ids", []))
            ready = _ready_cards(ready_ids)
            st["ready"] = ready
            st["ready_run"] = sum(1 for r in ready if r["id"] in set(ready_ids))
            self._json(200, st)
            return
        if path == "/loop/settings":
            self._json(200, loop_settings())
            return
        if path == "/inbox":
            with _LOOP_LOCK:
                ready_ids = list(_LOOP_STATE.get("ready_ids", []))
            self._json(200, _build_inbox(ready_ids))
            return
        if path != "/":
            self._json(404, {"error": "not found"})
            return
        options = json.dumps(
            {
                "resumes": list_resumes(),
                "fixtures": list_fixtures(),
                "auth": auth.status(),
            }
        )
        html = INDEX_HTML.replace("/*OPTIONS*/", options)
        if _DEV:
            html = html.replace("</body>", _DEV_REFRESH_SCRIPT + "</body>")
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        # CSRF/origin guard (decision 062): every POST here is state-changing (saves, submits,
        # launches a browser), so reject a cross-origin request — a page on another site the user
        # has open must not be able to drive this localhost server. A same-origin fetch (loopback
        # Origin, or none) passes; non-browser clients (curl/CLI/tests) send no Origin and pass,
        # which is fine — CSRF is a browser-only attack.
        if not _same_origin(self):
            self._json(403, {"ok": False, "error":
                "Cross-origin request blocked. Use the ApplicationBot UI on this machine."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            if path == "/tailor":
                self._json(200, do_tailor(json.loads(raw or b"{}")))
            elif path == "/resume/update":
                p = json.loads(raw or b"{}")
                rp = _allowlisted(p["resume"], list_resumes())
                catalogue.replace_resume(rp, p["data"])
                self._json(200, {"ok": True})
            elif path == "/resume/rank-projects":
                # Score the current (posted) projects by technical impressiveness via Claude,
                # persist the scores into resume.yaml, and return the re-scored résumé so the
                # UI can reorder. Saves the posted edits as a side effect (like Save).
                p = json.loads(raw or b"{}")
                rp = _allowlisted(p["resume"], list_resumes())
                result = impact.score_projects(Resume.model_validate(p["data"]))
                catalogue.save_resume(rp, result.resume)
                self._json(200, {"ok": True,
                                 "resume": result.resume.model_dump(exclude_none=True),
                                 "ranked": [list(t) for t in result.ranked]})
            elif path == "/resume/import-linkedin":
                p = json.loads(raw or b"{}")
                rp = _allowlisted(p["resume"], list_resumes())
                data = base64.b64decode(p["data_b64"])
                result = linkedin.import_into(rp, p.get("filename", "upload"), data)
                self._json(200, {"ok": True, **result})
            elif path == "/resume/import-file":
                # Upload a résumé document (PDF/DOCX/TXT): Claude parses it and MERGES new
                # entries into the user's résumé (decision — DECISIONS.md). Import into the
                # selected résumé only if it's an existing profile/ one; otherwise into the
                # canonical profile/resume.yaml (created if absent) — so an upload also works
                # as the very first résumé, and can never write a shipped examples/ file.
                from . import resume_import
                p = json.loads(raw or b"{}")
                sel = (p.get("resume") or "").strip()
                if sel.startswith("profile/") and sel in {a["path"] for a in list_resumes()}:
                    target = REPO_ROOT / sel
                else:
                    target = REPO_ROOT / resume_import.DEFAULT_RESUME
                data = base64.b64decode(p["data_b64"])
                result = resume_import.import_resume(target, p.get("filename", "upload"), data)
                self._json(200, {"ok": True, **result})
            elif path == "/resume/uploads/delete":
                # Stop sending a kept résumé document to postings (decision 152). Deletes only the
                # file itself; whatever it merged into the résumé stays.
                from . import resume_docs
                p = json.loads(raw or b"{}")
                name = (p.get("name") or "").strip()
                if resume_docs.delete(name):
                    self._json(200, {"ok": True, "docs": resume_docs.listing()})
                else:
                    self._json(400, {"ok": False,
                                     "error": f"Couldn't remove {name or 'that file'} — it is no "
                                              "longer in your kept résumé files. Reload the page."})
            elif path == "/profile/update":
                p = json.loads(raw or b"{}")
                apply_profile.replace_profile(p.get("data") or {})
                self._json(200, {"ok": True})
            elif path == "/auth/apikey":
                # Connect the FALLBACK Anthropic API key (decision 111). Validate it with a free
                # models.list() call before storing, so we never save a key that doesn't work;
                # only then write it to the OS keychain (never YAML/git). Returns fresh status.
                p = json.loads(raw or b"{}")
                key = (p.get("key") or "").strip()
                if not key:
                    self._json(400, {"ok": False, "message": "Paste your Anthropic API key (starts with sk-ant-)."})
                else:
                    try:
                        import anthropic
                        anthropic.Anthropic(api_key=key, timeout=20, max_retries=0).models.list()
                    except Exception as e:
                        name = type(e).__name__
                        msg = ("That key was rejected (401) — check it at console.anthropic.com."
                               if "Authentication" in name else f"Couldn't verify the key ({name}): {e}")
                        self._json(200, {"ok": False, "message": msg})
                    else:
                        auth.set_api_key(key)
                        self._json(200, {"ok": True, "message": "API key connected (fallback).",
                                         "status": auth.status()})
            elif path == "/auth/apikey/disconnect":
                auth.clear_api_key()
                self._json(200, {"ok": True, "status": auth.status()})
            elif path == "/mailbox/link":
                # Link the bot inbox (decision 057): test the IMAP connection, and only save on
                # success so we never store credentials that don't work. Password → OS keychain.
                from . import mailbox
                p = json.loads(raw or b"{}")
                email = (p.get("email") or "").strip()
                host = (p.get("host") or "").strip() or mailbox.suggest_host(email)
                password = p.get("password") or ""
                try:
                    port = int(p.get("port") or 993)
                except (TypeError, ValueError):
                    port = 993
                if not (email and host and password):
                    self._json(400, {"ok": False,
                                     "message": "Enter the email, IMAP host, and app password "
                                     "(host is guessed from common providers if left blank)."})
                else:
                    ok, msg = mailbox.test_connection(
                        mailbox.MailboxConfig(host=host, email=email, password=password, port=port))
                    if ok:
                        # The keychain write is verified inside save_link; report a failed save as
                        # a failed link rather than 500ing, so the user sees the actual reason.
                        try:
                            mailbox.save_link(host, email, password, port)
                        except Exception as e:
                            ok, msg = False, f"Signed in to {host} as {email}, but {e}"
                    self._json(200, {"ok": ok, "message": msg, "status": mailbox.link_status()})
            elif path == "/mailbox/gmail/connect":
                # One-click Gmail connect (decision 065): run the OAuth loopback flow, which opens
                # the consent screen in the local browser and blocks this (threaded) request until
                # the user approves. Nothing is stored unless a reusable token comes back AND a test
                # read succeeds. Slow by nature — the UI shows a "waiting for Google" state.
                from . import mailbox
                p = json.loads(raw or b"{}")
                client_id = (p.get("client_id") or "").strip()
                client_secret = (p.get("client_secret") or "").strip()
                if not (client_id and client_secret):
                    self._json(400, {"ok": False, "message":
                                     "Paste your Google Cloud OAuth client ID and secret first — "
                                     "the one-time setup steps are linked above the button."})
                else:
                    ok, msg = mailbox.connect_gmail(client_id, client_secret)
                    self._json(200, {"ok": ok, "message": msg,
                                     "status": {**mailbox.link_status(),
                                                "client_id": mailbox.gmail_client_id()}})
            elif path == "/mailbox/unlink":
                from . import mailbox
                existed = mailbox.clear_link()
                self._json(200, {"ok": True, "existed": existed,
                                 "status": {**mailbox.link_status(),
                                            "client_id": mailbox.gmail_client_id()}})
            elif path == "/track/import-inbox":
                # Fold application emails in the linked inbox into the tracker (decision 151):
                # confirmations become rows, rejections/interview invites move an existing row's
                # status. Returns the full summary so the UI can say exactly what changed and
                # offer the one-click undo.
                from . import inbox_import
                p = json.loads(raw or b"{}")
                out = inbox_import.run_import(
                    limit=int(p.get("limit") or 50), newer_than_days=int(p.get("days") or 30))
                self._json(200, {"ok": not (out["errors"] and not (out["created"] or out["updated"])),
                                 **out})
            elif path == "/track/import-undo":
                from . import inbox_import
                p = json.loads(raw or b"{}")
                res = inbox_import.undo_run(str(p.get("run_id") or ""))
                self._json(200, {"ok": True, **res})
            elif path == "/track/enable-email-alerts":
                # The importer found forwarded job-alert emails but discovery's alert source is
                # off, so those openings are going unused. One click switches it on for exactly
                # the providers whose emails are actually in the inbox (UI Principle #2).
                p = json.loads(raw or b"{}")
                providers = [s for s in (p.get("providers") or []) if isinstance(s, str)]
                f = filters.load_filters()
                f.email_alerts.enabled = True
                f.email_alerts.providers = sorted(set(f.email_alerts.providers) | set(providers))
                filters.save_filters(f)
                self._json(200, {"ok": True, "providers": f.email_alerts.providers})
            elif path == "/discovery/update":
                p = json.loads(raw or b"{}")
                filters.save_filters(filters.DiscoveryFilters.model_validate(p["data"]))
                self._json(200, {"ok": True})
            elif path == "/notifications/update":
                from . import notifications as notif
                p = json.loads(raw or b"{}")
                notif.save_config(_notify_config_from_payload(p.get("data") or {}))
                self._json(200, {"ok": True, "config": notif.load_config().to_dict()})
            elif path == "/notifications/test":
                # Fire a real test notification through the POSTED (possibly unsaved) config, so
                # the user proves a channel works before relying on it (UI Principle #1/#5). Sends
                # to every configured channel regardless of the per-event toggles, and reports
                # per-channel success/failure with the exact error on failure (Principle #3).
                from . import notifications as notif
                p = json.loads(raw or b"{}")
                cfg = _notify_config_from_payload(p.get("data") or {})
                n = notif.build_notifier(cfg, link_base=self._link_base())
                note = notif.Notification(
                    event=notif.APPROVAL_NEEDED, title="ApplicationBot test",
                    body="If you can see this, notifications are working.", link="/#notifications")
                results, errors = [], []
                for ch in n.channels:
                    try:
                        ch.send(note)
                        results.append(ch.name)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{ch.name}: {type(e).__name__}: {e}")
                if not n.channels:
                    self._json(200, {"ok": False, "message": (
                        "No channels enabled. Turn on desktop notifications, or enable ntfy and "
                        "enter a topic, then test again.")})
                elif errors:
                    self._json(200, {"ok": False, "message": (
                        f"Sent via {', '.join(results) or 'nothing'}. Failed — {'; '.join(errors)}")})
                else:
                    self._json(200, {"ok": True, "message": (
                        f"Test sent via {', '.join(results)}. Check your "
                        + ("Mac and phone." if "ntfy" in results else "Mac's Notification Center."))})
            elif path == "/notifications/read":
                # Mark logged notifications seen (decision 145) — clears the nav badge. Called when
                # the Notifications tab opens; ids=null marks all read.
                p = json.loads(raw or b"{}")
                ids = p.get("ids")
                tracker.mark_notifications_read(ids if ids else None)
                self._json(200, {"ok": True, "unread": tracker.unread_notification_count()})
            elif path == "/notifications/dismiss":
                # Remove notifications from the tab (decision 145). ids=null clears them all. The
                # rows stay in the DB (dismissed=1) as an audit trail.
                p = json.loads(raw or b"{}")
                ids = p.get("ids")
                n = tracker.dismiss_notifications(ids if ids else None)
                self._json(200, {"ok": True, "dismissed": n,
                                 "unread": tracker.unread_notification_count()})
            elif path == "/aggregators/test":
                p = json.loads(raw or b"{}")
                self._json(200, test_aggregators(p.get("data")))
            elif path == "/fit-insights/apply":
                # One-click accept of a learned recommendation (decision 046): merge one
                # {field: value} into discovery.yaml. Only fields the analyzer proposes
                # (experience_levels / min_fit) are accepted, and the merged config is
                # re-validated so a bad value can never corrupt the filters.
                p = json.loads(raw or b"{}")
                fld, val = p.get("field"), p.get("value")
                if fld not in ("experience_levels", "min_fit"):
                    self._json(400, {"error": f"not an applyable recommendation field: {fld}"})
                else:
                    data = filters.load_filters().model_dump()
                    data[fld] = val
                    filters.save_filters(filters.DiscoveryFilters.model_validate(data))
                    self._json(200, {"ok": True, "field": fld, "value": val})
            elif path == "/candidates/accept":
                # One-click add of a scouted source (decision 135): append {ats, token} to
                # discovery.yaml boards via the same idempotent path the CLI --accept uses.
                # `added=False` means it was already configured (e.g. added in another tab) —
                # still a success from the user's view.
                from . import source_scout

                p = json.loads(raw or b"{}")
                ats, token = (p.get("ats") or "").strip(), (p.get("token") or "").strip()
                if not ats or not token:
                    self._json(400, {"error": "ats and token are required"})
                else:
                    added = source_scout.merge_into_filters(ats, token)
                    self._json(200, {"ok": True, "added": added})
            elif path == "/candidates/accept-spec":
                # One-click enable of a declarative JSON-API aggregator (decision 135): add its name
                # to json_aggregators. `added=False` = already enabled or not in the registry.
                from . import source_scout

                p = json.loads(raw or b"{}")
                name = (p.get("name") or "").strip()
                if not name:
                    self._json(400, {"error": "name is required"})
                else:
                    added = source_scout.enable_json_aggregator(name)
                    self._json(200, {"ok": True, "added": added})
            elif path == "/candidates/accept-contrib":
                # One-click enable of a merged drop-in adapter (decision 139): add its name to
                # contrib_sources. `added=False` = already enabled or not a loaded adapter.
                from . import source_scout

                p = json.loads(raw or b"{}")
                name = (p.get("name") or "").strip()
                if not name:
                    self._json(400, {"error": "name is required"})
                else:
                    added = source_scout.enable_contrib_source(name)
                    self._json(200, {"ok": True, "added": added})
            elif path == "/track/add":
                p = json.loads(raw or b"{}")
                app_id = tracker.add_application(p.get("data", {}))
                self._json(200, {"ok": True, "id": app_id})
            elif path == "/track/update":
                p = json.loads(raw or b"{}")
                changed = tracker.update_application(int(p["id"]), p.get("changes", {}))
                self._json(200, {"ok": True, "changed": changed})
            elif path == "/track/answers":
                # Save the answers edited in a review panel (decision 153) — used by the next
                # fill/submit of that application.
                p = json.loads(raw or b"{}")
                self._json(200, save_answers(int(p["id"]), p.get("answers", {})))
            elif path == "/track/rescan":
                # Re-read one posting's form so the review panel shows current questions, control
                # types, required marks and answers (decision 164). Headless; never submits.
                # `retailor: true` writes this one application a fresh résumé first (decision 180).
                p = json.loads(raw or b"{}")
                self._json(200, queue_rescan(int(p["id"]), retailor=bool(p.get("retailor"))))
            elif path == "/track/delete":
                p = json.loads(raw or b"{}")
                deleted = tracker.delete_application(int(p["id"]))
                self._json(200, {"ok": True, "deleted": deleted})
            elif path == "/fixtures/add":
                self._json(200, add_fixture_from_application(json.loads(raw or b"{}")))
            elif path == "/pdf":
                p = json.loads(raw or b"{}")
                base = load_resume(_allowlisted(p["resume"], list_resumes()))
                base = apply_profile.resume_with_profile_links(base, apply_profile.load_profile())
                tailored = TailoredResume.model_validate(p["tailored"])
                self._send(200, render_pdf(base, tailored), "application/pdf",
                           {"Content-Disposition": 'attachment; filename="tailored_resume.pdf"'})
            elif path == "/parked/reapply":
                # Cross-origin already rejected by the do_POST origin guard (decision 062); an
                # armed submit is doubly safe there.
                p = json.loads(raw or b"{}")
                self._json(200, start_reapply(int(p["id"]), arm=bool(p.get("arm")),
                                              retailor=bool(p.get("retailor"))))
            elif path == "/test-run":
                p = json.loads(raw or b"{}")
                self._json(200, start_test_run(bool(p.get("fresh")),
                                               mode="tailor" if p.get("mode") == "tailor" else "apply"))
            elif path == "/test-run/close":
                _TEST_HOLD.set()  # release the review hold so the browser closes
                self._json(200, {"ok": True})
            elif path == "/loop/start":
                p = json.loads(raw or b"{}")
                try:
                    goal = int(p["goal"]) if p.get("goal") not in (None, "") else None
                except (TypeError, ValueError):
                    goal = None
                try:
                    watch_interval = int(p["watch_interval"]) if p.get("watch_interval") not in (None, "") else 30
                except (TypeError, ValueError):
                    watch_interval = 30
                self._json(200, start_loop(bool(p.get("rescan")), bool(p.get("retailor")),
                                           goal=goal, maintain=bool(p.get("maintain")),
                                           watch=bool(p.get("watch")), watch_interval=watch_interval,
                                           # Apply mode is the default (decision 176): only an
                                           # explicit dry_run:true holds submission back.
                                           dry_run=bool(p.get("dry_run")),
                                           # Decision 179: watch each submit happen.
                                           show_browser=bool(p.get("show_browser"))))
            elif path == "/loop/stop":
                self._json(200, stop_loop())
            elif path == "/loop/settings":
                # The Loop settings popup (decision 178). Writes discovery.yaml + safety.yaml;
                # a run in flight keeps the values it started with.
                p = json.loads(raw or b"{}")
                try:
                    self._json(200, save_loop_settings(p.get("data") or {}))
                except (TypeError, ValueError) as e:
                    self._json(200, {"ok": False, "error": f"Couldn't save those settings: {e}"})
            elif path == "/loop/apply":
                # Apply to one prepared application. Cross-origin already rejected by the
                # do_POST origin guard (decision 062) — an armed submit is doubly safe there.
                p = json.loads(raw or b"{}")
                self._json(200, queue_submit(int(p["id"])))
            elif path == "/loop/watch":
                # Watch one prepared application autofill — a visible dry-run, never submits.
                p = json.loads(raw or b"{}")
                self._json(200, queue_watch(int(p["id"])))
            elif path == "/loop/watch-apply":
                # Watch one prepared application be submitted FOR REAL (decision 179) — the armed
                # submit in a visible browser. Cross-origin is already rejected by the do_POST
                # origin guard (decision 062); the UI confirms before calling.
                p = json.loads(raw or b"{}")
                self._json(200, queue_watch_submit(int(p["id"])))
            elif path == "/judged/prepare":
                # "Apply" / "Apply anyway" on a posting in the search breakdown (decision 174):
                # tailor (or not) and fill it as a dry-run, then queue it for the armed submit.
                p = json.loads(raw or b"{}")
                self._json(200, queue_prepare(p.get("url", ""), tailor=bool(p.get("tailor", True))))
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # surface a readable message to the UI
            self._json(400, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *args) -> None:  # quieter console
        pass


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ApplicationBot — Resume Review</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAYvElEQVR42pWba6xtV3Xff/O1Xvvch6+vsbGNAdsEG0FJ06QgpxgwRW1DWtRK8KFCUZISaCOUilSthEqqtFIq9UukNAg1aVRVShXSNGkxUaVCiQOtSKhLoakoMZA0McQGv6/v2Xuv15xz9MOc67H3OTbJlfY9e8+99lrzNf7jP/5jTCUi0rYtxhiMMbRty8nJCfv9HucsSmn6rmNzcsJut6MsS0SEYRjYbDZst1vqqiLEiPeepq7Z7rbUdYP3nhgjVVWx227ZbDb0wwBAWRTsdjs2Jyd0XYfWGmstbbvnZHPCvt1jrcNoTdt1nOTnF0WBAvr8/N12S1lVSIyM40gz9amuCSGkPjUN2+2WpmkYx3Hp026H2m23UpQlMQRijLiioO97iqIghICI4KylHwbKomD0HgXYua1kHAeU1hhjGIaBsiwZhgFjDEopxnHMbT3WOkSE4D1FWdL3Pc7lthAojp9/1Cc/Pd85+r6nLPPzlcIYyzD0lGXFOLx4n/w4UpQlqu97AQGY/0xvlVIgMr8XERRAagYUMbdNP1UoBJn/Mv02SrofHHyvSPdFrZ6xftbUl9X10z10vk7rdNGZfh6NAzV3fH5vjTGM44jWGqUV3nsK59JKK4XSiuADzjn8OKKMIQrEEKgrh/gBXdh04xjBOPADWJfaRECbpS2G1AGtwXuwBcQRlE6dCgGMzX9Nmm3v02/DmNsAH8E6hm5ASDtIJPVzHAasc0iMiAjTGK21+bqlTZ1evy6TvYQQKKuKtt1TVTV+HIkilGXJfr+nqhr82FNVCpTjqec6tqHh2es9EYM1hr7vqeqKoe8xxqKUYhgHyrJi6DuscyjIZlHRdR2uKIgxIjHgipK+aynKihA8EgVXFHRdR1lWeD8C0FQOE1tedlPFietBK0Jw7NuWk02TcMWkPnVdR7NpaNsOay1aa/q+p6lrlIhIu99jrMUYTdt2bDYb2n2bQFAnECzrDSrsEV3w37+u+dSXI1970nBtL4x+2p7L1lIqtU2mwrR91bQ9FTGutns2gZh/m36oiHkjT9chKm8gwRnFpTpyzy2KB+4N3H93oChLrp/u2dQVPi9qXdfsdzuquiZ4T4iRqizZt/sFBEMGwTLPdlGWBO+JIhhbUOiOR54o+Ln/qvnSN9IuLB1YDVrxZ/o3jU/y+wP8USz2O11zCE+zGUeBEGHw6fu/8HLhJ94WuOc2w2434pxGK80wDFRVRZ9BUCvFMI5UZYkahkFijPPqiSRwmcAtiKIuIw99RfMz/9myH4STagGd407NY1l9VvN/x5OwDC3j4OEErH4vR5M4PSPtnHTNtodNBR/+QeEt90TaDrSSA3BN/Ra00kSJaLVCxvlCrRERoijqCj7zFfgnD1pCTIMPUeFjwjwRiKKIKAQ14970igIh/50+pzaV3sf8HRBYvp+uF9KuX9/74Lu8C3yETQnDCP/4N+Dzf2ioKyFEQefxJC+Ux6pJi913CRiA5BuzzzXW4azwx08E/sUnS7QSnFWEONlkWprUGUGi5Elbde5ohxwMIAqCENffTZOafztPWkzPWHaFmi8SWXajD2ANKK356Y8Ljz+rqStL12UOMY6g1MwNiqJAb05OGIYBlKLIaN80TUZxxS99ruLp00jpFD7I3Ml4sAPSFly/1tt/svPJnvXqugk/RNJ7pVa74vg1T5bMkyRHNhIjlEZ4aqv56G+DkpGqbtjv9xSZxY7jSF3VtG2LnehtDIFhGGiaht1uz2ZT8vuPwWceiVyowYcVEh+trFKw66EbhTMGPfUvj16tbDwKi/fIM1QVirpQSFwGdh4IrhzFPMnTJPkIJ6Xw2a8ZvvotuPumPVXdMAw9xhicc7RdR13X2LIoCT6gFPPWsK5A4fmt3ze0g6ZyQph7s2BGzB06beHeW4S3v1Zx88XMB894huX3aULWELcM6tcfFh7+48jljZ4HeC7QyjJp8QgdBTAarrfCZ7+uePWtBbt2oHAGiYlyO+fSWNP0TWQzkzQFEhT/93GFM9PWFUSSd1hv2edb4Qdfr/ipdxoKyzlYrVZ/1RG2q6PrIpdrxeCFL34jcuMFTZhMI0/4ercofb6bnUmphv/zJyoBoZKDCZzH2mfGBuBDwLmC4Ad2o+GpU401iXxI3q5rN9YOwh03CD/1Tk1hYfCCP3iB98LoYfSSX6xeS1sIsGuhH4Qfvd/w+tvhmdOIXnmGNehN8Yg62hETtxCVOMpjzwnbvacqC7wPCwhmwNebzYYxh6hFUdC2e+qmZrsf2fUxcwGZO7DeJdtOePM9UNg0GKsTxT9+memlVu/PtAtGCz6kyfmxNxteeys8exrQHMYwrFZ+Akt1YCLJDrSGdlAEVTL0LUVZHIHgHr3bbnFFAUoxDAN13TB0LaItPqozZGRBYkUIkRs38QxJOWMFcs7nI+K0RvvRJ5f2vrcY7n0pPHMa0OqIH8jihdb3iBMvyZPio7Bve8o6eTalVAbBlqZu0GVV4b2HOcbvsa4ghuXuckxkspsKIeJ9yCHtalDyAgM/Z3KWVVM5vE6fRp8Cx/e/1fLqm9MkzC4yP58ziyKoVWgfs4+0zjEeaxHO0Q8DemFI6S5a6XmvBzn092vSIZJYljoembzA2NXZuZEXALBpUnqfnv3+t1ruuio8ez35ohhldnmIEKPMBCrm2RBSWwhyqDFk7jzpBHpSSRAIIWCdJXiPUpp5eGq5+ewFOIcTZNp6PNhjF/aiFrOmzkA/Jif1997meOVVeHab9IRwHvNcBUkx+0ZRELzHWEcI6bfGaPw44pxbgaAixd1tiyvrFIvLoVK0pqXTKryozcv5AcyxL588zEx/42LvWsMYwAI//oDl5TcIz+9i0k7imiXKYgrz85IHK4qSsWspigyCw5h1jzaBYFGWkIXOpmkYuj3GuOz+Vh2bCNCKm0c5y/WnACVMf9evVdt6paIonBEqG+i9IkgKrqJoAoq9Vxil+LsPFNx6CU73yUPFtWnme0tcg6LQDz1F3dAPGQTzQjd1ja7qmjEHCdamwMEWJTEGhDjf5DyDlZVxH9t04fSf6mWtmjHFGMPdLwlUssV3e0K3x3d7Yt+ixpa+a6lo+cBbI1cvJB5yuEDMDDWuOuOcww89hUs7wK8EWRtjXMRKSaEjuU0mAhQXEjTNhRbOYMB6q//hYx37PibBcu3HmQAogdStNzoun1hiBtVbbiy5rxrYt2HhjyrxDq3Tb2+8qPiNL0Uef05TuUlUVceSQ9b/FGmMmhBjjkXUPFbrxzFpclkRsq4gxB5wZ/z0egKSGiNZ1Fi+c07zR9/q+PC/+WbyxUodibGJUWgFuy5y/5874SffdSt+SG1KK264WHHDJQ7EarXqh7PQd6dILIhiZqp+LJRI/hBjRBtH9FkRMpoxM0FbNw1tu8cYO0dJJyc1EgdEDHEVI8gq5FTn2IQiUd+bb3D86F+7iV0X8qqtRRfmHee98Oo76uRO1dL58CIuYzK75AEWEwCFqEO2OkVkzhWMwx7namLwc54ihcPbbRILQ2AYR5qmZuhatCmSq4sHAevsBteubt4ZipxIUbz9ey//KRVCoR/kQDNgHU2vIsIFilbcJV80d2vRTbMJwDD0FJcadrsO5yyFcxkEG+wEglprXAbBuioJpx7J20syu5EV2skR8q0XLUbo+sCZmFiOQrGVpnesH8oLudOZbCyq1Bwm5vh5Fk3yTFjrGPuOoiiIMTB6T1lVdH2HDSEk4Mura4xBMmhMhGf6bg5a1yLFOfG6Nalno5ez8hBHq3oOSUq7SIMkIebgGZOHz6A50d1JepFZYlGrYCmijWEMAa0USqlEjozBpkxQQZSYQNA6YhxQys6JqCl1RZwmQp1RhWcQtIonr4189MEn6MaIPtqqa11vUQkOw+wYhabS/PjfuIUrFyxjENQ5iJOImczeZVG2OeAoMUa0tsjowSi0Uow5D2mbuqHtUnbYWUvbtZycNMTQg9hlF8Y1ACU3hshZDMifjU7xeMpkzUH07DqVWmsMC9AqBVGB0epA+8+b6pD5TR+yZ5FJ2zmIK9QBCIbgGUPIILjH7nbbOWMyjANNs2Hs9mhTZpRdmW5WVdQ5/HZazdELVy86Pvye27OO+B1h4Iz2LwLOJAXaaqGooR/IijQLyZ/2zlokORNYRfp+PB8Em01yg8MwZBCc4uSKeDoiohMZWgOhnBfhLisAMOYILC2nnM2KrFZbq/Mjw36MVIXikW/Do8/AX3wlXKiWLNCU5JxCaH08g3MmWlG44iwIliVd22InD5C2W8QaSwxTNJj0+5gZmFpFXS+WDtMaqsJ8RwcYo9ANcmY7hCg0JXz+D+AnPpYyPt9/N3zkPSv1N09AjJkKQ8IbkYUAZZMNMaCNZfSJlyil8CFgrE1UeEobxzh5gRGUnW188qcrPrMKQeUAzbWCbhA+/cVrtH1cmcDCCBVJC3zV7RWve2WTkP7ATBTGCJ/9mvDIE5qXXhQeegS+8VTkrpv1gSK8lo7j7K4FLQvlXbJdMfdDEUPAOIetqpSinvXydmGCTLuAQ1RdhPlDtxRzEPTUUwMf+62nk82qVc4vA58isuuEN77mhO++q2H06+KJNIlDL3zfywauNo7Hrxne8dqRqxsYvKYqWAkbZ2U7RGVSlhqcdfihw7lyKQOYTGC32+V6Gr8Kh3doXc4oq9SKaU+DjhMKr0mNoh+F224q+Okfvp22Dys3eMRlonDzlYLBy5FLzIrzqPjeVyje9arH+IVPPc8/etsrsLY5AOSpBmWK+2fTPxJw+qGnuFwfgGDbtjRNg53SYNqYBIJtS9PUhOsDEqeIcEH/yLKN14g4A3FufsXN5Txx6txsQTKDEBcmuM45hChUhaUqLG0HFzep2CJJXIrn95HnW+HSBSGEdL/jCpipi24FgpJBsMqCSAJBYwAhxCSJRe9R2qzmd0VVJgBSa5FEHegCkHBAMkc9pkDTZtVzpCiz1K4SXcBohdGK9/zlq9z/+ku89GqJs2mVfRQ++I6GX/5c5LNfDVxs9AEBOnCzkoiQsZZhTCCoVSoFctZhJUa0tdleEmWUOKKwh7JXtnGOkpSHauiS+Jvi7sMOLdUg07cxJxyqQuOsOoqAhHvuqLjnjuqMo/yB76n5ge+Bj3yy52c+MaZUWgZatVKyU9djZrb+AASVM9iyqui6FmNsqtPre042NTH2K/FzFYnJeWJnnij9wiLJIYVdIsoQYh685ne+2vNvP9Oz67L30DopvqvM8RSLvPley4+8peYDf6Xk9x4N/Ob/jly5oImZtyxZZ4W1Dj92OJvqjs4HQZ9BsG4yEywQYqrjWalB00qLknNqAGSVC5dz7H4hJwrBh/QqC83DXx9458929FLgbKa/Wp0RUlGCBv79/xx4/FrHP31Xw7vfYPj4FwZEioUcZZ1RkFSLeLlht2vPB8G+7zFa44qCtm3ZNBXx+pAES1miwZl+TnV4Z1jhmouucUNmLX49HeMY8CHJb7/6+Uivam670bDvobTQeeaE60EEKkJTO37tCwP/8K9H7rxZc0MT6EahsGrBrQzeRVEw9u0BE6yriq5t0eM4prwAzGnj4EeUNrO0vAiOcpAgFQ7d4EHSQxbqLKuKjiVqilzvFPvRAkLrNc4ont0JP/T98F/+geL1LxOut1PYK/zSjyj+3XvV7CWCKJ7fC7fdYPhX773ISREZwxIRTjgVQpjzAkoptE6SmHXuMDMka58vy7ZW2cYPkxBytvhpJU/LqlL0TLZMgQ+RbStE0QcTGqLwhjvhzpcovvsO6EfBR+FKA2+8E954N9xySejHJeoPAlcvaGrrGf0UI+RodYoZVt5GrTNDZVmm4kNFqgnuO6wrEImZZ0/5uuVm6xqgo9zJ7DinUHS+/ih7E4LQjxEfV/iB5OdmDh/S55T+WlxYjId9CRF2fbqGGFdqUjI1ax1+GHAuVa967ymKgqHvzwHBZpMTIwVaxbmY8bygdilVUQdh8yzKrWiQIKhVCCsIwxjnOCBEiEFwFh78XyMXysB/eyRSJgvh29ciH/udAWvgm89ESptYz4QN13aRfR+T3D7XLyuUEsaxp7jhBUBws9mkWGAGwT2bpkLvhlxVoQ6Ej2mkYtJAHn9uydqKOawfPOQBh/K51gYZTul2A3CZukgh8I0nmge/GPhPXxgpnKHOA7Va+MlfGQDFpjb4KGgtXGg033q244O/8CjX42WqRi2eIArOwEld0ne5SCoERr8USemh77E2g6D3OFfQDyOXGsOFKleG5V7LKisbA2wqzW/+j1OefM5z6cQw+mSvIQohpPfTK6xfIdUg3nSlxppk5++5z3C5ijz+rGcM4EWz6yLPbAPPbgPXdhEfFT4qru0C374WePcbHJcbxSOPjTz6nEvFXit7HINwoRRq6zG2IHifKkS0YRgHCuewS33qotv7AE2tuOsliq89IdRqUWMW0xLqQvPNZ4T3feRRfv79t3Pb1fKACZ5ROY7i/kubDd96codE4fvusvzaB0p+5Xc9+2HZwpxTWRRFcd+rLH/7vtT9X//dLWXVoIyZMcsqYfDCK27SlEVKsJ6nyNiiLGcTsNbS9wPWlcDAX/ou4RNfkoUNropzpkzx5YsND33lOd7yoa9z370NFyo9e44lGFKs6+VEUgYI4HTbc0Oj+PAP3cWb7nW86V73Z6o7/rmPP8GDD++5fOUKUdSBKOgDvOVegzaWft/R1KkcMMRIUZR0fYc6PT2VCQRDCFR1zXa74+Sk5slrgbf+855trzBmISMcFCcEtIx0Xc92NxCjn0WKKduj0OduhzRRkaFv+fMvV7z3r76E77qtngXRFyqwRuDpU89//NyzfOILAxev3Igp6lQSO+1iL1zaaB76UMkN1YAtGrquxVqL0Zqu72maBhVjlEkQ0Voz9D1VXbPfd5ycWP7lJ+FDv9py6xVD7w9jPokTPqSctM6zI8eeQ50NiGZWLRHEc/3501TS7iJmii1W91BKL/5dKYagUbbmypWLaFeBMvNDCgNPXId/9rcMH3yHYfSJCU7lQDHGFAt0HaptWzHGEHMdjDEG70OKCiWCgnf/fOC3vzJy00XN6A8V4gQHq8BZyPnE1VEapQ5zibIuhU+nOpQEFIEY4orILDgwEZdJ+9daobRFlEmsNYNOYeCZLbzp1Yr/8PdLDEn/s9YyJYFU1gmtMaiubWUarERBG0PMF4aY/PLTW8Xf/NmBL/9J4KaLqVJ8LpGZcwTLSsecutKrE0JyFKOv8uWrytGUi59X/xwFehY6coZnfpFyEE+fCq+7PQ3+lksaH5YwP4aA0jqH4alNxRCl61cmMB0u6LukEIvC6ZGn9wU/9q9bPv3lwMVGUblDgRQOA8FjJWga9NG4Dn63Tmiel0uYZDc5ktUnAea0E97+OsdHf9hw240FbR9QEilWh0DWp+OGvl+BYPApNq8q9rsddT5jJzFiXUn0Laao+MVP9/ziQwP/78mkIjur0fooqQkHWuJ8oGbVlgDwMJ+tVqHydA85iCwPwTHxivTtnTcb3vdAwd95s1CW9cz6jDa0XZuOAbXfAQTnL+qaLp8jUBkYy6qm71qaTcm1rfCp3+t4+I8Mf/B4z2mvs1kIWpukuSs9a3xaqSSPiV5CIqWRmEwtZveSNSKUMoiERb2JEa1N0vdz0tZq4fKJ4Y4rgftfU/PAazSXTzwxluzbljofA1rOB7VUVZWOBoV0tqhrO1S734uxNh8xA2MNPh8xCxMwWsM4emzW1UqnsM4AHrD0fZaatMaPHlekI3Za50OK3lMUjmEY88HFqSTPpbDUTHmJiHOWYRhxzhJCRFgObhauIIT0rMIZlIwo64CAH8FHjYT8fO+T5qg1wXusy21aZ3HVY61DdV0nxzVCx3VDB0WG69obiZh8anFdezOfK8ht6WyOPqjlkdXJslkpEkHpo0OWR22y4oQhyuxWjWY563RwyFMdjovldJrWGu2cSy4QDg4YTkWFWmu8T6sf8mFKrRQSPWXhCMHnMzlLumlSmkXILsgx5LYYIyEj8DCOaG3xIaQOGZN2iU1tiTFqxtGn+/owK9AprW+R6HFWz+kuZy0+5/6Zdlru0yz85GyYH0f0fr+b4+RxGKjrmq5tKYsCiZGQ5aO2TURisquySjU29VxrLLnavGVKuCqlUia2a1fHcNIh7T6DUN93qWIzF2s3TUPX5qJGUuw+Pb+qSmKMxBhmXb/KzxcRyqJI9l/VjOMAwtKnup6PBqVywJaqrs+CYJ+ZYNd18+nxyTV2XUfhHJJPflZZVyvK1LEwlZ60C+uSzLvbLk3WMI5z7V6Xj630fT+fHl/aOoyxMzut65q2S5PF+vldS1Hk5wefTqO2LeUMgoGyPJyshQm2CxOcz9RqnbbyxA7hTJsibc0wtx0itjkiHSFGrDFJk8uZaMlmEELA6CkTvWrLpTqSzTAc9em4bd0na0yuCVTz81OfIkqrmQgZa/j/n1rcNfMGQFAAAAAASUVORK5CYII=">
<link rel="apple-touch-icon" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAAXNSR0IArs4c6QAAAERlWElmTU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAQKADAAQAAAABAAAAQAAAAABGUUKwAAAW8UlEQVRoBYWaaaxd11XHz3gnP/s5sZ+H2E2c2dhSSgtKk5AB2g9FTShUAYUWpSFB8KXQMlVUEYPEIKGqRGKQAPGlElHaJgwlrSiRUCJa0sZpJodSkjZOSkINdZrp2e/ee2Z+/7X2ue++JIXj53P32XsN/7X22nuvvc+J27Zrmrosy/F4PJ/PsiyPoqiuq+FwNJ/NhqNRXddd1w0G+Ww2h6YsiiRN0jQr5vPxZDKbzfLcWKp6OBryOFqw5PkMmvG4KIo0TZMkoWBa5j1LhfwtLIMBj8ssqOMROQuW0Xg8nU5Hw2HTNkVRxlVZtgDsOhS0bRPHCQb4Y9M0KG7blpokjpu29ceY5zimnkdoYBRL22KWWJK07YwlSRYS3oTFlQaWBAzS8n+zbNGSwACGpMCAtsmyrKqqLM2oAgrIqqrE6KauY2xKEvqBx7qqKIuzaXgUS9azSIJY6qbGWrFUldNQBtyCJYelabqoSzdZcJxr2cKC/Sah3KpFNDUSFBcDORI6QoiuJyoy4qHrADEYDOfz+ZCeMlJ4eIQGSkUDFpYFAUBUEHVx1FV1DTESYNxkKeajoViMI6VAmBXzIh+YlroeOAtaagFCO1rGiO1Z3jTqoAE6yAmfeGNjA+ngg5RaXE540AMl+IYjKMAaxxH1o/Ggq8s4ycsaC1vvtDzLiUUcjFX0EgjUS2KJYcnzQVnJf/QqQWIs9NIACwkqecF6CUY5hY7r6myQ10UZpXlLyLZtPhg4MGhgodMCMLTEMepivKJo7CMYMVsDWtE5yKKyib9+Mv7qt6LnX05f2cCAji6HLdGYgR+mmODjkcoY2aEW9hZjog632P8Wxpgx0sXUUKDVWOI4z6Ido+6Ctfhtb2mO7m9HYGu+50gznHB3vQFdJ3F2lwH9I15J4+6hZ+NPPZx97dtdWTOaGWpQQAUC/WqAhgqVFzWqFOYtNVQ6TUuTPZjxlERGJbYN8+jIOe3NV3ZXXBCVVRvJQulbIOxxqjqeEkI2LRIt9BedCykdWlbVaEgAVJ/8cnb3I2lVd3kqNPxJmamXVL+WUfcmAVBkPb0TLj+6Jb0IGWD/dauaiG5//zu6W38oIiYVmX0I2VQx4JFuJqK2DmKNyAy1jPEh6IviTx8YfPbxZJDRP6ZoAcj9aBY4Su5cgcxoeTBM4g2tovC2JfcbI7X8BiW4yXqDDr/x7e0vvzsFiYBpgdJUsTmIGQksCrZsaQ3SIG4aBX2ed/X8M48O//7xZJhpEQjCLR7U0bqkUuX+vihQaX8iUoGIDzUq6M9qKCwu61GJUqXRoJI+/9vH0ru+Uo/HBqxpWE+1vA6HjH5IJ5NJjE2aR5vGJxDmHySkcfP0qfwjd8VVo7Eqib2qhVbV9LX+i8rlAo/uUsdEWW5QrVB6YUGzrGLRY1CyXozy6I9vqg6fk9SNFpOAk5mtI4doMo8tZjQeuON++OMk/fSx+EwRj3LI0MJdiv2ijF8JU+aiUCOortcAGlbngkL8dh/mTAlWMjYiTLK5zOWi6xspajFXBhCtz6K7H81+a38D2SZOTV8xKy92aJgTJBiXmFnw/NfL8bHnNIwQqMkJ2YKq/0DHEzwf3R8dOEuopVftBkYkqLVK6zdjFgGB9PCz3SvTaJAj00BD6fTG5GVzlz8HGgLp2HPJyVfa/TsljMWBOVQexbVpkvnCyUhnUWRNyEjTsujJF9r1WYr75VXk9B0AF+hXx93Hrk+uuQS7HYLBEWZoZY5dlNXclzXl/81Xo7+4v1kvE+Z4gHqzW+iGyLAlo0y1JL5ypnv6VPqWXc2sIEXQ7MLKS7CwxiWMXYIH9D60Wf7w1QuveujI+aCStTJDfwzyj/5ofN1hliPGovpvcddgVSblf4gJZWjgnBbRgdXu1mvSSdbNS5psoJtYelUKeuzhyWuoJA/ooqdOMr9rdlG2wiAmY4giBnFCssrAxRTlOZb3ElEvrpPDoUCeRzo+8D+SiAvWumsPqwdplUpTHVT2MLx+ce9bu1kZHTwr/rlrk3HWskJJgMmQ4/nzJ680l1HjfULh1VnKE+uSJ2BkhFQyhSaEjVIOTy3pF+UsrLiJXAuJSZbMfuDuX22zVI9crkslLqf2gjdsrTEJ8byKzj07vu2adJi26gfv214UStUbdkHvSpGMdTNl/ZFlU8pGFSlRxHzK6JWRgLbRGgZikNuLWMjSItGo73S5pmWsvW5vdxrx9s8uZ15Gh3bFt16dDZKmMBtCOPUuk0ip9ptMMiUOM+B0kcQBE0/GYGQYEEjcZQ8zkiJEnGJe+gM/PeTIFneTHmJgE2zv1B68WtS5hmZWReevxbdcTRxgg3pdGa0DRan6AUL3kXhazdcCtsDpSWddNwljV4OYoV0qnUYY0pQ/mWbTamY4IK+lyUKW+6LCkenRJy5jX7rBwJTAts7wtRH9cPGe+INXE49tUWlEaUNgQ2vTZb7Gy7A47GDynJhXxmCDmI1lMhlPWA40iNl5aL+rzJw1YUm39aaEa5Lx+gB3AX+JmqbXXVQIA+B85vGOjaKiig7vi2++MmPAMT1s4lZRrvEfZydA6AHQAxq0rMdomc5mybwIPcAmiGzUZj7iDJlBIloRpHAy9BJqV5AuYP/Pn4GPcHWWtCwjTaudAZl83cXTMr50f/KBK5VUkvASKSiRBpMppb0llqNpywZOeoAEAhpmTjyeMqUvYsuWVm0MkBIC0QQubo7fzfAyy3iWsol78z+mCMigz7PkvLPLuJ7XZVEVRc1fWTZVOZ+VR/bWP325UGv32vtr2S+0aQQQhC1zYH+SECs5zbQs29xJ6DA3GV1QaSLC0u4ghNtR26/iOo5eWq/PzGDUuPBGL/h91450PGTdUK+dt2+4bVRMmUoNEEshXKnZf+mB9HPH2/UiSZjf0WnSxMO16BZ8YacnAPZsgsdMu97lPbGcAPvmIDYzhIwO0QA3LAFoHK9Pm49/+uRLpzlcUdbtQOUss6Wq2yuPrPz89Xs87cuyZP/usfM6Mhmt5Dd+6UxbVxttO9SWM/hBTVyi7+R74DNnEkKeTgObWMo4JyJ+bCXWeQFBpvhn3upSR+P6ghmbyk10F+HdH37rjhdf0xYbNLQbJLmWB4L46KGxxo5FdjALVvnSKI1B7Vy4R6GvFiiJfonSfy6OrdgQNmWh0xMO4EjhiBvyoIz/mGLbnGLI4GjqFiSJsm2c4eySiRKuIC4Aopb0+PordqreCZxmiRjHLWfdJiUQBw7JXmIw2bauWq1ZQzPHhzwzzTDv67jETqjIg8hGmXk6nsmTyJAwBtQMBnJvEwz2Hi4lmaLL715gBqSwZFowx41a1IvNrx5v/6yYDwJFHTi8I1CIMTQzMdIrdlSjAzVGLCzaalpQa32m/yx2EWHTqH703zEjTmNg0e+0IICxYioXPl5g+l4FwGdykaKLqweLODnHgkhl73qBh8QMNnUWpYbTDSaHyHQSqIO2cMbEeDAGkyBOMUr2ppek2GqlflZ0n7zv1KlXGQNopYI/4xCVNvXoDyhVocVkbTW75d1r4wFHsUYKQ0+BVRCYgSZJi7cEcRcSxmWSECYkc0ygwGZMZ5z1cQbGXKQ01Y4WlUb4IBYPq2+AgijJkEBdlAS241CRhbvVVGJqDLO1gd14glG2g6EbWbAk1pEFWbJEFVIgX+nSCAwS1fM6LWOx04aGKYc7g5jxkM2nc86FfEOjQcxhupymQWzq4bRC8Ij0hCYLg9Eg+dCP7wshJINM9+K2qLGCN7Lq0V0Z285YG2vAGXSZ5Br93ntfrdTYII4dvaZRO/OcMIjVA3ZKSpsGhxZm+pG801cf84FpliA5k0twrE4/PJDRqpoqb9eDXVtrAo8RPvZ89Nos+oHzovFAlJJmze530+AS5HyJ0TTaVYXyCNDbbr7jvcHrTyXcu8SzFcTpF8JNh91dX98EesHmv1FLmTfZRr7iFNUenR2iQRbf+3j0e5+X+9/7/dHvvFfNyCf6fQwgjhmHEdWDQbeXuy2nEhHHqXYMAS0BXPfrs6kTHOfvtZsIMPa1qEQTPfXAI+svn2YjYYRSLEv4R9MlB8Zvu2iiZN5sC5K79l9PxM+/zNa++5dvROvTdnWbHWOYTpliLkePcfHrYOQXhGuU+6mE1iudSujQyweHTiWIUI0dDZuFUsqGWwAcDMK5kDidt//86Gsvn9FJpdtgnoNc2eVLrzUYsIReXBwYXnlucfdD2elZfPX5zSAlVnVAYfwWLXKehPdKhEbH4Jy+12HrolOJthNgVmLinuE7L3ghMOR8BYNJsWz2QQiX+dOh+4gLronYNKxM0l/5yf1nZps9YCymu4t2rWbMTwtHuLi6ja66sLt2z38+8kxxyzsOkRSLIISQ/K0VSRWyyJuotfw58VMJ0h/fumgl5r9yIb3OsHMh7TB1ZCch8C2UUzD0/Hodd9wFwe7VfG2nthdvuDwBs+qFMGOZjLLtE46P2x3bNIAs+jlJaIuadI29gty/PPvJLA3iqCw3TyWwczad8cKjxFJeEzEFaUgoxOCmW+UIw2pQzRaq7Ncc1FtCrL8BeqiQy4FOUhVFAwAo1JFJVfyxm/ZMi27trNxjb/f25OM/s3Lng+2DzzSjoQ0JqdVosO7QgRwnZUS4TtszOlaZP/Gv1IcLMLr3Dnecjos6EGhnJOeZA+1XGN8MOXVmpNr4j0P4HQ1QluQ6GQU+lsSH9g2PHhqxGfA44dj0PW8f/vWHxjdflTKu6G1hcX3c8aVYjdtw+hNodDyKcUSY1mfWAU2K0DNzEIu4S3IcqMSo6N0iYfB7kx62XPDxrP8s0ttGTAzxXV+af+or5ayQ55RCqRGEeFl3zmHf94P5bT8yuv0nBg+fmH7zFEeotBts6wYzHGkNb+XCezcGBr1B+sDkSl9QYFGrmTvo5livhwP4XpP9mlIHzrPJl/7+Up1cHOxSRtJ1bDHufaT88J11Ohg5XKa6QLNIEKPuy3dzYlX97HXDdx2Njz/PC1a9asFAuzNfk6ThXjY0Gq68pyGDG43GCSOaXSbZBbkQ2wWMsWMikm/BNCskRhj5T8HRKjDNi3pUWOsvlF2jeBiXnmx/4ckuHozGw3TntvT8NRSmeZ4O83SQp+fuzs7ezrtJtlaje5+Q144ciHevsP2XXuskxTAv6+g35Wx+KmFfFExnU73+Z0DQA4xmtgvueIY8vI7WC0gKwrwUzDGjzD67yQbTCpGib30ez2odYhaN8u6iiW6/IfmnX0suO9iVVccifeCs+HMfSf7oJp0XmcEcL3TXHB7d/r4JOytTrYhFpnqgpTPDuZBmVYYWpxJ0CkFJPolXiDAFqBgsfnojDBZ45GWPAVoMu0Sb+3nevNTK+6ymnc41k4pSF73WXbgW7dzGEW8EPP52r3R7V6OL9kZD35iY+XrDx362VdYqpfbfYlNzETj1KkNH+3q9FCYhGiiJkmoVpBQP6FlCuLxsGlSnR7V62QmoDJSSgC85cmPZEqE1oAWvo8FOarVJJ8ZoZg/ixz5GhrP1FoLzxH4YSkvvWfumQ5OSOZpTFiKHXQLB40k2WSutbJrwgPbEIvPLbDGrkNaDR2Pfbr9Ct1RRlVVd8a47BRPiCKM/ua889s3ooWfiXNlD9x/f7n7375oTpyKOWhZCWdf/7VszlhfmIQx3eXHHuq/3qgC2QayvNtgF9CsxHzWM+NxGX7WwiA3SioUiMBtyk8Pwjp/7ToX/vJcEIcCVpYY+4KesHRPHh3NeNezcvzOqqnaUxV98ur3/6x3bMRj4o4vuuI99Fi/jknnV7VuNB3nywPHv/tnnX9159i6TT+8ROd32cY7Eau6nEnNyHzpIJxK8tcTr2AR6hrKdinYHd/ECx6BZ1/deUCb87y+U9x5b3zFhF6QJ03H4XRX8sYyqhe8akn27JznIyu7Wa7Oj53S41pdtXDAt2o15y44Uuwk2mg7u7H7hndocPPF8mw5XOBlRDFv0wbW2wsSoTyfsVCK8oVEuRBXBUlVayNjz42REHD2Hz35kv0OXLeZefiH77btObczK91x+1sSO3IC7fHlXeA1zZVWXr54uL9o7/Mwvjb74VLNR9DFmRho80bISX3Vxesn+9Phzs/uOzyfj7Y7EEBDS0WXnsSwwR4UF1wYxqVG5+a1EGMTae0Z8znHDHfV/v6bO9TAJliCvqav5xnx6eu8O3vbZKuppiGCYKT2HJVRkvNXqqPuD2w5d8X07zCmie9MLFU88u/Hhvzz51IuT8co2O8kSIQP64NnxP/76YMdI2w+gMxksxnSs10yvO5XQq5v0D/+hvOMLzbYRqaEuZuPgLSVGDeeAOIN4cw/RCRZxWzrD+43JgAPclWT6zrduu/TA0DxiEiVU9N7LRMuJ/ynvf3J2ulmZbN/BhzTWpm7amHcfvSH/jesTjuOZQ0k6iQJNoKxt6fK3Epvn7iCsz1TDH/vE7LkXW17rWioFHnVD+I8ZIEC5dY0M6KcsN4Jqa4GGJaXmTJABR8rlPBY+MsBkyB76PUnz8WScD0bMiVRYbVTV0YV7k3t+Mdmzqh0MuDVc+1MJjIk3zpwhfcAmf32JX3GmUrq2fPBE/sE/581sx8l4D1DA5W790+UoCTSZsmXWlWnQWN9oTmaPacYYl/FKiLGYISDWl1aa6WwcQkI2MR7Ef3Vb+q7LOA/tP3iyrEenEnYmbd9K6Kuzmi0B9tFHaCbIWKHTqP7sY/Gv3llMy45BJizWBep3aXMoQmgoVal6iGwpDMYFb9MCfus3yO2yKpnYC+TXfaAKdp7bhvEnPpDeeAXfT+q4BHoH5lkzOJU6sH65WrsHUVJmDuVDmy89Hf3mPeXXXiCP9cMcaMJl1E7YV/Ek/BJm/83OnkN9YtbRJIIeec8cfnE82cTRg8nv/1R+3RFWLveIOGRfUGBqqGFXRvyxGPMWTS8/lLvY54/2ARwTK+/RXtmI7nqwuudYfeI7LS+rkWC4HIRslWZkS3R4okJU+gmajEaTgVvmPCbKW+weR6xXF+9Lb7w8e/9V6a4VUqaUr4EYrPgeCsIGr2tHZjM+sPVyiTbS6+FgyLsD2lCJMdoh+GeAyvD04ev66eLEd9NvnKxfPN0VtdZpvUrjkzhHBWCzwfAaMFrtQ1RGcR/lEOvg0swNH8wxqenEIVKU7tmRXLDWHjl3OCGc7btAhin5AvO9gPkHTwAr+VZCiTOxtDSI+1MJcEDNmCZZxSSCD2CQjkZ8HFlwDkMsE4x8WBjVRZQNo5YPHYGUdXUR89jwzaE+C+BLxyjlsRAloGFJFiykPhiemQSmd2jEwiwTZ4PZdO6JPb7td1ojAGAA719YiQHWD+KBeoCL4QupH9nRA9QggqML7pRxD51Iq2g8g7XvWhe+oTcgo9VYchQjJCReyyymxd0JC1PHphbOZHVEaVrsgyyixR230EIfg1QsbF1ygkqzasz0DJ3HFiGl7sZSdgjhQ2Pix5IK8m1LxDXHaaCyPdLHyDDak4WPaHjHzGeXb2DRWNC5OSx0aQjoxaPvQxg/LLSLl5CuxYJ+WUsPjGjUkJCVALYA1q89WCGUfELpG7yyvy+INxmxzAWqqp8xJC9cYnEfSWevTuFEi02oy5VLxM4PWuj6OUwzAjuyHDN4zaF1wDwBDfGwuSzIdzq9IAq5y3kc9CmExAKlu8FYeF2inR307ifijZnNWJTDbGoxxxFCxAxil1h02EygO0uYG8kyDZg6MMyN5JrqZIg9neYjS//sWUObKBrkdkhhJ42wQU3YadfvswFjmulVQ9y+VlaOrN2qXoAO9D0hLOBmzGga8C8mTfdwEFiAjluYSWhFjjICrcJicSSac9Cirz/1YQGjy4EFFoD5txKj0f8CYdViciGszw4AAAAASUVORK5CYII=">
<style>
  :root {
    --bg:#fafafa; --surface:#ffffff; --surface-2:#f4f4f5; --field:#ffffff;
    --ink:#18181b; --strong:#09090b; --muted:#71717a; --faint:#a1a1aa;
    --line:#e4e4e7; --accent:#2563eb; --accent-text:#1d4ed8; --accent-ink:#ffffff; --ai:#7c3aed;
    --accent-weak:#eff6ff; --accent-weak-2:#dbeafe;
    --ok:#16a34a; --ok-text:#15803d; --ok-bg:#f0fdf4; --ok-tint:#f7fdf9;
    --bad:#c81e1e; --bad-bg:#fef2f2;
    --warn:#a16207; --warn-strong:#b45309; --warn-line:#f59e0b; --warn-bg:#fffbeb; --warn-chip:#fde68a;
    --neutral-tint:#f4f4f5; --accent-tint:#f5f8ff;
    --track:#f4f4f5; --btn-dark:#18181b;
    --shadow:0 1px 2px rgba(0,0,0,.05);
    --radius:10px;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
    color-scheme:light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#09090b; --surface:#18181b; --surface-2:#27272a; --field:#101013;
      --ink:#ededef; --strong:#fafafa; --muted:#a1a1aa; --faint:#71717a;
      --line:#2e2e33; --accent:#2563eb; --accent-text:#60a5fa; --accent-ink:#ffffff; --ai:#a78bfa;
      --accent-weak:#182135; --accent-weak-2:#20304d;
      --ok:#22c55e; --ok-text:#4ade80; --ok-bg:#0e2417; --ok-tint:#0d1f15;
      --bad:#f87171; --bad-bg:#2a1416;
      --warn:#fbbf24; --warn-strong:#fcd34d; --warn-line:#b45309; --warn-bg:#251c0e; --warn-chip:#3d2f12;
      --neutral-tint:#1c1c1f; --accent-tint:#141a26;
      --track:#27272a; --btn-dark:#27272a;
      --shadow:0 1px 2px rgba(0,0,0,.4);
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"] {
      --bg:#09090b; --surface:#18181b; --surface-2:#27272a; --field:#101013;
      --ink:#ededef; --strong:#fafafa; --muted:#a1a1aa; --faint:#71717a;
      --line:#2e2e33; --accent:#2563eb; --accent-text:#60a5fa; --accent-ink:#ffffff; --ai:#a78bfa;
      --accent-weak:#182135; --accent-weak-2:#20304d;
      --ok:#22c55e; --ok-text:#4ade80; --ok-bg:#0e2417; --ok-tint:#0d1f15;
      --bad:#f87171; --bad-bg:#2a1416;
      --warn:#fbbf24; --warn-strong:#fcd34d; --warn-line:#b45309; --warn-bg:#251c0e; --warn-chip:#3d2f12;
      --neutral-tint:#1c1c1f; --accent-tint:#141a26;
      --track:#27272a; --btn-dark:#27272a;
      --shadow:0 1px 2px rgba(0,0,0,.4);
      color-scheme:dark;
  }
  /* Brand logomark — theme-matched tile logos: dark-background tile in dark mode,
     light-background tile in light mode (base64 @96px from assets/logo-{dark,light}mode.png).
     Regenerate with: python scripts/embed_brand_assets.py */
  :root { --lm-lightmode:url("data:image/png;base64,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"); --lm-darkmode:url("data:image/png;base64,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"); --lm:var(--lm-lightmode); }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --lm:var(--lm-darkmode); } }
  :root[data-theme="dark"] { --lm:var(--lm-darkmode); }
  :root[data-theme="light"] { --lm:var(--lm-lightmode); }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; color:var(--ink); background:var(--bg); -webkit-font-smoothing:antialiased; }
  .app { display:grid; grid-template-columns:248px 1fr; min-height:100vh; }
  /* Left nav rail */
  aside.nav { background:var(--surface); border-right:1px solid var(--line); padding:16px 12px; position:sticky; top:0; height:100vh; display:flex; flex-direction:column; }
  .brand { display:flex; align-items:center; gap:8px; font-size:15px; font-weight:800; letter-spacing:-.01em; padding:12px 10px; }
  .brand-logo { width:22px; height:22px; flex:none; background:var(--lm) center/contain no-repeat; }
  .navlist { display:flex; flex-direction:column; gap:3px; }
  .tab { display:flex; align-items:center; gap:10px; width:100%; text-align:left; margin:0; padding:8px 10px; border:0; border-radius:8px; background:transparent; color:var(--muted); font-weight:500; font-size:13.5px; cursor:pointer; transition:background .12s, color .12s; }
  .tab:hover { background:var(--surface-2); filter:none; }
  .tab.active { background:var(--accent-weak); color:var(--accent-text); }
  .tab .ic { width:17px; height:17px; flex:none; opacity:.9; }
  .tab.active .ic { opacity:1; }
  /* Count of items needing the user, on the Notifications tab. Hidden (via [hidden]) when zero. */
  .nav-badge { margin-left:auto; min-width:18px; height:18px; padding:0 5px; border-radius:9px; background:var(--warn-line,#d97706); color:#fff; font-size:11px; font-weight:700; line-height:18px; text-align:center; }
  .nav-badge[hidden] { display:none; }
  .nav-foot { margin-top:auto; display:flex; flex-direction:column; gap:10px; padding:12px 0; }
  #theme-toggle { width:100%; margin:0; padding:9px; display:flex; align-items:center; justify-content:center; gap:7px; background:var(--surface-2); color:var(--muted); font-weight:600; font-size:13px; border:1px solid var(--line); border-radius:8px; cursor:pointer; }
  #theme-toggle:hover { color:var(--ink); border-color:var(--muted); filter:none; }
  /* Inline stroke icon inside a text button (sits next to a label). */
  .btn-ic { width:15px; height:15px; flex:none; }
  label { display:block; font-size:12px; font-weight:600; color:var(--muted); margin:14px 0 4px; text-transform:uppercase; letter-spacing:.03em; }
  select, textarea, input { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; font:inherit; background:var(--field); color:var(--ink); }
  select:focus, textarea:focus, input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-weak); }
  textarea { min-height:140px; resize:vertical; }
  button { width:100%; margin-top:18px; padding:9px 14px; border:0; border-radius:8px; background:var(--accent); color:var(--accent-ink); font-weight:600; font-size:13.5px; cursor:pointer; transition:filter .12s; }
  button:hover { filter:brightness(1.08); }
  button:disabled { opacity:.5; cursor:wait; filter:none; }
  /* Account panel — a clickable button that opens the Claude-connection modal. */
  .account { display:block; width:100%; margin:0; text-align:left; cursor:pointer; border:1px solid var(--line); border-radius:10px; padding:9px 11px; font:inherit; font-size:12.5px; line-height:1.4; background:var(--surface-2); color:var(--ink); }
  .account:hover { border-color:var(--muted); filter:none; }
  .account .dot { display:inline-block; width:8px; height:8px; border-radius:99px; margin-right:6px; vertical-align:middle; }
  .account .on { background:var(--ok); } .account .off { background:var(--faint); }
  .account .acc-eng { font-weight:600; }
  .account .acc-sub { color:var(--muted); font-size:11.5px; margin-top:2px; }
  .account .acc-manage { color:var(--accent-text); font-size:11.5px; margin-top:4px; }
  /* Connection modal sections (subscription primary, API-key fallback). */
  .conn-sec { border:1px solid var(--line); border-radius:10px; padding:14px 15px; margin-top:12px; background:var(--surface); }
  .conn-head { display:flex; align-items:center; gap:8px; font-weight:600; font-size:14px; color:var(--strong); }
  .conn-head .tag { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.03em; padding:1px 7px; border-radius:99px; border:1px solid var(--line); color:var(--muted); }
  .conn-head .tag.primary { color:var(--accent-text); border-color:color-mix(in srgb, var(--accent-text) 40%, transparent); }
  .conn-body { color:var(--muted); font-size:12.5px; line-height:1.5; margin-top:6px; }
  .conn-body code { background:var(--surface-2); padding:1px 5px; border-radius:4px; font-size:12px; }
  .conn-row { display:flex; gap:8px; align-items:center; margin-top:10px; flex-wrap:wrap; }
  .conn-row input { flex:1; min-width:180px; margin:0; }
  .conn-row button { width:auto; margin:0; }
  .conn-ok { color:var(--ok-text); } .conn-off { color:var(--muted); }
  main { padding:28px 32px; overflow:auto; height:100vh; position:relative; }
  /* Persistent brand mark, top-right of the content area (theme-aware var --lm; decorative). */
  .brandmark { position:absolute; top:20px; right:32px; z-index:6; pointer-events:none;
    width:38px; height:38px; background:var(--lm) center/contain no-repeat; }
  /* Tailor/profile controls bar */
  .controls { display:flex; flex-wrap:wrap; gap:12px 16px; align-items:flex-end; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:16px 18px; margin-bottom:22px; box-shadow:var(--shadow); }
  .controls .ctrl { display:flex; flex-direction:column; gap:5px; flex:1 1 160px; min-width:140px; }
  .controls .ctrl.wide { flex-basis:100%; }
  .controls .ctrl label { margin:0; }
  .controls .ctrl input, .controls .ctrl select { margin:0; }
  .controls .ctrl textarea { min-height:90px; margin-top:6px; }
  .controls button { width:auto; margin:0; padding:10px 18px; white-space:nowrap; }
  .controls .ctrl-go { flex:0 0 auto; }
  .controls .ctrl.hidden { display:none; }
  .controls .ctrl .rl-h { color:var(--muted); font-size:12px; line-height:1.4; }
  #resume-pick .ctrl { flex-basis:100%; }
  .editor { max-width:none; }
  .editor h3 { margin:22px 0 8px; font-size:15px; }
  .editing { font-size:13px; color:var(--muted); line-height:1.5; }
  /* Page header — a title + constrained lead line that anchors a view (reusable across tabs). */
  .page-head { margin:20px 0; }
  .page-title { margin:0 0 5px; font-size:22px; font-weight:800; letter-spacing:-.02em; line-height:1.15; color:var(--strong); }
  .page-sub { max-width:66ch; margin:0; font-size:13px; color:var(--muted); line-height:1.55; }
  .page-sub code { background:var(--surface-2); padding:1px 5px; border-radius:4px; font-size:12px; }
  .form { border:1px solid var(--line); border-radius:10px; padding:14px; background:var(--surface); }
  .form input, .form select, .form textarea { margin-bottom:8px; }
  .form textarea { min-height:80px; }
  .row2 { display:flex; gap:8px; }
  .msg { font-size:13px; margin-top:6px; min-height:1em; }
  .msg.ok { color:var(--ok-text); } .msg.err { color:var(--bad); } .msg.busy { color:var(--muted); }
  /* Track tab */
  .tcounts { display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 14px; }
  .tcounts .pill { font-size:12px; font-weight:600; padding:6px 12px; border-radius:99px; background:var(--surface); border:1px solid var(--line); cursor:pointer; }
  .tcounts .pill.active { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
  .tcounts .pill .n { font-variant-numeric:tabular-nums; }
  /* Metric tiles — a KPI row of bordered stat cards (funnel stages, token spend); the
     Apify/Bright Data "statistics" look. The value is proportional sans (big numbers read
     loose in tabular-nums); the sub-line + meter carry magnitude and stage conversion. */
  .mtiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(118px, 1fr)); gap:10px; margin:4px 0 16px; }
  /* Funnel/spend tiles fill the full track-view width — no mid-screen cap (they auto-fit more
     tiles per row as the window widens). */
  .mtiles.tight { max-width:none; }
  .mtile { border:1px solid var(--line); border-radius:10px; padding:11px 13px; background:var(--surface); }
  .mtile-label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .mtile-val { font-size:24px; font-weight:700; letter-spacing:-.02em; color:var(--strong); line-height:1.1; margin-top:3px; }
  .mtile-sub { font-size:11.5px; color:var(--muted); font-family:var(--mono); font-variant-numeric:tabular-nums; margin-top:3px; min-height:1em; }
  .mtile-sub .conv { color:var(--ok-text); }
  .mtile-meter { margin-top:9px; height:4px; border-radius:99px; background:var(--accent-weak); overflow:hidden; }
  .mtile-meter-fill { height:100%; border-radius:99px; background:var(--accent); min-width:2px; transition:width .3s; }
  .tu-cap { font-size:12px; color:var(--muted); margin:0 0 8px; }
  .tu-toggle { display:inline-flex; align-items:center; gap:4px; margin:2px 0 0; }
  .tu-toggle .caret { display:inline-block; transition:transform .12s; }
  .tu-toggle.open .caret { transform:rotate(180deg); }
  .funnel-empty { font-size:12.5px; color:var(--faint); margin:0 0 14px; }
  .trackbar { display:flex; gap:8px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }
  .trackbar input { flex:1; min-width:200px; margin:0; }
  .trackbar select { width:auto; margin:0; }
  .tbtn { width:auto; margin:0; padding:8px 14px; }
  /* Inbox-import result strip: what the run did, plus its undo / enable-alerts actions. */
  .importout { margin:-4px 0 12px; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--surface); font-size:13px; }
  .impmsg { color:var(--fg); }
  .impacts { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px; }
  /* Track view uses the full screen width — no 640px editor cap. */
  .track-editor { max-width:none; }
  .track-editor .editing { max-width:820px; }
  /* width:max-content lets fixed-layout honor each <col>'s width and scroll in .twrap, instead of
     squeezing text columns to nothing while native date inputs hog their min-width. */
  .ttable { width:max-content; table-layout:fixed; border-collapse:collapse; background:var(--surface); border:1px solid var(--line); border-radius:8px; font-size:13px; }
  .ttable th { position:relative; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); padding:8px 8px; border-bottom:1px solid var(--line); white-space:nowrap; overflow:hidden; cursor:grab; }
  .ttable th .lbl { display:block; overflow:hidden; text-overflow:ellipsis; padding-right:6px; }
  /* Drag-to-reorder feedback: the grabbed header dims; the drop target shows a left insertion bar. */
  .ttable th.dragging { opacity:.4; cursor:grabbing; }
  .ttable th.dropto { box-shadow:inset 2px 0 0 0 var(--accent); }
  /* Spreadsheet-style drag-to-resize handle on each column's right edge. */
  .ttable th .rz { position:absolute; top:0; right:0; width:7px; height:100%; cursor:col-resize; user-select:none; }
  .ttable th .rz:hover, .ttable th.rzing .rz { background:var(--accent); opacity:.4; }
  body.rz-drag { cursor:col-resize; user-select:none; }
  /* Show/hide-columns menu */
  .colmenu { position:relative; }
  /* Anchor to the button's right edge (it sits near the viewport's right), so the menu opens inward
     instead of overflowing off-screen and getting clipped. */
  .colmenu .menu { position:absolute; z-index:30; top:calc(100% + 4px); right:0; left:auto; background:var(--surface); border:1px solid var(--line); border-radius:8px; box-shadow:0 6px 24px rgba(0,0,0,.12); padding:8px; min-width:210px; max-height:340px; overflow-y:auto; }
  .colmenu .menu label { display:flex; align-items:center; gap:8px; margin:2px 0; padding:2px; font-size:13px; font-weight:400; text-transform:none; letter-spacing:normal; color:var(--ink); cursor:pointer; }
  .colmenu .menu label:hover { background:var(--bg); border-radius:4px; }
  /* Keep the checkbox its natural size — .trackbar input's flex:1/min-width:200px would otherwise
     stretch it across the row and shove the label text to the far edge. */
  .colmenu .menu input { width:auto; flex:0 0 auto; min-width:0; }
  .colmenu .menu .rst { width:100%; margin:8px 0 0; padding:6px 10px; background:var(--accent-weak); color:var(--accent-text); font-size:12px; }
  .ttable td { padding:3px 4px; border-bottom:1px solid var(--line); vertical-align:middle; overflow:hidden; }
  .ttable tr:last-child td { border-bottom:0; }
  .ttable input, .ttable select { width:100%; border:1px solid transparent; background:transparent; padding:5px 6px; margin:0; border-radius:4px; text-overflow:ellipsis; }
  .ttable input:hover, .ttable select:hover { border-color:var(--line); }
  .ttable input:focus, .ttable select:focus { border-color:var(--accent); background:var(--surface); outline:none; }
  /* Status colours — one muted system shared with the feed's .stbadge (statusMeta in JS):
     neutral=pending, blue=applied, green=positive reply, amber=blocked, red=failed. */
  .ttable .st-applied { color:var(--accent-text); }
  .ttable .st-responded, .ttable .st-interview, .ttable .st-offer { color:var(--ok-text); }
  .ttable .st-blocked { color:var(--warn); }
  .ttable .st-failed { color:var(--bad); }
  .ttable .st-discovered, .ttable .st-tailored, .ttable .st-dryrun,
  .ttable .st-rejected, .ttable .st-noresponse { color:var(--muted); }
  /* Status renders as a badge: the st-* class sets the color, the tint/border derive from it. */
  .ttable select.stcell { font-weight:600; border-radius:99px; padding:4px 10px;
                          border:1px solid transparent; background:color-mix(in srgb, currentColor 14%, transparent); }
  .ttable select.stcell:hover { border-color:currentColor; background:color-mix(in srgb, currentColor 22%, transparent); }
  .ttable select.stcell:focus { border-color:currentColor; background:color-mix(in srgb, currentColor 14%, transparent); }
  /* Date cells: plain text until clicked (no native picker chrome in every row); "—" when empty. */
  .ttable .datebtn { width:100%; margin:0; text-align:left; background:transparent; color:var(--ink);
                     border:1px solid transparent; border-radius:4px; padding:5px 6px; font:inherit;
                     font-variant-numeric:tabular-nums; cursor:pointer; }
  .ttable .datebtn:hover { border-color:var(--line); }
  .ttable .datebtn.empty { color:var(--faint); }
  .ttable .delrow { width:auto; margin:0; padding:4px 8px; background:var(--surface); color:var(--bad); border:1px solid var(--line); font-size:12px; }
  .ttable .rerun { width:auto; margin:0; padding:4px 8px; background:var(--surface); color:var(--accent-text); border:1px solid var(--line); font-size:12px; white-space:nowrap; }
  .ttable .rerun:disabled { opacity:.6; cursor:default; }
  .ttable .rowsaved { color:var(--ok-text); font-size:12px; }
  .ttable .reslink { color:var(--accent-text); text-decoration:none; font-size:12px; white-space:nowrap; padding:5px 6px; display:inline-block; }
  .ttable .reslink:hover { text-decoration:underline; }
  .ttable .muted { color:var(--muted); padding:5px 6px; display:inline-block; }
  /* Source URL: a labelled "Open posting" button, never the raw URL as text (decision 174);
     the ✎ button beside it swaps in the editable input when the value itself is wanted. */
  .ttable .urlcell { display:flex; align-items:center; gap:2px; }
  .ttable .urlcell .urltext { flex:1; min-width:0; }
  /* contain:inline-size keeps a long stored URL out of the table's intrinsic width: without it
     the cell stretches the column far past the width set here and on the resize handle,
     squeezing every other column. */
  .ttable .urllink { flex:1; min-width:0; contain:inline-size; color:var(--accent-text); text-decoration:none;
                     font-size:12px; padding:5px 6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
                     border:1px solid var(--line); border-radius:5px; background:var(--surface); text-align:center; }
  .ttable .urllink:hover { border-color:var(--accent); }
  .ttable .urledit { width:auto; margin:0; flex:none; padding:4px 6px; background:var(--surface);
                     color:var(--muted); border:1px solid var(--line); font-size:12px; }
  .ttable .urledit:hover { color:var(--accent-text); }
  /* Run history: a per-posting run count that expands an inline sub-row (decision 084). */
  .ttable .runsbtn { width:auto; margin:0; padding:4px 7px; background:var(--surface); color:var(--accent-text);
                     border:1px solid var(--line); font-size:12px; white-space:nowrap; }
  .ttable .runsbtn:hover { border-color:var(--accent); }
  .ttable .runsbtn .caret { display:inline-block; transition:transform .12s; margin-left:3px; }
  .ttable .runsbtn.open .caret { transform:rotate(180deg); }
  /* Run history expands as a monospace terminal log on the recessed --field surface:
     dim timestamp, colored outcome tag, muted detail — one log line per run. */
  .ttable tr.runsrow > td { padding:0; background:var(--field); }
  .runsbox { padding:10px 14px; display:flex; flex-direction:column; gap:5px; font-family:var(--mono); }
  .runline { display:flex; align-items:baseline; gap:12px; font-size:11.5px; }
  .runline .runwhen { color:var(--faint); white-space:nowrap; font-variant-numeric:tabular-nums; }
  .runline .runoutcome { font-weight:700; white-space:nowrap; }
  .runline .rundetail { color:var(--muted); flex:1; min-width:0; }
  .runline .reslink { padding:0; }
  /* Token breakdown sub-row (decision 095) — reuses the runsbtn trigger; its own compact table. */
  .ttable tr.tokrow > td { padding:0; background:var(--track); }
  .tokbox { padding:8px 12px; }
  .toktable { border-collapse:collapse; font-size:12px; font-variant-numeric:tabular-nums; }
  .toktable th, .toktable td { padding:3px 14px 3px 0; text-align:left; }
  .toktable th { font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); border-bottom:1px solid var(--line); }
  .toktable .tok-num { text-align:right; }
  .toktable .tok-act { color:var(--text); }
  .toktable .tok-tot { font-weight:600; }
  .toktable .tok-calls { color:var(--muted); }
  .toktable tr.tok-total-row td { border-top:1px solid var(--line); font-weight:600; color:var(--text); }
  /* Discovery/judging aggregate line above the table. */
  .track-usage { margin:0 0 14px; }
  .track-usage.hidden { display:none; }
  .track-usage-line { width:auto; margin:0; padding:6px 10px; background:var(--surface); color:var(--text);
    border:1px solid var(--line); border-radius:8px; font-size:12.5px; font-variant-numeric:tabular-nums; cursor:pointer; text-align:left; }
  .track-usage-line:hover { border-color:var(--accent); filter:none; }
  .track-usage-line .tu-label { color:var(--muted); }
  .track-usage-line .caret { display:inline-block; transition:transform .12s; }
  .track-usage-line.open .caret { transform:rotate(180deg); }
  .tu-detail { display:flex; flex-wrap:wrap; gap:6px 22px; padding:8px 10px 0; font-size:12px; font-variant-numeric:tabular-nums; }
  .tu-detail.hidden { display:none; }
  .tu-act { display:flex; gap:8px; }
  .tu-act .tu-act-name { color:var(--muted); }
  .twrap { overflow-x:auto; }
  .tempty { color:var(--muted); padding:24px; text-align:center; border:1px dashed var(--line); border-radius:8px; }
  /* Consistent waiting indicator: spinner + label (+ elapsed seconds for long waits). */
  @keyframes spin { to { transform:rotate(360deg); } }
  .spin { display:inline-block; width:13px; height:13px; border:2px solid var(--line); border-top-color:var(--accent);
          border-radius:50%; animation:spin .7s linear infinite; vertical-align:-2px; margin-right:7px; }
  .spin.light { border-color:rgba(255,255,255,.45); border-top-color:#fff; }
  button .spin { margin-right:6px; }
  .busy-l { font-weight:600; }
  .busy-s { color:var(--muted); margin-left:8px; font-variant-numeric:tabular-nums; }
  .sec { margin-bottom:18px; }
  .editor h4 { margin:16px 0 6px; font-size:13px; }
  .chkrow { display:flex; align-items:center; gap:8px; margin:8px 0; font-size:13px; font-weight:400; text-transform:none; letter-spacing:normal; color:var(--ink); }
  .chkrow input { width:auto; }
  .lvls { display:flex; flex-wrap:wrap; gap:2px 20px; }
  .lvls .chkrow { margin:4px 0; }
  .brd-row { display:flex; gap:8px; align-items:center; margin:6px 0; }
  .brd-row .bd-ats { width:130px; flex:none; }
  .brd-row .del { width:auto; margin:0; padding:4px 10px; background:var(--surface-2); color:var(--bad); }
  .cards { display:flex; flex-direction:column; gap:10px; }
  .card { position:relative; border:1px solid var(--line); border-radius:10px; padding:12px 12px 10px; background:var(--surface); }
  .card .del { position:absolute; top:8px; right:8px; width:auto; margin:0; padding:1px 8px; background:var(--surface-2); color:var(--bad); font-size:13px; }
  .row2 { display:flex; gap:8px; }
  .row2 > * { flex:1; }
  .fld { margin-bottom:8px; }
  .fld label { margin:0 0 3px; text-transform:none; font-size:11px; }
  .addbtn { width:auto; margin:8px 0 0; padding:6px 12px; background:var(--accent-weak); color:var(--accent-text); }
  .saverow { position:sticky; bottom:0; background:var(--bg); padding:12px 0; display:flex; align-items:center; gap:12px; }
  /* bleed the sticky bar's background down over main's 28px bottom padding so scrolled content can't peek through below it */
  .saverow::after { content:""; position:absolute; left:0; right:0; top:100%; height:28px; background:var(--bg); }
  .saverow button { width:auto; margin:0; }
  /* profile section-jump nav */
  /* top:-28px pulls the stuck bar flush over main's 28px top padding, so the pills get equal space above and below (not 28px of padding on top only) and scrolled content can't peek above it */
  .pnav { position:sticky; top:-28px; z-index:5; display:flex; flex-wrap:wrap; gap:6px; background:var(--bg); padding:12px 0; margin-bottom:4px; border-bottom:1px solid var(--line); }
  .pnav a { font-size:12px; font-weight:600; color:var(--accent-text); background:var(--accent-weak); padding:5px 10px; border-radius:99px; text-decoration:none; }
  .pnav a:hover { background:var(--accent-weak-2); }
  .subhint { color:var(--muted); font-size:12px; margin:0 0 8px; line-height:1.45; }
  /* group heading inside a card (e.g. Location's "Where you live" / "Where you'll work") */
  .grouphead { font-size:12px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
    color:var(--muted); margin:18px 2px 6px; }
  .grouphead:first-child { margin-top:2px; }
  /* collapsible entry cards — collapsed shows a one-line summary; click to edit granularly */
  .card.entry { padding:0; }
  .entry-head { display:flex; align-items:center; gap:8px; padding:10px 12px; cursor:pointer; user-select:none; }
  .entry-head .chev { color:var(--muted); font-size:11px; transition:transform .12s; }
  .card.entry:not(.collapsed) .entry-head .chev { transform:rotate(90deg); }
  .entry-title { flex:1; font-weight:600; font-size:14px; }
  .entry-title.blank { color:var(--muted); font-weight:400; font-style:italic; }
  .entry-head .del { position:static; }
  .entry-body { padding:0 12px 12px; }
  .card.entry.collapsed .entry-body { display:none; }
  /* Screening answers — wider profile editor + a ranked "needs answer" list vs a compact grid. */
  #view-profile .editor { max-width:none; }
  .qa-summary { display:flex; align-items:center; flex-wrap:wrap; gap:10px 16px; margin:2px 0 14px; font-size:13px; }
  .qa-summary .pill { display:inline-flex; align-items:center; gap:6px; font-weight:600; }
  .qa-summary .pill b { font-size:15px; }
  .qa-summary .dot { width:9px; height:9px; border-radius:99px; display:inline-block; }
  .qa-start { width:auto; margin:0; padding:7px 14px; font-weight:600; }
  .qa-start[disabled] { opacity:.45; cursor:default; }
  .qa-grouphead { font-size:12px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
    color:var(--muted); margin:18px 2px 8px; }
  .qa-grouphead:first-child { margin-top:2px; }
  .card.qa-open { border-left:3px solid var(--warn-line); background:var(--surface)df6; padding:12px 14px; }
  .card.qa-open .qa-qrow { display:flex; align-items:flex-start; gap:8px; margin-bottom:8px; }
  .qa-badge { flex:none; font-size:11px; font-weight:700; color:var(--warn); background:var(--warn-chip);
    border-radius:99px; padding:2px 9px; white-space:nowrap; margin-top:1px; }
  .qa-q { flex:1; font-weight:600; font-size:14px; line-height:1.35; }
  .card.qa-open textarea.qa-a { min-height:56px; }
  /* Check-all-that-apply questions: every captured option as a checkbox, not a single-pick dropdown. */
  /* Instruction copy, not a value — always the UI font, even in the Review panel's mono cells. */
  .qa-multihint { font-size:11.5px; color:var(--muted); margin-bottom:5px;
    font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; }
  .qa-multi { display:grid; grid-template-columns:repeat(auto-fill, minmax(190px, 1fr)); gap:2px 12px;
    max-height:210px; overflow-y:auto; border:1px solid var(--line); border-radius:8px;
    padding:8px 10px; background:var(--field); }
  .qa-multi label.qa-opt { display:flex; align-items:center; gap:7px; font-size:13px; line-height:1.3;
    cursor:pointer; margin:0; text-transform:none; letter-spacing:0; font-weight:400; color:var(--ink); }
  .qa-multi label.qa-opt input[type=checkbox] { width:auto; flex:none; margin:0; padding:0; }
  .card.qa-open .del { top:8px; right:8px; }
  /* Answered / auto-handled: compact two-column grid of collapsed cards to use the width. */
  .qa-answered { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  @media (max-width: 780px) { .qa-answered { grid-template-columns:1fr; } }
  .qa-tag { font-size:11px; font-weight:700; border-radius:99px; padding:1px 7px; margin-right:6px; white-space:nowrap; }
  .linkedin { border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:18px; background:var(--surface); }
  .linkedin input[type=file] { width:auto; border:0; padding:0; }
  .linkedin button { width:auto; margin:8px 8px 0 0; padding:7px 14px; }
  .linkedin code { background:var(--surface-2); padding:1px 4px; border-radius:3px; font-size:12px; }
  /* LinkedIn import is the rarer path: a small secondary button that opens an inline panel. */
  .linkedin button.li-alt { background:transparent; color:var(--accent); border:1px solid var(--line);
    font-size:12px; font-weight:600; padding:6px 11px; }
  .linkedin button.li-alt:hover { filter:none; background:var(--surface-2); }
  .li-panel { border:1px solid var(--line); border-radius:8px; padding:12px; margin-top:12px;
    background:var(--surface-2); }
  .li-panel .editing { margin-top:0; }
  /* Kept résumé files (decision 152): one row per uploaded PDF that jobs may be sent as-is. */
  .kept-row { display:flex; align-items:center; justify-content:space-between; gap:10px;
    border:1px solid var(--line); border-radius:8px; padding:7px 11px; margin-top:6px;
    background:var(--surface-2); font-size:12.5px; }
  .kept-row .kept-when { color:var(--muted); }
  .kept-row button { width:auto; margin:0; padding:5px 11px; font-size:12px;
    background:var(--surface); color:var(--accent-text); border:1px solid var(--line); }
  .meta { margin-bottom:16px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:99px; background:var(--accent-weak); color:var(--accent-text); font-size:12px; font-weight:600; }
  #dl-pdf { width:auto; margin:0 0 16px; padding:8px 14px; background:var(--btn-dark); }
  .notes, .warn { font-size:13px; border-radius:8px; padding:10px 12px; margin:10px 0; }
  .notes { background:var(--ok-bg); }
  .warn { background:var(--bad-bg); color:var(--bad); }
  .hidden { display:none; }
  /* resume card */
  .resume { background:var(--surface); max-width:820px; margin:0 auto; padding:34px 42px; border:1px solid var(--line); border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,.05); }
  .resume header { text-align:center; margin-bottom:2px; }
  .resume h1 { margin:0; font-size:27px; }
  .resume .contact { color:var(--muted); font-size:13px; margin-top:2px; }
  /* Clear separation between sections; tight spacing within them (no wasted whitespace). */
  .resume section { margin-top:15px; }
  .resume h2 { font-size:13.5px; text-transform:uppercase; letter-spacing:.05em; border-bottom:1.5px solid var(--ink); padding-bottom:3px; margin:0 0 7px; }
  .resume .entry { margin-bottom:7px; }
  .resume .row { display:flex; justify-content:space-between; gap:16px; line-height:1.35; }
  .resume .row .r, .resume .tech { color:var(--muted); font-size:13px; white-space:nowrap; }
  .resume ul { margin:2px 0 0; padding-left:18px; }
  .resume li { margin:1px 0; font-size:13.5px; line-height:1.34; }
  .resume .skillrow { font-size:13.5px; margin:1px 0; line-height:1.34; }
  .resume p { font-size:13.5px; margin:2px 0; line-height:1.36; }
  .empty { color:var(--muted); text-align:center; margin-top:60px; }
  /* Fit-insights panel (decision 046) */
  .fit-head { font-weight:600; margin-bottom:4px; }
  .fit-line { color:var(--muted); font-size:13px; margin:2px 0; }
  .fit-rec { display:flex; flex-direction:column; align-items:flex-start; gap:8px;
             background:var(--card, var(--surface-2)); border:1px solid var(--line, var(--line));
             border-radius:6px; padding:8px 10px; margin:6px 0; font-size:13px; }
  .fit-rec button { flex:0 0 auto; align-self:flex-start; }
  .fit-trend { margin:2px 0 12px; padding-bottom:10px; border-bottom:1px solid var(--line); }
  .fit-trend svg { display:block; margin:6px 0 2px; overflow:visible; }
  /* Fit trend chart — all colors are tokens so it re-themes live (light/dark). */
  .fc-grid { stroke:var(--line); stroke-width:1; }
  .fc-baseline { stroke:var(--line); stroke-width:1.25; }
  .fc-ylabel { fill:var(--faint); font-size:9px; font-variant-numeric:tabular-nums; }
  .fc-area { fill:var(--accent); opacity:.12; stroke:none; }
  .fc-mean { fill:none; stroke:var(--muted); stroke-width:1.5; stroke-linejoin:round; stroke-linecap:round; }
  .fc-best { fill:none; stroke:var(--accent); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
  .fc-dot { fill:var(--accent); stroke:var(--surface); stroke-width:1.5; }
  .fc-bar { stroke:var(--warn-line); stroke-width:1.5; stroke-dasharray:4 3; }
  .fit-legend { display:flex; flex-wrap:wrap; align-items:center; gap:4px 14px; color:var(--muted); font-size:11px; }
  .fit-legend .lg { display:inline-flex; align-items:center; gap:6px; }
  .fit-legend .sw { width:15px; border-top:2px solid currentColor; }
  .fit-legend .sw.best { color:var(--accent-text); }
  .fit-legend .sw.mean { color:var(--muted); }
  .fit-legend .sw.bar { color:var(--warn-line); border-top-style:dashed; }
  .fit-window-bar { display:flex; justify-content:flex-end; align-items:center; gap:6px;
                    color:var(--muted); font-size:12px; margin-bottom:2px; }
  .fit-window { font-size:12px; padding:1px 4px; }
  .ps-grid { display:flex; align-items:flex-end; gap:12px; height:110px; margin:8px 0 2px; padding:0 2px; }
  .ps-col { display:flex; flex-direction:column; align-items:center; gap:3px; width:52px; }
  .ps-fit { font-size:11.5px; font-weight:700; color:var(--strong); font-variant-numeric:tabular-nums; }
  .ps-bar-wrap { width:26px; height:72px; display:flex; align-items:flex-end;
                 background:var(--track); border-radius:4px; overflow:hidden; }
  .ps-bar { width:100%; background:var(--accent); border-radius:4px 4px 0 0; min-height:3px; transition:height .3s; }
  .ps-band { font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .ps-n { font-size:10.5px; color:var(--faint); }
  /* Review pane: résumé + "why was this tailored this way" side panel */
  .reviewwrap { display:flex; gap:20px; align-items:flex-start; }
  .reviewwrap > #result { flex:1; min-width:0; }
  .why-panel { width:300px; flex:none; position:sticky; top:16px; background:var(--surface); border:1px solid var(--line);
               border-radius:8px; padding:14px 16px; font-size:13px; box-shadow:0 1px 3px rgba(0,0,0,.05); }
  .why-panel.hidden { display:none; }
  .why-panel h3 { margin:0 0 8px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  .why-panel .wtitle { font-weight:700; font-size:14px; margin-bottom:6px; }
  .why-panel .wbody { line-height:1.5; }
  .why-panel .whint { color:var(--muted); line-height:1.5; }
  .resume .entry[data-why] { cursor:pointer; border-radius:6px; box-shadow:inset 3px 0 0 transparent; transition:background .1s, box-shadow .1s; }
  .resume .entry[data-why]:hover { background:var(--accent-tint); box-shadow:inset 3px 0 0 var(--accent); }
  .resume .entry.why-active { background:var(--accent-weak); box-shadow:inset 3px 0 0 var(--accent); }
  @media (max-width: 900px) { .reviewwrap { flex-direction:column; } .why-panel { width:100%; position:static; } }
  /* Discover / test run */
  .testprog { margin-top:14px; padding:14px; border:1px solid var(--line); border-radius:8px; background:var(--surface); }
  .tstep { font-size:13px; color:var(--muted); padding:2px 0; }
  .tstep.act { color:var(--accent-text); font-weight:600; }
  .tstep.done { color:var(--ok-text); }
  .tmsg { margin-top:8px; font-size:14px; color:var(--ink); }
  .tmeta { margin-top:6px; font-size:12px; color:var(--muted); }
  .tmeta.cache { color:var(--ink); }
  .sfunnel { margin-top:10px; font-size:12px; }
  .sfunnel > summary { cursor:pointer; color:var(--muted); user-select:none; }
  .sfunnel > summary:hover { color:var(--ink); }
  .sfunnel > summary b { color:var(--ok-text); }
  .sfbody { margin:8px 0 2px; }
  /* Only two columns (label + main); the drop-reason stacks UNDER the bar inside .sfmain so it
     never steals width from the track — that kept a longer reason from shrinking its own bar. */
  .sfrow { display:grid; grid-template-columns:150px 1fr; gap:10px; align-items:center; padding:3px 0; }
  .sflabel { color:var(--ink); text-align:right; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sfmain { min-width:0; }
  .sftrack { position:relative; background:var(--field); border-radius:4px; height:20px; width:100%; }
  .sfbar { height:100%; border-radius:4px; background:var(--accent); transition:width .3s ease; }
  .sfrow.sfkept .sfbar { background:var(--ok); }
  .sfcount { position:absolute; left:8px; top:0; line-height:20px; font-family:var(--mono);
             font-variant-numeric:tabular-nums; color:var(--strong); font-size:11px; }
  .sfdrop { margin-top:3px; color:var(--bad); font-family:var(--mono); font-size:11px; }
  .sfreason { color:var(--muted); font-family:inherit; margin-left:4px; }
  @media (max-width:520px) { .sfrow { grid-template-columns:1fr; } .sflabel { text-align:left; } }
  .linklike { background:none; border:none; padding:0; margin-left:8px; color:var(--accent-text);
              font:inherit; text-decoration:underline; cursor:pointer; }
  .linklike:disabled { color:var(--muted); text-decoration:none; cursor:default; }
  .tbar { margin-top:8px; height:7px; background:var(--line); border-radius:4px; overflow:hidden; }
  .tbarfill { height:100%; background:var(--accent); transition:width .3s; }
  .testchosen { margin-top:14px; padding:14px; border:1px solid var(--accent); border-radius:8px; background:var(--accent-tint); }
  .tclabel { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .tctitle { font-size:15px; font-weight:600; margin-top:4px; }
  .tcmeta { font-size:12px; color:var(--muted); margin-top:4px; word-break:break-all; }
  .tcwhy { font-size:13px; margin-top:6px; line-height:1.5; }
  .fitpill { font-size:12px; font-weight:600; color:var(--accent-ink); background:var(--accent); border-radius:10px; padding:1px 8px; margin-left:6px; }
  .tfinish { margin-top:14px; padding-top:12px; border-top:1px solid var(--line); font-size:13px; }
  .tfinish button { width:auto; margin-top:8px; }
  .testjudged { margin-top:14px; }
  .tjhead { font-size:13px; color:var(--muted); margin-bottom:8px; line-height:1.5; }
  .tjrow { border:1px solid var(--line); border-left-width:4px; border-radius:6px; padding:9px 11px; margin-bottom:7px; }
  .tjrow.ok { border-left-color:var(--ok); background:var(--ok-tint); }
  .tjrow.no { border-left-color:var(--line); background:var(--neutral-tint); }
  .tjtop { display:flex; align-items:baseline; gap:8px; }
  .tjscore { font-weight:700; font-size:13px; min-width:34px; }
  .tjrow.ok .tjscore { color:var(--ok-text); } .tjrow.no .tjscore { color:var(--warn-strong); }
  .tjname { font-weight:600; font-size:14px; }
  .tjmeta { font-size:12px; color:var(--muted); margin-top:3px; word-break:break-all; }
  .tjwhy { font-size:12.5px; margin-top:4px; line-height:1.45; }
  .tjmiss { font-size:12px; color:var(--warn); margin-top:3px; }
  /* Per-posting actions (decision 174): Apply / Apply anyway + the posting link as a button.
     A posting URL is never printed as text — long ATS URLs wrap into three unreadable lines. */
  .tjacts { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px; }
  /* .linkbtn is the shared shape for "a link the user clicks" anywhere in the app — an external
     resource, a job posting, a console page — so no bare URL is ever printed as body text. */
  .tjbtn, .linkbtn { width:auto; margin:0; padding:5px 11px; font-size:12.5px; font-weight:600;
           border-radius:6px; text-decoration:none; display:inline-block; cursor:pointer;
           border:1px solid var(--line); background:var(--surface); color:var(--accent-text); }
  .tjbtn:hover:not(:disabled), .linkbtn:hover { border-color:var(--accent); }
  .tjbtn:disabled { opacity:.55; cursor:default; }
  .tjapply { background:var(--btn-dark); border-color:var(--btn-dark); color:var(--accent-ink); }
  .tjapply:hover:not(:disabled) { filter:brightness(1.08); }
  /* Below the cutoff: applying is deliberate, so it reads as the amber exception, not the default. */
  .tjanyway { background:var(--warn-bg); border-color:var(--warn-line); color:var(--warn-strong); }
  .tjnote { font-size:12px; color:var(--muted); flex:1 1 100%; line-height:1.45; }
  .tjnote.ok { color:var(--ok-text); }
  .tjnote.err { color:var(--bad); }
  /* "Apply as-is" (decision 180): the same click without the tailoring pass, so it reads as the
     secondary of the pair rather than a second primary action. */
  .tjasis { background:var(--surface); border-color:var(--line); color:var(--ink); font-weight:600; }
  #parked-panel { border-left:4px solid var(--warn-line); padding-left:18px; }
  .pkcard { border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin-bottom:8px; background:var(--surface)df6; }
  .pk-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .pk-title { font-weight:700; font-size:14px; }
  .pk-tag { font-size:11.5px; font-weight:700; color:var(--warn); background:var(--warn-chip); border-radius:10px; padding:2px 9px; }
  .pk-detail { font-size:12.5px; color:var(--muted); margin:6px 0 8px; line-height:1.4; }
  /* Résumé provenance chip (decision 144): green = freshly tailored, amber = reused,
     neutral = sent untailored at the user's request (decision 174). */
  .rsrc { font-size:11px; font-weight:700; border-radius:10px; padding:2px 8px; white-space:nowrap; cursor:default; }
  .rsrc-fresh { color:var(--ok-text); background:var(--ok-bg); }
  .rsrc-reuse { color:var(--warn-strong); background:var(--warn-bg); }
  .rsrc-asis { color:var(--muted); background:var(--neutral-tint); border:1px solid var(--line); }
  .rv-src, .drawer-src { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .drawer-src { margin-top:8px; }
  .pk-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .pk-fix { width:auto; margin:0; }
  .pk-submit { background:#b3261e; border-color:#b3261e; color:#fff; }
  .pk-submit:hover { background:#8f1e18; border-color:#8f1e18; }
  .pk-note { font-size:12.5px; color:var(--muted); }
  /* Auto-apply loop (decision 069) */
  #loop-panel { border-left:4px solid var(--accent); padding-left:18px; }
  #loop-stop { width:auto; margin:0; background:var(--btn-dark); border-color:var(--btn-dark); color:var(--accent-ink); }
  /* Live loop status reads as a monochrome terminal stream (recessed --field + monospace,
     a colored prompt glyph) — the Railway/Apify "live run log" look. */
  .loopstat { margin-top:12px; padding:10px 13px; border:1px solid var(--line); border-radius:8px;
              background:var(--field); font-family:var(--mono); font-size:12.5px; display:flex; align-items:center; gap:9px; line-height:1.4; }
  .loopstat::before { content:"\203a"; color:var(--accent-text); font-weight:700; flex:none; }
  .loopstat.hidden { display:none; }  /* .loopstat sets display:flex; beat it when also .hidden */
  .loopstat.err { border-color:var(--bad); color:var(--bad); }
  .loopstat.err::before { color:var(--bad); }
  .loopstat .lp-count { margin-left:auto; font-weight:700; color:var(--muted); white-space:nowrap; }
  .loop-ready-head { font-weight:700; font-size:13.5px; margin:16px 0 8px; }
  /* Notification feed (decision 145): the durable log of every push, below the live action cards. */
  .nf-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .nf-clear { width:auto; margin:0; font:inherit; font-size:12px; font-weight:500; color:var(--accent-text);
              background:none; border:none; cursor:pointer; padding:2px 4px; }
  .nf-clear:hover { text-decoration:underline; }
  .nflist { display:flex; flex-direction:column; border:1px solid var(--border); border-radius:8px; overflow:hidden; }
  .nfrow { display:flex; align-items:flex-start; gap:10px; padding:10px 12px; border-top:1px solid var(--border); }
  .nfrow:first-child { border-top:none; }
  .nfrow.unread { background:color-mix(in srgb, var(--accent) 8%, transparent); }
  .nf-dot { flex:0 0 auto; width:8px; height:8px; border-radius:50%; margin-top:5px; background:var(--faint); }
  .nf-dot.unread { background:var(--accent); }
  .nf-dot.urgent { background:var(--bad); }
  .nf-main { flex:1 1 auto; min-width:0; }
  .nf-meta { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
  .nf-title { font-weight:600; font-size:13px; }
  .nf-time { font-size:11.5px; color:var(--muted); }
  .nf-tag { font-size:11px; color:var(--muted); border:1px solid var(--border); border-radius:10px; padding:0 6px; text-transform:capitalize; }
  .nf-body { font-size:12.5px; color:var(--muted); margin-top:2px; line-height:1.45; }
  .nf-x { flex:0 0 auto; width:auto; margin:0; font:inherit; font-size:13px; line-height:1; color:var(--faint);
          background:none; border:none; cursor:pointer; padding:2px 4px; border-radius:4px; }
  .nf-x:hover { color:var(--text); background:var(--hover, rgba(127,127,127,.12)); }
  .nf-act { width:auto; margin:6px 0 0; padding:4px 10px; font:inherit; font-size:12px; font-weight:600;
            color:#fff; background:var(--accent); border:none; border-radius:6px; cursor:pointer; }
  .nf-act:hover { filter:brightness(1.06); }
  /* Brief highlight when a feed row jumps to its action card. */
  .nf-flash { animation:nfflash 1.4s ease-out; }
  @keyframes nfflash { 0%,40% { box-shadow:0 0 0 2px var(--accent); } 100% { box-shadow:0 0 0 2px transparent; } }
  .loop-goal { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-top:12px;
               font-size:12.5px; color:var(--muted); text-transform:none; letter-spacing:normal;
               font-weight:400; }
  .loop-goal input[type=number] { width:64px; margin:0; padding:4px 6px; text-align:center;
               font-size:13px; }
  .loop-apply { width:auto; margin:0; background:#b3261e; border-color:#b3261e; color:#fff; }
  .loop-apply:hover { background:#8f1e18; border-color:#8f1e18; }
  .loop-apply:disabled { opacity:.6; }
  /* Review-before-you-apply panel — opens in the #review-modal popup, never inside the card */
  .review-toggle { width:auto; margin:0; background:var(--btn-dark); border-color:var(--btn-dark); color:var(--accent-ink); }
  .rv-sec { margin:10px 0; }
  .rv-h { font-weight:700; font-size:12.5px; margin-bottom:6px; }
  .rv-h.rv-warn { color:var(--bad); margin-top:10px; }
  .rv-metas { display:flex; flex-wrap:wrap; gap:4px 18px; font-size:12.5px; }
  .rv-meta { display:flex; gap:6px; }
  .rv-k { color:var(--muted); min-width:64px; }
  .rv-acts { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .rv-btn { width:auto; margin:0; padding:5px 11px; font-size:12.5px; display:inline-block;
            background:var(--btn-dark); border:1px solid var(--btn-dark); border-radius:6px;
            color:var(--accent-ink); text-decoration:none; cursor:pointer; }
  .rv-btn:hover { filter:brightness(1.08); }
  .rv-note { font-size:12.5px; color:var(--muted); }
  .rv-fields { width:100%; border-collapse:collapse; font-size:12.5px; }
  .rv-fields td { padding:4px 8px; border-top:1px solid var(--line); vertical-align:top; }
  .rv-fl { color:var(--muted); width:38%; word-break:break-word; }
  .rv-fv { font-family:var(--mono); word-break:break-word; }
  /* Editable answers (decision 153): the input IS the value that will be submitted. */
  .rv-edit { width:100%; margin:0; padding:4px 7px; font-size:12.5px; font-family:var(--mono);
             background:var(--field); border:1px solid var(--line); border-radius:5px;
             color:inherit; line-height:1.45; }
  .rv-edit:focus { outline:2px solid var(--accent); outline-offset:1px; }
  .rv-ctl, .rv-edited, .rv-req, .rv-opt, .rv-ai, .rv-flagbadge {
                        display:inline-block; margin-top:3px; font-size:11px;
                        font-weight:700; border-radius:9px; padding:1px 7px; }
  .rv-ctl { color:var(--muted); background:var(--field); border:1px solid var(--line); }
  .rv-edited { color:var(--accent-text); background:var(--accent-weak); border:1px solid var(--accent); }
  /* Required vs optional (decision 164): required is what must be answered to submit. */
  .rv-req { color:var(--warn); background:var(--warn-bg); border:1px solid var(--warn-line); }
  .rv-opt { color:var(--muted); background:transparent; border:1px dashed var(--line); }
  /* Answer doesn't fit its question (decision 166) — the wrong-context fill, called out on the
     row itself with the reason, so the fix is the edit box already next to it. */
  .rv-flagbadge { color:var(--bad); background:var(--warn-bg); border:1px solid var(--bad); }
  .rv-flagged td { background:var(--warn-bg); }
  .rv-flagged td:first-child { box-shadow:inset 3px 0 0 var(--bad); }
  .rv-flagwhy { margin-top:3px; font-size:11.5px; color:var(--bad); font-weight:600; }
  /* The form text a generic label was read from (decision 167). */
  .rv-around { margin-top:3px; font-size:11px; color:var(--muted); font-style:italic;
               overflow-wrap:anywhere; }
  /* How the answer was produced, when a model produced it — the rows worth a human glance. */
  .rv-ai { color:var(--muted); background:var(--field); border:1px dashed var(--line); }
  .rv-save { margin-top:10px; }
  .rv-rescan { margin-top:10px; }
  /* "Type a different value…" escape from a captured dropdown, and the way back to the list. */
  .rv-choice-back { margin-top:5px; font-size:11.5px; padding:3px 8px; }
  .rv-note.rv-ok { color:var(--ok-text); font-weight:700; }
  .rv-note.rv-err { color:var(--bad); font-weight:700; }
  .rv-jd { margin-top:8px; padding:10px; max-height:280px; overflow:auto; background:var(--field);
           border:1px solid var(--line); border-radius:6px; font-size:12px; white-space:pre-wrap;
           font-family:var(--mono); line-height:1.5; }
  .rv-jd.hidden { display:none; }
  .rv-signoff { margin-top:12px; padding-top:10px; border-top:1px solid var(--line);
                display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  /* Collapse-from-the-bottom row (decision 179) — the way out of a long review. */
  .rv-collapse { margin-top:10px; }
  .loop-rescan { display:flex; gap:8px; align-items:flex-start; margin-top:10px; font-size:12.5px;
                 color:var(--muted); line-height:1.4; max-width:560px;
                 text-transform:none; letter-spacing:normal; font-weight:400; }
  .loop-rescan input { width:auto; margin:2px 0 0; flex:0 0 auto; }
  /* Loop settings popup (decision 178): a labelled number + its explanation underneath. */
  .lset-row { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-top:4px; }
  .lset-row label { margin:0; text-transform:none; letter-spacing:normal; font-size:13px;
                    font-weight:600; color:var(--ink); }
  .lset-row input { width:70px; margin:0; padding:4px 6px; text-align:center; }
  .lset-row span { font-size:13px; color:var(--ink); }
  .lset-h { display:block; margin-top:4px; max-width:600px; color:var(--muted); font-size:12px;
            line-height:1.45; }
  /* First-run tour — a spotlight walkthrough that highlights each section and says, in one line,
     what it does (UI Principle #4). The dim backdrop covers the content; the nav rail floats above
     it (aside.nav is a sticky stacking context) so the highlighted tab glows through. */
  .tour-overlay { position:fixed; inset:0; z-index:100; background:rgba(0,0,0,.45); }
  .tour-overlay.hidden { display:none; }
  .tour-on aside.nav { z-index:101; }
  .tab.tour-spot { background:var(--accent-weak); color:var(--accent-text); box-shadow:0 0 0 2px var(--accent); }
  .tour-pop { position:fixed; width:322px; max-width:calc(100vw - 32px); background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); padding:16px 18px; z-index:102; }
  .tour-pop.center { left:50%; top:50%; transform:translate(-50%,-50%); width:392px; }
  .tour-arrow { position:absolute; left:-8px; top:20px; width:15px; height:15px; background:var(--surface); border-left:1px solid var(--line); border-bottom:1px solid var(--line); transform:rotate(45deg); }
  .tour-pop.center .tour-arrow { display:none; }
  .tour-count { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--faint); font-weight:600; }
  .tour-title { font-size:17px; font-weight:700; letter-spacing:-.01em; margin:3px 0 6px; display:flex; align-items:center; gap:8px; }
  .tour-body { font-size:13.5px; color:var(--muted); line-height:1.5; margin:0; }
  .tour-foot { display:flex; align-items:center; gap:8px; margin-top:16px; }
  .tour-foot .grow { flex:1; }
  .tour-foot button { width:auto; margin:0; padding:7px 14px; font-size:13px; }
  .tour-skip { background:transparent; color:var(--muted); border:1px solid var(--line); }
  .tour-back { background:var(--surface-2); color:var(--ink); border:1px solid var(--line); }
  #tour-open { width:100%; margin:0; padding:9px; display:flex; align-items:center; justify-content:center; gap:7px; background:var(--accent-weak); color:var(--accent-text); font-weight:600; font-size:13px; border:1px solid var(--line); border-radius:8px; cursor:pointer; }
  #tour-open:hover { filter:none; border-color:var(--accent); }
  /* First-visit nudges — one dismissible line pointing at where to start in this section (UI Principle #2). */
  .nudge { display:flex; align-items:flex-start; gap:12px; padding:12px 14px; margin:0 0 14px; background:var(--accent-weak); border:1px solid var(--accent); border-radius:12px; }
  .nudge.hidden { display:none; }
  .nudge-b { flex:1; min-width:0; font-size:13px; color:var(--ink); line-height:1.5; }
  .nudge-b b { color:var(--accent-text); }
  .nudge-go { width:auto; margin:8px 0 0; padding:6px 13px; font-size:13px; }
  .nudge-x { flex:0 0 auto; width:auto; margin:0; padding:2px 9px; background:transparent; color:var(--muted); border:1px solid var(--line); border-radius:8px; font-size:13px; line-height:1.4; }
  .nudge-x:hover { color:var(--ink); border-color:var(--muted); filter:none; }
  .flash-target { animation:flashpulse 1.5s ease-out 1; border-radius:10px; }
  @keyframes flashpulse { 0%,100% { box-shadow:0 0 0 0 rgba(0,0,0,0); } 25%,55% { box-shadow:0 0 0 3px var(--accent); } }
  /* ── Dashboard refinements (shadcn / infra-console language) ─────────────────────────────
     Toggle switches for on/off options, monospace for metrics/data, and a monochrome
     terminal-style stream for live run logs. */
  /* Toggle switch — an on/off option reads as a switch. The control stays a real
     <input type=checkbox> (JS still reads .checked); only its appearance changes. */
  .loop-rescan input[type=checkbox] { appearance:none; -webkit-appearance:none; position:relative;
    flex:0 0 auto; width:34px; height:19px; margin:0; border-radius:99px; background:var(--surface-2);
    border:1px solid var(--line); cursor:pointer; transition:background .15s, border-color .15s; }
  .loop-rescan input[type=checkbox]::after { content:""; position:absolute; top:1px; left:1px;
    width:15px; height:15px; border-radius:50%; background:var(--strong); transition:transform .15s; }
  .loop-rescan input[type=checkbox]:checked { background:var(--accent); border-color:var(--accent); }
  .loop-rescan input[type=checkbox]:checked::after { transform:translateX(15px); background:#fff; }
  .loop-rescan input[type=checkbox]:focus-visible { outline:2px solid var(--accent-text); outline-offset:2px; }
  /* Metrics read as data — tabular monospace for counts, funnel figures, and the fit chart. */
  .tcounts .pill .n { font-family:var(--mono); font-weight:700; }
  .ps-fit, .ps-band, .ps-n { font-family:var(--mono); }
  /* Live run log — the dry-run progress panel is a recessed, monospace terminal stream. */
  .testprog { background:var(--field); font-family:var(--mono); }
  .testprog .tstep, .testprog .tmeta { font-size:12px; }
  .loopstat .lp-count { font-family:var(--mono); }
  /* ── Track: scorecards · status system · card feed · context drawer · terminal ──────── */
  /* Hero scorecards — 3–4 at the top; identical border, big dark value, muted label. */
  .scorecards { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; margin:6px 0 18px; }
  .scorecard { border:1px solid var(--line); border-radius:10px; padding:14px 16px; background:var(--surface); }
  .scorecard .sc-val { font-size:30px; font-weight:800; letter-spacing:-.02em; color:var(--strong); line-height:1.05; }
  .scorecard .sc-label { font-size:12px; color:var(--muted); margin-top:4px; }
  .scorecard.info .sc-val { color:var(--accent-text); }
  .scorecard.warn2 .sc-val { color:var(--warn); }
  .scorecard.bad2 .sc-val { color:var(--bad); }
  /* Unified status badge — muted dot + label; the dot pulses only for a live/active state. */
  .stbadge { display:inline-flex; align-items:center; gap:6px; padding:3px 9px; border-radius:99px; font-size:11.5px;
    font-weight:600; white-space:nowrap; border:1px solid color-mix(in srgb, currentColor 28%, transparent);
    background:color-mix(in srgb, currentColor 12%, transparent); }
  .stbadge .dot { width:7px; height:7px; border-radius:99px; background:currentColor; flex:none; }
  .st-neutral { color:var(--muted); } .st-info { color:var(--accent-text); } .st-good { color:var(--ok-text); }
  .st-warn2 { color:var(--warn); } .st-bad2 { color:var(--bad); }
  .stbadge.live .dot { animation:stpulse 1.6s ease-in-out infinite; }
  @keyframes stpulse { 0%,100% { box-shadow:0 0 0 0 color-mix(in srgb, currentColor 60%, transparent); } 65% { box-shadow:0 0 0 5px transparent; } }
  /* Segmented view toggle (Feed | Table). */
  .viewtog { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .viewtog button { width:auto; margin:0; padding:6px 13px; background:var(--surface); color:var(--muted); font-size:12.5px; font-weight:600; border:0; border-radius:0; }
  .viewtog button.on { background:var(--surface-2); color:var(--ink); }
  .viewtog button + button { border-left:1px solid var(--line); }
  .viewtog button[disabled] { opacity:.45; cursor:not-allowed; }
  /* Dry-run options: one labelled segmented control per choice (job source · how far to go) */
  .dryopts { display:flex; flex-wrap:wrap; gap:18px; margin:2px 0 10px; }
  .dryopt { display:flex; align-items:center; gap:8px; }
  .dryopt-l { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  #dry-paste { margin-bottom:10px; }
  /* Application feed — uniform vertical cards (company/role/site metadata + status dot). */
  .feed { display:flex; flex-direction:column; gap:8px; }
  .feed.hidden { display:none; }
  .fcard { display:flex; align-items:center; gap:14px; padding:12px 15px; border:1px solid var(--line); border-radius:10px;
    background:var(--surface); cursor:pointer; transition:border-color .12s; text-align:left; width:100%; margin:0; }
  .fcard:hover { border-color:var(--muted); }
  .fcard.sel { border-color:var(--accent); box-shadow:inset 2px 0 0 var(--accent); }
  .fcard .fc-main { flex:1; min-width:0; }
  .fcard .fc-title { font-size:14px; font-weight:600; color:var(--strong); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .fcard .fc-meta { font-size:12px; color:var(--muted); margin-top:3px; font-family:var(--mono); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  /* Why a blocked application is parked — a short "what blocked" line under the metadata. */
  .fcard .fc-blocker { font-size:12px; color:var(--warn); margin-top:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .fcard .fc-blocker::before { content:"⚠ "; }
  .fcard .fc-fit { font-size:12px; font-weight:700; color:var(--muted); font-family:var(--mono); flex:none; }
  /* Metadata string — tight, borderless, •-separated (e.g. greenhouse • dry-run • 2 runs). */
  .metaline { color:var(--muted); font-size:12px; }
  .sep { opacity:.45; margin:0 6px; }
  /* Context drawer — slides in from the right when a feed card is clicked. */
  .drawer-scrim { position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:110; opacity:0; transition:opacity .18s; }
  .drawer-scrim.open { opacity:1; }
  .drawer { position:fixed; top:0; right:0; height:100vh; width:460px; max-width:92vw; background:var(--surface);
    border-left:1px solid var(--line); z-index:111; display:flex; flex-direction:column; transform:translateX(100%);
    transition:transform .2s ease-out; box-shadow:-10px 0 34px -14px rgba(0,0,0,.5); }
  .drawer.open { transform:translateX(0); }
  .drawer-head { display:flex; align-items:flex-start; gap:10px; padding:16px 18px; border-bottom:1px solid var(--line); }
  .drawer-head .dh-main { flex:1; min-width:0; }
  .drawer-title { font-size:15px; font-weight:700; color:var(--strong); line-height:1.3; }
  .drawer-x { width:auto; margin:0; padding:4px 10px; background:var(--surface-2); color:var(--muted); border:1px solid var(--line); font-size:15px; line-height:1; }
  .drawer-x:hover { color:var(--ink); border-color:var(--muted); filter:none; }
  .drawer-body { padding:16px 18px; overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:16px; }
  .drawer-sec-label { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:600; margin-bottom:8px; }
  .drawer-actions { display:flex; flex-wrap:wrap; gap:8px; }
  /* One shared secondary-button look for every drawer action, whether it's a <button> (Re-run,
     Retailor, Save to fixtures) or an <a> (Open posting, View résumé) — otherwise buttons fall
     through to the solid-accent base style and links render as bare text (inconsistent row). */
  .drawer-actions button, .drawer-actions a { width:auto; margin:0; padding:7px 13px;
    display:inline-flex; align-items:center; gap:6px; background:var(--surface);
    color:var(--accent-text); border:1px solid var(--line); border-radius:8px; font-size:12.5px;
    font-weight:600; white-space:nowrap; text-decoration:none; cursor:pointer; transition:filter .12s; }
  .drawer-actions button:hover, .drawer-actions a:hover { filter:brightness(1.06); }
  .drawer-actions button:disabled { opacity:.6; cursor:default; filter:none; }
  /* Active terminal window — a code block for run logs (dim lines, colored levels). */
  .terminal { background:var(--field); border:1px solid var(--line); border-radius:8px; font-family:var(--mono);
    font-size:11.5px; line-height:1.55; padding:11px 13px; overflow:auto; max-height:300px; }
  .terminal .tl { color:var(--muted); white-space:pre-wrap; word-break:break-word; margin:2px 0; }
  .terminal .tl .tl-when { color:var(--faint); }
  .terminal .tl .tl-prompt { color:var(--accent-text); }
  .terminal .tl.warn { color:var(--warn); } .terminal .tl.err { color:var(--bad); } .terminal .tl.ok { color:var(--ok-text); }
  /* Collapsible pipeline/spend details, to keep the top uncluttered. */
  .trk-details { border:0; margin:0 0 16px; }
  .trk-details > summary { cursor:pointer; font-size:12.5px; font-weight:600; color:var(--accent-text); list-style:none; padding:2px 0; display:inline-flex; align-items:center; gap:6px; }
  .trk-details > summary::-webkit-details-marker { display:none; }
  .trk-details > summary .caret { transition:transform .12s; }
  .trk-details[open] > summary .caret { transform:rotate(180deg); }
  .trk-details-body { padding-top:12px; }
  /* ── Discover: aligned panel headers · concise toggles · 2-col info · settings modal ── */
  /* Panel header: title on the left, its primary action button lined up on the right. */
  .panel-head { display:flex; align-items:center; gap:14px; margin:0 0 8px; flex-wrap:wrap; }
  .panel-head h3 { margin:0; flex:1; min-width:0; font-size:15px; }
  .panel-head button { width:auto; margin:0; padding:9px 16px; white-space:nowrap; }
  .editing.tight { margin:0 0 12px; max-width:74ch; }
  /* Concise toggle rows — switch + short bold label + one muted hint line. */
  .loop-rescan { max-width:none; margin-top:12px; }
  .loop-rescan .rl-main { display:flex; flex-direction:column; gap:1px; min-width:0; }
  .loop-rescan .rl-t { font-weight:600; color:var(--ink); font-size:13px; }
  .loop-rescan .rl-h { color:var(--muted); font-size:12px; line-height:1.4; }
  /* Read-only info panels stack full page-width. */
  .disc-grid { display:flex; flex-direction:column; gap:16px; }
  .disc-grid .editor { margin:0; }
  .disc-actions { display:flex; justify-content:flex-end; margin:0 0 16px; }
  .disc-actions button { width:auto; margin:0; padding:9px 15px; display:inline-flex; align-items:center; gap:7px; }
  /* Centered modal (Discovery settings). */
  .modal-scrim { position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:112; display:flex; align-items:flex-start; justify-content:center; padding:44px 20px; overflow:auto; }
  .modal-scrim.hidden { display:none; }
  /* Flex column with a capped height: the head and the save footer stay put while only
     the body scrolls, so "Save settings" is always pinned within reach (no scroll-to-bottom). */
  .modal { display:flex; flex-direction:column; max-height:calc(100vh - 88px); background:var(--surface); border:1px solid var(--line); border-radius:12px; width:740px; max-width:100%; box-shadow:var(--shadow); }
  .modal-head { flex:0 0 auto; display:flex; align-items:center; gap:10px; padding:15px 20px; border-bottom:1px solid var(--line); background:var(--surface); border-radius:12px 12px 0 0; }
  .modal-head h3 { margin:0; flex:1; font-size:16px; }
  .modal-x { width:auto; margin:0; padding:4px 10px; background:var(--surface-2); color:var(--muted); border:1px solid var(--line); font-size:15px; line-height:1; }
  .modal-x:hover { color:var(--ink); border-color:var(--muted); filter:none; }
  .modal-body { flex:1 1 auto; overflow-y:auto; padding:18px 20px; }
  .modal-foot { flex:0 0 auto; display:flex; align-items:center; gap:12px; padding:14px 20px; border-top:1px solid var(--line); background:var(--surface); border-radius:0 0 12px 12px; }
  .modal-foot button { width:auto; margin:0; }
  /* The review popup carries a full answer table, so it gets more room than a settings modal. */
  .modal-wide { width:1020px; }
  /* Per-aggregator test results (Discovery settings): one ✓/✗ row per probed source. */
  .agg-test-out { margin-top:8px; display:flex; flex-direction:column; gap:5px; }
  .agg-res { font-size:12.5px; line-height:1.4; display:flex; align-items:baseline; gap:7px; }
  .agg-dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; align-self:center; }
  .agg-dot.ok { background:var(--ok); } .agg-dot.err { background:var(--bad); }
  /* Accessibility: a clearly visible keyboard-focus ring on every interactive control
     (mouse clicks don't trigger :focus-visible, so this never shows on click). */
  a:focus-visible, button:focus-visible, .tab:focus-visible, [tabindex]:focus-visible,
  summary:focus-visible { outline:2px solid var(--accent-text); outline-offset:2px; border-radius:8px; }
  /* Respect users who ask for less motion: drop the decorative flash + smooth scroll, but keep
     functional loading spinners so "working…" never looks frozen. */
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior:auto; }
    .flash-target { animation:none !important; }
  }
</style>
</head>
<body>
<div id="tour-overlay" class="tour-overlay hidden"></div>
<div id="tour-pop" class="tour-pop hidden" role="dialog" aria-modal="true" aria-labelledby="tour-title" aria-describedby="tour-body">
  <div class="tour-arrow"></div>
  <div id="tour-count" class="tour-count"></div>
  <div id="tour-title" class="tour-title"></div>
  <p id="tour-body" class="tour-body"></p>
  <div class="tour-foot">
    <button id="tour-skip" class="tour-skip" type="button">Skip tour</button>
    <span class="grow"></span>
    <button id="tour-back" class="tour-back hidden" type="button">Back</button>
    <button id="tour-next" type="button">Next →</button>
  </div>
</div>
<div class="app">
  <aside class="nav">
    <div class="brand"><span class="brand-logo" aria-hidden="true"></span>ApplicationBot</div>
    <nav class="navlist">
      <button class="tab active" data-view="discover"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>Discover</button>
      <button class="tab" data-view="profile"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/></svg>Profile</button>
      <button class="tab" data-view="track"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>Track</button>
      <button class="tab" data-view="notifications"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>Notifications<span class="nav-badge" id="notif-badge" hidden></span></button>
      <button class="tab" data-view="settings"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/></svg>Settings</button>
    </nav>
    <div class="nav-foot">
      <button id="tour-open" type="button"><svg class="btn-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/></svg>Take the tour</button>
      <button id="account" class="account" type="button" title="Manage Claude connection">Checking Claude sign-in…</button>
      <button id="theme-toggle" type="button" aria-label="Toggle dark mode"></button>
    </div>
  </aside>

  <main>
    <div class="brandmark" aria-hidden="true"></div>
    <div id="view-discover">
      <div id="discover-nudge" class="nudge hidden">
        <div class="nudge-b">First, tell the bot <b>what jobs to find</b> — set your roles, keywords,
          location, and pay in <b>Discovery settings</b> below. Then run a dry-run and watch it
          search, tailor, and fill one application (it never submits until you arm it).
          <br><button id="discover-nudge-go" class="nudge-go" type="button">Set what jobs to find →</button></div>
        <button id="discover-nudge-x" class="nudge-x" type="button" aria-label="Dismiss">✕</button>
      </div>
      <header class="page-head">
        <h2 class="page-title">Discover &amp; apply</h2>
        <p class="page-sub">Find matching openings, tailor and fill each one, and apply. The
          auto-apply loop submits for real — tick <b>Dry run</b> on it, or use the dry-run panel
          below, to prepare without submitting.</p>
      </header>
      <div class="disc-actions">
        <button id="disc-open" type="button" class="tbtn"><svg class="btn-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>Discovery settings</button>
      </div>
      <div class="editor" id="loop-panel">
        <div class="panel-head">
          <h3>Auto-apply loop</h3>
          <button id="loop-settings-open" type="button" class="tbtn">⚙ Loop settings</button>
          <button id="loop-start" type="button">▶ Start applying</button>
          <button id="loop-stop" type="button" class="hidden">■ Stop loop</button>
        </div>
        <p class="editing tight" id="loop-blurb-live">Finds matches and <b>applies</b> to each one for
          you — tailor · export · fill · submit — one after another, with no click per application.
          Real applications are sent. Stop anytime; to halt every submit instantly, create the file
          <code>profile/KILL</code>.</p>
        <p class="editing tight hidden" id="loop-blurb-dry">Dry run: finds matches and prepares each one
          (tailor · export · fill) in the background — they stack up below as <b>Ready to apply</b> and
          nothing is submitted. Click <b>Apply&nbsp;▶</b> on one to send just that application
          (confirms first). Stop anytime.</p>
        <span id="loop-msg" class="msg"></span>
        <label class="loop-rescan"><input type="checkbox" id="loop-dry-run">
          <span class="rl-main"><span class="rl-t">Dry run — prepare everything, submit nothing</span>
          <span class="rl-h">Every application is filled and held under <b>Ready to apply</b> for you to
            send with one click. Off (the default) means the loop submits each one itself.</span></span></label>
        <div class="loop-goal">
          <label>Goal: stop after
            <input type="number" id="loop-goal" min="1" step="1" placeholder="∞" inputmode="numeric">
            application(s)</label>
          <span class="rl-h" id="loop-goal-hint">Counts applications submitted — or, in a dry run, prepared
            and waiting for you. Leave blank to work through every match the boards return.</span>
        </div>
        <label class="loop-rescan" id="loop-maintain-wrap"><input type="checkbox" id="loop-maintain">
          <span class="rl-main"><span class="rl-t">Keep topping up to the goal</span>
          <span class="rl-h">Dry run only: as you apply to ready ones, keep discovering &amp; preparing so the goal-many stay ready. Off = stop once the goal is reached.</span></span></label>
        <label class="loop-rescan" id="loop-watch-wrap"><input type="checkbox" id="loop-watch">
          <span class="rl-main"><span class="rl-t">Keep watching — re-check the boards on a schedule</span>
          <span class="rl-h">Don't stop when caught up: re-search every
            <input type="number" id="loop-watch-interval" min="1" step="1" value="30" inputmode="numeric"
              style="width:52px;text-align:center;margin:0 3px;padding:2px 4px"> min and apply to each
            newly-posted match as it appears (in a dry run, hold it under <b>Ready to apply</b> for your
            click instead). Best for catching seasonal roles the day they post.</span></span></label>
        <label class="loop-rescan"><input type="checkbox" id="loop-rescan">
          <span class="rl-main"><span class="rl-t">Re-prepare postings I've already seen</span>
          <span class="rl-h">Re-fills every match from the last search, reusing cached fit scores &amp; tailored résumés — no Claude spend when nothing changed.</span></span></label>
        <label class="loop-rescan" id="loop-show-browser-wrap"><input type="checkbox" id="loop-show-browser">
          <span class="rl-main"><span class="rl-t">Show the browser while it applies — watch each submit</span>
          <span class="rl-h" id="loop-show-browser-hint">Every application is filled and submitted in a
            window you can watch, which then closes itself and the loop moves on. Slower per
            application; nothing else about the submit changes. To watch just one, use
            <b>Watch it apply ▶</b> inside that application's Review.</span></span></label>
        <p class="editing tight" style="margin-top:8px">Which résumé each application gets, the fit
          cutoff, and how many applications one run may send are in
          <a href="#" id="loop-settings-link" class="linklike">⚙ Loop settings</a>.</p>
        <p class="editing tight" style="margin-top:8px">Want a ping when a match is ready or an
          application needs you — even with this window in the background?
          <a href="#" id="loop-notify-link" class="linklike">Set up notifications in Settings →</a></p>
        <div id="loop-status" class="loopstat hidden"></div>
        <!-- Ready applications (and their Review panels) sit ABOVE the search breakdown: the
             review is what you act on, so it must not be pushed below a long list of judged
             postings. Collapsed, each one is a slim row on top of that list. -->
        <div id="loop-ready"></div>
        <div id="loop-scan" class="testprog hidden"></div>
        <div id="loop-judged" class="testjudged hidden"></div>
      </div>
      <div class="editor" id="dry-run-panel">
        <div class="panel-head">
          <h3>Run a dry-run</h3>
          <button id="test-run" type="button">▶ Find &amp; fill one (dry-run)</button>
        </div>
        <div class="dryopts">
          <div class="dryopt"><span class="dryopt-l">Job</span>
            <div class="viewtog">
              <button id="dry-job-find" type="button" class="on">Best match it finds</button>
              <button id="dry-job-paste" type="button">A posting I paste</button>
            </div>
          </div>
          <div class="dryopt"><span class="dryopt-l">How far to go</span>
            <div class="viewtog">
              <button id="dry-mode-apply" type="button" class="on">Tailor + fill the form</button>
              <button id="dry-mode-tailor" type="button">Tailor the résumé only</button>
            </div>
          </div>
        </div>
        <p class="editing tight" id="dry-run-blurb"></p>
        <!-- Tailor a posting the search never found (a job someone sent you). Only the tailoring
             half can run here: a pasted posting has no application form to fill. -->
        <div id="dry-paste" class="hidden">
          <div class="controls">
            <div class="ctrl"><label for="jobmode">Posting</label>
              <select id="jobmode">
                <option value="fixture">From a saved posting</option>
                <option value="custom">Paste a posting</option>
              </select>
            </div>
            <div id="fixtureBox" class="ctrl"><label for="fixture">Saved posting</label><select id="fixture"></select></div>
            <div id="customBox" class="ctrl wide hidden">
              <label for="title">Posting details</label>
              <input id="title" placeholder="Job title (optional)">
              <input id="company" placeholder="Company (optional)" style="margin-top:6px">
              <textarea id="body" placeholder="Paste the job description here…"></textarea>
            </div>
            <div class="ctrl"><label for="backend">Engine</label>
              <select id="backend">
                <option value="auto">auto (subscription → API key → rules)</option>
                <option value="claude-code">claude-code (your subscription)</option>
                <option value="anthropic-api">anthropic-api (your API key — fallback)</option>
                <option value="rules">rules (no account)</option>
              </select>
            </div>
            <div class="ctrl"><label for="quality">Quality</label>
              <select id="quality">
                <option value="fast">Fast — Sonnet, ~30s</option>
                <option value="balanced" selected>Balanced — Opus, ~40s (recommended)</option>
                <option value="max">Max quality — Opus + deep reasoning, ~2 min</option>
              </select>
            </div>
            <div class="ctrl"><label for="pages">Length</label>
              <select id="pages">
                <option value="1">1 page</option>
                <option value="1.5">1.5 pages</option>
                <option value="2">2 pages</option>
              </select>
            </div>
            <div class="ctrl"><label for="linechars">Line length</label>
              <input id="linechars" type="number" value="100" min="40" max="220">
            </div>
          </div>
        </div>
        <span id="test-msg" class="msg"></span>
        <div id="test-progress" class="testprog hidden"></div>
        <div id="test-chosen" class="testchosen hidden"></div>
        <div id="test-judged" class="testjudged hidden"></div>
        <!-- The tailored résumé itself: rendered preview, drift warnings, and the PDF. Shared by
             both tailor-only paths (a discovered match and a pasted posting). -->
        <div id="tailor-out" class="hidden">
          <div id="meta" class="meta hidden"></div>
          <button id="dl-pdf" class="hidden">⬇ Download PDF</button>
          <span id="pdf-msg" class="msg"></span>
          <div class="reviewwrap">
            <div id="result"></div>
            <aside id="why-panel" class="why-panel hidden"></aside>
          </div>
        </div>
      </div>
      <div class="editor" id="parked-panel" style="display:none">
        <h3 style="margin-top:0">Applications waiting on you</h3>
        <p class="editing tight">Filled but couldn't finish on their own — each needs one thing from
          you. Click to go straight to the fix.</p>
        <div id="parked-body"></div>
      </div>
      <div class="disc-grid">
        <div class="editor" id="sources-overview">
          <h3 style="margin-top:0">Where your postings come from</h3>
          <p class="editing tight">Every source feeding discovery — broad aggregators, early-career
            feeds, target companies by ATS, and the aggregator→ATS bridge.</p>
          <div id="sources-body">Loading…</div>
        </div>
        <div class="editor" id="fit-insights" style="display:none">
          <h3 style="margin-top:0">What past runs taught the search</h3>
          <p class="editing tight">Every posting Claude judges is remembered, so runs steer scarce
            judge slots toward what scored highest for you. Below: what it learned and recommends.</p>
          <div id="fit-insights-body">Loading…</div>
        </div>
        <div class="editor" id="new-sources" style="display:none">
          <h3 style="margin-top:0">New sources found</h3>
          <p class="editing tight">Companies a source-scout run found on an ATS you already
            support and verified are live. Add one and the next discovery run searches it.</p>
          <div id="candidates-body">Loading…</div>
        </div>
      </div>
    </div>

    <div id="view-profile" class="hidden">
      <div class="editor">
        <div id="profile-nudge" class="nudge hidden">
          <div class="nudge-b"><b>Start here.</b> Import your résumé and it fills these sections in
            for you — then review and add anything missing. No résumé handy? Fill the fields directly.
            <br><button id="profile-nudge-go" class="nudge-go" type="button">Import my résumé →</button></div>
          <button id="profile-nudge-x" class="nudge-x" type="button" aria-label="Dismiss">✕</button>
        </div>
        <header class="page-head">
          <h2 class="page-title">Your details &amp; résumé</h2>
          <p class="page-sub">Everything about you, in one place — edit any section granularly
            (click an entry to expand). Applicant details save to
            <code>profile/application_profile.yaml</code>; experience, projects, education, and
            skills save to your résumé <b id="editing-path"></b>. Both are git-ignored; tailoring
            picks the relevant parts per job.</p>
        </header>

        <!-- Picks the profile the whole app works from: the sections below edit it, imports merge
             into it, and discovery/tailoring/applying all read it. Lived on the old Review tab.
             The hint is exact on purpose — switching it does NOT switch your applicant details,
             which are one shared file (profile/application_profile.yaml). -->
        <div class="controls" id="resume-pick">
          <div class="ctrl"><label for="resume">Profile you're working on</label><select id="resume"></select>
            <span class="rl-h">The résumé this page edits, imports merge into, and every application
              is tailored from. Your applicant details below are shared by all of them.</span>
          </div>
        </div>

        <div id="s-upload" class="linkedin">
          <h3 style="margin-top:0">Start here — upload your résumé</h3>
          <p class="editing">Have a résumé already? Upload the <b>PDF or Word (.docx)</b> file and
            Claude reads it into the sections below — experience, projects, education, and skills.
            New entries are merged in; anything you've already filled is left untouched. No résumé
            file? Fill the fields below directly, or import from LinkedIn.</p>
          <input id="rf-file" type="file" accept=".pdf,.docx,.txt,.md">
          <button id="rf-import" type="button">Upload &amp; parse</button>
          <button id="li-toggle" type="button" class="li-alt"
                  aria-expanded="false" aria-controls="s-linkedin">Import from LinkedIn instead</button>
          <span id="rf-msg" class="msg"></span>

          <div id="s-linkedin" class="li-panel hidden">
            <h4 style="margin:0 0 6px">Import from LinkedIn</h4>
            <p class="editing">LinkedIn can't be linked live (their API restricts it and
              scraping breaks their terms). Instead, on LinkedIn go to <b>Settings → Data
              Privacy → Get a copy of your data</b>, download the archive, and upload it here
              (the <code>.zip</code>, or the Positions/Education/Skills <code>.csv</code>
              files). We'll merge new experience, education, and skills into the sections below
              (existing entries aren't touched).</p>
            <input id="li-file" type="file" accept=".zip,.csv">
            <button id="li-import" type="button">Import</button>
            <span id="li-msg" class="msg"></span>
          </div>

          <p class="editing" style="margin-bottom:0">A <b>PDF</b> you upload is also kept as a file:
            when a job asks for essentially only skills that résumé already shows, we send it
            as-is instead of a tailored one — your real résumé beats a generated one. Word and text
            uploads are parsed but not kept (an application form needs a PDF).</p>
          <div id="rf-kept"></div>
        </div>

        <div id="profile-form">Loading…</div>

        <div class="saverow">
          <button id="save-profile">Save profile</button>
          <span id="profile-msg" class="msg"></span>
        </div>

        <!-- Export the portable setup (decision 188). The zip mirrors profile/, so the restore
             instruction below is literally true — no import step exists yet, and promising one
             the app can't do would be worse than saying "unzip it here". -->
        <div id="s-export" class="linkedin">
          <h3 style="margin-top:0">Back up this profile / move it to another computer</h3>
          <p class="editing">Downloads one <code>.zip</code> with everything you set up on this
            page — applicant details and saved screening answers, your search filters, every
            résumé listed above, and the résumé PDFs kept for sending as-is. To restore it,
            unzip the file into <code>profile/</code> on the other machine and restart the app.</p>
          <button id="export-profile" type="button">⬇ Download profile (.zip)</button>
          <span id="export-msg" class="msg"></span>
          <p class="editing" style="margin-bottom:0"><b>Left out on purpose:</b> your linked
            inbox and its credentials, the arming switch and submission cap, notification
            settings, your application history, and caches — machine-specific things you set
            once on the new computer.</p>
        </div>
      </div>
    </div>

    <div id="view-track" class="hidden">
      <div class="editor track-editor">
        <header class="page-head">
          <h2 class="page-title">Application tracker</h2>
        </header>
        <div id="track-scores" class="scorecards"></div>
        <div id="track-counts" class="tcounts"></div>
        <details class="trk-details">
          <summary>Pipeline funnel &amp; spend <span class="caret">▾</span></summary>
          <div class="trk-details-body">
            <div id="track-funnel" class="funnel"></div>
            <div id="track-usage" class="track-usage hidden"></div>
          </div>
        </details>
        <div class="trackbar">
          <input id="track-search" type="text" placeholder="Search company, role, location, notes…">
          <div class="viewtog" role="tablist" aria-label="View">
            <button id="view-feed" type="button" class="on">Feed</button>
            <button id="view-table" type="button">Table</button>
          </div>
          <div class="colmenu" id="colmenu-wrap">
            <button id="track-cols-btn" type="button" class="tbtn">Columns ▾</button>
            <div id="track-cols-menu" class="menu hidden"></div>
          </div>
          <button id="track-add" type="button" class="tbtn">+ Add application</button>
          <button id="track-import" type="button" class="tbtn"
                  title="Read the linked inbox and add every application you were emailed about">Import from inbox</button>
          <span id="track-msg" class="msg"></span>
        </div>
        <div id="track-import-out" class="importout hidden"></div>
        <div id="track-feed" class="feed"></div>
        <div id="track-body" class="hidden">Loading…</div>
      </div>
    </div>

    <div id="view-notifications" class="hidden">
      <header class="page-head">
        <h2 class="page-title">Notifications</h2>
        <p class="page-sub">Everything that needs you, in one place — applications the loop
          prepared and is holding for your approval, and any it paused because a step needs you.
          Act on them right here. The list updates itself as you submit and resolve.</p>
      </header>
      <div class="editor" id="notif-center">
        <div id="notif-body">Loading…</div>
      </div>
    </div>

    <div id="view-settings" class="hidden">
      <header class="page-head">
        <h2 class="page-title">Settings</h2>
        <p class="page-sub">Set-once configuration — how Claude tailors your résumé, how you're
          notified, your linked inbox, and appearance. The auto-apply loop and discovery filters
          live in Discover.</p>
      </header>

      <div class="editor" id="set-claude">
        <h3 style="margin-top:0">Claude connection</h3>
        <p class="editing tight" id="claude-active"></p>
        <div class="conn-sec" id="claude-sub"></div>
        <div class="conn-sec" id="claude-key"></div>
      </div>

      <div class="editor" id="set-notify">
        <h3 style="margin-top:0">Notifications</h3>
        <p class="editing tight">The loop prepares each match then waits for your OK before it
          submits, and pauses any application that needs your input. Get pinged for those the
          moment they happen — even with this window in the background.</p>
        <label class="loop-rescan"><input type="checkbox" id="ntf-desktop">
          <span class="rl-main"><span class="rl-t">Desktop notification on this Mac</span>
          <span class="rl-h">A native macOS notification — works for both the app and the browser (the loop runs on this Mac).</span></span></label>
        <div class="subhint" id="ntf-desktop-hint" hidden style="margin:2px 0 6px 34px">
          Clicking one won't open the app yet — the browser build can't deep-link on its own. To
          make desktop notifications clickable, install <b>terminal-notifier</b>:
          <code id="ntf-tn-cmd">brew install terminal-notifier</code>
          <button type="button" class="linklike" id="ntf-tn-copy" style="width:auto;margin:0 0 0 8px">Copy</button></div>
        <label class="loop-rescan"><input type="checkbox" id="ntf-ntfy">
          <span class="rl-main"><span class="rl-t">Push to my phone (ntfy)</span>
          <span class="rl-h">Install the free <b>ntfy</b> app, subscribe to a topic you choose below, and your phone buzzes even away from the Mac.</span></span></label>
        <div class="fld" id="ntf-topic-wrap" style="margin:2px 0 6px 34px">
          <label>ntfy topic</label>
          <input type="text" id="ntf-topic" placeholder="e.g. applicationbot-8f3k2q (pick something unguessable)">
          <div class="subhint">Anyone who knows the topic can read your alerts, so make it long &amp; random. Subscribe to the same topic in the ntfy phone app.</div>
        </div>
        <div class="ntf-events" style="margin-left:34px">
          <label class="loop-rescan"><input type="checkbox" id="ntf-ev-approval">
            <span class="rl-main"><span class="rl-t">When one is ready for my approval</span></span></label>
          <label class="loop-rescan"><input type="checkbox" id="ntf-ev-intervention">
            <span class="rl-main"><span class="rl-t">When one is blocked and needs my input</span></span></label>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
          <button id="ntf-save" type="button" class="addbtn">Save</button>
          <button id="ntf-test" type="button" class="addbtn">Send test</button>
          <span id="ntf-msg" class="subhint"></span>
        </div>
      </div>

      <div class="editor" id="set-mailbox"><div id="set-mailbox-mount">Loading…</div></div>

      <div class="editor" id="set-appearance">
        <h3 style="margin-top:0">Appearance</h3>
        <p class="editing tight">Theme for this app. Follows your system setting by default.</p>
        <div class="viewtog" id="theme-seg" role="group" aria-label="Theme">
          <button type="button" data-mode="system">System</button>
          <button type="button" data-mode="light">Light</button>
          <button type="button" data-mode="dark">Dark</button>
        </div>
      </div>
    </div>
  </main>
</div>
<div id="drawer-scrim" class="drawer-scrim hidden"></div>
<aside id="track-drawer" class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" aria-hidden="true">
  <div class="drawer-head">
    <div class="dh-main">
      <div id="drawer-title" class="drawer-title">—</div>
      <div id="drawer-meta" class="metaline" style="margin-top:6px"></div>
    </div>
    <button id="drawer-x" class="drawer-x" type="button" aria-label="Close">✕</button>
  </div>
  <div id="drawer-body" class="drawer-body"></div>
</aside>
<!-- Loop settings (decision 178): everything that governs a run but isn't a per-run choice, set
     before you start it. Saved to profile/discovery.yaml + profile/safety.yaml. -->
<div id="loop-modal" class="modal-scrim hidden" role="dialog" aria-modal="true" aria-labelledby="loop-modal-title">
  <div class="modal">
    <div class="modal-head">
      <h3 id="loop-modal-title">Loop settings</h3>
      <button id="loop-modal-x" class="modal-x" type="button" aria-label="Close">✕</button>
    </div>
    <div class="modal-body">
      <p class="editing tight">These govern every application the auto-apply loop prepares. A run
        uses the values saved when it started — editing them won't change a run already going.</p>

      <div class="sec">
        <h4>Résumé for each application</h4>
        <label class="loop-rescan"><input type="radio" name="lset-tailor" value="smart">
          <span class="rl-main"><span class="rl-t">Tailor with Claude, reusing when it can</span>
          <span class="rl-h">Tailors each résumé to the posting, but reuses an earlier tailored one when the next posting demands the same skills — no second Claude call for a near-identical job. The default.</span></span></label>
        <label class="loop-rescan"><input type="radio" name="lset-tailor" value="always">
          <span class="rl-main"><span class="rl-t">Always re-tailor from scratch</span>
          <span class="rl-h">Regenerate every résumé with Claude even when nothing changed — spends Claude usage on every posting.</span></span></label>
        <label class="loop-rescan"><input type="radio" name="lset-tailor" value="under">
          <span class="rl-main"><span class="rl-t">Only tailor when the fit is under
            <input type="number" id="lset-below" min="0" max="100" step="1"
              style="width:56px;text-align:center;margin:0 3px;padding:2px 4px" inputmode="numeric">/100</span>
          <span class="rl-h">Spends Claude only where it helps: a posting your résumé already fits is sent as-is; a weaker match is tailored to close the gap. A posting Claude couldn't score is tailored.</span></span></label>
        <label class="loop-rescan"><input type="radio" name="lset-tailor" value="never">
          <span class="rl-main"><span class="rl-t">Never tailor — send my résumé as-is</span>
          <span class="rl-h">No Claude call at all: your uploaded résumé if you have one, otherwise your base résumé rendered as it stands.</span></span></label>
      </div>

      <div class="sec">
        <h4>Which postings the loop applies to</h4>
        <div class="lset-row"><label for="lset-minfit">Minimum fit</label>
          <input type="number" id="lset-minfit" min="0" max="100" step="1" inputmode="numeric"><span>/100</span></div>
        <span class="lset-h" id="lset-minfit-hint">Claude scores every posting 0-100; the loop only
          prepares ones at or above this. The same setting as <b>min_fit</b> in Discovery settings —
          saving here saves there.</span>
      </div>

      <div class="sec">
        <h4>Résumé reuse</h4>
        <div class="lset-row"><label for="lset-reuse">Reuse an earlier tailored résumé when the two postings' skills overlap at least</label>
          <input type="number" id="lset-reuse" min="0" max="100" step="5" inputmode="numeric"><span>%</span></div>
        <span class="lset-h">How similar two postings' demanded skills must be before the loop sends
          the résumé it already tailored instead of calling Claude again. Higher = stricter: fewer
          reuses, more Claude usage. <b>0% never reuses.</b></span>
      </div>

      <div class="sec">
        <h4>Submission cap</h4>
        <div class="lset-row"><label for="lset-cap">Stop after</label>
          <input type="number" id="lset-cap" min="1" step="1" inputmode="numeric">
          <span>submitted application(s) in one run</span></div>
        <span class="lset-h">A ceiling on how many applications one run may send, whatever the goal
          says. The loop stops when it's reached and says so; anything already prepared waits under
          <b>Ready to apply</b>. Stored in <code>profile/safety.yaml</code>, so the command-line
          runner honours it too. It has no effect on a dry run — that submits nothing.</span>
      </div>
    </div>
    <div class="modal-foot">
      <button id="loop-settings-save">Save settings</button>
      <span id="loop-settings-msg" class="msg"></span>
    </div>
  </div>
</div>

<div id="disc-modal" class="modal-scrim hidden" role="dialog" aria-modal="true" aria-labelledby="disc-modal-title">
  <div class="modal">
    <div class="modal-head">
      <h3 id="disc-modal-title">Discovery settings</h3>
      <button id="disc-modal-x" class="modal-x" type="button" aria-label="Close">✕</button>
    </div>
    <div class="modal-body">
      <p class="editing tight">Control what the bot searches and how it filters matches — no config
        files to touch. Saved to <code>profile/discovery.yaml</code> (git-ignored).</p>
      <div id="disc-form">Loading…</div>
    </div>
    <div class="modal-foot">
      <button id="save-disc">Save settings</button>
      <span id="disc-msg" class="msg"></span>
    </div>
  </div>
</div>

<!-- Review (decision 184): every card's "Review" opens HERE, not in the card. One panel, shown
     over the page, so the lists underneath stay slim one-line rows. Its contents are painted by
     renderReview; the sign-off buttons live inside them, so a submit is still one step past
     seeing the answers. -->
<div id="review-modal" class="modal-scrim hidden" role="dialog" aria-modal="true" aria-labelledby="review-modal-title">
  <div class="modal modal-wide">
    <div class="modal-head">
      <h3 id="review-modal-title">Review</h3>
      <button id="review-modal-x" class="modal-x" type="button" aria-label="Close">✕</button>
    </div>
    <div class="modal-body">
      <div id="review-panel"></div>
    </div>
  </div>
</div>
<script>
const OPTS = /*OPTIONS*/;
const $ = (id) => document.getElementById(id);

function fill(sel, items) {
  sel.innerHTML = "";
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it.path; o.textContent = it.label; sel.appendChild(o);
  }
}
fill($("resume"), OPTS.resumes);
fill($("fixture"), OPTS.fixtures);
// Prefer a profile/ resume if present.
const prof = OPTS.resumes.find(r => r.path.startsWith("profile/"));
if (prof) $("resume").value = prof.path;

// ---- Claude connection panel + modal (subscription PRIMARY, API-key FALLBACK) ----
let AUTH = OPTS.auth || {};
function renderAccount(a) {
  AUTH = a || {};
  const el = $("account");
  let dot, eng, sub;
  if (a.claude_code) {
    dot = "on"; eng = "Claude subscription"; sub = "via Claude Code — recommended, not metered";
  } else if (a.api_key_set) {
    dot = "on"; eng = "Anthropic API key";
    sub = "fallback · " + (a.api_key_masked || "connected") + " · pay-per-token";
  } else {
    dot = "off"; eng = "Not connected"; sub = "using the free rules engine";
  }
  el.innerHTML = `<span class="dot ${dot}"></span><span class="acc-eng">${escapeHtml(eng)}</span>`
    + `<div class="acc-sub">${escapeHtml(sub)}</div><div class="acc-manage">Manage connection →</div>`;
}
renderAccount(OPTS.auth);
renderClaudeModal(OPTS.auth);   // paint the Settings → Claude connection section on first load
// Footer connection chip → Settings, landing on the Claude section (UI Principle #2: the fix, one click).
$("account").addEventListener("click", () => goSettings("set-claude"));

// Switch to Settings and scroll a section into view. Used by the footer chip and the loop's
// "Set up notifications" link so both land on the exact panel that unblocks the user.
function goSettings(anchorId) {
  const tab = document.querySelector('.tab[data-view="settings"]');
  if (tab) tab.click();
  if (anchorId) { const t = $(anchorId); if (t) t.scrollIntoView({behavior:"smooth", block:"start"}); }
}
$("loop-notify-link").addEventListener("click", (e) => { e.preventDefault(); goSettings("set-notify"); });

async function refreshAuth() {
  try { const a = await (await fetch("/auth/status")).json(); renderAccount(a); renderClaudeModal(a); } catch (e) {}
}

function renderClaudeModal(a) {
  a = a || AUTH;
  const engName = a.claude_code ? "your Claude subscription (Claude Code)"
    : a.api_key_set ? "your Anthropic API key (fallback)" : "the free rules engine";
  $("claude-active").innerHTML = "Right now, tailoring uses <b>" + escapeHtml(engName) + "</b>. "
    + "The app prefers your subscription, falls back to an API key, then the no-account rules engine.";
  const subHead = '<div class="conn-head">Claude subscription <span class="tag primary">Primary</span></div>';
  $("claude-sub").innerHTML = a.claude_code
    ? subHead + '<div class="conn-body"><span class="conn-ok">✓ Connected via Claude Code.</span> '
        + 'Tailoring runs on your Claude Pro/Max plan (not metered). Sign-in lives inside Claude Code itself.</div>'
    : subHead + '<div class="conn-body">Not detected. Install <b>Claude Code</b> and run <code>claude</code> → '
        + '<code>/login</code> to tailor on your subscription (recommended — no per-token cost). '
        + '<a class="linkbtn" href="https://claude.com/product/claude-code" target="_blank" rel="noopener">Get Claude Code ↗</a><br>'
        + '<span style="color:var(--faint)">Anthropic only allows the subscription inside Claude Code / Claude.ai, '
        + 'so this app can’t “log in with Claude” directly.</span></div>';
  const keyHead = '<div class="conn-head">Anthropic API key <span class="tag">Fallback</span></div>';
  const key = $("claude-key");
  if (a.api_key_set) {
    key.innerHTML = keyHead + '<div class="conn-body"><span class="conn-ok">✓ Connected</span> — <b>'
      + escapeHtml(a.api_key_masked || "key") + '</b>. Used only when Claude Code isn’t available. '
      + 'Billed pay-per-token to your API account.</div>'
      + '<div class="conn-row"><button id="key-disconnect" class="tbtn" type="button">Disconnect</button>'
      + '<span id="key-msg" class="msg"></span></div>';
    $("key-disconnect").addEventListener("click", disconnectKey);
  } else {
    key.innerHTML = keyHead + '<div class="conn-body">Optional. Uses the <b>metered Anthropic API</b> with your own key '
      + '— pay-per-token, <b>separate</b> from your subscription. Stored in your OS keychain, never in a file.<br>'
      + '<a class="linkbtn" href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">Create an API key ↗</a></div>'
      + '<div class="conn-row"><input id="key-input" type="password" placeholder="sk-ant-…" autocomplete="off">'
      + '<button id="key-connect" type="button">Connect</button></div>'
      + '<div class="conn-row"><span id="key-msg" class="msg"></span></div>';
    $("key-connect").addEventListener("click", connectKey);
  }
}

async function connectKey() {
  const inp = $("key-input"), btn = $("key-connect"), msg = $("key-msg");
  const key = (inp.value || "").trim();
  if (!key) { msg.className = "msg err"; msg.textContent = "Paste your Anthropic API key."; return; }
  btnBusy(btn, "Verifying…"); msg.className = "msg busy"; msg.textContent = "";
  try {
    const d = await (await fetch("/auth/apikey", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({key})})).json();
    btnDone(btn);
    if (d.ok) { renderAccount(d.status); renderClaudeModal(d.status); }
    else { msg.className = "msg err"; msg.textContent = d.message || "Couldn't connect."; }
  } catch (e) { btnDone(btn); msg.className = "msg err"; msg.textContent = String(e.message || e); }
}

async function disconnectKey() {
  const btn = $("key-disconnect");
  btnBusy(btn, "Disconnecting…");
  try {
    const d = await (await fetch("/auth/apikey/disconnect", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:"{}"})).json();
    btnDone(btn); renderAccount(d.status); renderClaudeModal(d.status);
  } catch (e) { btnDone(btn); }
}

// ---- Dry-run options (decision 163) -----------------------------------------
// Two choices, one button. Job: the best match the search finds, or a posting you paste (a job
// nobody discovered for you). How far: tailor + fill the form, or stop at the tailored résumé.
// A pasted posting has no application form, so it can only tailor — the fill option says why
// rather than silently failing (UI Principle #3).
const DRY = { job: "find", mode: "apply" };
function renderDryOpts() {
  const pasted = DRY.job === "paste";
  if (pasted) DRY.mode = "tailor";
  $("dry-job-find").classList.toggle("on", !pasted);
  $("dry-job-paste").classList.toggle("on", pasted);
  $("dry-mode-apply").classList.toggle("on", DRY.mode === "apply");
  $("dry-mode-tailor").classList.toggle("on", DRY.mode === "tailor");
  $("dry-mode-apply").disabled = pasted;
  $("dry-mode-apply").title = pasted
    ? "A pasted posting has no application form to fill — it can only be tailored for."
    : "";
  $("dry-paste").classList.toggle("hidden", !pasted);
  $("test-run").textContent = pasted ? "▶ Tailor for this posting"
                            : DRY.mode === "tailor" ? "▶ Find one & tailor (dry-run)"
                            : "▶ Find & fill one (dry-run)";
  $("dry-run-blurb").innerHTML = pasted
    ? "Tailors your résumé to the posting below and shows the result — nothing is searched, "
      + "filled, or submitted. Use it for a job someone sent you."
    : DRY.mode === "tailor"
    ? "Searches, ranks every posting by fit, and tailors your résumé for the single best match — "
      + "then stops. <b>No browser opens and no form is filled.</b> The PDF is kept, so a later "
      + "apply run reuses it instead of paying for the tailoring twice."
    : "One end-to-end pass: searches, ranks every posting by fit, then tailors and auto-fills the "
      + "single best match in a browser you can watch. <b>Never submits</b> — review it, click "
      + "Finish. Recorded in Track.";
}
$("dry-job-find").addEventListener("click", () => { DRY.job = "find"; renderDryOpts(); });
$("dry-job-paste").addEventListener("click", () => { DRY.job = "paste"; renderDryOpts(); });
$("dry-mode-apply").addEventListener("click", () => { DRY.mode = "apply"; renderDryOpts(); });
$("dry-mode-tailor").addEventListener("click", () => { DRY.mode = "tailor"; renderDryOpts(); });
renderDryOpts();

$("jobmode").addEventListener("change", () => {
  const custom = $("jobmode").value === "custom";
  $("customBox").classList.toggle("hidden", !custom);
  $("fixtureBox").classList.toggle("hidden", custom);
});

let lastReq = null;
$("dl-pdf").addEventListener("click", async () => {
  if (!lastReq) return;
  const btn = $("dl-pdf"), msg = $("pdf-msg");
  btnBusy(btn, "Generating PDF…"); msg.className = "msg"; msg.textContent = "";
  try {
    const res = await fetch("/pdf", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(lastReq) });
    if (!res.ok) { let e = {}; try { e = await res.json(); } catch (x) {} throw new Error(e.error || "PDF export failed"); }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a"); a.href = url; a.download = "tailored_resume.pdf";
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  } catch (e) {
    msg.className = "msg err"; msg.textContent = String(e.message || e);
  } finally { btnDone(btn); }
});
// Tailor for a posting the user pasted or saved (no discovery, no form fill) — the manual half of
// the dry run. Renders into the same #tailor-out block a tailor-only discovered run uses.
function clearTailorOut() {
  $("tailor-out").classList.add("hidden");
  $("meta").classList.add("hidden"); $("dl-pdf").classList.add("hidden"); $("pdf-msg").textContent = "";
  $("result").innerHTML = ""; $("why-panel").classList.add("hidden");
}
function showTailored({html, notes, warnings, backend, pages, title, company, pdfHref}) {
  $("tailor-out").classList.remove("hidden");
  let meta = `<span class="badge">engine: ${escapeHtml(backend || "")}</span>`
    + (pages ? ` <span class="badge">${pages}pg</span>` : "")
    + ` &nbsp; <b>${escapeHtml(title || "")}</b>${company ? " @ " + escapeHtml(company) : ""}`;
  if (notes && notes.length) meta += `<div class="notes"><b>Notes:</b> ${notes.map(escapeHtml).join(" ")}</div>`;
  if (warnings && warnings.length) meta += `<div class="warn"><b>⚠ Drift warnings:</b> ${warnings.map(escapeHtml).join("; ")}</div>`;
  $("meta").innerHTML = meta; $("meta").classList.remove("hidden");
  $("result").innerHTML = `<div class="resume">${html || ""}</div>`;
  if (pdfHref) {                       // already-rendered PDF (a discovered tailor-only run)
    $("dl-pdf").classList.add("hidden");
    $("pdf-msg").innerHTML = `<a href="${pdfHref}" target="_blank" rel="noopener">Open the tailored PDF ↗</a>`;
  } else {                             // pasted posting: render on demand from what we just got
    $("dl-pdf").classList.remove("hidden");
  }
  showWhyIntro();
}
async function tailorPastedPosting() {
  const jobmode = $("jobmode").value;
  const payload = {
    resume: currentResume(),
    backend: $("backend").value,
    quality: $("quality").value,
    pages: parseFloat($("pages").value),
    line_chars: parseInt($("linechars").value) || 100,
    job: jobmode === "custom"
      ? { mode:"custom", title:$("title").value, company:$("company").value, body:$("body").value }
      : { mode:"fixture", fixture:$("fixture").value },
  };
  const btn = $("test-run"), msg = $("test-msg");
  const est = { fast: "~30s", balanced: "~40s", max: "up to ~2 min" }[$("quality").value] || "";
  btnBusy(btn, "Tailoring…");
  msg.className = "msg busy";
  const stop = busyInto(msg, `Tailoring your résumé to this posting (${est})…`, true);  // long Claude call — show elapsed
  clearTailorOut();
  try {
    const res = await fetch("/tailor", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "request failed");
    lastReq = { resume: currentResume(), tailored: data.tailored };
    showTailored(data);
    msg.className = "msg ok"; msg.textContent = "Tailored ✓ — nothing was filled or submitted.";
  } catch (e) {
    msg.className = "msg err"; msg.textContent = String(e.message || e);
  } finally {
    stop(); btnDone(btn);
  }
}

// ---- "Why was this tailored this way" — per-entry rationale panel ----
function showWhyIntro() {
  const wp = $("why-panel"); wp.classList.remove("hidden"); wp.innerHTML = "";
  const n = document.querySelectorAll("#result .entry[data-why]").length;
  wp.appendChild(el("h3", {text:"Why this tailoring"}));
  wp.appendChild(el("div", {class:"whint", text: n
    ? "Click any experience, project, or activity to see why it was kept and how it was tailored for this job."
    : "This run didn't include per-entry tailoring notes."}));
  document.querySelectorAll("#result .entry.why-active").forEach(e => e.classList.remove("why-active"));
}
$("result").addEventListener("click", (ev) => {
  const entry = ev.target.closest(".entry[data-why]");
  if (!entry) return;
  document.querySelectorAll("#result .entry.why-active").forEach(e => e.classList.remove("why-active"));
  entry.classList.add("why-active");
  const title = (entry.querySelector(".l b") || {}).textContent || "This entry";
  const wp = $("why-panel"); wp.classList.remove("hidden"); wp.innerHTML = "";
  wp.appendChild(el("h3", {text:"Why this entry"}));
  wp.appendChild(el("div", {class:"wtitle", text:title}));
  wp.appendChild(el("div", {class:"wbody", text: entry.getAttribute("data-why") || ""}));
});

// ---- Résumé data editor (full edit form) ----
const currentResume = () => $("resume").value;
let R = null;

// tiny safe DOM builder — values set via .value (no HTML-escaping pitfalls).
function el(tag, props, kids) {
  const e = document.createElement(tag);
  props = props || {};
  for (const k in props) {
    if (k === "class") e.className = props[k];
    else if (k === "value") e.value = props[k] == null ? "" : props[k];
    else if (k === "text") e.textContent = props[k];
    else if (k === "on") { for (const ev in props[k]) e.addEventListener(ev, props[k][ev]); }
    else e.setAttribute(k, props[k]);
  }
  for (const c of [].concat(kids || [])) if (c != null) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return e;
}
const linesOf = v => (v || "").split("\\n").map(s => s.trim()).filter(Boolean);
const orNull = v => { v = (v || "").trim(); return v || null; };
const row2 = (a, b) => el("div", {class:"row2"}, [a, b]);
const fld = (label, key, value) => el("div", {class:"fld"}, [el("label", {text:label}), el("input", {class:"f", "data-k":key, value:value})]);
const area = (label, key, value, ph) => el("div", {class:"fld"}, [el("label", {text:label}), el("textarea", {class:"f", "data-k":key, placeholder:ph||"", value:value})]);
const cardData = card => { const o = {}; card.querySelectorAll("[data-k]").forEach(f => o[f.dataset.k] = f.value); return o; };

// ---- Consistent waiting UI (used by every async action) ----
// While work is in flight: disable the trigger button and show a spinner + working label on it;
// show a spinner + specific status message in-place; for long waits, tick elapsed seconds so it
// never looks frozen. On done, callers restore the button and replace the message with the result.
function btnBusy(btn, workingText) {
  if (btn._orig == null) btn._orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = "";
  btn.append(el("span", {class:"spin light"}), document.createTextNode(workingText));
}
function btnDone(btn) {
  btn.disabled = false;
  if (btn._orig != null) { btn.innerHTML = btn._orig; btn._orig = null; }
}
// Fill `container` with a spinner + label; if longRunning, append a live elapsed-seconds counter.
// Returns stop() to clear the timer. Callers overwrite the container to show the result.
function busyInto(container, label, longRunning) {
  container.innerHTML = "";
  const secs = longRunning ? el("span", {class:"busy-s", text:"0s"}) : null;
  container.append(el("span", {class:"spin"}), el("span", {class:"busy-l", text:label}));
  if (!secs) return () => {};
  container.append(secs);
  const t0 = Date.now();
  const iv = setInterval(() => { secs.textContent = Math.round((Date.now() - t0) / 1000) + "s"; }, 500);
  return () => clearInterval(iv);
}

// Collapsible entry: header shows a one-line summary; expand to edit the fields granularly.
function entryCard(fields, summaryFn) {
  const card = el("div", {class:"card entry collapsed"});
  const title = el("span", {class:"entry-title"});
  const del = el("button", {class:"del", type:"button", text:"✕", title:"Remove",
    on:{click:(ev)=>{ ev.stopPropagation(); card.remove(); }}});
  const head = el("div", {class:"entry-head"}, [el("span", {class:"chev", text:"▸"}), title, del]);
  const refresh = () => {
    const s = (summaryFn(card) || "").trim();
    title.textContent = s || "New entry — click to edit";
    title.className = "entry-title" + (s ? "" : " blank");
  };
  head.addEventListener("click", () => { card.classList.toggle("collapsed"); refresh(); });
  card.append(head, el("div", {class:"entry-body"}, fields));
  refresh();
  return card;
}
function expCard(e) {
  e = e || {};
  return entryCard([
    row2(fld("Organization","organization",e.organization), fld("Role / title","role",e.role)),
    fld("Location","location",e.location),
    row2(fld("Start — e.g. May 2024","start",e.start), fld("End — e.g. Present","end",e.end)),
    area("Bullets (one per line)","bullets",(e.bullets||[]).join("\\n")),
  ], c => { const d = cardData(c); return [d.organization, d.role].filter(Boolean).join(" — "); });
}
function projCard(p) {
  p = p || {};
  // Hidden `impact` carries Claude's technical-impressiveness score (1–5) through the save
  // round-trip; the collapsed header shows it as a ★ badge so ranking is visible at a glance.
  return entryCard([
    el("input", {type:"hidden", "data-k":"impact", value:(p.impact==null?"":String(p.impact))}),
    row2(fld("Project name","name",p.name), fld("Tech — e.g. Python, SQL","tech",p.tech)),
    fld("Link (optional) — repo, demo, or write-up","link",p.link),
    area("Bullets (one per line)","bullets",(p.bullets||[]).join("\\n")),
  ], c => { const d = cardData(c); return (d.impact ? "★"+d.impact+"  " : "") + (d.name||""); });
}
function eduCard(e) {
  e = e || {};
  return entryCard([
    row2(fld("School","school",e.school), fld("Location","location",e.location)),
    row2(fld("Degree","degree",e.degree), fld("Graduation","graduation",e.graduation)),
    area("Details (one per line)","details",(e.details||[]).join("\\n")),
  ], c => { const d = cardData(c); return [d.school, d.degree].filter(Boolean).join(" — "); });
}
function skillCard(s) {
  s = s || {};
  return entryCard([
    fld("Category","category",s.category),
    fld("Items (comma-separated)","items",(s.items||[]).join(", ")),
  ], c => { const d = cardData(c); return [d.category, d.items].filter(Boolean).join(": "); });
}
// A spoken/written language. Proficiency uses the wording application forms offer, so the stored
// value matches their dropdown options directly; selField keeps any other saved wording.
const PROFICIENCY_OPTS = [["","— not stated —"],"Native","Fluent","Professional","Conversational","Basic"];
function langCard(l) {
  l = l || {};
  return entryCard([
    row2(fld("Language — e.g. Spanish","name",l.name),
         selField("Proficiency","proficiency",l.proficiency,PROFICIENCY_OPTS)),
  ], c => { const d = cardData(c); return [d.name, d.proficiency].filter(Boolean).join(" — "); });
}
function section(title, id, items, addLabel, blank) {
  const body = el("div", {id:id, class:"cards"}, items);
  const add = el("button", {class:"addbtn", type:"button", text:addLabel, on:{click:()=>{
    const c = blank(); body.appendChild(c);
    c.classList.remove("collapsed");           // new entries open ready to edit
    const inp = c.querySelector("input,textarea"); if (inp) inp.focus();
  }}});
  return el("div", {class:"sec"}, [el("h3", {text:title}), body, add]);
}
const cardsIn = id => [...$(id).querySelectorAll(":scope > .card")];
function expData(card) {
  const d = cardData(card);
  return { organization:(d.organization||"").trim(), role:(d.role||"").trim(), location:orNull(d.location),
           start:(d.start||"").trim(), end:(d.end||"").trim(), bullets:linesOf(d.bullets) };
}
function collect() {
  const b = cardData($("basic"));
  return {
    contact: { name:(b.name||"").trim(), email:(b.email||"").trim(), phone:orNull(b.phone), location:orNull(b.location), links:linesOf(b.links) },
    summary: orNull(b.summary),
    certifications: linesOf(b.certifications),
    section_order: (R && R.section_order) || null,
    skills: cardsIn("sec-skills").map(c => { const d = cardData(c); return { category:(d.category||"").trim(), items:(d.items||"").split(",").map(s=>s.trim()).filter(Boolean) }; }).filter(s => s.category),
    experience: cardsIn("sec-experience").map(expData).filter(e => e.organization || e.role),
    activities: cardsIn("sec-activities").map(expData).filter(e => e.organization || e.role),
    projects: cardsIn("sec-projects").map(c => { const d = cardData(c); const im = parseInt(d.impact,10); return { name:(d.name||"").trim(), tech:orNull(d.tech), link:orNull(d.link), impact:(im>=1 && im<=5)?im:null, bullets:linesOf(d.bullets) }; }).filter(p => p.name),
    education: cardsIn("sec-education").map(c => { const d = cardData(c); return { school:(d.school||"").trim(), degree:(d.degree||"").trim(), location:orNull(d.location), graduation:orNull(d.graduation), details:linesOf(d.details) }; }).filter(e => e.school),
  };
}

// One definition of "show this section": the nav clicks, the deep-link router, the tour, and the
// initial landing all go through it, so a view can never be shown without the data it needs.
// `nudge:false` is for the landing call — a first-visit nudge belongs to navigating INTO a section,
// and at load /setup/status hasn't answered yet, so it would prompt users who are already set up.
function showView(v, {nudge = true} = {}) {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x.dataset.view === v));
  $("view-discover").classList.toggle("hidden", v !== "discover");
  $("view-profile").classList.toggle("hidden", v !== "profile");
  $("view-track").classList.toggle("hidden", v !== "track");
  $("view-notifications").classList.toggle("hidden", v !== "notifications");
  $("view-settings").classList.toggle("hidden", v !== "settings");
  if (v === "profile") loadProfile();
  if (v === "track") loadTrack();
  if (v === "discover") { pollLoop(); loadParked(); loadSources(); loadFitInsights(); loadCandidates(); loadDisc(); pollTest(); }
  if (v === "notifications") loadInbox();
  if (v === "settings") loadSettings();
  if (nudge) maybeShowNudge(v);
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => showView(t.dataset.view)));

// Deep-link support: /#<view> selects that tab on load and on hash change. The loop's push
// notifications link to /#notifications (decision 138), and a desktop notification that opens the
// app lands there too. Previously the #hash in those links did nothing (no router existed).
// Returns whether the hash named a real view, so the landing call knows to fall back to Discover.
function applyHash() {
  const h = (location.hash || "").replace(/^#/, "");
  const known = h && document.querySelector('.tab[data-view="' + h + '"]');
  if (known) showView(h);
  return !!known;
}
window.addEventListener("hashchange", applyHash);

// Nav badge = count of items needing the user; keep it live in the background (cheap /inbox poll).
refreshBadge();
setInterval(refreshBadge, 30000);
// Land on Discover — the stage the user actually starts from (find jobs), not Review & tailor, which
// only has something to show once a posting exists. Honors an initial /#notifications (etc.) first.
// Deferred a tick so it runs AFTER the whole script has initialized — showView reaches maybeShowNudge,
// which reads TOUR_ACTIVE (declared later); firing during synchronous init would hit its dead zone.
setTimeout(() => { if (!applyHash()) showView("discover", {nudge: false}); }, 0);

// ---- First-visit nudges — the one thing to do in Profile / Discover, shown once per section --
// (moved out of the old up-front checklist: résumé import auto-fills the Profile fields, so the
// "add details" and "choose jobs" prompts belong where the user lands, not as chores at launch).
// The tour drives the tabs too; suppress nudges while it runs so they don't flash behind it.
let TOUR_ACTIVE = false;
let SETUP = null;  // cached /setup/status readiness, so a nudge hides once its section is done
const nudgeSeen = (v) => { try { return localStorage.getItem("ab-nudge-" + v) === "1"; } catch (e) { return false; } };
const markNudgeSeen = (v) => { try { localStorage.setItem("ab-nudge-" + v, "1"); } catch (e) {} };
function setupOk(key) {  // true when that readiness step is satisfied (unknown ⇒ assume not, so we still help)
  if (!SETUP) return false;
  const s = (SETUP.steps || []).find(x => x.key === key);
  return !!(s && s.ok);
}
function maybeShowNudge(v) {
  if (TOUR_ACTIVE) return;
  if (v === "profile") {
    const done = setupOk("profile") && setupOk("resume");
    if (nudgeSeen("profile") || done) return;
    $("profile-nudge").classList.remove("hidden");
  } else if (v === "discover") {
    if (nudgeSeen("discover") || setupOk("discovery")) return;
    $("discover-nudge").classList.remove("hidden");
  }
}
function dismissNudge(v) { markNudgeSeen(v); $(v + "-nudge").classList.add("hidden"); }
$("profile-nudge-x").addEventListener("click", () => dismissNudge("profile"));
$("discover-nudge-x").addEventListener("click", () => dismissNudge("discover"));
$("profile-nudge-go").addEventListener("click", () => {
  dismissNudge("profile");
  const e = $("s-upload"); if (e) e.scrollIntoView({behavior:"smooth", block:"center"});
});
$("discover-nudge-go").addEventListener("click", () => {
  dismissNudge("discover");
  openDiscModal();
});
$("resume").addEventListener("change", () => { if (!$("view-profile").classList.contains("hidden")) loadProfile(); });

// ---- Discover: run one full dry-run test ------------------------------------
let TEST_TIMER = null, TEST_T0 = null, TEST_JUDGED_SIG = "";
async function startTestRun(fresh) {
  if (DRY.job === "paste" && !fresh) return tailorPastedPosting();   // no search to run
  const btn = $("test-run"), msg = $("test-msg");
  msg.className = "msg"; msg.textContent = "";
  clearTailorOut();
  btnBusy(btn, fresh ? "Re-searching…" : "Starting…");
  try {
    const r = await (await fetch("/test-run", {method:"POST", headers:{"Content-Type":"application/json"},
                                               body: JSON.stringify({fresh: !!fresh, mode: DRY.mode})})).json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = r.error || "Could not start."; btnDone(btn); return; }
    TEST_T0 = Date.now();
    pollTest();
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); btnDone(btn); }
}
$("test-run").addEventListener("click", () => startTestRun(false));

function testStepList(s) {
  // A tailor-only run stops after the PDF — don't show fill/review steps it will never reach.
  const steps = [["discover","Discovering postings"],["match","Judging fit"],
                 ["tailor","Tailoring résumé"],["pdf","Exporting PDF"]];
  if (s.mode !== "tailor") steps.push(["apply","Filling the form"],["review","Filled — review"]);
  const order = steps.map(x => x[0]);
  const cur = order.indexOf(s.step);
  return steps.map(([k,label],i) => {
    const done = (s.phase==="done") || (cur>i);
    const active = cur===i && s.phase!=="done";
    const mark = done ? "✓" : (active ? "●" : "○");
    return `<div class="tstep ${active?'act':''} ${done?'done':''}">${mark} ${label}</div>`;
  }).join("");
}

function renderScanFunnel(f, judged, minFit) {
  // Visual bar funnel: one horizontal bar per stage, width ∝ postings still alive, with the drop
  // (−N + reason) called out at each narrowing. Shows WHERE postings are lost (diagnose-first).
  // No-op on cache hits (funnel isn't recomputed) or before a run has data.
  if (!f || !f.discovered) return "";
  const disc = f.discovered;
  const stages = [{label:"Discovered", count:disc, drop:0, reason:""}];
  if (f.after_gates != null) {
    const gmap = {gate_title:"title-exclude", gate_level:"experience level", gate_remote:"remote-only",
                  gate_salary:"below min salary", gate_stale:"too old"};
    const parts = [];
    for (const k in gmap) if (f[k]) parts.push(`${f[k]} ${gmap[k]}`);
    stages.push({label:"Passed coarse gates", count:f.after_gates, drop:disc - f.after_gates,
                 reason:parts.join(" · ") || "gates"});
  }
  if (f.skipped_seen) stages.push({label:"Not already in tracker", count:f.after_seen,
                 drop:f.skipped_seen, reason:"already in your tracker"});
  if (f.non_fillable) stages.push({label:"On a fillable portal", count:f.into_matcher,
                 drop:f.non_fillable, reason:"Workday / iCIMS — can't auto-fill yet"});
  const nSkills = f.min_skills != null ? f.min_skills : 1;
  stages.push({label:`Matched your skills (≥${nSkills})`, count:f.matched != null ? f.matched : 0,
                 drop:f.keyword_dropped || 0, reason:"too few skill keywords in the posting"});
  const nJudged = f.judged != null ? f.judged : 0;
  stages.push({label:`Judged for fit by Claude`, count:nJudged,
                 drop:Math.max(0, (f.matched || 0) - nJudged),
                 reason:`past the top-${f.top_n != null ? f.top_n : "N"} judge cap — never scored`});
  // Final stage: of those judged, how many cleared the fit bar (the number that actually "gets through").
  if (Array.isArray(judged) && minFit != null) {
    const cleared = judged.filter(j => j.cleared).length;
    stages.push({label:`Cleared min-fit ≥${minFit}`, count:cleared, drop:Math.max(0, nJudged - cleared),
                 reason:`judged below ${minFit}/100`, kept:true});
  }
  const rows = stages.map(s => {
    const pct = Math.max(1.5, Math.round(100 * s.count / disc));  // keep a sliver visible even at ~0
    const drop = s.drop ? `<div class="sfdrop">−${s.drop} <span class="sfreason">${escapeHtml(s.reason)}</span></div>` : "";
    return `<div class="sfrow${s.kept ? " sfkept" : ""}">`
         + `<div class="sflabel">${escapeHtml(s.label)}</div>`
         + `<div class="sfmain"><div class="sftrack"><div class="sfbar" style="width:${pct}%"></div>`
         + `<span class="sfcount">${s.count}</span></div>${drop}</div></div>`;
  }).join("");
  const lost = disc - (stages[stages.length-1].count);
  // Postings back only because their prepared application was never reviewed (decision 149) —
  // named here so "why is this one back?" is answered on the breakdown itself.
  const back = f.revisited ? ` · ${f.revisited} brought back (prepared but never reviewed)` : "";
  return `<details class="sfunnel" open><summary>Search funnel — `
       + `<b>${stages[stages.length-1].count}</b> of ${disc} postings got through`
       + `${lost>0 ? ` · ${lost} dropped along the way` : ""}${back}</summary>`
       + `<div class="sfbody">${rows}</div></details>`;
}

function renderChosen(s) {
  const c = s.chosen; if (!c) return "";
  const fit = (c.fit_score!=null) ? `<span class="fitpill">fit ${c.fit_score}/100 ${c.qualified?'✓ qualified':'✗ not qualified'}</span>` : "";
  const meta = [c.location, c.compensation].filter(Boolean).map(escapeHtml).join(" · ");
  let html = `<div class="tclabel">Following through on this one posting:</div>`;
  html += `<div class="tctitle">${escapeHtml(c.company)} — ${escapeHtml(c.title)} ${fit}</div>`;
  if (meta) html += `<div class="tcmeta">${meta}</div>`;
  if (c.dimensions) html += `<div class="tcmeta">${["skills","experience","seniority"]
    .filter(k => c.dimensions[k] != null).map(k => `${k} ${c.dimensions[k]}`).join(" · ")}</div>`;
  if (c.why) html += `<div class="tcwhy"><b>Why:</b> ${escapeHtml(c.why)}</div>`;
  if (c.missing && c.missing.length) html += `<div class="tcwhy"><b>Missing:</b> ${c.missing.slice(0,3).map(escapeHtml).join("; ")}</div>`;
  html += `<div class="tjacts">${openPostingBtn(c.url)}</div>`;
  return html;
}

// A posting's URL as a labelled button, never as raw link text — the URL itself tells the user
// nothing they can act on, and an ATS URL is long enough to wreck the layout of any card it's in.
function openPostingBtn(url, label) {
  if (!url) return "";
  return `<a class="tjbtn tjopen" href="${escapeHtml(url)}" target="_blank" rel="noopener"`
       + ` title="Open this posting on its job board: ${escapeHtml(url)}">`
       + `${escapeHtml(label || "Open posting")} ↗</a>`;
}

// Whether an Apply click from the search breakdown tailors the résumé first. On by default —
function renderJudged(s) {
  const rows = s.judged || [];
  const cleared = rows.filter(r => r.cleared).length;
  const minFit = (s.min_fit != null) ? s.min_fit : 50;
  let html = `<div class="tjhead">Postings Claude judged this run — ${rows.length} scored, `
    + `${cleared} cleared your ${minFit}/100 cutoff. Denied ones are shown so you can see what the searches return, `
    + `and <b>Apply anyway</b> applies to one regardless of its score.</div>`;
  if (s.calib_note) html += `<div class="tjhead">→ ${escapeHtml(s.calib_note)}</div>`;
  for (const r of rows) {
    const cls = r.cleared ? "tjrow ok" : "tjrow no";
    const badge = r.cleared ? `✓ ${r.fit_score}` : `✗ ${r.fit_score}`;
    const meta = [r.location, r.compensation].filter(Boolean).map(escapeHtml).join(" · ");
    html += `<div class="${cls}">`
      + `<div class="tjtop"><span class="tjscore">${badge}</span>`
      + `<span class="tjname">${escapeHtml(r.company)} — ${escapeHtml(r.title)}</span></div>`;
    if (meta) html += `<div class="tjmeta">${meta}</div>`;
    if (r.dimensions) html += `<div class="tjmeta">${["skills","experience","seniority"]
      .filter(k => r.dimensions[k] != null).map(k => `${k} ${r.dimensions[k]}`).join(" · ")}</div>`;
    if (r.why) html += `<div class="tjwhy">${escapeHtml(r.why)}</div>`;
    if (r.missing && r.missing.length) html += `<div class="tjmiss"><b>Missing:</b> ${r.missing.map(escapeHtml).join("; ")}</div>`;
    // Apply from here on ANY judged posting — including one below the cutoff ("Apply anyway"),
    // which the automatic queue drops. The click APPLIES (decision 177): tailor, fill, submit,
    // confirmed once in the click handler. A loop running in dry run prepares it instead.
    //
    // Tailoring is a choice per POSTING, not a mode (decision 180): each row carries both buttons,
    // so the label says what that click will do instead of depending on a checkbox set earlier.
    const verb = r.cleared ? "Apply" : "Apply anyway";
    const under = r.cleared ? "" : ` despite the ${r.fit_score}/100 score being under your ${minFit}/100 cutoff`;
    const why = `Apply to this posting${under}: tailor your résumé to it, fill the form, and submit. `
      + `You confirm before anything is sent.`;
    const whyAsIs = `Apply to this posting${under} with your résumé exactly as it is — no Claude call, `
      + `nothing rewritten — then fill the form and submit. You confirm before anything is sent.`;
    html += `<div class="tjacts">`
      + `<button type="button" class="tjbtn ${r.cleared ? "tjapply" : "tjanyway"}" `
      + `data-japply="${escapeHtml(r.url)}" data-jtailor="1" title="${escapeHtml(why)}">${verb} ▶</button>`
      + `<button type="button" class="tjbtn tjasis" `
      + `data-japply="${escapeHtml(r.url)}" data-jtailor="0" title="${escapeHtml(whyAsIs)}">${verb} as-is ▶</button>`
      + openPostingBtn(r.url)
      + `<span class="tjnote"></span></div></div>`;
  }
  return html;
}

// One delegated handler for every Apply / Apply anyway button in either search breakdown: the
// breakdowns re-render on a 2s poll, so per-button listeners would be rebound (and lost)
// constantly. The click applies to the posting (decision 177) — tailor, fill, submit — and the
// row itself reports what happened. The one exception is a loop running in DRY RUN: the click is
// served by that loop, which submits nothing, so it prepares and holds it instead.
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest && ev.target.closest("[data-japply]");
  if (!btn) return;
  const url = btn.getAttribute("data-japply");
  const note = btn.parentElement.querySelector(".tjnote");
  const setNote = (cls, text) => { if (note) { note.className = "tjnote " + cls; note.textContent = text; } };
  const row = btn.closest(".tjrow");
  const who = row ? (row.querySelector(".tjname") || {}).textContent || "" : "";
  // Which of the row's two buttons was clicked (decision 180) — the tailoring choice belongs to
  // this posting, not to a mode set somewhere else.
  const tailor = btn.getAttribute("data-jtailor") !== "0";
  // Ask the server, not a cached flag: whether this click submits depends on the loop's mode
  // right now, and the confirm must state what will actually happen (UI Principle #3).
  let st = {};
  try { st = await (await fetch("/loop/status")).json(); } catch (e) {}
  const dryLoop = !!(st.running && st.dry_run);
  if (!dryLoop) {
    const ok = confirm("Really apply to " + (who || "this posting") + "?\\n\\n"
      + (tailor ? "ApplicationBot will tailor your résumé, fill the form and SUBMIT it. "
                : "ApplicationBot will fill the form with your résumé exactly as it is (no tailoring) "
                  + "and SUBMIT it. ")
      + "This is a real, irreversible submission; the pre-submit check still stops it if a required "
      + "field is unanswered.");
    if (!ok) return;
  }
  btnBusy(btn, dryLoop ? "Preparing…" : "Applying…");
  setNote("", "");
  let r;
  try {
    r = await (await fetch("/judged/prepare", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({url, tailor})})).json();
  } catch (e) {
    btnDone(btn); setNote("err", String(e.message || e)); return;
  }
  if (!r.ok) {
    btnDone(btn); setNote("err", r.error || "Could not apply to this posting."); return;
  }
  if (r.queued) {
    btnDone(btn);
    setNote("", dryLoop
      ? "Queued — the loop is in dry run, so it prepares this one at its next step and holds it under “Ready to apply”."
      : "Queued — the auto-apply loop applies to this one at its next step.");
    pollLoop(); return;
  }
  // Applying owns the browser, so a second one can't start until this finishes — disable the
  // rest rather than letting a second click fail with a bare "already in progress".
  const others = Array.from(document.querySelectorAll("[data-japply]")).filter(b => b !== btn);
  others.forEach(b => { b.disabled = true; });
  setNote("", tailor ? "Tailoring your résumé, filling the form and submitting — progress is in the panel above."
                     : "Filling the form with your résumé as-is and submitting — progress is in the panel above.");
  TEST_T0 = Date.now();
  pollTest();
  const fin = await awaitPrepare();
  btnDone(btn);
  others.forEach(b => { b.disabled = false; });
  setNote(fin.ok ? "ok" : "err", fin.message);
  pollLoop(); loadParked(); refreshBadge();
});

// Wait for an Apply started from the breakdown to settle, so its row ends in a definite state
// (UI Principle #5) — the panel above shows live progress, but the row must say how it ended.
async function awaitPrepare() {
  for (;;) {
    await new Promise(done => setTimeout(done, 1500));
    let s;
    try { s = await (await fetch("/test-run/status")).json(); } catch (e) { continue; }
    if (!s || s.phase === "running") continue;
    if (s.phase === "error")
      return {ok: false, message: (s.errors || []).join(" · ") || "Applying to this posting failed."};
    return {ok: true, message: s.message || "Done — check Track for this application."};
  }
}

async function pollTest() {
  let s;
  try { s = await (await fetch("/test-run/status")).json(); } catch (e) { return; }
  const prog = $("test-progress"), chosen = $("test-chosen"), btn = $("test-run"), msg = $("test-msg");
  if (!s || s.phase === "idle") { prog.classList.add("hidden"); chosen.classList.add("hidden"); btnDone(btn); return; }

  const running = s.phase === "running", filled = s.phase === "filled";
  prog.classList.remove("hidden");
  let body = testStepList(s);
  body += `<div class="tmsg">${escapeHtml(s.message || "")}</div>`;
  if (s.step === "match" && s.judged_total) {
    const pct = Math.round(100 * (s.judged||0) / s.judged_total);
    body += `<div class="tbar"><div class="tbarfill" style="width:${pct}%"></div></div>`;
  }
  if (s.scanned) body += `<div class="tmeta">Scanned ${s.scanned} postings · ${s.matched} matched your skills${s.skipped_seen ? ` · ${s.skipped_seen} already in tracker skipped` : ""}</div>`;
  body += renderScanFunnel(s.funnel, s.judged, s.min_fit);
  if (s.from_cache) {
    const m = s.cache_age_min || 0;
    const age = m < 90 ? `${m} min ago` : `${Math.round(m/60)}h ago`;
    body += `<div class="tmeta cache">♻ Reused a saved search from ${age} — same results as before, so this run added no point to the fit chart and taught the search nothing. Re-search fresh to judge live, add a chart point, and train.`
          + `<button id="test-fresh" type="button" class="linklike"${running ? " disabled" : ""}>Re-search fresh</button></div>`;
  } else if (s.phase === "error" && s.can_research) {
    body += `<div class="tmeta cache">Nothing cleared your fit cutoff this run. Re-search fresh to pull new postings and judge them live.`
          + `<button id="test-fresh" type="button" class="linklike">Re-search fresh</button></div>`;
  }
  const el = TEST_T0 ? Math.round((Date.now()-TEST_T0)/1000) : null;
  if ((running || filled) && el!=null) body += `<div class="tmeta">${el}s elapsed</div>`;
  prog.innerHTML = body;
  const fresh = $("test-fresh");
  if (fresh) fresh.addEventListener("click", () => startTestRun(true));

  if (s.chosen) { chosen.classList.remove("hidden"); chosen.innerHTML = renderChosen(s); }

  const judged = $("test-judged");
  // Re-render the breakdown only when its own facts change. It is polled every ~1s, and a blind
  // re-render would wipe the "Preparing…" state and the outcome note off an Apply button the user
  // just clicked in it — the click's own progress would erase itself.
  if (s.judged && s.judged.length) {
    judged.classList.remove("hidden");
    const sig = JSON.stringify([s.judged, s.min_fit, s.calib_note]);
    if (sig !== TEST_JUDGED_SIG) { TEST_JUDGED_SIG = sig; judged.innerHTML = renderJudged(s); }
  } else { judged.classList.add("hidden"); TEST_JUDGED_SIG = ""; }

  if (filled) {
    if (!document.getElementById("test-finish")) {
      const box = document.createElement("div"); box.className = "tfinish";
      box.innerHTML = `<b>✓ Form filled — nothing was submitted.</b> Review it in the browser window that opened, then finish.`;
      const fb = document.createElement("button"); fb.id = "test-finish"; fb.textContent = "Finish — close browser";
      fb.addEventListener("click", async () => { fb.disabled = true; fb.textContent = "Closing…"; await fetch("/test-run/close",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}); });
      box.appendChild(fb); prog.appendChild(box);
    }
  }
  if (s.errors && s.errors.length && (s.phase==="error")) {
    msg.className = "msg err"; msg.textContent = s.errors.join(" · ");
  }
  if (s.phase === "done") {
    btnDone(btn); msg.className = "msg ok";
    msg.textContent = s.message || "Done — recorded a dry-run row in Track.";
    // Tailor-only: show the résumé it produced right here, with its drift warnings and the PDF.
    if (s.tailored && $("tailor-out").classList.contains("hidden"))
      showTailored({...s.tailored, pdfHref: "/test-run/resume"});
    loadFitInsights(); loadParked();
  }
  if (s.phase === "error") { btnDone(btn); loadFitInsights(); loadParked(); }

  if (running || filled) { clearTimeout(TEST_TIMER); TEST_TIMER = setTimeout(pollTest, 1200); }
  else { btnDone(btn); }
}

// ---- Discover: auto-apply loop (decision 069) --------------------------------
// Poll /loop/status while the loop runs. The loop owns the browser, so preparation runs in the
// background (no window pops up); each prepared application appears as a "Ready to apply" card
// with an Apply ▶ button that submits just that one (armed, one-shot, confirmed first).
let LOOP_TIMER = null;
let LOOP_CARDS = new Map();  // app id -> {sig, node}: cards persist across polls so Review stays open
let _loopRunning = false;    // last polled state — a save in the settings popup says so when a run is live
async function pollLoop() {
  if (LOOP_TIMER) { clearTimeout(LOOP_TIMER); LOOP_TIMER = null; }
  let s;
  try { s = await (await fetch("/loop/status")).json(); }
  catch (e) { return; }
  renderLoop(s);
  if (s.running) LOOP_TIMER = setTimeout(pollLoop, 2000);
}

function renderLoop(s) {
  const start = $("loop-start"), stop = $("loop-stop"), status = $("loop-status"), ready = $("loop-ready");
  const running = !!s.running;
  _loopRunning = running;
  start.classList.toggle("hidden", running);
  stop.classList.toggle("hidden", !running);
  $("loop-rescan").disabled = running;
  $("loop-goal").disabled = running;
  $("loop-dry-run").disabled = running;
  // "Keep topping up" only means something with a goal set, and only in a dry run — an apply-mode
  // loop submits the ready ones itself, so the count never drops back below the goal.
  $("loop-maintain").disabled = running || !$("loop-goal").value.trim() || !$("loop-dry-run").checked;
  $("loop-watch").disabled = running;
  // The re-check interval only applies when "Keep watching" is on.
  $("loop-watch-interval").disabled = running || !$("loop-watch").checked;
  // Watching each submit is meaningless in a dry run — it submits nothing (decision 179).
  loopSyncShowBrowser(running);
  if (running || (s.message && s.phase !== "idle")) {
    status.classList.remove("hidden");
    status.className = "loopstat" + (s.phase === "error" ? " err" : "");
    status.innerHTML = "";
    if (running && s.phase !== "caught_up") status.appendChild(el("span", {class:"spin"}));
    status.appendChild(el("span", {text: s.message || (running ? "Working…" : "")}));
    if (s.prepared) status.appendChild(el("span", {class:"lp-count", text: s.prepared + " prepared"}));
    if (s.submitted) status.appendChild(el("span", {class:"lp-count", text: s.submitted + " submitted"}));
  } else {
    status.classList.add("hidden");
  }
  renderLoopScan(s);
  const list = (s && s.ready) || [];
  // Reuse each card's DOM node across the 2s poll instead of rebuilding the list — a card is
  // rebuilt only when its own facts change. (The open review is a popup of its own since
  // decision 184, so a rebuild can no longer wipe it; this just keeps the list from flickering.)
  const keep = new Map();
  const nodes = list.map(a => {
    const sig = JSON.stringify([a.company, a.role, a.fit, a.portal, a.resume_source]);
    const prev = LOOP_CARDS.get(a.id);
    const node = (prev && prev.sig === sig) ? prev.node : loopReadyCard(a);
    keep.set(a.id, {sig, node});
    return node;
  });
  LOOP_CARDS = keep;
  ready.innerHTML = "";
  if (list.length) {
    // The list is the durable one (decision 183), so it can hold applications this run didn't
    // prepare — an earlier run's, or ones that outlived a restart. Goal progress counts only
    // THIS run (`ready_run`); the rest are named separately so neither number is a lie.
    const run = (s.ready_run == null) ? list.length : s.ready_run;
    const held = list.length - run;
    let head;
    if (s.goal) head = "Ready to apply (" + run + " of " + s.goal + " goal"
                       + (s.maintain ? ", topping up)" : ")")
                       + (held ? " · " + held + " more prepared earlier" : "");
    else if (!running) head = "Ready to apply (" + list.length + ") — prepared earlier, waiting for you";
    else head = "Ready to apply (" + list.length + ")"
                + (held ? " · " + held + " prepared earlier" : "");
    ready.appendChild(el("div", {class:"loop-ready-head", text: head}));
    nodes.forEach(n => ready.appendChild(n));
  }
}

// Search breakdown for the loop (decision 149) — the same funnel + judged list the one-shot dry
// run shows, so a running loop reports WHERE its postings went instead of only "searching…".
// Rendered from the last search's stats, and only when they CHANGE: the panel is polled every 2s
// and a blind re-render would re-open a funnel the user just collapsed.
let LOOP_SCAN_SIG = "";
function renderLoopScan(s) {
  const scan = $("loop-scan"), judged = $("loop-judged");
  const rows = (s && s.judged) || [], f = (s && s.funnel) || {};
  if (!s || (!rows.length && !f.discovered)) {
    scan.classList.add("hidden"); judged.classList.add("hidden"); LOOP_SCAN_SIG = ""; return;
  }
  const sig = JSON.stringify([s.searches, s.scanned, s.matched, s.cleared, s.min_fit, f, rows.length]);
  if (sig === LOOP_SCAN_SIG) return;
  LOOP_SCAN_SIG = sig;
  const bits = [];
  if (s.searches) bits.push("Search " + s.searches);
  if (s.scanned) bits.push("scanned " + s.scanned + " postings");
  if (s.matched != null) bits.push(s.matched + " matched your skills");
  bits.push(rows.length + " judged by Claude");
  bits.push((s.cleared || 0) + " cleared your cutoff → prepared");
  scan.classList.remove("hidden");
  scan.innerHTML = `<div class="tmeta">${escapeHtml(bits.join(" · "))}</div>`
                 + (s.from_cache ? `<div class="tmeta cache">♻ Re-used the last saved search — no boards were re-read and nothing was re-judged.</div>` : "")
                 + renderScanFunnel(s.funnel, rows, s.min_fit);
  if (rows.length) { judged.classList.remove("hidden"); judged.innerHTML = renderJudged(s); }
  else judged.classList.add("hidden");
}

// Résumé provenance chip (decision 144): a small badge saying whether a run's résumé was freshly
// tailored or reused. `source` is the human string persisted on the tracker row; empty → no chip
// (older rows / not yet run). "Reused …" → amber "Reused" badge; anything else → green "Tailored".
// The full sentence is the hover title so the user can see exactly which résumé and why.
function resumeSrcChip(source) {
  if (!source) return null;
  // "Your …" = sent with no tailoring at all (decision 174) — a third state, not a reuse: the
  // chip must not claim a tailoring pass that never ran.
  if (String(source).startsWith("Your "))
    return el("span", {class:"rsrc rsrc-asis", title:source, text:"Untailored résumé"});
  const reused = String(source).startsWith("Reused");
  return el("span", {class: "rsrc " + (reused ? "rsrc-reuse" : "rsrc-fresh"),
    title: source, text: reused ? "Reused résumé" : "Tailored résumé"});
}

function loopReadyCard(a) {
  const title = (a.company || "—") + (a.role ? " — " + a.role : "");
  const tags = [];
  if (a.fit != null) tags.push("fit " + a.fit);
  if (a.portal) tags.push(a.portal);
  const head = el("div", {class:"pk-head"}, [
    el("span", {class:"pk-title", text:title}),
    tags.length ? el("span", {class:"pk-tag", text:tags.join(" · ")}) : null,
    // Résumé provenance (decision 144): a chip so "reused" is visible before the user opens Review.
    resumeSrcChip(a.resume_source)]);
  // Review before you sign off: the Apply button lives inside the reviewed panel, so a real
  // submit is always one deliberate step past seeing exactly what will be sent.
  const review = el("button", {class:"review-toggle", type:"button", text:"Review",
    title:"See the exact answers, résumé and posting before you submit — opens over the page",
    on:{click:()=>openReview(a.id, title)}});
  return el("div", {class:"pkcard"}, [head, el("div", {class:"pk-actions"}, [review])]);
}

// The application whose review the popup currently holds (null = closed). Every late-arriving
// render — the first load, a rescan that finished minutes later — checks this before painting, so
// a response for a review the user has since closed or swapped away from is dropped.
let REVIEW_OPEN = null;

// Open one application's review in the popup (decision 184). Loaded fresh each time — a card can
// be rescanned, re-tailored or edited between opens, and a stale panel would misreport what will
// be submitted. The heavy artifacts — résumé PDF, filled-form screenshot — are still opened on
// demand from their own routes, never inlined.
async function openReview(id, title, signoff) {
  const panel = $("review-panel");
  REVIEW_OPEN = id;
  $("review-modal-title").textContent = title || "Review";
  $("review-modal").classList.remove("hidden");
  $("review-modal-x").focus();
  panel.innerHTML = "";
  panel.appendChild(el("div", {class:"loopstat"}, [el("span", {class:"spin"}),
    el("span", {text:"Loading review…"})]));
  try {
    const r = await (await fetch("/track/review?id=" + id)).json();
    if (REVIEW_OPEN !== id) return;  // closed, or another application opened, while this loaded
    if (r.error) { panel.innerHTML = ""; panel.appendChild(el("div", {class:"msg err", text:r.error})); return; }
    renderReview(panel, r, title, signoff);
  } catch (e) {
    if (REVIEW_OPEN !== id) return;
    panel.innerHTML = "";
    panel.appendChild(el("div", {class:"msg err", text:"Could not load the review: " + (e.message||e)}));
  }
}

// Close the review popup. Nothing is lost: answers are saved by Save answers (and auto-saved
// before any fill or submit), and reopening the card reloads the panel from the server.
function closeReview() {
  REVIEW_OPEN = null;
  $("review-modal").classList.add("hidden");
  $("review-panel").innerHTML = "";
}
$("review-modal-x").addEventListener("click", closeReview);
$("review-modal").addEventListener("click", (e) => { if (e.target === $("review-modal")) closeReview(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("review-modal").classList.contains("hidden")) closeReview();
});

// signoff: optional (r, title) => [Element] building the sign-off row (Watch/Submit buttons),
// so each card type keeps its own submit endpoint + messaging. Defaults to the goal-loop actions.
function renderReview(panel, r, title, signoff) {
  panel.innerHTML = "";
  // A rescan re-renders this panel; drop the old edit boxes' registration so a later save can
  // never write from inputs that are no longer on screen.
  delete REVIEW_EDITS[r.id];
  const p = r.posting || {};
  // Posting details — the facts we matched and are applying against.
  const meta = [];
  const addMeta = (label, v) => { if (v) meta.push(el("div", {class:"rv-meta"}, [
    el("span", {class:"rv-k", text:label}), el("span", {text:String(v)})])); };
  addMeta("Location", p.location);
  addMeta("Remote", p.remote);
  addMeta("Pay", p.pay);
  addMeta("Portal", p.portal);
  addMeta("Fit", p.fit);
  if (p.url) meta.push(el("div", {class:"rv-meta"}, [el("span", {class:"rv-k", text:"Link"}),
    el("a", {class:"linkbtn", href:p.url, target:"_blank", rel:"noopener",
             title:"Open this posting on its job board: " + p.url, text:"Open posting ↗"})]));
  panel.appendChild(el("div", {class:"rv-sec"}, [
    el("div", {class:"rv-h", text:"Posting"}), el("div", {class:"rv-metas"}, meta)]));

  // Artifact buttons: résumé (lazy — opens the PDF in a new tab; never rendered inline).
  const acts = [];
  if (r.has_resume) acts.push(el("a", {class:"rv-btn", href:"/track/resume?id=" + r.id,
    target:"_blank", rel:"noopener", text:"View tailored résumé ↗"}));
  else acts.push(el("span", {class:"rv-note", text:"No tailored résumé stored yet."}));
  if (r.has_screenshot) acts.push(el("a", {class:"rv-btn", href:"/track/screenshot?id=" + r.id,
    target:"_blank", rel:"noopener", text:"View filled form ↗"}));
  panel.appendChild(el("div", {class:"rv-sec"}, [el("div", {class:"rv-acts"}, acts)]));
  // Résumé provenance (decision 144): state whether this run reuses a résumé or tailored a fresh
  // one, before the user signs off — so a reused résumé is never a surprise found post-submit.
  if (p.resume_source) panel.appendChild(el("div", {class:"rv-sec"}, [
    el("div", {class:"rv-src"}, [resumeSrcChip(p.resume_source),
      el("span", {class:"rv-note", text:p.resume_source})])]));

  // The exact answers the bot will submit — every one EDITABLE (decision 153). An edit is saved
  // against this posting and replaces the bot's answer on the next fill, including the real
  // submit, so what is left in these boxes is what actually gets sent.
  const filled = r.filled || [];
  const unanswered = r.unanswered || [];
  const fieldWrap = el("div", {class:"rv-sec"});
  const edits = [];  // {label, inp, initial} — collected for save + the pre-submit auto-save
  fieldWrap.appendChild(el("div", {class:"rv-h",
    text:"Answers it will submit (" + filled.length + ")"}));
  // Required vs optional (decision 164): what MUST be answered before a submit goes through,
  // counted across both tables, so the user knows the size of the job before reading the rows.
  const rows = filled.concat(unanswered);
  const nReq = rows.filter(f => f.required === true).length;
  const nOpt = rows.filter(f => f.required === false).length;
  const nUnmarked = rows.length - nReq - nOpt;
  if (r.required_known) {
    fieldWrap.appendChild(el("div", {class:"rv-note", text:
      nReq + " required · " + nOpt + " optional"
      + (nUnmarked ? " · " + nUnmarked + " the form didn't mark" : "")
      + ". Only required fields have to be answered to submit."}));
  } else if (rows.length) {
    fieldWrap.appendChild(el("div", {class:"rv-note", text:
      "This fill didn't record which questions are required — click “Rescan questions” below to "
      + "re-read the form and mark them."}));
  }
  // Answers that don't fit their question (decision 166) — counted up front, because the failure
  // they catch looks fine field-by-field: a filled box holding the answer to a different question.
  const flagged = rows.filter(f => f.flag);
  if (flagged.length) {
    fieldWrap.appendChild(el("div", {class:"rv-h rv-warn", text:
      flagged.length + (flagged.length === 1 ? " answer doesn't" : " answers don't")
      + " match what the question asks — highlighted below with what to check. Edit them here; "
      + "your edit is what gets submitted."}));
  }
  if (filled.length) {
    const tbl = el("table", {class:"rv-fields"});
    filled.forEach(f => tbl.appendChild(answerRow(f.label, f.value, f.control, f.edited, edits, false, f)));
    fieldWrap.appendChild(tbl);
  } else {
    fieldWrap.appendChild(el("div", {class:"rv-note",
      text:"No filled fields recorded yet — re-fill this posting (dry-run) to capture what it will submit."}));
  }
  // Fields it could NOT answer — the pre-submit check blocks a real submit while any of these
  // is required. Editable too, so the fix is right here instead of a read-only warning.
  if (unanswered.length) {
    const open = unanswered.filter(u => !(u.value || "").trim()).length;
    // Blocking vs merely missing: only an unanswered REQUIRED field stops a real submit.
    const blocking = unanswered.filter(u => u.required === true && !(u.value || "").trim()).length;
    fieldWrap.appendChild(el("div", {class:"rv-h rv-warn",
      text:"Needs attention — unanswered (" + open + " of " + unanswered.length + ")"
        + (blocking ? " — " + blocking + " required, which block a real submit"
                    : (r.required_known && open ? " — none required, so a submit isn't blocked" : ""))}));
    const tbl = el("table", {class:"rv-fields"});
    unanswered.forEach(u => tbl.appendChild(
      answerRow(u.label, u.value, u.detail, u.edited, edits, true, u)));
    fieldWrap.appendChild(tbl);
  }
  if (edits.length) {
    const status = el("span", {class:"rv-note"});
    const save = el("button", {class:"rv-btn", type:"button", text:"Save answers",
      title:"Save your edits — the next fill of this application submits these values, and "
          + "reusable answers are added to your answer bank",
      on:{click:()=>saveAnswers(r.id)}});
    REVIEW_EDITS[r.id] = {edits: edits, btn: save, status: status};
    fieldWrap.appendChild(el("div", {class:"rv-acts rv-save"}, [save, status]));
    fieldWrap.appendChild(el("div", {class:"rv-note",
      text:"Edit any answer above — it replaces the bot's own answer the next time this "
         + "application is filled, including the real submit, and reusable answers are added to "
         + "your answer bank so future applications don't ask again. Unsaved edits are saved for "
         + "you when you click Watch it fill or Apply."}));
  }
  // Rescan (decision 164): re-read the posting's form so the questions, their control types,
  // their required marks and the bot's answers are current. Headless dry-run — never submits.
  const rescanNote = el("span", {class:"rv-note"});
  if (RESCAN_MSG[r.id]) {  // outcome of the rescan that just rebuilt this panel
    rescanNote.className = "rv-note rv-ok";
    rescanNote.textContent = RESCAN_MSG[r.id];
    delete RESCAN_MSG[r.id];
  }
  const rescanBtn = el("button", {class:"rv-btn", type:"button", text:"Rescan questions",
    title:"Re-read this posting's form in the background — refreshes every question, whether it's "
        + "required, and the answers. No window opens and nothing is submitted.",
    on:{click:()=>rescanReview(r.id, rescanBtn, rescanNote, panel, title, signoff)}});
  // Tailoring is decided per application, right here (decision 180): the résumé THIS one will
  // submit is written for this posting, from the job description stored when it was prepared.
  // Says which of the two it is, so the button never hides what it's about to replace.
  const asIs = String((p && p.resume_source) || "").startsWith("Your ");
  const retailorBtn = el("button", {class:"rv-btn", type:"button",
    text: asIs ? "Tailor this résumé" : "Re-tailor résumé",
    title:(asIs ? "This application is set to send your résumé exactly as it is. Write it a résumé "
                + "tailored to this posting instead"
                : "Write this application a fresh résumé from this posting's job description, "
                + "replacing the one it has")
        + " — then re-fill the form in the background. Spends Claude usage; nothing is submitted.",
    on:{click:()=>rescanReview(r.id, retailorBtn, rescanNote, panel, title, signoff, true)}});
  fieldWrap.appendChild(el("div", {class:"rv-acts rv-rescan"}, [rescanBtn, retailorBtn, rescanNote]));
  fieldWrap.appendChild(el("div", {class:"rv-note",
    text:(r.when ? "Form last read " + r.when.replace("T", " ") + ". " : "")
       + "Rescan when the posting has changed its form, or when a question above looks stale — it "
       + "re-reads the live form and refreshes the questions, their required marks and the answers. "
       + (asIs ? "Tailor this résumé" : "Re-tailor résumé")
       + " changes which résumé this one application submits; every other application keeps its own, "
       + "and the loop's default stays whatever ⚙ Loop settings says."}));
  panel.dataset.when = r.when || "";  // the rescan watches this for "the new report landed"
  panel.appendChild(fieldWrap);

  // The JD it tailored against (collapsible — long).
  if (r.jd) {
    const body = el("pre", {class:"rv-jd hidden", text:r.jd});
    const tog = el("button", {class:"rv-btn", type:"button", text:"Show job description ▾",
      on:{click:(ev)=>{ const h = body.classList.toggle("hidden");
        ev.target.textContent = (h ? "Show" : "Hide") + " job description " + (h ? "▾" : "▴"); }}});
    panel.appendChild(el("div", {class:"rv-sec"}, [tog, body]));
  }

  // Sign-off: a dry-run "watch it fill" and the real submit — both live INSIDE the reviewed
  // panel, so a submit is always one step past seeing the answers. The buttons (and which
  // endpoint they hit) are card-type specific, supplied by `signoff`.
  panel.appendChild(el("div", {class:"rv-signoff"}, (signoff || loopSignoff)(r, title)));

  // Close from the BOTTOM too (decision 179, kept for the popup): a full review is long, so after
  // reading the answers the ✕ in the header is a scroll away.
  panel.appendChild(el("div", {class:"rv-acts rv-collapse"}, [
    el("button", {class:"rv-btn", type:"button", text:"Close review",
      title:"Close this popup and go back to the list — nothing is lost, reopen it with Review",
      on:{click:()=>closeReview()}})]));
}

// Editable answers per open review panel: id -> {edits, btn, status}, so the submit/watch
// buttons can flush unsaved edits before the browser re-fills the form (decision 153).
const REVIEW_EDITS = {};

// One answer row: the label (plus how it was filled, or why it wasn't) and an input holding the
// value that will be submitted. A check-all-that-apply question gets the same checkbox widget the
// Profile screen uses — the answer is multi-valued there, so it must be here too. The résumé
// upload is not editable — the file is the answer.
function answerRow(label, value, note, edited, edits, isBlank, row) {
  const v = (value == null) ? "" : String(value);
  if (note === "file") return el("tr", {}, [
    el("td", {class:"rv-fl", text:label || "—"}),
    el("td", {class:"rv-fv", text:v || "—"})]);
  row = row || {};
  let cell, inp;
  if (isMultiAnswer(row.kind, row.options)) {
    const w = multiCheckboxes(row.options, v);
    cell = w.node; inp = w.hidden;
  } else if (isSingleChoice(row.kind, row.options)) {
    // The form offers a fixed list here — edit it as that list, not as free text (decision 165).
    const w = singleChoiceInput(row.options, v);
    cell = w.node; inp = w.hidden;
  } else {
    const long = v.length > 60;
    inp = long ? el("textarea", {class:"rv-edit", rows:"3", value:v})
               : el("input", {class:"rv-edit", type:"text", value:v});
    inp.placeholder = isBlank ? "Type the answer to submit…" : "";
    cell = inp;
  }
  edits.push({label: label, inp: inp, initial: v});
  const marks = [el("div", {text: label || "—"})];
  // Does the form make this one mandatory (decision 164)? Shown first — it decides whether the
  // user has to fill the row at all. Absent when the fill couldn't tell (no badge, never a guess).
  if (row.required === true) marks.push(el("span", {class:"rv-req", text:"Required",
    title:"The form marks this field required — a real submit is blocked until it's answered"}));
  else if (row.required === false) marks.push(el("span", {class:"rv-opt", text:"Optional",
    title:"The form does not require this field — it can be left blank"}));
  if (edited) marks.push(el("span", {class:"rv-edited", text:"your edit"}));
  else if (note && note !== "text") marks.push(el("span", {class:"rv-ctl", text:note}));
  // Who produced the answer, when it wasn't a straight lookup from your profile or answer bank.
  // A drafted or model-picked answer is the one most likely to be right in form and wrong in
  // context, so it says so on the row.
  if (!edited && row.source === "generated") marks.push(el("span", {class:"rv-ai",
    text:"AI-drafted", title:"Claude wrote this from your résumé — check it answers THIS question"}));
  else if (!edited && row.source === "option:claude") marks.push(el("span", {class:"rv-ai",
    text:"AI-picked", title:"Claude chose this from the options the form offered"}));
  // What a generic label was read from (decision 167) — "Date" alone is not reviewable, "Date"
  // under "Applicant certification · follows the field: Signature" is.
  if (row.context) marks.push(el("div", {class:"rv-around", text:"on the form: " + row.context,
    title:"The text around this field, which is how the bot worked out what it asks for"}));
  // The answer doesn't fit what the question asks (decision 166): badge + the reason, right
  // above the box that fixes it.
  if (row.flag) {
    marks.push(el("span", {class:"rv-flagbadge", text:"Check this", title:row.flag}));
    marks.push(el("div", {class:"rv-flagwhy", text:row.flag}));
  }
  return el("tr", {class: row.flag ? "rv-flagged" : ""},
    [el("td", {class:"rv-fl"}, marks), el("td", {class:"rv-fv"}, [cell])]);
}

// Save a review panel's changed answers. `quiet` = the pre-submit auto-save (no "nothing to
// save" chatter). Resolves {ok, changed} so a submit can abort if the save failed — submitting
// answers the user thinks they edited would be exactly the wrong outcome.
async function saveAnswers(id, quiet) {
  const st = REVIEW_EDITS[id];
  if (!st) return {ok:true, changed:0};
  const changed = {};
  st.edits.forEach(e => { const v = e.inp.value.trim();
    if (v !== e.initial.trim()) changed[e.label] = v; });
  const n = Object.keys(changed).length;
  if (!n) {
    if (!quiet) { st.status.className = "rv-note"; st.status.textContent = "No changes to save."; }
    return {ok:true, changed:0};
  }
  const label = st.btn.textContent;
  st.btn.disabled = true; st.btn.textContent = "Saving…";
  st.status.className = "rv-note"; st.status.textContent = "";
  try {
    const r = await (await fetch("/track/answers", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id: id, answers: changed })})).json();
    st.btn.disabled = false; st.btn.textContent = label;
    if (!r.ok) {
      st.status.className = "rv-note rv-err";
      st.status.textContent = r.error || "Could not save the answers.";
      return {ok:false, changed:0};
    }
    st.edits.forEach(e => { e.initial = e.inp.value; });
    st.status.className = r.learn_error ? "rv-note rv-err" : "rv-note rv-ok";
    // Say what was saved AND what was learned — an edit the bot can't reuse (company-specific,
    // EEO, or a field your profile owns) must not read as "learned" (UI Principle #5).
    let msg = "Saved ✓ — " + n + " edited answer" + (n === 1 ? "" : "s")
      + " will be submitted instead of the bot's.";
    if (r.learned) msg += " " + r.learned + " saved to your answer bank — future applications "
      + "asking " + (r.learned === 1 ? "it" : "them") + " are answered this way.";
    if (r.posting_only) msg += " " + r.posting_only + " kept for this posting only "
      + "(company-specific and EEO answers are never reused).";
    if (r.learn_error) msg += " " + r.learn_error;
    st.status.textContent = msg;
    const owned = r.profile_owned || [];
    if (owned.length) {
      st.status.appendChild(el("span", {text:" " + owned.join(", ")
        + (owned.length === 1 ? " is" : " are") + " answered from your apply profile, which "
        + "outranks the answer bank — this edit applies to this posting only. "}));
      st.status.appendChild(el("a", {href:"#", text:"Change it in Profile →",
        on:{click:(ev)=>{ ev.preventDefault();
          const t = document.querySelector('.tab[data-view="profile"]'); if (t) t.click(); }}}));
    }
    return {ok:true, changed:n};
  } catch (e) {
    st.btn.disabled = false; st.btn.textContent = label;
    st.status.className = "rv-note rv-err";
    st.status.textContent = "Could not save the answers: " + (e.message || e);
    return {ok:false, changed:0};
  }
}

// Outcome of a rescan, handed to the panel the rescan itself rebuilds: id -> message. Read and
// cleared by renderReview, so the result lands next to the button that was clicked.
const RESCAN_MSG = {};

// Re-read one posting's application form, then re-render this panel from the fresh report
// (decision 164) — the questions the form asks now, their control types, which ones it marks
// REQUIRED, and the answers the bot produces today. Headless dry-run: no window opens and nothing
// is ever submitted. It runs on the loop thread while the loop is running (queued behind its
// current step) and immediately otherwise; either way it has landed once this application's
// archived report carries a NEW timestamp, which is what this polls for.
//
// `retailor=true` (decision 180) is the same job with a new résumé written for this posting first
// — the per-application tailoring control. Only the labels and the request body differ; the
// landed-yet? polling is identical, because it is the same refresh.
async function rescanReview(id, btn, note, panel, title, signoff, retailor) {
  const before = panel.dataset.when || "";
  // The re-render replaces every edit box, so flush unsaved edits first — and they're also what
  // the rescan's own fill should submit (decision 153).
  const saved = await saveAnswers(id, true);
  if (!saved.ok) {
    note.className = "rv-note rv-err";
    note.textContent = "Your edited answers could not be saved, so nothing was rescanned. Fix the "
      + "error above the Save answers button, then try again.";
    return;
  }
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = retailor ? "Tailoring…" : "Rescanning…";
  note.className = "rv-note";
  const working = retailor ? "Writing this application a résumé for the posting, then re-filling… "
                           : "Re-reading the form… ";
  note.textContent = working;
  const t0 = Date.now();
  let queued = false;
  const tick = setInterval(() => {
    note.textContent = (queued
      ? (retailor ? "Queued — the running loop re-tailors this at its next step… "
                  : "Queued — the running loop rescans this at its next step… ")
      : working) + Math.round((Date.now() - t0)/1000) + "s";
  }, 1000);
  const stop = (cls, msg) => {
    clearInterval(tick);
    btn.disabled = false; btn.textContent = label;
    note.className = "rv-note" + (cls ? " " + cls : ""); note.textContent = msg;
  };
  const wait = ms => new Promise(res => setTimeout(res, ms));
  try {
    const r = await (await fetch("/track/rescan", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({id: id, retailor: !!retailor})})).json();
    if (!r.ok) { stop("rv-err", r.error || (retailor ? "Could not start the re-tailor."
                                                     : "Could not start the rescan.")); return; }
    queued = !!r.queued;
    const deadline = Date.now() + 10 * 60 * 1000;  // a long form + Claude drafting can take minutes
    while (Date.now() < deadline) {
      await wait(2000);
      let fresh = null;
      try { fresh = await (await fetch("/track/review?id=" + id)).json(); } catch (e) { fresh = null; }
      if (fresh && !fresh.error && (fresh.when || "") !== before) {
        clearInterval(tick);
        const open = (fresh.unanswered || []).filter(u => !(u.value || "").trim()).length;
        RESCAN_MSG[id] = (retailor
          ? "Re-tailored ✓ — this application now submits a résumé written for this posting, and "
            + "the form was re-filled with it: "
          : "Rescanned ✓ — ") + (fresh.filled || []).length + " answer(s) ready, "
          + open + " unanswered. Nothing was submitted.";
        // The popup holds one review at a time: if the user closed it or opened another
        // application while this ran, don't paint this one over theirs — the message is kept in
        // RESCAN_MSG and shown when they reopen this application (decision 184).
        if (REVIEW_OPEN === id) renderReview(panel, fresh, title, signoff);
        return;
      }
      if (!queued) {
        // A direct run reports its own failure (browser launch, dead posting) — surface it
        // instead of spinning until the timeout.
        let s = null;
        try { s = await (await fetch("/test-run/status")).json(); } catch (e) { s = null; }
        if (s && s.phase === "error") {
          stop("rv-err", (retailor ? "The re-tailor failed: " : "The rescan failed: ")
            + ((s.errors || []).join(" ") || "see Discover for details."));
          return;
        }
      }
    }
    stop("rv-err", (retailor ? "The re-tailor" : "The rescan")
      + " hasn't finished after 10 minutes. Check the Discover tab for the run's status, then try "
      + "again.");
  } catch (e) {
    stop("rv-err", (retailor ? "Could not re-tailor: " : "Could not rescan: ") + (e.message || e));
  }
}

// Goal-loop Ready cards: watch via /loop/watch, submit via /loop/apply (armed, decision 058).
function loopSignoff(r, title) {
  return [
    el("button", {class:"rv-btn", type:"button", text:"Watch it fill",
      title:"Open a browser and watch the autofill — a dry-run; nothing is submitted",
      on:{click:(ev)=>watchReady(r.id, ev.target, title)}}),
    // Watch the real thing go in (decision 179): the same submit as Apply, in a visible browser
    // that stays open on the confirmation page.
    el("button", {class:"loop-apply", type:"button", text:"Watch it apply ▶",
      title:"Submit this application (irreversible) in a browser you can watch — the window stays "
          + "open on the result until you close it. Confirms first.",
      on:{click:(ev)=>applyReady(r.id, ev.target, title, true)}}),
    el("button", {class:"loop-apply", type:"button", text:"Apply ▶",
      title:"Submit this one application (irreversible) — confirms first",
      on:{click:(ev)=>applyReady(r.id, ev.target, title)}})];
}

// Parked ("waiting on you") cards: the same preview, but the buttons drive the parked flow
// (/parked/reapply) — dry-run re-fill (visible, so it doubles as "watch it fill") and the
// per-click armed "Submit for real" (decision 058). Keeps each flow's endpoint + messaging.
function parkedSignoff(r, title) {
  return [
    el("button", {class:"rv-btn", type:"button", text:"Re-apply (dry-run) ▶",
      title:"Re-fill this posting in a browser you can watch — never submits",
      on:{click:(ev)=>reapplyParked(r.id, ev.target, false)}}),
    el("button", {class:"loop-apply", type:"button", text:"Submit for real ▶",
      title:"Actually submit this application (irreversible) — confirms first",
      on:{click:(ev)=>reapplyParked(r.id, ev.target, true, title)}})];
}

// Watch one prepared application autofill: a visible dry-run that never submits. Routed through
// the loop's queue while it runs (the loop opens the browser at its next step); run directly when
// the loop is idle (drives the shared test-progress panel below).
async function watchReady(id, btn, who) {
  ensureDiscoverVisible();  // progress + the browser-fill status render in Discover — show them
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Opening…"; }
  const msg = $("loop-msg"); msg.className = "msg"; msg.textContent = "";
  // Fill with what the user sees in the review panel — save any unsaved edits first (decision 153).
  const saved = await saveAnswers(id, true);
  if (!saved.ok) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    msg.className = "msg err";
    msg.textContent = "Your edited answers could not be saved, so nothing was filled. Fix the error above the Save answers button, then try again.";
    return;
  }
  // The answers are saved, so the review has done its job — close the popup (decision 184). What
  // happens next, success or error, is reported in Discover, which the popup would otherwise cover.
  closeReview();
  try {
    const r = await (await fetch("/loop/watch", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id })})).json();
    if (btn) { btn.disabled = false; btn.textContent = label; }
    if (!r.ok) { msg.className = "msg err"; msg.textContent = r.error || "Could not start the watch."; return; }
    if (r.queued) { msg.textContent = "Queued — the loop will open the browser to fill this one at its next step. Nothing is submitted."; }
    else { pollTest(); }  // loop idle → start_reapply drives the shared progress panel
    pollLoop();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    msg.className = "msg err"; msg.textContent = String(e.message || e);
  }
}

// Submit one prepared application. While the loop runs it's queued for the loop thread (which owns
// the browser); if the loop is idle it falls back to the per-click armed re-apply, which drives the
// shared test-progress panel below. Always confirms first (irreversible).
//
// `watch=true` (decision 179) is the same submit in a browser you can watch: it hits
// /loop/watch-apply, which runs the armed submit headed and leaves the window open on the result.
async function applyReady(id, btn, who, watch) {
  const ok = confirm("Really SUBMIT this application" + (who ? " to " + who : "") + "?\\n\\n"
    + "This is a real, irreversible submission. The bot fills the form and clicks Submit; the "
    + "pre-submit check still stops it if a required field is unanswered."
    + (watch ? "\\n\\nA browser window opens so you can watch it happen, and stays open on the "
             + "result until you close it." : ""));
  if (!ok) return;
  ensureDiscoverVisible();  // the submit progress renders in Discover — show it, never submit silently
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = watch ? "Opening…" : "Submitting…"; }
  const msg = $("loop-msg"); msg.className = "msg"; msg.textContent = "";
  // A real submit must send exactly the answers shown — save unsaved edits or stop (decision 153).
  const saved = await saveAnswers(id, true);
  if (!saved.ok) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    msg.className = "msg err";
    msg.textContent = "Your edited answers could not be saved, so nothing was submitted. Fix the error above the Save answers button, then try again.";
    return;
  }
  // Exactly the answers just reviewed are on their way — close the popup (decision 184) so the
  // submit's own progress and any error are visible in Discover instead of behind it.
  closeReview();
  try {
    const r = await (await fetch(watch ? "/loop/watch-apply" : "/loop/apply",
      {method:"POST", headers:{"Content-Type":"application/json"},
       body: JSON.stringify({ id })})).json();
    if (!r.ok) {
      if (btn) { btn.disabled = false; btn.textContent = label; }
      msg.className = "msg err"; msg.textContent = r.error || "Could not submit.";
      return;
    }
    if (r.queued) {
      if (btn) btn.textContent = "Queued…";
      if (watch) {
        msg.textContent = "Queued — the loop opens the browser and submits this one at its next "
          + "step. Watch it fill and click Submit; close the window when you're done.";
      }
    }
    else { pollTest(); }  // loop idle → start_reapply drives the shared progress panel
    pollLoop();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    msg.className = "msg err"; msg.textContent = String(e.message || e);
  }
}

// "Show the browser while it applies" shows each SUBMIT, and a dry run submits nothing — so the
// box is off and disabled there, saying why rather than promising a window that never opens
// (decision 179, UI Principle #3/#4).
function loopSyncShowBrowser(running) {
  const dry = $("loop-dry-run").checked;
  const box = $("loop-show-browser"), hint = $("loop-show-browser-hint");
  box.disabled = !!running || dry;
  if (dry) box.checked = false;
  hint.textContent = dry
    ? "A dry run submits nothing, so there is no submit to watch. To watch a dry-run fill instead, "
      + "use “Watch it fill” inside a ready application's Review."
    : "Every application is filled and submitted in a window you can watch, which then closes "
      + "itself and the loop moves on. Slower per application; nothing else about the submit "
      + "changes. To watch just one, use “Watch it apply ▶” inside that application's Review.";
}

// "Keep topping up" is meaningless without a goal, and meaningless outside a dry run (an
// apply-mode loop applies to the ready ones itself) — gate it on both, live.
function loopSyncMaintain() {
  const off = !$("loop-goal").value.trim() || !$("loop-dry-run").checked;
  $("loop-maintain").disabled = off;
  if (off) $("loop-maintain").checked = false;
}
$("loop-goal").addEventListener("input", loopSyncMaintain);

// The panel must say what Start will actually DO before it is clicked (UI Principle #1/#3):
// swap the blurb between "applies for you" and "prepares only" as the switch is toggled.
$("loop-dry-run").addEventListener("change", () => {
  const dry = $("loop-dry-run").checked;
  $("loop-blurb-live").classList.toggle("hidden", dry);
  $("loop-blurb-dry").classList.toggle("hidden", !dry);
  $("loop-start").textContent = dry ? "▶ Start loop (dry run)" : "▶ Start applying";
  loopSyncMaintain();
  loopSyncShowBrowser(_loopRunning);
});

$("loop-watch").addEventListener("change", () => {
  // The re-check interval only applies when "Keep watching" is on.
  $("loop-watch-interval").disabled = !$("loop-watch").checked;
});

$("loop-start").addEventListener("click", async () => {
  const btn = $("loop-start"), msg = $("loop-msg");
  // Apply mode sends real applications with no further click, so starting it IS the arming step
  // (Agent Guideline #3) — confirm once, here, and never again per application.
  if (!$("loop-dry-run").checked) {
    const goalTxt = $("loop-goal").value.trim();
    const ok = confirm("Start applying for real?\\n\\n"
      + "The loop will tailor, fill and SUBMIT " + (goalTxt ? goalTxt + " application(s)" : "every match it finds")
      + " with no further confirmation. Submissions are irreversible.\\n\\n"
      + ($("loop-show-browser").checked
          ? "Each one is submitted in a browser window you can watch.\\n\\n" : "")
      + "Stop ends it after the current step. Tick “Dry run” instead to prepare without submitting.");
    if (!ok) return;
  }
  msg.className = "msg"; msg.textContent = "";
  btnBusy(btn, "Starting…");
  try {
    // Re-tailoring is no longer a per-run checkbox — it's the "Always re-tailor" choice in Loop
    // settings, which the worker reads from the saved config (decision 178).
    const rescan = $("loop-rescan").checked, retailor = false;
    const goalRaw = $("loop-goal").value.trim();
    const goal = goalRaw ? parseInt(goalRaw, 10) : null;
    const maintain = $("loop-maintain").checked;
    const watch = $("loop-watch").checked;
    const dry_run = $("loop-dry-run").checked;
    const wiRaw = $("loop-watch-interval").value.trim();
    const watch_interval = wiRaw ? parseInt(wiRaw, 10) : 30;
    // Show the browser for every submit, so the user can watch each application go in (179).
    const show_browser = $("loop-show-browser").checked;
    const r = await (await fetch("/loop/start", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({rescan, retailor, goal, maintain, watch, watch_interval, dry_run, show_browser})})).json();
    btnDone(btn);
    if (!r.ok) { msg.className = "msg err"; msg.textContent = r.error || "Could not start."; return; }
    pollLoop();
  } catch (e) { btnDone(btn); msg.className = "msg err"; msg.textContent = String(e.message || e); }
});

// ---- Loop settings popup (decision 178) --------------------------------------------------
// Everything that governs a run but isn't a per-run choice: which résumé each application gets,
// the fit cutoff, résumé reuse, and the submission cap. Loaded from the server every time it opens
// (never from a stale cache), saved to profile/discovery.yaml + profile/safety.yaml.
let LSET_PREV = null;
function lsetSyncBelow() {
  // The fit threshold only means something for the "only tailor when under N" choice.
  const on = document.querySelector('input[name="lset-tailor"]:checked');
  $("lset-below").disabled = !(on && on.value === "under");
}
// Every control in the popup, so they can be locked while the saved values are still loading —
// typing into a field the pending load is about to overwrite would silently discard the edit.
function lsetControls() {
  return [...document.querySelectorAll('#loop-modal input'), $("loop-settings-save")];
}
async function openLoopSettings() {
  const m = $("loop-modal"), msg = $("loop-settings-msg");
  LSET_PREV = document.activeElement;
  m.classList.remove("hidden");
  $("loop-modal-x").focus();
  lsetControls().forEach(c => { c.disabled = true; });
  msg.className = "msg busy"; msg.textContent = "Loading your settings…";
  try {
    const s = await (await fetch("/loop/settings")).json();
    if (!s.ok) throw new Error(s.error || "could not load");
    const mode = s.tailor_mode || "smart";
    document.querySelectorAll('input[name="lset-tailor"]').forEach(r => { r.checked = (r.value === mode); });
    $("lset-below").value = s.tailor_below_fit;
    $("lset-minfit").value = s.min_fit;
    $("lset-reuse").value = Math.round((s.reuse_threshold || 0) * 100);
    $("lset-cap").value = s.max_submissions_per_run;
    lsetControls().forEach(c => { c.disabled = false; });
    lsetSyncBelow();   // re-disables the threshold box unless "under" is the chosen mode
    // Outcome calibration can raise the effective cutoff above what's typed here — say so rather
    // than letting the number silently under-report the bar the loop actually applies.
    $("lset-minfit-hint").textContent = s.calib_note
      ? s.calib_note
      : "Claude scores every posting 0-100; the loop only prepares ones at or above this. The same "
        + "setting as min_fit in Discovery settings — saving here saves there.";
    msg.className = "msg"; msg.textContent = "";
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = "Couldn't load your settings — " + String(e.message || e) + ". Close and reopen to retry.";
  }
}
function closeLoopSettings() {
  $("loop-modal").classList.add("hidden");
  if (LSET_PREV && LSET_PREV.focus) LSET_PREV.focus();
}
$("loop-settings-open").addEventListener("click", openLoopSettings);
$("loop-settings-link").addEventListener("click", (e) => { e.preventDefault(); openLoopSettings(); });
$("loop-modal-x").addEventListener("click", closeLoopSettings);
$("loop-modal").addEventListener("click", (e) => { if (e.target === $("loop-modal")) closeLoopSettings(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("loop-modal").classList.contains("hidden")) closeLoopSettings();
});
document.querySelectorAll('input[name="lset-tailor"]').forEach(r => r.addEventListener("change", lsetSyncBelow));

$("loop-settings-save").addEventListener("click", async () => {
  const btn = $("loop-settings-save"), msg = $("loop-settings-msg");
  btnBusy(btn, "Saving…");
  msg.className = "msg busy";
  const stop = busyInto(msg, "Saving loop settings…", false);
  try {
    const on = document.querySelector('input[name="lset-tailor"]:checked');
    const r = await (await fetch("/loop/settings", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({data: {
        tailor_mode: on ? on.value : "smart",
        tailor_below_fit: parseInt($("lset-below").value, 10) || 70,
        min_fit: parseInt($("lset-minfit").value, 10) || 0,
        reuse_threshold: (parseInt($("lset-reuse").value, 10) || 0) / 100,
        max_submissions_per_run: parseInt($("lset-cap").value, 10) || 1,
      }})})).json();
    stop();
    if (!r.ok) throw new Error(r.error || "save failed");
    // Show what actually landed — the server clamps out-of-range values.
    $("lset-minfit").value = r.min_fit;
    $("lset-reuse").value = Math.round((r.reuse_threshold || 0) * 100);
    $("lset-cap").value = r.max_submissions_per_run;
    msg.className = "msg ok";
    msg.textContent = _loopRunning ? "Saved ✓ — the run in progress keeps the settings it started with."
                                   : "Saved ✓";
  } catch (e) {
    stop(); msg.className = "msg err";
    msg.textContent = "Couldn't save — " + String(e.message || e);
  } finally { btnDone(btn); }
});

$("loop-stop").addEventListener("click", async () => {
  const btn = $("loop-stop"); btnBusy(btn, "Stopping…");
  try { await fetch("/loop/stop", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}); }
  catch (e) {}
  btnDone(btn);
  pollLoop();
});

// ---- Settings: push-notification settings (decision 135) ----
function ntfCollect() {
  return {
    desktop: $("ntf-desktop").checked,
    ntfy: {enabled: $("ntf-ntfy").checked, topic: $("ntf-topic").value.trim()},
    events: {approval_needed: $("ntf-ev-approval").checked,
             intervention_needed: $("ntf-ev-intervention").checked},
  };
}
function ntfSyncTopic() {
  // The topic field is only meaningful when ntfy is on — dim it otherwise.
  $("ntf-topic-wrap").style.opacity = $("ntf-ntfy").checked ? "1" : ".5";
  $("ntf-topic").disabled = !$("ntf-ntfy").checked;
}
function ntfDesktopHint(dc) {
  // Show the "install terminal-notifier" tip only when clicking a desktop notification can't open
  // the app (from source, terminal-notifier absent). Hidden in the packaged app / when installed.
  const hint = $("ntf-desktop-hint"); if (!hint) return;
  const show = dc && dc.applicable && !dc.clickable;
  hint.hidden = !show;
  if (show && dc.install) $("ntf-tn-cmd").textContent = dc.install;
}
async function loadNotifications() {
  try {
    const {config: c, desktop_click: dc} = await (await fetch("/notifications")).json();
    ntfDesktopHint(dc);
    $("ntf-desktop").checked = !!c.desktop;
    $("ntf-ntfy").checked = !!(c.ntfy && c.ntfy.enabled);
    $("ntf-topic").value = (c.ntfy && c.ntfy.topic) || "";
    $("ntf-ev-approval").checked = !!(c.events && c.events.approval_needed);
    $("ntf-ev-intervention").checked = !!(c.events && c.events.intervention_needed);
    ntfSyncTopic();
  } catch (e) { /* leave defaults; the panel is optional */ }
}
async function saveNotifications(btn) {
  const msg = $("ntf-msg"); msg.textContent = ""; msg.style.color = "";
  const data = ntfCollect();
  if (data.ntfy.enabled && !data.ntfy.topic) {
    msg.textContent = "Enter an ntfy topic, or turn off phone push."; msg.style.color = "var(--bad)"; return; }
  btnBusy(btn, "Saving…");
  try {
    const r = await (await fetch("/notifications/update", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({data})})).json();
    msg.textContent = r.ok ? "Saved ✓" : "Could not save."; msg.style.color = r.ok ? "var(--ok-text)" : "var(--bad)";
  } catch (e) { msg.textContent = "Failed: " + e.message; msg.style.color = "var(--bad)"; }
  finally { btnDone(btn); }
}
async function testNotifications(btn) {
  const msg = $("ntf-msg"); msg.textContent = ""; msg.style.color = "";
  const data = ntfCollect();
  if (data.ntfy.enabled && !data.ntfy.topic) {
    msg.textContent = "Enter an ntfy topic first, or turn off phone push."; msg.style.color = "var(--bad)"; return; }
  btnBusy(btn, "Sending…");
  try {
    const r = await (await fetch("/notifications/test", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({data})})).json();
    msg.textContent = (r.ok ? "✓ " : "⚠ ") + (r.message || ""); msg.style.color = r.ok ? "var(--ok-text)" : "var(--bad)";
  } catch (e) { msg.textContent = "Failed: " + e.message; msg.style.color = "var(--bad)"; }
  finally { btnDone(btn); }
}
$("ntf-ntfy").addEventListener("change", ntfSyncTopic);
$("ntf-save").addEventListener("click", () => saveNotifications($("ntf-save")));
$("ntf-test").addEventListener("click", () => testNotifications($("ntf-test")));
$("ntf-tn-copy").addEventListener("click", async (e) => {
  try { await navigator.clipboard.writeText($("ntf-tn-cmd").textContent); e.target.textContent = "Copied ✓"; }
  catch (err) { e.target.textContent = "Copy failed"; }
  setTimeout(() => { e.target.textContent = "Copy"; }, 1500);
});

// ---- Settings tab loader: Claude connection, notifications, linked inbox, and appearance ----
let SETTINGS_MB_MOUNTED = false;
function loadSettings() {
  refreshAuth();          // Claude connection section (also refreshes the footer chip)
  loadNotifications();    // notification toggles
  reflectTheme();         // highlight the active theme button
  // Inbox panel: build it once into the Settings mount (its buttons bind at build time), then
  // (re)load its live connection status each visit.
  if (!SETTINGS_MB_MOUNTED) {
    const mount = $("set-mailbox-mount");
    if (mount) { mount.innerHTML = ""; mount.appendChild(mailboxPanel()); SETTINGS_MB_MOUNTED = true; }
  }
  loadMailbox();
}

// ---- Notifications tab: one action center for everything needing the user (decision 138) ----
// Reuses the exact Discover cards (parkedCard / loopReadyCard) so a fix here is identical to a fix
// there; the count feeds the nav badge. The submit/watch/resolve actions run through the shared
// Discover progress panels, so those functions hop to Discover first (ensureDiscoverVisible) —
// never a silent submit into a hidden panel (UI Principle #5).
function updateBadge(n) {
  const b = $("notif-badge"); if (!b) return;
  if (n > 0) { b.textContent = n > 99 ? "99+" : String(n); b.hidden = false; }
  else b.hidden = true;
}
async function refreshBadge() {
  // The badge counts things needing you NOW — applications ready to submit + blocked ones needing
  // a fix (decision 138). It falls as you submit/resolve them; it does not clear just from opening
  // the tab (the notification LOG below the cards is a separate, dismissible record — decision 145).
  try { const d = await (await fetch("/inbox")).json(); updateBadge(d.count || 0); } catch (e) {}
}
function ensureDiscoverVisible() {
  if ($("view-discover").classList.contains("hidden")) {
    const t = document.querySelector('.tab[data-view="discover"]'); if (t) t.click();
  }
}
function nfRelTime(iso) {
  if (!iso) return "";
  const t = new Date(String(iso).replace(" ", "T")); const s = (Date.now() - t.getTime()) / 1000;
  if (isNaN(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
async function dismissNotif(id, rowEl) {
  try {
    await fetch("/notifications/dismiss", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ids:[id]})});
    if (rowEl) rowEl.remove();
    refreshBadge();
  } catch (e) {}
}
async function clearNotifs() {
  try {
    await fetch("/notifications/dismiss", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ids:null})});
    loadInbox();
  } catch (e) {}
}
function notifRow(n) {
  const dot = el("span", {class:"nf-dot" + (n.urgent ? " urgent" : "") + (n.read ? "" : " unread")});
  const meta = el("div", {class:"nf-meta"}, [
    el("span", {class:"nf-title", text:n.title}),
    el("span", {class:"nf-time", text:nfRelTime(n.created_at)})]);
  // If the application this notification was about is already handled (submitted/applied/skipped),
  // say so — the record stays, but it's clearly no longer awaiting action.
  const st = n.app_status;
  if (st && st !== "dry-run" && st !== "blocked")
    meta.appendChild(el("span", {class:"nf-tag", text:st}));
  const main = el("div", {class:"nf-main"}, [meta, el("div", {class:"nf-body", text:n.body})]);
  // Actionable → an inline jump to this application's card at the top, where Review/Submit/Resolve
  // live. This is the "review and submit" the push promised, from the notification itself.
  if (n.actionable && n.application_id) {
    const label = st === "blocked" ? "Resolve →" : "Review & submit →";
    main.appendChild(el("button", {class:"nf-act", type:"button", text:label,
      on:{click:()=>jumpToCard(n.application_id)}}));
  }
  const x = el("button", {class:"nf-x", type:"button", title:"Dismiss", "aria-label":"Dismiss", text:"✕"});
  const row = el("div", {class:"nfrow" + (n.read ? "" : " unread")}, [dot, main, x]);
  x.addEventListener("click", () => dismissNotif(n.id, row));
  return row;
}
async function loadInbox() {
  const body = $("notif-body");
  body.innerHTML = ""; body.appendChild(el("div", {class:"loopstat"}, [el("span", {class:"spin"}), el("span", {text:"Loading…"})]));
  let d;
  try { d = await (await fetch("/inbox")).json(); }
  catch (e) { body.innerHTML = ""; body.appendChild(el("div", {class:"msg err", text:"Could not load your notifications: " + String(e.message || e)})); return; }
  const ready = d.ready || [], parked = d.parked || [];
  // Every notification is shown (decision 145) — a push that says "open Notifications to review
  // and submit" must actually appear here. Actionable ones get an inline "Review/Resolve →" that
  // jumps to their card at the top (tagged with an id below); handled ones are a plain record.
  const feed = d.notifications || [];
  body.innerHTML = "";
  if (!ready.length && !parked.length && !feed.length) {
    body.appendChild(el("p", {class:"editing tight", text:"You're all caught up — nothing needs you right now."}));
    body.appendChild(el("p", {class:"subhint", text:"When the auto-apply loop prepares an application for your approval, or pauses one on a step it needs you for, it shows up here (and pushes to your Mac/phone). Start or check the loop in Discover."}));
    updateBadge(0);
    return;
  }
  // Still-actionable items first — blocked (more urgent) then ready-to-submit — with the exact
  // Discover cards, so Review / Apply / Resolve work identically to Discover. Each card gets an id
  // so a feed row can scroll to it.
  if (parked.length) {
    body.appendChild(el("div", {class:"loop-ready-head", text:"Blocked — needs you (" + parked.length + ")"}));
    parked.forEach(p => { const c = parkedCard(p); c.id = "inbox-app-" + p.id; body.appendChild(c); });
  }
  if (ready.length) {
    body.appendChild(el("div", {class:"loop-ready-head", text:"Ready to submit (" + ready.length + ")"}));
    ready.forEach(a => { const c = loopReadyCard(a); c.id = "inbox-app-" + a.id; body.appendChild(c); });
  }
  // The full record of every push (decision 145).
  if (feed.length) {
    const head = el("div", {class:"loop-ready-head nf-head"}, [
      el("span", {text:"Recent notifications (" + feed.length + ")"}),
      el("button", {class:"nf-clear", type:"button", text:"Clear all", on:{click:()=>clearNotifs()}})]);
    body.appendChild(head);
    const list = el("div", {class:"nflist"});
    feed.forEach(n => list.appendChild(notifRow(n)));
    body.appendChild(list);
  }
  // Mark the log seen so the "new" dots clear next time; the badge reflects actionable count.
  try { await fetch("/notifications/read", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ids:null})}); } catch (e) {}
  updateBadge(d.count || 0);
}
function jumpToCard(appId) {
  const c = document.getElementById("inbox-app-" + appId);
  if (!c) return;
  c.scrollIntoView({behavior:"smooth", block:"center"});
  c.classList.add("nf-flash"); setTimeout(() => c.classList.remove("nf-flash"), 1400);
}

// ---- Discover: applications parked on a user-resolvable block (park & resume) ----
async function loadParked() {
  const panel = $("parked-panel"), body = $("parked-body");
  let d;
  try { d = await (await fetch("/parked")).json(); }
  catch (e) { panel.style.display = "none"; return; }
  const parked = (d && d.parked) || [];
  if (!parked.length) { panel.style.display = "none"; return; }
  panel.style.display = "";
  body.innerHTML = "";
  parked.forEach(p => body.appendChild(parkedCard(p)));
}

function parkedCard(p) {
  const title = (p.company || "—") + (p.role ? " — " + p.role : "");
  const head = el("div", {class:"pk-head"}, [
    el("span", {class:"pk-title", text:title}),
    el("span", {class:"pk-tag", text:p.label || "Blocked"})]);
  const actions = el("div", {class:"pk-actions"});
  if (p.resolve === "profile-answers")
    actions.append(el("button", {class:"pk-fix", type:"button", text:(p.action || "Resolve") + " →",
      on:{click:()=>goToProfileAnswers()}}));
  else if (p.resolve === "credentials")
    actions.append(el("span", {class:"pk-note", text:(p.action || "Store the login") + ", then re-apply this posting."}));
  else if (p.kind === "captcha")
    actions.append(el("span", {class:"pk-note", text:"Re-apply this posting and solve the CAPTCHA in the browser window that opens."}));
  else if (p.kind === "bot_wall")
    // Not a site error and not a CAPTCHA: the site refused us, so there is nothing to answer or
    // configure. Both re-apply buttons stay — if the block has lifted, the retry just works.
    actions.append(el("span", {class:"pk-note", text:"The site refused us as automated traffic and never showed the form — nothing to fix on your side. Try again later or from a different network, or apply by hand."}));
  else
    actions.append(el("span", {class:"pk-note", text:"This is a site error, not something you can answer — skip it or try again later."}));
  // A resumable parked app has a real "Submit for real" — gate it behind the same review panel
  // as the loop's ready cards (decision 125), so the submit is one step past seeing the answers,
  // résumé, filled-form screenshot, and the still-unanswered fields. The dry-run re-fill and the
  // armed submit (decision 058) live inside the panel via parkedSignoff.
  if (p.resumable)
    actions.append(el("button", {class:"review-toggle", type:"button", text:"Review",
      title:"See the exact answers, résumé and posting before you submit — opens over the page",
      on:{click:()=>openReview(p.id, title, parkedSignoff)}}));
  const kids = [head];
  if (p.detail) kids.push(el("div", {class:"pk-detail", text:p.detail}));
  kids.push(actions);
  return el("div", {class:"pkcard"}, kids);
}

// Resume a parked application: re-drive the deterministic fill on the same posting. `arm=false`
// is a dry-run (never submits); `arm=true` really submits THIS one application after an explicit
// confirm (decision 058). Reuses the run-progress panel + Finish button lower in the Discover tab.
async function reapplyParked(id, btn, arm, who, retailor) {
  const label = btn ? btn.textContent : "";
  if (arm) {
    const ok = confirm("Really SUBMIT this application" + (who ? " to " + who : "") + "?\\n\\n"
      + "This is a real, irreversible submission. Make sure the block is resolved. "
      + "It fills the form and clicks Submit; the pre-submit check still stops it if a required "
      + "field is unanswered.");
    if (!ok) return;
  }
  ensureDiscoverVisible();  // the re-fill / submit progress renders in Discover — show it
  const msg = $("test-msg");
  if (btn) { btn.disabled = true; btn.textContent = arm ? "Submitting…" : (retailor ? "Re-tailoring…" : "Starting…"); }
  if (msg) { msg.className = "msg"; msg.textContent = ""; }
  // Never fill with answers the user edited but didn't save (decision 153).
  const saved = await saveAnswers(id, true);
  if (!saved.ok) {
    if (msg) { msg.className = "msg err";
      msg.textContent = "Your edited answers could not be saved, so nothing was filled. Fix the error above the Save answers button, then try again."; }
    if (btn) { btn.disabled = false; btn.textContent = label; }
    return;
  }
  // Started from a review popup: close it (decision 184) — this run reports into Discover's
  // progress panel, which the popup covers. A no-op when it wasn't opened from there.
  closeReview();
  try {
    const r = await (await fetch("/parked/reapply", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id, arm: !!arm, retailor: !!retailor })})).json();
    if (!r.ok) {
      if (msg) { msg.className = "msg err"; msg.textContent = r.error || "Could not start."; }
      if (btn) { btn.disabled = false; btn.textContent = label; }
      return;
    }
    TEST_T0 = Date.now();
    const prog = $("test-progress"); if (prog) prog.scrollIntoView({behavior:"smooth", block:"start"});
    pollTest();
  } catch (e) {
    if (msg) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

// Deep-link to the Profile tab's "Needs your answer" list (UI Principle #2: one click to the fix).
function goToProfileAnswers() {
  const tab = document.querySelector('.tab[data-view="profile"]');
  if (tab) tab.click();  // triggers loadProfile() (async render)
  let tries = 0;
  const tick = () => {
    const t = document.getElementById("sec-qa");
    if (t) {
      t.scrollIntoView({behavior:"smooth", block:"start"});
      const first = document.querySelector("#qa-need textarea.qa-a, #qa-need input.qa-q");
      if (first) first.focus();
      return;
    }
    if (tries++ < 30) setTimeout(tick, 100);  // wait for the profile render, up to ~3s
  };
  setTimeout(tick, 100);
}

// ---- Track tab: the local application store (applications.db) ----
// Columns to show, in order. status/dates get special controls; the rest are text inputs.
// [key, label, default pixel width] — a fixed-layout table with per-column pixel widths so
// columns are individually resizable (drag the right edge) and can overflow into a horizontal
// scroll, like a spreadsheet. Per-user width/hidden overrides persist in localStorage.
const TRACK_COLS = [
  ["status","Status",120], ["fit_score","Fit",60], ["company","Company",140], ["role","Role",180], ["location","Location",140],
  ["remote","Remote",80], ["pay","Pay",100], ["portal","Portal",110], ["method","Method",90],
  ["source_url","Source URL",220], ["date_discovered","Discovered",104], ["date_dry_run","Dry-run",104], ["date_applied","Applied",104],
  ["run_count","Runs",76], ["tokens","Tokens",96], ["follow_up_date","Follow up",110], ["resume_path","Résumé used",160], ["notes","Notes",240],
];
let TRACK_STATE = { status:null, search:"", statuses:[] };

// Spreadsheet column layout (width + visibility + order), remembered per browser.
const TRACK_LS_W = "ab_track_colw", TRACK_LS_H = "ab_track_hidden", TRACK_LS_O = "ab_track_order";
let TRACK_COLW = {}, TRACK_HIDDEN = new Set(), TRACK_ORDER = [], TRACK_APPS = [];
try { TRACK_COLW = JSON.parse(localStorage.getItem(TRACK_LS_W) || "{}") || {}; } catch (e) {}
try { TRACK_HIDDEN = new Set(JSON.parse(localStorage.getItem(TRACK_LS_H) || "[]")); } catch (e) {}
try { TRACK_ORDER = JSON.parse(localStorage.getItem(TRACK_LS_O) || "[]") || []; } catch (e) {}
const saveColW = () => { try { localStorage.setItem(TRACK_LS_W, JSON.stringify(TRACK_COLW)); } catch (e) {} };
const saveHidden = () => { try { localStorage.setItem(TRACK_LS_H, JSON.stringify([...TRACK_HIDDEN])); } catch (e) {} };
const saveOrder = () => { try { localStorage.setItem(TRACK_LS_O, JSON.stringify(TRACK_ORDER)); } catch (e) {} };
const colWidth = (key, def) => TRACK_COLW[key] || def;
// Columns in the user's saved order, with any not-yet-ordered (e.g. a newly added) column kept in
// its TRACK_COLS position so a stale saved order never drops or duplicates a column.
const orderedCols = () => {
  const byKey = new Map(TRACK_COLS.map(c => [c[0], c]));
  const seen = new Set(), out = [];
  for (const k of TRACK_ORDER) { const c = byKey.get(k); if (c && !seen.has(k)) { out.push(c); seen.add(k); } }
  for (const c of TRACK_COLS) if (!seen.has(c[0])) out.push(c);
  return out;
};
const visibleCols = () => orderedCols().filter(([k]) => !TRACK_HIDDEN.has(k));

async function loadTrack() {
  const body = $("track-body");
  busyInto(body, "Loading applications…", false);
  try {
    const q = new URLSearchParams();
    if (TRACK_STATE.status) q.set("status", TRACK_STATE.status);
    if (TRACK_STATE.search) q.set("search", TRACK_STATE.search);
    const d = await (await fetch("/track?" + q.toString())).json();
    TRACK_STATE.statuses = d.statuses;
    renderScores(d.counts);
    renderCounts(d.counts);
    renderFunnel(d.funnel);
    renderUsageDiscovery(d.usage_discovery);
    renderFeed(d.applications);
    renderTrack(d.applications);
  } catch (e) {
    body.innerHTML = ""; body.append(el("div", {class:"msg err", text:"Couldn't load applications: " + (e.message||e)}));
  }
}

function renderCounts(counts) {
  const c = $("track-counts"); c.innerHTML = "";
  const mk = (key, label, n) => el("span", {
    class: "pill" + ((TRACK_STATE.status===key || (key===null && !TRACK_STATE.status)) ? " active" : ""),
    on:{click:()=>{ TRACK_STATE.status = key; loadTrack(); }}},
    [label + " ", el("span", {class:"n", text:String(n)})]);
  c.append(mk(null, "All", counts.total || 0));
  for (const s of TRACK_STATE.statuses) c.append(mk(s, s, counts[s] || 0));
}

// Exactly four hero scorecards at the top of Track — total processed, applied, blocked, failed.
function renderScores(counts) {
  const box = $("track-scores"); if (!box) return;
  const c = counts || {};
  const sum = (...ks) => ks.reduce((n, k) => n + (c[k] || 0), 0);
  const cards = [
    {label:"Total processed", val:c.total || 0, cls:""},
    {label:"Applied", val:sum("applied", "responded", "interview", "offer"), cls:"info"},
    {label:"Blocked / needs you", val:c.blocked || 0, cls:"warn2"},
    {label:"Failed", val:c.failed || 0, cls:"bad2"},
  ];
  box.innerHTML = "";
  cards.forEach(k => box.append(el("div", {class:"scorecard " + k.cls}, [
    el("div", {class:"sc-val", text:grp(k.val)}),
    el("div", {class:"sc-label", text:k.label})])));
}

// One muted status system, shared by the feed badge and the table cell colours. Colour by outcome:
// neutral=pending, blue=applied, green=positive reply, amber=blocked (pulses — needs you), red=failed.
const STATUS_META = {
  discovered:{c:"st-neutral"}, tailored:{c:"st-neutral"}, "dry-run":{c:"st-neutral"},
  applied:{c:"st-info"}, responded:{c:"st-good"}, interview:{c:"st-good"}, offer:{c:"st-good"},
  blocked:{c:"st-warn2", live:true}, rejected:{c:"st-neutral"}, "no-response":{c:"st-neutral"},
  failed:{c:"st-bad2"},
};
function statusMeta(s) { return STATUS_META[s] || {c:"st-neutral"}; }
function stbadge(status) {
  const m = statusMeta(status);
  return el("span", {class:"stbadge " + m.c + (m.live ? " live" : "")},
    [el("span", {class:"dot"}), status || "—"]);
}

// The application feed — one uniform card per row (company · role, a •-metadata string, fit, status).
function renderFeed(apps) {
  const box = $("track-feed"); if (!box) return;
  box.innerHTML = "";
  if (!apps.length) {
    box.append(el("div", {class:"tempty", text:
      TRACK_STATE.search || TRACK_STATE.status
        ? "No applications match this filter."
        : "No applications yet. The pipeline records them here as it runs — or add one manually."}));
    return;
  }
  apps.forEach(app => box.append(feedCard(app)));
}
function metaString(bits) {
  const wrap = el("span");
  bits.filter(Boolean).forEach((b, i) => {
    if (i) wrap.append(el("span", {class:"sep", text:"•"}));
    wrap.append(document.createTextNode(b));
  });
  return wrap;
}
function feedCard(app) {
  const title = (app.company || "—") + (app.role ? "  ·  " + app.role : "");
  const meta = el("div", {class:"fc-meta"}, [metaString([
    app.portal, app.location, app.run_count ? app.run_count + (app.run_count == 1 ? " run" : " runs") : null])]);
  const mainKids = [el("div", {class:"fc-title", text:title}), meta];
  if (app.blocker)
    mainKids.push(el("div", {class:"fc-blocker", title:app.blocker_detail || "", text:app.blocker}));
  const kids = [el("div", {class:"fc-main"}, mainKids)];
  if (app.fit_score != null && app.fit_score !== "") kids.push(el("span", {class:"fc-fit", text:"fit " + app.fit_score}));
  const rsc = resumeSrcChip(app.resume_source);  // freshly tailored vs reused (decision 144)
  if (rsc) kids.push(rsc);
  kids.push(stbadge(app.status));
  const card = el("button", {class:"fcard", type:"button", on:{click:() => openDrawer(app)}}, kids);
  card.dataset.id = app.id;
  return card;
}

// Context drawer — slides in from the right on a feed-card click: status, actions, and the run log.
let DRAWER_PREV = null;
function openDrawer(app) {
  const dr = $("track-drawer"), scrim = $("drawer-scrim");
  DRAWER_PREV = document.activeElement;
  $("drawer-title").textContent = (app.company || "—") + (app.role ? " — " + app.role : "");
  const meta = $("drawer-meta"); meta.innerHTML = "";
  meta.append(metaString([app.portal, app.method, app.location, app.pay]));
  const body = $("drawer-body"); body.innerHTML = "";
  const statusRow = el("div", {}, [stbadge(app.status)]);
  if (app.fit_score != null && app.fit_score !== "")
    statusRow.append(el("span", {class:"fc-fit", style:"margin-left:10px", text:"fit " + app.fit_score}));
  body.append(statusRow);
  // Résumé provenance (decision 144): the chip + its full sentence, so the drawer says exactly
  // which résumé this application used and why.
  if (app.resume_source)
    body.append(el("div", {class:"drawer-src"}, [resumeSrcChip(app.resume_source),
      el("span", {class:"rv-note", text:app.resume_source})]));
  const acts = el("div", {class:"drawer-actions"});
  if (app.source_url && /^https?:/i.test(app.source_url))
    acts.append(el("a", {href:app.source_url, target:"_blank", class:"tbtn", text:"Open posting ↗"}));
  if (app.resume_path)
    acts.append(el("a", {href:"/track/resume?id=" + app.id, target:"_blank", class:"tbtn", text:"View résumé ↗"}));
  if (app.status === "dry-run" && app.source_url)
    acts.append(el("button", {class:"rerun", type:"button", text:"Re-run ▶",
      title:"Re-fill this posting in a watchable browser (dry-run — nothing is submitted)",
      on:{click:(ev) => rerunDry(app, ev.target, false)}}));
  acts.append(el("button", {class:"tbtn", type:"button", text:"Retailor résumé →",
    title:"Open Discover's dry-run with this posting loaded and tailor a résumé to it — with the option to save it to your reusable postings first",
    on:{click:(ev) => retailorApp(app, ev.target)}}));
  acts.append(el("button", {class:"tbtn", type:"button", text:"Save to fixtures",
    title:"Add this posting to your saved postings so you can tailor against it anytime from Discover",
    on:{click:(ev) => saveAppFixture(app, ev.target)}}));
  if (acts.childElementCount)
    body.append(el("div", {}, [el("div", {class:"drawer-sec-label", text:"Actions"}), acts]));
  const termWrap = el("div", {}, [el("div", {class:"drawer-sec-label", text:"Run log"})]);
  const term = el("div", {class:"terminal"}, [el("div", {class:"tl", text:"Loading run history…"})]);
  termWrap.append(term); body.append(termWrap);
  loadDrawerRuns(app, term);
  scrim.classList.remove("hidden");
  requestAnimationFrame(() => { scrim.classList.add("open"); dr.classList.add("open"); });
  dr.setAttribute("aria-hidden", "false");
  document.querySelectorAll("#track-feed .fcard").forEach(c => c.classList.toggle("sel", c.dataset.id === String(app.id)));
  $("drawer-x").focus();
}
function closeDrawer() {
  const dr = $("track-drawer"), scrim = $("drawer-scrim");
  dr.classList.remove("open"); scrim.classList.remove("open");
  dr.setAttribute("aria-hidden", "true");
  setTimeout(() => scrim.classList.add("hidden"), 220);
  document.querySelectorAll("#track-feed .fcard.sel").forEach(c => c.classList.remove("sel"));
  if (DRAWER_PREV && DRAWER_PREV.focus) DRAWER_PREV.focus();
}
async function loadDrawerRuns(app, term) {
  try {
    const d = await (await fetch("/track/runs?id=" + app.id)).json();
    term.innerHTML = "";
    if (!d.runs || !d.runs.length) { term.append(el("div", {class:"tl", text:"No runs recorded yet."})); return; }
    d.runs.forEach(r => {
      const lvl = r.outcome === "failed" ? "err" : (r.outcome === "blocked" ? "warn" : "");
      term.append(el("div", {class:"tl " + lvl}, [
        el("span", {class:"tl-prompt", text:"› "}),
        el("span", {class:"tl-when", text:(r.ran_at || "").replace("T", " ") + "  "}),
        (r.outcome || "run") + (r.detail ? " — " + r.detail : ""),
      ]));
    });
    if (app.resume_path) term.append(el("div", {class:"tl", text:"résumé: " + app.resume_path}));
  } catch (e) {
    term.innerHTML = ""; term.append(el("div", {class:"tl err", text:"Couldn't load runs: " + (e.message || e)}));
  }
}

// Add this application's posting to the saved-fixtures list (POST /fixtures/add pulls the JD
// from its archived posting.md by id). On success the fixture picker is refreshed in place and
// the button latches to "Saved ✓" so the user knows it's reusable now.
async function saveAppFixture(app, btn) {
  btnBusy(btn, "Saving…");
  try {
    const res = await fetch("/fixtures/add", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({id: app.id})});
    const d = await res.json();
    if (!res.ok || d.error) throw new Error(d.error || "could not save");
    OPTS.fixtures = d.fixtures; fill($("fixture"), OPTS.fixtures);
    btn.disabled = true; btn._orig = null; btn.textContent = "Saved to fixtures ✓";
  } catch (e) {
    btnDone(btn);
    alert("Couldn't save to fixtures: " + (e.message || e));
  }
}

// Retailor this application's résumé: optionally save its posting to fixtures, then jump to the
// Review tab with the posting loaded so the user is one click from re-tailoring. If saved, the
// new fixture is selected; otherwise the posting is dropped into the "paste a posting" box.
async function retailorApp(app, btn) {
  btnBusy(btn, "Loading…");
  let rv = {};
  try { rv = await (await fetch("/track/review?id=" + app.id)).json(); } catch (e) { rv = {}; }
  btnDone(btn);
  const jd = (rv && rv.jd) || "";
  let fixtureToken = null;
  if (jd && confirm("Add this posting to your saved fixtures so you can reuse it anytime?\\n\\n" +
      "OK — save it, then tailor\\nCancel — tailor once without saving")) {
    try {
      const res = await fetch("/fixtures/add", {method:"POST",
        headers:{"Content-Type":"application/json"}, body: JSON.stringify({id: app.id})});
      const d = await res.json();
      if (!res.ok || d.error) throw new Error(d.error || "could not save");
      OPTS.fixtures = d.fixtures; fill($("fixture"), OPTS.fixtures);
      fixtureToken = d.fixture && d.fixture.path;
    } catch (e) {
      alert("Couldn't save to fixtures: " + (e.message || e) + "\\nContinuing to tailor without saving.");
    }
  }
  // Land on Discover's dry-run with this posting loaded and the run set to tailor-only, so the
  // user is one click from re-tailoring it (decision 163 — this used to open the Review tab).
  showView("discover");
  closeDrawer();
  DRY.job = "paste"; renderDryOpts();
  if (fixtureToken) {
    $("jobmode").value = "fixture"; $("jobmode").dispatchEvent(new Event("change"));
    $("fixture").value = fixtureToken;
  } else {
    $("jobmode").value = "custom"; $("jobmode").dispatchEvent(new Event("change"));
    $("title").value = app.role || ""; $("company").value = app.company || ""; $("body").value = jd;
  }
  $("test-run").scrollIntoView({behavior:"smooth", block:"center"});
  $("test-run").focus();
}

// The discovery→offer funnel (survey #4): one metric tile per stage. The count is the value;
// the meter width (relative to Discovered) and sub-line carry the drop-off + stage conversion.
function renderFunnel(funnel) {
  const box = $("track-funnel"); if (!box) return;
  box.innerHTML = "";
  const top = (funnel && funnel[0] && funnel[0].count) || 0;
  if (!top) {
    box.className = "funnel-empty";
    box.textContent = "The funnel fills in as the pipeline discovers, fills, and (once armed) submits applications.";
    return;
  }
  box.className = "mtiles tight";
  for (const s of funnel) {
    const pct = Math.round(100 * s.count / top);
    const sub = [document.createTextNode(pct + "% of top")];
    if (s.conversion_from_prev != null)
      sub.push(el("span", {class:"conv", text:"  ·  " + Math.round(100 * s.conversion_from_prev) + "% conv"}));
    box.append(el("div", {class:"mtile"}, [
      el("div", {class:"mtile-label", text:s.stage}),
      el("div", {class:"mtile-val", text:grp(s.count)}),
      el("div", {class:"mtile-sub"}, sub),
      el("div", {class:"mtile-meter"}, [el("div", {class:"mtile-meter-fill", style:"width:" + pct + "%"})]),
    ]));
  }
}

// A metric tile: uppercase label, big proportional-sans value, optional mono sub-line, optional meter.
function mtile(label, value, sub, meterPct) {
  const kids = [el("div", {class:"mtile-label", text:label}), el("div", {class:"mtile-val", text:value})];
  if (sub != null) kids.push(el("div", {class:"mtile-sub"}, Array.isArray(sub) ? sub : [document.createTextNode(sub)]));
  if (meterPct != null) kids.push(el("div", {class:"mtile-meter"}, [el("div", {class:"mtile-meter-fill", style:"width:" + meterPct + "%"})]));
  return el("div", {class:"mtile"}, kids);
}

// Discovery/judging Claude spend not tied to any one application (the batched fit judge, decision
// 087). One line above the table, hidden until such spend exists — it's the answer to "how much is
// the pipeline spending finding jobs, separate from what each application cost". Clicking it
// expands the same per-activity breakdown the per-row cells use.
function renderUsageDiscovery(u) {
  const box = $("track-usage"); if (!box) return;
  box.innerHTML = "";
  if (!u || !u.total_tokens) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const rows = Object.keys(u.by_activity || {}).map(k => [k, u.by_activity[k]])
    .sort((a, b) => b[1].total_tokens - a[1].total_tokens);
  const cap = el("div", {class:"tu-cap",
    text:"Your Claude spend on discovery & judging (all-time), tracked locally on this machine — "
      + "it covers finding and scoring postings, so it isn't charged to any one application."});
  const tiles = el("div", {class:"mtiles tight"}, [
    mtile("Total tokens", fmtTokens(u.total_tokens), grp(u.total_tokens) + " · " + u.calls + (u.calls === 1 ? " call" : " calls")),
    mtile("Input", fmtTokens(u.input_tokens), grp(u.input_tokens)),
    mtile("Output", fmtTokens(u.output_tokens), grp(u.output_tokens)),
  ]);
  const detail = el("div", {class:"tu-detail hidden"});
  rows.forEach(([k, v]) => detail.append(el("div", {class:"tu-act"}, [
    el("span", {class:"tu-act-name", text:ACT_LABELS[k] || k}),
    el("span", {class:"tu-act-num", text:fmtTokens(v.total_tokens) + " (" + grp(v.input_tokens)
      + " in / " + grp(v.output_tokens) + " out)"})])));
  const toggle = el("button", {class:"linklike tu-toggle", type:"button",
    title:"Per-activity breakdown of discovery & judging tokens."},
    [document.createTextNode("Show per-activity breakdown "), el("span", {class:"caret", text:"▾"})]);
  toggle.addEventListener("click", () => {
    const hidden = detail.classList.toggle("hidden");
    toggle.classList.toggle("open", !hidden);
  });
  box.append(cap, tiles, toggle, detail);
}

// The Source URL cell: the URL itself is the link — clicking the text opens the posting in a new
// tab. The cell stays editable (a manually-added row needs a way to set its URL), so an ✎ button
// swaps the link for a text input; committing the input saves and returns to the link. A value
// that isn't an http(s) URL has nothing to open, so it renders as the input directly — that also
// keeps a stored `javascript:`/`data:` string from ever becoming a clickable payload.
function urlCell(app) {
  const cell = el("span", {class:"urlcell"});
  let editing = false;
  // Re-render only this cell, never the whole row: saveCell writes "Saved ✓" into the row AFTER
  // its await, so replacing the row first detaches that span and the save (or its error) lands on
  // a dead node — no confirmation, a silent failure (UI Principle #5).
  const render = () => {
    cell.innerHTML = "";
    if (editing || !isHttpUrl(app.source_url)) {
      const input = el("input", {type:"text", value:app.source_url || "", class:"urltext",
        title:app.source_url || "", placeholder:"https://…",
        on:{change: async (e) => {
          const v = e.target.value.trim();
          if (!(await saveCell(app.id, "source_url", v))) return;  // save failed → row shows error
          app.source_url = v;
          editing = false;
          render();
        }}});
      cell.append(input);
      if (editing) input.focus();
      return;
    }
    // A labelled button, not the raw URL as text: the URL itself is unreadable at this column
    // width and tells the user nothing they can act on. The full URL stays on hover, and ✎
    // still swaps in the editable input when they need the value itself.
    cell.append(
      el("a", {class:"urllink", href:app.source_url, target:"_blank", rel:"noopener noreferrer",
        title:"Open this posting on its job board: " + app.source_url, text:"Open posting ↗"}),
      el("button", {class:"urledit", type:"button", text:"✎", title:"Edit this URL",
        on:{click:()=>{ editing = true; render(); }}}));
  };
  render();
  return cell;
}

function isHttpUrl(v) {
  try { const u = new URL((v || "").trim()); return u.protocol === "http:" || u.protocol === "https:"; }
  catch (e) { return false; }
}

function statusCell(app) {
  const sel = el("select", {class:"stcell st-" + app.status.replace("-","")});
  for (const s of TRACK_STATE.statuses) {
    const o = el("option", {value:s, text:s}); if (s===app.status) o.selected = true; sel.appendChild(o);
  }
  sel.addEventListener("change", () => { sel.className = "stcell st-" + sel.value.replace("-",""); saveCell(app.id, "status", sel.value); });
  return sel;
}

// Date cell: shows the date as plain text (muted "—" when empty) instead of a native date picker in
// every row. Clicking swaps in a real <input type=date>; picking a value saves and reverts to text,
// blurring reverts unchanged. Mirrors urlCell so only the clicked cell ever shows an editor.
function dateCell(app, key) {
  const cell = el("span", {class:"datecell"});
  let editing = false;
  const fmt = (v) => { const m = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(v || ""); return m ? (m[2] + "/" + m[3] + "/" + m[1]) : ""; };
  const render = () => {
    cell.innerHTML = "";
    if (editing) {
      const input = el("input", {type:"date", value:app[key] || "",
        on:{change: async (e) => {
              const v = e.target.value;
              if (!(await saveCell(app.id, key, v))) return;  // save failed → row shows error
              app[key] = v; editing = false; render();
            },
            blur: () => { editing = false; render(); }}});
      cell.append(input); input.focus();
      if (input.showPicker) { try { input.showPicker(); } catch (e) {} }
      return;
    }
    const txt = fmt(app[key]);
    cell.append(el("button", {class:"datebtn" + (txt ? "" : " empty"), type:"button",
      text: txt || "—", title: txt ? "Edit date" : "Set date",
      on:{click:()=>{ editing = true; render(); }}}));
  };
  render();
  return cell;
}

// Re-run a previous dry-run from the tracker: re-drive the same deterministic fill on the same
// posting URL (never submits — reuses reapplyParked with arm=false). `retailor=false` reuses the
// stored tailored PDF; `retailor=true` regenerates the résumé from the saved JD first (decision
// 086). Switch to the Discover tab first so the fill reports into the one shared run-progress
// panel + Finish button, instead of a second progress UI.
function rerunDry(app, btn, retailor) {
  const tab = document.querySelector('.tab[data-view="discover"]');
  if (tab) tab.click();
  reapplyParked(app.id, btn, false, null, retailor);
}

function renderTrack(apps) {
  TRACK_APPS = apps;
  const body = $("track-body"); body.innerHTML = "";
  if (!apps.length) {
    body.append(el("div", {class:"tempty", text:
      TRACK_STATE.search || TRACK_STATE.status
        ? "No applications match this filter."
        : "No applications yet. The pipeline records them here as it runs — or add one manually."}));
    return;
  }
  const vis = visibleCols();
  const colEls = {};
  const cols = vis.map(([key,,def]) => { const c = el("col", {style:"width:"+colWidth(key,def)+"px"}); colEls[key] = c; return c; })
    .concat([el("col", {style:"width:200px"})]);
  const ths = vis.map(([key,label,def]) => {
    const rz = el("div", {class:"rz", title:"Drag to resize"});
    const th = el("th", {draggable:"true", title:"Drag to reorder"},
      [el("span", {class:"lbl", text:label}), rz]);
    th.dataset.key = key;
    rz.addEventListener("mousedown", (ev) => startResize(ev, key, colEls[key], def, th));
    attachColDrag(th, key);
    return th;
  });
  const head = el("tr", {}, ths.concat([el("th", {text:""})]));
  const rows = apps.map(app => {
    const tds = vis.map(([key]) => {
      let input;
      if (key === "status") input = statusCell(app);
      else if (key === "run_count") input = runsCell(app);
      else if (key === "tokens") input = tokensCell(app);
      else if (key === "date_discovered" || key === "date_dry_run" || key === "date_applied")
        input = dateCell(app, key);
      else if (key === "resume_path")
        input = app.resume_path
          ? el("a", {class:"reslink", href:"/track/resume?id=" + app.id, target:"_blank",
                     title:app.resume_path, text:"View résumé ↗"})
          : el("span", {class:"muted", text:"—"});
      else if (key === "source_url") input = urlCell(app);
      else input = el("input", {type:"text", value:app[key] || "", placeholder:"—", on:{change:e=>saveCell(app.id, key, e.target.value)}});
      return el("td", {}, [input]);
    });
    const saved = el("span", {class:"rowsaved"});
    const del = el("button", {class:"delrow", type:"button", text:"Delete",
      on:{click:()=>delApp(app.id)}});
    // Re-run: only for rows that were dry-runs and still have a posting URL to re-fill. "Re-run"
    // reuses the stored résumé; "Re-tailor" (shown only when a saved JD lets it run offline)
    // regenerates the résumé from that JD + your current base résumé first (decision 086).
    const acts = [];
    if (app.status === "dry-run" && app.source_url) {
      acts.push(el("button", {class:"rerun", type:"button", text:"Re-run ▶",
        title:"Re-fill this posting with the stored résumé — never submits",
        on:{click:(ev)=>rerunDry(app, ev.target, false)}}));
      if (app.has_jd)
        acts.push(el("button", {class:"rerun retailor", type:"button", text:"Re-tailor ▶",
          title:"Regenerate the résumé from this posting's saved job description (a Claude call), then re-fill — never submits",
          on:{click:(ev)=>rerunDry(app, ev.target, true)}}));
    }
    acts.push(del, saved);
    tds.push(el("td", {}, [el("div", {style:"display:flex;gap:6px;align-items:center"}, acts)]));
    const tr = el("tr", {}, tds); tr._saved = saved; tr.dataset.id = app.id;
    return tr;
  });
  const table = el("table", {class:"ttable"},
    [el("colgroup", {}, cols), el("thead", {}, [head]), el("tbody", {}, rows)]);
  body.append(el("div", {class:"twrap"}, [table]));
}

// "Runs" cell: a per-posting run count that expands an inline history sub-row (decision 084).
// 0 runs → a muted dash (nothing to expand).
function runsCell(app) {
  const n = app.run_count || 0;
  if (!n) return el("span", {class:"muted", text:"—"});
  return el("button", {class:"runsbtn", type:"button", title:"Show this posting's run history",
    on:{click:(ev)=>toggleRuns(ev.currentTarget, app)}},
    [document.createTextNode(n + (n === 1 ? " run" : " runs")), el("span", {class:"caret", text:"▾"})]);
}

// Compact token count: 2030 → "2.0k", 1_450_000 → "1.4M", small values verbatim. Used for the
// Tokens column; the expanded sub-row shows exact comma-grouped numbers.
function fmtTokens(n) {
  n = n || 0;
  const trim = (x) => x.endsWith(".0") ? x.slice(0, -2) : x;
  if (n >= 1e6) return trim((n / 1e6).toFixed(1)) + "M";
  if (n >= 1e3) return trim((n / 1e3).toFixed(1)) + "k";
  return String(n);
}
const grp = (n) => (n || 0).toLocaleString();
// Human labels for the activity keys (usage.ACTIVITIES); anything unmapped shows as-is.
const ACT_LABELS = {tailoring:"Tailoring", "form-entry":"Form entry", judging:"Judging",
  enrichment:"Enrichment", salary:"Salary", impact:"Impact", other:"Other"};

// "Tokens" cell: the per-application Claude spend (input+output), one number that expands an
// inline sub-row splitting it into in/out and a per-activity breakdown (decision 095). No tokens
// recorded for this posting → a muted dash.
function tokensCell(app) {
  const t = app.tokens;
  if (!t || !t.total_tokens) return el("span", {class:"muted", text:"—"});
  return el("button", {class:"runsbtn", type:"button",
    title:"Show what Claude spent on this application, by activity",
    on:{click:(ev)=>toggleTokens(ev.currentTarget, app)}},
    [document.createTextNode(fmtTokens(t.total_tokens)), el("span", {class:"caret", text:"▾"})]);
}

// Toggle the token-breakdown sub-row under a posting. All data is already in the /track payload
// (app.tokens) — no fetch — so a second click just collapses it.
function toggleTokens(btn, app) {
  const tr = btn.closest("tr");
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("tokrow")) { next.remove(); btn.classList.remove("open"); return; }
  btn.classList.add("open");
  const t = app.tokens || {};
  const rows = Object.keys(t.by_activity || {})
    .map(k => [k, t.by_activity[k]])
    .sort((a, b) => b[1].total_tokens - a[1].total_tokens);
  const cell = (txt, cls) => el("td", {class:cls || "", text:txt});
  const bodyRows = rows.map(([k, v]) => el("tr", {}, [
    cell(ACT_LABELS[k] || k, "tok-act"),
    cell(grp(v.input_tokens), "tok-num"),
    cell(grp(v.output_tokens), "tok-num"),
    cell(grp(v.total_tokens), "tok-num tok-tot"),
    cell(String(v.calls), "tok-num tok-calls"),
  ]));
  const totRow = el("tr", {class:"tok-total-row"}, [
    cell("Total", "tok-act"),
    cell(grp(t.input_tokens), "tok-num"),
    cell(grp(t.output_tokens), "tok-num"),
    cell(grp(t.total_tokens), "tok-num tok-tot"),
    cell(String(t.calls), "tok-num tok-calls"),
  ]);
  const head = el("tr", {}, [
    el("th", {text:"Activity", class:"tok-act"}), el("th", {text:"In", class:"tok-num"}),
    el("th", {text:"Out", class:"tok-num"}), el("th", {text:"Total", class:"tok-num"}),
    el("th", {text:"Calls", class:"tok-num"})]);
  const table = el("table", {class:"toktable"},
    [el("thead", {}, [head]), el("tbody", {}, bodyRows.concat([totRow]))]);
  const holder = el("div", {class:"tokbox"}, [table]);
  const rr = el("tr", {class:"tokrow"}, [el("td", {colspan:String(tr.children.length)}, [holder])]);
  tr.after(rr);
}

// Toggle the run-history sub-row under a posting. Lazy-loads /track/runs on first open so the
// main table stays light; a second click collapses it.
async function toggleRuns(btn, app) {
  const tr = btn.closest("tr");
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("runsrow")) { next.remove(); btn.classList.remove("open"); return; }
  btn.classList.add("open");
  const holder = el("div", {class:"runsbox"});
  const rr = el("tr", {class:"runsrow"}, [el("td", {colspan:String(tr.children.length)}, [holder])]);
  tr.after(rr);
  busyInto(holder, "Loading runs…", false);
  try {
    const d = await (await fetch("/track/runs?id=" + app.id)).json();
    holder.innerHTML = "";
    if (!d.runs || !d.runs.length) { holder.append(el("div", {class:"muted", text:"No runs recorded yet."})); return; }
    d.runs.forEach(r => holder.append(runRow(r, app.id)));
  } catch (e) {
    holder.innerHTML = ""; holder.append(el("div", {class:"msg err", text:"Couldn't load runs: " + (e.message||e)}));
  }
}

// One line in the run history: when it ran, the outcome, the fill summary, and a link to the
// résumé that run used (served by /track/resume for the posting).
function runRow(r, appId) {
  const cls = "runoutcome st-" + (r.outcome || "").replace("-", "");
  const kids = [
    el("span", {class:"runwhen", text:(r.ran_at || "").replace("T", " ")}),
    el("span", {class:cls, text:r.outcome || "run"}),
    el("span", {class:"rundetail", text:r.detail || ""}),
  ];
  if (r.resume_path)
    kids.push(el("a", {class:"reslink", href:"/track/resume?id=" + appId, target:"_blank",
      title:r.resume_path, text:"résumé ↗"}));
  return el("div", {class:"runline"}, kids);
}

// Drag a column's right edge to resize it; persist the new width.
function startResize(ev, key, colEl, def, th) {
  ev.preventDefault(); ev.stopPropagation();
  const startX = ev.clientX;
  const startW = (colEl && colEl.getBoundingClientRect().width) || colWidth(key, def);
  document.body.classList.add("rz-drag"); if (th) th.classList.add("rzing");
  const move = (e) => {
    const w = Math.max(50, Math.round(startW + (e.clientX - startX)));
    TRACK_COLW[key] = w; if (colEl) colEl.style.width = w + "px";
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    document.body.classList.remove("rz-drag"); if (th) th.classList.remove("rzing");
    saveColW();
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

// Drag a column header onto another to reorder; persist the new order. The resize handle calls
// preventDefault() on its mousedown, so grabbing the right edge resizes and never starts a drag.
let DRAG_KEY = null;
function attachColDrag(th, key) {
  th.addEventListener("dragstart", (e) => {
    DRAG_KEY = key; th.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", key); } catch (err) {}
  });
  th.addEventListener("dragend", () => {
    DRAG_KEY = null; th.classList.remove("dragging");
    document.querySelectorAll(".ttable th.dropto").forEach(x => x.classList.remove("dropto"));
  });
  th.addEventListener("dragover", (e) => {
    if (DRAG_KEY == null || DRAG_KEY === key) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move"; th.classList.add("dropto");
  });
  th.addEventListener("dragleave", () => th.classList.remove("dropto"));
  th.addEventListener("drop", (e) => {
    e.preventDefault(); th.classList.remove("dropto");
    if (DRAG_KEY == null || DRAG_KEY === key) return;
    reorderCol(DRAG_KEY, key);
  });
}

// Move column `from` to sit directly before column `to`, then persist and re-render.
function reorderCol(from, to) {
  const order = orderedCols().map(c => c[0]);
  const src = order.indexOf(from);
  if (src >= 0) order.splice(src, 1);
  const dst = order.indexOf(to);
  order.splice(dst < 0 ? order.length : dst, 0, from);
  TRACK_ORDER = order; saveOrder(); renderTrack(TRACK_APPS);
}

// Show/hide columns — one checkbox per column, plus a reset.
function renderColMenu() {
  const m = $("track-cols-menu"); m.innerHTML = "";
  TRACK_COLS.forEach(([key, label]) => {
    const cb = el("input", {type:"checkbox"}); cb.checked = !TRACK_HIDDEN.has(key);
    cb.addEventListener("change", () => {
      if (cb.checked) { TRACK_HIDDEN.delete(key); }
      else if (visibleCols().length <= 1) { cb.checked = true; return; }  // keep at least one column
      else { TRACK_HIDDEN.add(key); }
      saveHidden(); renderTrack(TRACK_APPS);
    });
    m.appendChild(el("label", {}, [cb, " " + label]));
  });
  m.appendChild(el("button", {class:"rst", type:"button", text:"Reset columns", on:{click:()=>{
    TRACK_HIDDEN.clear(); TRACK_COLW = {}; TRACK_ORDER = [];
    saveHidden(); saveColW(); saveOrder(); renderColMenu(); renderTrack(TRACK_APPS);
  }}}));
}
$("track-cols-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const m = $("track-cols-menu");
  const show = m.classList.contains("hidden");
  if (show) renderColMenu();
  m.classList.toggle("hidden");
});
document.addEventListener("click", (e) => {
  const m = $("track-cols-menu");
  if (m && !m.classList.contains("hidden") && !e.target.closest(".colmenu")) m.classList.add("hidden");
});

async function saveCell(id, field, value) {
  const tr = document.querySelector('#track-body tr[data-id="' + id + '"]');
  const saved = tr && tr._saved;
  try {
    const d = await (await fetch("/track/update", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id, changes: { [field]: value } })})).json();
    if (!d.ok) throw new Error(d.error || "save failed");
    if (saved) { saved.className = "rowsaved"; saved.textContent = "Saved ✓"; setTimeout(()=>{ saved.textContent = ""; }, 1500); }
    if (field === "status") { const dd = await (await fetch("/track")).json(); renderCounts(dd.counts); }
    return true;   // callers that must react only to a REAL save (urlCell's ↗) can await this
  } catch (e) {
    if (saved) { saved.className = "msg err"; saved.textContent = String(e.message || e); }
    return false;
  }
}

async function addApp() {
  const btn = $("track-add"), msg = $("track-msg");
  btnBusy(btn, "Adding…"); msg.className = "msg busy"; msg.textContent = "";
  try {
    const d = await (await fetch("/track/add", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ data: {} })})).json();
    if (!d.ok) throw new Error(d.error || "add failed");
    TRACK_STATE.status = null; $("track-search").value = ""; TRACK_STATE.search = "";
    await loadTrack();
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { btnDone(btn); }
}

async function delApp(id) {
  if (!confirm("Delete this application from your tracker? This can't be undone.")) return;
  try {
    const d = await (await fetch("/track/delete", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id })})).json();
    if (!d.ok) throw new Error(d.error || "delete failed");
    await loadTrack();
  } catch (e) { $("track-msg").className = "msg err"; $("track-msg").textContent = String(e.message || e); }
}

// ---- Import applications from the linked inbox (decision 151) ----
// Reads the inbox, turns "thank you for applying" emails into rows and rejection/interview
// emails into status changes. Every run is undoable, and a run that finds forwarded job-alert
// emails discovery isn't using offers the one click that switches them on (UI Principle #2).
async function importInbox() {
  const btn = $("track-import"), out = $("track-import-out");
  out.classList.remove("hidden");
  btnBusy(btn, "Reading inbox…");
  const stop = busyInto(out, "Reading your inbox and matching emails to applications…", true);
  try {
    const d = await (await fetch("/track/import-inbox", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({})})).json();
    stop();
    renderImport(d);
    await loadTrack();
  } catch (e) {
    stop();
    out.innerHTML = "";
    out.append(el("div", {class:"msg err", text:"Import failed: " + String(e.message || e)}));
  } finally { btnDone(btn); }
}

function renderImport(d) {
  const out = $("track-import-out");
  out.innerHTML = "";
  out.append(el("div", {class: d.ok ? "impmsg" : "msg err", text: d.message || ""}));
  (d.errors || []).forEach(e => out.append(el("div", {class:"msg err", text: e})));
  const acts = el("div", {class:"impacts"});
  if (d.run_id && ((d.created || []).length || (d.updated || []).length)) {
    acts.append(el("button", {class:"tbtn", type:"button",
      text:"Undo this import (" + ((d.created||[]).length + (d.updated||[]).length) + ")",
      title:"Delete the rows this import added and put the changed statuses back",
      on:{click: async (ev) => {
        const b = ev.currentTarget;
        btnBusy(b, "Undoing…");
        try {
          const r = await (await fetch("/track/import-undo", {method:"POST",
            headers:{"Content-Type":"application/json"},
            body: JSON.stringify({run_id: d.run_id})})).json();
          out.innerHTML = "";
          out.append(el("div", {class:"impmsg", text:
            "Undone — deleted " + r.deleted + " row(s), restored " + r.restored + " status(es)."}));
          await loadTrack();
        } catch (e) { btnDone(b); out.append(el("div", {class:"msg err", text:String(e.message||e)})); }
      }}}));
  }
  const alerts = d.alerts || {};
  const names = Object.keys(alerts);
  if (names.length && !d.alerts_enabled) {
    // Those emails are new openings, not applications — they belong to Discover, which is off.
    acts.append(el("button", {class:"tbtn", type:"button",
      text:"Use these job alerts in Discover",
      title:"Turn on job-alert discovery for " + names.join(", "),
      on:{click: async (ev) => {
        const b = ev.currentTarget;
        btnBusy(b, "Turning on…");
        try {
          await fetch("/track/enable-email-alerts", {method:"POST",
            headers:{"Content-Type":"application/json"}, body: JSON.stringify({providers: names})});
          b.replaceWith(el("span", {class:"impmsg", text:
            "Job-alert discovery is on for " + names.join(", ") + " — run a search on Discover."}));
        } catch (e) { btnDone(b); out.append(el("div", {class:"msg err", text:String(e.message||e)})); }
      }}}));
  }
  if (acts.childNodes.length) out.append(acts);
}

$("track-import").addEventListener("click", importInbox);
$("track-add").addEventListener("click", addApp);
let trackSearchT = null;
$("track-search").addEventListener("input", (e) => {
  clearTimeout(trackSearchT);
  trackSearchT = setTimeout(() => { TRACK_STATE.search = e.target.value.trim(); loadTrack(); }, 250);
});

// Feed | Table view toggle — remembered per browser. Table view shows the full editable spreadsheet
// (and its Columns menu); Feed view shows the card list + drawer. Default: Feed.
function setTrackView(v) {
  const feed = v !== "table";
  $("track-feed").classList.toggle("hidden", !feed);
  $("track-body").classList.toggle("hidden", feed);
  $("colmenu-wrap").style.display = feed ? "none" : "";
  $("view-feed").classList.toggle("on", feed);
  $("view-table").classList.toggle("on", !feed);
  try { localStorage.setItem("ab_track_view", feed ? "feed" : "table"); } catch (e) {}
}
$("view-feed").addEventListener("click", () => setTrackView("feed"));
$("view-table").addEventListener("click", () => setTrackView("table"));
(function(){ let v = "feed"; try { v = localStorage.getItem("ab_track_view") || "feed"; } catch (e) {} setTrackView(v); })();

// Drawer close: X button, click the scrim, or Escape. Tab is trapped inside while open
// (ARIA dialog pattern — matches the setup-overlay reference).
$("drawer-x").addEventListener("click", closeDrawer);
$("drawer-scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("track-drawer").classList.contains("open")) closeDrawer();
});
$("track-drawer").addEventListener("keydown", (e) => {
  if (e.key !== "Tab") return;
  const f = $("track-drawer").querySelectorAll('a[href],button:not([disabled]),input,[tabindex]:not([tabindex="-1"])');
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

// Discovery settings modal — the settings form lives here (opened from the Discover action bar or
// the first-visit nudge), keeping the Discover page itself to actions + status.
let DISC_MODAL_PREV = null;
function openDiscModal() {
  const m = $("disc-modal");
  DISC_MODAL_PREV = document.activeElement;
  m.classList.remove("hidden");
  $("disc-modal-x").focus();
}
function closeDiscModal() {
  $("disc-modal").classList.add("hidden");
  if (DISC_MODAL_PREV && DISC_MODAL_PREV.focus) DISC_MODAL_PREV.focus();
}
$("disc-open").addEventListener("click", openDiscModal);
$("disc-modal-x").addEventListener("click", closeDiscModal);
$("disc-modal").addEventListener("click", (e) => { if (e.target === $("disc-modal")) closeDiscModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("disc-modal").classList.contains("hidden")) closeDiscModal();
});

// ---- LinkedIn import ----
function fileB64(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}
$("rf-import").addEventListener("click", async () => {
  const f = $("rf-file").files[0], msg = $("rf-msg"), btn = $("rf-import");
  if (!f) { msg.className = "msg err"; msg.textContent = "Choose your résumé file (PDF or .docx) first."; return; }
  btnBusy(btn, "Parsing…"); msg.className = "msg busy";
  const stop = busyInto(msg, "Reading your résumé and structuring it with Claude…", true);
  try {
    const d = await (await fetch("/resume/import-file", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ resume: currentResume(), filename: f.name, data_b64: await fileB64(f) }) })).json();
    if (!d.ok) throw new Error(d.error || "parse failed");
    const a = d.added || {}, bits = [];
    for (const [k, lbl] of [["experience","experience"],["activities","activities"],["projects","projects"],
                            ["education","education"],["skills","skills"],["certifications","certifications"]])
      if (a[k]) bits.push(a[k] + " " + lbl);
    if (d.summary_added) bits.push("summary");
    if ((d.contact_filled||[]).length) bits.push("contact (" + d.contact_filled.join(", ") + ")");
    await loadProfile();
    msg.className = "msg ok";
    // Say what was matched against the existing profile instead of re-added — silence would read
    // as "it ignored half my résumé" (and the match is fuzzy, so the user must be able to check it).
    const uniq = a => [...new Set(a || [])];
    const dup = uniq(d.skipped), enr = uniq(d.enriched);
    msg.textContent = (d.created ? "Created your résumé from that file — added " : "Merged — added ")
      + (bits.length ? bits.join(", ") + "." : "nothing new (everything was already in your résumé).")
      + (enr.length ? " Filled in missing details on " + enr.join(", ") + "." : "")
      + (dup.length ? " Already in your résumé, so not duplicated: " + dup.join(", ") + "." : "")
      + " Review the sections below and Save."
      + (d.kept_document ? " Kept the PDF: jobs whose demanded skills it already covers get this"
                         + " file as-is, instead of a tailored résumé." : "");
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { stop(); btnDone(btn); }
});

// ---- Kept résumé files (decision 152) ----
// The PDFs the user uploaded, which postings can be sent verbatim. Listed so it is never a
// surprise which file an employer receives, and removable in one click.
function renderKeptResumes(docs) {
  const box = $("rf-kept");
  box.textContent = "";
  if (!(docs || []).length) return;
  box.appendChild(el("div", {class:"drawer-sec-label", style:"margin:12px 0 0",
                            text:"Kept résumé files — sent as-is to closely matching jobs"}));
  for (const d of docs) {
    const btn = el("button", {type:"button", text:"Remove"});
    btn.addEventListener("click", async () => {
      btnBusy(btn, "Removing…");
      try {
        const r = await (await fetch("/resume/uploads/delete", { method:"POST",
          headers:{"Content-Type":"application/json"}, body: JSON.stringify({ name: d.name }) })).json();
        if (!r.ok) throw new Error(r.error || "remove failed");
        renderKeptResumes(r.docs || []);
      } catch (e) {
        btnDone(btn);
        const m = $("rf-msg"); m.className = "msg err"; m.textContent = String(e.message || e);
      }
    });
    box.appendChild(el("div", {class:"kept-row"}, [
      el("span", {}, [el("b", {text:d.filename}),
                      el("span", {class:"kept-when",
                                  text:d.uploaded_at ? " — uploaded " + d.uploaded_at.split("T")[0] : ""})]),
      btn,
    ]));
  }
}
async function loadKeptResumes() {
  try {
    const d = await (await fetch("/resume/uploads")).json();
    renderKeptResumes(d.docs || []);
  } catch (e) { /* the list is informational; a fetch failure must not break the Profile page */ }
}
$("li-toggle").addEventListener("click", () => {
  const p = $("s-linkedin"), open = p.classList.toggle("hidden") === false;
  $("li-toggle").setAttribute("aria-expanded", String(open));
  $("li-toggle").textContent = open ? "Hide LinkedIn import" : "Import from LinkedIn instead";
  if (open) p.scrollIntoView({behavior:"smooth", block:"nearest"});
});
$("li-import").addEventListener("click", async () => {
  const f = $("li-file").files[0], msg = $("li-msg"), btn = $("li-import");
  if (!f) { msg.className = "msg err"; msg.textContent = "Choose your LinkedIn export file first."; return; }
  btnBusy(btn, "Importing…"); msg.className = "msg busy";
  const stop = busyInto(msg, "Importing your LinkedIn data…", false);
  try {
    const d = await (await fetch("/resume/import-linkedin", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ resume: currentResume(), filename: f.name, data_b64: await fileB64(f) }) })).json();
    if (!d.ok) throw new Error(d.error || "import failed");
    const a = d.added || {};
    // Same reporting as the résumé upload: entries matched against what's already on file are named,
    // because a fuzzy match the user cannot see reads as "it ignored half my export".
    const uniq = x => [...new Set(x || [])];
    const dup = uniq(d.skipped), enr = uniq(d.enriched);
    await loadProfile();
    msg.className = "msg ok";
    msg.textContent = `Imported ${a.experience||0} experience, ${a.education||0} education, ${a.skills||0} skills`
      + ((d.found_files||[]).length ? " (from " + d.found_files.join(", ") + ")." : " — no LinkedIn CSVs found in that file.")
      + (enr.length ? " Filled in missing details on " + enr.join(", ") + "." : "")
      + (dup.length ? " Already in your résumé, so not duplicated: " + dup.join(", ") + "." : "");
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { stop(); btnDone(btn); }
});

// ---- Apply profile editor ----
let P = null;
function boolSel(label, key, val) {
  const s = el("select", {class:"f", "data-k":key}, [
    el("option", {value:"", text:"—"}),
    el("option", {value:"yes", text:"Yes"}),
    el("option", {value:"no", text:"No"}),
  ]);
  s.value = val === true ? "yes" : (val === false ? "no" : "");
  return el("div", {class:"fld"}, [el("label", {text:label}), s]);
}

// ---- Structured location + start-date inputs (match how application forms collect them) ----
// Stored formats are unchanged for the autofill resolver: location = "City, ST", country = name,
// earliest_start_date = a preset phrase or an ISO date. The dropdowns just compose/parse those.
const US_STATES = [["AL","Alabama"],["AK","Alaska"],["AZ","Arizona"],["AR","Arkansas"],["CA","California"],
  ["CO","Colorado"],["CT","Connecticut"],["DE","Delaware"],["DC","District of Columbia"],["FL","Florida"],
  ["GA","Georgia"],["HI","Hawaii"],["ID","Idaho"],["IL","Illinois"],["IN","Indiana"],["IA","Iowa"],
  ["KS","Kansas"],["KY","Kentucky"],["LA","Louisiana"],["ME","Maine"],["MD","Maryland"],["MA","Massachusetts"],
  ["MI","Michigan"],["MN","Minnesota"],["MS","Mississippi"],["MO","Missouri"],["MT","Montana"],["NE","Nebraska"],
  ["NV","Nevada"],["NH","New Hampshire"],["NJ","New Jersey"],["NM","New Mexico"],["NY","New York"],
  ["NC","North Carolina"],["ND","North Dakota"],["OH","Ohio"],["OK","Oklahoma"],["OR","Oregon"],
  ["PA","Pennsylvania"],["RI","Rhode Island"],["SC","South Carolina"],["SD","South Dakota"],["TN","Tennessee"],
  ["TX","Texas"],["UT","Utah"],["VT","Vermont"],["VA","Virginia"],["WA","Washington"],["WV","West Virginia"],
  ["WI","Wisconsin"],["WY","Wyoming"]];
const STATE_OPTS = [["","—"], ...US_STATES.map(([a,n]) => [a, n + " (" + a + ")"])];
const COUNTRIES = ["United States","Canada","United Kingdom","Ireland","Australia","New Zealand","India",
  "Germany","France","Netherlands","Spain","Italy","Switzerland","Sweden","Poland","Portugal","Mexico",
  "Brazil","Argentina","Singapore","Japan","China","South Korea","Israel","United Arab Emirates","Other"];
const START_PRESETS = ["Immediately", "2 weeks' notice", "1 month"];
// Work-arrangement preference — drives how remote/hybrid/on-site questions and office-location
// dropdowns are answered (value stored on the profile, label shown to the user).
const WORK_ARRANGEMENT_OPTS = [
  ["", "No preference"],
  ["in_office_if_commutable", "Prefer in-office when the office is commutable"],
  ["hybrid", "Prefer hybrid"],
  ["in_office", "Always prefer in-office"],
  ["remote", "Always prefer remote"],
];
// Voluntary EEO option lists — standard self-identification wording that matches most ATS forms;
// the autofill combobox falls back to Claude to map onto a form's exact option text. Each starts
// with a blank "—" so leaving it unanswered (decline to self-identify) stays possible.
const PRONOUN_OPTS = [["","—"],"He/Him","She/Her","They/Them","Ze/Zir","Prefer not to say"];
const GENDER_OPTS = [["","—"],"Male","Female","Non-binary","Prefer not to say"];
const RACE_OPTS = [["","—"],"American Indian or Alaska Native","Asian","Black or African American",
  "Hispanic or Latino","Native Hawaiian or Other Pacific Islander","White","Two or More Races",
  "Prefer not to say"];
const VETERAN_OPTS = [["","—"],"I am not a protected veteran",
  "I identify as one or more of the classifications of a protected veteran","I don't wish to answer"];
const DISABILITY_OPTS = [["","—"],"No, I do not have a disability and have not had one in the past",
  "Yes, I have a disability, or have had one in the past","I do not want to answer"];

// Generic <select>: opts is an array of "value" strings or [value,label] pairs. Preserves an
// existing value that isn't in the list (so we never silently drop saved data).
function selField(label, key, value, opts) {
  const list = opts.map(o => Array.isArray(o) ? o : [o, o]);
  if (value && !list.some(([v]) => v === value)) list.push([value, value]);
  const sel = el("select", {class:"f", "data-k":key}, list.map(([v,l]) => el("option", {value:v, text:l})));
  sel.value = value == null ? "" : value;
  return el("div", {class:"fld"}, [el("label", {text:label}), sel]);
}
// Split a stored "City, ST" (or "City, State name") into {city, state-abbr}.
function parseLocation(loc) {
  loc = (loc || "").trim();
  if (!loc) return { city:"", state:"" };
  const parts = loc.split(",").map(s => s.trim()).filter(Boolean);
  if (parts.length >= 2) {
    const last = parts[parts.length - 1].toLowerCase();
    const hit = US_STATES.find(([a,n]) => a.toLowerCase() === last || n.toLowerCase() === last);
    if (hit) return { city: parts.slice(0, -1).join(", "), state: hit[0] };
  }
  return { city: loc, state:"" };   // no recognizable state — keep the whole thing as the city text
}
// Earliest start date: a preset-or-"specific date" dropdown; picking "Specific date…" reveals a
// native date picker. Returns the .fld; collectProfile reads start_date_kind + start_date_date.
function startDateField(value) {
  value = (value || "").trim();
  const isDate = /^\\d{4}-\\d{2}-\\d{2}$/.test(value);
  const custom = value && !START_PRESETS.includes(value) && !isDate;  // preserve any pre-existing free text
  const kind = START_PRESETS.includes(value) ? value : (isDate ? "specific" : (custom ? value : ""));
  const opts = [el("option", {value:"", text:"—"}), ...START_PRESETS.map(pp => el("option", {value:pp, text:pp}))];
  if (custom) opts.push(el("option", {value:value, text:value}));
  opts.push(el("option", {value:"specific", text:"Specific date…"}));
  const sel = el("select", {class:"f", "data-k":"start_date_kind"}, opts);
  sel.value = kind;
  const date = el("input", {type:"date", class:"f", "data-k":"start_date_date", value: isDate ? value : "", style:"margin-top:6px"});
  date.classList.toggle("hidden", kind !== "specific");
  sel.addEventListener("change", () => date.classList.toggle("hidden", sel.value !== "specific"));
  return el("div", {class:"fld"}, [el("label", {text:"Earliest start date"}), sel, date]);
}
function qaStatus(qa) {
  if ((qa.maps_to||"").trim()) return {mark:"↔", label:"Auto-answered from your profile ("+qa.maps_to.trim()+")", color:"var(--ok-text)"};
  if ((qa.answer||"").trim()) return qa.generated
    ? {mark:"✨", label:"AI-drafted — review & edit", color:"var(--ai)"}
    : {mark:"✓", label:"Answered", color:"var(--ok-text)"};
  return {mark:"○", label:"Needs your answer", color:"var(--warn-strong)"};
}
// Hidden fields that carry the classification/flags through the save round-trip (cardData reads any [data-k]).
function qaHidden(qa) {
  return [
    el("input", {type:"hidden", "data-k":"maps_to", value:(qa.maps_to||"").trim()}),
    el("input", {type:"hidden", "data-k":"generated", value: qa.generated ? "1" : ""}),
    el("input", {type:"hidden", "data-k":"seen_count", value: String(qa.seen_count||0)}),
    el("input", {type:"hidden", "data-k":"input_kind", value: qa.input_kind||""}),
    el("input", {type:"hidden", "data-k":"options", value: JSON.stringify(qa.options||[])}),
  ];
}
// ---- Check-all-that-apply answers (shared by the Profile screen and the Review panel) ----
// A question that takes MORE THAN ONE answer: captured from a checkbox GROUP — several checkboxes
// under one question ("Language Skill(s) (Check all that apply)"). Rendering it as a single-choice
// dropdown or one text box loses every answer but one, so BOTH editors use the widget below.
const isMultiAnswer = (kind, options) => (kind||"") === "checkbox" &&
  (Array.isArray(options) ? options.filter(Boolean).length : 0) > 1;
// Multi answers live in one string, "; "-joined — the format the form-fill splits on to tick each box.
const splitMulti = v => (v||"").split(";").map(s => s.trim()).filter(Boolean);
// One checkbox per captured option, mirrored into a hidden input so every caller reads the answer
// from `.value` exactly like a text box. Returns {node, hidden}.
function multiCheckboxes(options, value) {
  const opts = options.filter(Boolean);
  const chosen = new Set(splitMulti(value));
  const hidden = el("input", {type:"hidden", value: value||""});
  const box = el("div", {class:"qa-multi"});
  const sync = () => { hidden.value = [...box.querySelectorAll("input:checked")].map(i => i.value).join("; "); };
  const add = (v, label, checked) => {
    const cb = el("input", {type:"checkbox", value:v, on:{change:sync}});
    cb.checked = checked;
    box.appendChild(el("label", {class:"qa-opt"}, [cb, el("span", {text:label})]));
  };
  opts.forEach(o => add(o, o, chosen.has(o)));
  // A stored answer whose option the form no longer offers still shows, so it is never silently dropped.
  splitMulti(value).filter(v => !opts.includes(v)).forEach(v => add(v, v + " (not in this form)", true));
  return {node: el("div", {}, [
    el("div", {class:"qa-multihint", text:"Check every option that applies — all checked answers get selected on the form."}),
    box, hidden]), hidden: hidden};
}
// ---- One-of-many answers: the form offered a fixed list of choices (select / radio / combobox) ----
// Editing one as a free-text box invites a value no option matches, which then fills as blank or
// forces the fill to guess — so the review panel offers the form's own options (decision 165).
const isSingleChoice = (kind, options) => (kind||"") !== "checkbox" &&
  (Array.isArray(options) ? options.filter(Boolean).length : 0) > 1;
const OTHER_CHOICE = "__applicationbot_type_your_own__";  // sentinel option, never a real answer
// A dropdown over the captured options, mirrored into a hidden input so every caller reads the
// answer from `.value` exactly like a text box. The captured list can fall short of the form's
// real one (long lists are truncated when scanned, and a posting can change its options), so
// "Type a different value…" always keeps a text box one click away — never a dead end.
function singleChoiceInput(options, value) {
  const opts = options.filter(Boolean);
  const hidden = el("input", {type:"hidden", value: value||""});
  const box = el("div", {});
  const asText = () => {
    const inp = el("input", {class:"rv-edit", type:"text", value: hidden.value||"",
                             placeholder:"Type the answer to submit…"});
    inp.addEventListener("input", () => { hidden.value = inp.value; });
    const back = el("button", {class:"rv-btn rv-choice-back", type:"button",
      text:"Choose from the form's options instead", on:{click:asSelect}});
    box.innerHTML = ""; box.appendChild(inp); box.appendChild(back);
    inp.focus();
  };
  const asSelect = () => {
    const list = [["", "— choose an option —"]].concat(opts.map(o => [o, o]));
    // A value the form no longer offers still shows, so an answer is never silently dropped.
    if ((hidden.value||"") && !opts.includes(hidden.value))
      list.push([hidden.value, hidden.value + " (not in this form)"]);
    list.push([OTHER_CHOICE, "Type a different value…"]);
    const sel = el("select", {class:"rv-edit"}, list.map(([v,l]) => el("option", {value:v, text:l})));
    sel.value = hidden.value || "";
    sel.addEventListener("change", () => {
      if (sel.value === OTHER_CHOICE) { asText(); return; }
      hidden.value = sel.value;
    });
    box.innerHTML = ""; box.appendChild(sel);
  };
  asSelect();
  return {node: el("div", {}, [box, hidden]), hidden: hidden};
}
// Profile-screen wrapper: the same widget, with the hidden field named for the save round-trip.
function qaMultiInput(qa, cls, opts) {
  const w = multiCheckboxes(opts, qa.answer);
  w.hidden.setAttribute("data-k", "answer");
  w.node.className = cls;
  return w.node;
}
// The answer input, recreated as the form's real control: checkboxes when the form had a
// check-all-that-apply group, a dropdown when the field had options (so the answer matches at
// fill time), else a free-text box.
function qaAnswerInput(qa, cls) {
  const opts = Array.isArray(qa.options) ? qa.options.filter(Boolean) : [];
  if (isMultiAnswer(qa.input_kind, qa.options)) return qaMultiInput(qa, cls, opts);
  if (opts.length) {
    const list = [["","— choose an option —"]].concat(opts.map(o => [o, o]));
    if ((qa.answer||"") && !opts.includes(qa.answer)) list.push([qa.answer, qa.answer]);  // keep a stored value
    const sel = el("select", {class:cls, "data-k":"answer"}, list.map(([v,l]) => el("option", {value:v, text:l})));
    sel.value = qa.answer || "";
    return sel;
  }
  return el("textarea", {class:cls, "data-k":"answer", placeholder:"Type your answer…", value: qa.answer||""});
}
// Compact collapsed card for an ANSWERED / auto-handled question.
function qaCard(qa) {
  qa = qa || {};
  const st = qaStatus(qa);
  const fields = [
    el("div", {style:"font-size:12px;font-weight:600;margin-bottom:4px;color:"+st.color, text: st.mark+" "+st.label}),
    area("Question","question",qa.question),
    el("div", {class:"fld"}, [el("label", {text:"Answer"}), qaAnswerInput(qa, "f")]),
    ...qaHidden(qa),
  ];
  return entryCard(fields, c => { const q=(cardData(c).question||"").trim(); const s=q.length>64?q.slice(0,64)+"…":q; return st.mark+"  "+(s||"New answer"); });
}
// Prominent OPEN card for an UNANSWERED question: seen-badge + question + answer box, ready to type.
function qaOpenCard(qa) {
  qa = qa || {};
  const seen = qa.seen_count||0;
  const card = el("div", {class:"card qa-open"});
  const del = el("button", {class:"del", type:"button", text:"✕", title:"Remove", on:{click:(ev)=>{ ev.stopPropagation(); card.remove(); refreshQaSummary(); }}});
  const qrow = el("div", {class:"qa-qrow"});
  if (seen>0) qrow.append(el("span", {class:"qa-badge", text:"seen "+seen+"×"}));
  const kids = [del, qrow];
  if ((qa.question||"").trim()) {
    qrow.append(el("span", {class:"qa-q", text:qa.question}));
    kids.push(el("input", {type:"hidden", "data-k":"question", value:qa.question}));
  } else {
    qrow.append(el("input", {class:"f qa-q", "data-k":"question", placeholder:"Type the question…"}));
  }
  kids.push(qaAnswerInput(qa, "f qa-a"));
  kids.push(...qaHidden(qa));
  card.append(...kids);
  return card;
}
function qaPill(color, n, label) {
  return el("span", {class:"pill"}, [el("span",{class:"dot",style:"background:"+color}), el("b",{text:String(n)}), el("span",{text:label})]);
}
function refreshQaSummary() {
  const box = $("qa-summary-counts"); if (!box) return;
  const cards = [...document.querySelectorAll("#qa-need > .card")];
  const need = cards.length;
  const start = $("qa-start");
  if (start) { start.textContent = need ? ("Start answering ("+need+")") : "All answered ✓"; start.disabled = !need; }
  const nb = $("qa-need-count"); if (nb) nb.textContent = String(need);
}
// The whole "Saved answers" section: ranked unanswered list on top, compact answered grid below.
function screeningSection(list) {
  list = (list||[]).slice();
  const isAns = qa => (qa.answer||"").trim() || (qa.maps_to||"").trim();
  const need = list.filter(qa => !isAns(qa)).sort((a,b) => (b.seen_count||0)-(a.seen_count||0));
  const done = list.filter(isAns);
  const mapped = done.filter(qa => (qa.maps_to||"").trim()).length;

  const body = el("div", {id:"sec-qa"});
  const needHead = el("div", {class:"qa-grouphead"}, [
    el("span", {text: need.length ? "Needs your answer — ranked by how often they've come up" : "Needs your answer — all caught up ✓"})]);
  const needWrap = el("div", {id:"qa-need", class:"cards"});
  need.forEach(qa => needWrap.appendChild(qaOpenCard(qa)));
  const addBtn = el("button", {class:"addbtn", type:"button", text:"+ Add a question manually", on:{click:()=>{
    const c = qaOpenCard(); needWrap.appendChild(c);
    const inp = c.querySelector("input.qa-q, textarea"); if (inp) inp.focus();
    refreshQaSummary();
  }}});

  const doneWrap = el("div", {class:"qa-answered"});
  done.forEach(qa => doneWrap.appendChild(qaCard(qa)));

  const startBtn = el("button", {id:"qa-start", class:"qa-start", type:"button", on:{click:()=>{
    const t = needWrap.querySelector("textarea.qa-a, input.qa-q");
    if (t) { t.scrollIntoView({behavior:"smooth", block:"center"}); t.focus(); }
  }}});
  const summary = el("div", {id:"qa-summary-counts", class:"qa-summary"}, [
    qaPill("var(--warn-line)", need.length, "need your answer"),
    qaPill("var(--ok)", done.length-mapped, "answered"),
    qaPill("var(--ai)", mapped, "auto from profile"),
    startBtn,
  ]);
  // A tiny hidden counter element so refreshQaSummary can update the pill without a rebuild.
  summary.querySelector(".pill b").id = "qa-need-count";

  body.append(needHead, needWrap, addBtn);
  if (done.length) body.append(el("div", {class:"qa-grouphead", text:"Answered & auto-handled ("+done.length+")"}), doneWrap);
  const sec = el("div", {class:"sec"}, [el("h3", {text:"Saved answers to screening questions"}), summary, body]);
  setTimeout(refreshQaSummary, 0);
  return sec;
}
function acctRow(name, ok, text) {
  return el("div", {style:"display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)"}, [
    el("span", {style:"font-weight:700;font-size:15px;color:"+(ok?"var(--ok-text)":"var(--muted)"), text: ok ? "✓" : "○"}),
    el("span", {style:"font-weight:600;min-width:130px", text:name}),
    el("span", {style:"color:var(--muted);font-size:13px", text:text}),
  ]);
}
function nativeAccountsPanel() {
  // MyGreenhouse Quick Apply is opt-in and needs the linked inbox for its emailed code (decision
  // 172); greenhouse_problem is the server's exact blocker, so show it rather than a bare ✗.
  const ghOK = P.greenhouse_quick_apply && !P.greenhouse_problem;
  const card = el("div", {class:"card"}, [
    el("p", {class:"hint", text:"Which native autofills the Apply stage can use. Greenhouse's Quick Apply is optional (set it up below) — ApplicationBot fills Greenhouse forms on its own either way; Lever/Ashby/Workday parse your uploaded résumé and need no account."}),
    acctRow("MyGreenhouse", ghOK, ghOK ? ("Signed in with an emailed code · " + P.greenhouse_email)
                                       : (P.greenhouse_quick_apply ? P.greenhouse_problem : "Off — not needed; the form is filled for you")),
    acctRow("Lever", true, "No login needed — résumé-parse autofill"),
    acctRow("Ashby", true, "No login needed — résumé-parse autofill"),
    acctRow("Workday", true, "No login needed — résumé-parse autofill"),
  ]);
  return el("div", {class:"sec"}, [el("h3", {text:"Autofill accounts"}), card]);
}
// Bot email link panel — its own secure store (password → OS keychain), not the profile YAML.
function mailboxPanel() {
  // Primary path: paste email + a Gmail app password. Google blocked normal-password IMAP login in
  // 2022, so an app password (a 16-char code you generate once) is the closest paste-and-go option.
  const emailIn = el("input", {class:"f", id:"mb-email", placeholder:"you@gmail.com"});
  const passIn  = el("input", {class:"f", id:"mb-pass", type:"password", placeholder:"16-character app password"});
  const hostIn  = el("input", {class:"f", id:"mb-host", placeholder:"auto-detected from your email address"});
  const portIn  = el("input", {class:"f", id:"mb-port", value:"993"});
  const linkBtn = el("button", {id:"mb-link", class:"addbtn", type:"button", text:"Connect"});
  const twofaLink = el("a", {href:"https://myaccount.google.com/security", target:"_blank", rel:"noopener", text:"2-Step Verification"});
  const appPwLink = el("a", {href:"https://myaccount.google.com/apppasswords", target:"_blank", rel:"noopener", text:"App passwords"});
  const setup = el("details", {open:""}, [
    el("summary", {class:"subhint", text:"How to get an app password — ~1 min (Google no longer allows your normal password here)", style:"cursor:pointer;font-weight:600"}),
    el("ol", {class:"subhint", style:"margin:6px 0 0 18px;line-height:1.6"}, [
      el("li", {}, [document.createTextNode("Turn on "), twofaLink, document.createTextNode(" for your Google account (app passwords only appear once it’s on).")]),
      el("li", {}, [document.createTextNode("Open "), appPwLink, document.createTextNode(", type a name like “ApplicationBot”, and click Create — Google shows a 16-character code.")]),
      el("li", {text:"Paste your Gmail address and that 16-character code below, then click Connect."}),
    ]),
  ]);
  const emailFld = el("div", {class:"fld"}, [el("label", {text:"Gmail address"}), emailIn]);
  const passFld = el("div", {class:"fld"}, [
    el("label", {text:"App password"}),
    passIn,
    el("div", {class:"subhint", text:"The 16-character code from Google above — not your normal Gmail password. Stored in your OS keychain, never in a file."}),
  ]);
  const serverDetails = el("details", {}, [
    el("summary", {class:"subhint", text:"Not Gmail? Set your mail server", style:"cursor:pointer"}),
    el("p", {class:"subhint", text:"Left blank, the server is auto-detected for Gmail, Outlook, Yahoo, iCloud, and Fastmail."}),
    row2(el("div", {class:"fld"}, [el("label", {text:"IMAP host"}), hostIn]),
         el("div", {class:"fld"}, [el("label", {text:"Port"}), portIn])),
  ]);

  // Alternative: one-click OAuth (read-only). Kept for anyone who prefers not to use an app password.
  const cidIn  = el("input", {class:"f", id:"mb-cid", placeholder:"e.g. 8391027-xq3z.apps.googleusercontent.com"});
  const csecIn = el("input", {class:"f", id:"mb-csec", type:"password", placeholder:"e.g. GOCSPX-aB1cD2eF3gH4"});
  const gmailBtn = el("button", {id:"mb-gmail", class:"addbtn", type:"button", text:"Connect with Google (read-only)"});
  const oauthLink = el("a", {href:"https://console.cloud.google.com/auth/clients", target:"_blank", rel:"noopener", text:"Google Cloud → Clients"});
  const oauth = el("details", {}, [
    el("summary", {class:"subhint", text:"Prefer read-only access? Connect with a Google app instead (more setup)", style:"cursor:pointer"}),
    el("p", {class:"subhint", text:"An app password grants full mailbox access; this OAuth path grants read-only. The trade-off is more one-time setup: you register a free Google “app” and paste its two keys (the app’s keys — not your Gmail login)."}),
    el("ol", {class:"subhint", style:"margin:6px 0 0 18px;line-height:1.6"}, [
      el("li", {}, [document.createTextNode("In "), oauthLink, document.createTextNode(", Create client → application type "), el("b",{text:"Desktop app"}), document.createTextNode(".")]),
      el("li", {text:"On the consent screen, add your Gmail as a test user and set the app to “In production” (else access expires after 7 days)."}),
      el("li", {}, [document.createTextNode("Copy the "), el("b",{text:"Client ID"}), document.createTextNode(" and "), el("b",{text:"Client secret"}), document.createTextNode(" into the boxes, then click Connect with Google.")]),
    ]),
    el("div", {class:"fld"}, [el("label", {text:"Client ID"}), cidIn, el("div", {class:"subhint", text:"Ends in .apps.googleusercontent.com — not your email."})]),
    el("div", {class:"fld"}, [el("label", {text:"Client secret"}), csecIn]),
    el("div", {style:"margin-top:6px"}, [gmailBtn]),
  ]);

  const status  = el("div", {id:"mb-status", class:"subhint", text:"Loading…"});
  const msg     = el("div", {id:"mb-msg", class:"subhint"});
  const unlinkBtn = el("button", {id:"mb-unlink", class:"addbtn", type:"button", text:"Disconnect", style:"display:none"});
  linkBtn.addEventListener("click", () => linkMailbox(linkBtn));
  gmailBtn.addEventListener("click", () => connectGmail(gmailBtn));
  unlinkBtn.addEventListener("click", () => unlinkMailbox(unlinkBtn));

  const card = el("div", {class:"card"}, [
    el("p", {class:"hint", text:"Optional — only for account-gated portals (Workday). When the bot creates a Workday account for you, Workday emails a verification link; connecting your inbox lets the bot read that one email and click it."}),
    status,
    setup,
    emailFld,
    passFld,
    serverDetails,
    el("div", {style:"display:flex;gap:8px;align-items:center;margin-top:6px"}, [linkBtn, unlinkBtn]),
    msg,
    oauth,
  ]);
  return el("div", {class:"sec"}, [el("h3", {text:"Bot email — for Workday verification (optional)"}), card]);
}
async function loadMailbox() {
  try {
    const s = await (await fetch("/mailbox")).json();
    const st = $("mb-status"), un = $("mb-unlink");
    if (s.linked) {
      const how = s.auth === "oauth" ? "Gmail, read-only" : (s.host + ":" + s.port);
      st.textContent = "✓ Connected: " + s.email + " (" + how + ") · " + s.source;
      st.style.color = "var(--ok-text)";
      if (un) un.style.display = "";
      if (s.client_id && $("mb-cid") && !$("mb-cid").value) $("mb-cid").value = s.client_id;
      if ($("mb-email") && !$("mb-email").value) $("mb-email").value = s.email;
      if ($("mb-host")  && !$("mb-host").value)  $("mb-host").value  = s.host;
      if ($("mb-port")) $("mb-port").value = s.port;
    } else if (s.problem) {
      // Linked on disk but the keychain secret is gone — say which credential vanished and how to
      // restore it, and keep Disconnect available to clear the stale record.
      st.textContent = "⚠ " + s.problem;
      st.style.color = "var(--bad)";
      if (un) un.style.display = "";
    } else {
      st.textContent = "Not connected — add your Gmail address + app password below to auto-verify Workday accounts.";
      st.style.color = "";
      if (un) un.style.display = "none";
    }
  } catch (e) { const st = $("mb-status"); if (st) st.textContent = "Could not load connection status."; }
}
async function connectGmail(btn) {
  const client_id = $("mb-cid").value.trim(), client_secret = $("mb-csec").value.trim();
  const msg = $("mb-msg"); msg.textContent = ""; msg.style.color = "";
  if (!client_id || !client_secret) { msg.textContent = "Paste the Google OAuth client ID and secret (see the setup steps above)."; msg.style.color = "var(--bad)"; return; }
  const t0 = Date.now();
  btnBusy(btn, "Waiting for Google…");
  msg.style.color = "";
  const tick = setInterval(() => { msg.textContent = "A Google sign-in tab should have opened — approve read-only access. (" + Math.round((Date.now()-t0)/1000) + "s)"; }, 1000);
  try {
    const r = await (await fetch("/mailbox/gmail/connect", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({client_id, client_secret})})).json();
    clearInterval(tick);
    msg.textContent = (r.ok ? "✓ " : "⚠ ") + (r.message || "");
    msg.style.color = r.ok ? "var(--ok-text)" : "var(--bad)";
    if (r.ok) { $("mb-csec").value = ""; await loadMailbox(); }
  } catch (e) { clearInterval(tick); msg.textContent = "Failed: " + e.message; msg.style.color = "var(--bad)"; }
  finally { btnDone(btn); }
}
async function linkMailbox(btn) {
  const email = $("mb-email").value.trim(), host = $("mb-host").value.trim();
  const port = $("mb-port").value.trim() || "993", password = $("mb-pass").value;
  const msg = $("mb-msg"); msg.textContent = ""; msg.style.color = "";
  if (!email || !password) { msg.textContent = "Enter the email and app password."; msg.style.color = "var(--bad)"; return; }
  btnBusy(btn, "Linking & testing…");
  try {
    const r = await (await fetch("/mailbox/link", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({email, host, port, password})})).json();
    msg.textContent = (r.ok ? "✓ " : "⚠ ") + (r.message || "");
    msg.style.color = r.ok ? "var(--ok-text)" : "var(--bad)";
    if (r.ok) { $("mb-pass").value = ""; await loadMailbox(); }
  } catch (e) { msg.textContent = "Failed: " + e.message; msg.style.color = "var(--bad)"; }
  finally { btnDone(btn); }
}
async function unlinkMailbox(btn) {
  btnBusy(btn, "Unlinking…");
  try { await fetch("/mailbox/unlink", {method:"POST"}); $("mb-msg").textContent = "Unlinked."; $("mb-msg").style.color = ""; await loadMailbox(); }
  catch (e) { $("mb-msg").textContent = "Failed: " + e.message; }
  finally { btnDone(btn); }
}
// One unified Profile screen: applicant details + résumé content + screening answers + logins.
function renderProfileForm() {
  const f = $("profile-form"); f.innerHTML = "";
  $("editing-path").textContent = currentResume();
  const secs = [];
  const put = (id, node) => { node.id = id; secs.push(node); return node; };

  // Applicant details (apply profile) — the primary form-autofill identity. Everything about WHERE
  // (home address + work-location preferences) lives in the Location section below instead, so a
  // user setting "no preference" has one place to look rather than five fields spread down the form.
  const applicant = el("div", {id:"profile-card", class:"card"});
  applicant.append(
    row2(fld("First name","first_name",P.first_name), fld("Last name","last_name",P.last_name)),
    row2(fld("Email","email",P.email), fld("Phone","phone",P.phone)),
    row2(fld("LinkedIn URL","linkedin_url",P.linkedin_url), fld("GitHub URL","github_url",P.github_url)),
    fld("Portfolio / website","portfolio_url",P.portfolio_url),
    row2(boolSel("Authorized to work?","work_authorized",P.work_authorized), boolSel("Requires sponsorship?","requires_sponsorship",P.requires_sponsorship)),
    boolSel("U.S. citizen?","us_citizen",P.us_citizen),
    row2(fld("Desired salary","desired_salary",P.desired_salary), startDateField(P.earliest_start_date)),
    fld("Years of experience","years_experience",P.years_experience),
    fld("How did you hear about this job? (default answer)","how_heard",P.how_heard),
    row2(selField("Gender (optional)","gender",P.gender,GENDER_OPTS), selField("Pronouns (optional)","pronouns",P.pronouns,PRONOUN_OPTS)),
    selField("Race / ethnicity (optional)","race_ethnicity",P.race_ethnicity,RACE_OPTS),
    selField("Veteran status (optional)","veteran_status",P.veteran_status,VETERAN_OPTS),
    selField("Disability status (optional)","disability_status",P.disability_status,DISABILITY_OPTS),
  );
  put("s-applicant", el("div", {class:"sec"}, [
    el("h3", {text:"Applicant details"}),
    el("p", {class:"subhint", text:"Contact, work eligibility, and optional EEO — used to auto-fill application forms."}),
    applicant]));

  // Location (apply profile) — home address + every work-location preference in one section.
  // collectProfile() reads this card alongside #profile-card, so it saves with the rest of the form.
  const loc = parseLocation(P.location);
  const location = el("div", {id:"location-card", class:"card"});
  location.append(
    el("p", {class:"grouphead", text:"Where you live"}),
    el("p", {class:"subhint", text:"Your address as forms ask for it. Also the home end of the commute judgement below."}),
    selField("Country","country", P.country || "United States", COUNTRIES),
    row2(selField("State","state", loc.state, STATE_OPTS), fld("City","city", loc.city)),
    // Portals that split the address into four required boxes (Jobvite, BambooHR) need these two;
    // City and State above are what fill the other two. Left blank, those forms stop for review.
    row2(fld("Street address","street_address",P.street_address), fld("ZIP / postal code","postal_code",P.postal_code)),
    el("p", {class:"grouphead", text:"Where you'll work"}),
    el("p", {class:"subhint", text:"How the bot answers relocation, remote/hybrid/on-site, and office-choice questions on application forms. No preference? Leave these at “—” / “No preference” and blank — nothing here is required."}),
    row2(boolSel("Willing to relocate?","willing_to_relocate",P.willing_to_relocate),
         boolSel("Open to remote?","open_to_remote",P.open_to_remote)),
    row2(selField("Preferred work arrangement","work_arrangement",P.work_arrangement||"",WORK_ARRANGEMENT_OPTS),
         fld("Max commute (miles) — for 'commutable' judgement","max_commute_miles",P.max_commute_miles==null?"":String(P.max_commute_miles))),
    area("Preferred office locations (one per line, most preferred first — e.g. 'New York, NY', 'Remote')","preferred_locations",(P.preferred_locations||[]).join("\\n")),
  );
  put("s-location", el("div", {class:"sec"}, [
    el("h3", {text:"Location"}),
    el("p", {class:"subhint", text:"Where you live and where you're willing to work. Discovery's remote-only filter lives on the Discover tab."}),
    location]));

  // Spoken/written languages (apply profile) — nothing on the résumé carries these, and forms
  // ask for them as check-all-that-apply groups and per-language proficiency dropdowns.
  const langSec = section("Languages","sec-languages",(P.languages||[]).map(langCard),"+ Add language",()=>langCard());
  langSec.insertBefore(el("p", {class:"subhint", text:"Languages you speak, most proficient first. Fills \\"Language Skill(s) (check all that apply)\\" groups and language-proficiency questions on application forms."}), langSec.querySelector(".cards"));
  put("s-languages", langSec);

  // Résumé content (source of truth for tailoring) — collapsible entries.
  put("s-experience", section("Experience","sec-experience",(R.experience||[]).map(expCard),"+ Add experience",()=>expCard()));
  put("s-activities", section("Leadership & activities","sec-activities",(R.activities||[]).map(expCard),"+ Add activity",()=>expCard()));
  // Projects are ordered most→least impressive (Claude's ★ score); unscored sort last.
  const projSorted = (R.projects||[]).slice().sort((a,b) => (b.impact||0) - (a.impact||0));
  const projSec = section("Projects","sec-projects",projSorted.map(projCard),"+ Add project",()=>projCard());
  const rankBtn = el("button", {id:"rank-proj", class:"addbtn", type:"button", text:"★ Rank by impressiveness"});
  const rankMsg = el("div", {id:"rank-msg", class:"msg"});
  rankBtn.addEventListener("click", rankProjects);
  projSec.insertBefore(el("p", {class:"subhint", text:"Claude scores each project 1–5 on technical depth and difficulty, then orders them so your résumé leads with your strongest work. Saves your current edits."}), projSec.querySelector(".cards"));
  projSec.append(rankBtn, rankMsg);
  put("s-projects", projSec);
  put("s-education", section("Education","sec-education",(R.education||[]).map(eduCard),"+ Add education",()=>eduCard()));
  put("s-skills", section("Skills","sec-skills",(R.skills||[]).map(skillCard),"+ Add skill category",()=>skillCard()));

  // Résumé header & summary (résumé print fields, distinct from the form-autofill identity).
  const c = R.contact || {};
  const basic = el("div", {id:"basic", class:"card"});
  basic.append(
    row2(fld("Name","name",c.name), fld("Email","email",c.email)),
    row2(fld("Phone","phone",c.phone), fld("Location","location",c.location)),
    area("Links (one per line)","links",(c.links||[]).join("\\n")),
    area("Summary (optional)","summary",R.summary||"","A short professional summary…"),
    area("Certifications (one per line)","certifications",(R.certifications||[]).join("\\n")));
  put("s-resume-header", el("div", {class:"sec"}, [
    el("h3", {text:"Résumé header & summary"}),
    el("p", {class:"subhint", text:"Name, contact, and summary as they print on your résumé. Form autofill uses Applicant details above."}),
    basic]));

  // Screening answers (apply profile) — collapsible entries.
  put("s-screening", screeningSection(P.custom_answers||[]));

  // Autofill accounts status + native logins (apply profile).
  put("s-accounts", nativeAccountsPanel());
  const creds = el("div", {id:"creds-card", class:"card"});
  // MyGreenhouse signs in with an emailed security code, not a password (decision 182), so this is
  // off by default and needs the linked inbox. greenhouse_problem is the server's exact blocker.
  const ghProblem = P.greenhouse_problem || "";
  const ghStatus = el("div", {class:"subhint", style:"margin-top:6px"},
    P.greenhouse_quick_apply
      ? [el("span", {text: ghProblem ? ("⚠ " + ghProblem) : "✓ Ready — the security code is read from your linked inbox."})]
      : [el("span", {text:"Off — ApplicationBot fills Greenhouse forms itself, no account needed."})]);
  if (P.greenhouse_quick_apply && ghProblem) {
    ghStatus.append(el("button", {class:"linklike", type:"button", text:"Open inbox settings", style:"margin-left:8px",
      on:{click:()=>{ showView("settings"); setTimeout(()=>{ const t = $("set-mailbox-mount"); if (t) t.scrollIntoView({behavior:"smooth", block:"start"}); }, 0); }}}));
  }
  creds.append(
    el("p", {class:"hint", text:"Optional. When on, the Apply stage signs in to MyGreenhouse and uses its Quick Apply autofill first, then fills the rest. Greenhouse emails a security code to sign in, so this needs your MyGreenhouse address to be the inbox linked in Settings. No password is stored."}),
    row2(selField("MyGreenhouse Quick Apply","greenhouse_quick_apply", P.greenhouse_quick_apply ? "yes" : "no", [["no","Off — fill the form myself"],["yes","On — sign in with an emailed code"]]),
         fld("MyGreenhouse email","greenhouse_email",P.greenhouse_email)),
    ghStatus);
  put("s-logins", el("div", {class:"sec"}, [el("h3", {text:"Native autofill logins (optional)"}), creds]));
  // The linked inbox (bot email for Workday verification / email-alert reading) now lives in Settings.

  // Section-jump nav (s-upload is the static import block above the form).
  const jump = [
    ["s-upload","Import résumé"],
    ["s-applicant","Applicant details"], ["s-location","Location"], ["s-languages","Languages"],
    ["s-experience","Experience"], ["s-activities","Activities"],
    ["s-projects","Projects"], ["s-education","Education"], ["s-skills","Skills"],
    ["s-resume-header","Résumé header"], ["s-screening","Screening answers"],
    ["s-accounts","Autofill accounts"], ["s-logins","Logins"],
    ["s-export","Back up / move"],
  ];
  const nav = el("div", {class:"pnav"}, jump.map(([id,label]) =>
    el("a", {href:"#", text:label, on:{click:(ev)=>{ ev.preventDefault(); const t = $(id); if (t) t.scrollIntoView({behavior:"smooth", block:"start"}); }}})));

  f.append(nav, ...secs);
}
function collectProfile() {
  const d = Object.assign({}, cardData($("profile-card")), cardData($("location-card")), cardData($("creds-card")));
  const tri = k => (d[k] === "yes" ? true : (d[k] === "no" ? false : null));
  const t = k => (d[k] || "").trim();
  // Compose the structured inputs back into the resolver's stored formats.
  const location = [t("city"), t("state")].filter(Boolean).join(", ");   // "City, ST" | "City" | "ST"
  const start_kind = t("start_date_kind");
  const earliest_start_date = start_kind === "specific" ? t("start_date_date") : start_kind;
  return {
    first_name:t("first_name"), last_name:t("last_name"), email:t("email"), phone:t("phone"), location:location,
    country:t("country"), how_heard:t("how_heard"),
    street_address:t("street_address"), postal_code:t("postal_code"),
    linkedin_url:t("linkedin_url"), github_url:t("github_url"), portfolio_url:t("portfolio_url"),
    work_authorized:tri("work_authorized"), requires_sponsorship:tri("requires_sponsorship"), us_citizen:tri("us_citizen"),
    willing_to_relocate:tri("willing_to_relocate"), open_to_remote:tri("open_to_remote"),
    work_arrangement:t("work_arrangement"),
    max_commute_miles: (parseInt(t("max_commute_miles"),10) || null),
    preferred_locations: (d["preferred_locations"]||"").split("\\n").map(s=>s.trim()).filter(Boolean),
    desired_salary:t("desired_salary"), earliest_start_date:earliest_start_date, years_experience:t("years_experience"),
    languages: cardsIn("sec-languages").map(c => { const d = cardData(c); return {name:(d.name||"").trim(), proficiency:(d.proficiency||"").trim()}; }).filter(x => x.name),
    gender:t("gender"), pronouns:t("pronouns"), race_ethnicity:t("race_ethnicity"), veteran_status:t("veteran_status"), disability_status:t("disability_status"),
    greenhouse_email:t("greenhouse_email"), greenhouse_quick_apply: t("greenhouse_quick_apply") === "yes",
    custom_answers: [...$("sec-qa").querySelectorAll(".card")].map(c => { const q = cardData(c); let opts=[]; try { opts = JSON.parse(q.options||"[]"); } catch(e){} return { question:(q.question||"").trim(), answer:(q.answer||"").trim(), maps_to:(q.maps_to||"").trim(), generated: q.generated === "1", seen_count: parseInt(q.seen_count||"0",10)||0, input_kind:(q.input_kind||""), options: Array.isArray(opts)?opts:[] }; }).filter(x => x.question || x.answer || x.maps_to),
  };
}
async function loadProfile() {
  $("profile-msg").textContent = "";
  busyInto($("profile-form"), "Loading your profile…", false);
  try {
    const [rd, pd] = await Promise.all([
      fetch("/resume?path=" + encodeURIComponent(currentResume())).then(r => r.json()),
      fetch("/profile").then(r => r.json()),
    ]);
    if (rd.error) throw new Error(rd.error);
    if (pd.error) throw new Error(pd.error);
    R = rd.resume; P = pd.profile; renderProfileForm();
  } catch (e) { $("profile-form").innerHTML = ""; $("profile-form").appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
  loadKeptResumes();
}
async function rankProjects() {
  const btn = $("rank-proj");
  btnBusy(btn, "Ranking…");
  const stop = busyInto($("rank-msg"), "Saving, then Claude is scoring your projects by technical impressiveness…", true);
  try {
    // Persist profile edits first (the rank endpoint saves the résumé itself), so the
    // reload below can't drop any unsaved screening/applicant changes on the same page.
    const rp = await (await fetch("/profile/update", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ data: collectProfile() }) })).json();
    if (!rp.ok) throw new Error(rp.error || "profile save failed");
    const r = await (await fetch("/resume/rank-projects", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ resume: currentResume(), data: collect() }) })).json();
    stop();
    if (!r.ok) throw new Error(r.error || "ranking failed");
    await loadProfile();   // reload résumé (now scored + reordered) and profile, then re-render
    const m = $("rank-msg"); m.className = "msg ok";
    m.textContent = "Ranked ✓  " + r.ranked.map(x => x[0] + " (★" + x[1] + ")").join("  ·  ");
  } catch (e) {
    stop(); btnDone(btn);
    const m = $("rank-msg"); m.className = "msg err"; m.textContent = String(e.message || e);
  }
}
async function saveProfile() {
  const btn = $("save-profile"), msg = $("profile-msg");
  btnBusy(btn, "Saving…"); msg.className = "msg busy";
  const stop = busyInto(msg, "Saving your profile…", false);
  try {
    const r1 = await (await fetch("/resume/update", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ resume: currentResume(), data: collect() }) })).json();
    if (!r1.ok) throw new Error(r1.error || "résumé save failed");
    const r2 = await (await fetch("/profile/update", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ data: collectProfile() }) })).json();
    if (!r2.ok) throw new Error(r2.error || "profile save failed");
    await loadProfile(); msg.className = "msg ok"; msg.textContent = "Saved ✓";
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { stop(); btnDone(btn); }
}
$("save-profile").addEventListener("click", saveProfile);

// Download the portable setup as a .zip (decision 188). The server returns JSON on failure, so a
// blocker ("no profile to export yet") lands inline next to the button instead of saving a file
// the user would only discover was broken later (UI Principle #3).
$("export-profile").addEventListener("click", async () => {
  const btn = $("export-profile"), msg = $("export-msg");
  btnBusy(btn, "Preparing…"); msg.className = "msg"; msg.textContent = "";
  try {
    const res = await fetch("/profile/export");
    if (!res.ok) { let e = {}; try { e = await res.json(); } catch (x) {} throw new Error(e.error || "Export failed"); }
    const name = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
    const url = URL.createObjectURL(await res.blob());
    const a = el("a", {href:url, download:(name ? name[1] : "applicationbot-profile.zip")});
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    msg.className = "msg ok"; msg.textContent = "Downloaded ✓";
  } catch (e) {
    msg.className = "msg err"; msg.textContent = String(e.message || e);
  } finally { btnDone(btn); }
});

// ---- Discovery settings editor (all of profile/discovery.yaml, from the dashboard) ----
function mkChk(key, checked) { const i = el("input", {type:"checkbox"}); i.checked = !!checked; if (key) i.dataset.k = key; return i; }
function chkRow(label, key, checked) { return el("label", {class:"chkrow"}, [mkChk(key, checked), " " + label]); }
function numFld(label, key, value) {
  const i = el("input", {type:"number", class:"f", value:(value==null?"":value)}); i.dataset.k = key;
  return el("div", {class:"fld"}, [el("label", {text:label}), i]);
}
function boardRow(b) {
  b = b || {};
  const sel = el("select", {class:"f bd-ats"}, ["greenhouse","lever","ashby","smartrecruiters","recruitee","workable"].map(a => el("option", {value:a, text:a})));
  sel.value = b.ats || "greenhouse";
  const tok = el("input", {class:"f bd-token", placeholder:"company slug — e.g. stripe", value:b.token || ""});
  const row = el("div", {class:"brd-row"});
  const del = el("button", {class:"del", type:"button", text:"✕", title:"Remove company", on:{click:()=>row.remove()}});
  row.append(sel, tok, del);
  return row;
}
let _discPreserve = {};   // filters fields with no form control — round-tripped so a save can't wipe them
function renderDiscForm(f, levels) {
  const form = $("disc-form"); form.innerHTML = "";
  // json_aggregators is enabled via the "New sources found" panel, and the tailoring/reuse settings
  // live in the Loop settings popup (decision 178) — neither has a control here, so both are
  // round-tripped: saving this form must not silently reset them.
  _discPreserve = { json_aggregators: f.json_aggregators || [],
                    tailor_mode: f.tailor_mode, tailor_below_fit: f.tailor_below_fit,
                    reuse_threshold: f.reuse_threshold };

  // Broad aggregators come first: they search across many companies and are the biggest lever
  // on how much discovery surfaces. The specific target-company list comes after them.
  const aggTestOut = el("div", {id:"agg-test-out", class:"agg-test-out"});
  const aggTestBtn = el("button", {class:"addbtn", type:"button", text:"Test aggregators",
    title:"Live-probe every configured aggregator with your current settings and report how many postings each returns — or the exact error",
    on:{click:()=>testAggregators(aggTestBtn, aggTestOut)}});
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Broad aggregators"}),
    el("div", {class:"editing", text:"Wide sources that search across many companies at once — the main drivers of how much discovery finds. Set these up first; your specific target companies come after."}),
    aggTestBtn, aggTestOut,
  ]));

  const a = f.adzuna || {};
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Adzuna aggregator (optional)"}),
    el("div", {class:"editing"}, [
      "A broad job aggregator spanning many companies. ",
      el("a", {class:"linkbtn", href:"https://developer.adzuna.com", target:"_blank", rel:"noopener",
               text:"Get a free Adzuna key ↗"}),
      " and paste it below — or use your own by setting the ",
      el("code", {text:"ADZUNA_APP_ID"}), " / ", el("code", {text:"ADZUNA_APP_KEY"}),
      " environment variables. Leave blank to search only your target companies below. Aggregator hits are auto-bridged to their real ATS and upgraded to the full job description.",
    ]),
    fld("App ID", "adz_app_id", a.app_id),
    fld("App key", "adz_app_key", a.app_key),
    fld("Country code — e.g. us", "adz_country", a.country || "us"),
    numFld("Max pages to fetch (50 results each)", "adz_max_pages", a.max_pages==null?1:a.max_pages),
  ]));

  const ec = f.early_career || {};
  const kinds = ec.kinds || ["new-grad","intern"];
  const kindBox = (v,label) => { const i = mkChk(null, kinds.includes(v)); i.dataset.eck = v; return el("label", {class:"chkrow"}, [i, " " + label]); };
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Early-career feeds (new-grad & internships)"}),
    el("div", {class:"editing", text:"Discover from community-curated GitHub lists of new-grad and internship roles — early-career by construction, no company list needed. Best when your target companies are senior-heavy. Only roles on ATSs we can fill (Greenhouse/Lever/Ashby/Workday/SmartRecruiters) are used."}),
    chkRow("Enable early-career feeds", "ec_enabled", ec.enabled),
    el("label", {text:"Include"}),
    el("div", {class:"lvls"}, [kindBox("new-grad","new-grad"), kindBox("intern","internships")]),
    numFld("How many top-matching listings to pull full descriptions for (per run)", "ec_max_resolve", ec.max_resolve==null?40:ec.max_resolve),
    area("Extra GitHub job boards (one raw listings.json URL per line; any repo using the SimplifyJobs schema)", "ec_feeds",
         (ec.feeds||[]).map(x => typeof x === "string" ? x : x.url).join("\\n"),
         "https://raw.githubusercontent.com/<owner>/<repo>/<branch>/.github/scripts/listings.json"),
  ]));

  const rb = f.remote_boards || {};
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Remote aggregators (keyless)"}),
    el("div", {class:"editing", text:"Public remote-job APIs — no signup, no key. Remote-only by construction, so most useful when you want remote roles; your filters still drop what doesn't fit."}),
    chkRow("Himalayas", "rb_himalayas", rb.himalayas),
    chkRow("RemoteOK", "rb_remoteok", rb.remoteok),
    numFld("Max results each contributes before filtering", "rb_max_results", rb.max_results==null?100:rb.max_results),
  ]));

  const ea = f.email_alerts || {};
  const eaProvs = ea.providers || [];
  const provBox = (v,label) => { const i = mkChk(null, eaProvs.includes(v)); i.dataset.alertp = v; return el("label", {class:"chkrow"}, [i, " " + label]); };
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Forwarded job-alert emails"}),
    el("div", {class:"editing", text:"Ingest job alerts from sites you already subscribe to, by forwarding their emails to your linked bot inbox (a Gmail filter → Forward). No second account, no scraping. Leads only: the apply links redirect out, so a lead becomes auto-applyable only if we can resolve it to a form we fill (Greenhouse/Lever/Ashby/SmartRecruiters/Recruitee/Workable/Workday). Link the bot inbox in the Profile tab first."}),
    chkRow("Enable forwarded email alerts", "ea_enabled", ea.enabled),
    el("label", {text:"Providers you're subscribed to (forward their alerts to the bot inbox)"}),
    el("div", {class:"lvls"}, [provBox("lensa","Lensa"), provBox("aflac","Aflac"), provBox("linkedin","LinkedIn")]),
    numFld("Newest alert emails to scan per provider", "ea_limit", ea.limit_per_provider==null?25:ea.limit_per_provider),
  ]));

  // Specific target companies — each is one company's public ATS board, polled directly.
  const boards = el("div", {id:"disc-boards", class:"cards"}, (f.boards||[]).map(boardRow));
  const addBoard = el("button", {class:"addbtn", type:"button", text:"+ Add company",
    on:{click:()=>boards.appendChild(boardRow({}))}});
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Target companies"}), boards, addBoard,
    el("div", {class:"editing", text:"Specific companies whose public ATS board we poll directly. Read the slug off the company's careers URL: boards.greenhouse.io/<token>, jobs.lever.co/<slug>, jobs.ashbyhq.com/<name>, jobs.smartrecruiters.com/<Company>, <company>.recruitee.com, apply.workable.com/<account>."}),
  ]));

  const lvlBoxes = el("div", {class:"lvls"}, levels.map(l => {
    const i = mkChk(null, (f.experience_levels||[]).includes(l)); i.dataset.lvl = l;
    return el("label", {class:"chkrow"}, [i, " " + l]);
  }));
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Filters"}),
    chkRow("Remote only — drop non-remote postings", "remote_only", f.remote_only),
    numFld("Minimum annual salary (0 = no floor; postings with no stated pay are kept)", "min_salary", f.min_salary),
    area("Exclude titles containing (one per line)", "title_exclude", (f.title_exclude||[]).join("\\n")),
    el("label", {text:"Experience levels (none checked = any level)"}),
    lvlBoxes,
  ]));

  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Matching"}),
    numFld("Min skill matches to keep a posting (keyword floor)", "min_skills", f.min_skills),
    numFld("How many top matches Claude judges for fit", "top_n", f.top_n),
    numFld("Minimum fit score to apply — 0-100 (dry-run/apply only follow through at or above this)", "min_fit", f.min_fit),
    chkRow("Auto-raise minimum fit above a score band your recorded outcomes prove gets no responses", "calibrate_min_fit", f.calibrate_min_fit),
    chkRow("Skip postings already in my tracker (don't re-surface)", "skip_seen", f.skip_seen),
    area("Aggregator search keywords (one per line; empty = derive from your résumé)", "keywords", (f.keywords||[]).join("\\n")),
  ]));
}
const discEl = k => $("disc-form").querySelector('[data-k="' + k + '"]');
const discVal = k => { const e = discEl(k); return e ? e.value : ""; };
const discChk = k => { const e = discEl(k); return e ? e.checked : false; };
const discInt = (k, d) => { const v = parseInt(discVal(k), 10); return isNaN(v) ? d : v; };
function collectDisc() {
  return {
    ..._discPreserve,   // json_aggregators etc. — preserved so saving this form can't wipe them
    boards: [...document.querySelectorAll("#disc-boards .brd-row")]
      .map(r => ({ ats: r.querySelector(".bd-ats").value, token: r.querySelector(".bd-token").value.trim() }))
      .filter(b => b.token),
    remote_only: discChk("remote_only"),
    min_salary: discInt("min_salary", 0),
    title_exclude: linesOf(discVal("title_exclude")),
    experience_levels: [...document.querySelectorAll("#disc-form [data-lvl]")].filter(c => c.checked).map(c => c.dataset.lvl),
    keywords: linesOf(discVal("keywords")),
    min_skills: discInt("min_skills", 2),
    top_n: discInt("top_n", 10),
    min_fit: discInt("min_fit", 50),
    calibrate_min_fit: discChk("calibrate_min_fit"),
    skip_seen: discChk("skip_seen"),
    adzuna: {
      app_id: discVal("adz_app_id").trim(),
      app_key: discVal("adz_app_key").trim(),
      country: discVal("adz_country").trim() || "us",
      max_pages: discInt("adz_max_pages", 1),
    },
    early_career: {
      enabled: discChk("ec_enabled"),
      kinds: [...document.querySelectorAll("#disc-form [data-eck]")].filter(c => c.checked).map(c => c.dataset.eck),
      max_resolve: discInt("ec_max_resolve", 40),
      feeds: linesOf(discVal("ec_feeds")),
    },
    remote_boards: {
      himalayas: discChk("rb_himalayas"),
      remoteok: discChk("rb_remoteok"),
      max_results: discInt("rb_max_results", 100),
    },
    email_alerts: {
      enabled: discChk("ea_enabled"),
      providers: [...document.querySelectorAll("#disc-form [data-alertp]")].filter(c => c.checked).map(c => c.dataset.alertp),
      limit_per_provider: discInt("ea_limit", 25),
    },
  };
}
async function loadDisc() {
  $("disc-msg").textContent = "";
  busyInto($("disc-form"), "Loading settings…", false);
  try {
    const d = await (await fetch("/discovery")).json();
    if (d.error) throw new Error(d.error);
    renderDiscForm(d.filters, d.levels);
  } catch (e) { $("disc-form").innerHTML = ""; $("disc-form").appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
}
async function saveDisc() {
  const btn = $("save-disc"), msg = $("disc-msg");
  btnBusy(btn, "Saving…"); msg.className = "msg busy";
  const stop = busyInto(msg, "Saving settings…", false);
  try {
    const r = await (await fetch("/discovery/update", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ data: collectDisc() }) })).json();
    if (!r.ok) throw new Error(r.error || "save failed");
    msg.className = "msg ok"; msg.textContent = "Saved ✓";
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { stop(); btnDone(btn); }
}
$("save-disc").addEventListener("click", saveDisc);

// Live-probe the configured aggregators (uses the CURRENT form values, so you can test before
// saving). Renders one ✓/✗ row per source with its sample count or exact error (UI Principle #3/#5).
async function testAggregators(btn, out) {
  btnBusy(btn, "Testing…");
  const stop = busyInto(out, "Probing each aggregator…", true);
  try {
    const r = await (await fetch("/aggregators/test", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ data: collectDisc() }) })).json();
    stop(); out.innerHTML = "";
    if (r.error) throw new Error(r.error);
    const res = r.results || [];
    if (!res.length) {
      out.appendChild(el("div", {class:"editing", text:"No aggregators configured to test. Add an Adzuna key, or enable early-career feeds / Himalayas / RemoteOK, then test again."}));
      return;
    }
    for (const s of res) {
      const row = el("div", {class:"agg-res"});
      const detail = s.ok
        ? (s.count > 0
            ? s.count + " posting" + (s.count === 1 ? "" : "s") + " in a quick sample" + (s.sample ? " — e.g. “" + s.sample + "”" : "")
            : "reachable, but 0 postings for your profile in a quick sample")
        : s.error;
      row.append(el("span", {class:"agg-dot " + (s.ok ? "ok" : "err")}),
                 el("b", {text:s.name}), el("span", {text:" — " + detail}));
      out.appendChild(row);
    }
    if (r.resume === false)
      out.appendChild(el("div", {class:"editing", text:"No profile/resume.yaml found — Adzuna, Google, RemoteOK and early-career feeds need a résumé to derive search terms, so they were skipped."}));
  } catch (e) { stop(); out.innerHTML = ""; out.appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
  finally { btnDone(btn); }
}

// ---- Sources overview (read-only "where & how" for the Discover tab) ----
function srcRow(label, text){ return el("div", {class:"editing"}, [el("b", {text: label + ": "}), text]); }
async function loadSources(){
  const box = $("sources-body");
  busyInto(box, "Loading sources…", false);
  try {
    const s = await (await fetch("/sources")).json();
    if (s.error) throw new Error(s.error);
    box.innerHTML = "";
    const bk = Object.keys(s.boards_by_ats || {});
    box.appendChild(srcRow("Target companies",
      bk.length ? bk.map(a => a + " (" + s.boards_by_ats[a].join(", ") + ")").join("  ·  ")
                : "none yet — add some in Discovery settings below"));
    const agg = s.aggregator || {};
    box.appendChild(srcRow("Adzuna aggregator",
      agg.active ? ("active — via " + agg.via + ", country " + agg.country)
                 : "not set up — add a free key in Discovery settings below"));
    const ec = s.early_career || {};
    box.appendChild(srcRow("New-grad & internship feeds",
      ec.enabled ? ("on (" + (ec.kinds || []).join(", ") + ")") : "off"));
    const rb = s.remote_boards || {};
    const rbOn = [rb.himalayas && "Himalayas", rb.remoteok && "RemoteOK"].filter(Boolean);
    box.appendChild(srcRow("Remote aggregators (keyless)",
      rbOn.length ? ("on — " + rbOn.join(", ")) : "off"));
    const ja = s.json_aggregators || [];
    box.appendChild(srcRow("JSON-API aggregators",
      ja.length ? ("on — " + ja.join(", ")) : "off — enable any found under “New sources found”"));
    const ct = s.contrib_sources || [];
    if (ct.length) box.appendChild(srcRow("Custom adapters", "on — " + ct.join(", ")));
    const ea = s.email_alerts || {};
    box.appendChild(srcRow("Forwarded job-alert emails",
      !ea.enabled ? "off"
        : !(ea.providers || []).length ? "on, but no providers selected — pick some in Discovery settings below"
        : !ea.linked ? ("on (" + ea.providers.join(", ") + ") — but no bot inbox is linked; link it in the Profile tab so alerts can be read")
        : ("on — " + ea.providers.join(", ") + " (leads; auto-appliable only when a link resolves to a fillable ATS)")));
    // Google Jobs is off by design (non-functional, decision 116) — only surface it if hand-enabled.
    if ((s.google || {}).enabled)
      box.appendChild(srcRow("Google Jobs",
        "on — but currently non-functional (Google renders Jobs client-side); use another aggregator"));
    box.appendChild(srcRow("Aggregator→ATS bridge",
      "on — aggregator hits are resolved to their real ATS and upgraded to the full job description"));
    box.appendChild(el("div", {class:"editing", text:"Forms we can auto-fill: " + (s.fillable_ats || []).join(", ") + "."}));
  } catch(e){ box.innerHTML = ""; box.appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
}

// ---- New sources found (source-scout candidates, decision 134) ----
// Lists validated, not-yet-configured boards the routine staged; one-click "Add" wires each into
// discovery.yaml (same idempotent path as the CLI --accept). Panel stays hidden when none staged.
async function loadCandidates(){
  const panel = $("new-sources"), box = $("candidates-body");
  try {
    const r = await (await fetch("/candidates")).json();
    if (r.error) throw new Error(r.error);
    const cs = r.candidates || [], specs = r.specs || [], contrib = r.contrib || [];
    if (!cs.length && !specs.length && !contrib.length) { panel.style.display = "none"; return; }  // nothing staged
    panel.style.display = "";
    box.innerHTML = "";
    // one-click add row: `detail` text + an Add button that POSTs `path` with `body` and,
    // on success, refreshes the boards form + sources overview. Each row's add is collected in
    // `adders` so "Add all" can drive exactly the same per-row path (same endpoints, same
    // per-row end-state) instead of a parallel bulk route that could drift from it.
    const adders = [];
    const addRow = (detail, path, body) => {
      const row = el("div", {class:"fit-rec"});
      row.appendChild(el("span", {text: detail}));
      const b = el("button", {type:"button", text:"Add"});
      // quiet: skip the per-add refresh; the "Add all" caller refreshes once at the end.
      // Returns "" on success, else the error message (so the bulk run can report failures).
      const add = async (quiet) => {
        if (b.disabled) return "";              // already added
        b.disabled = true; b.textContent = "Adding…";
        try {
          const res = await (await fetch(path, {method:"POST",
            headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)})).json();
          if (!res.ok) throw new Error(res.error || "Failed");
          b.textContent = "Added ✓";            // definite end-state (UI Principle #5)
          if (!quiet) { loadDisc(); loadSources(); }  // reflect it in the boards form + overview
          return "";
        } catch(e) { b.disabled = false; b.textContent = "Add"; return String(e.message || e); }
      };
      b.addEventListener("click", async () => { const err = await add(false); if (err) alert(err); });
      adders.push(add);
      row.appendChild(b);
      box.appendChild(row);
    };
    const roles = n => n + " open role" + (n === 1 ? "" : "s");
    cs.forEach(c => addRow(
      c.ats + ":" + c.token + " — " + roles(c.n_postings) + (c.sample_title ? " (e.g. “" + c.sample_title + "”)" : ""),
      "/candidates/accept", {ats: c.ats, token: c.token}));
    specs.forEach(s => addRow(
      s.name + " (aggregator) — " + roles(s.n_postings) + (s.sample_title ? " (e.g. “" + s.sample_title + "”)" : ""),
      "/candidates/accept-spec", {name: s.name}));
    contrib.forEach(c => addRow(
      c.name + " (custom adapter)" + (c.description ? " — " + c.description : ""),
      "/candidates/accept-contrib", {name: c.name}));
    // "Add all" — wire every staged source into discovery.yaml in one click. Sequential (each
    // add rewrites the same config file), with live "n of N" progress and a definite end state,
    // reusing the shared waiting pattern (UI Principle #5). Only shown when it saves a click.
    if (adders.length > 1) {
      const bar = el("div", {class:"fit-rec"});
      const note = el("span", {text: "Add all " + adders.length + " to discovery in one click."});
      const all = el("button", {type:"button", text: "Add all (" + adders.length + ")"});
      all.addEventListener("click", async () => {
        all.disabled = true;
        const errs = [];
        for (let i = 0; i < adders.length; i++) {
          all.textContent = "Adding " + (i + 1) + " of " + adders.length + "…";
          const err = await adders[i](true);
          if (err) errs.push(err);
        }
        loadDisc(); loadSources();             // one refresh for the whole batch
        const ok = adders.length - errs.length;
        all.textContent = errs.length ? "Added " + ok + " of " + adders.length : "Added all ✓";
        if (errs.length) {
          all.disabled = false;                 // the failed rows can be retried
          note.textContent = errs.length + " could not be added: " + errs.join(" · ")
            + " — press Add on those rows to retry.";
          bar.classList.add("msg", "err");
        } else {
          note.textContent = "All " + ok + " added — the next discovery run searches them.";
        }
      });
      bar.appendChild(note); bar.appendChild(all);
      box.insertBefore(bar, box.firstChild);
    }
  } catch(e){ panel.style.display = ""; box.innerHTML = ""; box.appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
}

// ---- Discover: chart how fit improves run over run (decision 046) -----------
// Wrap the chart with a window toggle. Default is Lifetime (all runs); the user can
// narrow to the most recent N when the history gets long.
function renderFitTrend(box, allRuns){
  if (!allRuns.length) return;
  const wrap = el("div", {class:"fit-trend"});
  const inner = el("div");
  const WINDOWS = [["Lifetime", 0], ["Last 30", 30], ["Last 10", 10]];
  const opts = WINDOWS.filter(([, n]) => n === 0 || allRuns.length > n);
  const draw = n => { inner.innerHTML = ""; drawFitChart(inner, n ? allRuns.slice(-n) : allRuns); };
  if (opts.length > 1) {
    const sel = el("select", {class:"fit-window"});
    opts.forEach(([label, n]) => sel.appendChild(el("option", {value:String(n), text:label})));
    sel.addEventListener("change", () => draw(parseInt(sel.value, 10) || 0));
    wrap.appendChild(el("div", {class:"fit-window-bar"},
      [el("label", {text:"Show"}), sel]));
  }
  wrap.appendChild(inner);
  box.appendChild(wrap);
  draw(0);   // default: lifetime
}

function drawFitChart(box, runs){
  if (!runs.length) return;
  const n = runs.length;
  const PADL = 20, PADR = 12, PADT = 8, PADB = 6, plotH = 78;   // room at left for 0/50/100
  const H = PADT + plotH + PADB;
  const W = PADL + Math.max(120, n * 24) + PADR;
  const xL = PADL, xR = W - PADR;
  const x = i => n === 1 ? (xL + xR) / 2 : xL + i * (xR - xL) / (n - 1);
  const y = v => PADT + (1 - Math.max(0, Math.min(100, v)) / 100) * plotH;
  const mf = runs[n - 1].min_fit;                               // current "your bar" line
  const best = runs.map((r, i) => x(i).toFixed(1) + "," + y(r.best_fit).toFixed(1)).join(" ");
  const mean = runs.map((r, i) => x(i).toFixed(1) + "," + y(r.mean_fit).toFixed(1)).join(" ");
  // recessive grid + baseline, with 0/50/100 reference labels
  let grid = "";
  for (const v of [100, 50, 0]) {
    const yy = y(v).toFixed(1);
    grid += `<line class="${v === 0 ? "fc-baseline" : "fc-grid"}" x1="${xL}" y1="${yy}" x2="${xR}" y2="${yy}"/>`
          + `<text class="fc-ylabel" x="${xL - 5}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end">${v}</text>`;
  }
  // filled area under the headline (best) series — only meaningful with ≥2 points
  const area = n >= 2
    ? `<polygon class="fc-area" points="${x(0).toFixed(1)},${y(0).toFixed(1)} ${best} ${x(n-1).toFixed(1)},${y(0).toFixed(1)}"/>`
    : "";
  const meanLine = n >= 2 ? `<polyline class="fc-mean" points="${mean}"/>` : "";
  const bestLine = n >= 2 ? `<polyline class="fc-best" points="${best}"/>` : "";
  // dashed threshold rule; labelled in the legend (no on-chart text — it collides with the peak)
  const thresh = `<line class="fc-bar" x1="${xL}" y1="${y(mf).toFixed(1)}" x2="${xR}" y2="${y(mf).toFixed(1)}"/>`;
  // visible dot + a wider transparent hit target carrying the hover tooltip
  const dots = runs.map((r, i) => {
    const cx = x(i).toFixed(1), cy = y(r.best_fit).toFixed(1);
    const t = `run ${i+1}: best ${r.best_fit}, mean ${r.mean_fit}, ${r.cleared}/${r.n_judged} cleared`;
    return `<circle class="fc-dot" cx="${cx}" cy="${cy}" r="2.8"/>`
         + `<circle cx="${cx}" cy="${cy}" r="8" fill="transparent"><title>${t}</title></circle>`;
  }).join("");
  const svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="max-width:100%" role="img">`
    + grid + area + thresh + meanLine + bestLine + dots + `</svg>`;
  const first = runs[0].best_fit, last = runs[n - 1].best_fit;
  const arrow = last > first ? "▲ improving" : (last < first ? "▼ down" : "▬ flat");
  box.appendChild(el("div", {class:"fit-head",
    text:`Results over ${n} run${n>1?"s":""}: best fit ${first} → ${last} (${arrow}); ${runs[n-1].cleared} above your bar this run`}));
  const chart = el("div"); chart.innerHTML = svg; box.appendChild(chart);
  const legend = el("div", {class:"fit-legend"});
  legend.innerHTML = `<span class="lg"><span class="sw best"></span>best fit</span>`
    + `<span class="lg"><span class="sw mean"></span>mean fit</span>`
    + `<span class="lg"><span class="sw bar"></span>your bar (min_fit ${mf})</span>`
    + `<span class="lg" style="color:var(--faint)">hover a point for that run's numbers</span>`;
  box.appendChild(legend);
}

// ---- Discover: what past runs taught the search (decision 046) --------------
async function loadFitInsights(){
  const panel = $("fit-insights"), box = $("fit-insights-body");
  try {
    const a = await (await fetch("/fit-insights")).json();
    if (a.error) throw new Error(a.error);
    if (!a.n_judged) { panel.style.display = "none"; return; }  // nothing learned yet
    panel.style.display = "";
    box.innerHTML = "";
    renderFitTrend(box, a.runs || []);   // improvement over time, run by run
    (a.lines || []).forEach((line, i) => box.appendChild(
      el("div", {class: i === 0 ? "fit-head" : "fit-line", text: line})));
    const recs = (a.recommendations || []);
    if (recs.length) {
      box.appendChild(el("div", {class:"editing", text:"Recommendations:", style:"margin-top:10px;font-weight:600"}));
      recs.forEach(r => {
        const row = el("div", {class:"fit-rec"});
        row.appendChild(el("span", {text: r.message}));
        if (r.field) {  // one-click applyable (experience_levels / min_fit)
          const b = el("button", {type:"button", text:"Apply"});
          b.addEventListener("click", async () => {
            b.disabled = true; b.textContent = "Applying…";
            const res = await (await fetch("/fit-insights/apply", {method:"POST",
              headers:{"Content-Type":"application/json"},
              body: JSON.stringify({field: r.field, value: r.value})})).json();
            if (res.ok) { b.textContent = "Applied ✓"; loadDisc(); setTimeout(loadFitInsights, 600); }
            else { b.disabled = false; b.textContent = "Apply"; alert(res.error || "Failed"); }
          });
          row.appendChild(b);
        }
        box.appendChild(row);
      });
    }
    renderPrescore(box, a.prescore);
  } catch(e){ panel.style.display = ""; box.innerHTML = ""; box.appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
}

// How well the zero-token pre-score tracks Claude's actual verdict for this résumé (decision
// 052/055): one bar per pre-score band, its height = mean actual fit, plus a one-line read.
function renderPrescore(box, ps) {
  const bands = (ps && ps.bands) || [];
  if (!bands.length) return;  // no pre-score history yet (pre-053 runs)
  box.appendChild(el("div", {class:"editing", style:"margin-top:12px;font-weight:600",
    text:"How well the quick pre-score predicts fit"}));
  const grid = el("div", {class:"ps-grid"});
  for (const b of bands) {
    const h = Math.max(4, Math.round(b.mean_fit));  // bar height ∝ mean actual fit (0-100)
    grid.append(el("div", {class:"ps-col"}, [
      el("div", {class:"ps-fit", text:String(Math.round(b.mean_fit))}),
      el("div", {class:"ps-bar-wrap"}, [el("div", {class:"ps-bar", style:"height:" + h + "%"})]),
      el("div", {class:"ps-band", text:b.band}),
      el("div", {class:"ps-n", text:"n=" + b.n}),
    ]));
  }
  box.appendChild(grid);
  box.appendChild(el("div", {class:"editing", style:"margin-top:4px",
    text:"quick pre-score band (x) → average actual fit Claude gave (bar)"}));
  if (ps.note) box.appendChild(el("div", {class:"fit-line", style:"margin-top:6px", text:ps.note}));
}

function escapeHtml(s){ const d=document.createElement("div"); d.textContent=s; return d.innerHTML; }

// ---- theme: light / dark, remembers choice, follows system by default ----
(function(){
  const root = document.documentElement, btn = document.getElementById("theme-toggle");
  const sysDark = () => window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const SVG = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
  const SUN = '<svg class="btn-ic" '+SVG+'><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
  const MOON = '<svg class="btn-ic" '+SVG+'><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
  let mode = "system"; try { mode = localStorage.getItem("ab-theme") || "system"; } catch(e){}
  function apply(m){
    mode = m;
    if (m === "dark" || m === "light") root.setAttribute("data-theme", m);
    else root.removeAttribute("data-theme");
    const dark = m === "dark" || (m !== "light" && sysDark());
    if (btn) btn.innerHTML = dark ? SUN + "Light" : MOON + "Dark";
    reflectTheme();
  }
  // Highlight the active choice in the Settings → Appearance segment (System / Light / Dark).
  window.reflectTheme = function(){
    document.querySelectorAll('#theme-seg button').forEach(b => b.classList.toggle("on", b.dataset.mode === mode));
  };
  window.setThemeMode = function(m){ try { localStorage.setItem("ab-theme", m); } catch(e){} apply(m); };
  apply(mode);
  // Footer button cycles light↔dark off the current effective theme; segment picks any of the three.
  if (btn) btn.addEventListener("click", () => {
    const dark = root.getAttribute("data-theme") === "dark"
      || (!root.getAttribute("data-theme") && sysDark());
    window.setThemeMode(dark ? "light" : "dark");
  });
  document.querySelectorAll('#theme-seg button').forEach(b =>
    b.addEventListener("click", () => window.setThemeMode(b.dataset.mode)));
})();

// ---- First-run tour — a spotlight walkthrough of what each section does ----------------------
// Replaces the old up-front chore checklist: a quick tour that highlights each nav tab in turn and
// says, in one line, what it's for (UI Principle #4). The two things that used to be checklist
// chores — "add details" and "choose jobs" — now surface as first-visit nudges where the user lands
// (see maybeShowNudge), because résumé import auto-fills the details. Auto-runs once on a fresh
// browser; reopenable any time from the nav "Take the tour" button.
(function(){
  const DONE_KEY = "ab-tour-done";
  const overlay = $("tour-overlay"), pop = $("tour-pop"), nav = document.querySelector("aside.nav");
  const STEPS = [
    { view:null, title:"👋 Welcome to ApplicationBot", body:"It finds jobs, tailors your résumé, and fills out applications for you — everything runs as a safe dry-run until you arm it. Here's a 20-second tour of the four sections." },
    { view:null, title:"🔑 How Claude tailors your résumé", body:"Primary: your Claude subscription via Claude Code (recommended — not metered; sign in inside Claude Code). Fallback: your own Anthropic API key (pay-per-token, separate from your subscription) — a third-party app can't use the subscription any other way. Neither? The free rules engine runs. Manage it anytime from the connection panel in the bottom-left, or in Settings." },
    { view:"profile", title:"👤 Profile", body:"Your details and résumé. Import your résumé and it fills these in automatically — the bot uses them to answer application questions truthfully." },
    { view:"discover", title:"🔍 Discover", body:"Choose what jobs to find, then run a dry-run: it searches, ranks every posting by fit, and either tailors your résumé only, or goes all the way and fills the application in a browser you can watch. You can also paste a posting nobody found for you." },
    { view:"track", title:"📊 Track", body:"Every application the bot discovered, tailored, and filled — with status, notes, and how much Claude each one cost." },
    { view:"settings", title:"⚙️ Settings", body:"Set-once configuration: your Claude connection, push notifications (desktop + phone), your linked inbox for account-gated portals, and light/dark theme." },
  ];
  let i = 0, startView = null, running = false;
  const spot = (v) => document.querySelectorAll(".tab").forEach(t => t.classList.toggle("tour-spot", !!v && t.dataset.view === v));

  function place(v){
    if (!v){ pop.classList.add("center"); pop.style.top = pop.style.left = ""; return; }
    pop.classList.remove("center");
    const tab = document.querySelector('.tab[data-view="' + v + '"]');
    const nr = nav.getBoundingClientRect(), tr = (tab || nav).getBoundingClientRect();
    pop.style.left = (nr.right + 14) + "px";
    let top = tr.top + tr.height / 2 - 27;                       // align the arrow (~27px down) with the tab
    top = Math.max(12, Math.min(top, window.innerHeight - pop.offsetHeight - 12));
    pop.style.top = top + "px";
  }

  function show(){
    const s = STEPS[i];
    if (s.view) showView(s.view);
    spot(s.view);
    $("tour-count").textContent = "Step " + (i + 1) + " of " + STEPS.length;
    $("tour-title").textContent = s.title;
    $("tour-body").textContent = s.body;
    $("tour-back").classList.toggle("hidden", i === 0);
    $("tour-next").textContent = i === STEPS.length - 1 ? "Get started →" : "Next →";
    place(s.view);
    place(s.view);                                               // twice: first render sets height, second re-clamps
    $("tour-next").focus();
  }

  function open(){
    running = true; TOUR_ACTIVE = true;
    const active = document.querySelector(".tab.active");
    startView = active ? active.dataset.view : "discover";
    document.body.classList.add("tour-on");
    overlay.classList.remove("hidden"); pop.classList.remove("hidden");
    i = 0; show();
  }
  function close(goProfile){
    running = false; TOUR_ACTIVE = false;
    try { localStorage.setItem(DONE_KEY, "1"); } catch(e){}
    spot(null);
    document.body.classList.remove("tour-on");
    overlay.classList.add("hidden"); pop.classList.add("hidden");
    showView(goProfile ? "profile" : (startView || "discover"));
  }

  $("tour-next").addEventListener("click", () => { if (i < STEPS.length - 1){ i++; show(); } else close(true); });
  $("tour-back").addEventListener("click", () => { if (i > 0){ i--; show(); } });
  $("tour-skip").addEventListener("click", () => close(false));
  $("tour-open").addEventListener("click", open);
  window.addEventListener("resize", () => { if (running) place(STEPS[i].view); });
  pop.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;                                 // keep focus inside the popover
    const f = Array.from(pop.querySelectorAll("button")).filter(b => !b.disabled && b.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && running) close(false); });

  // Cache readiness for the nudges, and auto-run the tour once per browser.
  (async () => {
    try { SETUP = await (await fetch("/setup/status")).json(); } catch(e){ SETUP = null; }
    let done = false; try { done = localStorage.getItem(DONE_KEY) === "1"; } catch(e){}
    if (!done) open();
  })();
})();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    parser = argparse.ArgumentParser(description="Local web UI for reviewing tailored resumes.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--version", action="version", version=f"ApplicationBot {__version__}")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"ApplicationBot review UI running at {url}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

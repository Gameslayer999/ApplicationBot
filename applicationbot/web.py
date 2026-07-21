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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import apply_profile, auth, catalogue, filters, impact, linkedin, tracker
from .job_description import JobDescription, load_job_description
from .backends import DEFAULT_QUALITY
from .length import LengthBudget
from .models import Resume, TailoredResume
from .pdf import render_pdf
from .render import render_html, render_markdown
from .resume import load_resume
from .tailor import tailor_resume

REPO_ROOT = Path(__file__).resolve().parent.parent

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
        "scanned": 0, "matched": 0, "judged": 0, "judged_total": 0,
        "from_cache": False, "cache_age_min": None, "can_research": False,
        "chosen": None, "report": None, "errors": [],
    }


def _set(**kw) -> None:
    with _TEST_LOCK:
        _TEST_STATE.update(kw)


def _test_worker(force_fresh: bool = False) -> None:
    """Run the full testing-mode pipeline in the background, updating _TEST_STATE.
    `force_fresh` bypasses the discovery snapshot cache and re-searches every board."""
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
            _set(phase="error", errors=["No target boards in profile/discovery.yaml. Add some in the Discover tab."])
            return

        use_claude = backends.claude_code_available()
        _set(step="discover", message=("Re-searching every board (ignoring cache)…" if force_fresh
                                       else "Discovering postings from your target boards…"))

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
        judged = [{
            "company": m.posting.company, "title": m.posting.title,
            "location": m.posting.location, "compensation": m.posting.compensation,
            "url": m.posting.url, "ats": m.posting.ats,
            "fit_score": m.fit_score, "qualified": m.qualified,
            "dimensions": m.dimensions or None,
            "why": m.why, "missing": (m.missing or [])[:3],
            "cleared": (m.fit_score is not None and m.fit_score >= min_fit),
        } for m in res.matches if m.fit_score is not None]
        cache_age_min = int((res.cache_age_seconds or 0) // 60) if res.from_cache else None
        _set(scanned=res.discovered, matched=len(res.matches), errors=res.errors,
             skipped_seen=res.skipped_seen, skipped_shown=res.skipped_shown, judged=judged,
             min_fit=min_fit, calib_note=calib_note, from_cache=res.from_cache,
             cache_age_min=cache_age_min)
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


def start_test_run(force_fresh: bool = False) -> dict:
    if _loop_running():
        return {"ok": False, "error": "The auto-apply loop is running (it owns the browser). "
                "Stop the loop first, or use its Apply buttons."}
    with _TEST_LOCK:
        if _TEST_STATE.get("phase") == "running":
            return {"ok": False, "error": "A test run is already in progress."}
        _TEST_STATE.clear()
        _TEST_STATE.update(_test_reset())
    threading.Thread(target=_test_worker, kwargs={"force_fresh": force_fresh}, daemon=True).start()
    return {"ok": True}


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
        report = run_apply(
            url, pdf, resolver, headed=True, pause=True,
            meta={"company": company, "role": role, "source_url": url,
                  "fit_score": app.get("fit_score") or None},
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


# --------------------------------------------------------------------------- auto-apply loop
# The "prepare-then-prompt" mode (decision 069): discover as many matches as possible, prepare
# each cleared one (tailor → PDF → headless dry-run fill) into a "Ready to apply" queue, and let
# the user submit each with one click. The loop core lives in autoloop.py (pure, tested); this is
# the web glue — one worker thread that OWNS the single browser slot for its lifetime, so the
# test-run / re-apply buttons are refused while it runs (they'd fight for the browser). User
# Apply clicks are enqueued and drained by the loop thread itself, keeping everything serialized.

_LOOP_LOCK = threading.Lock()
_LOOP_STATE: dict = {"running": False, "phase": "idle", "message": "", "prepared": 0,
                     "ready_ids": [], "current": None}
_LOOP_STOP = threading.Event()
_LOOP_SUBMITS: list[int] = []  # app-ids the user clicked "Apply" on, awaiting the loop thread


def _loop_reset() -> dict:
    return {"running": True, "phase": "starting", "message": "Starting…",
            "prepared": 0, "ready_ids": [], "current": None}


def _loop_set(**kw) -> None:
    with _LOOP_LOCK:
        _LOOP_STATE.update(kw)


def _loop_running() -> bool:
    with _LOOP_LOCK:
        return bool(_LOOP_STATE.get("running"))


def _loop_submit(app_id: int) -> None:
    """Armed one-shot submit of one prepared application, headless, on the loop thread. Reuses
    the per-click armed SafetyGate (decision 058): armed for exactly one submission, independent
    of profile/safety.yaml, still halted by the KILL file and the pre-submit required-field gate.
    A block/unconfirmed records that outcome — never a silent submit."""
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
        _loop_set(message=f"Can't submit {who}: its URL or tailored PDF is missing.")
        return
    _loop_set(phase="submitting", current={"company": company, "role": role, "fit": app.get("fit_score")},
              message=f"Submitting to {who}…")
    resolver = AnswerResolver(
        resume=load_resume("profile/resume.yaml"),
        profile=apply_profile.load_profile(),
        enable_generation=backends.claude_code_available(),
    )
    report = run_apply(
        url, pdf, resolver, headed=False, pause=False,
        meta={"company": company, "role": role, "source_url": url,
              "fit_score": app.get("fit_score") or None},
        gate=_reapply_gate(True))
    if report.submitted and report.submit_state == "submitted":
        _loop_set(message=f"Submitted to {who} — {report.confirmation or 'confirmation seen'}.")
    elif report.submit_state in ("unconfirmed", "blocked"):
        _loop_set(message=(f"{who}: not submitted ({report.submit_state}) — "
                           + (report.confirmation or "; ".join(report.blockers) or "see the tracker")))
    else:
        _loop_set(message=f"{who}: filled but not submitted (the arm did not take).")
    # Drop it from the ready list whatever the outcome — a submitted row is no longer 'dry-run',
    # and a re-blocked one moves to the parked panel; either way it shouldn't sit in "ready".
    with _LOOP_LOCK:
        if app_id in _LOOP_STATE["ready_ids"]:
            _LOOP_STATE["ready_ids"].remove(app_id)


def _loop_take_submits() -> list[int]:
    with _LOOP_LOCK:
        ids = list(_LOOP_SUBMITS)
        _LOOP_SUBMITS.clear()
    return ids


def _loop_worker(rescan: bool = False, force_retailor: bool = False) -> None:
    from . import autoloop, backends, pipeline
    from .filters import load_filters
    from .runner import cleared_queue

    try:
        resume = load_resume("profile/resume.yaml")
        filters = load_filters()
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
                "No target boards in Discovery settings. Add boards, then start the loop."))
            return

        min_fit, _ = pipeline.effective_min_fit(filters)

        # rescan (user opt-in): re-prepare postings that were already scored, REUSING their
        # cached fit scores (decision 037) — no board re-search, no Claude re-judge (a fit
        # score rarely changes between runs). Computed once up front from the freshest
        # snapshot; served as a single bounded batch so it re-prepares the set once, then
        # reports caught-up. If nothing is cached, bail with an actionable message rather than
        # silently doing nothing (UI principle #3).
        rescan_pool: list = []
        if rescan:
            rescan_pool = cleared_queue(
                pipeline.cached_matches(resume, filters, profile=profile), min_fit)
            if not rescan_pool:
                _loop_set(running=False, phase="caught_up", current=None, message=(
                    "Nothing recently scored to re-prepare. Start a normal auto-apply loop "
                    "first (it scores and caches matches); then re-check to re-prepare them "
                    "without re-scoring, while the cache is fresh."))
                return

        served = {"done": False}

        def discover_batch():
            if rescan:
                # One-shot: serve the pre-scored pool once, then empty ⇒ caught up.
                if served["done"]:
                    return []
                served["done"] = True
                return rescan_pool
            # only_new=True (decision 053/056): each search returns ONLY postings not judged
            # before, so no posting is ever re-judged — the loop spends judge tokens only on
            # genuinely new openings and stops when nothing new remains (user's token cap).
            res = pipeline.discover_and_match(resume, filters, profile=profile,
                                              use_claude=True, only_new=True)
            for e in res.errors:
                _loop_set(message=f"discovery note: {e}")
            return cleared_queue(res.matches, min_fit)

        def prepare_one(m):
            p = m.posting
            _loop_set(phase="preparing",
                      current={"company": p.company, "role": p.title, "fit": m.fit_score},
                      message=f"Preparing {p.company} — {p.title} (fit {m.fit_score})…")
            # Dry-run prepare (gate=None): run_testing_mode reuses the already-tailored PDF when
            # the résumé/profile haven't changed (stamp match) — re-fill only, no Claude
            # re-tailor. This is what makes a rescan of unchanged postings spend zero tokens.
            # force_retailor overrides that to regenerate the résumé anyway (the escape hatch).
            pipeline.run_testing_mode(
                resume, m, "profile/resume.yaml", apply_profile.DEFAULT_PATH,
                backend="auto", headed=False, slow_mo=0, pause=False, gate=None,
                force_retailor=force_retailor)
            row = tracker.find_by_source_url(p.url)
            with _LOOP_LOCK:
                _LOOP_STATE["prepared"] += 1
                # A clean dry-run row is "ready to apply"; a blocked one goes to the parked
                # panel instead (parking.py), so it never shows as ready.
                if row and row.get("status") == "dry-run" and row["id"] not in _LOOP_STATE["ready_ids"]:
                    _LOOP_STATE["ready_ids"].append(row["id"])

        def on_event(kind, payload=None):
            if kind == "searching":
                _loop_set(phase="searching", current=None,
                          message="Searching every board for new matches…")
            elif kind == "caught_up":
                _loop_set(phase="caught_up",
                          message="Caught up — no new matches to prepare.")

        reason = autoloop.auto_apply_loop(
            discover_batch, prepare_one, _loop_take_submits, _loop_submit,
            _LOOP_STOP.is_set, on_event=on_event)

        with _LOOP_LOCK:
            ready_n = len(_LOOP_STATE["ready_ids"])
        if reason == "caught_up":
            _loop_set(running=False, phase="caught_up", current=None, message=(
                f"Caught up — no new matches. {ready_n} application(s) ready for you to apply. "
                "Start the loop again later to re-search."))
        else:
            _loop_set(running=False, phase="stopped", current=None,
                      message=f"Loop stopped. {ready_n} application(s) ready for you to apply.")
    except Exception as e:
        _loop_set(running=False, phase="error", message=f"{type(e).__name__}: {e}")


def start_loop(rescan: bool = False, force_retailor: bool = False) -> dict:
    with _LOOP_LOCK:
        if _LOOP_STATE.get("running"):
            return {"ok": False, "error": "The auto-apply loop is already running."}
        _LOOP_STOP.clear()
        _LOOP_SUBMITS.clear()
        _LOOP_STATE.clear()
        _LOOP_STATE.update(_loop_reset())
    threading.Thread(target=_loop_worker, args=(rescan, force_retailor), daemon=True).start()
    return {"ok": True}


def stop_loop() -> dict:
    if not _loop_running():
        return {"ok": True, "already": True}
    _LOOP_STOP.set()
    _loop_set(message="Stopping after the current step finishes…")
    return {"ok": True}


def queue_submit(app_id: int) -> dict:
    """Apply to one prepared application. While the loop runs, enqueue it for the loop thread
    (which owns the browser); otherwise submit directly via the per-click armed re-apply."""
    with _LOOP_LOCK:
        running = bool(_LOOP_STATE.get("running"))
        if running and app_id not in _LOOP_SUBMITS:
            _LOOP_SUBMITS.append(app_id)
    if running:
        return {"ok": True, "queued": True}
    return start_reapply(app_id, arm=True)


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
            try:
                load_resume(p)
            except Exception:
                continue
            out.append({"path": str(p.relative_to(REPO_ROOT)), "label": f"{folder}/{p.name}"})
    return out


def list_fixtures() -> list[dict[str, str]]:
    out = []
    for p in sorted((REPO_ROOT / "fixtures" / "job_descriptions").glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        out.append({"path": str(p.relative_to(REPO_ROOT)), "label": p.name})
    return out


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
        jd = load_job_description(_allowlisted(job["fixture"], list_fixtures()))

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
        if path == "/profile":
            # Never serve the MyGreenhouse password to the browser (decision 060) — it lives in
            # the OS keychain. Send a boolean link status instead; the password input is write-only.
            prof = apply_profile.load_profile()
            d = prof.model_dump()
            d.pop("greenhouse_password", None)
            d["greenhouse_linked"] = apply_profile.greenhouse_linked(prof)
            self._json(200, {"profile": d})
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
                "bridge": {"enabled": True, "upgrade_ats": list(ATS_SOURCES)},
            })
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
            from . import resume_store
            rc = tracker.run_counts()
            # Claude token spend per posting, keyed by source URL (decision 095) — attached so the
            # Track table can show a per-application Tokens column that expands to the in/out split
            # and the per-activity breakdown (tailoring / form-entry / …).
            usage_by = tracker.usage_by_application()
            for a in apps:
                a["run_count"] = rc.get(a["id"], 0)
                a["has_jd"] = resume_store.has_jd(a.get("resume_path", ""))
                a["tokens"] = usage_by.get((a.get("source_url") or "").strip())
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
        if path == "/test-run/status":
            with _TEST_LOCK:
                self._json(200, dict(_TEST_STATE))
            return
        if path == "/loop/status":
            # Auto-apply loop state + the "Ready to apply" list (decision 069). The ready list
            # is resolved live from the tracker so a row that has since been submitted (status
            # left 'dry-run') or edited drops out automatically.
            with _LOOP_LOCK:
                st = dict(_LOOP_STATE)
                ready_ids = list(st.pop("ready_ids", []))
            ready = []
            for aid in ready_ids:
                a = tracker.get_application(aid)
                if a and a.get("status") == "dry-run":
                    ready.append({"id": aid, "company": a["company"], "role": a["role"],
                                  "fit": a.get("fit_score"), "portal": a["portal"],
                                  "url": a["source_url"]})
            st["ready"] = ready
            self._json(200, st)
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
            elif path == "/profile/update":
                p = json.loads(raw or b"{}")
                data = p.get("data") or {}
                # Route the MyGreenhouse password to the keychain (write-only), never the YAML
                # (decision 060). A blank value means "leave the stored password unchanged" — so an
                # ordinary profile save never wipes it; clearing is the explicit unlink below.
                if "greenhouse_password" in data:
                    pw = (data.pop("greenhouse_password") or "").strip()
                    if pw:
                        apply_profile.set_greenhouse_password(pw)
                apply_profile.replace_profile(data)
                self._json(200, {"ok": True})
            elif path == "/profile/greenhouse/unlink":
                apply_profile.set_greenhouse_password("")  # clear the keychain entry
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
                        mailbox.save_link(host, email, password, port)
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
            elif path == "/discovery/update":
                p = json.loads(raw or b"{}")
                filters.save_filters(filters.DiscoveryFilters.model_validate(p["data"]))
                self._json(200, {"ok": True})
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
            elif path == "/track/add":
                p = json.loads(raw or b"{}")
                app_id = tracker.add_application(p.get("data", {}))
                self._json(200, {"ok": True, "id": app_id})
            elif path == "/track/update":
                p = json.loads(raw or b"{}")
                changed = tracker.update_application(int(p["id"]), p.get("changes", {}))
                self._json(200, {"ok": True, "changed": changed})
            elif path == "/track/delete":
                p = json.loads(raw or b"{}")
                deleted = tracker.delete_application(int(p["id"]))
                self._json(200, {"ok": True, "deleted": deleted})
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
                self._json(200, start_test_run(bool(json.loads(raw or b"{}").get("fresh"))))
            elif path == "/test-run/close":
                _TEST_HOLD.set()  # release the review hold so the browser closes
                self._json(200, {"ok": True})
            elif path == "/loop/start":
                p = json.loads(raw or b"{}")
                self._json(200, start_loop(bool(p.get("rescan")), bool(p.get("retailor"))))
            elif path == "/loop/stop":
                self._json(200, stop_loop())
            elif path == "/loop/apply":
                # Apply to one prepared application. Cross-origin already rejected by the
                # do_POST origin guard (decision 062) — an armed submit is doubly safe there.
                p = json.loads(raw or b"{}")
                self._json(200, queue_submit(int(p["id"])))
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
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<link rel="apple-touch-icon" href="data:image/png;base64,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">
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
  /* Review controls bar */
  .controls { display:flex; flex-wrap:wrap; gap:12px 16px; align-items:flex-end; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:16px 18px; margin-bottom:22px; box-shadow:var(--shadow); }
  .controls .ctrl { display:flex; flex-direction:column; gap:5px; flex:1 1 160px; min-width:140px; }
  .controls .ctrl.wide { flex-basis:100%; }
  .controls .ctrl label { margin:0; }
  .controls .ctrl input, .controls .ctrl select { margin:0; }
  .controls .ctrl textarea { min-height:90px; margin-top:6px; }
  .controls button { width:auto; margin:0; padding:10px 18px; white-space:nowrap; }
  .controls .ctrl-go { flex:0 0 auto; }
  .controls .ctrl.hidden { display:none; }
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
  .mtiles.tight { max-width:840px; }
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
  /* Source URL: the URL is the link and takes the cell's width, truncating with an ellipsis;
     the ✎ button beside it swaps in the editable input. */
  .ttable .urlcell { display:flex; align-items:center; gap:2px; }
  .ttable .urlcell .urltext { flex:1; min-width:0; }
  /* contain:inline-size keeps the URL's own (very long, unbreakable) text out of the table's
     intrinsic width: without it the link stretches the column far past the width set here and on
     the resize handle, squeezing every other column. */
  .ttable .urllink { flex:1; min-width:0; contain:inline-size; color:var(--accent-text); text-decoration:none;
                     font-size:12px; padding:5px 6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ttable .urllink:hover { text-decoration:underline; }
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
  .card.qa-open .del { top:8px; right:8px; }
  /* Answered / auto-handled: compact two-column grid of collapsed cards to use the width. */
  .qa-answered { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  @media (max-width: 780px) { .qa-answered { grid-template-columns:1fr; } }
  .qa-tag { font-size:11px; font-weight:700; border-radius:99px; padding:1px 7px; margin-right:6px; white-space:nowrap; }
  .linkedin { border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:18px; background:var(--surface); }
  .linkedin input[type=file] { width:auto; border:0; padding:0; }
  .linkedin button { width:auto; margin:8px 8px 0 0; padding:7px 14px; }
  .linkedin code { background:var(--surface-2); padding:1px 4px; border-radius:3px; font-size:12px; }
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
  #parked-panel { border-left:4px solid var(--warn-line); padding-left:18px; }
  .pkcard { border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin-bottom:8px; background:var(--surface)df6; }
  .pk-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .pk-title { font-weight:700; font-size:14px; }
  .pk-tag { font-size:11.5px; font-weight:700; color:var(--warn); background:var(--warn-chip); border-radius:10px; padding:2px 9px; }
  .pk-detail { font-size:12.5px; color:var(--muted); margin:6px 0 8px; line-height:1.4; }
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
  .loop-apply { width:auto; margin:0; background:#b3261e; border-color:#b3261e; color:#fff; }
  .loop-apply:hover { background:#8f1e18; border-color:#8f1e18; }
  .loop-apply:disabled { opacity:.6; }
  .loop-rescan { display:flex; gap:8px; align-items:flex-start; margin-top:10px; font-size:12.5px;
                 color:var(--muted); line-height:1.4; max-width:560px;
                 text-transform:none; letter-spacing:normal; font-weight:400; }
  .loop-rescan input { width:auto; margin:2px 0 0; flex:0 0 auto; }
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
  .drawer-actions button, .drawer-actions a { width:auto; margin:0; padding:8px 14px; }
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
  .modal { background:var(--surface); border:1px solid var(--line); border-radius:12px; width:740px; max-width:100%; box-shadow:var(--shadow); }
  .modal-head { display:flex; align-items:center; gap:10px; padding:15px 20px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--surface); border-radius:12px 12px 0 0; z-index:1; }
  .modal-head h3 { margin:0; flex:1; font-size:16px; }
  .modal-x { width:auto; margin:0; padding:4px 10px; background:var(--surface-2); color:var(--muted); border:1px solid var(--line); font-size:15px; line-height:1; }
  .modal-x:hover { color:var(--ink); border-color:var(--muted); filter:none; }
  .modal-body { padding:18px 20px; }
  .modal-body .saverow { position:static; background:transparent; padding:14px 0 0; }
  .modal-body .saverow::after { display:none; }
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
      <button class="tab active" data-view="review"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/></svg>Review</button>
      <button class="tab" data-view="discover"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>Discover</button>
      <button class="tab" data-view="profile"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/></svg>Profile</button>
      <button class="tab" data-view="track"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>Track</button>
    </nav>
    <div class="nav-foot">
      <button id="tour-open" type="button"><svg class="btn-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/></svg>Take the tour</button>
      <button id="account" class="account" type="button" title="Manage Claude connection">Checking Claude sign-in…</button>
      <button id="theme-toggle" type="button" aria-label="Toggle dark mode"></button>
    </div>
  </aside>

  <main>
    <div class="brandmark" aria-hidden="true"></div>
    <div id="view-discover" class="hidden">
      <div id="discover-nudge" class="nudge hidden">
        <div class="nudge-b">First, tell the bot <b>what jobs to find</b> — set your roles, keywords,
          location, and pay in <b>Discovery settings</b> below. Then run a dry-run and watch it
          search, tailor, and fill one application (it never submits until you arm it).
          <br><button id="discover-nudge-go" class="nudge-go" type="button">Set what jobs to find →</button></div>
        <button id="discover-nudge-x" class="nudge-x" type="button" aria-label="Dismiss">✕</button>
      </div>
      <header class="page-head">
        <h2 class="page-title">Discover &amp; apply</h2>
        <p class="page-sub">Find matching openings, prepare each one (tailor, export, fill), and
          stack them up to apply. Everything runs as a dry-run until you arm submission.</p>
      </header>
      <div class="disc-actions">
        <button id="disc-open" type="button" class="tbtn"><svg class="btn-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>Discovery settings</button>
      </div>
      <div class="editor" id="loop-panel">
        <div class="panel-head">
          <h3>Auto-apply loop</h3>
          <button id="loop-start" type="button">▶ Start loop</button>
          <button id="loop-stop" type="button" class="hidden">■ Stop loop</button>
        </div>
        <p class="editing tight">Finds matches and prepares each one (tailor · export · fill) as a
          background dry-run — they stack up below as <b>Ready to apply</b>. Click <b>Apply&nbsp;▶</b>
          on one to submit just that application (confirms first). Stop anytime.</p>
        <span id="loop-msg" class="msg"></span>
        <label class="loop-rescan"><input type="checkbox" id="loop-rescan">
          <span class="rl-main"><span class="rl-t">Re-prepare postings I've already seen</span>
          <span class="rl-h">Re-fills every match from the last search, reusing cached fit scores &amp; tailored résumés — no Claude spend when nothing changed.</span></span></label>
        <label class="loop-rescan"><input type="checkbox" id="loop-retailor">
          <span class="rl-main"><span class="rl-t">Re-tailor from scratch</span>
          <span class="rl-h">Regenerate every résumé with Claude even when nothing changed — spends Claude usage on every posting.</span></span></label>
        <div id="loop-status" class="loopstat hidden"></div>
        <div id="loop-ready"></div>
      </div>
      <div class="editor" id="dry-run-panel">
        <div class="panel-head">
          <h3>Run a dry-run</h3>
          <button id="test-run" type="button">▶ Find &amp; fill one (dry-run)</button>
        </div>
        <p class="editing tight">One end-to-end pass: searches, ranks every posting by fit, then
          tailors and auto-fills the single best match in a browser you can watch. <b>Never
          submits</b> — review it, click Finish. Recorded in Track.</p>
        <span id="test-msg" class="msg"></span>
        <div id="test-progress" class="testprog hidden"></div>
        <div id="test-chosen" class="testchosen hidden"></div>
        <div id="test-judged" class="testjudged hidden"></div>
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
          <p class="editing tight">Every source feeding discovery — target boards by ATS, the
            aggregator, early-career feeds, and the aggregator→ATS bridge.</p>
          <div id="sources-body">Loading…</div>
        </div>
        <div class="editor" id="fit-insights" style="display:none">
          <h3 style="margin-top:0">What past runs taught the search</h3>
          <p class="editing tight">Every posting Claude judges is remembered, so runs steer scarce
            judge slots toward what scored highest for you. Below: what it learned and recommends.</p>
          <div id="fit-insights-body">Loading…</div>
        </div>
      </div>
    </div>

    <div id="view-review">
      <header class="page-head">
        <h2 class="page-title">Review &amp; tailor</h2>
        <p class="page-sub">Pick a résumé and a job posting, tailor it with your chosen engine,
          and read the result — relevance notes, factual-drift warnings, and which engine ran.</p>
      </header>
      <div class="controls">
        <div class="ctrl"><label for="resume">Résumé</label><select id="resume"></select></div>
        <div class="ctrl"><label for="jobmode">Job posting</label>
          <select id="jobmode">
            <option value="fixture">From a saved fixture</option>
            <option value="custom">Paste a posting</option>
          </select>
        </div>
        <div id="fixtureBox" class="ctrl"><label for="fixture">Fixture</label><select id="fixture"></select></div>
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
        <div class="ctrl ctrl-go"><button id="go">Tailor résumé</button></div>
      </div>
      <div id="status" class="empty">Pick a resume and job, then tailor.</div>
      <div id="meta" class="meta hidden"></div>
      <button id="dl-pdf" class="hidden">⬇ Download PDF</button>
      <span id="pdf-msg" class="msg"></span>
      <div class="reviewwrap">
        <div id="result"></div>
        <aside id="why-panel" class="why-panel hidden"></aside>
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

        <div id="profile-form">Loading…</div>

        <div id="s-linkedin" class="linkedin">
          <h3 style="margin-top:0">Import from LinkedIn</h3>
          <p class="editing">LinkedIn can't be linked live (their API restricts it and
            scraping breaks their terms). Instead, on LinkedIn go to <b>Settings → Data
            Privacy → Get a copy of your data</b>, download the archive, and upload it here
            (the <code>.zip</code>, or the Positions/Education/Skills <code>.csv</code>
            files). We'll merge new experience, education, and skills into the sections above
            (existing entries aren't touched).</p>
          <input id="li-file" type="file" accept=".zip,.csv">
          <button id="li-import" type="button">Import</button>
          <span id="li-msg" class="msg"></span>
        </div>

        <div class="saverow">
          <button id="save-profile">Save profile</button>
          <span id="profile-msg" class="msg"></span>
        </div>
      </div>
    </div>

    <div id="view-track" class="hidden">
      <div class="editor track-editor">
        <header class="page-head">
          <h2 class="page-title">Application tracker</h2>
          <p class="page-sub">Every application the pipeline discovered, tailored, and (in
            <code>dry_run</code>) would have submitted — the local system of record
            (<code>applications.db</code>, git-ignored). Edit any cell inline; changes save
            as you go. <b>Drag a column's right edge to resize it</b>, and use <b>Columns</b>
            to hide any you don't need — your layout is remembered on this browser.</p>
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
          <span id="track-msg" class="msg"></span>
        </div>
        <div id="track-feed" class="feed"></div>
        <div id="track-body" class="hidden">Loading…</div>
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
      <div class="saverow">
        <button id="save-disc">Save settings</button>
        <span id="disc-msg" class="msg"></span>
      </div>
    </div>
  </div>
</div>
<div id="claude-modal" class="modal-scrim hidden" role="dialog" aria-modal="true" aria-labelledby="claude-modal-title">
  <div class="modal">
    <div class="modal-head">
      <h3 id="claude-modal-title">Claude connection</h3>
      <button id="claude-modal-x" class="modal-x" type="button" aria-label="Close">✕</button>
    </div>
    <div class="modal-body">
      <p class="editing tight" id="claude-active"></p>
      <div class="conn-sec" id="claude-sub"></div>
      <div class="conn-sec" id="claude-key"></div>
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
$("account").addEventListener("click", openClaudeModal);

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
        + '<a href="https://claude.com/product/claude-code" target="_blank" rel="noopener">Get Claude Code ↗</a><br>'
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
      + '(<a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com ↗</a>) '
      + '— pay-per-token, <b>separate</b> from your subscription. Stored in your OS keychain, never in a file.</div>'
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

// Claude-connection modal (reuses the centered-modal pattern; re-checks status on open).
let CLAUDE_MODAL_PREV = null;
function openClaudeModal() {
  renderClaudeModal(AUTH); refreshAuth();
  CLAUDE_MODAL_PREV = document.activeElement;
  $("claude-modal").classList.remove("hidden");
  $("claude-modal-x").focus();
}
function closeClaudeModal() {
  $("claude-modal").classList.add("hidden");
  if (CLAUDE_MODAL_PREV && CLAUDE_MODAL_PREV.focus) CLAUDE_MODAL_PREV.focus();
}
$("claude-modal-x").addEventListener("click", closeClaudeModal);
$("claude-modal").addEventListener("click", (e) => { if (e.target === $("claude-modal")) closeClaudeModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("claude-modal").classList.contains("hidden")) closeClaudeModal();
});

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
$("go").addEventListener("click", async () => {
  const mode = $("jobmode").value;
  const payload = {
    resume: $("resume").value,
    backend: $("backend").value,
    quality: $("quality").value,
    pages: parseFloat($("pages").value),
    line_chars: parseInt($("linechars").value) || 100,
    job: mode === "custom"
      ? { mode:"custom", title:$("title").value, company:$("company").value, body:$("body").value }
      : { mode:"fixture", fixture:$("fixture").value },
  };
  const btn = $("go");
  const est = { fast: "~30s", balanced: "~40s", max: "up to ~2 min" }[$("quality").value] || "";
  btnBusy(btn, "Tailoring…");
  $("status").className = "empty"; $("status").classList.remove("hidden");
  const stop = busyInto($("status"), `Tailoring your résumé to this job (${est})…`, true);   // long Claude call — show elapsed
  $("meta").classList.add("hidden"); $("dl-pdf").classList.add("hidden"); $("pdf-msg").textContent = "";
  $("result").innerHTML = ""; $("why-panel").classList.add("hidden");
  try {
    const res = await fetch("/tailor", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "request failed");
    $("status").classList.add("hidden");
    let meta = `<span class="badge">engine: ${data.backend}</span> <span class="badge">${data.pages}pg</span> &nbsp; <b>${data.title}</b>${data.company ? " @ " + data.company : ""}`;
    if (data.notes && data.notes.length) meta += `<div class="notes"><b>Notes:</b> ${data.notes.map(escapeHtml).join(" ")}</div>`;
    if (data.warnings && data.warnings.length) meta += `<div class="warn"><b>⚠ Drift warnings:</b> ${data.warnings.map(escapeHtml).join("; ")}</div>`;
    $("meta").innerHTML = meta; $("meta").classList.remove("hidden");
    $("result").innerHTML = `<div class="resume">${data.html}</div>`;
    lastReq = { resume: $("resume").value, tailored: data.tailored };
    $("dl-pdf").classList.remove("hidden");
    showWhyIntro();

  } catch (e) {
    $("status").classList.remove("hidden");
    $("status").innerHTML = `<div class="warn">${escapeHtml(String(e.message || e))}</div>`;
  } finally {
    stop(); btnDone(btn);
  }
});

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

document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  const v = t.dataset.view;
  $("view-review").classList.toggle("hidden", v !== "review");
  $("view-discover").classList.toggle("hidden", v !== "discover");
  $("view-profile").classList.toggle("hidden", v !== "profile");
  $("view-track").classList.toggle("hidden", v !== "track");
  if (v === "profile") loadProfile();
  if (v === "track") loadTrack();
  if (v === "discover") { pollLoop(); loadParked(); loadSources(); loadFitInsights(); loadDisc(); pollTest(); }
  maybeShowNudge(v);
}));

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
  const e = $("s-linkedin"); if (e) e.scrollIntoView({behavior:"smooth", block:"center"});
});
$("discover-nudge-go").addEventListener("click", () => {
  dismissNudge("discover");
  openDiscModal();
});
$("resume").addEventListener("change", () => { if (!$("view-profile").classList.contains("hidden")) loadProfile(); });

// ---- Discover: run one full dry-run test ------------------------------------
let TEST_TIMER = null, TEST_T0 = null;
async function startTestRun(fresh) {
  const btn = $("test-run"), msg = $("test-msg");
  msg.className = "msg"; msg.textContent = "";
  btnBusy(btn, fresh ? "Re-searching…" : "Starting…");
  try {
    const r = await (await fetch("/test-run", {method:"POST", headers:{"Content-Type":"application/json"},
                                               body: JSON.stringify({fresh: !!fresh})})).json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = r.error || "Could not start."; btnDone(btn); return; }
    TEST_T0 = Date.now();
    pollTest();
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); btnDone(btn); }
}
$("test-run").addEventListener("click", () => startTestRun(false));

function testStepList(s) {
  const steps = [["discover","Discovering postings"],["match","Judging fit"],
                 ["tailor","Tailoring résumé"],["pdf","Exporting PDF"],
                 ["apply","Filling the form"],["review","Filled — review"]];
  const order = steps.map(x => x[0]);
  const cur = order.indexOf(s.step);
  return steps.map(([k,label],i) => {
    const done = (s.phase==="done") || (cur>i);
    const active = cur===i && s.phase!=="done";
    const mark = done ? "✓" : (active ? "●" : "○");
    return `<div class="tstep ${active?'act':''} ${done?'done':''}">${mark} ${label}</div>`;
  }).join("");
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
  html += `<div class="tcmeta"><a href="${escapeHtml(c.url)}" target="_blank" rel="noopener">${escapeHtml(c.url)}</a></div>`;
  return html;
}

function renderJudged(s) {
  const rows = s.judged || [];
  const cleared = rows.filter(r => r.cleared).length;
  const minFit = (s.min_fit != null) ? s.min_fit : 50;
  let html = `<div class="tjhead">Postings Claude judged this run — ${rows.length} scored, `
    + `${cleared} cleared your ${minFit}/100 cutoff. Denied ones are shown so you can see what the searches return.</div>`;
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
    html += `<div class="tjmeta"><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.url)}</a></div></div>`;
  }
  return html;
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
  if (s.judged && s.judged.length) { judged.classList.remove("hidden"); judged.innerHTML = renderJudged(s); }
  else judged.classList.add("hidden");

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
  if (s.phase === "done") { btnDone(btn); msg.className = "msg ok"; msg.textContent = s.message || "Done — recorded a dry-run row in Track."; loadFitInsights(); loadParked(); }
  if (s.phase === "error") { btnDone(btn); loadFitInsights(); loadParked(); }

  if (running || filled) { clearTimeout(TEST_TIMER); TEST_TIMER = setTimeout(pollTest, 1200); }
  else { btnDone(btn); }
}

// ---- Discover: auto-apply loop (decision 069) --------------------------------
// Poll /loop/status while the loop runs. The loop owns the browser, so preparation runs in the
// background (no window pops up); each prepared application appears as a "Ready to apply" card
// with an Apply ▶ button that submits just that one (armed, one-shot, confirmed first).
let LOOP_TIMER = null;
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
  start.classList.toggle("hidden", running);
  stop.classList.toggle("hidden", !running);
  $("loop-rescan").disabled = running;
  $("loop-retailor").disabled = running;
  if (running || (s.message && s.phase !== "idle")) {
    status.classList.remove("hidden");
    status.className = "loopstat" + (s.phase === "error" ? " err" : "");
    status.innerHTML = "";
    if (running && s.phase !== "caught_up") status.appendChild(el("span", {class:"spin"}));
    status.appendChild(el("span", {text: s.message || (running ? "Working…" : "")}));
    if (s.prepared) status.appendChild(el("span", {class:"lp-count", text: s.prepared + " prepared"}));
  } else {
    status.classList.add("hidden");
  }
  const list = (s && s.ready) || [];
  ready.innerHTML = "";
  if (list.length) {
    ready.appendChild(el("div", {class:"loop-ready-head", text:"Ready to apply (" + list.length + ")"}));
    list.forEach(a => ready.appendChild(loopReadyCard(a)));
  }
}

function loopReadyCard(a) {
  const title = (a.company || "—") + (a.role ? " — " + a.role : "");
  const tags = [];
  if (a.fit != null) tags.push("fit " + a.fit);
  if (a.portal) tags.push(a.portal);
  const head = el("div", {class:"pk-head"}, [
    el("span", {class:"pk-title", text:title}),
    tags.length ? el("span", {class:"pk-tag", text:tags.join(" · ")}) : null]);
  const apply = el("button", {class:"loop-apply", type:"button", text:"Apply ▶",
    title:"Submit this one application (irreversible) — confirms first",
    on:{click:(ev)=>applyReady(a.id, ev.target, title)}});
  return el("div", {class:"pkcard"}, [head, el("div", {class:"pk-actions"}, [apply])]);
}

// Submit one prepared application. While the loop runs it's queued for the loop thread (which owns
// the browser); if the loop is idle it falls back to the per-click armed re-apply, which drives the
// shared test-progress panel below. Always confirms first (irreversible).
async function applyReady(id, btn, who) {
  const ok = confirm("Really SUBMIT this application" + (who ? " to " + who : "") + "?\\n\\n"
    + "This is a real, irreversible submission. The bot fills the form and clicks Submit; the "
    + "pre-submit check still stops it if a required field is unanswered.");
  if (!ok) return;
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Submitting…"; }
  const msg = $("loop-msg"); msg.className = "msg"; msg.textContent = "";
  try {
    const r = await (await fetch("/loop/apply", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id })})).json();
    if (!r.ok) {
      if (btn) { btn.disabled = false; btn.textContent = label; }
      msg.className = "msg err"; msg.textContent = r.error || "Could not submit.";
      return;
    }
    if (r.queued) { if (btn) btn.textContent = "Queued…"; }
    else { pollTest(); }  // loop idle → start_reapply drives the shared progress panel
    pollLoop();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    msg.className = "msg err"; msg.textContent = String(e.message || e);
  }
}

$("loop-start").addEventListener("click", async () => {
  const btn = $("loop-start"), msg = $("loop-msg");
  msg.className = "msg"; msg.textContent = "";
  btnBusy(btn, "Starting…");
  try {
    const rescan = $("loop-rescan").checked, retailor = $("loop-retailor").checked;
    const r = await (await fetch("/loop/start", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({rescan, retailor})})).json();
    btnDone(btn);
    if (!r.ok) { msg.className = "msg err"; msg.textContent = r.error || "Could not start."; return; }
    pollLoop();
  } catch (e) { btnDone(btn); msg.className = "msg err"; msg.textContent = String(e.message || e); }
});

$("loop-stop").addEventListener("click", async () => {
  const btn = $("loop-stop"); btnBusy(btn, "Stopping…");
  try { await fetch("/loop/stop", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}); }
  catch (e) {}
  btnDone(btn);
  pollLoop();
});

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
  if (p.resumable) {
    actions.append(el("button", {class:"pk-fix pk-reapply", type:"button", text:"Re-apply (dry-run) ▶",
      title:"Re-fill this posting with your updated answers — never submits",
      on:{click:(ev)=>reapplyParked(p.id, ev.target, false)}}));
    // Per-click armed submit (decision 058): really submits THIS one application, with a confirm.
    // Independent of profile/safety.yaml; the KILL file + the pre-submit required-field gate still apply.
    actions.append(el("button", {class:"pk-fix pk-submit", type:"button", text:"Submit for real ▶",
      title:"Actually submit this application (irreversible) — confirms first",
      on:{click:(ev)=>reapplyParked(p.id, ev.target, true, title)}}));
  }
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
  const msg = $("test-msg");
  if (btn) { btn.disabled = true; btn.textContent = arm ? "Submitting…" : (retailor ? "Re-tailoring…" : "Starting…"); }
  if (msg) { msg.className = "msg"; msg.textContent = ""; }
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
  const kids = [el("div", {class:"fc-main"}, [el("div", {class:"fc-title", text:title}), meta])];
  if (app.fit_score != null && app.fit_score !== "") kids.push(el("span", {class:"fc-fit", text:"fit " + app.fit_score}));
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
  const acts = el("div", {class:"drawer-actions"});
  if (app.source_url && /^https?:/i.test(app.source_url))
    acts.append(el("a", {href:app.source_url, target:"_blank", class:"tbtn", text:"Open posting ↗"}));
  if (app.resume_path)
    acts.append(el("a", {href:"/track/resume?id=" + app.id, target:"_blank", class:"tbtn", text:"View résumé ↗"}));
  if (app.status === "dry-run" && app.source_url)
    acts.append(el("button", {class:"rerun", type:"button", text:"Re-run ▶",
      title:"Re-fill this posting in a watchable browser (dry-run — nothing is submitted)",
      on:{click:(ev) => rerunDry(app, ev.target, false)}}));
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
    text:"Discovery & judging spend (all-time) — shared across candidates, not charged to any one application."});
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
    // The column is narrow, so the URL renders truncated with an ellipsis — the title makes the
    // full URL readable on hover without widening the column.
    cell.append(
      el("a", {class:"urllink", href:app.source_url, target:"_blank", rel:"noopener noreferrer",
        title:"Open " + app.source_url, text:app.source_url}),
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
    await loadProfile();
    msg.className = "msg ok";
    msg.textContent = `Imported ${a.experience||0} experience, ${a.education||0} education, ${a.skills||0} skills`
      + ((d.found_files||[]).length ? " (from " + d.found_files.join(", ") + ")." : " — no LinkedIn CSVs found in that file.");
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
// The answer input, recreated as the form's real control: a dropdown when the field had options
// (so the answer matches at fill time), else a free-text box.
function qaAnswerInput(qa, cls) {
  const opts = Array.isArray(qa.options) ? qa.options.filter(Boolean) : [];
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
  const ghOK = !!P.greenhouse_linked;  // password lives in the keychain, not P (decision 060)
  const card = el("div", {class:"card"}, [
    el("p", {class:"hint", text:"Which native autofills the Apply stage can use. Greenhouse uses your MyGreenhouse login (set below); Lever/Ashby/Workday parse your uploaded résumé and need no account."}),
    acctRow("MyGreenhouse", ghOK, ghOK ? ("Connected · " + P.greenhouse_email) : "Not set up — add credentials below"),
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

  // Applicant details (apply profile) — the primary form-autofill identity.
  const loc = parseLocation(P.location);
  const applicant = el("div", {id:"profile-card", class:"card"});
  applicant.append(
    row2(fld("First name","first_name",P.first_name), fld("Last name","last_name",P.last_name)),
    row2(fld("Email","email",P.email), fld("Phone","phone",P.phone)),
    selField("Country","country", P.country || "United States", COUNTRIES),
    row2(selField("State","state", loc.state, STATE_OPTS), fld("City","city", loc.city)),
    row2(fld("LinkedIn URL","linkedin_url",P.linkedin_url), fld("GitHub URL","github_url",P.github_url)),
    fld("Portfolio / website","portfolio_url",P.portfolio_url),
    row2(boolSel("Authorized to work?","work_authorized",P.work_authorized), boolSel("Requires sponsorship?","requires_sponsorship",P.requires_sponsorship)),
    row2(boolSel("U.S. citizen?","us_citizen",P.us_citizen), boolSel("Willing to relocate?","willing_to_relocate",P.willing_to_relocate)),
    boolSel("Open to remote?","open_to_remote",P.open_to_remote),
    row2(selField("Preferred work arrangement","work_arrangement",P.work_arrangement||"",WORK_ARRANGEMENT_OPTS),
         fld("Max commute (miles) — for 'commutable' judgement","max_commute_miles",P.max_commute_miles==null?"":String(P.max_commute_miles))),
    area("Preferred office locations (one per line, most preferred first — e.g. 'New York, NY', 'Remote')","preferred_locations",(P.preferred_locations||[]).join("\\n")),
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
  const linked = !!P.greenhouse_linked;
  // The password is write-only: never sent to the browser (it's in the OS keychain). Leave blank
  // to keep the saved one; type a new one to replace it; Disconnect to remove it.
  const passWrap = el("div", {class:"fld"}, [
    el("label", {text:"MyGreenhouse password"}),
    el("input", {class:"f", "data-k":"greenhouse_password", type:"password",
                 placeholder: linked ? "•••••••• saved — leave blank to keep" : "app password"})]);
  const ghControls = el("div", {class:"subhint", style:"margin-top:6px"}, [
    el("span", {text: linked ? "🔒 Password saved in your OS keychain." : "🔓 No password saved yet."})]);
  if (linked) {
    const dc = el("button", {class:"linklike", type:"button", text:"Disconnect", style:"margin-left:8px",
      on:{click: async ()=>{
        dc.disabled = true; dc.textContent = "Disconnecting…";
        await fetch("/profile/greenhouse/unlink", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
        loadProfile();
      }}});
    ghControls.append(dc);
  }
  creds.append(
    el("p", {class:"hint", text:"Optional. If set, the Apply stage logs into Greenhouse's own MyGreenhouse account and uses its autofill first, then fills the rest. Email is stored in your git-ignored profile; the password is stored in your OS keychain, never in a file."}),
    row2(fld("MyGreenhouse email","greenhouse_email",P.greenhouse_email), passWrap),
    ghControls);
  put("s-logins", el("div", {class:"sec"}, [el("h3", {text:"Native autofill logins (optional)"}), creds]));

  // Bot email for account-gated portals (Workday) — its own secure store (keychain), not the profile YAML.
  put("s-botemail", mailboxPanel());

  // Section-jump nav (s-linkedin is the static import block below the form).
  const jump = [
    ["s-applicant","Applicant details"], ["s-experience","Experience"], ["s-activities","Activities"],
    ["s-projects","Projects"], ["s-education","Education"], ["s-skills","Skills"],
    ["s-resume-header","Résumé header"], ["s-screening","Screening answers"],
    ["s-accounts","Autofill accounts"], ["s-logins","Logins"], ["s-botemail","Bot email"],
    ["s-linkedin","LinkedIn import"],
  ];
  const nav = el("div", {class:"pnav"}, jump.map(([id,label]) =>
    el("a", {href:"#", text:label, on:{click:(ev)=>{ ev.preventDefault(); const t = $(id); if (t) t.scrollIntoView({behavior:"smooth", block:"start"}); }}})));

  f.append(nav, ...secs);
}
function collectProfile() {
  const d = Object.assign({}, cardData($("profile-card")), cardData($("creds-card")));
  const tri = k => (d[k] === "yes" ? true : (d[k] === "no" ? false : null));
  const t = k => (d[k] || "").trim();
  // Compose the structured inputs back into the resolver's stored formats.
  const location = [t("city"), t("state")].filter(Boolean).join(", ");   // "City, ST" | "City" | "ST"
  const start_kind = t("start_date_kind");
  const earliest_start_date = start_kind === "specific" ? t("start_date_date") : start_kind;
  return {
    first_name:t("first_name"), last_name:t("last_name"), email:t("email"), phone:t("phone"), location:location,
    country:t("country"), how_heard:t("how_heard"),
    linkedin_url:t("linkedin_url"), github_url:t("github_url"), portfolio_url:t("portfolio_url"),
    work_authorized:tri("work_authorized"), requires_sponsorship:tri("requires_sponsorship"), us_citizen:tri("us_citizen"),
    willing_to_relocate:tri("willing_to_relocate"), open_to_remote:tri("open_to_remote"),
    work_arrangement:t("work_arrangement"),
    max_commute_miles: (parseInt(t("max_commute_miles"),10) || null),
    preferred_locations: (d["preferred_locations"]||"").split("\\n").map(s=>s.trim()).filter(Boolean),
    desired_salary:t("desired_salary"), earliest_start_date:earliest_start_date, years_experience:t("years_experience"),
    gender:t("gender"), pronouns:t("pronouns"), race_ethnicity:t("race_ethnicity"), veteran_status:t("veteran_status"), disability_status:t("disability_status"),
    greenhouse_email:t("greenhouse_email"), greenhouse_password:t("greenhouse_password"),
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
    R = rd.resume; P = pd.profile; renderProfileForm(); loadMailbox();
  } catch (e) { $("profile-form").innerHTML = ""; $("profile-form").appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
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
  const tok = el("input", {class:"f bd-token", placeholder:"board token / company slug — e.g. stripe", value:b.token || ""});
  const row = el("div", {class:"brd-row"});
  const del = el("button", {class:"del", type:"button", text:"✕", title:"Remove board", on:{click:()=>row.remove()}});
  row.append(sel, tok, del);
  return row;
}
function renderDiscForm(f, levels) {
  const form = $("disc-form"); form.innerHTML = "";

  const boards = el("div", {id:"disc-boards", class:"cards"}, (f.boards||[]).map(boardRow));
  const addBoard = el("button", {class:"addbtn", type:"button", text:"+ Add board",
    on:{click:()=>boards.appendChild(boardRow({}))}});
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Target boards"}), boards, addBoard,
    el("div", {class:"editing", text:"Companies whose public ATS board to poll. Read the token/slug off the careers URL: boards.greenhouse.io/<token>, jobs.lever.co/<slug>, jobs.ashbyhq.com/<name>, jobs.smartrecruiters.com/<Company>, <company>.recruitee.com, apply.workable.com/<account>."}),
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

  const ec = f.early_career || {};
  const kinds = ec.kinds || ["new-grad","intern"];
  const kindBox = (v,label) => { const i = mkChk(null, kinds.includes(v)); i.dataset.eck = v; return el("label", {class:"chkrow"}, [i, " " + label]); };
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Early-career feeds (new-grad & internships)"}),
    el("div", {class:"editing", text:"Discover from community-curated GitHub lists of new-grad and internship roles — early-career by construction, no company list needed. Best when your target boards are senior-heavy. Only roles on ATSs we can fill (Greenhouse/Lever/Ashby/Workday/SmartRecruiters) are used."}),
    chkRow("Enable early-career feeds", "ec_enabled", ec.enabled),
    el("label", {text:"Include"}),
    el("div", {class:"lvls"}, [kindBox("new-grad","new-grad"), kindBox("intern","internships")]),
    numFld("How many top-matching listings to pull full descriptions for (per run)", "ec_max_resolve", ec.max_resolve==null?40:ec.max_resolve),
    area("Extra GitHub job boards (one raw listings.json URL per line; any repo using the SimplifyJobs schema)", "ec_feeds",
         (ec.feeds||[]).map(x => typeof x === "string" ? x : x.url).join("\\n"),
         "https://raw.githubusercontent.com/<owner>/<repo>/<branch>/.github/scripts/listings.json"),
  ]));

  const a = f.adzuna || {};
  form.appendChild(el("div", {class:"sec"}, [
    el("h4", {text:"Adzuna aggregator (optional)"}),
    el("div", {class:"editing"}, [
      "A broad job aggregator beyond your target boards. Get a free key at ",
      el("a", {href:"https://developer.adzuna.com", target:"_blank", rel:"noopener", text:"developer.adzuna.com"}),
      " and paste it below — or use your own by setting the ",
      el("code", {text:"ADZUNA_APP_ID"}), " / ", el("code", {text:"ADZUNA_APP_KEY"}),
      " environment variables. Leave blank to search only the boards above. Aggregator hits are auto-bridged to their real ATS and upgraded to the full job description.",
    ]),
    fld("App ID", "adz_app_id", a.app_id),
    fld("App key", "adz_app_key", a.app_key),
    fld("Country code — e.g. us", "adz_country", a.country || "us"),
    numFld("Max pages to fetch (50 results each)", "adz_max_pages", a.max_pages==null?1:a.max_pages),
  ]));
}
const discEl = k => $("disc-form").querySelector('[data-k="' + k + '"]');
const discVal = k => { const e = discEl(k); return e ? e.value : ""; };
const discChk = k => { const e = discEl(k); return e ? e.checked : false; };
const discInt = (k, d) => { const v = parseInt(discVal(k), 10); return isNaN(v) ? d : v; };
function collectDisc() {
  return {
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
    box.appendChild(srcRow("Target boards",
      bk.length ? bk.map(a => a + " (" + s.boards_by_ats[a].join(", ") + ")").join("  ·  ")
                : "none yet — add some in Discovery settings below"));
    const agg = s.aggregator || {};
    box.appendChild(srcRow("Adzuna aggregator",
      agg.active ? ("active — via " + agg.via + ", country " + agg.country)
                 : "not set up — add a free key in Discovery settings below"));
    const ec = s.early_career || {};
    box.appendChild(srcRow("New-grad & internship feeds",
      ec.enabled ? ("on (" + (ec.kinds || []).join(", ") + ")") : "off"));
    box.appendChild(srcRow("Aggregator→ATS bridge",
      "on — aggregator hits are resolved to their real ATS and upgraded to the full job description"));
    box.appendChild(el("div", {class:"editing", text:"Forms we can auto-fill: " + (s.fillable_ats || []).join(", ") + "."}));
  } catch(e){ box.innerHTML = ""; box.appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
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
  function apply(mode){
    if (mode === "dark" || mode === "light") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
    const dark = mode === "dark" || (mode !== "light" && sysDark());
    if (btn) btn.innerHTML = dark ? SUN + "Light" : MOON + "Dark";
  }
  let saved = null; try { saved = localStorage.getItem("ab-theme"); } catch(e){}
  apply(saved || "system");
  if (btn) btn.addEventListener("click", () => {
    const dark = root.getAttribute("data-theme") === "dark"
      || (!root.getAttribute("data-theme") && sysDark());
    const next = dark ? "light" : "dark";
    try { localStorage.setItem("ab-theme", next); } catch(e){}
    apply(next);
  });
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
    { view:null, title:"🔑 How Claude tailors your résumé", body:"Primary: your Claude subscription via Claude Code (recommended — not metered; sign in inside Claude Code). Fallback: your own Anthropic API key (pay-per-token, separate from your subscription) — a third-party app can't use the subscription any other way. Neither? The free rules engine runs. Manage it anytime from the panel in the bottom-left." },
    { view:"profile", title:"👤 Profile", body:"Your details and résumé. Import your résumé and it fills these in automatically — the bot uses them to answer application questions truthfully." },
    { view:"discover", title:"🔍 Discover", body:"Choose what jobs to find, then let the bot search, rank every posting by how well it fits you, tailor your résumé, and fill the application — a dry-run you can watch." },
    { view:"review", title:"📝 Review", body:"See each tailored résumé and why it was written that way, and fine-tune it before it's used." },
    { view:"track", title:"📊 Track", body:"Every application the bot discovered, tailored, and filled — with status, notes, and how much Claude each one cost." },
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
    if (s.view){ const t = document.querySelector('.tab[data-view="' + s.view + '"]'); if (t) t.click(); }
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
    startView = active ? active.dataset.view : "review";
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
    const to = goProfile ? "profile" : (startView || "review");
    const t = document.querySelector('.tab[data-view="' + to + '"]'); if (t) t.click();
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

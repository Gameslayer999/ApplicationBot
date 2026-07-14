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
        _set(phase="done", step="done",
             message="Done — browser closed. A dry-run row was recorded in Track.",
             report={"summary": report.summary(), "submitted": report.submitted,
                     "url": report.url, "screenshot": report.screenshot})
    except Exception as e:
        _set(phase="error", errors=[f"{type(e).__name__}: {e}"])


def start_test_run(force_fresh: bool = False) -> dict:
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


def _reapply_worker(app_id: int, *, arm: bool = False) -> None:
    """Resume a parked application (decision 049): re-drive the DETERMINISTIC fill on the same
    posting URL with the stored tailored PDF, now that the user has resolved the block (answered
    the question, stored the login). No re-discovery, no re-tailoring — the answer/profile change
    is all that's new, so the same form fills further.

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


def start_reapply(app_id: int, *, arm: bool = False) -> dict:
    with _TEST_LOCK:
        if _TEST_STATE.get("phase") == "running":
            return {"ok": False, "error": "A run is already in progress — let it finish first."}
        _TEST_STATE.clear()
        _TEST_STATE.update(_test_reset())
    threading.Thread(target=_reapply_worker, kwargs={"app_id": app_id, "arm": arm},
                     daemon=True).start()
    return {"ok": True}


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
            self._json(200, {
                "applications": tracker.list_applications(
                    status=(q.get("status", [""])[0] or None),
                    search=(q.get("search", [""])[0] or None),
                ),
                "counts": tracker.status_counts(),
                "funnel": tracker.funnel_report(),
                "statuses": tracker.STATUSES,
                "fields": tracker.EDITABLE,
            })
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
            self._send(200, f.read_bytes(), "application/pdf",
                       {"Content-Disposition": 'inline; filename="' + f.name + '"'})
            return
        if path == "/test-run/status":
            with _TEST_LOCK:
                self._json(200, dict(_TEST_STATE))
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
                self._json(200, start_reapply(int(p["id"]), arm=bool(p.get("arm"))))
            elif path == "/test-run":
                self._json(200, start_test_run(bool(json.loads(raw or b"{}").get("fresh"))))
            elif path == "/test-run/close":
                _TEST_HOLD.set()  # release the review hold so the browser closes
                self._json(200, {"ok": True})
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
<style>
  :root {
    --bg:#f5f6f8; --surface:#ffffff; --surface-2:#eef1f5;
    --ink:#1a1c22; --strong:#0c0e13; --muted:#68727f; --faint:#98a1ad;
    --line:#e6e8ec; --accent:#2f68f5; --accent-ink:#ffffff; --ai:#6a4bd0;
    --accent-weak:#e9f0ff; --accent-weak-2:#d7e3ff;
    --ok:#1a9d54; --ok-bg:#eaf6ee; --ok-tint:#f2fbf5;
    --bad:#d23f31; --bad-bg:#fdecec;
    --warn:#8a5a00; --warn-strong:#b26a00; --warn-line:#e0a400; --warn-bg:#fffbf0; --warn-chip:#ffe7b3;
    --neutral-tint:#f7f8fa; --accent-tint:#f1f6ff;
    --track:#e9ecf1; --btn-dark:#222834;
    --shadow:0 1px 2px rgba(16,24,40,.06), 0 8px 24px -12px rgba(16,24,40,.18);
    --radius:12px;
    color-scheme:light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#0e1116; --surface:#171b22; --surface-2:#1e232c;
      --ink:#e5e8ee; --strong:#f4f6fa; --muted:#9aa4b2; --faint:#6c7686;
      --line:#2a303b; --accent:#4b7bf5; --accent-ink:#ffffff; --ai:#a48bf0;
      --accent-weak:#1a2540; --accent-weak-2:#243458;
      --ok:#4cc282; --ok-bg:#12271b; --ok-tint:#13231a;
      --bad:#f0736a; --bad-bg:#2c1a1a;
      --warn:#e0ab6c; --warn-strong:#e6b06e; --warn-line:#b8862f; --warn-bg:#241f12; --warn-chip:#463714;
      --neutral-tint:#1b2029; --accent-tint:#172236;
      --track:#272d38; --btn-dark:#2b3341;
      --shadow:0 1px 2px rgba(0,0,0,.5), 0 10px 30px -12px rgba(0,0,0,.6);
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"] {
      --bg:#0e1116; --surface:#171b22; --surface-2:#1e232c;
      --ink:#e5e8ee; --strong:#f4f6fa; --muted:#9aa4b2; --faint:#6c7686;
      --line:#2a303b; --accent:#4b7bf5; --accent-ink:#ffffff; --ai:#a48bf0;
      --accent-weak:#1a2540; --accent-weak-2:#243458;
      --ok:#4cc282; --ok-bg:#12271b; --ok-tint:#13231a;
      --bad:#f0736a; --bad-bg:#2c1a1a;
      --warn:#e0ab6c; --warn-strong:#e6b06e; --warn-line:#b8862f; --warn-bg:#241f12; --warn-chip:#463714;
      --neutral-tint:#1b2029; --accent-tint:#172236;
      --track:#272d38; --btn-dark:#2b3341;
      --shadow:0 1px 2px rgba(0,0,0,.5), 0 10px 30px -12px rgba(0,0,0,.6);
      color-scheme:dark;
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; color:var(--ink); background:var(--bg); -webkit-font-smoothing:antialiased; }
  .app { display:grid; grid-template-columns:248px 1fr; min-height:100vh; }
  /* Left nav rail */
  aside.nav { background:var(--surface); border-right:1px solid var(--line); padding:16px 12px; position:sticky; top:0; height:100vh; display:flex; flex-direction:column; }
  .brand { display:flex; align-items:center; gap:8px; font-size:15px; font-weight:800; letter-spacing:-.01em; padding:6px 10px 16px; }
  .navlist { display:flex; flex-direction:column; gap:3px; }
  .tab { display:flex; align-items:center; gap:11px; width:100%; text-align:left; margin:0; padding:10px 12px; border:0; border-radius:9px; background:transparent; color:var(--ink); font-weight:600; font-size:14px; cursor:pointer; transition:background .12s, color .12s; }
  .tab:hover { background:var(--surface-2); filter:none; }
  .tab.active { background:var(--accent-weak); color:var(--accent); }
  .tab .ic { width:20px; text-align:center; font-size:15px; }
  .nav-foot { margin-top:auto; display:flex; flex-direction:column; gap:10px; padding-top:14px; }
  #theme-toggle { width:100%; margin:0; padding:9px; background:var(--surface-2); color:var(--muted); font-weight:600; font-size:13px; border:1px solid var(--line); border-radius:9px; cursor:pointer; }
  #theme-toggle:hover { color:var(--ink); border-color:var(--muted); filter:none; }
  label { display:block; font-size:12px; font-weight:600; color:var(--muted); margin:14px 0 4px; text-transform:uppercase; letter-spacing:.03em; }
  select, textarea, input { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:9px; font:inherit; background:var(--surface); color:var(--ink); }
  select:focus, textarea:focus, input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-weak); }
  textarea { min-height:140px; resize:vertical; }
  button { width:100%; margin-top:18px; padding:10px; border:0; border-radius:9px; background:var(--accent); color:var(--accent-ink); font-weight:600; cursor:pointer; transition:filter .12s; }
  button:hover { filter:brightness(1.06); }
  button:disabled { opacity:.5; cursor:wait; filter:none; }
  .account { border:1px solid var(--line); border-radius:10px; padding:10px 12px; font-size:12.5px; line-height:1.4; background:var(--surface-2); }
  .account .dot { display:inline-block; width:8px; height:8px; border-radius:99px; margin-right:6px; vertical-align:middle; }
  .account .on { background:var(--ok); } .account .off { background:var(--bad); }
  .account button { margin-top:8px; background:var(--btn-dark); }
  .account .hint { color:var(--muted); font-size:12px; margin-top:6px; line-height:1.4; }
  main { padding:28px 32px; overflow:auto; height:100vh; }
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
  .editor { max-width:640px; }
  .editor h3 { margin:22px 0 8px; font-size:15px; }
  .editing { font-size:13px; color:var(--muted); line-height:1.5; }
  .form { border:1px solid var(--line); border-radius:10px; padding:14px; background:var(--surface); }
  .form input, .form select, .form textarea { margin-bottom:8px; }
  .form textarea { min-height:80px; }
  .row2 { display:flex; gap:8px; }
  .msg { font-size:13px; margin-top:6px; min-height:1em; }
  .msg.ok { color:var(--ok); } .msg.err { color:var(--bad); } .msg.busy { color:var(--muted); }
  /* Track tab */
  .tcounts { display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 14px; }
  .tcounts .pill { font-size:12px; font-weight:600; padding:6px 12px; border-radius:99px; background:var(--surface); border:1px solid var(--line); cursor:pointer; }
  .tcounts .pill.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .tcounts .pill .n { font-variant-numeric:tabular-nums; }
  .funnel { display:flex; flex-direction:column; gap:5px; margin:0 0 16px; max-width:560px; }
  .funnel .fn-row { display:grid; grid-template-columns:78px 1fr 92px; align-items:center; gap:10px; }
  .funnel .fn-label { font-size:12px; font-weight:600; color:var(--ink); text-align:right; }
  .funnel .fn-track { background:var(--track); border-radius:5px; height:22px; overflow:hidden; }
  .funnel .fn-bar { height:100%; background:var(--accent); border-radius:5px; min-width:2px; transition:width .3s; }
  .funnel .fn-meta { font-size:11.5px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .funnel .fn-meta b { color:var(--strong); }
  .funnel .fn-conv { color:var(--ok); }
  .funnel-empty { font-size:12.5px; color:var(--faint); margin:0 0 14px; }
  .trackbar { display:flex; gap:8px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }
  .trackbar input { flex:1; min-width:200px; margin:0; }
  .trackbar select { width:auto; margin:0; }
  .tbtn { width:auto; margin:0; padding:8px 14px; }
  /* Track view uses the full screen width — no 640px editor cap. */
  .track-editor { max-width:none; }
  .track-editor .editing { max-width:820px; }
  .ttable { width:auto; table-layout:fixed; border-collapse:collapse; background:var(--surface); border:1px solid var(--line); border-radius:8px; font-size:13px; }
  .ttable th { position:relative; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); padding:8px 8px; border-bottom:1px solid var(--line); white-space:nowrap; overflow:hidden; }
  .ttable th .lbl { display:block; overflow:hidden; text-overflow:ellipsis; padding-right:6px; }
  /* Spreadsheet-style drag-to-resize handle on each column's right edge. */
  .ttable th .rz { position:absolute; top:0; right:0; width:7px; height:100%; cursor:col-resize; user-select:none; }
  .ttable th .rz:hover, .ttable th.rzing .rz { background:var(--accent); opacity:.4; }
  body.rz-drag { cursor:col-resize; user-select:none; }
  /* Show/hide-columns menu */
  .colmenu { position:relative; }
  .colmenu .menu { position:absolute; z-index:30; top:calc(100% + 4px); left:0; background:var(--surface); border:1px solid var(--line); border-radius:8px; box-shadow:0 6px 24px rgba(0,0,0,.12); padding:8px; min-width:190px; max-height:340px; overflow:auto; }
  .colmenu .menu label { display:flex; align-items:center; gap:8px; margin:2px 0; padding:2px; font-size:13px; font-weight:400; text-transform:none; letter-spacing:normal; color:var(--ink); cursor:pointer; }
  .colmenu .menu label:hover { background:var(--bg); border-radius:4px; }
  .colmenu .menu input { width:auto; }
  .colmenu .menu .rst { width:100%; margin:8px 0 0; padding:6px 10px; background:var(--accent-weak); color:var(--accent); font-size:12px; }
  .ttable td { padding:3px 4px; border-bottom:1px solid var(--line); vertical-align:middle; overflow:hidden; }
  .ttable tr:last-child td { border-bottom:0; }
  .ttable input, .ttable select { width:100%; border:1px solid transparent; background:transparent; padding:5px 6px; margin:0; border-radius:4px; text-overflow:ellipsis; }
  .ttable input:hover, .ttable select:hover { border-color:var(--line); }
  .ttable input:focus, .ttable select:focus { border-color:var(--accent); background:var(--surface); outline:none; }
  .ttable .st-dryrun { color:var(--warn); } .ttable .st-applied { color:var(--accent); }
  .ttable .st-responded { color:var(--ok); } .ttable .st-failed { color:var(--bad); }
  .ttable .st-discovered, .ttable .st-tailored { color:var(--muted); }
  .ttable .delrow { width:auto; margin:0; padding:4px 8px; background:var(--surface); color:var(--bad); border:1px solid var(--line); font-size:12px; }
  .ttable .rerun { width:auto; margin:0; padding:4px 8px; background:var(--surface); color:var(--accent); border:1px solid var(--line); font-size:12px; white-space:nowrap; }
  .ttable .rerun:disabled { opacity:.6; cursor:default; }
  .ttable .rowsaved { color:var(--ok); font-size:12px; }
  .ttable .reslink { color:var(--accent); text-decoration:none; font-size:12px; white-space:nowrap; padding:5px 6px; display:inline-block; }
  .ttable .reslink:hover { text-decoration:underline; }
  .ttable .muted { color:var(--muted); padding:5px 6px; display:inline-block; }
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
  .card { position:relative; border:1px solid var(--line); border-radius:8px; padding:12px 12px 10px; background:var(--surface); }
  .card .del { position:absolute; top:8px; right:8px; width:auto; margin:0; padding:1px 8px; background:var(--surface-2); color:var(--bad); font-size:13px; }
  .row2 { display:flex; gap:8px; }
  .row2 > * { flex:1; }
  .fld { margin-bottom:8px; }
  .fld label { margin:0 0 3px; text-transform:none; font-size:11px; }
  .addbtn { width:auto; margin:8px 0 0; padding:6px 12px; background:var(--accent-weak); color:var(--accent); }
  .saverow { position:sticky; bottom:0; background:var(--bg); padding:12px 0 4px; display:flex; align-items:center; gap:12px; }
  .saverow button { width:auto; margin:0; }
  /* profile section-jump nav */
  .pnav { position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap; gap:6px; background:var(--bg); padding:10px 0; margin-bottom:4px; border-bottom:1px solid var(--line); }
  .pnav a { font-size:12px; font-weight:600; color:var(--accent); background:var(--accent-weak); padding:5px 10px; border-radius:99px; text-decoration:none; }
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
  #view-profile .editor { max-width:1040px; }
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
  .badge { display:inline-block; padding:2px 8px; border-radius:99px; background:var(--accent-weak); color:var(--accent); font-size:12px; font-weight:600; }
  #dl-pdf { width:auto; margin:0 0 16px; padding:8px 14px; background:#111; }
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
  .fit-legend .sw.best { color:var(--accent); }
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
  .tstep.act { color:var(--accent); font-weight:600; }
  .tstep.done { color:var(--ok); }
  .tmsg { margin-top:8px; font-size:14px; color:var(--ink); }
  .tmeta { margin-top:6px; font-size:12px; color:var(--muted); }
  .tmeta.cache { color:var(--ink); }
  .linklike { background:none; border:none; padding:0; margin-left:8px; color:var(--accent);
              font:inherit; text-decoration:underline; cursor:pointer; }
  .linklike:disabled { color:var(--muted); text-decoration:none; cursor:default; }
  .tbar { margin-top:8px; height:7px; background:var(--line); border-radius:4px; overflow:hidden; }
  .tbarfill { height:100%; background:var(--accent); transition:width .3s; }
  .testchosen { margin-top:14px; padding:14px; border:1px solid var(--accent); border-radius:8px; background:var(--accent-tint); }
  .tclabel { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .tctitle { font-size:15px; font-weight:600; margin-top:4px; }
  .tcmeta { font-size:12px; color:var(--muted); margin-top:4px; word-break:break-all; }
  .tcwhy { font-size:13px; margin-top:6px; line-height:1.5; }
  .fitpill { font-size:12px; font-weight:600; color:#fff; background:var(--accent); border-radius:10px; padding:1px 8px; margin-left:6px; }
  .tfinish { margin-top:14px; padding-top:12px; border-top:1px solid var(--line); font-size:13px; }
  .tfinish button { width:auto; margin-top:8px; }
  .testjudged { margin-top:14px; }
  .tjhead { font-size:13px; color:var(--muted); margin-bottom:8px; line-height:1.5; }
  .tjrow { border:1px solid var(--line); border-left-width:4px; border-radius:6px; padding:9px 11px; margin-bottom:7px; }
  .tjrow.ok { border-left-color:var(--ok); background:var(--ok-tint); }
  .tjrow.no { border-left-color:var(--line); background:var(--neutral-tint); }
  .tjtop { display:flex; align-items:baseline; gap:8px; }
  .tjscore { font-weight:700; font-size:13px; min-width:34px; }
  .tjrow.ok .tjscore { color:var(--ok); } .tjrow.no .tjscore { color:var(--warn-strong); }
  .tjname { font-weight:600; font-size:14px; }
  .tjmeta { font-size:12px; color:var(--muted); margin-top:3px; word-break:break-all; }
  .tjwhy { font-size:12.5px; margin-top:4px; line-height:1.45; }
  .tjmiss { font-size:12px; color:var(--warn); margin-top:3px; }
  #parked-panel { border-left:4px solid var(--warn-line); }
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
</style>
</head>
<body>
<div class="app">
  <aside class="nav">
    <div class="brand">📄 ApplicationBot</div>
    <nav class="navlist">
      <button class="tab active" data-view="review"><span class="ic">📝</span>Review</button>
      <button class="tab" data-view="discover"><span class="ic">🔍</span>Discover</button>
      <button class="tab" data-view="profile"><span class="ic">👤</span>Profile</button>
      <button class="tab" data-view="track"><span class="ic">📊</span>Track</button>
    </nav>
    <div class="nav-foot">
      <div id="account" class="account">Checking Claude sign-in…</div>
      <button id="theme-toggle" type="button" aria-label="Toggle dark mode">🌙 Dark</button>
    </div>
  </aside>

  <main>
    <div id="view-discover" class="hidden">
      <div class="editor" id="parked-panel" style="display:none">
        <h3 style="margin-top:0">Applications waiting on you</h3>
        <p class="editing">These were filled but couldn't finish on their own — each needs one
          thing from you before it can go through. Click to go straight to the fix.</p>
        <div id="parked-body"></div>
      </div>
      <div class="editor" id="sources-overview">
        <h3 style="margin-top:0">Where your postings come from</h3>
        <p class="editing">The live view of every source feeding discovery — target boards by
          ATS, the optional aggregator, early-career feeds, and the aggregator→ATS bridge.
          Configure them in <b>Discovery settings</b> below.</p>
        <div id="sources-body">Loading…</div>
      </div>
      <div class="editor" id="fit-insights" style="display:none">
        <h3 style="margin-top:0">What past runs taught the search</h3>
        <p class="editing">Every posting Claude judges is remembered. The search now steers each
          run's scarce judge slots toward the kinds of postings that scored highest for you before,
          so new runs should surface more matches above your bar. Below is what it has learned and
          what it recommends changing.</p>
        <div id="fit-insights-body">Loading…</div>
      </div>
      <div class="editor">
        <h3 style="margin-top:0">Discovery settings</h3>
        <p class="editing">Control what the bot searches and how it filters matches — every
          setting is editable here, no code or config files to touch. Saved to
          <code>profile/discovery.yaml</code> (git-ignored).</p>
        <div id="disc-form">Loading…</div>
        <div class="saverow">
          <button id="save-disc">Save settings</button>
          <span id="disc-msg" class="msg"></span>
        </div>

        <h3>Run a dry-run</h3>
        <p class="editing">Run one full end-to-end <b>dry-run</b> with the settings above: the
          bot searches your target boards, ranks every posting by how well it fits <i>your</i>
          qualifications, then <b>follows through on exactly one</b> — the single best match —
          tailoring your résumé and auto-filling that application in a browser you can watch. It
          <b>never submits</b> (Agent Guideline #3); when it finishes filling, review it in the
          browser and click <b>Finish</b>. A <code>dry-run</code> row is recorded in Track.</p>
        <div class="saverow">
          <button id="test-run" type="button">▶ Find &amp; fill one application (dry-run)</button>
          <span id="test-msg" class="msg"></span>
        </div>
        <div id="test-progress" class="testprog hidden"></div>
        <div id="test-chosen" class="testchosen hidden"></div>
        <div id="test-judged" class="testjudged hidden"></div>
      </div>
    </div>

    <div id="view-review">
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
            <option value="auto">auto (Claude subscription if available, else rules)</option>
            <option value="claude-code">claude-code (your subscription)</option>
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
        <p class="editing">Everything about you, in one place. Edit any section granularly —
          click an entry to expand it. Applicant details save to
          <code>profile/application_profile.yaml</code>; experience, projects, education, and
          skills save to your résumé <b id="editing-path"></b>. Both are git-ignored. Tailoring
          picks the relevant parts per job.</p>

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
        <p class="editing">Every application the pipeline discovered, tailored, and (in
          <code>dry_run</code>) would have submitted — the local system of record
          (<code>applications.db</code>, git-ignored). Edit any cell inline; changes save
          as you go. <b>Drag a column's right edge to resize it</b>, and use <b>Columns</b>
          to hide any you don't need — your layout is remembered on this browser.</p>
        <div id="track-counts" class="tcounts"></div>
        <div id="track-funnel" class="funnel"></div>
        <div class="trackbar">
          <input id="track-search" type="text" placeholder="Search company, role, location, notes…">
          <div class="colmenu">
            <button id="track-cols-btn" type="button" class="tbtn">Columns ▾</button>
            <div id="track-cols-menu" class="menu hidden"></div>
          </div>
          <button id="track-add" type="button" class="tbtn">+ Add application</button>
          <span id="track-msg" class="msg"></span>
        </div>
        <div id="track-body">Loading…</div>
      </div>
    </div>
  </main>
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

// ---- Claude access panel (subscription via Claude Code) ----
function renderAccount(a) {
  const el = $("account");
  if (a.available) {
    el.innerHTML = `<span class="dot on"></span><b>Claude ready</b> — using your Claude subscription via Claude Code (not the paid API). The <b>claude-code</b> engine rewrites bullets to match each job.`;
  } else {
    el.innerHTML = `<span class="dot off"></span><b>Claude Code not found.</b> The <b>rules</b> engine still works with no account.<div class="hint">${escapeHtml(a.hint || "")}</div>`;
  }
}
renderAccount(OPTS.auth);

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
  if (v === "discover") { loadParked(); loadSources(); loadFitInsights(); loadDisc(); pollTest(); }
}));
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
async function reapplyParked(id, btn, arm, who) {
  const label = btn ? btn.textContent : "";
  if (arm) {
    const ok = confirm("Really SUBMIT this application" + (who ? " to " + who : "") + "?\\n\\n"
      + "This is a real, irreversible submission. Make sure the block is resolved. "
      + "It fills the form and clicks Submit; the pre-submit check still stops it if a required "
      + "field is unanswered.");
    if (!ok) return;
  }
  const msg = $("test-msg");
  if (btn) { btn.disabled = true; btn.textContent = arm ? "Submitting…" : "Starting…"; }
  if (msg) { msg.className = "msg"; msg.textContent = ""; }
  try {
    const r = await (await fetch("/parked/reapply", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id, arm: !!arm })})).json();
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
  ["source_url","Source URL",220], ["date_discovered","Discovered",120], ["date_applied","Applied",120],
  ["follow_up_date","Follow up",110], ["resume_path","Résumé used",160], ["notes","Notes",240],
];
let TRACK_STATE = { status:null, search:"", statuses:[] };

// Spreadsheet column layout (width + visibility), remembered per browser.
const TRACK_LS_W = "ab_track_colw", TRACK_LS_H = "ab_track_hidden";
let TRACK_COLW = {}, TRACK_HIDDEN = new Set(), TRACK_APPS = [];
try { TRACK_COLW = JSON.parse(localStorage.getItem(TRACK_LS_W) || "{}") || {}; } catch (e) {}
try { TRACK_HIDDEN = new Set(JSON.parse(localStorage.getItem(TRACK_LS_H) || "[]")); } catch (e) {}
const saveColW = () => { try { localStorage.setItem(TRACK_LS_W, JSON.stringify(TRACK_COLW)); } catch (e) {} };
const saveHidden = () => { try { localStorage.setItem(TRACK_LS_H, JSON.stringify([...TRACK_HIDDEN])); } catch (e) {} };
const colWidth = (key, def) => TRACK_COLW[key] || def;
const visibleCols = () => TRACK_COLS.filter(([k]) => !TRACK_HIDDEN.has(k));

async function loadTrack() {
  const body = $("track-body");
  busyInto(body, "Loading applications…", false);
  try {
    const q = new URLSearchParams();
    if (TRACK_STATE.status) q.set("status", TRACK_STATE.status);
    if (TRACK_STATE.search) q.set("search", TRACK_STATE.search);
    const d = await (await fetch("/track?" + q.toString())).json();
    TRACK_STATE.statuses = d.statuses;
    renderCounts(d.counts);
    renderFunnel(d.funnel);
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

// The discovery→offer funnel (survey #4): one bar per stage, width relative to Discovered,
// with the count and the conversion from the previous stage.
function renderFunnel(funnel) {
  const box = $("track-funnel"); if (!box) return;
  box.innerHTML = "";
  const top = (funnel && funnel[0] && funnel[0].count) || 0;
  if (!top) {
    box.className = "funnel-empty";
    box.textContent = "The funnel fills in as the pipeline discovers, fills, and (once armed) submits applications.";
    return;
  }
  box.className = "funnel";
  for (const s of funnel) {
    const pct = Math.round(100 * s.count / top);
    const conv = (s.conversion_from_prev != null)
      ? el("span", {class:"fn-conv", text:" · " + Math.round(100 * s.conversion_from_prev) + "%"})
      : null;
    const meta = el("span", {class:"fn-meta"}, [el("b", {text:String(s.count)}), " " + pct + "%"]);
    if (conv) meta.append(conv);
    box.append(el("div", {class:"fn-row"}, [
      el("span", {class:"fn-label", text:s.stage}),
      el("div", {class:"fn-track"}, [el("div", {class:"fn-bar", style:"width:" + pct + "%"})]),
      meta,
    ]));
  }
}

function statusCell(app) {
  const sel = el("select", {class:"st-" + app.status.replace("-","")});
  for (const s of TRACK_STATE.statuses) {
    const o = el("option", {value:s, text:s}); if (s===app.status) o.selected = true; sel.appendChild(o);
  }
  sel.addEventListener("change", () => { sel.className = "st-" + sel.value.replace("-",""); saveCell(app.id, "status", sel.value); });
  return sel;
}

// Re-run a previous dry-run from the tracker: re-drive the same deterministic fill on the same
// posting URL with the stored tailored PDF (never submits — reuses reapplyParked with arm=false).
// Switch to the Discover tab first so the fill reports into the one shared run-progress panel +
// Finish button, instead of a second progress UI.
function rerunDry(app, btn) {
  const tab = document.querySelector('.tab[data-view="discover"]');
  if (tab) tab.click();
  reapplyParked(app.id, btn, false);
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
    const th = el("th", {}, [el("span", {class:"lbl", text:label}), rz]);
    rz.addEventListener("mousedown", (ev) => startResize(ev, key, colEls[key], def, th));
    return th;
  });
  const head = el("tr", {}, ths.concat([el("th", {text:""})]));
  const rows = apps.map(app => {
    const tds = vis.map(([key]) => {
      let input;
      if (key === "status") input = statusCell(app);
      else if (key === "date_discovered" || key === "date_applied")
        input = el("input", {type:"date", value:app[key] || "", on:{change:e=>saveCell(app.id, key, e.target.value)}});
      else if (key === "resume_path")
        input = app.resume_path
          ? el("a", {class:"reslink", href:"/track/resume?id=" + app.id, target:"_blank",
                     title:app.resume_path, text:"View résumé ↗"})
          : el("span", {class:"muted", text:"—"});
      else input = el("input", {type:"text", value:app[key] || "", on:{change:e=>saveCell(app.id, key, e.target.value)}});
      return el("td", {}, [input]);
    });
    const saved = el("span", {class:"rowsaved"});
    const del = el("button", {class:"delrow", type:"button", text:"Delete",
      on:{click:()=>delApp(app.id)}});
    // Re-run: only for rows that were dry-runs and still have a posting URL to re-fill.
    const acts = [];
    if (app.status === "dry-run" && app.source_url)
      acts.push(el("button", {class:"rerun", type:"button", text:"Re-run ▶",
        title:"Re-fill this posting in a browser to check it — never submits",
        on:{click:(ev)=>rerunDry(app, ev.target)}}));
    acts.push(del, saved);
    tds.push(el("td", {}, [el("div", {style:"display:flex;gap:6px;align-items:center"}, acts)]));
    const tr = el("tr", {}, tds); tr._saved = saved; tr.dataset.id = app.id;
    return tr;
  });
  const table = el("table", {class:"ttable"},
    [el("colgroup", {}, cols), el("thead", {}, [head]), el("tbody", {}, rows)]);
  body.append(el("div", {class:"twrap"}, [table]));
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
    TRACK_HIDDEN.clear(); TRACK_COLW = {}; saveHidden(); saveColW(); renderColMenu(); renderTrack(TRACK_APPS);
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
  } catch (e) {
    if (saved) { saved.className = "msg err"; saved.textContent = String(e.message || e); }
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
  if ((qa.maps_to||"").trim()) return {mark:"↔", label:"Auto-answered from your profile ("+qa.maps_to.trim()+")", color:"var(--ok)"};
  if ((qa.answer||"").trim()) return qa.generated
    ? {mark:"✨", label:"AI-drafted — review & edit", color:"var(--ai)"}
    : {mark:"✓", label:"Answered", color:"var(--ok)"};
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
    el("span", {style:"font-weight:700;font-size:15px;color:"+(ok?"var(--ok)":"var(--muted)"), text: ok ? "✓" : "○"}),
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
      st.style.color = "var(--ok)";
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
    msg.style.color = r.ok ? "var(--ok)" : "var(--bad)";
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
    msg.style.color = r.ok ? "var(--ok)" : "var(--bad)";
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
    el("div", {class:"editing", text:"Discover from community-curated SimplifyJobs lists of new-grad and internship roles — early-career by construction, no company list needed. Best when your target boards are senior-heavy. Only roles on ATSs we can fill (Greenhouse/Lever/Ashby) are used."}),
    chkRow("Enable early-career feeds", "ec_enabled", ec.enabled),
    el("label", {text:"Include"}),
    el("div", {class:"lvls"}, [kindBox("new-grad","new-grad"), kindBox("intern","internships")]),
    numFld("How many top-matching listings to pull full descriptions for (per run)", "ec_max_resolve", ec.max_resolve==null?40:ec.max_resolve),
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
  function apply(mode){
    if (mode === "dark" || mode === "light") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
    const dark = mode === "dark" || (mode !== "light" && sysDark());
    if (btn) btn.textContent = dark ? "☀️ Light" : "🌙 Dark";
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
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local web UI for reviewing tailored resumes.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
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

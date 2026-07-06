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

from . import apply_profile, auth, catalogue, filters, linkedin, tracker
from .job_description import JobDescription, load_job_description
from .backends import DEFAULT_QUALITY
from .length import LengthBudget
from .models import TailoredResume
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
        "chosen": None, "report": None, "errors": [],
    }


def _set(**kw) -> None:
    with _TEST_LOCK:
        _TEST_STATE.update(kw)


def _test_worker() -> None:
    """Run the full testing-mode pipeline in the background, updating _TEST_STATE."""
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
        _set(step="discover", message="Discovering postings from your target boards…")

        def on_judge(done, total):
            _set(step="match", judged=done, judged_total=total,
                 message=f"Judging fit with Claude — {done}/{total} postings…")

        res = pipeline.discover_and_match(resume, filters, profile=profile,
                                          use_claude=use_claude, on_progress=on_judge)
        # Surface every Claude-judged posting — accepted AND denied — so the user can see what
        # the searches return and why each is rejected (ranked best-first).
        judged = [{
            "company": m.posting.company, "title": m.posting.title,
            "location": m.posting.location, "compensation": m.posting.compensation,
            "url": m.posting.url, "ats": m.posting.ats,
            "fit_score": m.fit_score, "qualified": m.qualified,
            "why": m.why, "missing": (m.missing or [])[:3],
            "cleared": (m.fit_score is not None and m.fit_score >= filters.min_fit),
        } for m in res.matches if m.fit_score is not None]
        _set(scanned=res.discovered, matched=len(res.matches), errors=res.errors,
             skipped_seen=res.skipped_seen, judged=judged, min_fit=filters.min_fit)
        if not res.matches:
            extra = ["No new postings matched your qualifications."]
            if res.skipped_seen:
                extra.append(f"({res.skipped_seen} already in your tracker were skipped.)")
            _set(phase="error", errors=(res.errors or []) + extra)
            return

        top = pipeline.pick_top(res.matches, min_fit=filters.min_fit)
        if top is None:
            best = max((m.fit_score for m in res.matches if m.fit_score is not None), default=None)
            best_txt = f" Best fit this run was {best}/100." if best is not None else ""
            _set(phase="error", errors=[
                f"No match reached your minimum fit of {filters.min_fit}/100, so nothing was "
                f"applied to.{best_txt} See the judged postings below for why. To find a match: "
                "lower “Minimum fit score”, raise “How many top matches Claude judges”, set "
                "“Experience levels” to your level (so senior roles are filtered out before "
                "judging), or add boards that better fit your résumé — all in Discovery settings."])
            return
        p = top.posting
        chosen = {
            "company": p.company, "title": p.title, "location": p.location,
            "compensation": p.compensation, "url": p.url, "ats": p.ats,
            "fit_score": top.fit_score, "qualified": top.qualified,
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


def start_test_run() -> dict:
    with _TEST_LOCK:
        if _TEST_STATE.get("phase") == "running":
            return {"ok": False, "error": "A test run is already in progress."}
        _TEST_STATE.clear()
        _TEST_STATE.update(_test_reset())
    threading.Thread(target=_test_worker, daemon=True).start()
    return {"ok": True}


def list_resumes() -> list[dict[str, str]]:
    # The apply profile and discovery filters live alongside résumés in profile/ but are not
    # résumés — exclude them so they never show up as a selectable resume (they fail to load
    # as a Resume, which broke the Profile page). Keep this in sync with the config modules.
    from . import filters

    exclude = {
        Path(apply_profile.DEFAULT_PATH).name,  # application_profile.yaml
        Path(filters.DEFAULT_PATH).name,        # discovery.yaml
        "discovery.example.yaml",               # the committed example template (examples/)
    }
    out = []
    for folder in ("profile", "examples"):
        for p in sorted((REPO_ROOT / folder).glob("*.yaml")):
            if p.name in exclude:
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
    return {
        "backend": result.backend,
        "pages": result.pages,
        "title": jd.title,
        "company": jd.company,
        "html": render_html(resume, result.tailored),
        "markdown": render_markdown(resume, result.tailored),
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
            self._json(200, {"profile": apply_profile.load_profile().model_dump()})
            return
        if path == "/discovery":
            self._json(200, {
                "filters": filters.load_filters().model_dump(),
                "levels": filters.EXPERIENCE_LEVELS,
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
                "statuses": tracker.STATUSES,
                "fields": tracker.EDITABLE,
            })
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
            elif path == "/resume/import-linkedin":
                p = json.loads(raw or b"{}")
                rp = _allowlisted(p["resume"], list_resumes())
                data = base64.b64decode(p["data_b64"])
                result = linkedin.import_into(rp, p.get("filename", "upload"), data)
                self._json(200, {"ok": True, **result})
            elif path == "/profile/update":
                p = json.loads(raw or b"{}")
                apply_profile.replace_profile(p["data"])
                self._json(200, {"ok": True})
            elif path == "/discovery/update":
                p = json.loads(raw or b"{}")
                filters.save_filters(filters.DiscoveryFilters.model_validate(p["data"]))
                self._json(200, {"ok": True})
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
                tailored = TailoredResume.model_validate(p["tailored"])
                self._send(200, render_pdf(base, tailored), "application/pdf",
                           {"Content-Disposition": 'attachment; filename="tailored_resume.pdf"'})
            elif path == "/test-run":
                self._json(200, start_test_run())
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
  :root { --ink:#1c1c1c; --muted:#666; --line:#e2e2e2; --accent:#2a5bd7; --bg:#f5f5f4; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif; color:var(--ink); background:var(--bg); }
  .app { display:grid; grid-template-columns:320px 1fr; min-height:100vh; }
  aside { background:#fff; border-right:1px solid var(--line); padding:20px; position:sticky; top:0; height:100vh; overflow:auto; }
  aside h1 { font-size:16px; margin:0 0 16px; }
  label { display:block; font-size:12px; font-weight:600; color:var(--muted); margin:14px 0 4px; text-transform:uppercase; letter-spacing:.03em; }
  select, textarea, input { width:100%; padding:8px; border:1px solid var(--line); border-radius:6px; font:inherit; }
  textarea { min-height:140px; resize:vertical; }
  button { width:100%; margin-top:18px; padding:10px; border:0; border-radius:6px; background:var(--accent); color:#fff; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:wait; }
  .account { border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin-bottom:6px; font-size:13px; }
  .account .dot { display:inline-block; width:8px; height:8px; border-radius:99px; margin-right:6px; vertical-align:middle; }
  .account .on { background:#2a9d5b; } .account .off { background:#c0392b; }
  .account button { margin-top:8px; background:#111; }
  .account .hint { color:var(--muted); font-size:12px; margin-top:6px; line-height:1.4; }
  main { padding:28px; overflow:auto; }
  .tabs { display:flex; gap:8px; margin-bottom:18px; }
  .tab { width:auto; margin:0; padding:8px 16px; background:#eef1fb; color:var(--accent); }
  .tab.active { background:var(--accent); color:#fff; }
  .editor { max-width:640px; }
  .editor h3 { margin:22px 0 8px; font-size:15px; }
  .editing { font-size:13px; color:var(--muted); line-height:1.5; }
  .form { border:1px solid var(--line); border-radius:8px; padding:14px; background:#fff; }
  .form input, .form select, .form textarea { margin-bottom:8px; }
  .form textarea { min-height:80px; }
  .row2 { display:flex; gap:8px; }
  .msg { font-size:13px; margin-top:6px; min-height:1em; }
  .msg.ok { color:#2a9d5b; } .msg.err { color:#c0392b; } .msg.busy { color:var(--muted); }
  /* Track tab */
  .tcounts { display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 14px; }
  .tcounts .pill { font-size:12px; font-weight:600; padding:6px 12px; border-radius:99px; background:#fff; border:1px solid var(--line); cursor:pointer; }
  .tcounts .pill.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .tcounts .pill .n { font-variant-numeric:tabular-nums; }
  .trackbar { display:flex; gap:8px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }
  .trackbar input { flex:1; min-width:200px; margin:0; }
  .trackbar select { width:auto; margin:0; }
  .tbtn { width:auto; margin:0; padding:8px 14px; }
  /* Track view uses the full screen width — no 640px editor cap. */
  .track-editor { max-width:none; }
  .track-editor .editing { max-width:820px; }
  .ttable { width:auto; table-layout:fixed; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; font-size:13px; }
  .ttable th { position:relative; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); padding:8px 8px; border-bottom:1px solid var(--line); white-space:nowrap; overflow:hidden; }
  .ttable th .lbl { display:block; overflow:hidden; text-overflow:ellipsis; padding-right:6px; }
  /* Spreadsheet-style drag-to-resize handle on each column's right edge. */
  .ttable th .rz { position:absolute; top:0; right:0; width:7px; height:100%; cursor:col-resize; user-select:none; }
  .ttable th .rz:hover, .ttable th.rzing .rz { background:var(--accent); opacity:.4; }
  body.rz-drag { cursor:col-resize; user-select:none; }
  /* Show/hide-columns menu */
  .colmenu { position:relative; }
  .colmenu .menu { position:absolute; z-index:30; top:calc(100% + 4px); left:0; background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:0 6px 24px rgba(0,0,0,.12); padding:8px; min-width:190px; max-height:340px; overflow:auto; }
  .colmenu .menu label { display:flex; align-items:center; gap:8px; margin:2px 0; padding:2px; font-size:13px; font-weight:400; text-transform:none; letter-spacing:normal; color:var(--ink); cursor:pointer; }
  .colmenu .menu label:hover { background:var(--bg); border-radius:4px; }
  .colmenu .menu input { width:auto; }
  .colmenu .menu .rst { width:100%; margin:8px 0 0; padding:6px 10px; background:#eef1fb; color:var(--accent); font-size:12px; }
  .ttable td { padding:3px 4px; border-bottom:1px solid var(--line); vertical-align:middle; overflow:hidden; }
  .ttable tr:last-child td { border-bottom:0; }
  .ttable input, .ttable select { width:100%; border:1px solid transparent; background:transparent; padding:5px 6px; margin:0; border-radius:4px; text-overflow:ellipsis; }
  .ttable input:hover, .ttable select:hover { border-color:var(--line); }
  .ttable input:focus, .ttable select:focus { border-color:var(--accent); background:#fff; outline:none; }
  .ttable .st-dryrun { color:#8a6d00; } .ttable .st-applied { color:#2a5bd7; }
  .ttable .st-responded { color:#2a9d5b; } .ttable .st-failed { color:#c0392b; }
  .ttable .st-discovered, .ttable .st-tailored { color:var(--muted); }
  .ttable .delrow { width:auto; margin:0; padding:4px 8px; background:#fff; color:#c0392b; border:1px solid var(--line); font-size:12px; }
  .ttable .rowsaved { color:#2a9d5b; font-size:12px; }
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
  .brd-row .del { width:auto; margin:0; padding:4px 10px; background:#f4f4f4; color:#a11; }
  .cards { display:flex; flex-direction:column; gap:10px; }
  .card { position:relative; border:1px solid var(--line); border-radius:8px; padding:12px 12px 10px; background:#fff; }
  .card .del { position:absolute; top:8px; right:8px; width:auto; margin:0; padding:1px 8px; background:#f4f4f4; color:#a11; font-size:13px; }
  .row2 { display:flex; gap:8px; }
  .row2 > * { flex:1; }
  .fld { margin-bottom:8px; }
  .fld label { margin:0 0 3px; text-transform:none; font-size:11px; }
  .addbtn { width:auto; margin:8px 0 0; padding:6px 12px; background:#eef1fb; color:var(--accent); }
  .saverow { position:sticky; bottom:0; background:var(--bg); padding:12px 0 4px; display:flex; align-items:center; gap:12px; }
  .saverow button { width:auto; margin:0; }
  /* profile section-jump nav */
  .pnav { position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap; gap:6px; background:var(--bg); padding:2px 0 12px; margin-bottom:4px; }
  .pnav a { font-size:12px; font-weight:600; color:var(--accent); background:#eef1fb; padding:5px 10px; border-radius:99px; text-decoration:none; }
  .pnav a:hover { background:#dfe6fb; }
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
  .linkedin { border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:18px; background:#fff; }
  .linkedin input[type=file] { width:auto; border:0; padding:0; }
  .linkedin button { width:auto; margin:8px 8px 0 0; padding:7px 14px; }
  .linkedin code { background:#f1f1f1; padding:1px 4px; border-radius:3px; font-size:12px; }
  .meta { margin-bottom:16px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:99px; background:#eef1fb; color:var(--accent); font-size:12px; font-weight:600; }
  #dl-pdf { width:auto; margin:0 0 16px; padding:8px 14px; background:#111; }
  .notes, .warn { font-size:13px; border-radius:8px; padding:10px 12px; margin:10px 0; }
  .notes { background:#eef7ee; }
  .warn { background:#fdeaea; color:#a11; }
  .hidden { display:none; }
  /* resume card */
  .resume { background:#fff; max-width:820px; margin:0 auto; padding:34px 42px; border:1px solid var(--line); border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,.05); }
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
  /* Review pane: résumé + "why was this tailored this way" side panel */
  .reviewwrap { display:flex; gap:20px; align-items:flex-start; }
  .reviewwrap > #result { flex:1; min-width:0; }
  .why-panel { width:300px; flex:none; position:sticky; top:16px; background:#fff; border:1px solid var(--line);
               border-radius:8px; padding:14px 16px; font-size:13px; box-shadow:0 1px 3px rgba(0,0,0,.05); }
  .why-panel.hidden { display:none; }
  .why-panel h3 { margin:0 0 8px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  .why-panel .wtitle { font-weight:700; font-size:14px; margin-bottom:6px; }
  .why-panel .wbody { line-height:1.5; }
  .why-panel .whint { color:var(--muted); line-height:1.5; }
  .resume .entry[data-why] { cursor:pointer; border-radius:6px; box-shadow:inset 3px 0 0 transparent; transition:background .1s, box-shadow .1s; }
  .resume .entry[data-why]:hover { background:#f3f6ff; box-shadow:inset 3px 0 0 var(--accent); }
  .resume .entry.why-active { background:#eef1fb; box-shadow:inset 3px 0 0 var(--accent); }
  @media (max-width: 900px) { .reviewwrap { flex-direction:column; } .why-panel { width:100%; position:static; } }
  /* Discover / test run */
  .testprog { margin-top:14px; padding:14px; border:1px solid var(--line); border-radius:8px; background:#fff; }
  .tstep { font-size:13px; color:var(--muted); padding:2px 0; }
  .tstep.act { color:var(--accent); font-weight:600; }
  .tstep.done { color:#2a9d5b; }
  .tmsg { margin-top:8px; font-size:14px; color:var(--ink); }
  .tmeta { margin-top:6px; font-size:12px; color:var(--muted); }
  .tbar { margin-top:8px; height:7px; background:var(--line); border-radius:4px; overflow:hidden; }
  .tbarfill { height:100%; background:var(--accent); transition:width .3s; }
  .testchosen { margin-top:14px; padding:14px; border:1px solid var(--accent); border-radius:8px; background:#f7f9ff; }
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
  .tjrow.ok { border-left-color:#2a9d5b; background:#f4fbf6; }
  .tjrow.no { border-left-color:#c0c4cc; background:#fafafa; }
  .tjtop { display:flex; align-items:baseline; gap:8px; }
  .tjscore { font-weight:700; font-size:13px; min-width:34px; }
  .tjrow.ok .tjscore { color:#2a9d5b; } .tjrow.no .tjscore { color:#b26a00; }
  .tjname { font-weight:600; font-size:14px; }
  .tjmeta { font-size:12px; color:var(--muted); margin-top:3px; word-break:break-all; }
  .tjwhy { font-size:12.5px; margin-top:4px; line-height:1.45; }
  .tjmiss { font-size:12px; color:#8a5a00; margin-top:3px; }
</style>
</head>
<body>
<div class="app">
  <aside>
    <h1>📄 ApplicationBot — Resume Review</h1>

    <div id="account" class="account">Checking Claude sign-in…</div>

    <label for="resume">Resume</label>
    <select id="resume"></select>

    <label for="jobmode">Job posting</label>
    <select id="jobmode">
      <option value="fixture">From a saved fixture</option>
      <option value="custom">Paste a posting</option>
    </select>

    <div id="fixtureBox">
      <select id="fixture"></select>
    </div>
    <div id="customBox" class="hidden">
      <input id="title" placeholder="Job title (optional)">
      <input id="company" placeholder="Company (optional)" style="margin-top:6px">
      <textarea id="body" placeholder="Paste the job description here…" style="margin-top:6px"></textarea>
    </div>

    <label for="backend">Engine</label>
    <select id="backend">
      <option value="auto">auto (Claude subscription if available, else rules)</option>
      <option value="claude-code">claude-code (your subscription)</option>
      <option value="rules">rules (no account)</option>
    </select>

    <label for="quality">Quality (Claude engine)</label>
    <select id="quality">
      <option value="fast">Fast — Sonnet, ~30s</option>
      <option value="balanced" selected>Balanced — Opus, ~40s (recommended)</option>
      <option value="max">Max quality — Opus + deep reasoning, ~2 min</option>
    </select>

    <label for="pages">Length</label>
    <select id="pages">
      <option value="1">1 page</option>
      <option value="1.5">1.5 pages</option>
      <option value="2">2 pages</option>
    </select>

    <label for="linechars">Line length (characters)</label>
    <input id="linechars" type="number" value="100" min="40" max="220">

    <button id="go">Tailor résumé</button>
  </aside>

  <main>
    <div class="tabs">
      <button class="tab active" data-view="review">Review</button>
      <button class="tab" data-view="discover">Discover</button>
      <button class="tab" data-view="profile">Profile</button>
      <button class="tab" data-view="track">Track</button>
    </div>

    <div id="view-discover" class="hidden">
      <div class="editor" id="sources-overview">
        <h3 style="margin-top:0">Where your postings come from</h3>
        <p class="editing">The live view of every source feeding discovery — target boards by
          ATS, the optional aggregator, early-career feeds, and the aggregator→ATS bridge.
          Configure them in <b>Discovery settings</b> below.</p>
        <div id="sources-body">Loading…</div>
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
  return entryCard([
    row2(fld("Project name","name",p.name), fld("Tech — e.g. Python, SQL","tech",p.tech)),
    area("Bullets (one per line)","bullets",(p.bullets||[]).join("\\n")),
  ], c => cardData(c).name);
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
    projects: cardsIn("sec-projects").map(c => { const d = cardData(c); return { name:(d.name||"").trim(), tech:orNull(d.tech), bullets:linesOf(d.bullets) }; }).filter(p => p.name),
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
  if (v === "discover") { loadSources(); loadDisc(); pollTest(); }
}));
$("resume").addEventListener("change", () => { if (!$("view-profile").classList.contains("hidden")) loadProfile(); });

// ---- Discover: run one full dry-run test ------------------------------------
let TEST_TIMER = null, TEST_T0 = null;
$("test-run").addEventListener("click", async () => {
  const btn = $("test-run"), msg = $("test-msg");
  msg.className = "msg"; msg.textContent = "";
  btnBusy(btn, "Starting…");
  try {
    const r = await (await fetch("/test-run", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"})).json();
    if (!r.ok) { msg.className = "msg err"; msg.textContent = r.error || "Could not start."; btnDone(btn); return; }
    TEST_T0 = Date.now();
    pollTest();
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); btnDone(btn); }
});

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
  for (const r of rows) {
    const cls = r.cleared ? "tjrow ok" : "tjrow no";
    const badge = r.cleared ? `✓ ${r.fit_score}` : `✗ ${r.fit_score}`;
    const meta = [r.location, r.compensation].filter(Boolean).map(escapeHtml).join(" · ");
    html += `<div class="${cls}">`
      + `<div class="tjtop"><span class="tjscore">${badge}</span>`
      + `<span class="tjname">${escapeHtml(r.company)} — ${escapeHtml(r.title)}</span></div>`;
    if (meta) html += `<div class="tjmeta">${meta}</div>`;
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
  const el = TEST_T0 ? Math.round((Date.now()-TEST_T0)/1000) : null;
  if ((running || filled) && el!=null) body += `<div class="tmeta">${el}s elapsed</div>`;
  prog.innerHTML = body;

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
  if (s.phase === "done") { btnDone(btn); msg.className = "msg ok"; msg.textContent = "Done — recorded a dry-run row in Track."; }
  if (s.phase === "error") { btnDone(btn); }

  if (running || filled) { clearTimeout(TEST_TIMER); TEST_TIMER = setTimeout(pollTest, 1200); }
  else { btnDone(btn); }
}

// ---- Track tab: the local application store (applications.db) ----
// Columns to show, in order. status/dates get special controls; the rest are text inputs.
// [key, label, default pixel width] — a fixed-layout table with per-column pixel widths so
// columns are individually resizable (drag the right edge) and can overflow into a horizontal
// scroll, like a spreadsheet. Per-user width/hidden overrides persist in localStorage.
const TRACK_COLS = [
  ["status","Status",120], ["company","Company",140], ["role","Role",180], ["location","Location",140],
  ["remote","Remote",80], ["pay","Pay",100], ["portal","Portal",110], ["method","Method",90],
  ["source_url","Source URL",220], ["date_discovered","Discovered",120], ["date_applied","Applied",120],
  ["resume_path","Résumé used",160], ["notes","Notes",240],
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

function statusCell(app) {
  const sel = el("select", {class:"st-" + app.status.replace("-","")});
  for (const s of TRACK_STATE.statuses) {
    const o = el("option", {value:s, text:s}); if (s===app.status) o.selected = true; sel.appendChild(o);
  }
  sel.addEventListener("change", () => { sel.className = "st-" + sel.value.replace("-",""); saveCell(app.id, "status", sel.value); });
  return sel;
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
    .concat([el("col", {style:"width:96px"})]);
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
    tds.push(el("td", {}, [el("div", {style:"display:flex;gap:6px;align-items:center"}, [del, saved])]));
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
function qaCard(qa) {
  qa = qa || {};
  const hasAns = (qa.answer || "").trim();
  const mapped = (qa.maps_to || "").trim();
  const fields = [];
  if (mapped) fields.push(el("div", {style:"font-size:12px;font-weight:600;color:#0b7a3b;margin-bottom:4px", text:"↔ Auto-answered from your profile ("+mapped+") — no action needed"}));
  else if (!hasAns) fields.push(el("div", {style:"font-size:12px;font-weight:600;color:#b26a00;margin-bottom:4px", text:"○ Needs your answer — captured from an application"}));
  else if (qa.generated) fields.push(el("div", {style:"font-size:12px;font-weight:600;color:#6a4bd0;margin-bottom:4px", text:"✨ AI-drafted — review & edit"}));
  fields.push(area("Question","question",qa.question), area("Answer","answer",qa.answer));
  // Preserve the classification/flag through the save round-trip (cardData reads any [data-k]).
  fields.push(el("input", {type:"hidden", "data-k":"maps_to", value: mapped}));
  fields.push(el("input", {type:"hidden", "data-k":"generated", value: qa.generated ? "1" : ""}));
  return entryCard(fields, c => { const q = (cardData(c).question||"").trim(); return q.length > 70 ? q.slice(0,70)+"…" : q; });
}
function acctRow(name, ok, text) {
  return el("div", {style:"display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)"}, [
    el("span", {style:"font-weight:700;font-size:15px;color:"+(ok?"#0b7a3b":"#b0b0b0"), text: ok ? "✓" : "○"}),
    el("span", {style:"font-weight:600;min-width:130px", text:name}),
    el("span", {style:"color:var(--muted,#666);font-size:13px", text:text}),
  ]);
}
function nativeAccountsPanel() {
  const ghOK = !!((P.greenhouse_email||"").trim() && (P.greenhouse_password||"").trim());
  const card = el("div", {class:"card"}, [
    el("p", {class:"hint", text:"Which native autofills the Apply stage can use. Greenhouse uses your MyGreenhouse login (set below); Lever/Ashby/Workday parse your uploaded résumé and need no account."}),
    acctRow("MyGreenhouse", ghOK, ghOK ? ("Connected · " + P.greenhouse_email) : "Not set up — add credentials below"),
    acctRow("Lever", true, "No login needed — résumé-parse autofill"),
    acctRow("Ashby", true, "No login needed — résumé-parse autofill"),
    acctRow("Workday", true, "No login needed — résumé-parse autofill"),
  ]);
  return el("div", {class:"sec"}, [el("h3", {text:"Autofill accounts"}), card]);
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
    row2(fld("Desired salary","desired_salary",P.desired_salary), startDateField(P.earliest_start_date)),
    fld("Years of experience","years_experience",P.years_experience),
    fld("How did you hear about this job? (default answer)","how_heard",P.how_heard),
    row2(fld("Gender (optional)","gender",P.gender), fld("Race / ethnicity (optional)","race_ethnicity",P.race_ethnicity)),
    row2(fld("Veteran status (optional)","veteran_status",P.veteran_status), fld("Disability status (optional)","disability_status",P.disability_status)),
  );
  put("s-applicant", el("div", {class:"sec"}, [
    el("h3", {text:"Applicant details"}),
    el("p", {class:"subhint", text:"Contact, work eligibility, and optional EEO — used to auto-fill application forms."}),
    applicant]));

  // Résumé content (source of truth for tailoring) — collapsible entries.
  put("s-experience", section("Experience","sec-experience",(R.experience||[]).map(expCard),"+ Add experience",()=>expCard()));
  put("s-activities", section("Leadership & activities","sec-activities",(R.activities||[]).map(expCard),"+ Add activity",()=>expCard()));
  put("s-projects", section("Projects","sec-projects",(R.projects||[]).map(projCard),"+ Add project",()=>projCard()));
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
  put("s-screening", section("Saved answers to screening questions","sec-qa",(P.custom_answers||[]).map(qaCard),"+ Add answer",()=>qaCard()));

  // Autofill accounts status + native logins (apply profile).
  put("s-accounts", nativeAccountsPanel());
  const creds = el("div", {id:"creds-card", class:"card"});
  creds.append(
    el("p", {class:"hint", text:"Optional. If set, the Apply stage logs into Greenhouse's own MyGreenhouse account and uses its autofill first, then fills the rest. Stored locally in your git-ignored profile."}),
    row2(fld("MyGreenhouse email","greenhouse_email",P.greenhouse_email), fld("MyGreenhouse password","greenhouse_password",P.greenhouse_password)));
  put("s-logins", el("div", {class:"sec"}, [el("h3", {text:"Native autofill logins (optional)"}), creds]));

  // Section-jump nav (s-linkedin is the static import block below the form).
  const jump = [
    ["s-applicant","Applicant details"], ["s-experience","Experience"], ["s-activities","Activities"],
    ["s-projects","Projects"], ["s-education","Education"], ["s-skills","Skills"],
    ["s-resume-header","Résumé header"], ["s-screening","Screening answers"],
    ["s-accounts","Autofill accounts"], ["s-logins","Logins"], ["s-linkedin","LinkedIn import"],
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
    desired_salary:t("desired_salary"), earliest_start_date:earliest_start_date, years_experience:t("years_experience"),
    gender:t("gender"), race_ethnicity:t("race_ethnicity"), veteran_status:t("veteran_status"), disability_status:t("disability_status"),
    greenhouse_email:t("greenhouse_email"), greenhouse_password:t("greenhouse_password"),
    custom_answers: cardsIn("sec-qa").map(c => { const q = cardData(c); return { question:(q.question||"").trim(), answer:(q.answer||"").trim(), maps_to:(q.maps_to||"").trim(), generated: q.generated === "1" }; }).filter(x => x.question || x.answer || x.maps_to),
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

function escapeHtml(s){ const d=document.createElement("div"); d.textContent=s; return d.innerHTML; }
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

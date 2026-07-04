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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import apply_profile, auth, catalogue, linkedin, tracker
from .job_description import JobDescription, load_job_description
from .backends import DEFAULT_QUALITY
from .length import LengthBudget
from .models import TailoredResume
from .pdf import render_pdf
from .render import render_html, render_markdown
from .resume import load_resume
from .tailor import tailor_resume

REPO_ROOT = Path(__file__).resolve().parent.parent


def list_resumes() -> list[dict[str, str]]:
    # The apply profile lives alongside résumés in profile/ but is not a résumé — exclude it
    # so it never shows up as a selectable resume (it fails to load as a Resume).
    exclude = {Path(apply_profile.DEFAULT_PATH).name}
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
  .ttable { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; font-size:13px; }
  .ttable th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); padding:8px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
  .ttable td { padding:4px 6px; border-bottom:1px solid var(--line); vertical-align:middle; }
  .ttable tr:last-child td { border-bottom:0; }
  .ttable input, .ttable select { border:1px solid transparent; background:transparent; padding:5px 6px; margin:0; border-radius:4px; }
  .ttable input:hover, .ttable select:hover { border-color:var(--line); }
  .ttable input:focus, .ttable select:focus { border-color:var(--accent); background:#fff; outline:none; }
  .ttable .st-dryrun { color:#8a6d00; } .ttable .st-applied { color:#2a5bd7; }
  .ttable .st-responded { color:#2a9d5b; } .ttable .st-failed { color:#c0392b; }
  .ttable .st-discovered, .ttable .st-tailored { color:var(--muted); }
  .ttable .delrow { width:auto; margin:0; padding:4px 8px; background:#fff; color:#c0392b; border:1px solid var(--line); font-size:12px; }
  .ttable .rowsaved { color:#2a9d5b; font-size:12px; }
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
      <button class="tab" data-view="profile">Profile</button>
      <button class="tab" data-view="track">Track</button>
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
      <div class="editor">
        <p class="editing">Every application the pipeline discovered, tailored, and (in
          <code>dry_run</code>) would have submitted — the local system of record
          (<code>applications.db</code>, git-ignored). Edit any cell inline; changes save
          as you go.</p>
        <div id="track-counts" class="tcounts"></div>
        <div class="trackbar">
          <input id="track-search" type="text" placeholder="Search company, role, location, notes…">
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
  $("view-profile").classList.toggle("hidden", v !== "profile");
  $("view-track").classList.toggle("hidden", v !== "track");
  if (v === "profile") loadProfile();
  if (v === "track") loadTrack();
}));
$("resume").addEventListener("change", () => { if (!$("view-profile").classList.contains("hidden")) loadProfile(); });

// ---- Track tab: the local application store (applications.db) ----
// Columns to show, in order. status/dates get special controls; the rest are text inputs.
const TRACK_COLS = [
  ["status","Status"], ["company","Company"], ["role","Role"], ["location","Location"],
  ["remote","Remote"], ["pay","Pay"], ["portal","Portal"], ["method","Method"],
  ["source_url","Source URL"], ["date_discovered","Discovered"], ["date_applied","Applied"],
  ["resume_path","Résumé used"], ["notes","Notes"],
];
let TRACK_STATE = { status:null, search:"", statuses:[] };

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
  const body = $("track-body"); body.innerHTML = "";
  if (!apps.length) {
    body.append(el("div", {class:"tempty", text:
      TRACK_STATE.search || TRACK_STATE.status
        ? "No applications match this filter."
        : "No applications yet. The pipeline records them here as it runs — or add one manually."}));
    return;
  }
  const head = el("tr", {}, TRACK_COLS.map(([,label]) => el("th", {text:label})).concat([el("th", {text:""})]));
  const rows = apps.map(app => {
    const tds = TRACK_COLS.map(([key]) => {
      let input;
      if (key === "status") input = statusCell(app);
      else if (key === "date_discovered" || key === "date_applied")
        input = el("input", {type:"date", value:app[key] || "", on:{change:e=>saveCell(app.id, key, e.target.value)}});
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
  const table = el("table", {class:"ttable"}, [el("thead", {}, [head]), el("tbody", {}, rows)]);
  body.append(el("div", {class:"twrap"}, [table]));
}

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
  const fields = [];
  if (!hasAns) fields.push(el("div", {style:"font-size:12px;font-weight:600;color:#b26a00;margin-bottom:4px", text:"○ Needs your answer — captured from an application"}));
  else if (qa.generated) fields.push(el("div", {style:"font-size:12px;font-weight:600;color:#6a4bd0;margin-bottom:4px", text:"✨ AI-drafted — review & edit"}));
  fields.push(area("Question","question",qa.question), area("Answer","answer",qa.answer));
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
    custom_answers: cardsIn("sec-qa").map(c => { const q = cardData(c); return { question:(q.question||"").trim(), answer:(q.answer||"").trim() }; }).filter(x => x.question || x.answer),
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

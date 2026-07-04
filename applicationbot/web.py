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

from . import apply_profile, auth, catalogue, linkedin
from .job_description import JobDescription, load_job_description
from .length import LengthBudget
from .models import TailoredResume
from .pdf import render_pdf
from .render import render_html, render_markdown
from .resume import load_resume
from .tailor import tailor_resume

REPO_ROOT = Path(__file__).resolve().parent.parent


def list_resumes() -> list[dict[str, str]]:
    out = []
    for folder in ("profile", "examples"):
        for p in sorted((REPO_ROOT / folder).glob("*.yaml")):
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
  .msg.ok { color:#2a9d5b; } .msg.err { color:#c0392b; }
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
      <button class="tab" data-view="data">Résumé data</button>
      <button class="tab" data-view="profile">Apply profile</button>
    </div>

    <div id="view-review">
      <div id="status" class="empty">Pick a resume and job, then tailor.</div>
      <div id="meta" class="meta hidden"></div>
      <button id="dl-pdf" class="hidden">⬇ Download PDF</button>
      <div id="result"></div>
    </div>

    <div id="view-data" class="hidden">
      <div class="editor">
        <p class="editing">Editing <b id="editing-path"></b> — your source of truth. Edit
          basic info, experience, activities, projects, education, and skills; add or remove
          entries; then <b>Save</b>. Tailoring picks the relevant parts per job.</p>

        <div class="linkedin">
          <h3 style="margin-top:0">Import from LinkedIn</h3>
          <p class="editing">LinkedIn can't be linked live (their API restricts it and
            scraping breaks their terms). Instead, on LinkedIn go to <b>Settings → Data
            Privacy → Get a copy of your data</b>, download the archive, and upload it here
            (the <code>.zip</code>, or the Positions/Education/Skills <code>.csv</code>
            files). We'll merge new experience, education, and skills below (existing entries
            aren't touched).</p>
          <input id="li-file" type="file" accept=".zip,.csv">
          <button id="li-import" type="button">Import</button>
          <span id="li-msg" class="msg"></span>
        </div>

        <div id="edit-form"></div>
        <div class="saverow">
          <button id="save-resume">Save résumé data</button>
          <span id="save-msg" class="msg"></span>
        </div>
      </div>
    </div>

    <div id="view-profile" class="hidden">
      <div class="editor">
        <p class="editing">Answers used to auto-fill application forms — work eligibility,
          preferences, optional EEO, and a growing bank of answers to screening questions.
          Saved to <code>profile/application_profile.yaml</code> (git-ignored). Then Save.</p>
        <div id="profile-form"></div>
        <div class="saverow">
          <button id="save-profile">Save apply profile</button>
          <span id="profile-msg" class="msg"></span>
        </div>
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
  const res = await fetch("/pdf", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(lastReq) });
  if (!res.ok) { let e = {}; try { e = await res.json(); } catch (x) {} alert(e.error || "PDF export failed"); return; }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a"); a.href = url; a.download = "tailored_resume.pdf";
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
});
$("go").addEventListener("click", async () => {
  const mode = $("jobmode").value;
  const payload = {
    resume: $("resume").value,
    backend: $("backend").value,
    pages: parseFloat($("pages").value),
    line_chars: parseInt($("linechars").value) || 100,
    job: mode === "custom"
      ? { mode:"custom", title:$("title").value, company:$("company").value, body:$("body").value }
      : { mode:"fixture", fixture:$("fixture").value },
  };
  $("go").disabled = true;
  $("status").textContent = "Tailoring…"; $("status").classList.remove("hidden");
  $("meta").classList.add("hidden"); $("dl-pdf").classList.add("hidden"); $("result").innerHTML = "";
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
  } catch (e) {
    $("status").classList.remove("hidden");
    $("status").innerHTML = `<div class="warn">${escapeHtml(String(e.message || e))}</div>`;
  } finally {
    $("go").disabled = false;
  }
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
const delBtn = card => el("button", {class:"del", type:"button", text:"✕", title:"Remove", on:{click:()=>card.remove()}});
const cardData = card => { const o = {}; card.querySelectorAll("[data-k]").forEach(f => o[f.dataset.k] = f.value); return o; };

function expCard(e) {
  e = e || {}; const card = el("div", {class:"card"});
  card.append(
    row2(fld("Organization","organization",e.organization), fld("Role / title","role",e.role)),
    fld("Location","location",e.location),
    row2(fld("Start — e.g. May 2024","start",e.start), fld("End — e.g. Present","end",e.end)),
    area("Bullets (one per line)","bullets",(e.bullets||[]).join("\\n")),
    delBtn(card));
  return card;
}
function projCard(p) {
  p = p || {}; const card = el("div", {class:"card"});
  card.append(
    row2(fld("Project name","name",p.name), fld("Tech — e.g. Python, SQL","tech",p.tech)),
    area("Bullets (one per line)","bullets",(p.bullets||[]).join("\\n")),
    delBtn(card));
  return card;
}
function eduCard(e) {
  e = e || {}; const card = el("div", {class:"card"});
  card.append(
    row2(fld("School","school",e.school), fld("Location","location",e.location)),
    row2(fld("Degree","degree",e.degree), fld("Graduation","graduation",e.graduation)),
    area("Details (one per line)","details",(e.details||[]).join("\\n")),
    delBtn(card));
  return card;
}
function skillCard(s) {
  s = s || {}; const card = el("div", {class:"card"});
  card.append(
    fld("Category","category",s.category),
    fld("Items (comma-separated)","items",(s.items||[]).join(", ")),
    delBtn(card));
  return card;
}
function section(title, id, items, addLabel, blank) {
  const body = el("div", {id:id, class:"cards"}, items);
  return el("div", {class:"sec"}, [
    el("h3", {text:title}), body,
    el("button", {class:"addbtn", type:"button", text:addLabel, on:{click:()=>body.appendChild(blank())}}),
  ]);
}
function renderForm() {
  const f = $("edit-form"); f.innerHTML = "";
  const c = R.contact || {};
  const basic = el("div", {id:"basic", class:"card"});
  basic.append(
    row2(fld("Name","name",c.name), fld("Email","email",c.email)),
    row2(fld("Phone","phone",c.phone), fld("Location","location",c.location)),
    area("Links (one per line)","links",(c.links||[]).join("\\n")),
    area("Summary (optional)","summary",R.summary||"","A short professional summary…"),
    area("Certifications (one per line)","certifications",(R.certifications||[]).join("\\n")));
  f.append(el("div", {class:"sec"}, [el("h3", {text:"Basic info"}), basic]));
  f.append(section("Skills","sec-skills",(R.skills||[]).map(skillCard),"+ Add skill category",()=>skillCard()));
  f.append(section("Experience","sec-experience",(R.experience||[]).map(expCard),"+ Add experience",()=>expCard()));
  f.append(section("Leadership & activities","sec-activities",(R.activities||[]).map(expCard),"+ Add activity",()=>expCard()));
  f.append(section("Projects","sec-projects",(R.projects||[]).map(projCard),"+ Add project",()=>projCard()));
  f.append(section("Education","sec-education",(R.education||[]).map(eduCard),"+ Add education",()=>eduCard()));
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
  $("view-data").classList.toggle("hidden", v !== "data");
  $("view-profile").classList.toggle("hidden", v !== "profile");
  if (v === "data") loadData();
  if (v === "profile") loadProfile();
}));
async function loadData() {
  $("editing-path").textContent = currentResume();
  $("save-msg").textContent = ""; $("edit-form").textContent = "Loading…";
  try {
    const d = await (await fetch("/resume?path=" + encodeURIComponent(currentResume()))).json();
    if (d.error) throw new Error(d.error);
    R = d.resume; renderForm();
  } catch (e) {
    $("edit-form").innerHTML = ""; $("edit-form").appendChild(el("div", {class:"msg err", text:String(e.message || e)}));
  }
}
async function saveResume() {
  const btn = $("save-resume"), msg = $("save-msg");
  btn.disabled = true; msg.className = "msg"; msg.textContent = "Saving…";
  try {
    const d = await (await fetch("/resume/update", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ resume: currentResume(), data: collect() }) })).json();
    if (!d.ok) throw new Error(d.error || "save failed");
    await loadData();
    msg.className = "msg ok"; msg.textContent = "Saved ✓";
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { btn.disabled = false; }
}
$("save-resume").addEventListener("click", saveResume);
$("resume").addEventListener("change", () => { if (!$("view-data").classList.contains("hidden")) loadData(); });

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
  btn.disabled = true; msg.className = "msg"; msg.textContent = "Importing…";
  try {
    const d = await (await fetch("/resume/import-linkedin", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ resume: currentResume(), filename: f.name, data_b64: await fileB64(f) }) })).json();
    if (!d.ok) throw new Error(d.error || "import failed");
    const a = d.added || {};
    msg.className = "msg ok";
    msg.textContent = `Imported ${a.experience||0} experience, ${a.education||0} education, ${a.skills||0} skills`
      + ((d.found_files||[]).length ? " (from " + d.found_files.join(", ") + ")." : " — no LinkedIn CSVs found in that file.");
    await loadData();
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { btn.disabled = false; }
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
function qaCard(qa) {
  qa = qa || {}; const card = el("div", {class:"card"});
  const hasAns = (qa.answer || "").trim();
  let badge = null;
  if (!hasAns) badge = el("div", {style:"font-size:12px;font-weight:600;color:#b26a00;margin-bottom:4px", text:"○ Needs your answer — captured from an application"});
  else if (qa.generated) badge = el("div", {style:"font-size:12px;font-weight:600;color:#6a4bd0;margin-bottom:4px", text:"✨ AI-drafted — review & edit"});
  card.append(...(badge ? [badge] : []), area("Question","question",qa.question), area("Answer","answer",qa.answer), delBtn(card));
  return card;
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
function renderProfile() {
  const f = $("profile-form"); f.innerHTML = "";
  f.append(nativeAccountsPanel());
  const card = el("div", {id:"profile-card", class:"card"});
  card.append(
    row2(fld("First name","first_name",P.first_name), fld("Last name","last_name",P.last_name)),
    row2(fld("Email","email",P.email), fld("Phone","phone",P.phone)),
    row2(fld("Location","location",P.location), fld("Country","country",P.country)),
    row2(fld("LinkedIn URL","linkedin_url",P.linkedin_url), fld("GitHub URL","github_url",P.github_url)),
    fld("Portfolio / website","portfolio_url",P.portfolio_url),
    row2(boolSel("Authorized to work?","work_authorized",P.work_authorized), boolSel("Requires sponsorship?","requires_sponsorship",P.requires_sponsorship)),
    row2(boolSel("U.S. citizen?","us_citizen",P.us_citizen), boolSel("Willing to relocate?","willing_to_relocate",P.willing_to_relocate)),
    boolSel("Open to remote?","open_to_remote",P.open_to_remote),
    row2(fld("Desired salary","desired_salary",P.desired_salary), fld("Earliest start date","earliest_start_date",P.earliest_start_date)),
    fld("Years of experience","years_experience",P.years_experience),
    fld("How did you hear about this job? (default answer)","how_heard",P.how_heard),
    row2(fld("Gender (optional)","gender",P.gender), fld("Race / ethnicity (optional)","race_ethnicity",P.race_ethnicity)),
    row2(fld("Veteran status (optional)","veteran_status",P.veteran_status), fld("Disability status (optional)","disability_status",P.disability_status)),
  );
  f.append(el("div", {class:"sec"}, [el("h3", {text:"Applicant details"}), card]));

  const creds = el("div", {id:"creds-card", class:"card"});
  creds.append(
    el("p", {class:"hint", text:"Optional. If set, the Apply stage logs into Greenhouse's own MyGreenhouse account and uses its autofill first, then fills the rest. Stored locally in your git-ignored profile."}),
    row2(fld("MyGreenhouse email","greenhouse_email",P.greenhouse_email), fld("MyGreenhouse password","greenhouse_password",P.greenhouse_password)),
  );
  f.append(el("div", {class:"sec"}, [el("h3", {text:"Native autofill logins (optional)"}), creds]));

  f.append(section("Saved answers to screening questions", "sec-qa", (P.custom_answers||[]).map(qaCard), "+ Add answer", () => qaCard()));
}
function collectProfile() {
  const d = Object.assign({}, cardData($("profile-card")), cardData($("creds-card")));
  const tri = k => (d[k] === "yes" ? true : (d[k] === "no" ? false : null));
  const t = k => (d[k] || "").trim();
  return {
    first_name:t("first_name"), last_name:t("last_name"), email:t("email"), phone:t("phone"), location:t("location"),
    country:t("country"), how_heard:t("how_heard"),
    linkedin_url:t("linkedin_url"), github_url:t("github_url"), portfolio_url:t("portfolio_url"),
    work_authorized:tri("work_authorized"), requires_sponsorship:tri("requires_sponsorship"), us_citizen:tri("us_citizen"),
    willing_to_relocate:tri("willing_to_relocate"), open_to_remote:tri("open_to_remote"),
    desired_salary:t("desired_salary"), earliest_start_date:t("earliest_start_date"), years_experience:t("years_experience"),
    gender:t("gender"), race_ethnicity:t("race_ethnicity"), veteran_status:t("veteran_status"), disability_status:t("disability_status"),
    greenhouse_email:t("greenhouse_email"), greenhouse_password:t("greenhouse_password"),
    custom_answers: cardsIn("sec-qa").map(c => { const q = cardData(c); return { question:(q.question||"").trim(), answer:(q.answer||"").trim() }; }).filter(x => x.question || x.answer),
  };
}
async function loadProfile() {
  $("profile-msg").textContent = ""; $("profile-form").textContent = "Loading…";
  try {
    const d = await (await fetch("/profile")).json();
    if (d.error) throw new Error(d.error);
    P = d.profile; renderProfile();
  } catch (e) { $("profile-form").innerHTML = ""; $("profile-form").appendChild(el("div", {class:"msg err", text:String(e.message || e)})); }
}
async function saveProfile() {
  const btn = $("save-profile"), msg = $("profile-msg");
  btn.disabled = true; msg.className = "msg"; msg.textContent = "Saving…";
  try {
    const d = await (await fetch("/profile/update", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ data: collectProfile() }) })).json();
    if (!d.ok) throw new Error(d.error || "save failed");
    await loadProfile(); msg.className = "msg ok"; msg.textContent = "Saved ✓";
  } catch (e) { msg.className = "msg err"; msg.textContent = String(e.message || e); }
  finally { btn.disabled = false; }
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

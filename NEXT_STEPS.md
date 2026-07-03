# NEXT_STEPS.md — Living Build Queue

> Read this at the start of every session to pick up where the last one left off.
> Update it at the end of every session where anything changed (Agent Guideline #10).

---

## Current state

- **Stage 3 (Tailor) has a first working implementation.** The resume customizer is
  built in Python: base resume → Claude tailors it to a job description → Markdown out.
- Docs in place: [CLAUDE.md](CLAUDE.md), [README.md](README.md), this file, and
  [DECISIONS.md](DECISIONS.md). Foundational decisions logged (001–004).
- Scope defined at a high level: a **cloneable, personalized, filter-driven** pipeline
  with five stages — **Configure → Discover → Tailor → Apply → Track** — that runs with
  no human intervention, gated by a `dry_run` safety switch.
- Stack decided: **Python** (decision 001); LLM is **Claude `claude-opus-4-8`** via the
  Anthropic SDK (decision 004).

### The customizer (implemented)

Package [applicationbot/](applicationbot/):
- `models.py` — Pydantic schema: base `Resume` (source of truth) + `TailoredResume`.
- `resume.py` — load a base resume from YAML.
- `job_description.py` — parse JD fixtures (Markdown + YAML front matter).
- `tailor.py` — the Claude call (`messages.parse` w/ structured output) + a
  `check_factual_drift` guard that flags any skill/role/cert not in the base resume.
- `render.py` — render a tailored resume to Markdown.
- `cli.py` — `python -m applicationbot.cli JD_FILE [--resume R.yaml] [--out O.md] [--backend ...]`.
- `backends.py` — pluggable engines: `claude` (primary, OAuth/key) + `rules` (no-key
  fallback); `auto` selection.
- `web.py` — local review UI (`python -m applicationbot.web`, stdlib http.server on
  127.0.0.1): pick resume + job + engine, see the tailored resume rendered as HTML plus
  notes/warnings. `render.render_html` is the HTML render target.

Test data: a synthetic full-stack sample resume ([examples/sample_resume.yaml](examples/sample_resume.yaml))
and real job-description fixtures collected from live ATS postings (in the scratchpad;
**not yet moved into the repo** — see Now).

**Verified:** resume loading, JD parsing, rendering, and drift detection all pass an
offline smoke test. **Not yet verified:** the live Claude call — needs API credentials
(`ANTHROPIC_API_KEY` or `ant auth login`), which weren't available in the build shell.

To run once credentials are set:
```
pip install -r requirements.txt
python -m applicationbot.cli <path-to-jd.md>
```

---

## Target architecture (working sketch, not final)

Five stages, each a candidate module boundary:

1. **Configure** — user profile + filters. Personal/contact info, base resume, and
   job-search filters (roles, keywords, location/remote, pay range, seniority, company
   type). All user-specific data is local and git-ignored.
2. **Discover** — scraper(s) that find postings matching the filters and extract
   structured posting details.
3. **Tailor** — resume (and optional cover-letter) customization per posting.
4. **Apply** — form/portal auto-fill + submission, behind the `dry_run` safety switch.
5. **Track** — persistent record of every application (see data model below).

### Tracked application record (fields to capture)

Company, role/title, location, remote/on-site, **pay rate**, **application portal**,
application method, source URL, date discovered, date applied, status
(discovered / tailored / applied / dry-run / failed / responded), tailored resume used,
and free-form notes.

---

## Now

- [ ] **Run the customizer live via Claude** on `profile/resume.yaml` once logged in
      (`ant auth login`, no key needed) — confirm bullet-rewriting output is factual, the
      drift check stays clean, and the format matches. (`rules` path already verified.)
- [ ] Re-run the frontend/full-stack JD collector — it didn't land; we have 6 fixtures
      (backend + data/ML) in `fixtures/job_descriptions/`, want ~3 frontend too.
- [ ] Add a smoke test / tiny pytest for the non-API pieces (loading, parsing, render,
      `check_factual_drift`, `select_backend`).

## Next

### Apply stage (decision 016) — the current focus

- [x] **PDF resume export** — done via `fpdf2` (pure Python, **no Chromium**): real-text,
      single-column, ATS-parseable, generated from the structured resume. Download button in
      the review UI; `--out x.pdf` in the CLI.
- [ ] **Greenhouse Playwright adapter, dry-run only** — Playwright is needed here (a browser
      IS the form-fill mechanism; unrelated to PDF). Dry-run UX (user's spec): **headed,
      slow-mo browser so you watch it fill the form live**, then **pause at the end with the
      form filled for review** + screenshot; never clicks submit (Guideline #3). Map fields
      from the resume + apply profile, upload the PDF. Then Lever/Ashby adapters.
- [ ] **Screening-question answering** — Claude (subscription) drafts truthful answers from
      the apply profile + resume + JD; save them to the answer bank so it's asked once.
- [ ] **Autonomous runner + tracking** — a queue/loop that tailors → fills → (when armed)
      submits, records each application (Track stage fields), and surfaces blockers
      (CAPTCHA/login/unanswerable) as periodic updates, not prompts. Global kill switch.
- [ ] Later: browser **extension** surface for sites that resist headless automation.

### Other

- [ ] Editor niceties — drag-to-reorder entries/bullets, and a live length/1-page meter.
- [ ] Skills-line wrapping — the Skills section renders `Category: a, b, c…` lines that can
      wrap to a short second line (separate from the achievement-bullet rule); tidy if it
      bothers in practice.
- [ ] Catalogue efficiency v2 (only if needed) — if keyword pre-selection misses
      semantically-relevant items on a large catalogue, add embeddings/semantic retrieval
      (decision 013 lists this as the future upgrade).
- [ ] **PDF/DOCX export** from the web UI (a "Download" button) — the HTML render exists;
      add a print-to-PDF or a templated document render that mirrors the source layout.
- [ ] Cover-letter generation (same structured + LLM approach).
- [ ] Design the **user profile + filter** schema (Configure stage).
- [ ] Design the **tracked application record** schema (Track stage).
- [ ] Prototype the scraper (Discover) against one job board / career page; emit the same
      JD shape the fixtures use, so the customizer needs no changes.
- [ ] Stand up the tracking store and write a record end-to-end in `dry_run`.

## Later

- [ ] Auto-fill + submit flow with the `dry_run` default and global kill switch.
- [ ] Per-site adapters for common application portals.
- [ ] Dashboard / status view over tracked applications (see UI Design Principles).
- [ ] Rate limiting and site-terms compliance for the scraper.
- [ ] Cover-letter generation.
- [ ] Onboarding flow for a freshly-cloned repo (get a new user configured quickly).

---

## Recently added (this session, latest first)

- 2026-07-03 — **Agent bus for parallel Cursor ↔ Claude VS Code work** (decision 014):
  git-ignored `.agent-bus/`, `applicationbot/agent_bus.py` CLI (post/read/ack/claim/watch),
  Cursor hooks (sessionStart + stop), and [docs/AGENT_COLLAB.md](docs/AGENT_COLLAB.md).
  Run `python -m applicationbot.agent_bus watch --agent <cursor|claude>` in a side terminal
  for canary alerts without waiting for prompts to finish.

- 2026-07-03 — **PDF export without Chromium** — chose `fpdf2` (pure Python, pip-only, no
  system libs) over weasyprint/wkhtmltopdf. `applicationbot/pdf.py` renders the tailored
  resume to a real-text, ATS-parseable single-column PDF from the structured data. Wired a
  **Download PDF** button into the review UI (`POST /pdf`) and `--out x.pdf` into the CLI.
  Verified (valid `%PDF`, endpoint + CLI). Clears the Apply prerequisite of "a file to upload."
- 2026-07-03 — **Started the Apply stage** (decision 016). Researched it: no candidate-facing
  ATS submission API exists, so we drive the real form via **per-ATS Playwright**,
  **autonomous-first** (blockers become logged exceptions, not prompts), with a browser
  extension as a later surface. Built the first foundation piece: the **application-answer
  profile** (`applicationbot/apply_profile.py` + an "Apply profile" web tab) — work
  eligibility, EEO, salary/start, links, and a growing bank of answers to screening
  questions, stored git-ignored. Verified end-to-end.
- 2026-07-03 — **LinkedIn import** (decision 015, `applicationbot/linkedin.py`): parses the
  user's LinkedIn "Get a copy of your data" export (ZIP or CSVs) and merges new
  experience/education/skills into the catalogue, deduping against existing entries. Upload
  via the Résumé data tab (base64 JSON to `POST /resume/import-linkedin`). Live LinkedIn
  linking isn't possible (API-restricted; scraping breaks ToS) — this is the compliant path.
  Verified end-to-end (synthetic export → merged, deduped).
- 2026-07-03 — Made the one-line **bullet character length a configurable input**
  (`LengthBudget.line_chars`, default 100) — `--line-chars` (CLI) and a "Line length"
  field (web). The prompt derives the one-line limit and forbidden slightly-over zone from
  it. (Bullet exactness is a nitpick; the real goal is ATS-friendly output.)

## Decisions needed

- Tech stack and primary language.
- Scraping strategy (per-site adapters vs. generic extraction; how to stay within site
  terms and handle rate limits).
- Resume-tailoring method (template + rules, LLM-based rewrite, or hybrid).
- Application-submission approach (headless browser form automation, per-site adapters).
- Storage for profile, postings, resumes, and application history (files vs. database).
- Config format for the user profile + filters (and how a cloned user sets it up).
- How the `dry_run` / armed state and global kill switch are represented and toggled.

Record each decision in [DECISIONS.md](DECISIONS.md) once the user chooses.

---

## Recently completed

- 2026-07-03 — Repurposed the repo from a prior project; rewrote CLAUDE.md, README.md,
  and stubbed NEXT_STEPS.md and DECISIONS.md for ApplicationBot.
- 2026-07-03 — Expanded scope to the five-stage cloneable/personalized design
  (Configure → Discover → Tailor → Apply → Track); reconciled full-automation intent
  with a `dry_run` safety switch in Agent Guideline #3.
- 2026-07-03 — Turned the Résumé data tab into a **full edit form**: edit/add/delete any
  experience, activity, project, education, or skill entry, plus a **Basic info** section
  (name, contact, links, summary, certifications) — all editable before generating. Saves
  the whole résumé back through a validated round-trip (`catalogue.replace_resume`,
  `POST /resume/update`). Verified: rename + bullet edit + project delete + education add
  all persist; traversal guard holds. **Tightened the bullet rule to character-based**
  (one line ≤~85 chars, forbid the 86–135 "slightly-over" zone) — a real Claude run put
  all 17 achievement bullets cleanly on one line. Fixed a bug where `\n` in the page's JS
  string became a real newline (broke all dropdowns); now the rendered page JS is
  `node --check`ed during verification.
- 2026-07-03 — Added a **configurable length budget** (decision 012, `applicationbot/length.py`):
  `LengthBudget(pages)` derives per-section entry caps + bullets-per-entry; it's both
  instructed to Claude and hard-enforced after. Exposed as `--pages` (CLI) and a Length
  dropdown (web, 1 / 1.5 / 2 pages). Verified end-to-end on a real Claude Code run (1-page
  output within caps, consistent bullets).
- 2026-07-03 — Made Claude calls **token-efficient as the catalogue grows** (decision 013):
  `catalogue.select_relevant` pre-selects the job-relevant slice locally (free keyword pass
  via the new shared `applicationbot/relevance.py`) before sending to Claude — small
  catalogues pass through unchanged, large ones are bounded to ~2× the length budget.
  Verified: a 30-project catalogue trims to 6 before the call.
- 2026-07-03 — Added a **résumé-data (catalogue) editor** to the web UI (`applicationbot/catalogue.py`
  + a "Résumé data" tab): add experience/activities/projects that weren't on the uploaded
  resume, or add bullets to an existing entry — writing back to the git-ignored base YAML
  (validated round-trip). First step toward the catalogue (decision 007). Also **tightened
  the bullet rule**: each bullet must be either one full line or at least ~1.5 lines (no
  short dangling second line) — a prompt change, so it applies to the `claude-code` engine.
- 2026-07-03 — **Switched the Claude engine to use the Claude subscription, not the API**
  (decision 011). Confirmed via Anthropic docs that any `anthropic` SDK call — with an API
  key OR `ant auth login` — is billed as API usage; subscriptions are only reachable via
  Claude's own tools. So replaced the SDK backend with `ClaudeCodeBackend`, which shells
  out to `claude -p` (Claude Code) → runs on the user's subscription. Removed the
  `ant`/OAuth path and the `anthropic` dependency; reframed the web account panel; updated
  `run.sh` to check for Claude Code instead of installing `ant`. Verified end-to-end: a
  real `claude -p` tailoring produced factual, well-formatted output, drift-clean, no API
  usage. (This corrects an earlier wrong claim that `ant auth login` used the subscription
  — it's the API.)
- 2026-07-03 — Diagnosed a connectivity issue: the user's GlobalProtect VPN was blocking
  github.com AND api.anthropic.com; `run.sh` now detects an unreachable GitHub and
  explains it instead of dumping a git error (that check is now moot for Claude since we
  use Claude Code, but still useful).
- 2026-07-03 — Added `scripts/restart.sh` (stop + start on the latest code; delegates to
  stop.sh + run.sh).
- 2026-07-03 — `scripts/run.sh` now **auto-installs the `ant` CLI** (best-effort via
  Homebrew, non-fatal) so the web UI's "Log in with Claude" button is genuinely one-click,
  not a to-do list. Added a top-level **"Intuitive by default"** UI Design Principle to
  CLAUDE.md (every button clearly labeled with what it does; setup scripted so the UI shows
  a working button) and broadened the notifications principle to take users straight to the
  fix. (In this sandbox the auto-install can't reach GitHub, so it falls back gracefully and
  still serves — it will install on a machine with network.)
- 2026-07-03 — Added **Claude sign-in from the web UI** (decision 010, `applicationbot/auth.py`):
  an account panel showing auth status + a "Log in with Claude" button that drives the
  official `ant auth login` OAuth flow (opens the browser; stores a profile the SDK reads).
  Degrades gracefully with install guidance when the `ant` CLI isn't present. Also added
  **bullet-formatting rules** to the tailoring prompt (consistent bullet length within an
  entry; dense, line-filling bullets; no wasted whitespace — takes effect with the `claude`
  engine, since `rules` can't reword) and **tightened the review-UI CSS** (denser spacing,
  clearer section labels/separation) for all output. Verified over HTTP; live OAuth and the
  claude-engine bullet formatting are untested here (no `ant` / no credentials).
- 2026-07-03 — Added `scripts/run.sh` (start the web UI: sets up venv + deps, opens the
  browser, idempotent) and `scripts/stop.sh` (stop it, idempotent) per Agent Guideline #8.
  Verified the UI serves over HTTP (GET / → 200, POST /tailor → 200) on localhost:8000.
- 2026-07-03 — Added a local **web review UI** (`applicationbot/web.py`, decision 009):
  stdlib `http.server` on 127.0.0.1, no dependencies. Pick resume + job (fixture or pasted
  posting) + engine, see the tailored resume rendered as a styled HTML resume plus notes,
  drift warnings, and the engine used. Added `render.render_html`. All handler logic
  verified in-process (server binds; loopback HTTP couldn't be exercised in this sandbox).
- 2026-07-03 — Moved the 6 JD fixtures into `fixtures/job_descriptions/` (with a README;
  fixed the Affirm front-matter quoting). Simplified the backends to **Claude (primary,
  via `ant auth login` OAuth or API key) + rules (no-LLM fallback)** — dropped the Ollama
  local-model backend as too much setup hassle for most users. `auto` = Claude if
  authenticated, else rules.
- 2026-07-03 — Made the tailoring backend **pluggable** (decision 008): engines behind one
  interface with `auto` selection. The **rules backend needs no API key at all** and was
  verified tailoring Gabriel's resume across all 6 real fixtures with zero credentials.
  Hardened JD front-matter parsing against unquoted colons (real postings) and tightened
  rules-based skill matching to avoid tiny/numeric-token false positives.
- 2026-07-03 — Extended the resume schema for format fidelity (categorized skills,
  leadership/activities section, project tech line, optional summary, `section_order`);
  updated renderer + drift check + prompt to match. Built Gabriel Chan's `profile/resume.yaml`
  from his PDF via Claude's native PDF reading (decision 005). Logged decisions 005–007
  (PDF→YAML approach, format preservation, and the catalogue direction). Rejected
  OpenDataLoader PDF (Java dependency not worth it for one small resume).
- 2026-07-03 — Added `.gitignore` (PII/secrets covered), logged decisions 001–004,
  and built the **resume customizer** (Tailor stage): Python package `applicationbot/`
  with a structured-data + Claude tailoring design, a factual-drift guard, a Markdown
  renderer, and a CLI. Collected real job-description fixtures via parallel subagents and
  a synthetic sample resume for testing. Non-API pipeline verified offline; live Claude
  call pending credentials.

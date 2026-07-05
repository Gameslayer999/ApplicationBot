# NEXT_STEPS.md — Living Build Queue

> Read this at the start of every session to pick up where the last one left off.
> Update it at the end of every session where anything changed (Agent Guideline #10).

---

## Current state

- **Stage 2 (Discover) has a first working, verified implementation (decision 026).**
  Qualification-driven, not company-driven: `applicationbot/pipeline.py` discovers postings
  from pluggable **sources** (`discovery.py`: public no-auth **Greenhouse/Lever/Ashby** APIs —
  full JD, same ATSs Apply fills — plus an optional **Adzuna** aggregator that self-skips
  without a free key), gates them (`filters.py`), and ranks by **qualification fit**
  (`matching.py`: free keyword pre-filter → Claude judges the top-N, naming missing
  requirements). `--apply-first` = **testing mode**: top match → tailor → PDF → headed
  **dry-run** apply you watch fill (never submits), recorded as a `dry-run` tracker row.
  *Verified live: 618 real postings across Stripe/cin7/Ramp, 0 errors; judge discriminates
  fit correctly; full loop ran end-to-end headless with `submitted:False`.* Emits the exact
  fixture JD shape, so Tailor/Apply needed no changes.
- **Stage 3 (Tailor) has a first working implementation.** The resume customizer is
  built in Python: base resume → Claude tailors it to a job description → Markdown out.
  User-selectable speed/quality tier (fast/balanced/max); default **balanced ≈ 35–40s**
  per résumé (was ~2 min — decision 025).
- **Stage 5 (Track) has its store + primary view.** Local SQLite (`applications.db`,
  git-ignored) via `applicationbot/tracker.py`, plus an editable **Track tab** in the web
  UI (decision 024). The autonomous runner (Apply stage) will write rows to it.
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

### Discover stage (decision 026) — the just-built focus

- [ ] **Watch the testing-mode loop headed, on the real résumé** — run
      `python -m applicationbot.pipeline --apply-first` (Claude judge on, headed browser) and
      eyeball one job go discover → tailor → fill live. So far verified headless + rules-tailor;
      confirm the Claude-judged pick + Claude-tailor + visible fill all behave.
- [ ] **Autonomous runner over ALL qualified matches** — loop the testing-mode core across the
      ranked list (not just the top match), dry-run by default, global kill switch, blockers as
      periodic updates not prompts. Builds directly on `pipeline.run_testing_mode`.
- [~] **Surface Discover in the web UI** — **done (first cut):** a **"Discover" tab** with a
      one-click **"Find & fill one application (dry-run)"** button that runs the whole
      testing-mode loop in a background thread, streams step-by-step progress (incl. a Claude
      judged-N/M bar), shows the single chosen match (fit / why / missing), and a **Finish —
      close browser** button (web-friendly review hold, replacing the terminal pause). Never
      submits; records a `dry-run` Track row. *Remaining:* edit `discovery.yaml` boards/gates
      from the tab (still hand-edited), and a browse-all-ranked-matches view.
- [ ] **Aggregator full-JD** — Adzuna is snippet-only; either fetch the `redirect_url` page for
      full text (ToS-permitting) or accept snippet-degraded tailoring for aggregator hits, and
      add a free-key setup path (env or the Discover tab).
- [ ] **More sources behind the interface** (as needed): USAJobs (federal, full JD, free key),
      remote feeds (Remotive/Arbeitnow — honor poll/attribution terms). Optional Workday.
- [ ] **De-dupe against the tracker** — skip postings already discovered/applied (by source URL)
      so re-runs don't re-surface the same jobs.

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
- [~] **Greenhouse Playwright adapter, dry-run** — `applicationbot/apply.py`: a fully-tested
      `AnswerResolver` (label → answer from résumé + apply profile + answer bank, or None =
      logged exception) plus a defensive Playwright driver. Dry-run = **headed + slow-mo so
      you watch it fill live**, uploads the PDF, screenshots, **pauses for review, never
      submits** (Guideline #3). Run: `playwright install chromium` (once), then
      `python -m applicationbot.apply <greenhouse-url> --pdf tailored.pdf`.
      *Verified live end-to-end against a real Greenhouse form (Chromium runs here) — 15/15
      fields filled, 0 errors, never submits.* Remaining: Lever/Ashby adapters.
- [x] **Dynamic per-question iteration** — the driver now fills **every** field, not a fixed
      list: text inputs, native `<select>`, react-select comboboxes, and radio groups. For
      each it derives the question label, asks the resolver, and fills by control type;
      required fields it can't fill are reported by name (`— REQUIRED, not filled`). Résumé
      upload now goes through Greenhouse's own "Attach" button via the file chooser (fixes the
      site-thrown `Cannot read properties of undefined (reading 'uploadFile')`). *Logic +
      resolver verified against the live field labels; combobox/radio DOM handling still needs
      one live-run pass to tune selectors.*
- [ ] **End-to-end wiring** — one command: tailor → export PDF → dry-run apply, so a job
      URL goes straight to a watch-it-fill run.
- [ ] Claude drafts answers for unresolved free-text questions → saved to the answer bank.
- [ ] **Screening-question answering** — Claude (subscription) drafts truthful answers from
      the apply profile + resume + JD; save them to the answer bank so it's asked once.
- [ ] **Autonomous runner + tracking** — a queue/loop that tailors → fills → (when armed)
      submits, records each application (Track stage fields), and surfaces blockers
      (CAPTCHA/login/unanswerable) as periodic updates, not prompts. Global kill switch.
      *(Tracking store is now built — decision 024 — the runner just calls
      `tracker.add_application(...)`; the loop/kill-switch is what remains.)*
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
- [~] Design the **user profile + filter** schema (Configure stage) — minimal seed built
      (`filters.py` `DiscoveryFilters`: boards + coarse gates + matcher knobs, decision 026);
      the full Configure schema (multi-role targets, richer preferences) is still to design.
- [x] Design the **tracked application record** schema (Track stage) — decision 024.
- [x] Prototype the scraper (Discover) against one job board / career page; emit the same
      JD shape the fixtures use — **done, decision 026** (public ATS APIs, not scraping;
      Greenhouse/Lever/Ashby + Adzuna behind a pluggable source interface).
- [x] Stand up the tracking store and write a record end-to-end in `dry_run` — SQLite
      store + editable Track tab (decision 024).
- [x] **Apply dry-runs auto-record to the tracker** — `run_apply` writes a `dry-run` row
      (role/company from the page title, portal from `detect_ats`, source URL, résumé path),
      upserted by source URL, never clobbering user edits; `--no-record` opts out
      (decision 024 update). **Next Track work:** optional one-way CSV/Sheets export.

## Later

- [ ] Auto-fill + submit flow with the `dry_run` default and global kill switch.
- [ ] Per-site adapters for common application portals.
- [ ] Dashboard / status view over tracked applications (see UI Design Principles).
- [ ] Rate limiting and site-terms compliance for the scraper.
- [ ] Cover-letter generation.
- [ ] Onboarding flow for a freshly-cloned repo (get a new user configured quickly).

---

## Recently added (this session, latest first)

- 2026-07-05 — **Fixed autofill failing on embedded (iframe) ATS forms.** Root cause of "fields
  visible on screen but nothing filled": many ATS forms render inside an **iframe** — e.g.
  Greenhouse's `job_app` embed on a company's own careers site (stripe.com → the 61-field form
  lives in `job-boards.greenhouse.io/embed/job_app`). The driver only queried the main frame, so
  it saw ~1 field while the user saw the full form. Made the driver **iframe-aware**:
  `_find_form_frame()` picks the frame that actually holds the fields (skipping recaptcha/analytics
  chrome), `_open_application_form()` now returns that frame + the ATS **re-derived from the frame
  URL** (a Greenhouse form on stripe.com was mis-detected as `generic`), and all fill helpers
  (`_upload_resume`/`_fill_all_fields`/`_fill_radio_groups`/`_flag_missing_required`/native-autofill)
  run against that frame. **Verified:** the exact failing pick (Stripe FDE Privy) went **0 → 9
  fields filled, 0 errors** (First/Last/Email/Phone/Country/Location/Gender), with company-specific
  + EEO questions correctly surfaced as needs-attention; **no regression** on the non-iframe Ashby
  form (still 5/5). *Follow-ups surfaced (separate, smaller):* a few Greenhouse dropdowns don't
  option-match the resolver value (e.g. country "United States"), and work-auth questions get
  mislabeled with the location value — resolver refinements, not the frame bug.

- 2026-07-05 — **Fixed dry-run filling nothing but the résumé.** Two bugs: (1) the driver
  filled before the ATS form's JS-rendered fields existed — added `_open_application_form()`
  which reveals the form (clicks Apply if needed) and **waits for a real application field to
  be visible** before filling, and on timeout skips with an actionable red banner instead of a
  silent no-op; (2) the fill/radio/required scans were hardcoded to `form …`, but **Ashby
  renders its fields outside any `<form>`** (0 form elements), so they matched nothing — added
  `_scope_prefix()` (form-scoped when a `<form>` exists → Greenhouse/Lever unchanged; page-wide
  otherwise, excluding nav/header/footer/search chrome). **Verified** live on a real Ashby form:
  1 field → **5** (Résumé, Name, Email, Phone, LinkedIn), 2 open-ended questions correctly
  surfaced as needs-attention, 0 errors, `submitted:False`.

- 2026-07-05 — **Discover "Run test" button in the web UI.** New **Discover** tab with a single
  **"Find & fill one application (dry-run)"** button: a background worker runs discover → match →
  pick the **one** best match → tailor → PDF → headed dry-run apply, streaming step progress
  (with a Claude judged-N/M bar and elapsed time) to the page via `GET /test-run/status`. When
  filling finishes it shows the chosen posting (fit/why/missing) and a **Finish — close browser**
  button; the browser stays open for review until clicked (`POST /test-run/close`) — a
  web-friendly replacement for the CLI's terminal pause (new `hold`/`on_filled` params on
  `run_apply`; `status_cb`/`hold`/`on_filled` on `run_testing_mode`; `on_progress` on
  `match`/`discover_and_match`). Never submits (Guideline #3); records a `dry-run` Track row.
  **Verified:** web.py imports, served page JS `node --check`-clean, `/`, `/test-run`,
  `/test-run/status` over HTTP, and the full worker path (status_cb + on_filled + hold release +
  returned report, `submitted:False`) headless. Also fixed the **Profile page crash**:
  `list_resumes()` listed the new `profile/discovery.yaml`, which failed to load as a `Resume`
  (no `contact`) — now excluded alongside `application_profile.yaml`.

- 2026-07-04 — **Discover stage: qualification-driven pipeline (decision 026).** Researched the
  2026 job-discovery landscape (verified against official docs) and built Stage 2. **Sources**
  (`discovery.py`) behind one pluggable interface: public no-auth **Greenhouse/Lever/Ashby**
  board APIs (full JD, no scraping — the same ATSs Apply fills, so a hit flows straight to
  tailor→apply) + optional **Adzuna** aggregator (self-skips without a free key). `Posting`
  normalizes across sources and emits the **exact fixture JD shape**, so Tailor/Apply are
  unchanged. **Matching** (`matching.py` + `relevance.qualification_score`): hybrid — free
  keyword pre-filter ranks/prunes, then Claude judges the top-N for true fit and names missing
  requirements (grounded, invents nothing). **Filters** (`filters.py`, git-ignored
  `profile/discovery.yaml`, seeded from `examples/discovery.example.yaml`): target boards +
  coarse gates (remote/salary/title) + matcher knobs; **aggregator keywords derived from the
  profile**, not company lists — qualification over company, per the user. **Runner**
  (`pipeline.py`): `python -m applicationbot.pipeline` lists ranked matches; `--apply-first` =
  **testing mode** (top match → tailor → PDF → headed **dry-run** apply you watch, never
  submits, records a `dry-run` tracker row). Zero new deps (stdlib `urllib` + certifi if
  present). **Verified live:** 618 postings across Stripe/cin7/Ramp, 0 errors, JD round-trips
  through the existing loader; keyword filter 618→143; Claude judge discriminates (SWE 82/100
  but flags missing-degree; sales AE 4/100); full testing-mode loop ran headless end-to-end
  (`submitted:False`, tracker row #1). Remaining: autonomous runner over all matches, a
  Discover web tab, aggregator full-JD + key setup, more sources, tracker de-dupe.

- 2026-07-04 — **Apply dry-runs auto-record to the tracker (decision 024 update).** `run_apply`
  now writes a `dry-run` row after filling, so applications land in the Track tab without manual
  entry: new `apply._record_dry_run()` derives (role, company) from the posting's page title
  (`_title_role_company`), portal from `detect_ats`, plus source URL + uploaded résumé path.
  **Upserted by source URL** (new `tracker.find_by_source_url`) — a re-run updates the existing
  row instead of duplicating it, refreshing only runner-owned fields (résumé path / portal /
  method; role/company only-if-empty) and **never clobbering** the user's `status` / `notes` /
  `pay`. Best-effort: a tracker error goes to `report.errors`, never breaks the fill. On by
  default; `--no-record` (CLI) / `record=False` (`run_apply`) opts out. **Verified:** title parse
  3/3, insert/upsert/no-clobber/fill-if-empty against a temp DB, and the full path through the
  real `run_apply` against a live browser page (title → role "Staff Backend Engineer" / company
  "Wayfair"; one row; re-run updated the same row). No real `applications.db` touched.
- 2026-07-04 — **Tailoring speed: 3 speed/quality tiers, thinking off by default (decision 025).**
  Tailoring took ~2 min. Benchmarked the real code path and isolated the cause to **extended
  thinking** (on by default in Claude Code) — with it on the model burns 10–21k output tokens
  reasoning before writing the ~3k-token résumé JSON; output generation is the wall-clock cost.
  Controlled A/B (same Opus, only thinking toggled): **113.8s → 39.5s**. Cheaper models were
  *worse* (they think more: Sonnet 180s, Haiku 138s); stripping agent context didn't help and
  broke prompt caching. Fix: `QUALITY_TIERS` in `backends.py` — `fast` (Sonnet, no-think, ~30s),
  `balanced` (Opus, no-think, ~40s, **new default**), `max` (Opus + thinking, ~114s = old
  behaviour). Thinking toggled via `MAX_THINKING_TOKENS=0` in the CLI subprocess env
  (`run_claude_cli(think=...)`, defaults True so the answer-bank path is unchanged); threaded
  `select_backend(name, quality)` → `tailor_resume(..., quality=)`. Surfaced as a **Quality**
  dropdown in the web UI (labels model + time estimate; the in-progress status names the expected
  wait) and a `--quality` CLI flag. **Verified end-to-end:** real CLI run at the default = 35.8s,
  valid `TailoredResume`, factually-grounded output with correct relevance notes; all modules
  import; 6-config benchmark table in DECISIONS.md #025.
- 2026-07-04 — **Track stage: local SQLite store + editable Track tab (decision 024).** After
  researching options (Sheets / Airtable / Notion / dedicated trackers), chose **local SQLite**
  as the system of record — dedicated trackers (Teal/Huntr/Simplify) have **no personal write
  API** (extension-only or recruiter-only), and a cloud store would force per-user accounts +
  ship PII off-machine. New `applicationbot/tracker.py` (stdlib `sqlite3`, zero deps): one
  `applications` table matching the fixed field set, `STATUSES` lifecycle, WAL mode (runner
  writes while UI reads), auto date-stamp on `applied`, status validation, search/filter/counts,
  and a `python -m applicationbot.tracker add|list|counts|delete` CLI. DB `applications.db` at
  repo root, **git-ignored** (added explicit `.gitignore` line). New **"Track" tab** in the web
  UI (Review | Profile | Track): every application in a scrollable table with **inline edit of
  any cell** (auto-saves per cell), a per-row **status dropdown**, clickable **status-count pills
  that double as filters**, free-text search, add, and delete — endpoints `GET /track`,
  `POST /track/{add,update,delete}`. **Verified:** store CRUD + validation + auto-stamp + search
  (temp DB); all `/track` endpoints over real HTTP; rendered page JS `node --check`-clean; and
  the full tab driven **live in headless Chromium** (add → inline edit "Saved ✓" → status change
  updates count pills → reload persists → filter → delete), **zero console errors**. The
  autonomous runner will just call `tracker.add_application(...)` to record `dry-run` rows.
- 2026-07-04 — **Tailoring quality + per-entry "why" rationale (decision 023).** (1) Bullets must
  now be concrete about the actual work (feature built / bug fixed / system migrated + tech +
  outcome; no "worked on") and (3) **quantify where the résumé factually supports it** — a strong
  preference with an explicit no-fabrication guard (pushed back on forcing a number on every bullet,
  which would induce made-up metrics). Prompt-only, so it affects the `claude-code` engine. (2) New
  optional `tailor_note` on Experience/Project (tailored-only, never printed): Claude writes a "why
  kept / how tailored" sentence per entry, the rules engine writes a deterministic one, the renderer
  emits it as `data-why`, and the Review pane has a **click-an-entry → side panel** showing the
  rationale. Verified live (panel shows entry title + why on click; markdown/PDF carry no leak).
- 2026-07-04 — **Apply profile: structured location + start-date inputs (decision 022).** Location
  is now Country dropdown + US-State dropdown + City text; Earliest start date is a preset dropdown
  (Immediately / 2 weeks' notice / 1 month / Specific date…) that reveals a native date picker for a
  specific date. UI-only: composes/parses to the resolver's existing stored formats (`location` =
  "City, ST", `country` = name, `earliest_start_date` = phrase or ISO date) — `ApplicationProfile`
  unchanged. Verified live (parse "Edison, NJ" → US/NJ/Edison; save → "San Francisco, CA" + ISO date).
- 2026-07-04 — **Consistent waiting/status feedback (decision 021, UI Principle #5).** Every async
  action now uses one shared pattern: the trigger button disables + shows a spinner and a specific
  working label ("Tailoring…", "Generating PDF…", "Saving…"), an in-place spinner + status message
  appears, long waits (the Claude tailoring call) show **elapsed seconds** so it never looks frozen,
  and each ends in a definite state (result / "Saved ✓" / inline actionable error). PDF export —
  which previously showed **nothing** and errored via `alert()` — now shows progress and an inline
  `#pdf-msg` error. Shared helpers `btnBusy`/`btnDone`/`busyInto` + one `.spin` CSS keyframe; no
  per-feature variants. Also: `tailor_resume` now appends a note when the length budget **drops
  entries** ("Omitted N experience entries to fit 1 page — increase Length…") so truncation is
  visible. **Root-caused the "tailoring ignores my new experience" report**: it was the
  `list_resumes()` bug (fixed in decision 020) — the résumé dropdown defaulted to
  `application_profile.yaml`, so edits/tailoring pointed at the wrong file. Verified end-to-end
  (headless Chromium): add experience → save → tailor now includes it; spinners/timer/errors work.
- 2026-07-04 — **Unified Profile screen (decision 020).** Merged the "Résumé data" and "Apply
  profile" tabs into one **Profile** tab so everything about you is edited in one place (tabs
  are now Review | Profile). Layout, top-to-bottom: Applicant details (unchanged) → Experience /
  Activities / Projects / Education / Skills → Résumé header & summary → Screening answers →
  Autofill accounts → Native logins → LinkedIn import, with a sticky section-jump nav and a single
  **Save** that writes both files. Every list entry is now a **collapsible card** — collapsed
  shows a one-line summary, click to expand and granularly edit its fields; new entries open
  expanded. Bullets stay a "one per line" textarea (user's choice). Also fixed a latent bug:
  `list_resumes()` included `application_profile.yaml`, so the résumé dropdown defaulted to the
  apply-profile file (fails to load as a Resume) — now excluded. Verified live end-to-end in
  headless Chromium (collapse/expand, live summaries, dual-file Save round-trip); no console errors.
- 2026-07-04 — **Self-improving answer bank (decision 018).** Autofill now learns: open-ended
  questions ("describe your experience with X") with no banked answer are **drafted by Claude**
  (subscription CLI, grounded strictly in the résumé — no fabrication) and **cached**; new
  reusable questions we can't answer are **captured as blank pending entries** for the user to
  answer once; both feed future autofill. **Never cached:** company-specific questions ("why do
  you want to work here") and demographic/EEO (handled by structured fields). New files:
  `answer_bank.py` (classifiers + generation), `QA.generated` flag, `remember_answers` /
  `capture_questions`; runner persists after each run; CLI `--no-generate` / `--no-learn`; UI
  marks entries "✨ AI-drafted — review" / "○ Needs your answer". Reused `backends.run_claude_cli`.
  Classifiers + learning verified live (generation stubbed — no Claude CLI in sandbox).
- 2026-07-04 — **Dry-run picker: preselected vs random.** `fixtures/applications.txt` holds the
  app pool; `./scripts/apply-dry-run.sh` uses the first, `… random` picks one at random (bash
  `$RANDOM`), `… <url>` a specific one — to test filler flexibility across different forms.
- 2026-07-04 — **Location async-search fix + "Autofill accounts" dashboard panel.** (1) The
  Location field is a react-select **async geocoder search**, not a text box: "Edison, NJ"
  returns zero suggestions, so we were leaving uncommitted text. Now: type the value → if no
  options, retry with the city (first token) → pick the option best matching the full "City,
  ST" (US state abbreviations expanded, e.g. NJ→New Jersey) → **commit the selection**; never
  leave loose text (clear + report if nothing matches). Verified live: "Edison, NJ" → commits
  "Edison, New Jersey, United States" as the react-select chip. (2) Apply-profile tab now opens
  with an **"Autofill accounts"** panel showing each provider's status — MyGreenhouse
  Connected/Not-set-up (from stored creds), Lever/Ashby/Workday "No login needed — résumé-parse".
- 2026-07-04 — **Native-first autofill framework (decision 017).** Apply now tries the ATS's
  own autofill before ours: upload résumé → trigger native autofill → our resolver fills only
  the still-empty fields (`_fill_all_fields(only_empty=True)`, detecting current values incl.
  react-select `single-value`). `detect_ats()` routes greenhouse/lever/ashby/workday; the
  report tags each field `native` vs `resolver`. Resume-parse ATSs (Lever/Ashby parse-on-upload,
  Workday "Autofill with Resume" button) work with zero setup. **MyGreenhouse** auto-login via
  stored credentials (new git-ignored `greenhouse_email`/`greenhouse_password` profile fields +
  UI section) is implemented best-effort but **unverified against a real account**. Verified
  live on Greenhouse: native-prefilled fields detected+kept, our resolver fills the rest, 15/15,
  0 errors. Runner renamed `run_greenhouse`→`run_apply` (alias kept); CLI accepts any ATS URL.
- 2026-07-04 — **Apply driver VERIFIED end-to-end against the live Greenhouse form.** Ran real
  headless Chromium against `job-boards.greenhouse.io/censys/…`: all 15 fields fill, 0 errors,
  `submitted=False`. Fixed the real react-select bugs the live DOM exposed: (a) options are
  `.select__option` (role=option), but the page also carries ~250 always-present hidden
  `[role=option]` phone-country entries — scoped selection to `.select__option:visible` so it
  no longer grabbed the wrong listbox; (b) open the menu via the `.select__control` container;
  (c) long async lists (location/country) accept the top suggestion (guarded to >3 options so
  Yes/No never mis-picks); (d) added short per-action timeouts (4–5s) so no blocked action
  stalls the default 30s. Confirmed live: Location, work-auth, sponsorship, citizenship
  confirmation, "Did AI complete this application?", Gender, Country all fill correctly.
  **Also fixed a dashboard-breaking bug**: an escaped-quote label in the profile editor
  produced `fld(""…")` in the served JS, a SyntaxError that killed all tabs — relabeled and
  verified the *Python-evaluated* served JS (not source) with node --check.
- 2026-07-04 — **react-select combobox fix + citizenship / AI-disclosure answers + a visible
  "done" signal.** (1) Dropdowns showing "Select…" are react-select comboboxes; the driver now
  opens them via the **control container** (the inner 1px input isn't clickable) and no longer
  skips combobox inputs on visibility — fixes work-auth / sponsorship / location not filling.
  (2) New `us_citizen` tri-state in the apply profile (+ UI toggle); the resolver answers "Are
  you a US citizen?" and "confirm you meet these requirements (citizen + in US)" from it —
  surfaces (never fabricates) when unset. (3) "Did AI complete this application?" answers **Yes**
  (honest; user-authorized for autonomous runs); generic "experience with AI" is NOT treated as
  disclosure. (4) On finish, a green **in-browser banner** ("✓ finished filling — DRY RUN,
  nothing submitted") + the report **summary prints to the terminal before the review pause**.
  (5) New `--debug` flag (on in `apply-dry-run.sh`) dumps every control's tag/type/role/label/
  visibility to diagnose the live DOM.
- 2026-07-04 — **Country + "How did you hear about this job?" now auto-fill.** Added profile
  fields `country` (default "United States") and `how_heard` (default "I found this role
  through an online job search.", reflecting our web-search discovery); both editable in the
  Apply-profile tab. The same question can be a text box or a dropdown per company, so
  dropdown/combobox fills now try the answer, then a ranked list of option hints
  (`option_hints()`: job board → online → search → company website → … → other) and select
  the actually-matching option. Verified: text → sentence verbatim; dropdowns pick a sensible
  honest option (e.g. Company Website / Job Board / Google Search); country dropdown → United
  States.
- 2026-07-04 — **Greenhouse adapter now fills every field, not a fixed list.** Was only
  filling 7 hardcoded standard fields → work-authorization, sponsorship, gender, race, and
  "How did you hear about this job?" were never touched. Now iterates all controls (text /
  `<select>` / react-select combobox / radio group), derives each question label, resolves,
  and fills by type; unfilled required fields are reported by name. Résumé upload switched to
  Greenhouse's "Attach" button + file chooser (fixes the site's `uploadFile of undefined`
  error). Resolver re-verified against the live field labels.
- 2026-07-03 — **Agent bus for parallel Cursor ↔ Claude VS Code work** (decision 014):
  git-ignored `.agent-bus/`, `applicationbot/agent_bus.py` CLI (post/read/ack/claim/watch),
  Cursor hooks (sessionStart + stop), and [docs/AGENT_COLLAB.md](docs/AGENT_COLLAB.md).
  Run `python -m applicationbot.agent_bus watch --agent <cursor|claude>` in a side terminal
  for canary alerts without waiting for prompts to finish.

- 2026-07-04 — Added `scripts/apply-dry-run.sh` — one idempotent script that sets up ALL
  deps for the Apply test (venv, pip deps, `playwright install chromium`), tailors a résumé
  to a fixture job, exports a PDF, and launches the visible dry-run fill. Defaults to a
  Greenhouse fixture URL; takes `[url] [resume.yaml]` overrides.
- 2026-07-03 — **Greenhouse dry-run adapter** (`applicationbot/apply.py`): a tested pure
  `AnswerResolver` (maps a form field's label → value from résumé contact + apply profile +
  answer bank, None if it can't answer) + a defensive Playwright driver that fills the
  standard fields, uploads the PDF, screenshots, and **pauses for review without submitting**
  (headed + slow-mo to watch live). CLI: `python -m applicationbot.apply URL --pdf X.pdf`.
  Resolver fully verified; browser driver awaits live run (`playwright install chromium`;
  can't launch Chromium in the sandbox). Added `playwright` to requirements.
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
- ~~Scraping strategy~~ — **resolved (decision 026):** no scraping; public ATS APIs
  (Greenhouse/Lever/Ashby) + Adzuna aggregator behind a pluggable source interface,
  qualification-driven matching.
- Resume-tailoring method (template + rules, LLM-based rewrite, or hybrid).
- Application-submission approach (headless browser form automation, per-site adapters).
- Storage for profile, postings, resumes, and application history (files vs. database).
- Config format for the user profile + filters — **partially resolved:** discovery filters
  built (decision 026, `profile/discovery.yaml`); full Configure-stage profile schema still open.
- How the `dry_run` / armed state and global kill switch are represented and toggled.

Record each decision in [DECISIONS.md](DECISIONS.md) once the user chooses.

---

## Recently completed

- 2026-07-04 — Added a **structural repo map** for faster agent orientation
  (`applicationbot/repo_map.py`, `python -m applicationbot.repo_map`) — decision 019.
  Parses every first-party `.py` with stdlib `ast` (zero deps), emits a compact markdown/
  `--json` map (per file: docstring, imports, constants, classes/functions with signatures
  + line numbers) plus a reverse-dependency graph. Generated on demand; `.repo-map.*` is
  git-ignored. Chose this over a vector database: at ~4k lines, exact grep is already
  instant and an embedding index would go stale on every edit. Revisit a local vector DB
  (sqlite-vec + Voyage) only past ~30–50k lines.
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

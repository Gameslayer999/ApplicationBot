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
- **Full-system audit (2026-07-06) folded into this queue.** Four deep-dives (autofill,
  discovery/pipeline, UI/tracking, tailoring/cloneability) mapped every gap against the
  final goal. Headline blockers: **no real submit path exists** (`apply.py` hardcodes
  `submitted=False`), no autonomous multi-application runner, account-gated portals
  (Workday ≈32% / iCIMS ≈10% of US enterprise postings) unsupported, no Claude
  usage-cap resilience mid-run, and fresh-clone onboarding is missing. Heavy engine
  work is queued at the top of **Now**; UI/UX items are delegated to parallel agents
  (see **Next**).

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

### Confirm the SmartRecruiters fix from the user's own network (decision 076) — BLOCKED ON USER

The nav work is verified; **the SmartRecruiters posting that prompted it is not**. Every live
attempt from the build environment is 403'd by DataDome, which named *this machine's* cloud egress
IP — so the block may be about where the build runs, not about the user's home network.

- [ ] **USER:** re-drive the reported posting from your own machine and report which happens:
  ```
  python -m applicationbot.apply "https://jobs.smartrecruiters.com/Consultadd4/87644936" \
      --pdf "<your résumé>.pdf" --resume profile/resume.yaml --headless --no-pause --no-record --dry-run
  ```
  - **Fields fill** → causes 1+2 were the whole story; nothing further to do.
  - **"blocked automated access"** → SmartRecruiters walls the user too. That is the site refusing
    us, and the honest answer is to apply there by hand — **do not** build evasion (Guideline #4).
    Consider instead **dropping SmartRecruiters from discovery** so the pipeline stops queueing
    postings it can never submit (it contributed ~298 of the 074 unlock).
  - **"form did not load"** (no wall) → a *fourth* cause past the `oneclick-ui` page; arm
    `nav_agentic: true` and let the learner take it.

### LinkedIn job alerts as a discovery source (decision 072) — BLOCKED ON USER

Approach approved 2026-07-15; no code written yet. Ingest is by **email forwarding**, not by
linking the personal Gmail: a filter on `jobalerts-noreply@linkedin.com` forwards to the
already-linked bot inbox (the address linked in `profile/mailbox.yaml`). **No `mailbox.py` changes
needed** — the second link slot originally scoped was dropped once forwarding was chosen.

- [ ] **USER:** add the forwarding address in personal Gmail (Settings → Forwarding and POP/IMAP).
      Gmail emails a confirmation code **to the bot inbox** — an agent can read it out with
      `fetch_verification(cfg, sender_contains="forwarding-noreply@google.com")`; no need to log in.
- [ ] **USER:** create the filter (From: `jobalerts-noreply@linkedin.com` → Forward to bot inbox).
      Click **Search** on the filter form first — 0 results means the alerts use a different sender
      and the filter should widen to `linkedin.com`. Tick *Also apply to matching conversations* to
      forward the existing backlog → gives the parser a real corpus immediately.
- [ ] **(A)** `LinkedInAlertSource`: read the bot inbox, parse alert cards → `Posting(ats=
      "linkedin_alert", extra={"snippet_only": True})`. Shape it on `AdzunaSource` (already
      snippet-only + redirect-linked + bridged). **Build against the real forwarded markup — do not
      guess the card structure.** Leads only: `auto_applyable=False` (the email links to
      `linkedin.com/comm/jobs/view/<id>` → redirects to LinkedIn, not an ATS; scraping the job page
      is robots-disallowed, Guideline #4).
- [ ] **(B)** Company→ATS-board resolver (**does not exist today** — grepped). Match a lead's
      company to its public Greenhouse/Lever/Ashby board → full JD + fillable apply URL → lead
      re-enters the pipeline auto-applyable. This is what makes the source worth having; A alone
      adds a human triage step and cuts against Guideline #0.
- [ ] **Reassess before building B:** does A's lead quality actually beat Adzuna's recall on the
      same filters? Adzuna already aggregates many boards and is already bridged. If not, stop at A
      or drop the source (see decision 072's open question).

Probe run 2026-07-15: bot inbox currently has **0** LinkedIn messages — the forward is required.

### Heaviest engine work (audit 2026-07-06) — build toward "fill AND submit any application, any site"

Ordered by value ÷ difficulty. Verification policy: **minimize live dry-runs** (token-heavy)
— verify with unit tests + local HTML form fixtures driven by Playwright (zero tokens, no
real postings), one consolidated live dry-run per milestone.

- [x] **Fillability gate** — done (2026-07-06, decision 035): `pipeline._is_fillable`
      drops workday/icims/`auto_applyable=False` postings BEFORE matching (no judge tokens
      on them), returns them as `PipelineResult.non_fillable`, surfaced in the CLI counts.
      *Deviation from the audit note:* not recorded as tracker rows yet (they're unjudged —
      would flood the Track tab); the runner can rank + record top manual candidates later.
- [x] **Real submit path + safety architecture** — done (2026-07-06, decision 035):
      `safety.py` (`SafetyGate`: `profile/safety.yaml` `armed`+`max_submissions_per_run`,
      `profile/KILL` kill file, checked immediately before every click);
      `apply._attempt_submit` (pre-submit gate on unresolved REQUIRED fields → `blocked`
      outcome, submit-button click, confirmation detection by text/URL, validation-rejection
      detection, form-gone ⇒ `unconfirmed`-but-submitted so we never double-submit);
      tracker rows become `applied`/method `auto` on real submission; `--dry-run` force-
      disarm on both CLIs. **Verified on local HTML fixtures** (13 tests incl. E2E armed
      submit + dry-run, zero tokens). *Remaining:* per-ATS validation of the submit click
      on live Lever/Ashby/SmartRecruiters DOMs (patterns cover "Submit application").
- [x] **Autonomous runner (first version)** — done (2026-07-06):
      `python -m applicationbot.runner` loops EVERY Claude-cleared match (never keyword-
      blind — closes the min_fit bypass), dry-run default, kill-file check between
      applications, `--max` per-run limit + submission cap stop, per-application failure
      isolation (a Claude-CLI failure stops the queue instead of burning it), outcomes
      recorded to the tracker via the existing `run_apply` path. 6 loop tests (injected
      apply_one, no browser). *Not yet run live end-to-end.*
- [x] **Claude usage-cap / rate-limit resilience** — done (2026-07-06, subagent):
      `backends.py` typed taxonomy (`ClaudeUnavailableError` ← `ClaudeAuthError` /
      `ClaudeRateLimitError`, classified from CLI stderr markers; all subclass RuntimeError
      so existing callers are untouched) + `runner.run_queue` pause-and-resume: a rate-limit
      hit waits 15 min (kill-file-abortable, ≤3 waits/run) and retries the SAME match; an
      auth failure stops the queue with the exact fix. 15 new tests (monkeypatched
      subprocess — no CLI calls). *Remaining:* per-run call accounting (nice-to-have).
- [x] **Multi-page form navigation** — done (2026-07-06): `apply._fill_all_pages` walks
      Next/Continue wizards page-by-page (anchored patterns can never match a submit
      control or "Continue with Google"), per-page fill + required-flagging (the
      required-label scan is now visible-only so hidden wizard steps don't pollute it),
      advance detection by form-signature change with frame re-location,
      validation-rejected advance ⇒ recorded stop, résumé upload retried on later pages
      (and flagged if NO page had an upload field). Pre-submit gate hardened: a live DOM
      scan of visible required labels with EMPTY controls blocks an armed submit even when
      the field was only captured as "no saved answer". Every dry-run also records
      `submit_probe` — the submit control it WOULD click — so the armed path's selectors
      get live-validated for free on ordinary dry-runs. Verified on 3-step wizard fixtures
      (dry-run walk, armed submit from the final page, rejected-advance block);
      single-page forms unchanged.
- [ ] **Account-gated portals (Workday first)** — the "any site" end goal: account
      creation + login + credential store (keyring, not plaintext YAML) +
      email-verification handling + the multi-page wizard. Largest single item
      (multi-week); starts once the submit path + runner are solid on the open ATSs.

### Adopted from the ai-job-search survey (2026-07-09) — user-approved queue

Ideas mined from [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search)
(17.7k-star Claude Code framework; strong on evaluation + document QA, has no Apply stage).
Ranked by value ÷ effort:

- [x] **ATS text-layer verification of generated PDFs** — done (2026-07-09, decision 043):
      `ats_check.verify_pdf` (pypdf) checks the text layer after every export — readable
      name/email/phone (catches latin-1 `?`-mangling), JD keyword coverage split *covered*
      vs *dropped-by-tailoring*. Wired into `pipeline.run_testing_mode` (notes reach the
      Discover tab) and `cli.py --out *.pdf`. 5 tests.
- [x] **Per-application archive** — done (2026-07-09, decision 043): `archive.py` writes
      `profile/applications/<company>-<role>-<urlhash>/` (posting.md + resume.pdf +
      report.json), dry-runs overwrite, real submissions freeze a `submitted-<date>/` copy;
      best-effort next to the tracker record in `run_apply`. 4 tests.
- [x] **Multi-dimension fit rubric in the judge** — done (2026-07-09, decision 043): judge
      returns skills/experience/seniority 0-100; `fit_score` computed in code via
      `FIT_WEIGHTS` (.45/.35/.20, renormalized when a dimension is absent); dimensions on
      `Match`, cache-safe, shown in CLI + Discover tab. Culture/career dimensions deferred
      until the Configure preference schema exists. 6 tests.
- [x] **Outcome → calibration loop** — done (2026-07-09, decision 043 + update):
      statuses + `fit_score` column (auto-migrated) + `calibration_report()` +
      `python -m applicationbot.tracker calibration`; Track tab gets a Fit column.
      Follow-ups done same day: `recommended_min_fit` → `pipeline.effective_min_fit`
      **auto-raises min_fit above a proven-dead band** on pipeline/runner/web runs (loud
      note, `--min-fit` wins, `calibrate_min_fit` filter toggle to disable), and the
      `follow_up_date` column + Track-tab "Follow up" column. 10 tests.

### Adopted from the ApplyPilot survey (2026-07-09) — user-approved queue

Ideas mined from [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)
(agentic auto-apply agent — Claude Code + Playwright MCP *drives the browser*; strong on
discovery breadth + Workday, weaker on determinism/safety). User approved #1–4 + CapSolver;
JobSpy deferred to Later; Workday hybrid pending design sign-off. Ranked value ÷ effort:

- [x] **JSON-LD → CSS → AI enrichment cascade** — done (2026-07-09, decision 047): new
      `enrich.py` (`fetch_full_jd`/`enrich_from_html` → `EnrichResult.tier`) — tier 1 parses
      `<script type=ld+json>` `JobPosting` structured data (description/apply-url/title/company/
      salary/remote, ≥50 chars), tier 2 a stdlib-`HTMLParser` description/apply-link scraper,
      tier 3 an **opt-in** Claude extractor (off by default, 30k-capped, schema-constrained).
      Built as a **reusable module** (not a one-off source): new `discovery.CareerSiteSource`
      consumes it, ATS auto-detected from the apply URL so a JSON-LD link routes into the right
      Apply adapter; `DiscoveryFilters.career_sites` wires it into config. `fetch_json`
      refactored onto a shared `fetch_text`; no new dependency. 8 tests, suite **156/156**;
      live-verified on a real Lever page (5,099-char JD), SPA degrades to empty.
      *Remaining:* surface the tier-count "% saved" line in the CLI/Discover tab; optionally
      use the cascade as the fallback inside `discovery._resolve_jd`.
- [~] **Cover-letter generation** — **deferred (user, 2026-07-09):** easy and reuses the
      tailoring machinery (structured facts + LLM, drift-checked) to draft a per-posting letter
      referencing the JD; unblocks forms that require one. Do after the other adoptions.
- [x] **`doctor` / env-validation command** — done (2026-07-09, decision 048): `python -m
      applicationbot.doctor` runs six read-only checks (Claude CLI signed in · Playwright
      Chromium installed · résumé loads · applicant profile loads · discovery has ≥1 source ·
      submit-safety state), prints ✓/✗/⚠ + a one-line actionable fix on failure, exit 0 iff
      every required check passes. Read-only (diagnoses, never edits). 6 tests; live green 6/6.
- [x] **`--continuous` polling mode** — done (2026-07-09, decision 048): runner
      `--continuous [--interval MIN]` (default 30) loops discover→judge→apply via an injectable
      `continuous_loop`; stops on KILL file / Ctrl-C / fatal Claude sign-in; reuses the
      discovery cache unless `--fresh`; dry-run + safety gate unchanged. 2 tests.
- [x] **CapSolver CAPTCHA solving (user-directed, #7)** — done (2026-07-09, decision 049):
      new `captcha.py` — `apply._attempt_submit` calls a gated hook after `may_submit()`
      (armed-only ⇒ dry-run never solves). **Fenced** per Guideline #4 (user overrode my
      reject rec, for personal use): off by default (`captcha.enabled` in safety.yaml),
      per-site opt-in (`captcha.sites` allowlist), key from env `CAPSOLVER_API_KEY` (never
      YAML), every attempt logged; any unmet gate → blocked outcome with the fix. Detects
      reCAPTCHA-v2/hCaptcha/Turnstile, solves via CapSolver (urllib, no new dep), injects the
      token. `doctor` reports the state. 12 tests, suite **190/190**. *Remaining:* one live
      armed dry-run once a key is set + a site allowlisted (no repo test can spend on the real
      CapSolver path). ApplyPilot's README claims CapSolver but its code has none — built fresh.
- [~] **Workday agentic-→-deterministic hybrid (#5 — Option C, M1 COMPLETE; M2/M3 next)** — decision 050.
      Deterministic adapter on Workday's stable `data-automation-id` selectors first; an agentic
      Claude-Code + Playwright-MCP worker (same Chrome via CDP, `stream-json`) handles ONLY pages
      the adapter doesn't recognize and **emits a recipe delta** (from its `browser_type`/
      `browser_select_option` tool calls) we persist, so agentic use trends to 0. Final submit
      stays behind the Python `SafetyGate` (NOT a prompt instruction, unlike ApplyPilot). Cracks
      the largest open blocker (Workday ≈32%). Settled: bot-owned email for account creation but
      **all tenant passwords stored** so the user can log in later; **shared committed** recipe
      library (selectors only, no PII); custom questions reuse the existing `AnswerResolver`; page
      identity = hash of the `data-automation-id` set. **M1 = deterministic login + standard
      fields, dry-run only** (agentic fallback = M2, armed submit = M3). Brick status:
      - [x] **1. Credential store** — `credentials.py`: per-tenant Workday passwords in the OS
            keychain via **keyring** (new dep), never YAML; git-ignored `profile/workday_accounts.json`
            index (tenant→email) for listing; CLI `list|get|delete`. 6 tests + live keychain round-trip.
      - [x] **2. Adapter field-fill core** — `workday.py` `fill_standard_fields` maps stable ids
            (legalName/city/email/phone) profile-first→résumé, empties dropped, `source="workday"`;
            handles wrapped + direct inputs; dry-run. Verified headless on
            `fixtures/apply_forms/workday_myinfo.html` (3 tests).
      - [x] **3. Wizard navigation + custom dropdowns** — `fill_wizard` walks pages via the visible
            `pageFooterNextButton`, filling text + custom button/listbox dropdowns each page,
            stopping at Review (no Next) — NEVER clicks Submit; page identity = md5 of the visible
            `data-automation-id` set (advance detection + future recipe key). `fill_dropdowns`
            (`_fill_dropdown`: open → read visible options → match in code → click by index) covers
            country/state (abbrev→full-name) + EEO (gender/veteran/…). Fills are `:visible`-scoped
            (never touches hidden pages — also fixed a 3s-per-hidden-field timeout). Verified
            headless on `fixtures/apply_forms/workday_wizard.html` (3-page walk, Submit never
            clicked). 3 tests.
      - [x] **4. Account create / sign-in + email verification** — done (decision 053): `mailbox.py`
            IMAP reader (`extract_verification` link/code parser [pure]; `fetch_/wait_for_verification`
            over an injected connection; env `MAILBOX_IMAP_HOST/EMAIL/PASSWORD`, secrets) + `workday.py`
            `sign_in`/`create_account`/`generate_password`/`ensure_account` (stored⇒sign-in else
            create-on-**bot-email**⇒**persist immediately**⇒verify via mailbox; `:visible`-scoped).
            16 offline tests (fake IMAP + `workday_account.html`). **Live step flagged:** create→verify
            →login against a real tenant with `MAILBOX_*` set. Unwired until brick 5.
      - [x] **4b. Link-the-email surface** — done (decision 057): secure store (password → OS keychain,
            host/email/port → git-ignored `profile/mailbox.yaml`; `load_config` prefers the link then
            env) + `test_connection` (real IMAP, actionable) + a **Profile-tab "Bot email" panel**
            (`GET /mailbox`, `POST /mailbox/link|unlink`, tests-before-it-saves) + CLI
            `mailbox link|status|test|unlink` + a `doctor` check. 8 tests; endpoints driven live; served
            JS node-clean. This is the "place to link the email" the user asked for before brick 5.
      - [x] **5. Wire-in** — done (decision 059): `workday.apply_workday` orchestrates
            start_application (Apply → Apply Manually) → `ensure_account` → résumé upload →
            `fill_wizard`, **never submits**; `run_apply` routes `ats == "workday"` to it (non-Workday
            path byte-identical under `else`); `_is_fillable` allows Workday + the aggregator bridge
            marks resolved Workday `auto_applyable=True`, so Workday postings reach the matcher/adapter;
            tracker logs a `dry-run` row. Verified end-to-end on `workday_full.html` (job→Apply→create
            account→3-page wizard→Review, Submit NEVER clicked) + dispatch + fillability tests. Full
            suite **264/264**. **M1 complete.**

      **M1 done — remaining Workday work:**
      - [ ] **Live-verify M1 on a real tenant** — with a linked bot inbox (decision 057), drive one
            real Workday application headed, dry-run: confirm the Apply→Apply Manually labels, account
            create + email verification, and the standard `data-automation-id`s match a live tenant;
            tune any that differ. The one step no fixture can cover.
      - [x] **M2 — agentic fallback + recipe distillation** — **DONE** (decisions 061 + 063).
            *Part 1 (061):* `workday_recipes.py` shared committed PII-free library
            (`{signature: [{automation_id, control, question}]}` — selectors+labels only, answers
            re-resolved per user); `unrecognized_fields`; `replay_recipe` (deterministic, no Claude);
            `run_agent_fill` (Claude-Code + Playwright-MCP over CDP, **distills by DIFFING
            empty→filled** — no MCP-ref parsing) with the HARD-RULES prompt. *Part 2 (063):*
            `_resolve_unrecognized` wired per page into `fill_wizard`/`apply_workday` (replay → armed
            agentic fallback → persist); `run_apply` opens a **CDP endpoint** for armed-agentic Workday
            runs; `agentic_enabled` gates it via `workday_agentic` in safety.yaml (**off by default**;
            replay always on/free). 12 tests incl. learn-once→replay-no-agent + a live `run_apply` CDP
            drive. **Only flagged live step left:** a real tenant's custom page driven by the actual
            Claude-over-MCP worker (needs Claude signed in, npx, `workday_agentic: true`).
      - [x] **M3 — armed submit** — done (decision 064): `_attempt_workday_submit`, reached from
            `apply_workday` only when the gate is armed, on the Review page — required-field scan →
            `may_submit()` (armed + no KILL + under cap) → click `pageFooterSubmitButton` →
            confirmation/error/unconfirmed detection, reusing the decision-035 architecture. No gate /
            unarmed ⇒ dry-run unchanged. 5 tests (happy path, empty-required-blocks-pre-click, KILL,
            unarmed, full armed flow). **Workday M1+M2+M3 code-complete.**

      **Sole remaining Workday item — the flagged live run (needs you):**
      - [ ] **Live-verify on a real tenant** — with a linked bot inbox (decision 057) + `workday_agentic:
            true`: (1) an armed **dry-run** headed pass to confirm the real Apply/Apply-Manually labels,
            account create + email verify, standard `data-automation-id`s, and the M2 Claude-over-MCP
            agent on a real custom page; tune any that differ. (2) Then a real armed submit once you
            arm `profile/safety.yaml`. This is the one step no fixture can cover.

### Adopted from the AutoApply-AI survey (2026-07-09) — user-approved queue

Ideas mined from [Rayyan9477/AutoApply-AI](https://github.com/Rayyan9477/AutoApply-AI-Agentic-Browser-Automation-for-Job-Search)
(full-stack FastAPI+React+Redis platform on `browser-use`+Playwright; strong on orchestration
/UI, but its live submission path is still "active development" — we are *ahead* on Apply).
User approved #1/#3/#4; **rejected #2 (Exa AI semantic discovery)** — a paid API, and its
results overlap our existing `enrich.py` cascade. Skipped their FastAPI/React/Redis/Postgres/
Prometheus stack (heft, fights simplicity-first) and LiteLLM multi-provider fallback (conflicts
with Claude-only, decision 004; rate-limit pause/resume already covers resilience). Ranked
value ÷ effort:

- [x] **#1 Park & resume blocked applications — M1+M2 done (2026-07-09, decision 051).** Port of
      their `intervention.py` pattern *without Redis*: because our fill is deterministic
      (decision 040), a resolved application resumes by re-driving the same form on the same URL —
      no browser-state serialization, no worker rendezvous. **M1 (durable state + classification):**
      pure [parking.py](applicationbot/parking.py) `classify(report)→ParkReason` maps a stalled fill
      to a user-actionable `kind` (needs_answer / login / captcha / form_rejected / site_error) +
      UI deep-link target + `resumable` flag; [tracker.py](applicationbot/tracker.py) gains a
      `blocked` status + `blocked_kind`/`blocked_detail` columns (additive migration) +
      `parked_applications()`; [apply._record_run](applicationbot/apply.py) parks an armed-blocked
      (or required-unanswered) fill as a `blocked` row instead of a silent `dry-run`, and a resolved
      re-run clears it → `applied`. **M2 (surface + resume):** `GET /parked` + a Discover-tab
      **"Applications waiting on you"** panel of Resolve cards that deep-link to the fix (button →
      Profile "Needs your answer" for needs_answer; instruction for login/captcha);
      `runner._report_parked` names parked apps after each cycle; a **"Re-apply (dry-run)"** button
      (`POST /parked/reapply` → `_reapply_worker`) re-drives the deterministic fill on the same URL
      with the stored PDF + a fresh resolver, reusing the test-run progress panel — **always
      dry-run** (the armed runner stays the only submit path, Guideline #3). 21 tests, suite
      **209/209**; drove the live HTTP server + full park→resolve→resume cycle end-to-end. Closes
      **blocked-work routing**, **durable run state**, and the **Workday email-verification** surface.
      **M3 done (2026-07-09, decision 058):** a red **"Submit for real ▶"** button on each resumable
      card really submits THAT one application via a **per-click arm** (one-shot `SafetyGate`, cap 1,
      independent of `safety.yaml` but still `KILL`-halted + pre-submit-gated), behind a `confirm()`;
      the armed `/parked/reapply` branch requires a same-origin request (`_same_origin`) since a POST
      now fires an irreversible submit. 6 tests, suite **260/260**; drove KILL-halts-armed-gate live.
      **WON'T DO — credentials UI for the `login` card (investigated 2026-07-09):** no apply flow
      emits a `login`-classifiable block today (MyGreenhouse login failure is non-fatal → falls back
      to our autofill; Workday's account errors don't match the LOGIN markers), the open ATSs need no
      login, and the one account-gated portal (Workday) already stores per-tenant creds in the OS
      keychain via `credentials.py` + `mailbox.py`. A generic "type a password on a card" UI would
      serve a near-empty set and be the wrong (less secure) store. *Instead* → the greenhouse-password
      keyring migration below.
- [x] **#3 Deterministic multi-factor ATS pre-score — done (2026-07-09, decision 052).** New
      [ats_score.py](applicationbot/ats_score.py) `ats_prescore(resume, title, jd_text)` → zero-token
      0-100 from skills (matched-count saturated at 6) + experience (candidate career-span years ÷
      the JD's floor "N years" bar) + education (degree rank HS=1…PhD=5, candidate ÷ JD floor) +
      title-keyword overlap, weighted `.40/.30/.20/.10`, renormalized over the factors the JD actually
      states. [matching.keyword_rank](applicationbot/matching.py) computes it (reusing the keyword
      pass's matched count — no re-scan), stores `Match.ats_score`, and ranks survivors by it instead
      of raw overlap count (predictor path uses it as tiebreak). **Claude stays the final judge —
      unchanged**; this only reorders WHICH `top_n` get judged, fixing decision 046's crowd-out (the
      experience factor sinks over-bar senior roles cheaply). Dropped the surveyed required/preferred
      skill split (not extractable from raw JD text); cache-safe. 9 tests, suite **221/221**; live
      drive: a 7-yr résumé leads with a Full-Stack role (ats 87) over a higher-keyword Staff role
      (kw 6 vs 4, ats 86). **Follow-up done (decision 055):** `ats_score` now feeds the
      `fit_learning` Predictor as a third shrunk bucket (pre-score band), so the predictor
      *calibrates* the heuristic against real Claude verdicts (a misleading high pre-score gets
      tempered) — pre-053 history stays a no-op. **Surfacing done (decision 055 addendum):**
      `fit_learning.prescore_calibration/prescore_insight` → `/fit-insights` → a Discover-tab mini
      bar chart ("how well the quick pre-score predicts fit": band → mean actual fit) + a one-line
      read of the calibration direction; hidden until pre-score history exists. Suite **257/257**.
- [x] **#4 Discovery→apply funnel analytics — done (2026-07-09, decision 054).**
      `tracker.funnel_report()` counts applications reaching each stage of a shrinking funnel —
      Discovered ⊇ Filled ⊇ Applied ⊇ Responded (rejection included; no-response excluded) ⊇
      Interview ⊇ Offer — from each row's current status (nested sets → monotone), with the
      conversion from the previous stage. Served in `/track`, rendered as labeled bars above the
      Track table, plus a `tracker funnel` CLI. Read-only, no schema change. 4 tests, suite
      **241/241**; drove the report + CLI + live `/track` payload. **AutoApply-AI survey complete
      (#1/#3/#4 shipped, #2 rejected).**

### Discover stage (decision 026) — the just-built focus

- [ ] **Watch the testing-mode loop headed, on the real résumé** — run
      `python -m applicationbot.pipeline --apply-first` (Claude judge on, headed browser) and
      eyeball one job go discover → tailor → fill live. So far verified headless + rules-tailor;
      confirm the Claude-judged pick + Claude-tailor + visible fill all behave.
- [x] **Autonomous runner over ALL qualified matches** — done two ways: the CLI
      `runner.run_queue` (decision 035) loops every cleared match dry-run/armed, and the web
      **auto-apply loop** (decision 069) prepares each cleared match into a "Ready to apply"
      queue and submits one per Apply click. Both dry-run by default, kill-file/Stop halted.
      *Remaining live step:* one real web-loop run end-to-end (see below).
- [~] **Surface Discover in the web UI** — **done (first cut):** a **"Discover" tab** with a
      one-click **"Find & fill one application (dry-run)"** button that runs the whole
      testing-mode loop in a background thread, streams step-by-step progress (incl. a Claude
      judged-N/M bar), shows the single chosen match (fit / why / missing), and a **Finish —
      close browser** button (web-friendly review hold, replacing the terminal pause). Never
      submits; records a `dry-run` Track row. Now also has a **full Discovery-settings editor**
      (boards + all gates/knobs, editable from the tab — no more hand-editing the yaml).
      *Remaining:* a browse-all-ranked-matches view.
- [x] **Aggregator full-JD** — **done (decision 032):** the aggregator→ATS bridge resolves an
      Adzuna/Jooble redirect to its real ATS and, for all six fillable ATSs
      (GH/Lever/Ashby/SmartRecruiters/Recruitee/Workable), **re-fetches the full JD** (`_resolve_jd`)
      to replace the snippet. Free-key setup path wired into the Discover tab (clickable
      developer.adzuna.com link + `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` env-var / own-key option).
- [~] **More sources behind the interface** — **added (decision 030):** SmartRecruiters +
      Recruitee, two *distinct* ATS form systems (public no-auth APIs, full JD, direct apply URL),
      to exercise the Apply autofill on more layouts. **Rejected:** hiring.cafe (its search API is
      now Bearer-auth-gated — replaying its token would circumvent an access control, Guideline #4)
      and LinkedIn (no candidate API; scraping breaks ToS). *Follow-ups behind the same interface:*
      Workable (needs a working no-auth endpoint — the widget returned 0 jobs for every slug tried),
      The Muse (full JD but apply links go through a themuse.com hop), USAJobs (federal, full JD, but
      routes into non-autofillable gov portals — discovery/tracking only).
- [x] **De-dupe against the tracker** — done: discovery skips postings already in the tracker
      (`tracker.seen_source_urls()`, `DiscoveryFilters.skip_seen=True`), surfaced as "skipped N
      already in tracker" in the CLI + Discover tab.
- [x] **Self-tuning fit loop** — done (2026-07-09, decision 046): `fit_learning.py` learns from
      every judged posting to steer the free pre-filter toward past winners (so more clear
      `min_fit` each run) + a Discover-tab diagnosis with one-click filter fixes. See Recently
      added.

- [ ] **Run the customizer live via Claude** on `profile/resume.yaml` once logged in
      (`ant auth login`, no key needed) — confirm bullet-rewriting output is factual, the
      drift check stays clean, and the format matches. (`rules` path already verified.)
- [ ] Re-run the frontend/full-stack JD collector — it didn't land; we have 6 fixtures
      (backend + data/ML) in `fixtures/job_descriptions/`, want ~3 frontend too.
- [ ] Add a smoke test / tiny pytest for the non-API pieces (loading, parsing, render,
      `check_factual_drift`, `select_backend`).

## Next

### Discovery funnel — surfaced by decisions 073/074 (2026-07-15)

- [ ] **`max_resolve` is now the binding constraint on early-career discovery.** Measured:
      1,130 fillable candidates survive `_CURATED_ATS` (up from ~1,130 → ~2,183 after 074),
      but only **40** are resolved + judged per run. Raising it is the single highest-yield
      knob left; it is also the Claude-judge cost knob, so it needs a deliberate
      cost/benefit call from the user rather than a default bump. Consider surfacing the
      discarded count in the UI ("40 of 2,183 judged") — silence here currently reads as
      "we looked at everything", which UI Principle #5 calls a bug.
- [ ] **iCIMS is dropped (158 active postings) on a technicality.** `apply.detect_ats`
      recognizes iCIMS, but `discovery.detect_ats_from_url` does **not** — it returns
      `"other"`, so `_is_fillable` rejects those postings before Apply ever sees them.
      Resolve the JD (likely via the same `enrich` cascade that unlocked Workday) and check
      whether the generic autofill path can actually fill an iCIMS form before enabling.
- [ ] **Two divergent ATS detectors are a latent trap.** `discovery.detect_ats_from_url`
      (knows smartrecruiters/recruitee/workable, no iCIMS, returns `"other"`) and
      `apply.detect_ats` (knows iCIMS, not the other three, returns `"generic"`) drift
      independently — decision 074 had to reason about both to know where a posting lands.
      Consider one shared detector with an explicit fillable/resolvable capability map.
- [ ] **`ATS_SOURCES` is overloaded** — it is both the discovery-source registry *and* the
      fillability predicate (`pipeline._is_fillable`, `discovery.py`). Adding a
      discovery-only source to that dict would silently assert an apply adapter exists.

### Test-suite hygiene (observed 2026-07-15)

- [x] **`test_mailbox.py::test_load_config_needs_all_three` leaked a real secret when it
      failed** — fixed 2026-07-15 (decision 075). It was environment-dependent (`load_config`
      prefers a stored link over env, so it read the real `profile/mailbox.yaml`) *and* the
      failure printed the live app-password. Test now pins `backend=_FakeKeyring(),
      path=_link_path()`; `password`/`refresh_token`/`client_secret` are `repr=False` so no
      traceback, log, or diff can print them again. Suite **351/351 green**.
- [ ] **`test_lever_labels.py::test_lever_eeo_selects_get_clean_label_and_normalize` is
      flaky.** Failed once in 3 consecutive full-suite runs on an unmodified `apply.py`;
      passes in isolation and on re-run. Playwright timing under load is the likely cause.
      Needs a deterministic wait rather than a re-run until it is trusted.

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

### UI/UX & onboarding (audit 2026-07-06) — delegated to parallel agents (Cursor)

Posted to the agent bus 2026-07-06; independent of the engine work above.

- [ ] **First-run onboarding** — guided setup that creates `profile/resume.yaml`,
      `application_profile.yaml`, and `discovery.yaml`; a real PDF/LinkedIn→YAML resume
      import (the generator `profile/README.md` promises does not exist in code).
- [ ] **Never edit the committed example** — a fresh clone's Profile tab currently
      round-trips edits into `examples/sample_resume.yaml` (PII-into-git risk); create
      `profile/resume.yaml` instead.
- [ ] **Batch/queue UI** — browse all ranked matches, per-row "apply to this one",
      approve-then-apply; natural home for the arm toggle + STOP (kill-switch) button.
- [ ] **Blocked-work routing** — unanswered-required-question failures deep-link to the
      Profile "Needs your answer" card (UI Principle #2).
- [x] **CSRF/origin guard on state-changing POSTs + plaintext Greenhouse password — DONE
      (2026-07-09, decisions 060 + 062).** (060) MyGreenhouse password moved to the OS keychain —
      never in YAML, never in the `/profile` payload; write-only field + Disconnect + one-time
      auto-migration. (062) A single `do_POST` origin guard rejects **every** cross-origin POST (403
      before dispatch); `_same_origin` passes a missing/loopback Origin and otherwise matches the
      Origin host against the `Host` header (correct under `--host` LAN binds); GETs stay unguarded;
      the per-endpoint 058 check was folded in. 12 tests across the two; suite **280/280**; drove a
      real cross-origin POST → 403 with the handler never called.
- [x] **Track lifecycle** — done (2026-07-09, decision 043 + update): interview / offer /
      rejected / no-response statuses + `follow_up_date` column, live in the Track tab.
- [ ] **Durable run state** — a server restart currently orphans the headed browser
      mid-fill and loses the in-flight record.

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

- [ ] **JobSpy discovery breadth (ApplyPilot survey, deferred 2026-07-09)** — `python-jobspy`
      wraps Indeed / Glassdoor / ZipRecruiter / Google Jobs (and LinkedIn — which we REJECTED
      on ToS grounds, decision 030; keep it rejected). Revisit Indeed / Google-Jobs only, with
      a per-board ToS call, once the ApplyPilot #1–4 items land.
- [ ] Auto-fill + submit flow with the `dry_run` default and global kill switch.
- [ ] Per-site adapters for common application portals.
- [ ] Dashboard / status view over tracked applications (see UI Design Principles).
- [x] Rate limiting and site-terms compliance for the scraper — done (2026-07-06,
      subagent): per-host 0.5s pacing in `fetch_json`/`resolve_redirect`; retry ×3 with
      1s/3s backoff on 429/5xx/timeouts honoring `Retry-After` (capped 30s); 404s fail fast.
- [ ] Cover-letter generation **+ upload** (forms that require one are unfillable today).
- [ ] Onboarding flow for a freshly-cloned repo (get a new user configured quickly).
- [ ] Drift-check hardening (audit 2026-07-06): rewritten bullets/summary/projects are
      never validated against the base résumé — must gate an armed submit.
- [~] Discovery robustness (audit 2026-07-06) — **mostly done (2026-07-06, subagent):**
      `canonical_url` dedup (tracking-noise query params stripped, job-id params kept)
      applied in `discover()` + the tracker skip-seen comparison; opt-in staleness gate
      (`DiscoveryFilters.max_posting_age_days`, default off — missing dates pass, Lever
      ms-epoch dates handled). *Remaining:* structured-pay coverage so `min_salary`
      actually enforces.
- [~] PDF (audit 2026-07-06): embed a Unicode TTF (latin-1 `?`-mangling of non-Western
      names) — still open. ~~Enforce true page-fit by measured height~~ **done
      (decision 042):** `pdf.fit_to_pages` measures the rendered PDF and trims until it fits.
- [ ] Profile schema gaps (audit 2026-07-06): GPA/test scores, security clearance,
      structured street address, phone country code, salary min/max, references.
- [ ] Per-ATS fill validation for SmartRecruiters/Recruitee/Workable + unify the two
      divergent `detect_ats` implementations (discovery vs apply).

---

## Recently added (this session, latest first)

- 2026-07-14 — **Auto-apply loop: prepare-then-prompt mode (decision 069).** The autonomous
  looping mode the user asked for — "look for as many matches as possible, then get started on
  them one by one and prompt me as it needs me to start applying." Sits between the two runner
  modes: it prepares each cleared match as a background **dry-run** (tailor → PDF → headless
  fill) into a **"Ready to apply"** queue in the Discover tab, and each card's red **Apply ▶**
  submits just that one application (per-click armed one-shot, decision 058 — confirms first,
  KILL-halted, pre-submit gated). **Token-frugal by construction** (user's constraint): each
  search asks discovery for `only_new` postings, so no posting is ever re-judged and the loop
  reaches "caught up" and stops when nothing new remains, instead of re-searching into the void.
  A **Stop** button halts it. One browser, serialized: the loop owns the browser slot (prepare
  runs headless), user Apply clicks are enqueued and drained by the loop thread, and
  test-run/re-apply are refused while it runs. New pure `autoloop.py` (the ordering brain) +
  `web.py` glue (`_loop_worker`, `start/stop_loop`, `queue_submit`, `GET /loop/status`,
  `POST /loop/start|stop|apply`) + a Discover-tab panel. 11 tests (6 core + 5 real-worker-thread
  glue with fakes); JS node-clean; `/loop/*` driven live. **Flagged live step (needs you):** one
  real loop run (Claude signed in, boards configured) to watch discover → prepare → a real
  Apply ▶ submit — not run here to avoid spending Claude usage / a live submission uninvited.
  ([autoloop.py](applicationbot/autoloop.py), [web.py](applicationbot/web.py))

- 2026-07-14 — **Web UI revamp: left nav rail + dark mode (decision 068).** The Review-only
  tailoring sidebar was docked on the left of *every* tab (dead, confusing space on
  Discover/Profile/Track). Now the left column is a persistent app **nav rail** (Review ·
  Discover · Profile · Track, with icons + Claude-status badge + theme toggle); the
  tailoring controls moved into the Review view as a compact top control bar. All colors
  moved to CSS **tokens** with a full **light/dark** theme (`prefers-color-scheme` default +
  a persisted `data-theme` toggle; `color-scheme` set so native selects/date-pickers follow).
  Pure presentation — no server code, element IDs, or JS wiring changed (one `.controls
  .ctrl.hidden` rule added so the paste-a-posting toggle still hides). `web.py` INDEX_HTML
  only. Verified live (Playwright) across all four tabs in both themes, the fixture/paste
  toggle both ways, and a full `rules`-engine tailor→render→PDF flow; console clean;
  `test_web_csrf.py` green. **Follow-up done same session:** the Discover **fit-trend chart**
  was redrawn through CSS-class tokens (accent/muted/warn-line/grid/surface-ringed dots) so it
  re-themes live, plus a dataviz redesign — recessive 0/50/100 grid, translucent area under the
  headline series, swatch legend, per-point hover; the pre-score bars already themed. Both
  verified in light + dark. **Then the remaining inline-colored JS was migrated too** —
  screening-answer status pills/marks, account ✓/○ rows, connect messages: every JS hex →
  a semantic token (`--ok`/`--bad`/`--warn-line`/`--warn-strong`/`--muted` + a new `--ai`
  purple for AI-drafted/auto-from-profile). No raw hex left in the JS; computed colors verified
  per theme. ([web.py](applicationbot/web.py))

- 2026-07-14 — **Required unmapped DROPDOWNS/SELECTS get a weak-model choice — never block submit
  (decision 067 amendment).** Sibling to the free-text fix below: a required dropdown/select the
  resolver, semantic classify, and hints all miss still blocked submit (a combobox was captured "no
  saved answer"; a native `<select>` even *errored* in `_fill_select(None)`). New
  `answer_bank.choose_required_option` lets the weak model **choose the best-fitting OFFERED option**
  (never invents one), grounded in the résumé — and **refuses** demographic/EEO + fact-owning
  enumerated questions (clearance/GPA/scores), which stay captured for the user. Wired into the fill
  loop's capture branch: native selects included, unmapped dropdowns deferred to the two-pass batch in
  round 1, `choose_option` tried for required controls in round 2 (committed via the existing
  combobox/select paths, reported `source=option:claude`; `_selectable_options` keeps a "Select…"
  placeholder from ever being picked). Native selects also gained the combobox's value→option Claude
  fallback. 4 tests (`tests/test_required_dropdown.py`) + an end-to-end headless drive of the new
  committed `required_dropdowns.html`: two answerable required dropdowns fill, clearance + gender are
  refused/captured. ([answer_bank.py](applicationbot/answer_bank.py), [apply.py](applicationbot/apply.py))

- 2026-07-14 — **Required unmapped free-text fields get a weak-model draft — never block submit
  (decision 067).** The user's case: WHOOP's "Why are you interested in working at WHOOP?" is a
  single-line `<input type="text" required>`, which `answer_bank.is_open_ended` returns False for, so
  `freetext_answer` returned `None`, recorded "no saved answer", and blocked the armed submit. Two
  changes: (1) free-text answers now draft with a **weak/cheap model** (`answer_bank.DRAFT_MODEL =
  "haiku"`; `generate_answer` defaults to it) — the user's explicit ask + a token win; (2)
  `freetext_answer(required=…)` force-drafts any **required** field even when it isn't open-ended,
  gated by new `answer_bank.is_draftable_required`, which still **refuses** numeric-fact (salary/GPA)
  and demographic/EEO questions (never fabricate those — they stay parked for the user, Guideline #7).
  Per-field required-ness read live via new `_IS_REQUIRED_JS`/`_is_required` (element attr, or a
  label/card marked `*`/`✱`/`★`/"required"). 4 tests (`tests/test_required_draft.py`) + drove the real
  committed `lever_custom_cards.html` headless: the WHOOP required input now fills (`source=generated`)
  instead of being skipped. ([answer_bank.py](applicationbot/answer_bank.py),
  [apply.py](applicationbot/apply.py))

- 2026-07-14 — **Track tab: "Re-run ▶" button on dry-run rows.** Any application whose status is
  `dry-run` and that still has a `source_url` now shows a "Re-run ▶" action next to Delete. It
  re-drives the same deterministic fill on the same posting with the stored tailored PDF and never
  submits — reusing the parked re-apply flow (`start_reapply(arm=False)`, decisions 049/058) and the
  Discover tab's single shared run-progress panel + Finish button (switches to that tab so there's
  one consistent progress UI). No new backend. `applicationbot/web.py` only.

- 2026-07-13 — **Lever custom-question fields now fill; verified live on WHOOP (decision 066).**
  Fixes the WHOOP dry-run where the work-auth/visa/hybrid radios were empty, the EEO dropdowns
  errored, and "Why are you interested…?" got a generic answer. Root cause (from the run's
  `report.json` + a live fetch of the Lever DOM): Lever renders a card's question in a
  `<div class="application-label"><div class="text">` — not a `<label>`/`<legend>` — so `_LABEL_JS`
  fell back to the raw input name `cards[uuid][field0]` and `_GROUP_QUESTION_JS` returned `''`.
  Fixes: (1) both JS helpers read the enclosing `.application-question` card's `.application-label
  .text` (guarded so a radio OPTION's own "Yes"/"No" `<label>` is still used, and the EEO `<select>`
  wrapper's all-options text is skipped); strip the `✱` glyph. (2) `option_hints` normalizes
  veteran/disability EEO answers to each ATS's option wording (Greenhouse "I am not a protected
  veteran" → Lever "I am not a veteran"; negation-safe). (3) new `_check_radio` selects a radio
  through Lever's hCaptcha overlay (normal→forced→JS `.checked`; captcha untouched, dry-run never
  submits). **Live headed dry-run result: 15 filled, 0 errors, nothing submitted** — all radios +
  EEO correct, "Why WHOOP?" grounded on WHOOP's mission. Committed fixture
  `fixtures/apply_forms/lever_custom_cards.html` + `tests/test_lever_labels.py` (4 tests, incl.
  radio-check + option-label regression guards). The captcha the user saw fires only at submit
  (dry-run never reaches it) — it did **not** cause the empty fields.
  ([apply.py](applicationbot/apply.py))

- 2026-07-09 — **One-click Gmail connect via OAuth (decision 065).** Replaces the app-password
  friction (2FA + hunt-through-settings + hand-typed IMAP host/port) with **"Sign in with Google"**:
  `mailbox.connect_gmail(client_id, client_secret)` runs the loopback consent flow, tests before it
  saves, and stores the refresh token + client secret in the OS keychain (email/client_id/`auth:
  oauth` in git-ignored `profile/mailbox.yaml`). Reads move to the **Gmail REST API with the
  read-only scope** (`gmail.readonly`) — deliberately not IMAP-over-OAuth, which forces Google's full
  send/delete scope. `test_connection`/`fetch_verification` branch to `_gmail_*` for oauth; the IMAP/
  env path is unchanged. Web: Profile "Bot email" panel leads with **Connect Gmail** (app-password
  moved to an "Advanced" `<details>`), `POST /mailbox/gmail/connect` (CSRF-guarded, threaded,
  elapsed-time waiting state). CLI `connect-gmail`; doctor/status say "Gmail, read-only". New deps
  `google-auth`/`google-auth-oauthlib`. 9 new tests; full suite 298/298; JS node-clean; endpoints
  driven live. **Live step flagged:** the real Google consent needs the user's Google Cloud client +
  browser. **User one-time setup:** create a Google Cloud "Desktop app" OAuth client and set the
  project to "In production" (else refresh tokens expire weekly); paste client_id/secret once
  ([mailbox.py](applicationbot/mailbox.py), [web.py](applicationbot/web.py),
  [doctor.py](applicationbot/doctor.py)).

- 2026-07-09 — **Seen-openings ledger: a preview shows only NEW openings on a re-run
  (decision 056).** Fixes the "dry-run searches come back with the same openings every re-run"
  report. Root cause was twofold: the snapshot cache (037) returns the identical result within
  its window, and `skip_seen` drops only postings in the *tracker* — which a list/dry-run never
  writes to — so a preview re-surfaces the whole list forever. New `discovery_seen.py` +
  git-ignored `profile/discovery_seen.json` records the canonical URL of every posting a preview
  surfaces; `discover_and_match(only_new=True)` hides already-shown matches then records the
  survivors (both the live and cache-hit paths), layered on top of the cache (which still holds
  the full ranked result) and `skip_seen`. On by default for the CLI list path (`--all` shows
  everything, `--reset-seen` / `python -m applicationbot.discovery_seen clear` forgets) and the
  web testing worker (normal = new-only; "Re-search fresh" = show all). **Runner unaffected**
  (`only_new` defaults False — it relies on tracker `skip_seen`). Kept separate from the tracker
  so previewing never pollutes application history/calibration. 6 new tests; full suite 250/250
  ([discovery_seen.py](applicationbot/discovery_seen.py),
  [pipeline.py](applicationbot/pipeline.py), [web.py](applicationbot/web.py)).

- 2026-07-09 — **Empty-result dry runs offer an immediate "Re-search fresh".** When a test
  run finds no postings, or none that clear the fit cutoff, the test panel now shows a
  Re-search fresh button so the user can immediately kick off another live search — previously
  that button appeared only on cache-reuse runs, so right after a fresh run (`from_cache=False`)
  the only path was the main button, which reuses the just-populated cache and returns the same
  nothing. Backend flags both empty-result error branches with `can_research=True`; the frontend
  renders the button on `phase==="error" && can_research` (shares the `test-fresh` id/handler, so
  no double-button with the cache note) ([web.py](applicationbot/web.py)). No data-model change.

- 2026-07-09 — **Fit chart: Lifetime default + window toggle; cache-served dry runs labeled
  (decision 046 follow-up).** The fit trend now defaults to **Lifetime** and offers a Show:
  Lifetime / Last 30 / Last 10 window (client-side slice, no refetch); `/fit-insights` returns
  the full run history instead of the last 30 ([web.py](applicationbot/web.py),
  [fit_learning.py](applicationbot/fit_learning.py)). Diagnosed why the chart showed one point
  despite repeated dry runs: a cache hit ([pipeline.py:115](applicationbot/pipeline.py#L115))
  returns before `append()`/`record_run()`, so only fresh (`force_fresh`) runs train and chart.
  Per user ("Fresh trains, label the rest"), the test panel's cache-reuse note now states the run
  "added no point to the fit chart and taught the search nothing — Re-search fresh to judge live,
  add a chart point, and train." No data-model change.

- 2026-07-09 — **Discovery feedback loop: learn from past judgments to surface higher-fit
  postings (decision 046).** Fixes the recurring "can't find a posting above the fit threshold"
  block. Root cause found in the real data: a run judged only the top 10 keyword-ranked postings
  (`top_n`), and the keyword pre-filter floats verbose **senior** JDs to the top — exactly what
  an early-career résumé scores lowest on (experience dim avg 23) — so the judge's slots were
  spent on ~20-scoring roles while higher-fit early-career roles sat unjudged at rank 11–91.
  New [fit_learning.py](applicationbot/fit_learning.py): (1) **store** — every judged posting is
  appended to git-ignored `profile/fit_history.jsonl` (fit + per-dimension scores + detected
  level + board) after each live run; (2) **engine** — a shrinkage-blended `Predictor`
  (level/board bucket means, inactive below 5 rows) **re-ranks the free pre-filter by predicted
  fit** so the judge's `top_n` slots go to postings most like past winners — zero extra Claude
  tokens, no-op with thin history, final best-first ordering unchanged; (3) **diagnosis** —
  `analyze()` reports dimension means + weakest dimension, per-level/board fit segments,
  recurring missing = résumé gaps, and recommends auditable edits (narrow `experience_levels` to
  winning bands, lower `min_fit` to best-achievable **only when nothing cleared**, drop dead
  boards). Surfaced in the pipeline CLI and a **Discover-tab panel** ("What past runs taught the
  search", `GET /fit-insights`) with **one-click apply** for `experience_levels`/`min_fit`
  (`POST /fit-insights/apply`, whitelisted + re-validated). **The panel also charts improvement
  over time**: each live run logs a summary to `profile/fit_runs.jsonl` and the tab draws an
  inline SVG sparkline (best + mean fit vs the dashed min_fit bar, per-run hover) under an
  "▲ improving" headline, and the CLI prints the best-fit series — so the user can literally
  watch fit climb run over run. Complements 043's `recommended_min_fit` (which only ever RAISES
  the bar) by steering the *supply* of high-fit postings. **Verified:** 19 new tests (predictor
  flips the judged slot from a skill-stuffed senior posting to a bare new-grad one;
  recommendations fire under the right guards; run-trend summary + ordering); real-data
  diagnosis correctly named experience (23) as the drag and greenhouse (25) dead vs ashby (54);
  all endpoints driven live + a **headless screenshot of the trend panel** (best/mean lines
  rising past the min_fit bar); fixed a test-isolation bug where `test_discovery_cache` wrote
  history into the real profile dir; suite **159/159**. *Next:* the predictor uses level+board
  buckets only — add matched-skill-density / title n-grams once history is larger; consider
  company-level (not just ATS) board granularity in the dead-board flag.

- 2026-07-09 — **Profile section-jump nav now sticks while scrolling.** The `.pnav`
  pill bar (Applicant details, Experience, Projects, …) already had `position:sticky`
  but never pinned, because `main` had `overflow:auto` with no height, so the viewport
  scrolled instead. Gave `main` `height:100vh` (making it the real scroll container, the
  same pattern `aside` uses) so the nav pins to the top and stays clickable from any
  scroll position; added a bottom border so it reads as a bar. `web.py` CSS only.

- 2026-07-09 — **Projects ranked by technical impressiveness (decision 045).** New
  `applicationbot/impact.py` makes one subscription-CLI Claude pass scoring each résumé
  project 1–5 on engineering depth/difficulty, cached in a new optional `Project.impact`
  field in resume.yaml. The Profile "Projects" section now orders projects by that score,
  shows a ★ badge on each card, round-trips the score through save (hidden field), and has a
  "Rank by impressiveness" button (`/resume/rank-projects`, shared spinner + live elapsed).
  Selection stays **relevance-first**: `impact` only breaks ties in `catalogue.select_relevant`,
  the rules engine's `sort_projects`, and the tailoring system prompt — so the résumé leads
  with the strongest work without forcing off-topic projects on. Verified live on the real
  résumé (AgentStatus/ApplicationBot → 5, low-code dashboard → 2); suite 132/132.

- 2026-07-09 — **Project links captured + used to answer "projects you're proud of".**
  Added an optional `link` field to the résumé `Project` model, a "Link (optional)" input on
  each project card in the Profile page (`projCard`), and its capture in the résumé save
  collector. `generate_answer` already grounds Claude in the full base-résumé JSON, so the
  link now flows into drafted answers for open-ended/textarea questions (e.g. "a personal
  project you're proud of?") with no extra plumbing; unset links are omitted from the context.
  Not printed on the résumé PDF (renderer ignores the new field) — capture + answer-grounding
  only, matching the request's scope.

- 2026-07-09 — **min_fit auto-calibration + follow-up date (decision 043 update).** The two
  043 follow-ups: (1) `tracker.recommended_min_fit` turns the dead-band hint into a value
  (a fit band with ≥5 resolved outcomes and 0 responses → raise to band-top+1; never lowers,
  never past the top band) and `pipeline.effective_min_fit(filters)` applies it on pipeline
  CLI, runner, and web test-runs with a loud "min_fit raised 50→75 by outcome calibration"
  note — explicit `--min-fit` always wins, and a new **`calibrate_min_fit`** Discovery
  setting (checkbox in the Discover tab, default on) disables it; any tracker error keeps
  the configured value. `tracker calibration` prints the recommendation and whether it's
  applied. (2) **`follow_up_date`** tracker column (same additive migration as `fit_score`)
  + a "Follow up" Track-tab column. **Verified:** 6 new tests, suite **132/132**, JS clean,
  real DB migrated live, `/track` fields + `/discovery` both serve the new fields.

- 2026-07-09 — **Four adoptions from the ai-job-search survey (decision 043).** Surveyed
  [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search) (17.7k-star
  Claude Code framework — strong evaluation/document QA, no Apply stage) and implemented the
  four ideas worth taking, all zero-token at run time: (1) **ATS text-layer verification**
  ([ats_check.py](applicationbot/ats_check.py), new dep `pypdf`) — every exported PDF is
  read back the way an ATS parses it: name/email/phone must be literal text (catches the
  known latin-1 `?`-mangling), and JD keyword coverage is split *covered* vs
  *dropped-by-tailoring*; notes surface in the Discover tab + CLI. (2) **Per-application
  archive** ([archive.py](applicationbot/archive.py)) — `profile/applications/<posting>/`
  snapshots posting text + exact PDF + fill report; dry-runs overwrite, a real submission
  freezes a `submitted-<date>/` copy forever ("what exactly did we send" insurance for the
  autonomous runner). (3) **Multi-dimension fit rubric** — the judge now scores
  skills/experience/seniority 0-100 and `fit_score` is **computed in code**
  (`matching.weighted_fit`, weights .45/.35/.20) — auditable verdicts, dims shown in CLI +
  Discover tab, old discovery caches load fine. (4) **Outcome calibration groundwork** —
  tracker statuses gain interview/offer/rejected/no-response, a `fit_score` column is
  stamped at apply time (additive migration ran on the real DB, 12 rows intact), and
  `python -m applicationbot.tracker calibration` reports response rate by fit band with a
  raise-`min_fit` hint once a band has ≥5 dead outcomes. **Not adopted:** reviewer-agent
  tailoring pass (2× cost vs decision 034), LaTeX toolchain, LinkedIn scraping (Guideline
  #4). **Verified:** 19 new tests, full suite **126/126**, served JS `node --check`-clean,
  live CLI export prints ATS notes, `/track` serves the new statuses + Fit column.

- 2026-07-09 — **Tailoring token diet + measured one-page guarantee (decision 042).**
  (1) **Delta output:** the Claude tailor now returns a `TailorDelta` — entries referenced by
  index with rewritten bullets/notes, reordered skills, summary — and
  [backends._delta_to_tailored](applicationbot/backends.py) reconstructs the full
  `TailoredResume` in Python, so orgs/roles/dates/education/certifications are **copied
  verbatim** (structurally drift-proof, zero output tokens for them); the response schema
  shrank 4.8k→1.5k chars. External `TailoredResume` shape unchanged — web/render/drift-check
  untouched. (2) **Input diet:** compact résumé JSON (no indent, empties dropped) in the
  tailor prompt and `generate_answer`; `job_description.trim_for_prompt` strips trailing
  EEO/legal boilerplate (last-40%-only markers) and caps the JD at 8k chars (stored JD
  untouched — pay-band parsing unaffected). (3) **One-page is now MEASURED, not estimated:**
  [pdf.fit_to_pages](applicationbot/pdf.py) renders the real PDF, counts pages, and trims
  least-relevant-first (bullets to a 2-bullet floor, then trailing entries, ≥1 experience
  kept) until it truly fits, appending a note naming exactly what was dropped; wired into
  `tailor_resume` so CLI/web/pipeline and both backends all emit guaranteed-fit content —
  closes the audit gap "auto page-break silently spills to page 2". **Verified:** 8 new tests
  ([tests/test_resume_fit.py](tests/test_resume_fit.py)), suite **107/107**, plus one live
  tailor (real résumé × 10.3k-char JD, fast tier): valid delta first try, PDF measured at
  exactly 1 page. *Still open from the audit:* unicode TTF embedding (latin-1 `?`-mangling).

- 2026-07-09 — **Two-pass batched fill + live validation + fabricated-salary fix (decision 041).**
  (1) **Two-pass fill:** [_fill_page](applicationbot/apply.py) runs each form page as
  deterministic round 1 (defers unresolved decisions into `PendingDecisions` instead of
  spawning Claude per field) → `_resolve_pending` makes **≤3 batched schema-constrained calls**
  (`classify_questions` enum-array, `match_banked_questions` bank-sent-once index-array,
  `pick_dropdown_options` index-array + per-item token guard, all in
  [answer_bank.py](applicationbot/answer_bank.py)) → round 2 re-runs the same deterministic
  loop over the injected results; leftovers are captured with **no per-field fallback calls**
  (`semantic_done`/`picks_done`). Typeahead searches stay inline; generation-off is unchanged
  single-pass. Verified: [tests/test_two_pass_fill.py](tests/test_two_pass_fill.py) on a new
  fixture — classify + bank-match + pick fill in EXACTLY 3 stubbed calls; failure degrades to
  captures. (2) **Consolidated live dry-run (AppLovin Greenhouse, headless, never submits):**
  16 filled, 0 errors, all 12 react-selects `option:literal`, submit probe found — and the new
  audit trail caught a real bug: with `desired_salary` unset the salary question was
  **Claude-drafted to a fabricated figure** (and a prior run had banked "85000"). Fixed:
  numeric-fact questions (salary/GPA/test scores) are never `is_open_ended` (never drafted);
  the salary rule falls through to the bank instead of short-circuiting `resolve()`;
  `prune_answer_bank` drops drafted numeric-fact entries (ran with `--apply` — the "85000" is
  gone); corpus grew to 66 cases. Re-ran the same dry-run: salary cleanly captured, 0
  AI-drafted, all other fields byte-identical. Full suite **99/99**. **User action: set
  "Desired salary" in the Profile tab** (pipeline runs also get the decision-039 market
  estimate; the standalone apply CLI does not).

- 2026-07-09 — **Autofill determinism hardening (decision 040).** Four gaps closed so the same
  form + profile always fills the same way and the learning loop can't corrupt itself:
  (1) **Resolver regression corpus** — [fixtures/resolver_corpus.yaml](fixtures/resolver_corpus.yaml)
  (65 cases: real labels from the SpaceX/Stripe/Robinhood/Instacart/GitLab/Discord sweeps, incl.
  6 must-stay-null enumerated questions) + [tests/test_resolver_corpus.py](tests/test_resolver_corpus.py)
  pin the exact `resolve()`/`option_hints()` output against a synthetic profile — a rule edit
  that flips an answer now fails loudly instead of silently. (2) **Write-time gates** —
  `answer_bank.valid_mapping` is enforced in `remember_answers` (an invalid Claude `maps_to`
  is dropped at persist time, answer text kept), garbage-length questions are never banked,
  the prune script reuses the same gate, and `learn_option` refuses generic boolean aliases
  ("yes" → descriptive option would leak into every future Yes/No dropdown). (3) **The 3
  fill-time Claude decision calls are `--json-schema`-constrained** (classify → enum of known
  types; bank-match/dropdown-pick → integer index) — no more free-text reply parsing.
  (4) **Claude never decides while a dropdown menu is open** — `_fill_combobox` reads options,
  closes the menu, decides, then recommits by exact text (`_commit_option_text`); each combobox
  fill records its matched tier (`option:literal/learned/hint/claude/substring`) on the report
  as a determinism audit trail. **Verified offline, zero tokens:** 17 new tests incl. a
  react-select-shaped fixture ([fixtures/apply_forms/combobox.html](fixtures/apply_forms/combobox.html))
  asserting the menu is closed at decide time; full suite **93/93**. *Remaining:* fold the
  closed-menu recommit check into the next consolidated live dry-run; two-pass batched fill
  (scan → one Claude call → deterministic fill) deliberately deferred.

- 2026-07-07 — **Dynamic salary estimate when no band is advertised (decision 039)** —
  extends 038's fallback: instead of one static `desired_salary` for every no-band posting,
  new [salary.py](applicationbot/salary.py) computes a market estimate for
  (title, location, years) by cross-checking **Claude** (median range) and **Adzuna** (mean
  advertised salary) — agree ≤20% → mean, else take the **lower** — cached per (title,
  location) in git-ignored `profile/salary_cache.json` (30-day TTL, zero calls on a hit).
  When a later posting for the same role *does* advertise a real band, `validate_against_band`
  drops the cached estimate if it's >40% off, so real data self-corrects a stale guess.
  Resolver precedence is now **band midpoint → market estimate → stored figure**; degrades to
  Claude-only without Adzuna keys (reuses the existing `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`), then
  to `desired_salary`. Wired once in `run_testing_mode` → CLI, runner, and web all benefit.
  Verified offline ([tests/test_salary.py](tests/test_salary.py), stubbed Claude + Adzuna, 9
  cases) + full suite 76/76.

- 2026-07-07 — **Salary expectation tracks the posting's pay band (decision 038)** — fixes
  a dry-run under-ask: the bot filled the static `85000` for a posting advertising
  *$124,000 – $186,000*, ~$40k below the floor. `AnswerResolver` now parses the advertised
  band (`_posting_pay_range`, from the structured `Posting.compensation` string then the JD
  body; `$X – $Y`/`to`, `K`-notation, hourly excluded via a ≥1000 floor) and fills its
  **midpoint** via one `_salary_expectation()` helper used by both the keyword salary rule
  and the classified `desired_salary` type; falls back to the stored `desired_salary` when
  no band is advertised. `pay=p.compensation` wired in [pipeline.py](applicationbot/pipeline.py);
  the standalone `apply` CLI (no posting) keeps the stored figure. Verified with 7 cases
  ($124k–$186k JD body → 155000) + full suite 67/67.

- 2026-07-07 — **Discovery snapshot cache (decision 037)** — repeated dry-runs no longer
  re-search every board and re-judge the same postings. After a live discovery,
  [discovery_cache.py](applicationbot/discovery_cache.py) saves the whole ranked result
  (postings + Claude verdicts) to git-ignored `profile/discovery_cache.json`;
  [discover_and_match](applicationbot/pipeline.py) reuses it — skipping the board search
  **and** the Claude judge — when it's younger than `cache_ttl_hours` (new filter,
  default 12h) and the résumé/boards/filters fingerprint matches. `skip_seen` is
  re-applied on every hit against the current tracker, so a role applied to since the
  snapshot still drops out. Wired once, so the pipeline CLI, runner, and web UI all
  benefit; `python -m applicationbot.pipeline --fresh` / `runner --fresh` force a
  re-search. The web Discover tab shows a "♻ Reused a saved search from Nm ago" note with
  a one-click **Re-search fresh** button ([web.py](applicationbot/web.py), `/test-run`
  accepts `{fresh:true}`). Verified offline ([tests/test_discovery_cache.py](tests/test_discovery_cache.py),
  stubbed network + Claude): reuse skips the search, `--fresh`/TTL-0/résumé-change force a
  re-search, skip_seen prunes a now-tracked role from a cache hit.

- 2026-07-06 — **Multi-page wizards + Claude-cap resilience + discovery robustness (orchestrated
  session: 2 subagents in parallel with the main agent).** (1) **Multi-page navigation** (main
  agent, [apply.py](applicationbot/apply.py)): `_fill_all_pages` walks Next/Continue wizards —
  per-page fill/required-flagging, signature-change advance detection with frame re-location,
  validation-rejected advances recorded, late-page résumé upload, `_MAX_FORM_PAGES=8` backstop.
  Hardened while testing: required-label scan is **visible-only** (hidden wizard steps were
  polluting the blocked-reason), the pre-submit gate adds a **live DOM scan** for visible
  required labels with empty controls (catches required fields captured as "no saved answer"),
  `_upload_resume` no longer burns 30s on upload-less pages, and every dry-run records a
  **`submit_probe`** (the submit control it WOULD click — free live validation of armed-path
  selectors). (2) **Cap resilience** (subagent, backends.py+runner.py): typed
  `ClaudeAuthError`/`ClaudeRateLimitError` classification; the runner waits 15 min
  (kill-abortable, ≤3×/run) and retries the same match on a rate limit, stops with the exact
  fix on auth failure. (3) **Discovery robustness** (subagent, discovery.py+filters.py):
  per-host 0.5s pacing, retry×3/backoff honoring Retry-After, `canonical_url` dedup wired into
  discover() + skip-seen, opt-in `max_posting_age_days` staleness gate. **Verified: 53 tests
  green across 8 modules** (wizard fixtures, monkeypatched subprocess/urlopen — zero tokens,
  zero live dry-runs, zero real postings). *Next:* one consolidated live dry-run (probe
  validates submit selectors on real ATSs), then Workday accounts (multi-page prerequisite ✓).

- 2026-07-06 — **The submit stage exists: safety switch + real submit path + autonomous runner
  + fillability gate (decision 035).** Built from the full-system audit (also 2026-07-06; four
  parallel deep-dives, findings folded into Now/Next/Later above; UI/UX queue delegated to the
  Cursor agent via the bus). (1) **`safety.py`** — `SafetyGate`: arming lives in git-ignored
  `profile/safety.yaml` (`armed: false` default, `max_submissions_per_run`), the global kill
  switch is the `profile/KILL` file, and `may_submit()` re-checks everything immediately before
  every click; an unreadable safety file can never arm. (2) **`apply._attempt_submit`** — the
  armed path: pre-submit gate (any unresolved REQUIRED field ⇒ `blocked` outcome with the field
  names, no human pause), submit-button click (`Submit application`/`Submit`/`input[type=submit]`;
  a bare "Apply" is deliberately never matched), confirmation detection (page text/URL),
  client-side validation-rejection detection (⇒ `blocked`), and form-gone-without-confirmation ⇒
  `unconfirmed`-but-submitted so we never risk a double submission. New `ApplyReport.submit_state/
  blockers/confirmation`; banner + review pause + tracker all submit-aware (a real submission
  upgrades the row to `applied`, method `auto`, date stamped). Both CLIs take `--dry-run` to force
  disarm and print a loud ARMED warning otherwise. (3) **`runner.py`** — the autonomous loop:
  `python -m applicationbot.runner` applies to EVERY Claude-cleared match (refuses keyword-only
  auto-apply, closing the `min_fit` bypass), headless dry-run by default, kill-file check between
  applications, `--max` + cap stops, per-application failure isolation with a stop-the-queue rule
  for Claude-CLI failures. (4) **Fillability gate** — `pipeline._is_fillable` keeps
  workday/icims/unresolved-aggregator postings out of the matcher entirely (no judge tokens
  wasted), exposed as `PipelineResult.non_fillable` + CLI count. **Verified with ZERO live
  dry-runs and zero Claude tokens:** new `tests/` package (23 passing) driving local ATS-shaped
  HTML fixtures (`fixtures/apply_forms/`) headless — including a true end-to-end armed run
  (fill from the sample résumé → gate → click → confirmation detected → `submitted: True`) and
  its dry-run twin. `profile/safety.yaml` + `profile/KILL` confirmed git-ignored. *Next:* Claude
  usage-cap resilience, live per-ATS submit validation (one consolidated dry-run), multi-page
  navigation, then Workday accounts.

- 2026-07-06 — **Captured questions recreate their real form control (dropdown stays a dropdown).**
  An unanswered question was always shown as a free-text box, even when the form field was a dropdown
  — so the user's typed answer often didn't match an option at fill time. Now the Apply stage records
  each unanswered field's **control kind + options** (`ApplyReport.captured`; `_field_options` reads a
  native `<select>`'s options or opens a react-select to read them; radio/checkbox groups pass their
  labels), threads it through `capture_questions(meta=…)` onto new `QA.input_kind` / `QA.options`
  (backfilled onto pre-existing blank entries when re-seen), and the Profile UI's `qaAnswerInput`
  renders a **dropdown with those exact options** when present, else a textarea. Round-trips through
  save. **Verified:** unit (kind/options stored + round-trip) + served JS clean + **live SpaceX**
  capture — GPA×3 and Active Security Clearance recorded as `dropdown` with their real option lists
  ("4.0 out of 4.0", "Never held a clearance", …), "Please specify" as `text`; the Profile tab then
  renders those as native `<select>`s (screenshot) and free-text as textareas, 0 console errors. Now
  the user picks the actual option, so it matches at fill time.

- 2026-07-06 — **Claude cost/latency overhaul for tailoring + fit judging (decision 034).**
  Root cause measured: every `run_claude_cli` spawned a full default Claude Code session —
  ~40,000 tokens of coding-agent context (system prompt, tool schemas, MCP, CLAUDE.md) per
  call, and the fit judge ran 10 serial spawns on the CLI's default model. Now every headless
  call is stripped (`--system-prompt`/`--tools ""`/`--strict-mcp-config`/`--setting-sources ""`
  — measured 184 tokens vs ~40k, 74x), the judge is pinned to Sonnet and batched 5 postings per
  call (`judge_fit_batch`), and both tailor + judge use `--json-schema` for guaranteed-valid
  JSON (kills the retry double-spend and the schema dump in the prompt). Prompts and quality
  tiers unchanged. Verified end-to-end: batch judge 7.1s/2 postings with correct verdicts;
  fast-tier tailor 13.9s (was ~30s), no drift warnings. Net for a 10-posting run: ~400k+
  overhead tokens → ~15k; judging minutes → under a minute.
- 2026-07-06 — **Screening-answers section redesigned (readable, ranked, quick-answer).** The
  "Saved answers to screening questions" list was a flat wall of collapsed cards, hard to tell
  answered from unanswered. Rebuilt it: (1) split into **"Needs your answer"** (open full-width
  panels, ready to type) vs a compact **"Answered & auto-handled"** 2-column grid (✓/✨/↔ marked);
  (2) unanswered are **ranked by a new `QA.seen_count`** — how many times autofill hit the question
  and couldn't answer it (`capture_questions` bumps it each recurrence; a "seen N×" badge shows it);
  (3) a **summary bar** (need / answered / auto-from-profile counts) + a **"Start answering" button**
  that scrolls to and focuses the first answer box; (4) the profile editor widened 640→1040px to use
  the page. `seen_count` round-trips through save (`collectProfile` now reads all `.card`s under
  `#sec-qa`, nested in the two groups, + a hidden `seen_count`). **Verified:** unit
  (`capture_questions` increments on recurrence, leaves answered alone, round-trips) + served JS
  `node --check` + **live headless drive**: ranked badges (6×/3×/1×), both groups populated, Start
  focuses an answer box, answer→Save→reload moves the item to Answered with counts preserved, 0
  console errors + screenshot. *Note:* existing entries start at seen_count 0 (predate this); ranks
  differentiate as autofill runs accumulate counts.

- 2026-07-06 — **8-form sweep across all 4 ATSs + checkbox-group aliasing fix.** Ran dry-runs over
  Stripe×2 (greenhouse), cin7×2 (lever), Ramp×2 (ashby), SpaceX×2 (greenhouse) to shake out gaps
  before the autonomous runner. **Result:** cin7 and Ramp fully clean (only Twitter/Portfolio/Other-
  website data gaps the user doesn't have); SpaceX 23 filled (remaining = genuine GPA/SAT/ACT/GRE/
  clearance data gaps); Stripe had ONE new code gap. **Fixed:** the "Please select the country or
  countries you anticipate working in" **checkbox group** — Stripe labels the option **"US"** (its
  list uses abbreviations UAE/UK/US), and `_fill_checkboxes` matched only the resolved *value*
  ("United States"), ignoring `option_hints`. Now the multi-select checkbox pass consults
  `option_hints` too (like the combobox/select passes), so the US-alias hint matches the "US" box.
  **Verified:** unit (ticks exactly ["US"], no over-match on Australia/etc. thanks to whole-word
  short-value matching) + **live Stripe**: country checkbox now "US", filled 21→22, only an OPTIONAL
  WhatsApp-marketing opt-in left (correctly not auto-checked). *Net state after this session's fixes:*
  across the sweep, every required field we have data for now fills; the only blanks are true data
  gaps (test scores, security clearance, secondary URLs) and correctly-skipped marketing opt-ins.
  *To push higher:* set veteran/disability in the Profile tab; consider a GPA profile field (SpaceX/
  new-grad forms ask for it).

- 2026-07-06 — **Descriptive-option dropdowns + polluted answer bank (required fields left blank).**
  Toward "consistently fully fill dry-runs", debugged the SpaceX Greenhouse form (from the tracker):
  required fields we know the answers to were blank/wrong. **Root causes + fixes:** (1) **Descriptive
  dropdowns** — work-auth options are "I am authorized to work in the United States for any employer"
  (not Yes/No), citizenship is "(a) U.S. citizen or national…"; our "Yes" shared no word so the
  Claude-pick **token guard rejected it**. Added deterministic `option_hints` for work-auth
  (any-employer / sponsorship / not-authorized, phrased to not substring-match the negative option)
  and citizenship (U.S. citizen), and **exempted booleans (yes/no/true/false) from the token guard**
  so Claude can map "Yes"→a descriptive option. (2) **ADA** "Can you perform the essential functions…"
  → answered **Yes** (new rule). (3) **Wrong "Yes"** on Security Clearance / Employment History from
  the **semantic classifier** — added `clearance`/`employment history`/GPA/test-score terms to the
  `classify_question` skip so enumerated questions are captured, not boolean-mapped. (4) **Polluted
  answer bank (the deep one):** a pre-fix run had learned WRONG `maps_to` and banked it —
  "SpaceX…Employment History → work_authorized" (→ "Yes") and "located in Japan → country" — and a
  banked mapping OVERRIDES the corrected structured rules. New idempotent
  [scripts/prune_answer_bank.py](scripts/prune_answer_bank.py) clears a banked `maps_to` when it's now
  invalid (enumerated/demographic/company-specific, or a structured rule now answers it) and drops
  garbage entries; ran it (3 fixed, answer text preserved). Added "employment" to the prior-company
  verbs so "Employment History" maps to No→"never worked" (company set in the pipeline). **Verified:**
  unit (all descriptive options map to the real SpaceX option text; guard/classify/prune) + prune
  idempotent + **live SpaceX**: filled 19→23, work-auth/citizenship/essential/employment/EEO all
  correct, clearance cleanly captured, **zero wrong fills**. *Remaining SpaceX gaps are genuine:* GPA
  / SAT / ACT / GRE (no data) and security clearance (no profile field). *EEO note:* gender/race fill;
  set **veteran/disability** in the Profile tab (the new dropdowns) to fill those too.

- 2026-07-06 — **LinkedIn now renders on the tailored résumé/PDF (reported "LinkedIn not filling").**
  Reported as a form-autofill miss, but reproduced on ALL the user's actual dry-run forms (from the
  tracker: MARGO/Lever, SpaceX×2/Greenhouse, Ramp/Ashby) and the LinkedIn FIELD fills correctly
  everywhere it exists — the Stripe forms simply have no LinkedIn field. **Real root cause:** the
  résumé's `contact.links` is empty (the LinkedIn/GitHub live only in the apply profile's Applicant
  details, a separate field from the résumé header's Links), so `_render_contact` emitted no links —
  the tailored résumé/PDF the user reviews and submits had no LinkedIn. Fix: new
  `apply_profile.resume_with_profile_links(resume, profile)` fills the résumé's contact links from
  the profile's linkedin/github/portfolio URLs **when the résumé itself has none** (deep-copy,
  no-op otherwise), wired into every render path — pipeline dry-run PDF, web preview + PDF export,
  CLI `--out`. **Verified:** unit (fills when empty, no-op when present, original untouched) + the
  rendered résumé header now shows `…| linkedin.com/in/… | github.com/…` + PDF generates + served JS
  clean + all modules import. *Note:* the form autofill was never broken; the résumé content was.

- 2026-07-06 — **Preferred office location: ranked-list preference (chose over a map).** "What is
  your preferred office location?" is a dropdown of the *company's* discrete offices, so a map
  (arbitrary lat/long + external tiles + geocoding every office name — network deps that break the
  local UI) is the wrong tool; the user agreed to a **ranked list** instead. New
  `ApplicationProfile.preferred_locations: list[str]` (most-preferred first) + a Profile-tab textarea
  ("one per line"). Resolver `_office_prefs()` builds the ranked candidates (explicit list → Remote
  if open_to_remote → home city as last resort); `_office_hints()` expands each with its city-only
  form ("New York, NY" → also "New York") so it matches whether the form option is the bare city or
  suffixed ("New York (HQ)"); a resolve rule + option_hints fire for office-CHOICE questions (guarded
  off the Yes/No "willing to work from the office"). The highest-ranked office the form actually
  offers wins; if none are offered it stays captured (never forces a wrong office). Reusable by
  Discover later for location ranking. **Verified:** 7 unit cases (rank-1 wins, falls to Remote then
  home, suffix match, none-offered→captured, doesn't hijack the yes/no) + served JS `node --check` +
  **live**: Profile-tab drive set/saved/persisted the list, then a Robinhood dry-run filled
  "What is your preferred office location? → New York, NY" (filled 19→20; real profile backed
  up/restored).

- 2026-07-06 — **Profile tab: EEO fields are now dropdowns + a pronouns field (wiring discovered
  gaps).** The sweep's recurring "Veteran Status / Disability Status — no saved answer" gaps weren't
  missing UI — the fields existed but were **free-text**, so they sat blank (you'd have to guess the
  exact EEO wording). Converted **gender / race-ethnicity / veteran-status / disability-status** from
  free-text to **dropdowns with standard EEO self-identification options** (`GENDER_OPTS`/`RACE_OPTS`/
  `VETERAN_OPTS`/`DISABILITY_OPTS` in [web.py](applicationbot/web.py), each starting with a blank "—"
  so declining stays possible), and added a new **Pronouns** dropdown + `ApplicationProfile.pronouns`
  field — the resolver's `_pronouns()` prefers it and only derives He/Him from gender when it's unset
  (so non-binary users get the right answer). `selField` preserves any previously-stored value, so no
  data migration. Standard wording maximizes direct option-matching; the combobox's Claude-pick maps
  onto a form's exact text otherwise. **Verified:** schema round-trip + resolver (pronouns explicit
  wins over derived; veteran/disability now resolve) + served JS `node --check`-clean + **live headless
  drive of the Profile tab** — all 5 dropdowns render with the right options, set pronouns=They/Them &
  veteran → Save → reload **persisted**, 0 console errors (real profile backed up/restored byte-safe).
  *Note:* "preferred office location" still has no field (genuinely per-company); left for the user.

- 2026-07-06 — **Checkbox support (consent/agreement + multi-select groups).** The driver skipped
  all `type=checkbox` controls, so Robinhood's REQUIRED demographic-consent box ("By checking this
  box, I consent to…") was never filled — blocking submission. New `_fill_checkboxes()` handles two
  cases the field/radio passes miss: (1) **standalone agreement/consent/certification** checkboxes
  → checked (`_is_agreement`: agree/consent/certify/acknowledge/authorize/terms/privacy/…), since
  they gate submission and checking them is inherent to applying (the armed user authorized it;
  dry-run never submits) — **optional opt-ins are always left** (`_is_optional_optin`: marketing/
  newsletter/talent-community/SMS/"contact me about other roles"; opt-in wins even when an agreement
  word is also present, e.g. "consent to marketing SMS"); (2) **multi-select groups** (>1 checkbox
  under one question, "race — check all that apply") → check the option(s) matching the resolved
  answer, like radios. Wired after `_fill_radio_groups`, before `_flag_missing_required` (so a
  checked consent isn't flagged missing). Native-checked boxes are recorded, not re-toggled.
  **Verified:** 10 unit assertions (5 agreements checked, 5 opt-ins/unknowns left) + **live** on
  Robinhood — the consent box is now checked (✓ in the screenshot), out of REQUIRED-not-filled,
  filled 18→19, and NO other checkbox was touched (conservative). *Follow-up:* checkbox groups
  without a `<fieldset>` may not group (each box's nearest long label differs) — the fieldset case
  works; revisit if a real form needs it.

- 2026-07-06 — **Autofill gap sweep: 8 fixes across 4 real ATS forms.** Drove dry-runs against
  Robinhood / Instacart / GitLab / Anthropic Greenhouse forms, collected every unanswered/mis-filled
  field, and fixed the cross-form patterns. **New answers:** (1) **state/province** dropdown from the
  location's state (`_state_from_location()`; "Edison, NJ" → "New Jersey", + abbrev hint — live:
  Instacart "(US) New Jersey"); (2) **preferred name** → first name (GitLab "Gabriel"); (3)
  **start-date** keyword broadening ("earliest you would want to start", "start working");
  (4) **pronouns** derived from gender (`_pronouns()`; before the gender rule so "gender pronouns"
  isn't answered "Male" — live: Robinhood "He / Him") + pronoun/gender option hints (Male→**Man**,
  live: Instacart "Man", Robinhood "Cisgender man"). **Mis-fills fixed:** (5) **"military status"**
  was answered "Yes" by the semantic classifier — added `military`/`pronoun`/`lgbt`/`transgender`/
  `sexual orientation` to `answer_bank._DEMOGRAPHIC` so it's never auto-classified (live: now
  cleanly "no saved answer"); (6) **"subject to employment agreements with your current employer"**
  returned the employer name ("Ninth Wave") — guarded the employer rule against
  agreement/restriction/non-compete phrasing; (7) **"preferred office location"** was answered with
  the home city — the location rule now excludes "office" (that asks which company office). **New
  default:** (8) **prior relationship with the hiring company** ("worked/interned/consulted/
  interviewed at <Company>") → "No", gated on the hiring company being named (so "worked with
  <tech>" and "used <product>" are never caught). **Verified:** ~25 unit assertions + live re-sweep
  — Robinhood 11→19 filled (needs 15→7), Instacart needs 7→4, GitLab needs 6→4; remaining misses are
  genuine (empty profile veteran/disability fields, LGBTQ/consent/government-official questions with
  no data). *Follow-ups:* Robinhood's demographic **consent checkbox** (REQUIRED, we don't handle
  checkboxes); "preferred office location" still needs an office-preference profile field; the prior-
  company default is unit-tested only (the CLI passes no company — it fires in the real pipeline).

- 2026-07-05 — **Autofill: per-country work authorization + sponsorship.** "Are you legally
  authorized to work in Japan for our Company?" was answered "Yes" off the applicant's *generic*
  work-auth flag — wrong for a US applicant applying to a foreign-country role. Now the work-auth
  and sponsorship rules detect a **concrete foreign country** named in the question and override:
  `_named_foreign_country(n)` extracts the place from a "work in <X>" clause, returns it only when
  it's a real country the applicant is NOT authorized in — None when the clause is absent, **vague**
  ("the location(s) you selected", "the country in which you are applying", "this country" — a
  `_VAGUE_PLACE` word list), or their **own** country (`_authorized_countries()` = home country,
  US spelled its many ways). Work-auth flips to **No** for a foreign country (else the general
  flag); sponsorship flips to **Yes** for a foreign country when they need none at home (else the
  general flag). No new schema/UI — the home country is the honest default. **Verified:** 14 unit
  cases (Japan/Canada/UK → No; US/vague/generic → the general flag unchanged; sponsorship symmetric)
  + **live headed dry-run** on the Discord "Account Executive – Japan" form — work-in-Japan now
  **No**, everything else intact, `submitted:False`. *Follow-up:* a dual-national / multi-visa
  applicant with authorization beyond their home country would need an explicit
  `work_authorized_countries` list (schema + Profile-tab field) — deferred until a real user needs it.

- 2026-07-05 — **Autofill: Discipline field + "located in <country>?" Yes/No (education/location edges).**
  Two correctness gaps the Discord Greenhouse form exposed. (1) **Discipline** (the education
  section's field-of-study dropdown) was left blank — the résumé stores the major inside the degree
  string ("Bachelor of Science in Computer Science, …") with no separate field. New
  `AnswerResolver._field_of_study()` parses the phrase after "in" up to the first comma
  ("Computer Science"); the resolver now matches `discipline`/`concentration`/`field of study`/
  `major` and returns it. (2) **"Are you currently located in Japan?"** was wrongly answered with
  the applicant's country ("United States") — Claude's semantic classifier mapped it to the
  `country` type. It's a Yes/No: new `_place_matches_applicant()` compares the named place to the
  applicant's country + location (US spelled its many ways, state abbrevs expanded), and a rule
  before the location/country block answers "Yes"/"No" for "are you (located|based|residing|living)
  in <place>?" / "do you live in <place>?". **Verified:** unit tests (Discipline→"Computer Science";
  Japan→No, US→Yes, Canada→No; real Location/Country/work-auth fields unchanged) + **live headed
  dry-run** on the Discord form — Discipline "Computer Science", located-in-Japan "No", 15 fields
  filled, only "Website" (genuinely empty) left, `submitted:False`. *New follow-up spotted:* "Are
  you legally authorized to work in Japan?" answers "Yes" from the applicant's *general* work-auth
  flag — a US-based applicant isn't necessarily authorized for a Japan role; per-country work
  authorization is a separate, thornier gap (no per-country data today).

- 2026-07-05 — **School dropdown fill fixed + main-campus precision (decision 033 follow-up).** The
  school typeahead still never filled after decision 033. **Root cause:** the searchable-combobox
  path typed the *full* résumé value ("The Pennsylvania State University") and then its first word
  ("The") as the search query — but a school picker is prefix-indexed under the normalized name
  ("Pennsylvania State University-…"), so neither query retrieved any option, so there was nothing
  to match or learn (self-improvement never bootstrapped because it never got one successful pick).
  Fix: new `_search_queries(value)` yields article-stripped, progressively-shorter queries
  ("The Pennsylvania State University" → "Pennsylvania State University" → "Pennsylvania State"),
  fed into `_fill_combobox`'s searchable paths. **Ordering matters:** **Phase 2b** (Claude picks
  from the retrieved options — it's told to prefer the primary/main campus) runs BEFORE **Phase 2c**
  (a no-Claude best-effort substring fallback for generation-off), because the first live run
  exposed a correctness bug — the substring fallback grabbed the first matching campus
  ("Pennsylvania State University - Erie, The Behrend College") and even *learned* it. Only Claude's
  vetted pick is learned; the substring pick is not. Comma values like "Edison, NJ" skip 2c so the
  location path is unchanged. **Verified LIVE (headed dry-run, Discord Greenhouse form with the
  native Education section, real profile):** School committed
  "Pennsylvania State University - University Park" (correct main campus), Degree "Bachelor's
  Degree", 13 fields filled, `submitted:False`. *Follow-ups:* the "Discipline" education field is
  left blank (résumé stores the major inside the degree string, no separate discipline field);
  "Are you currently located in Japan?" got the country value — a location-vs-country label edge.

- 2026-07-05 — **Self-improving dropdown resolver (decision 033).** Dropdowns kept breaking one at
  a time (country, degree, now school), each needing a hardcoded hint. Now the combobox filler
  learns: `_fill_combobox` literal-matches the answer + hints + **learned aliases** on first open;
  on a static list with no match, **Claude picks the best option from those fresh options**
  (`answer_bank.pick_dropdown_option`, guarded by a token-overlap check so it never picks an
  unrelated option — "Harvard" for "Penn State" → none); searchable lists type-then-pick. The
  value→option mapping is stored on the resolver, persisted to `ApplicationProfile.dropdown_aliases`,
  and consulted first next time — so a value that once needed Claude matches instantly with no
  call. **Verified live** on the Stripe form: no regression (country "US", gender "Male"); a
  hint-less degree resolved to "Bachelor's Degree" via Claude and was **learned**; a later fill
  with generation OFF matched it from the learned alias, zero Claude calls. This is the mechanism
  for the reported "school dropdown broke" — school (and any new dropdown) is now handled +
  learned automatically. *Note:* a full fill adds a Claude call only for genuinely-new dropdown
  values; the token guard rejects short-code options (e.g. "US"), which still rely on hints.

- 2026-07-05 — **JD-upgrade for all 6 ATSs + dashboard "Sources" section (decision 032 update).**
  (1) `_resolve_jd` extended to **SmartRecruiters / Workable / Recruitee** (was GH/Lever/Ashby only),
  so a bridged aggregator hit on any fillable ATS gets its snippet replaced with the full JD — also
  broadens what the early-career curated feeds can resolve. Verified live (SR 5601, Workable 6130,
  Recruitee 4133 chars; bridge upgraded an SR snippet). (2) New **"Where your postings come from"**
  overview at the top of the Discover tab (`GET /sources`): target boards by ATS, Adzuna status
  (active via your key / env vars / not set up), early-career on/off, the bridge, and the
  auto-fillable ATS list. **Fixed** the board-picker to offer all six ATSs (it only listed
  greenhouse/lever/ashby — the SmartRecruiters/Recruitee/Workable sources were unselectable). **Wired
  the Adzuna setup path**: clickable developer.adzuna.com free-key link + env-var/own-key note.
  Verified: served JS `node --check`-clean, `/sources` HTTP round-trip (real discovery.yaml backed
  up/restored), and a headless-Chromium drive of the tab (overview + 6-ATS dropdown + link, 0 console
  errors). Coordinated with the parallel Cursor agent via the bus (claimed discovery.py/web.py).

- 2026-07-05 — **Early-career discovery: SimplifyJobs new-grad/intern feeds (decision 031).** With
  senior-heavy boards, 0 of 10 judged roles cleared the fit cutoff for a junior résumé. New
  `CuratedListSource` reads the community SimplifyJobs New-Grad + Summer2026-Internships
  `listings.json` (early-career by construction), keeps `active` roles on a resolvable+fillable ATS
  (Greenhouse/Lever/Ashby), ranks them by title-relevance to the résumé, and **resolves the full JD
  for the top-K** via the linked ATS's single-job endpoint — emitting normal full-JD Postings.
  Curated postings are **judged first** in `keyword_rank` so verbose senior board JDs don't crowd
  them out. Config: `DiscoveryFilters.early_career` (enable/kinds/max_resolve) + a toggle in the
  Discover-settings editor; off by default. Personal-use only (public job links; Guideline #4).
  **Verified live:** on the same senior-heavy config, enabling it took the run from **0 cleared →
  4 cleared** (AppLovin New-Grad 82, MARGO 78, Blitzy 68, Evolver 68) while senior board roles
  still correctly denied (≤42). *Follow-ups:* add SmartRecruiters/Workday JD resolution (more of
  the ~40% supported grows); a "browse all judged / pick manually" view; USAJobs Pathways (discovery-only).

- 2026-07-05 — **Workable source + aggregator→ATS bridge (decision 032).** (1) **`WorkableSource`**
  added to `ATS_SOURCES` — `POST apply.workable.com/api/v3/accounts/{account}/jobs` (token-paginated)
  + `GET api/v2/…/{shortcode}` for the full JD (N+1 like SmartRecruiters, bounded by
  `_DETAIL_MAX_POSTINGS`); apply URL `apply.workable.com/{account}/j/{shortcode}/`. `fetch_json` now
  supports POST. Config: `{ats: workable, token: <account>}`. Completes the auto-apply ATS set
  (GH/Lever/SmartRecruiters/Workable). **Verified live:** mlabs 4/4 full JD, apply-URL format, JD
  round-trip. (2) **Aggregator→ATS bridge** — `resolve_redirect()` follows the 30x chain and
  `bridge_aggregator_postings()` turns an Adzuna/Jooble hit (which only returns a redirect through
  its own domain) into an auto-apply candidate: resolve → `detect_ats_from_url` (extended to cover
  recruitee+workable) → rewrite `ats`+`apply_url`, and for GH/Lever/Ashby **upgrade the snippet body
  to the full JD** via the curated-list resolvers. Wired into `discover_and_match` (`bridge=True`,
  `PipelineResult.bridged`), before matching; a **no-op when no aggregator postings are present**.
  Reused the parallel agent's `detect_ats_from_url`/`_resolve_jd` (coordinated via the agent bus).
  **Verified live:** detector across all 6 ATSs+workday; real 30x resolved; synthetic Adzuna→greenhouse
  with snippet→7.5k-char full-JD upgrade; in-pipeline bridge of an injected aggregator hit →
  greenhouse (11.7k-char JD) → match. *Findings this session:* hiring.cafe now auth-gated (rejected,
  Guideline #4); LinkedIn/Indeed/SEEK partner-gated (out); USAJobs/Jooble/Muse deferred behind the
  same interface (USAJobs is full-JD but gov-portal apply = not autofillable).

- 2026-07-05 — **Discover tab shows judged postings (denied + accepted) + judges more.** When a
  dry-run couldn't find a posting clearing the fit cutoff, the user had no visibility into what
  the searches returned. Now the Discover tab lists **every Claude-judged posting ranked**, each
  with its fit score, a ✓ cleared / ✗ denied marker (vs `min_fit`), the one-line "why", and any
  missing requirements — shown even when nothing clears. The "nothing cleared" message now names
  all the levers (lower min_fit, raise top_n, set experience_levels to your level, add boards).
  Raised default `top_n` 10→20 (judge more per run = more chances to clear). Backend: `_test_worker`
  exposes a `judged` list in the run state; UI `renderJudged`. **Verified live** on the real
  profile: surfaced 10 denied senior-Stripe roles (scores 8–42) for a junior/intern résumé at
  min_fit 70 — exactly the diagnostic the user wanted. *Insight it reveals:* senior-heavy boards +
  a high cutoff → set `experience_levels` (internship/new_grad/junior) to filter senior roles
  before judging, and/or lower `min_fit`.

- 2026-07-05 — **Dropdown option-matching: country-reside + degree now fill.** Live-DOM debug of
  the Stripe embedded Greenhouse form showed the country dropdown's US option is literally **"US"**
  (abbreviated list: UAE/UK/US) and degree options are standard levels ("Bachelor's Degree"), so
  our verbose values ("United States", "Bachelor of Science in Computer Science, …") matched
  nothing. Fixes: (1) `_degree_hints()` maps a résumé degree to the standard level ("Bachelor's
  Degree", etc.); (2) re-added US/USA country hints — now **safe** because `_matches` whole-words
  short values; (3) **rewrote `_fill_combobox`** to open the menu ONCE and match any candidate
  against the shown options (static lists like a 29-country dropdown resolve in one open — faster
  and more reliable than re-typing each candidate, which made react-select flaky), falling back to
  per-candidate typing for async lists (geocoder), with an Escape reset between phases so the
  geocoder still works. **Verified live:** Location→"Edison, New Jersey, United States",
  country-reside→"US", degree→"Bachelor's Degree", all prior fills intact, no wrong "Australia".
  *Known perf follow-up:* a full fill is ~2.5 min — the per-combobox `_open_options` timeouts
  compound; worth trimming later.

- 2026-07-05 — **Two more ATS discovery sources: SmartRecruiters + Recruitee (decision 030).**
  Researched improving discovery breadth (user named hiring.cafe/LinkedIn) with the explicit goal
  of exercising the Apply autofill on *more ATS form systems*. **Probed every candidate API live**
  — which mattered: **hiring.cafe's** search API has moved behind session Bearer-token auth
  (`/api/search-jobs` now 401/405; frontend uses `/ssr/search-jobs` with an auth-derived token), so
  the scraper repos the research cited are stale and using it would circumvent an access control
  (Guideline #4) — **rejected**; **LinkedIn** re-confirmed off-limits (no candidate API; scraping
  breaks ToS). Chose to **broaden the ATS layer** instead of adding an aggregator, since a new ATS
  is a genuinely new form system (aggregators dump you on a listing page or an ATS we already
  handle). Added `SmartRecruitersSource` (`api.smartrecruiters.com/v1/companies/{co}/postings` + a
  per-posting detail call for the full JD, bounded by `_SR_MAX_POSTINGS=100`) and `RecruiteeSource`
  (`{co}.recruitee.com/api/offers/`, one call, full JD inline) to `ATS_SOURCES` — **no schema
  change** (the `Board{ats, token}` model already takes any ats string; config is
  `{ats: smartrecruiters, token: <Company>}` / `{ats: recruitee, token: <company>}`). Zero new deps.
  **Verified live:** SmartRecruiters (PublicStorage 5/5, BoschGroup 3/3) + Recruitee (bunq 16/16)
  return full JD, direct apply URLs, and round-trip through `to_job_description()`/`to_markdown()`;
  the full pipeline ran discover→gate→match over 505 postings with 0 errors. Caveat: many big
  SmartRecruiters companies restrict their public postings API (return 0 — surfaced cleanly, not an
  error). Workable deferred (its anonymous widget returned 0 jobs for every slug tried).

- 2026-07-05 — **Autofill correctness fixes (from a real run).** (1) **Current job** was wrong —
  résumés aren't always most-recent-first, so `experience[0]` picked an ended role; now derives
  the CURRENT employer/title from the ongoing entry (`end` = Present), via `_current_experience`.
  (2) **"Where do you currently reside" / work-auth** now fill — added reside/residence/live
  coverage; country checked before city; work-eligibility before location. (3) **"Are you
  Hispanic/Latino?"** now answered (No) by deriving from the profile's `race_ethnicity`; also
  fixed `_has(n,"city")` matching "ethni-CITY" (word-boundaried). (4) **Wrong-country fill** — the
  combobox was committing "Australia" for "country: United States": removed the blind "pick first
  option" fallback, made short values (≤3 chars) match **whole words** only (so "US" ≠ "A-US-tralia",
  "No" ≠ "Norway", while "Yes" still matches "Yes, I am authorized"), and dropped unsafe US/USA
  hints. **Verified live on the Stripe embedded Greenhouse form:** 15 fields fill correctly
  (current employer=Ninth Wave, work-auth=Yes, sponsor=No, Hispanic/Latino=No, race=Asian), **zero
  wrong fills**, `submitted:False`. *Follow-up:* the "country where you currently reside" and
  "degree" dropdowns don't positively match their option text (now safely flagged for review, not
  mis-filled) — needs a live-DOM debug of those specific react-selects + degree normalization.

- 2026-07-05 — **Tailored résumés persist to a stable, bounded store (decision 029).** Dry-run
  PDFs were written to `$TMPDIR/tailored_*.pdf`, which macOS purges — so a Track row's
  `resume_path` would dangle and you couldn't review the résumé an application used. New
  `applicationbot/resume_store.py` writes each PDF to git-ignored `profile/tailored/`, named
  deterministically from the posting URL so a re-run **overwrites** (one file per posting, ~5 KB).
  Growth is bounded three ways: per-posting overwrite, **cascade delete** (`tracker.delete_application`
  removes the row's file, guarded to only touch paths under the store — never a user `--pdf`), and a
  100 MB **size-cap** backstop (`prune`). `pipeline._apply_one` now calls `resume_store.write_pdf`
  instead of `tempfile`. `scripts/migrate_tailored_pdfs.py` (idempotent) moved the 3 existing dry-run
  rows into the store. **Verified:** deterministic naming + overwrite; `is_managed` refuses external
  deletes; prune keeps newest; cascade delete removes managed / spares user PDFs; migration is a
  no-op on re-run; PDFs stay git-ignored. **Track tab now links to the stored PDF:** the
  "Résumé used" column renders a **"View résumé ↗"** link that opens the exact tailored PDF inline
  (`GET /track/resume?id=N` streams the row's file; actionable 404 when a row has none). *Verified
  over live HTTP:* the link serves the real 4,705-byte PDF with an inline filename, 404s cleanly for
  a missing row, and the served page JS is `node --check`-clean.

- 2026-07-05 — **Fit-score threshold for apply + fixed the low-fit bypass bug.** The dry-run was
  following through on poor matches (a 45/100 role) despite the CLI's `--min-fit 50`. **Root
  cause:** `pipeline.pick_top` fell back to `matches[0]` whenever nothing cleared the bar —
  even when Claude *had* judged them — so the threshold was silently bypassed. Fixed: if any
  posting was judged, respect the threshold (return None) instead of applying to a below-bar
  role; the keyword-only fallback (no Claude available) is preserved. Made the threshold a
  first-class setting: new `DiscoveryFilters.min_fit` (default 50), surfaced as **“Minimum fit
  score (0-100)”** in the Discover settings editor and used by the web dry-run worker (was
  hardcoded `min_fit=0` — the other reason the web tab ignored fit). CLI `--min-fit` now
  defaults to `filters.min_fit` (one source of truth). When nothing clears the bar the Discover
  tab shows an actionable message naming the best fit this run and pointing at the setting.
  **Verified:** `pick_top` unit tests (82 chosen over 45 at 50; lone 45 rejected at 50, accepted
  at 40; keyword-only fallback intact) + `/discovery` round-trip persists `min_fit`.

- 2026-07-05 — **Track tab is now spreadsheet-like: resizable + hideable columns.** The
  applications table ([web.py](applicationbot/web.py)) switched from fixed percentage widths to
  per-column **pixel widths with drag-to-resize handles** on each header's right edge (table can
  overflow into a horizontal scroll like a sheet), plus a **Columns ▾ menu** to show/hide any
  column (keeps at least one; “Reset columns” restores defaults). Width + visibility choices
  **persist per browser in localStorage**. Inline cell editing / status pills / add / delete all
  unchanged. **Verified:** served page JS `node --check`-clean; new controls present in the
  rendered HTML.

- 2026-07-05 — **Semantic question classification (decision 028).** On a keyword miss, Claude
  maps a novel application question onto a known structured field type (work-auth, sponsorship,
  remote/onsite, relocate, salary, start-date, location, …) so semantic variants are answered
  instead of captured blank — e.g. "Are you willing to work out of our NYC/SF office 2-3 days a
  week?" → `open_to_remote`. The **mapping** is cached (new `QA.maps_to`), not the answer, so it
  answers **live** from the profile and stays correct if the profile changes; open-ended prose
  still goes to the drafting path; company-specific/demographic never auto-map. `resolve_semantic`
  in the resolver + `answer_bank.classify_question` (robust parse of Claude's reply); the Profile
  tab shows mapped entries as "↔ Auto-answered from your profile" and preserves `maps_to` on save.
  **Verified:** office example + sponsorship/start-date classify correctly, no-type/company →
  None, live round-trip flips Yes→No on profile change, served JS `node --check`-clean.
- 2026-07-05 — **Tracker basic info + de-dup already-applied + smarter answer learning.**
  (1) Track records now capture company/role/location/remote/pay/source-URL from the discovered
  posting (`run_apply(meta=…)`) instead of scraping the ATS page title (rows were blank).
  (2) Discovery skips postings already in the tracker so re-runs don't re-apply to the same roles.
  (3) Resolver now answers current employer/title/degree/school/field/graduation from the résumé
  (were captured blank); only genuinely-unanswered questions are banked (not dropdown/format
  failures or ones naming the company); tighter company-specific detection; near-duplicate
  question dedup. (4) Fixed work-auth/sponsorship questions being answered with the applicant's
  city (work-eligibility now matched before location; "sponsor" verb mapped). Verified live on the
  Stripe embedded Greenhouse form.

- 2026-07-05 — **Discovery settings fully editable from the dashboard (no config-file editing).**
  New **Discovery-settings editor** at the top of the Discover tab ([web.py](applicationbot/web.py))
  covering **every** `DiscoveryFilters` field: target boards (ats + token, add/remove rows),
  filters (remote-only, min salary, title-exclude, experience-level checkboxes), matcher knobs
  (min-skills, top-n, skip-seen), aggregator keywords, and Adzuna key/country/pages. Backend:
  `GET /discovery` (current filters + the level taxonomy) and `POST /discovery/update`
  (`DiscoveryFilters.model_validate` → `save_filters`). Reuses the shared busy/Save-✓ pattern
  (UI Principle #5) and the existing card/field helpers. **Verified:** served page JS
  `node --check`-clean; full HTTP round-trip (GET → POST all fields → GET persisted) against a
  live server, and the saved config drives `apply_gates` correctly — the user's real
  `discovery.yaml` was backed up and restored byte-for-byte during the test.

- 2026-07-05 — **Experience-level discovery gate (decision 027).** New
  `DiscoveryFilters.experience_levels` coarse gate in [filters.py](applicationbot/filters.py),
  alongside `remote_only`/`min_salary`/`title_exclude`: keep only postings at the chosen levels
  — `internship`, `new_grad`, `junior`, `mid`, `senior`, `staff`, `manager` — detected from the
  posting **title** via word-boundaried regex (`_LEVEL_PATTERNS` + `detect_levels`). **Lenient**
  (user's choice): a title naming a *different* level is dropped, a title with no clear level
  passes to the qualification matcher (same "missing data → keep" rule as the salary gate).
  User values are normalized ("New Grad" → `new_grad`); unknown values ignored. Set in
  `profile/discovery.yaml` (example seeded in [examples/discovery.example.yaml](examples/discovery.example.yaml)).
  **Verified:** 15-title detection suite incl. false-positive traps (internal→manager not intern,
  leading→∅) all correct; lenient early-career gate keeps intern/new-grad/ambiguous & drops
  senior/manager; senior gate keeps senior+ambiguous & drops the rest; no-gate keeps all.
  Editable from the Discover tab (see the settings-editor entry above). *Optional later:*
  body/"X+ years" detection for level-less titles.

- 2026-07-05 — **Tracker basic info + de-dup already-applied + smarter question learning.**
  (1) **Track record now captures company/role/location/remote/pay/source-URL** from the
  discovered posting (reliable) instead of scraping the ATS page title (which left rows blank) —
  `run_apply(meta=…)` + `_record_dry_run` populate all columns; keyed on the posting URL.
  (2) **De-dup:** discovery now skips postings already in the tracker so re-runs don't re-surface
  or re-apply to the same roles (`tracker.seen_source_urls()`, `DiscoveryFilters.skip_seen=True`,
  surfaced as "skipped N already in tracker" in CLI + Discover tab). (3) **Learning refinements:**
  the resolver now answers current/previous **employer, job title, degree, school, field of study,
  graduation** from the résumé (were wrongly captured as blank "needs your answer"); capture only
  genuinely-unanswered ("no saved answer") questions, never dropdown/format-match failures or ones
  naming the company; tightened company-specific detection ("excited about {company}", etc.);
  near-duplicate questions collapse in the bank (punctuation/lead-in–insensitive dedup).
  (4) **Correctness fixes:** "Are you authorized to work in the location(s)…" / "…sponsor you…"
  no longer answered with the applicant's city — work-eligibility is matched before location, and
  "sponsor" (verb) now maps to the sponsorship field. **Verified** live on the Stripe embedded
  Greenhouse form (9→11 fields; employer/title fill; work-auth=Yes, sponsor=No) and unit tests for
  dedup, tracker columns, classification. *Remaining:* Greenhouse dropdowns still option-mismatch
  on some values (country "United States", degree "B.S. in Computer Science") — option-text matching.

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

- **Should bot-walled applications retry automatically, or stay one-click?** (decision 077 shipped
  the flag + manual "Try again"; auto-retry was deferred, not rejected.) Full automation
  (Guideline #0) argues the runner should re-attempt a `bot_wall` row on a later cycle without the
  user. Guideline #4 argues the opposite: a site that refused us must not be hammered on a timer —
  that *is* an abusive request pattern. Needed before building: a backoff (hours? next daily
  cycle?), a cap (how many refusals before a host is declared hopeless), and whether a hopeless
  host should be **dropped from discovery** so the pipeline stops queueing postings it can never
  submit (SmartRecruiters contributed ~298 of the 074 unlock — see the SmartRecruiters item in
  **Now**, which may settle this on its own).
- Tech stack and primary language.
- ~~Scraping strategy~~ — **resolved (decision 026):** no scraping; public ATS APIs
  (Greenhouse/Lever/Ashby) + Adzuna aggregator behind a pluggable source interface,
  qualification-driven matching.
- Resume-tailoring method (template + rules, LLM-based rewrite, or hybrid).
- Application-submission approach (headless browser form automation, per-site adapters).
- Storage for profile, postings, resumes, and application history (files vs. database).
- Config format for the user profile + filters — **partially resolved:** discovery filters
  built (decision 026, `profile/discovery.yaml`); full Configure-stage profile schema still open.
- ~~How the `dry_run` / armed state and global kill switch are represented and toggled~~ —
  **resolved (decision 035):** `profile/safety.yaml` (`armed: false` default +
  `max_submissions_per_run`) + `profile/KILL` kill file checked before every submit;
  `--dry-run` CLI override forces disarm.

Record each decision in [DECISIONS.md](DECISIONS.md) once the user chooses.

---

## Recently completed

- 2026-07-16 — **Apply: a searchable combobox the batch declined still gets its round-2 typeahead
  Claude pick — school picker now prefers the MAIN campus** (decision 080, follow-up to 033/079).
  After 079, School committed via the `substring` fallback (first fuzzy match), which can land on a
  branch campus. The live Greenhouse School field is an async search: its open list is the first 60
  schools alphabetically (never the applicant's), so the round-1 batch declines it and marked the
  label `picks_done` — which also suppressed Phase 2b, the article-stripped typeahead + Claude pick
  built for async school pickers. Fix: Phase 2b now runs on `gen_on` (generation + a value), not
  `use_claude` (which still respects `picks_done`), because its options come from the per-query
  async results, not the open list the batch saw. New async-picker fixture + two-pass test list the
  branch campus first: without the fix School = `…- Schuylkill Campus` (`substring`); with it =
  `Pennsylvania State University` (`option:claude`). Combobox/two-pass/required-dropdown/multipage/
  fillability/lever/determinism suites green.

- 2026-07-16 — **Apply: `aria-hidden` inputs are skipped — react-select's requiredInput mirror
  no longer hijacks its own dropdown** (decision 079). A SpaceX (Greenhouse) dry run left the
  **School** field on `Select…` while Degree/Discipline filled, yet the report logged School as
  filled (`source=resolver`, plain text). Root cause, found by driving the live form: Greenhouse
  renders each react-select as two inputs sharing one label — the real combobox and an
  `aria-hidden` `requiredInput` shadow. When the résumé value doesn't literally match an option,
  the combobox defers its pick to the batch and returns *without* marking the label done; the loop
  then reaches the mirror (empty `type` → free text), `.fill()`s it, and marks the label done — so
  round 2 never recommits the real selection and the field submits empty. Fix: one guard in
  `_fill_all_fields` skipping `aria-hidden="true"` inputs (never a fillable field), general to every
  react-select field/ATS. Verified live — School now commits through the combobox with a real
  option. New fixture + regression test (`tests/test_required_input_mirror.py`) fails without the
  guard and passes with it; combobox/two-pass/multipage/fillability/lever/corpus suites green.

- 2026-07-15 — **Track table: the Source URL *is* the link; editing moved behind an ✎**
  (decision 078 — supersedes the `↗` entry below). The `↗` made the URL openable but left it
  looking like plain text in a box: the only clickable target was a 12px glyph, while the
  obvious affordance — the URL itself — did nothing (UI Principle #1). The cell now renders an
  `<a>` whose text is the URL; `✎` swaps in the same input, and committing saves via `saveCell`
  and returns to the link. The `http(s)`-only guard and the save-then-sync ordering are
  inherited unchanged from the `↗`; only the cell re-renders, never the row (the "Saved ✓"
  span must survive the await — UI Principle #5).
  **Layout bug found and fixed:** a long URL is unbreakable text, so as a link it stretched the
  column to **583px** — past the 220px default and the resize handle — squeezing every other
  column (Company → 83px). An `<input>` never did this (small intrinsic width).
  `contain:inline-size` on the link keeps its text out of the table's intrinsic width so
  `table-layout:fixed` honours the `<col>` again; measured against a `git stash` baseline, the
  geometry is now **identical** (table 1370px, Source URL 98px, all 16 columns).
  Verified in the live UI on the real tracker: 18/18 rows are links with correct hrefs and zero
  inputs; a full ✎ → edit → "Saved ✓" → link-with-new-href round trip on row 21. Suite 375/375.
  Heeding the WAL note below, the probe's write was reverted **through the UI** (same write
  path, not a file copy) and confirmed directly against `applications.db`: row 21 holds its
  original URL, 0 rows contain the test value.
  ↳ **Open, deliberately not folded in:** the column renders at 98px, so links show truncated
  (`https://j…`, full URL on hover). Pre-existing — the input truncated identically, and
  `width:auto` + `table-layout:fixed` squeezes columns proportionally so the 220px default is
  ignored even at baseline. Fix by widening the default or labelling the link
  `smartrecruiters.com/…/87644936` instead of the raw URL.

- 2026-07-15 — **Track table: Source URL is a real link.** The column rendered the URL as a
  plain editable input; it now keeps that input (the cell has always been editable — a
  manually-added row needs a way to set its URL, Guideline #7) and adds a `↗` anchor
  (`target=_blank`, `rel=noopener noreferrer`) beside it, shown only for an `http(s)` value —
  an empty cell has nothing to open, and rejecting other schemes stops a stored
  `javascript:`/`data:` string becoming a clickable payload. The link re-syncs in place as the
  cell is edited (a stale link would quietly open the **wrong** posting); a first cut called
  `loadTrack()` instead, which re-rendered the row and detached the span `saveCell` writes
  "Saved ✓" into **after** its await — losing the confirmation and silently swallowing save
  errors (UI Principle #5). `saveCell` now returns success so the link only follows a real
  save. Verified by driving the live UI: link appears/vanishes/re-points as the value changes,
  "Saved ✓" still shows, `node --check` clean, suite 375/375.
  ⚠ **Process note for future agents:** that UI probe wrote a test URL into the **real**
  `applications.db`, and the backup/restore did not undo it — the DB is in **WAL mode**, so
  copying `applications.db` alone lets `applications.db-wal` replay the edit back. Row counts
  matched, which hid it. Repaired and verified row-by-row against the pre-probe copy (0 diffs).
  **Never point a UI probe at the real tracker**: `tracker.list_applications(path=DEFAULT_DB)`
  binds its default at import, so reassigning `tracker.DEFAULT_DB` does **not** redirect it —
  a temp-DB probe needs the server to be given the path, or restore with
  `PRAGMA wal_checkpoint(TRUNCATE)` and diff every row.

- 2026-07-15 — **Bot-walled applications parked as `bot_wall`, retryable later** (decision
  077). Fixes two bugs **076 introduced**, both proved by the user's own live run — real
  tracker **row 21** came back `status='dry-run'`, `blocked_kind='captcha'`. (1) The wall's
  vendor host is literally `captcha-delivery.com`, so `classify`'s `"captcha" in errors` scan
  mis-parked an IP block as "solve it in the open browser" — there's no puzzle and a headless
  run has no browser; now a **structured `ApplyReport.bot_wall`** is classified first (no
  prose-matching — that *was* the bug). (2) A walled run never reaches submit, so it stayed a
  `dry-run` row — which `web.py` advertises as **"ready to apply"**; now recorded `blocked`,
  so it parks. New `parking.BOT_WALL` is resumable **by time, not the user** (`resolve=""`,
  verb "Try again"); `/parked` + `_reapply_worker` are kind-agnostic, so flag→list→retry
  needed **no new plumbing**. Copy fixed where the kind broke it: note says "Refused" not
  "Dry-run: 0 field(s) filled"; the runner no longer tells a wall it's "waiting on you";
  the card no longer calls a refusal a "site error". Verified **on the real row** + the card
  **screenshotted in the real UI**; both fixes mutation-checked. Suite **374/374**.

- 2026-07-15 — **Agentic nav fallback + host-keyed nav recipes; bot walls reported as
  refusals** (decision 076). Chased the reported "dry-run couldn't find the application"
  (tracker row 21, SmartRecruiters, 0 fields) to **three** distinct causes, not one:
  `detect_ats` didn't know SmartRecruiters (the gap **074 flagged**); the reveal only matched
  `/\bapply\b/i` while the real control says **"I'm interested"**; and — found only by driving
  the real URL — the site answers **403 + a DataDome bot wall** in an **iframe over an empty
  body**, which the run misreported as "form did not load". Now: `_bot_wall_evidence` walks
  every frame and a wall yields a precise refusal that **suppresses the agent** (an agent hits
  the same wall from the same IP; aiming one at a bot wall is evasion — Guideline #4). The
  requested learner ships as `nav_recipes.py` + `run_agent_nav`, mirroring 061/063: opt-in
  `nav_agentic: true` (**off by default**, replay always free), a Claude+MCP worker reaches the
  form **once**, and the route is distilled **by DOM diff** into a **PII-free, committed,
  host-keyed** recipe — so one learned posting unblocks the whole site with no Claude after.
  **The live Claude-over-MCP step flagged by 061/063 is now actually driven** (real worker →
  form → learned `"Join our team"` → replay with the agent asserted to run exactly once).
  18 tests, suite **369/369**. ⚠ **Unconfirmed:** the SmartRecruiters fix itself — this build
  environment's cloud egress IP is the exact IP DataDome named, so every live attempt 403s
  here. See **Now**.

- 2026-07-15 — **Mailbox test isolation + secrets never render** (decision 075). The one
  long-standing suite failure: `test_load_config_needs_all_three` asserted `load_config(...)
  is None`, but `load_config` prefers a stored **link** over env (decision 057) and defaults
  to the real `profile/mailbox.yaml` — so on a linked machine it returned the live config,
  failed, and pytest printed the **real Gmail app-password**. Test now pins
  `backend=_FakeKeyring(), path=_link_path()` (the idiom the rest of the file already used);
  mutation-checked that it still catches its regression. Separately, `password`/
  `refresh_token`/`client_secret` are now `field(repr=False)`, so no traceback/log/diff can
  print a credential again — values still work, `asdict()` unchanged. Suite **351/351 green**
  (first fully-green run). Not a product bug: link-over-env precedence was always intended.

- 2026-07-15 — **GitHub repo job boards: drop-in feeds + Workday/SmartRecruiters unlocked**
  (decisions 073, 074). We already scraped GitHub boards (SimplifyJobs, decision 031); the
  investigation measured the funnel first and found the constraint was elsewhere. Of **3,459**
  active postings in the two feeds, only 1,130 passed `_CURATED_ATS` and only **40**
  (`max_resolve`) were resolved+judged — 97% discarded. **(a)** `_CURATED_ATS` now includes
  **workday + smartrecruiters**, recovering **755 + 298** postings from feeds we already fetch,
  pointing at ATSs Apply can already fill. Workday needed no new resolver: it ships JSON-LD in
  its initial HTML, so `enrich.fetch_full_jd` resolves it on a plain GET (**10/10 live, json-ld
  tier, no browser, no LLM call**) — my Playwright caveat was disproved by the spike.
  **(b)** `early_career.feeds` accepts any GitHub board publishing the SimplifyJobs
  `listings.json` schema as a **bare URL** — no code, no field mapping (the schema is the
  de-facto standard; fields were already read via `.get()`). Ranking stays global across feeds
  so `max_resolve` remains a whole-run budget. Bad feeds fail loudly naming the feed + fix; the
  web UI gained the field (without it, saving settings would have silently wiped `feeds`).
  Verified live end-to-end: dropped-in `vanshb03/New-Grad-2026` → 6 full-JD Postings, 0 errors,
  including a Workday JD (Northrop Grumman, 5,521 chars). `tests/test_curated_feeds.py` (13).

- 2026-07-14 — **Fix "Why Ramp?" left blank — short/optional "Why &lt;Company&gt;?" prompts now
  drafted** (decision 071): the field is a short, OPTIONAL single-line `<input>`, so it failed
  every draft gate — not a textarea, under the 25-char open-ended threshold, matched no
  `_COMPANY_SPECIFIC` phrase, and not required. Fix in `answer_bank.py`: `is_company_specific`
  now matches any prompt opening with "why " (dynamic company name can't be listed → also
  excluded from mapping/caching), and `is_open_ended` treats company-specific prompts as
  draftable even when short/single-line. Now grounded-drafted (résumé + company + JD) whenever
  company/JD context exists — the pipeline sets both. Refines decision 067 (was: draft short
  fields only when required). Verified via live Ramp-form field probe + gate/end-to-end tests
  (`test_determinism_gates.py`, `test_required_draft.py` contract updated).
- 2026-07-14 — **Fix "Application form did not load" on Ashby (Ramp) — SPA reveal-click timing**
  (decision 070): the "Apply for this Job" reveal-click was attempted once *before* the form-load
  poll loop, but Ashby mounts that control after `domcontentloaded`, so the click fired on a
  not-yet-existing button, never navigated to the form's `<posting>/application` route, and the
  loop timed out at 25s on the empty posting page. Moved the reveal-click *into* the poll loop
  (retries each pass until the control appears; `revealed` latch avoids re-clicking). General
  fix for any late-mounting SPA ATS, no special-casing. Verified live against the reported Ramp
  URL (loads `/application`, 12 fields, 0 errors) + new `tests/test_open_application_form.py` (2,
  regression fails on old code with the exact error, passes on the fix). `apply.py` only.
- 2026-07-14 — **Auto-apply loop: "re-prepare postings I've already seen" opt-in + accent-bar
  fix** (decision 069 follow-up): a checkbox on the loop panel starts it with `rescan=True`,
  re-preparing (re-tailor → PDF → dry-run fill) the whole last-scored set — including
  already-dry-run postings — while **reusing each one's cached fit score** via new
  `pipeline.cached_matches` (no board re-search, no Claude re-judge) **and reusing the
  already-tailored PDF** when the résumé/profile links are unchanged (a `<pdf>.stamp` content
  hash — new `pipeline.tailor_stamp` + `resume_store.read/write_stamp`). PDF reuse now applies
  to **every dry run** (Test-run button, loop prepare, autonomous/CLI dry-run), keyed on the
  gate being unarmed; a real armed submit always re-tailors. So any repeated dry run of an
  unchanged posting spends zero Claude tokens — only the local re-fill runs. A second loop-panel
  checkbox ("Re-tailor from scratch" → `force_retailor`) is the escape hatch to regenerate
  anyway. Rescan is a bounded one-shot; bails with an actionable message when nothing is cached.
  Also fixed the loop/parked panels' accent bar overlapping the text (`padding-left:18px`).
  `tests/test_autoloop_web.py`, `tests/test_discovery_cache.py`, `tests/test_rescan_reuse.py`
  (new) green; suite 331 pass.

- 2026-07-09 — **Readiness closers + ITAR gates auto-answered** (decision 044): "Are you
  up for it?" / "Are you ready?" / "Does this sound like you?" resolve to Yes (guarded
  keyword rule + `role_commitment` classifiable type for rephrasings; logistical "ready
  to start/relocate" phrasings excluded). ITAR/export-control gates and security-clearance
  *eligibility* resolve to Yes only when `us_citizen` is True (`itar_us_person` type +
  status-dropdown hints; "itar" whole-word matched — substring hit "mil-ITAR-y");
  *holding* a clearance stays captured. 11 new corpus cases; suite 132/132.

- 2026-07-07 — **Semantic answer-bank matching** (decision 036): a saved answer is now
  reused for any *rewording* of its question, not only the exact phrasing it was banked
  under. On a literal bank miss, Claude matches the question against the banked Q→A pairs
  by answer-fitness (`answer_bank.match_banked_question`), wired into both
  `resolve_semantic` and `freetext_answer` (short text fields previously got no semantic
  fallback at all). A hit is cached as a bank alias, so repeats match literally with zero
  Claude calls. Verified offline (`tests/test_bank_semantic.py`, all 9 test modules pass)
  and live: "Years of React experience" fills from a banked "How many years of experience
  do you have with React?"; unbanked questions still go to the needs-attention queue.
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

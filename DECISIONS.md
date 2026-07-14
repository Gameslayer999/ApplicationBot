# DECISIONS.md — Architecture & Tooling Decisions

> Every significant choice — architecture, tooling, service model, data layout,
> integration method, or a reversal of a prior decision — is logged here with its
> context, the options considered, the choice, and the reasoning (Agent Guideline #9).
> Code and scripts capture *what* the system does; this file captures *why*.

---

## Decision Index

| # | Date | Decision | Status |
|---|------|----------|--------|
| 001 | 2026-07-03 | Primary language: Python (polyglot later if needed) | Accepted |
| 002 | 2026-07-03 | Resume model: structured source-of-truth + LLM tailoring | Accepted |
| 003 | 2026-07-03 | Test data: real job descriptions collected as static fixtures | Accepted |
| 004 | 2026-07-03 | LLM provider/model: Claude (`claude-opus-4-8`) via the Anthropic SDK | Superseded by #011 |
| 005 | 2026-07-03 | PDF → YAML via Claude's native PDF reading; OpenDataLoader as optional fallback | Accepted |
| 006 | 2026-07-03 | Preserve the source resume's format (structure + section order); PDF/DOCX render later | Accepted |
| 007 | 2026-07-03 | Direction: the source of truth becomes a full user *catalogue* (superset) to select from | Accepted (direction) |
| 008 | 2026-07-03 | Pluggable tailoring backends (Claude / Ollama / rules) with `auto` selection | Accepted (Ollama later dropped) |
| 009 | 2026-07-03 | Review UI: a local stdlib web app (no deps, localhost), renders resume as HTML | Accepted |
| 010 | 2026-07-03 | Claude sign-in from the site drives the `ant auth login` OAuth flow (not a custom OAuth client) | Reversed by #011 (that's the API, not the subscription) |
| 011 | 2026-07-03 | Use Claude via the Claude Code CLI (subscription), not the Anthropic API/SDK | Accepted |
| 012 | 2026-07-03 | Configurable length budget (`pages`), instructed to Claude and hard-enforced | Accepted |
| 013 | 2026-07-03 | Catalogue storage: structured file + local relevance pre-selection to keep Claude prompts small | Accepted |
| 014 | 2026-07-03 | Parallel agents: git-ignored file bus + canary notify + Cursor hooks | Accepted |
| 015 | 2026-07-03 | LinkedIn: import the official data export (CSV), not live OAuth/scraping | Accepted |
| 016 | 2026-07-03 | Apply stage: per-ATS Playwright form automation, autonomous-first with an exception queue; browser extension later | Accepted |
| 017 | 2026-07-04 | Apply stage: use the ATS's own native autofill first, fill only the still-empty fields with our resolver; MyGreenhouse via stored credentials + auto-login | Accepted |
| 018 | 2026-07-04 | Self-improving answer bank: cache learned/generated answers for reuse; draft open-ended questions with Claude (grounded); never cache company-specific ones | Accepted |
| 019 | 2026-07-04 | Codebase index: a stdlib-`ast` structural repo map (not a vector DB) for faster agent orientation | Accepted |
| 020 | 2026-07-04 | Web UI: one unified **Profile** screen (merges "Résumé data" + "Apply profile") with collapsible entry cards | Accepted |
| 021 | 2026-07-04 | Consistent waiting/status feedback for every async action (spinner + label + elapsed; disabled trigger; surface dropped input) | Accepted |
| 022 | 2026-07-04 | Apply profile: structured Country/State dropdowns + City text, and a start-date preset/date-picker — UI-only, stored formats unchanged | Accepted |
| 023 | 2026-07-04 | Tailoring quality (concrete + quantified bullets, no fabrication) + per-entry "why tailored" rationale shown in a click-to-reveal Review panel | Accepted |
| 024 | 2026-07-04 | Track stage: local SQLite store (`applications.db`) as system of record + editable Track tab in the web UI; optional Sheets/CSV export later | Accepted |
| 025 | 2026-07-04 | Tailoring speed/quality tiers (fast/balanced/max) — extended thinking off by default; ~2 min → ~35s | Accepted |
| 026 | 2026-07-04 | Discover stage: qualification-driven, pluggable sources (public ATS APIs + one aggregator), hybrid keyword→Claude matcher, testing-mode end-to-end before autonomous | Accepted |
| 027 | 2026-07-05 | Experience-level discovery gate: title-based detection, lenient (drop a clearly-different level; keep undetected) | Accepted |
| 028 | 2026-07-05 | Semantic question classification: on a keyword miss, Claude maps a novel question onto a known structured field type; cache the mapping (answer stays live) | Accepted |
| 029 | 2026-07-05 | Persist tailored résumé PDFs to a stable git-ignored store (not `$TMPDIR`); bound growth via per-posting overwrite + cascade delete + size cap | Accepted |
| 030 | 2026-07-05 | More discovery sources: broaden the ATS layer (SmartRecruiters + Recruitee) over aggregators; reject hiring.cafe (now auth-gated) + LinkedIn (Guideline #4) | Accepted |
| 031 | 2026-07-05 | Early-career discovery: SimplifyJobs new-grad/intern JSON feeds → rank by title-relevance → resolve full JD for top-K via linked ATS; curated postings judged first | Accepted |
| 032 | 2026-07-05 | Workable source + aggregator→ATS bridge (resolve redirect → detect ATS → rewrite apply_url + upgrade snippet to full JD); partner APIs (SEEK/Indeed/LinkedIn) out | Accepted |
| 033 | 2026-07-05 | Self-improving dropdown resolver: Claude picks the option (guarded) when literal/hint match fails, and the value→option mapping is learned + reused without another Claude call | Accepted |
| 034 | 2026-07-06 | Strip the headless Claude session (74x less overhead/call), batch fit judging (5 postings/call on Sonnet), schema-enforced JSON output for tailor + judge | Accepted |
| 035 | 2026-07-06 | Submit stage: safety = `profile/safety.yaml` (`armed` default false + per-run cap) + `profile/KILL` kill-switch file; submit-first build order; verify on local HTML fixtures, not live dry-runs | Accepted |
| 036 | 2026-07-07 | Semantic answer-bank matching: on a literal bank miss, Claude matches the question against banked Q→A pairs (answer-fitness, not topic); hit reused + cached as an alias | Accepted |
| 037 | 2026-07-07 | Discovery snapshot cache: save the whole ranked result to git-ignored `profile/discovery_cache.json`; reuse it (skip board search + Claude judge) when younger than `cache_ttl_hours` (12h) and the résumé/boards/filters fingerprint matches; skip_seen re-applied on hit; `--fresh` forces re-search | Accepted |
| 038 | 2026-07-07 | Salary-expectation: when a posting advertises a pay band, fill its **midpoint** instead of the static profile figure (which undersold below-band postings); parse the band from the structured compensation string then the JD body; fall back to `desired_salary` when no band is advertised | Accepted |
| 039 | 2026-07-07 | Dynamic salary fallback when **no** band is advertised: a market estimate for (title, location, seniority) that Claude + Adzuna cross-check (agree≤20% → mean; else take the **lower**), cached per (title, location) in git-ignored `profile/salary_cache.json` (30-day TTL) and invalidated when a real advertised band later shows it's >40% off; degrades to Claude-only without Adzuna keys, then to the stored `desired_salary` | Accepted |
| 040 | 2026-07-09 | Autofill determinism hardening: a 65-case resolver regression corpus pins `resolve()`/`option_hints()`; `valid_mapping` gates every learned `maps_to` at write time (and generic boolean dropdown aliases are never learned); the 3 fill-time Claude decision calls are `--json-schema`-constrained (enum/index, no free-text parsing); Claude never decides while a dropdown menu is open (read → close → decide → recommit by exact text), and every combobox fill records its matched tier (`option:literal/learned/hint/claude/substring`) | Accepted |
| 041 | 2026-07-09 | Two-pass page fill: round 1 fills deterministically and DEFERS unresolved decisions; ≤3 batched schema-constrained Claude calls (classify / bank-match / dropdown-picks) adjudicate them; round 2 is the same deterministic loop over the injected results — Claude cost per PAGE, not per field. Live AppLovin dry-run exposed + fixed fabricated-salary drafting: numeric-fact questions are never drafted, the salary rule falls through to the bank, drafted numeric answers pruned | Accepted |
| 042 | 2026-07-09 | Tailoring token diet + one-page guarantee: the Claude backend returns a **TailorDelta** (entries by index + rewritten bullets; schema 4.8k→1.5k chars) reconstructed in Python so orgs/dates/education/certs are copied verbatim — never mangled, never paid as output; résumé JSON compacted + JD trailing-boilerplate trim (8k cap) on the input side; and `pdf.fit_to_pages` renders → measures → trims (least-relevant first, floors, user-facing note) until the PDF **actually** fits the page budget — wired into `tailor_resume` for all backends and surfaces | Accepted |
| 043 | 2026-07-09 | Four adoptions from the ai-job-search survey (all zero-token at run time): `ats_check.py` verifies every exported PDF's text layer (readable name/email/phone, JD keyword coverage split covered vs dropped-by-tailoring); `archive.py` snapshots each application (posting text + exact PDF + fill report) under git-ignored `profile/applications/`, freezing a dated copy on real submission; the fit judge returns **skills/experience/seniority** dimensions and `fit_score` is computed in code via `FIT_WEIGHTS` (.45/.35/.20); tracker gains interview/offer/rejected/no-response statuses + a `fit_score` column (migrated) + `calibration_report()` — response rate by fit band to tune `min_fit` from real outcomes. NOT adopted: reviewer-agent tailoring pass (2× cost vs 034), LaTeX, LinkedIn scraping (Guideline #4) | Accepted |
| 044 | 2026-07-09 | Readiness/commitment closers ("Are you up for it?", "Are you ready?", "Does this sound like you?") auto-answer **Yes** — applying IS the commitment — via a guarded keyword rule (start/relocate/remote/travel/when phrasings excluded) + a `role_commitment` classifiable type for rephrasings. ITAR/export-control gates and security-clearance *eligibility* auto-answer **Yes** only when `us_citizen` is True (a citizen is a "U.S. person"); non-citizens fall through to the bank/capture (green-card holders also qualify — the profile can't derive it), and *holding* a clearance stays captured. "itar" matched as a whole word (substring hits "mil-ITAR-y") | Accepted |
| 045 | 2026-07-09 | Project impressiveness ranking: Claude auto-scores each résumé project 1–5 on technical depth/difficulty (`impact.py`, one subscription-CLI pass, cached in `Project.impact` in resume.yaml); the Profile UI orders projects by that score, shows a ★ badge, and adds a "Rank by impressiveness" button. Selection stays **relevance-first** — impact only breaks ties in `catalogue.select_relevant`, the rules engine, and the tailoring prompt — so the résumé leads with the strongest work without forcing off-topic projects on | Accepted |
| 046 | 2026-07-09 | Discovery feedback loop (`fit_learning.py`): every judged posting is appended to git-ignored `profile/fit_history.jsonl` (fit + per-dimension scores + detected level + board). Before each run a `Predictor` (shrinkage-blended level/board means, inactive below 5 rows) **re-ranks the free keyword pre-filter by predicted fit**, so the judge's scarce `top_n` slots go to postings most like past winners instead of the verbose senior JDs a raw keyword count floats up — zero extra Claude tokens. `analyze()` diagnoses why postings clear or don't (dimension breakdown, per-level/board segments, recurring missing = résumé gaps) and recommends auditable edits: narrow `experience_levels` to winning bands, lower `min_fit` to best-achievable when nothing cleared, drop dead boards. Surfaced in the CLI + a Discover-tab panel with one-click apply for `experience_levels`/`min_fit`. Complements 043's `recommended_min_fit` (which only ever RAISES the bar) by steering the *supply* of high-fit postings | Accepted |
| 047 | 2026-07-09 | JSON-LD → CSS → LLM enrichment cascade (`enrich.py`) adopted from the ApplyPilot survey: `fetch_full_jd`/`enrich_from_html` read a full JD off any posting page — tier 1 parses `<script type=ld+json>` `JobPosting` structured data (ToS-clean, the data Google for Jobs indexes), tier 2 a stdlib-`HTMLParser` description/apply-link scraper, tier 3 an **opt-in** Claude extractor (off by default, 30k-capped, schema-constrained). Reusable module, not a one-off source: new `discovery.CareerSiteSource(career_sites)` consumes it (ATS auto-detected from the apply URL so a JSON-LD link routes into the right Apply adapter), and it can later backfill JD wherever an ATS resolver comes up empty. `fetch_json` refactored onto a shared `fetch_text` (same politeness/retry); no new dependency. Live-verified on a real Lever page (5,099-char JD), SPA degrades to empty | Accepted |
| 048 | 2026-07-09 | ApplyPilot adoptions #3/#4: `python -m applicationbot.doctor` runs six read-only readiness checks (Claude CLI signed in · Playwright Chromium installed · résumé loads · applicant profile loads · discovery has ≥1 source · submit-safety state) and prints each with ✓/✗/⚠ plus a one-line actionable fix on failure; exit 0 iff every *required* check passes (missing profile = optional ⚠). Runner gains `--continuous [--interval MIN]` (default 30): the per-cycle discover→judge→apply work is a `run_cycle()` closure, driven by an injectable module-level `continuous_loop(run_cycle, gate, interval_s, _sleep)` that repeats until the KILL file / Ctrl-C / a fatal Claude-sign-in `stop`, waiting via the existing kill-abortable `_wait_for_reset`. Cycles reuse the discovery cache unless `--fresh`; `skip_seen` keeps applied roles out; dry-run/safety unchanged | Accepted |
| 049 | 2026-07-09 | CAPTCHA auto-solving (`captcha.py`, CapSolver) — **user-directed over my survey rec to reject** (Guideline #4: solving a CAPTCHA to submit circumvents an anti-bot control / may breach site ToS); built **fenced** so it can't run silently. `apply._attempt_submit` calls a gated hook only after `may_submit()` (armed-only ⇒ dry-run never solves). Five gates: off by default (`captcha.enabled` in safety.yaml), per-site opt-in (`captcha.sites` host-suffix allowlist), armed-only, key from env `CAPSOLVER_API_KEY` (never YAML), every attempt logged. Any unmet gate ⇒ **blocked** outcome with the fix, never a silent bypass. Detects reCAPTCHA-v2/hCaptcha/Turnstile, solves via CapSolver createTask/getTaskResult (urllib, no new dep), injects the response token. ApplyPilot's README claims CapSolver but its code has none (detect+fail) — from-scratch build. `doctor` reports the state; `_attempt_submit` back-compat via `solve_captcha=None` | Accepted |
| 050 | 2026-07-09 | Workday hybrid (Option C, approved): deterministic `data-automation-id` adapter first, agentic worker only for unrecognized pages (distilled to replayable recipes), final submit always behind the Python `SafetyGate` — inverts ApplyPilot's all-agentic, prompt-only-safety approach. Enabling fact: Workday automation ids are stable across every tenant. M1 (login + standard fields, dry-run only) started: brick 1 `credentials.py` (per-tenant passwords in the OS keychain via **keyring** [new dep], never YAML; git-ignored tenant→email index for listing; CLI list/get/delete); brick 2 `workday.py` `fill_standard_fields` maps stable ids (legalName/city/email/phone) to profile-first-then-résumé values, dry-run, verified on a local Workday-shaped fixture headless. Settled: bot-owned email for account creation but all tenant passwords stored; shared committed recipe library; custom questions reuse `AnswerResolver`. Unwired (`_is_fillable` still drops Workday) until brick 5 — no current flow changes. Remaining M1: wizard nav + dropdowns, account-create/sign-in + IMAP, apply dispatch | Accepted |
| 051 | 2026-07-09 | AutoApply-AI survey + adoption #1 (park & resume blocked applications), M1+M2. Survey of Rayyan9477/AutoApply-AI (full-stack FastAPI+React+Redis on browser-use): adopted #1 (park/resume), #3 (deterministic ATS pre-score), #4 (funnel analytics); **rejected #2 (Exa AI semantic discovery — paid API, overlaps `enrich.py`)** and the whole FastAPI/React/Redis/Postgres/Prometheus/LiteLLM stack (heft; conflicts with simplicity-first + Claude-only decision 004). **M1:** pure `parking.classify(report)→ParkReason` maps a stalled fill to a user-actionable kind (needs_answer / login / captcha / form_rejected / site_error) + UI deep-link target + resumable flag; tracker gains a `blocked` status + `blocked_kind`/`blocked_detail` columns (additive migration) + `parked_applications()`; `_record_run` parks an armed-blocked (or required-unanswered) fill as a `blocked` row carrying the reason instead of a silent `dry-run`, and a resolved re-run clears it → `applied`. **M2:** `GET /parked` + a Discover-tab "Applications waiting on you" panel of Resolve cards deep-linking to the fix (Profile "Needs your answer" for needs_answer); `runner._report_parked` names parked apps after each cycle; a "Re-apply (dry-run)" button POSTs `/parked/reapply` → `_reapply_worker` re-drives the deterministic fill on the same URL with the stored PDF + a fresh resolver (reusing the test-run progress panel), **always dry-run** (armed runner stays the only submit path, Guideline #3). Dependency-free resume — NOT AutoApply-AI's Redis BLPOP/RPUSH. Remaining: armed one-click resume + a credentials UI for the login target | Accepted |
| 052 | 2026-07-09 | AutoApply-AI adoption #3: deterministic multi-factor pre-score (`ats_score.py`, zero tokens) orders the Claude judge queue. `ats_prescore(resume, title, jd_text)` → 0-100 from skills (matched-count saturated at 6) + experience (candidate career-span years ÷ the JD's floor "N years" bar) + education (candidate degree rank ÷ the JD's floor degree, HS=1…PhD=5) + title-keyword overlap, weighted .40/.30/.20/.10, renormalized over the factors the JD actually states (missing requirement ≠ zero, same as `weighted_fit`). `matching.keyword_rank` computes it (reusing the keyword pass's matched count — no re-scan), stores it on `Match.ats_score`, and ranks survivors by it instead of the raw overlap count; the predictor-active path uses it as the tiebreak below predicted fit. **Claude stays the final judge — unchanged**; this only changes WHICH `top_n` get judged, fixing decision 046's failure where a verbose senior JD's larger keyword overlap crowds early-career-fit roles out of the judged set (the experience factor now sinks the over-bar role cheaply). Rejected the surveyed scorer's required-vs-preferred skill split (not extractable from raw JD text) in favour of a saturated overlap count; cache-safe (`ats_score` defaults 0 on pre-052 snapshots). Live drive: a 7-yr full-stack résumé now leads with the Full-Stack role (ats 87) over a Staff role with higher keyword overlap (6 vs 4, ats 86), nurse posting gate-dropped | Accepted |
| 053 | 2026-07-09 | Workday M1 brick 4 (account create/sign-in + email verification). `mailbox.py`: IMAP reader for a dedicated bot inbox — `extract_verification` (pure) pulls a portal-looking verification link or a 6–8 digit code; `fetch_verification`/`wait_for_verification` (injected `_connect`/`_sleep`, tested against a fake IMAP) get the newest matching-sender message; creds from env `MAILBOX_IMAP_HOST/EMAIL/PASSWORD` (secrets, never YAML). `workday.py`: `sign_in`/`create_account` (`:visible`-scoped, reveal-create-form + tick-terms) + `generate_password` (secrets, complexity-meeting); `ensure_account` orchestrates stored⇒sign-in else create-on-**bot-email**⇒**persist immediately** (never lose a password)⇒verify via mailbox. Unwired until brick 5 — no current flow changes. 16 offline tests (fake IMAP + `workday_account.html` fixture); live step flagged (real inbox + tenant). Full suite 237/237 | Accepted |
| 054 | 2026-07-09 | AutoApply-AI adoption #4: discovery→offer funnel on the Track tab. `tracker.funnel_report()` counts applications reaching each stage of a shrinking funnel — Discovered ⊇ Filled (dry-run/blocked/submitted) ⊇ Applied (submitted) ⊇ Responded (a human replied, incl. a rejection; `no-response` excluded) ⊇ Interview ⊇ Offer — from each row's current status (sets nested so it's monotone despite storing only the latest status), with the conversion from the previous stage. Served in `/track`, rendered as labeled bars above the Track table, and a `tracker funnel` CLI command. Read-only over existing data; no schema change | Accepted |
| 055 | 2026-07-09 | Feed the deterministic pre-score (decision 052) into the fit-learning `Predictor` (decision 046). Each judged posting's `ats_score` is now stored in `fit_history.jsonl`; the predictor learns a third shrunk bucket — the pre-score band (width 20 → five bands) — averaged with the level + board estimates, and `matching.match` passes `m.ats_score` to `predict`. This **calibrates** the heuristic against real Claude verdicts: a pre-score band that historically judged low is tempered rather than trusted. Fully back-compatible — pre-053 history lacks `ats_score`, so `_prescore` is empty and `predict` falls back to the exact level+board average; the `ats_score` arg is optional so existing callers/tests are unchanged. Verified: high vs low band separation, misleading-band tempering (ats 90→pred 43 < ats 40→pred 52), old-history no-op. Full suite 244/244 | Accepted |
| 056 | 2026-07-09 | Seen-openings ledger (`discovery_seen.py`) so a dry-run/list preview shows only NEW openings on a re-run. Root cause of the "same openings every time" report: the snapshot cache (037) returns the identical result within its window AND even a `--fresh` search finds the same board postings, while `skip_seen` drops only postings in the *tracker* — which a preview never writes to. New git-ignored `profile/discovery_seen.json` records the canonical URL of every posting a preview surfaces; `discover_and_match(only_new=True)` hides already-shown matches then records the survivors, layered on top of the cache (which still holds the FULL ranked result) and `skip_seen`, re-applied fresh each run. Kept SEPARATE from the tracker (ledger entry = "shown once", tracker row = "acted on") so previewing never pollutes application history/calibration. On by default for the CLI list path (`--all` shows everything, `--reset-seen` / `python -m applicationbot.discovery_seen clear` forgets) and the web testing worker (normal run = new-only; "Re-search fresh" = show all). **Off by default so the autonomous runner is unaffected** (it relies on tracker `skip_seen` from real applies). 6 new tests; full suite 250/250 | Accepted |
| 057 | 2026-07-09 | Link the bot email inbox (secure store + Profile-tab UI + CLI + doctor). Password → OS keychain (`keyring`, service `applicationbot-mailbox`); host/email/port → git-ignored `profile/mailbox.yaml`. `mailbox.save_link/load_link/clear_link/link_status`; `load_config` prefers a stored link then falls back to env (`MAILBOX_*`); `test_connection` does a real IMAP login + INBOX-select returning an actionable (ok, msg); `suggest_host` guesses from the email domain. Web: a "Bot email (Workday verification)" Profile panel (`GET /mailbox`, `POST /mailbox/link|unlink`) whose Link & test **tests before it saves — bad creds never stored**; CLI `python -m applicationbot.mailbox link|status|test|unlink`; `doctor` reports linked/unlinked. Mirrors the keychain-for-secret pattern (050), avoids the plaintext-YAML anti-pattern. Back-compat: env-only path unchanged. 8 tests; served JS node-clean; endpoints driven live; `profile/mailbox.yaml` git-ignored. Full suite 257/257. Unblocks brick 5 | Accepted |
| 058 | 2026-07-09 | Per-click armed resume for parked applications (park & resume M3, extends 051). A "Submit for real ▶" button on each resumable parked card really submits THAT one application — a **second, per-application arming path** the user approved, independent of `profile/safety.yaml`'s global `armed` flag (green-light one reviewed application without arming the whole autonomous runner). `_reapply_gate(arm)` builds a one-shot `SafetyGate(armed=True, max_submissions_per_run=1)`; `_reapply_worker(arm=True)` passes it to the existing `run_apply` armed path (pre-submit required-field gate + confirmation detection + tracker `applied`, decision 035). Safety: the global `profile/KILL` still halts it (`may_submit`); a client `confirm()` names the company; and since a POST now fires an irreversible submit, the armed branch of `/parked/reapply` requires a same-origin request (`_same_origin`) to block a drive-by cross-site submit. Dry-run re-apply (arm=false) unchanged. 6 tests (gate armed/cap/kill, same-origin matrix, route cross-origin 403 before any run); full suite 260/260; drove KILL-halts-armed-gate live | Accepted |
| 059 | 2026-07-09 | Workday M1 brick 5 (end-to-end wire-in, dry-run) — **M1 complete**. `workday.apply_workday` orchestrates start_application (Apply → Apply Manually) → `ensure_account` (053) → résumé upload → `fill_wizard` (bricks 2–3), and **never submits** (no armed/submit branch in the Workday path). `run_apply` routes `ats == "workday"` to it instead of `_open_application_form`/`_fill_all_pages`; the non-Workday path is byte-identical under `else`. `pipeline._is_fillable` now allows Workday and the aggregator bridge marks resolved Workday `auto_applyable=True`, so Workday postings reach the matcher + adapter instead of being dropped; tracker logs a `dry-run` row unchanged. Verified end-to-end on new `workday_full.html` (job→Apply→Apply Manually→account create→3-page wizard→Review) headless: account created+stored, fields+dropdowns+résumé filled across 3 pages, Submit NEVER clicked; + dispatch + `_is_fillable` tests; updated the 035 fillability test. Full suite 264/264. Live step flagged (real tenant). Next: M2 agentic fallback, M3 armed submit | Accepted |
| 060 | 2026-07-09 | MyGreenhouse password moved from plaintext YAML → OS keychain (closes an audit item; Guideline #12). Was: `ApplicationProfile.greenhouse_password` stored plaintext in `profile/application_profile.yaml` AND sent to the browser on every `GET /profile`. Now: `apply_profile.set/get_greenhouse_password` store it in the keychain (`keyring`, service `applicationbot-greenhouse`) mirroring credentials.py/mailbox.py (050/057); the email stays in the YAML (non-secret). `save_profile` never writes the password; `GET /profile` strips it and returns `greenhouse_linked: bool`; `/profile/update` routes a typed password to the keychain (blank = keep existing) and `/profile/greenhouse/unlink` clears it; the Profile field is write-only + a Disconnect button; `apply.greenhouse_credentials` reads the keychain (legacy-plaintext fallback). One-time auto-migration in `load_profile` moves any existing plaintext into the keychain and scrubs the YAML (idempotent, best-effort). 6 tests (fake keyring) + full suite 270/270; drove GET/migration/unlink live (password never in the payload, YAML scrubbed, keychain holds it). No new dependency (keyring already in); `profile/*` already git-ignored | Accepted |
| 061 | 2026-07-09 | Workday M2 part 1: recipe backbone + agentic-fallback distillation (offline core; live agentic call built + flagged, pipeline wire-in is M2 part 2). `workday_recipes.py` = a **shared, committed, PII-free** library (`workday_recipes.json`, `{signature: [{automation_id, control, question}]}` — selectors + labels only, **never answer values**; answers re-resolved per user at replay). `workday.unrecognized_fields` finds visible+empty+unknown-id custom controls; `replay_recipe` re-fills a learned page deterministically via `AnswerResolver` (source `workday-recipe`, no Claude); `run_agent_fill` hands unrecognized fields to a Claude-Code+Playwright-MCP worker over CDP (`agent_prompt` = fields + facts + HARD RULES: never navigate/fabricate) and **distills the recipe by DIFFING which fields went empty→filled** — no dependence on parsing opaque MCP element refs. `_spawn` injectable → fake-agent tests, no Claude/CDP. 8 tests incl. PII-free store + the full learn-once→replay-no-agent loop; full suite 278/278. Unwired until part 2 (fill_wizard/apply_workday integration + CDP browser launch + gating) | Accepted |
| 062 | 2026-07-09 | General CSRF/origin guard on ALL state-changing POSTs (closes the audit item; extends 058). A single choke point at the top of `web.Handler.do_POST` rejects any cross-origin request (403) before dispatch — a page on another site the user has open can't drive the loopback server (saves, submits, browser launches). `_same_origin` generalized: a missing Origin/Referer passes (same-origin fetches often omit it; non-browser clients send none — not the CSRF threat), a loopback Origin passes, and otherwise the Origin host must equal the `Host` header the client addressed — so it's correct under `--host` LAN/name binds, not just 127.0.0.1 (a browser sets Origin itself, so a remote page can't forge a loopback value). The per-endpoint armed-`/parked/reapply` check (058) is now redundant and removed. GETs stay unguarded (read-only). 6 tests (`test_web_csrf.py` cross-origin blocked before the handler + loopback/no-Origin/GET allowed; `_same_origin` matrix incl. LAN-bind); full suite 280/280; drove a real cross-origin POST → 403 with the handler never called | Accepted |
| 063 | 2026-07-09 | Workday M2 part 2: agentic fallback + recipe replay wired into the pipeline (extends 061). `_resolve_unrecognized` runs per wizard page after the deterministic fill — replay a learned recipe first (free), then only if custom fields remain AND armed does `run_agent_fill` fill+learn+persist. `fill_wizard`/`apply_workday` gained optional `resolver`/`agentic`/`cdp_port`/`store_path`/`_agent_spawn` (no resolver ⇒ pure M1). `run_apply` opens a CDP endpoint (`--remote-debugging-port=<free port>`) only for armed-agentic Workday runs so the Playwright-MCP worker attaches to the same page; threads resolver+agentic+port to the adapter. `workday.agentic_enabled` reads `workday_agentic` from safety.yaml, **off by default** (replay always on/free; only Claude-learning of a NEW page is gated, mirroring 049). 4 tests incl. learn-once(on)→replay(off) agent-runs-exactly-once + a live `run_apply` drive confirming the real CDP launch + param threading. Full suite 283/283. **M2 complete** but for the flagged live Claude-over-MCP run on a real tenant. Next: M3 armed submit | Accepted |
| 064 | 2026-07-09 | Workday M3: armed submit gated by the SafetyGate (extends 035/059). `_attempt_workday_submit` — reached only from `apply_workday` when `gate.armed`, after `fill_wizard` reaches Review. Order: Review-page check (Submit control present) → `_workday_unmet_required` scan (empty aria-required/placeholder-dropdown → **blocked before any click**) → `gate.may_submit()` (armed + no KILL + under cap, last-moment) → click `pageFooterSubmitButton` + `record_submission` → confirmation via `_confirmation_evidence` (submitted) / Workday error alert (blocked) / Submit-gone (unconfirmed-but-submitted, no double-submit). `apply_workday` gained `gate`; `run_apply` passes it into the Workday branch (Workday owns its submit; generic `_attempt_submit` skipped). No gate/unarmed ⇒ M1/M2 dry-run unchanged. 5 tests (armed happy path submits + cap=1; empty-required blocks pre-click; KILL blocks; unarmed blocks; full armed Apply→create→wizard→Review→Submit). Full suite 289/289. **Workday M1+M2+M3 code-complete**; sole remaining item is the flagged live run on a real tenant | Accepted |
| 067 | 2026-07-14 | Weak-model answers for required unmapped fields, so an armed submit is never blocked by an empty required box — free-text drafts **and** (amendment) dropdowns/selects. Two changes: (1) free-text answers now draft with a deliberately **weak/cheap model** (`answer_bank.DRAFT_MODEL = "haiku"`; `generate_answer` defaults to it — a grounded, résumé-only paragraph needs no frontier model, and it saves tokens); (2) `freetext_answer(required=…)` force-drafts any **required** field even when it isn't "open-ended" by phrasing (the WHOOP case is a single-line `<input type="text" required>` — `is_open_ended` returns False, so before this it was skipped as "no saved answer" and blocked submit). New `answer_bank.is_draftable_required` gates the force-draft: it excludes numeric-fact (salary/GPA/test-score) and demographic/EEO questions, which we must **never** fabricate — those stay empty for the user (honesty, Guideline #7). Per-field required-ness read live via new `_IS_REQUIRED_JS`/`_is_required` (element `required`/`aria-required`, or a label/card marked `*`/`✱`/`★`/"required"). 4 new tests + drove the real committed `lever_custom_cards.html` headless: the WHOOP required text input now fills (`source=generated`) instead of being skipped; suite green | Accepted |
| 066 | 2026-07-13 | Lever custom-question label derivation (fixes the WHOOP dry-run report). Root-caused from the run's `report.json` + a live fetch of the Lever DOM: a Lever card renders its question in a `<div class="application-label"><div class="text">…</div></div>` — **not** a `<label>`/`<legend>` — while each radio OPTION is wrapped in its own `<label>` holding just "Yes"/"No". So `_LABEL_JS` fell through to the raw input `name` (`cards[uuid][field0]`) and `_GROUP_QUESTION_JS` returned `''`. Effects: the work-authorization + visa radio groups never filled (resolver got no/garbled question → `None`), and the "Why are you interested in working at WHOOP?" text got the label `cards[…][field0]`, so the generated answer was ungrounded ("vaguely made sense"). Fix: both JS helpers now, before the generic ancestor walk, find the enclosing `.application-question`/`li` card and read `.application-label .text`; the ancestor-walk selectors also include `.application-label`; `clean()` strips the `✱`/`★` required glyphs. Additive — Greenhouse/Ashby (which wrap inputs in real `<label>`s) are unchanged. Two follow-on fixes surfaced during a live headed dry-run and were folded in: **(a) option-label guard** — the reorder must NOT hijack a radio OPTION's own `<label>` ("Yes"/"No"), so `_LABEL_JS` returns the wrapping `<label>` when it has no nested `.application-label`, and falls to the card question only for the EEO-select wrapper (which nests one); **(b) captcha-overlay radio fallback** — Lever embeds an hCaptcha whose invisible enclave iframe sits over the form and swallows the radio click, so new `_check_radio` tries normal → forced → `.checked=`+input/change (the CAPTCHA is never touched; dry-run never submits). **EEO normalization DONE** (not deferred): `option_hints` now maps veteran/disability intent to each ATS's option wording (exact-first + negation-safe fuzzy — never a bare "veteran" that hits both "am a"/"am not a"); Greenhouse "I am not a protected veteran" → Lever "I am not a veteran". Race/gender already matched once labels were clean. Verified against the **real** fetched WHOOP DOM headless AND a full live headed dry-run: **15 filled, 0 errors, nothing submitted** — work-auth Yes, visa No, hybrid Yes, Gender/Race/Veteran filled, "Why WHOOP?" grounded (references WHOOP's mission). Committed PII-free fixture `fixtures/apply_forms/lever_custom_cards.html` + `tests/test_lever_labels.py` (4 tests incl. radio-check + option-label regression guards); fill/submit suite green (full suite 304/305, the one failure is a pre-existing mailbox test-isolation bug unrelated to apply.py). The captcha the user saw is real (Lever embeds hCaptcha) but fires only at **submit** (dry-run never reaches it) — it did not cause the empty fields | Accepted |
| 065 | 2026-07-09 | One-click Gmail connect via OAuth (extends 057). The bot-email link asked for email + IMAP host + port + a hand-generated **app password** (2FA + digging through Google settings) — the opposite of one-click. Now the primary path is **"Sign in with Google"**: `mailbox.connect_gmail(client_id, client_secret)` runs the loopback consent flow (`google-auth-oauthlib` `InstalledAppFlow.run_local_server`, `access_type=offline`+`prompt=consent` so a refresh token always comes back), reads the email via the Gmail profile endpoint, **tests before it saves** (link-before-save, 057), and stores refresh-token+client-secret in the OS keychain (service `applicationbot-gmail-oauth`) with email/client_id/`auth: oauth` in git-ignored `profile/mailbox.yaml`. Reads use the **Gmail REST API with the read-only scope** (`gmail.readonly`) via `urllib`+Bearer — deliberately NOT IMAP-over-XOAUTH2, which would force Google's full `mail.google.com` scope (send/delete); least privilege (Guideline #5). `MailboxConfig` gained `auth/refresh_token/client_id/client_secret`; `test_connection`/`fetch_verification` branch to `_gmail_*` when `auth=="oauth"` (IMAP/env path byte-identical). Web: Profile "Bot email" panel leads with **Connect Gmail** (client_id/secret + one-time setup steps; app-password moved to an "Advanced" `<details>`), `POST /mailbox/gmail/connect` (CSRF-guarded, threaded so consent doesn't freeze the UI, elapsed-time waiting state), `GET /mailbox` returns non-secret `auth`+`client_id` for one-click reconnect. CLI `connect-gmail`; doctor/status show "Gmail, read-only". New deps `google-auth`/`google-auth-oauthlib`. 9 new tests (fake keyring + injected flow/token/get: keychain-only secrets, read-only fetch, route-to-oauth, save-on-success, no-save on missing-token/failed-read); full suite 298/298; JS node-clean; endpoints driven live (GET shape, 400 on missing creds). Live step flagged: the real Google consent needs the user's Cloud client + browser. Setup burden: a one-time Google Cloud "Desktop app" OAuth client, project set to "In production" so refresh tokens don't expire weekly | Accepted |

---

## Decisions

## 001 — Primary language: Python

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The pipeline needs scraping, LLM calls, document generation, and later
browser automation for form submission. A primary language was needed before writing
any code.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Python | Mature LLM SDKs, scraping, doc generation; Playwright available | Weaker for a browser-native frontend |
| TypeScript / Node | One language for UI + browser automation | Weaker doc-generation / data tooling |

**Decision:** Python for now. If a frontend or another component is more efficient in a
different language later, use multiple languages (polyglot).

**Reasoning:** Python has the strongest ecosystem for the core of this project (LLM
tailoring, scraping, document generation). The user explicitly left the door open to
adding other languages where Python isn't the best fit.

## 002 — Resume model: structured source-of-truth + LLM tailoring

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The customizer must adapt the resume to each job description. How the base
resume is represented determines how tailoring works and how much the LLM can drift from
the truth.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Structured data + LLM tailoring | Factual (LLM selects/reorders/rephrases from a source of truth, can't invent experience); reusable; easy to compare versions | More upfront schema work |
| Whole-document LLM rewrite | Simple; preserves formatting | High risk of altered/invented facts; hard to constrain |
| Rules only (no LLM) | Cheap, deterministic | Shallow tailoring; can't rewrite prose to fit |

**Decision:** Base resume is structured data (source of truth). For each job, an LLM
selects, reorders, and rephrases from that data; a renderer produces the final document.

**Reasoning:** Keeps the output factual by construction — the LLM works from a fixed set
of true statements and may only re-emphasize them, not fabricate. Also the most reusable
and testable design. See [[001-python]].

## 003 — Test data: real job descriptions as static fixtures

**Date:** 2026-07-03
**Status:** Accepted

**Context:** Testing the customizer needs realistic job descriptions. The user's first
instinct was to build the scraper first, then the customizer.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Collect real JDs as static fixtures | Decouples the customizer from the scraper; real test data now; small | Fixtures can go stale (public postings expire) |
| Build the scraper first | Produces JDs the "real" way | Much larger task with its own open decisions; blocks the customizer |

**Decision:** Collect a small corpus of real job descriptions (across frontend, backend,
and data/ML, varying seniority) as static fixtures, and build the customizer against
them. Build the scraper later as its own stage.

**Reasoning:** The scraper is a separate stage with unresolved decisions (which sites,
site-terms/rate-limit handling, storage). Fixtures give real, verbatim test inputs now
and let the customizer be built and iterated independently. Also pairs the fixtures with
a synthetic sample resume so no real PII is involved in development.

## 004 — LLM provider/model: Claude via the Anthropic SDK

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The tailoring approach (decision 002) is LLM-based, so a provider and model
were needed.

**Decision:** Use Claude through the official Anthropic Python SDK (`anthropic`), default
model `claude-opus-4-8`, with structured output via `client.messages.parse()` and a
Pydantic schema.

**Reasoning:** Claude is well-suited to the select/rephrase-without-inventing task, and
structured outputs give a validated, typed result (the tailored resume) with no brittle
parsing. `claude-opus-4-8` is the current default capable model. Provider/model is
isolated in one module so it can be swapped if needed.

## 005 — PDF → YAML via Claude's native PDF reading

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The base resume is structured YAML, but users have PDFs/DOCX. We needed a
way to construct the YAML from a dropped-in resume. Considered OpenDataLoader PDF (a
strong open-source, layout-aware parser) at the user's suggestion.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Claude native PDF reading | Zero new deps; one step (PDF → YAML); vision-based, handles columns; resume is small prose | Sends the PDF to the API |
| OpenDataLoader → Markdown → Claude | Deterministic, local, layout-aware; great for tables/RAG/scale | Requires a Java 11+ runtime (friction for a clone-and-run tool); two steps; its strengths (tables, bounding boxes) don't matter for a 1-page resume |

**Decision:** Use Claude's native PDF reading to build the YAML. Keep OpenDataLoader as an
optional fallback for resumes where native extraction struggles (dense two-column,
scanned/image PDFs — its `--force-ocr` helps). Do not make Java a hard dependency.

**Reasoning:** Constructing the YAML is a once-per-user step on a small, mostly-prose
document; the hard part is semantic mapping (which line is a title vs. a date), an LLM
task regardless of parser. Native reading is simpler and dependency-free. See [[001-python]].

## 006 — Preserve the source resume's format

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The user wants generated resumes to keep the same or very similar format to
the resume they supply. The v1 schema (flat skills, no leadership section, single-column
generic Markdown) could not represent a real resume faithfully, let alone match its
format.

**Decision:** The resume schema mirrors real resume structure — categorized skills, a
separate leadership/activities section, projects with a tech-stack line, optional summary,
and an explicit `section_order` that the renderer honors. The tailoring prompt instructs
the model to keep the same section set and similar length. Markdown is the current render
target; a PDF/DOCX render target that reproduces the exact visual layout (right-aligned
dates, single-column, fonts) is deferred.

**Reasoning:** Faithful representation of the user's real resume is the prerequisite for
format fidelity — you can't preserve a format you didn't capture. Section-order-as-data
lets each user's layout be preserved without hardcoding one order. Exact pixel-level
reproduction needs a templated document renderer, which is a larger, separate task.

## 007 — Direction: the source of truth becomes a full user catalogue

**Date:** 2026-07-03
**Status:** Accepted (direction — not yet built)

**Context:** The user noted that we shouldn't tailor strictly from what's on a single
dropped-in resume. Instead the system should hold a whole *catalogue* of information
about the user (every role, project, bullet, skill, achievement — more than fits on one
page) and pick and choose per application.

**Decision:** Evolve the structured source of truth (decision 002) into a **catalogue**:
a superset of the user's history that can exceed one resume's worth of content. The
tailoring step then selects a resume-sized, format-appropriate subset per job. The
current `Resume` model is the seed of this; the catalogue adds breadth (more entries than
any one resume shows) and the tailorer gains a length/selection budget so output still
fits the target format.

**Reasoning:** Directly extends decision 002 — a richer source of truth means better,
more relevant tailoring, since the model can surface material the base resume omitted for
space. Deferred until after the single-resume customizer is proven end-to-end. See
[[002-resume-model]].

## 008 — Pluggable tailoring backends with auto-selection

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The user asked whether the customizer could run without any LLM API keys —
important for a clone-and-run tool where not every user has an Anthropic key.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Claude via account login (OAuth `ant auth login`) | Best quality; no `sk-` key string | Needs a Claude account + internet |
| Local model (Ollama) | No key, no cost, offline, anyone can run | Lower quality; needs Ollama + RAM/CPU |
| Rules-based (no LLM) | Zero deps/cost, deterministic, never invents | Shallow — reorders/selects but can't reword |
| Pluggable (all of the above) | Flexible; degrades gracefully | A bit more code |

**Decision:** Make the tailoring backend pluggable behind one interface
(`applicationbot/backends.py`): `ClaudeBackend`, `OllamaBackend`, `RulesBackend`. Default
selection is `auto` — Claude if credentials/OAuth are present, else a local Ollama model
if reachable, else the no-LLM rules engine. `--backend` overrides it.

**Reasoning:** Directly answers "does this need an API key?" — no. The rules backend
needs nothing (proven: it tailored a real resume to a real posting with zero credentials);
Ollama needs no cloud/account; Claude stays available for best quality via key or login.
`auto` gives a good out-of-box experience that degrades gracefully. The LLM prompt is
shared between the Claude and Ollama backends, and `check_factual_drift` guards all three.
Notably `ClaudeBackend` already supports OAuth login because the Anthropic SDK resolves an
`ant auth login` profile when no key is set — no extra code needed. See [[004-llm-provider]].

### Update (2026-07-03): Ollama backend dropped

Removed the local-model (Ollama) backend. Local LLMs are hard for most people to get
running correctly and not worth the hassle for those who can. The strategy is now:
**primary = Claude via OAuth login (`ant auth login`, no API key string); fallback =
rules (no LLM).** `auto` picks Claude if credentials/OAuth are present, else rules.
`--backend` choices are `auto | claude | rules`.

## 009 — Review UI: a local, dependency-free web app

**Date:** 2026-07-03
**Status:** Accepted

**Context:** Reviewing tailored resumes via the CLI (reading Markdown / files) gets
tedious, and a UI is also needed for eventual production use. The user preferred a simple
program on a local port.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Local web app, Python stdlib `http.server` | No deps, single language, `python -m ...` runs it; browser is a good review surface | Hand-rolled routing (small) |
| Local web app, Flask/FastAPI | Nicer routing | Extra dependency + (FastAPI) a server to install |
| Node/React SPA | Rich UI | New language + build tooling; overkill now |
| Auto-open rendered files / a TUI | Minimal | Poorer review experience; not a path to production UI |

**Decision:** A small local web app in `applicationbot/web.py` using the Python standard
library only (`http.server`, bound to `127.0.0.1`). It reuses the existing tailoring
pipeline and a new `render_html` target that renders the resume as a styled single-column
HTML card (right-aligned dates/locations) so it resembles a real resume. Endpoints only
read from allow-listed folders (`profile/`, `examples/`, `fixtures/job_descriptions/`).

**Reasoning:** Zero new dependencies keeps the clone-and-run promise and stays in one
language (decision 001). A browser page is a better review surface than a PDF and is the
natural seed for the production UI. Rendering to styled HTML also advances format fidelity
(decision 006) without needing a PDF/DOCX renderer yet — PDF export remains future work.
See [[006-preserve-format]], [[008-pluggable-backends]].

## 010 — Claude sign-in from the site drives the `ant auth login` OAuth flow

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The web UI should let a user sign into their Claude account (OAuth) so the
`claude` engine works without managing an API key string.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Drive `ant auth login` from the site (a "Log in with Claude" button that runs the CLI's OAuth) | Uses Anthropic's supported OAuth mechanism; stores a profile the SDK reads automatically | Requires the `ant` CLI installed |
| Build a custom browser-OAuth client in the app | Fully in-site | Needs a registered Anthropic OAuth client_id we don't have and can't self-serve; not available |
| API key only | Simple | Not OAuth; user must create/manage a key |

**Decision:** The site drives the official `ant auth login` flow. `applicationbot/auth.py`
detects credential state (API key / auth token / OAuth profile) and, on the "Log in with
Claude" button, runs `ant auth login` server-side — which opens the user's browser to
Anthropic, and on approval stores a profile under `~/.config/anthropic` that the Anthropic
SDK resolves automatically. If `ant` isn't installed, the UI shows install instructions
(and notes an API key is an alternative).

**Reasoning:** OAuth against a Claude subscription is only exposed through the official
CLI/first-party clients; there is no public self-serve OAuth client registration for a
third-party app, so a custom in-browser OAuth is not buildable. Wrapping `ant auth login`
is the supported path and still delivers "click a button, approve in the browser, done."
The `anthropic` SDK already reads the resulting profile with no extra code. See
[[008-pluggable-backends]].

## 011 — Use Claude via the Claude Code CLI (subscription), not the Anthropic API

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The user requires that the app use their Claude **subscription** (Pro/Max),
not the metered Claude **API**. Investigation confirmed a hard constraint: any call
through the `anthropic` SDK hits `api.anthropic.com` and is billed as API usage,
**regardless of auth** — an API key OR an `ant auth login` OAuth profile both authenticate
the developer/console account, not the subscription. Anthropic's own docs state that a set
`ANTHROPIC_API_KEY` yields "API usage charges rather than using your subscription's
included usage," and that subscription programmatic usage is available only through
Claude's own tools (Claude Code, Agent SDK) — not arbitrary third-party SDK apps. This
also corrects decisions #004 and #010, which assumed the SDK/`ant` path could use the
subscription (it can't).

**Options considered:**
| Option | Uses subscription? | Notes |
|---|---|---|
| Anthropic SDK (API key or `ant` OAuth) | No — always the API | Guaranteed structured output; but it's the API the user rejected |
| Shell out to Claude Code CLI (`claude -p`) | **Yes** | Runs on the subscription's included programmatic usage; needs Claude Code installed + signed in; structured output via prompt + validate/retry |
| Rules engine only | N/A (no LLM) | Free, offline; can't reword |

**Decision:** The Claude tailoring engine is `ClaudeCodeBackend`, which invokes the local
`claude --print ... --output-format json` CLI with the tailoring prompt and validates the
returned JSON against `TailoredResume` (one retry on malformed JSON). This runs on the
user's Claude subscription, not the API. Removed the SDK/API backend, the `ant auth login`
flow, and the `anthropic` dependency entirely. `auto` selects `claude-code` when the
`claude` CLI is present, else `rules`. The web UI's account panel now reports Claude Code
availability (sign-in happens inside Claude Code, not the app).

**Reasoning:** It's the only way to meet the "subscription, not API" requirement — the
subscription is reachable only through Claude's own tooling. Verified end-to-end: tailored
a real resume against a real posting via `claude -p`, producing factual, well-formatted
output with a clean drift check and no API usage. Trade-off accepted: depends on Claude
Code, and structured output is prompt-enforced (validated) rather than schema-guaranteed.
Supersedes [[004-llm-provider]]; reverses [[010-oauth-from-site]]; keeps the pluggable
design of [[008-pluggable-backends]] with `claude-code` + `rules`.

## 012 — Configurable length budget

**Date:** 2026-07-03
**Status:** Accepted

**Context:** Tailored resumes need to fit a target length (usually one page), and the user
wants that length to be a customizable variable.

**Decision:** `applicationbot/length.py` defines `LengthBudget(pages=1.0)` — `pages` is the
single knob. From it we derive caps (max experience/project/activity entries, max bullets
per entry) from a rough per-page capacity. The budget is applied twice: its `.prompt()` is
appended to the Claude prompt (so the model self-limits), and `.enforce()` hard-caps the
result afterward (so the budget holds for any engine, including rules). Exposed via
`--pages` (CLI) and a Length dropdown (web, 1 / 1.5 / 2 pages).

**Reasoning:** Belt-and-suspenders — instruction gets a well-shaped result, enforcement
guarantees the bound. Keeping `pages` as the sole variable makes it trivial to expose more
options later (custom page counts, per-section caps). See [[006-preserve-format]].

## 013 — Catalogue storage: structured file + local relevance pre-selection

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The résumé data is becoming a *catalogue* (decision 007) — a superset of the
user's history that can grow well past one resume. Every tailoring call currently sends the
whole thing to Claude, so as it grows, prompts get large: more tokens (subscription credit)
and slower calls. The user asked for the most token-efficient way to store this.

**Options considered:**
| Option | Token efficiency | Cost |
|---|---|---|
| One structured file, send it all to Claude | Poor as it grows — every call ships the full catalogue | Simplest (current) |
| Structured file + **local relevance pre-selection** → send only the relevant slice | Strong — Claude sees a bounded subset regardless of catalogue size | Small (reuses keyword scoring; no deps) |
| Structured file + **embeddings / vector store** → semantic top-K | Strongest relevance | Adds an embedding model/dependency + index to maintain; overkill for a personal catalogue of dozens–hundreds of items |
| Per-item files / a database | Neutral for tokens (the win is pre-selection, not the medium) | More moving parts |

**Decision:** Keep the catalogue as a single structured file (the existing YAML), and make
Claude calls token-efficient by **pre-selecting the relevant slice locally before the
call** (`catalogue.select_relevant`): a free keyword-relevance pass (shared
`relevance.py`) keeps ~2× the length budget's worth of the most job-relevant entries per
section. Small catalogues are sent unchanged (best quality, still cheap); large ones are
bounded. Skills/education/summary/contact are always kept (small). Embeddings remain a
future upgrade if keyword matching proves insufficient.

**Reasoning:** The token cost is driven by *how much of the catalogue reaches the prompt*,
not by the storage medium — so the highest-leverage, lowest-cost move is local
pre-selection, which reuses the rules engine's scoring and adds no dependencies. It keeps
prompts small and calls fast as the catalogue grows, while a small catalogue pays nothing.
See [[007-catalogue-direction]], [[011-claude-code]].

## 014 — Parallel agents: file bus + canary + Cursor hooks

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The user develops with both Cursor and the Claude VS Code extension in the
same repo and wants parallel collaboration without waiting for prompts to finish — a
lightweight inter-agent channel that stays out of git.

**Options considered:**
| Option | Pros | Cons |
|---|---|---|
| Shared git branch / PRs only | Simple; auditable | Slow; no real-time handoffs |
| External chat (Slack, etc.) | Real-time | Context outside repo; easy to lose file refs |
| **Git-ignored file bus + canary poll** | Works for both tools; no deps; refs paths directly | Near-real-time (~1s), not instant; requires discipline |
| Shared SQLite / Redis | True pub/sub | Overkill; another service to run |

**Decision:** A git-ignored `.agent-bus/` directory with JSON messages, sequence counters
in `canary.json`, notify file touches, path **claims** to reduce edit conflicts, and a
stdlib Python CLI (`applicationbot/agent_bus.py`). Cursor gets project hooks
(`sessionStart` injects context; `stop` nudges on unread mail). Claude VS Code uses the
same CLI + a documented session ritual in `CLAUDE.md` and `docs/AGENT_COLLAB.md`; users
run `watch --agent …` in a side terminal for alerts.

**Reasoning:** Both agents already read/write the filesystem; a file bus needs no network,
credentials, or new dependencies. Canary polling is good enough for two local agents.
Committed code defines the schema; runtime state stays local and PII-free.

## 015 — LinkedIn: import the official data export, not live OAuth/scraping

**Date:** 2026-07-03
**Status:** Accepted

**Context:** The user wanted to "link LinkedIn" to pull profile data into the catalogue.

**Options considered:**
| Option | Gets experience/education? | Compliant? |
|---|---|---|
| LinkedIn OAuth / OpenID sign-in | No — only name/email/photo; full-profile API is partner-restricted | Yes, but useless here |
| Scrape the LinkedIn profile | Yes | **No** — violates LinkedIn ToS + Agent Guideline #4 |
| Import LinkedIn's official data export (CSV) | **Yes** — Positions/Education/Skills | Yes — user's own data, downloaded by them |

**Decision:** Import LinkedIn's official "Get a copy of your data" export. The user
downloads the archive from LinkedIn and uploads it (`applicationbot/linkedin.py` parses
the ZIP or CSVs); `POST /resume/import-linkedin` merges new experience, education, and
skills into the catalogue, deduping against existing entries and never overwriting contact
info. Upload travels as base64 in JSON (no multipart parsing; `cgi` is gone in 3.13).

**Reasoning:** A live "link" that pulls full profile data is simply not available to
third-party apps — LinkedIn restricts the API and scraping is against their terms (and our
Guideline #4). The data export is the only compliant, reliable source of the user's real
history, and it maps cleanly onto the catalogue schema. See [[007-catalogue-direction]],
[[004-respect-tos]].

## 016 — Apply stage: per-ATS Playwright automation, autonomous-first

**Date:** 2026-07-03
**Status:** Accepted

**Context:** How to actually submit a tailored resume to a job. Research finding: there is
**no candidate-facing application-submission API** — the ATS submit endpoints (e.g.
Greenhouse's) require the *employer's* API key. So we must drive the real application form.
The user's north star is fully autonomous operation (run overnight/continuously; contact
the human only for periodic updates or when genuinely stuck), consistent with Guideline #3
(auto-submit once armed, no per-application confirmation).

**Options considered:**
| Option | Verdict |
|---|---|
| Per-ATS browser automation (Playwright) for Greenhouse/Lever/Ashby (our fixtures) | **Chosen** — reliable (consistent forms), covers the market, testable in dry-run |
| Browser extension (autofill in the user's real browser, human submits) | **Later surface** — good for logged-in/bot-protected sites, but human-in-loop; build toward it |
| LLM agentic browser (computer-use) | Deferred — most adaptive but slower/less reliable for irreversible submits |
| ATS submission API / Easy-Apply automation / CAPTCHA-defeating | Rejected — API needs employer key; Easy-Apply + CAPTCHA-bypass violate ToS + Guideline #4 |

**Decision:** Build the Apply stage as **per-ATS Playwright adapters** (start Greenhouse),
**autonomous-first**: the runner processes a queue of postings, tailors, fills the form,
uploads the PDF, auto-answers questions (Claude + a saved answer bank), and — when armed —
submits, all without a human in the loop. Anything it *can't* do autonomously (CAPTCHA,
login wall, unanswerable question) becomes a **logged exception surfaced in periodic
updates**, NOT a blocking prompt. `dry_run` is the default (fill + screenshot + record what
it would submit; never submit against a real posting in dev). A browser **extension** is a
planned second surface for sites that resist headless automation. Respect ToS: rate-limit,
no CAPTCHA evasion, no Easy-Apply automation.

**Prerequisites (build first):** (1) **PDF/DOCX resume export** — forms upload a file; (2)
an **application-answer profile** (work authorization, EEO, salary, start date, links, and
a growing bank of answers to screening questions) so the autonomous runner rarely gets
stuck. See [[003-safety-switch]] (Guideline #3), [[004-respect-tos]] (Guideline #4).


## 017 — Apply stage: native ATS autofill first, our resolver fills the gaps

**Date:** 2026-07-04
**Status:** Accepted

**Context:** Our per-ATS autofill (decision 016) fills a Greenhouse form 15/15 live. But many
ATSs ship their **own** autofill, which is more robust and fills exactly what the ATS expects.
Empirically (headless Chromium against the live Censys Greenhouse form): Greenhouse exposes
**"Quick Apply with MyGreenhouse"** (a candidate account at `my.greenhouse.io`; email login)
and Dropbox/Google-Drive resume sources; **uploading a résumé does NOT auto-populate fields**
(no parse autofill on the public form). Lever/Ashby/Workday, by contrast, **parse an uploaded
résumé into fields with no account** — the higher-ROI native autofill.

**Options considered:**
| Option | Verdict |
|---|---|
| Native autofill first, our resolver fills only the still-empty fields | **Chosen** — best of both: native robustness + our coverage of custom/EEO questions the ATS can't fill |
| Our resolver only (decision 016 as-is) | Kept as the fallback when no native autofill exists (e.g. Greenhouse w/o creds) |
| Native autofill only | Rejected — never covers per-company custom/screening/EEO questions |

**Decision:** Native-first, ATS-agnostic: **upload résumé → trigger the ATS's native autofill
→ our resolver fills only fields still empty** (`_fill_all_fields(only_empty=True)`, detecting
a field's current value incl. react-select `single-value`). Native mechanisms: resume-parse on
upload (Lever/Ashby), an "Autofill with Resume" button (Workday), and **MyGreenhouse via stored
credentials + auto-login** (per the user's choice — email+password in the git-ignored profile;
a login failure is logged and we fall back to our autofill, never blocking). The report tags
each field `native` vs `resolver`. Build priority: the zero-setup resume-parse ATSs first,
then MyGreenhouse. The MyGreenhouse login flow is implemented best-effort but **unverified**
against a real account (needs a live login to confirm). See [[016-apply-stage]].


## 018 — Self-improving answer bank (learn + generate)

**Date:** 2026-07-04
**Status:** Accepted

**Context:** Application questions repeat across companies, so the same ones shouldn't be
re-answered every time. The user asked that new questions autofill encounters be saved to the
Q&A bank for reuse — except company-specific ones ("why do you want to work here"), whose
answer differs per company — and that open-ended experience questions ("describe your
experience doing X") be drafted with the Claude **subscription** and also cached.

**Decision:** The answer bank (`ApplicationProfile.custom_answers`) becomes self-improving:
- **Reuse first:** `AnswerResolver.resolve()` checks structured fields then the bank (existing).
- **Generate open-ended:** on a miss for an open-ended free-text question, draft an answer with
  Claude via the subscription CLI (`answer_bank.generate_answer`, reusing
  `backends.run_claude_cli`), **grounded strictly in the résumé** — the system prompt forbids
  inventing experience and requires honesty when the résumé lacks it (integrity; Guideline #5).
- **Learn:** generated answers are cached to the bank (flagged `generated=True` for review);
  new reusable questions we couldn't answer are captured as **blank pending** entries so the
  user fills each once in the UI, then reuse is automatic.
- **Exceptions (never cached):** **company-specific** questions (classified by phrase) and
  **demographic/EEO** questions (handled by the structured optional EEO fields, blank = decline).
- Persistence happens after the run (`remember_answers` / `capture_questions`, dedup by
  normalized question). Generation is best-effort: no Claude CLI → skip drafting, fall back to
  the needs-attention queue. Toggles: `--no-generate`, `--no-learn`.

The UI's answer bank marks entries **✨ AI-drafted — review** and **○ Needs your answer**.
Classifiers + learning verified; live Claude drafting is unverified in-sandbox (no CLI there).
See [[016-apply-stage]], [[017-native-autofill]], [[011-claude-code-subscription]].


## 019 — Codebase index: structural repo map, not a vector database

**Date:** 2026-07-04
**Status:** Accepted

**Context:** The user asked for "something like a vector database" so that changing code
in this repo is faster and more efficient for an agent each session, and asked to compare
options before committing. Measured size: ~3.9k lines of first-party Python across 17
files (the repo is pure Python; the only non-Python source is HTML/JS embedded inside
Python f-strings in `web.py`, which any parser sees as opaque strings).

**Options considered:**
| Option | Infra / deps | Pros | Cons |
|---|---|---|---|
| Status quo (grep/glob + reads) | none | Exact, instant on 4k lines | No one-shot orientation; no dep graph |
| **Structural repo map (`ast`)** | none (stdlib) | Always fresh, exact, zero deps, gives symbol map + import graph | Python-only until a parser is added |
| Tree-sitter repo map | `tree-sitter` + grammars | Multi-language | Deps to maintain for no gain on a pure-Python repo |
| Local vector DB (sqlite-vec / LanceDB + Voyage embeddings) | embedding model/API | Concept search | Overkill at 4k lines; stale on every edit (repo churned by 2 agents); fuzzy top-k less precise than grep; new external dep |
| Server vector DB (Qdrant / pgvector / Milvus) | runs a service | Scale / multi-repo | Violates the cloneable, minimal-infra ethos |

**Decision:** Build a structural repo map on the stdlib `ast` module
(`applicationbot/repo_map.py`, run via `python -m applicationbot.repo_map`). It parses
every first-party `.py` file fresh on each run and emits a compact markdown (or `--json`)
map: per file → module docstring, first-party imports, constants, and classes/functions
with signatures and line numbers, plus a reverse-dependency graph (who imports each
module). Output is generated on demand (default stdout; `--out` writes a git-ignored
`.repo-map.md`), never committed. Rejected a vector database: semantic search earns its
keep on large, slow-churning codebases searched by concept — the opposite of this repo,
where exact grep is already instant and an embedding index would go stale on every edit.
Rejected tree-sitter: it adds grammar dependencies with no benefit while the repo is pure
Python; `_symbols_for()` is the single dispatch point where a tree-sitter backend can be
added if standalone non-Python source ever lands.

**Reasoning:** Matches the actual problem (fast orientation + impact analysis) at the
actual scale, with zero dependencies and zero staleness — consistent with the cloneable,
minimal-deps ethos and the "simplicity first / no unrequested future-proofing" guidelines.
Revisit a local vector DB (sqlite-vec + Voyage `voyage-code-3`) only if first-party code
grows past ~30–50k lines, where grep stops being enough.

---

## 020 — Web UI: one unified Profile screen with collapsible entry cards

**Date:** 2026-07-04
**Status:** Accepted

**Context:** The user could edit the "Applicant details" (apply-profile) section but had no
obvious way to granularly edit experiences/projects: those lived on a *separate* "Résumé
data" tab, split from the apply profile the same person edits. The request: a clean layout
that still lets you granularly edit anything in the profile. Two candidate directions —
unify the two editor tabs, or improve the résumé editor in place.

**Decision:** Merge the "Résumé data" and "Apply profile" tabs into **one "Profile" tab**
(tabs are now just Review | Profile). It renders, top-to-bottom: Applicant details (kept
verbatim — it drives form autofill), then Experience / Activities / Projects / Education /
Skills (from the résumé), then Résumé header & summary, Screening answers, Autofill
accounts, and Native logins — with a sticky **section-jump nav** at the top and a single
**Save** that writes both files (`/resume/update` + `/profile/update`). Every list entry
is now a **collapsible card**: collapsed it shows a one-line summary (e.g. "Acme — SWE"),
click to expand and edit its fields; new entries open expanded. Bullets stay as a
"one per line" textarea (user's choice — not per-bullet rows). The two data stores are
unchanged (résumé YAML + `application_profile.yaml`); only the presentation is unified.

Also fixed a latent bug this surfaced: `list_resumes()` globbed `profile/*.yaml`, which
included `application_profile.yaml`; alphabetically it sorted first, so the résumé dropdown
defaulted to the apply-profile file (which fails to load as a `Resume`). It is now excluded
from the résumé list.

**Reasoning:** One screen for "everything about me" is the obvious path (UI Design Principle
#1 — one obvious path over several ambiguous ones) and directly fixes the discoverability
gap. Collapsible cards keep a long profile clean while preserving granular edit-anything
access. Reused the existing card builders, endpoints, and validated round-trips, so the
change is presentation-only — no data-model migration, no new dependencies. Verified live
(headless Chromium): tab loads, entries collapse/expand, summaries update on edit, and the
single Save round-trips both résumé and apply-profile files; original data restored after.

---

## 021 — Consistent waiting/status feedback for every async action

**Date:** 2026-07-04
**Status:** Accepted

**Context:** The web UI's async actions gave inconsistent feedback: tailoring showed a static
"Tailoring…" with no sense of progress on a multi-second Claude call; **PDF export showed
nothing at all** and reported errors via a bare `alert()`; saves/imports showed ad-hoc inline
text. The user asked that waiting states always inform them, as a consistent UI/UX decision.
Separately, tailoring silently dropped résumé entries that didn't fit the length budget, so a
newly-added experience could look "ignored" (this compounded a real file-mismatch bug —
`list_resumes()` listed `application_profile.yaml` as a selectable résumé and it sorted first,
so edits/tailoring pointed at the wrong file; fixed alongside).

**Decision:** Establish **one shared waiting pattern** and apply it to every async action
(tailor, PDF export, profile save, LinkedIn import, profile load), captured as **UI Design
Principle #5** in CLAUDE.md. Implementation in `web.py`: shared helpers `btnBusy`/`btnDone`
(disable the trigger, swap its label to a spinner + specific working verb, restore after) and
`busyInto(container, label, longRunning)` (spinner + label in-place; a live elapsed-seconds
counter when `longRunning`, used for the Claude tailoring call). A single `.spin` CSS keyframe
+ `.busy-*` styles; no per-feature spinner/toast variants. Errors now render inline and
actionable (Principle #3) instead of `alert()` (PDF export gained a `#pdf-msg` line). Every
action ends in a definite state: the result, "Saved ✓", or an inline error. Additionally,
`tailor_resume` now appends a **relevance note** when `LengthBudget.enforce` drops entries
("Omitted N experience entries to fit 1 page — increase Length to include more"), so budget
truncation is visible rather than silent.

**Reasoning:** A single reusable pattern is what makes "you're never left guessing" a property
of the whole app rather than a per-screen accident, and it's cheaper to maintain than bespoke
indicators. Surfacing dropped input follows directly from Guideline #11 (be precise; never
"silently ignored") and Principle #3 (actionable). Verified live (headless Chromium): spinners
appear and clear, the tailor timer ticks, PDF/save/import show status and end cleanly, and a
newly-added experience now flows through save → tailor into the output.

---

## 022 — Apply profile: structured location + start-date inputs (dropdowns), stored formats unchanged

**Date:** 2026-07-04
**Status:** Accepted

**Context:** The apply profile collected Location, Country, and Earliest start date as free-text
boxes. The user asked to make them behave like real application forms — dropdown selectors —
so the profile is entered the way ATS forms actually ask for it. Constraint: these fields feed
the Apply-stage autofill resolver (`apply.py`), which expects `location` as `"City, ST"` (its
Greenhouse geocoder handler parses that), `country` as a name, and `earliest_start_date` as a
string. Changing the *stored* shape would break the resolver.

**Decision (UI-only, model unchanged):** In the web profile editor, replace the three text
boxes with structured inputs that **compose/parse the same stored strings**:
- **Country** → dropdown (curated list, United States default, "Other" escape; preserves any
  pre-existing value not in the list).
- **State** → US-state dropdown (value = abbreviation, label = "New Jersey (NJ)").
- **City** → text. On save, `location = "City, ST"` (or just city / just state); on load,
  `parseLocation()` splits a stored `"City, ST"` back into the dropdown + city.
- **Earliest start date** → a dropdown of the common form answers (Immediately / 2 weeks'
  notice / 1 month / Specific date…); choosing "Specific date…" reveals a native date picker.
  Stored as the preset phrase or an ISO `YYYY-MM-DD`; a pre-existing free-text value is kept as
  its own option so nothing is lost.

`ApplicationProfile` (Pydantic) is untouched — `location`, `country`, `earliest_start_date`
stay plain strings — so the resolver and the rest of the pipeline need no changes.

**Reasoning:** Matches how applications collect these (fewer typos, consistent `"City, ST"` for
the geocoder, valid dates) while staying a presentation change with zero blast radius on the
autofill/data model. US-centric state list fits the profile's existing US orientation
(citizenship/EEO fields); non-US users leave State on "—" and the city text carries the value.
Verified live (headless Chromium): `"Edison, NJ"` parses into US/NJ/Edison; preset start date
selects with the picker hidden; "Specific date…" reveals it; edits save back as
`"San Francisco, CA"` and an ISO date — both resolver-compatible.

---

## 023 — Tailoring quality (concrete + quantified bullets) and per-entry "why" rationale

**Date:** 2026-07-04
**Status:** Accepted

**Context:** Three résumé-building asks from the user: (1) bullets should specify the actual
work — features built, bugs fixed, systems migrated, etc.; (2) be able to select a section of
the tailored résumé and see *why* it was tailored that way; (3) every bullet should carry some
quantification.

**Decision:**
- **(1) Concreteness + (3) quantification — prompt-only** (`backends.py` SYSTEM_PROMPT, so it
  applies to the `claude-code` engine; the rules engine can't reword). Bullets must name the
  specific action and result (feature shipped / bug or bug-class fixed / system automated /
  migrated / optimized) with the technology and outcome, replacing vague verbs. Quantification
  is a **strong preference, not an absolute rule**: surface real magnitude wherever the base
  résumé supports it, but use ONLY numbers present in or safely implied by the base résumé —
  **never invent, estimate, or round up a metric**. Pushed back on "every bullet must have a
  number": forcing it would induce fabrication, violating the system's core truthfulness rule
  (a truthful bullet with no metric beats a fabricated figure).
- **(2) Per-entry rationale, click-to-reveal** (user-chosen granularity + surfacing). Added an
  optional `tailor_note` to the `Experience` and `Project` models (TAILORED-only; base résumé
  leaves it null, and `save_resume`'s `exclude_none` keeps it out of the base YAML). The Claude
  prompt fills one short "why kept / how tailored for this job" sentence per experience, project,
  and activity; the **rules engine** fills a deterministic version from its keyword match. The
  HTML renderer emits it as a `data-why` attribute on each entry; the Review pane shows a
  sticky **side panel** — clicking an entry highlights it and displays its rationale (falls back
  to an intro hint). Markdown/PDF renderers ignore `tailor_note`, so it never prints on the
  résumé.

**Reasoning:** (1)/(3) raise output quality within the existing truthfulness guarantee rather
than against it — hence the deliberate softening of (3) (Guideline #2: flag the better, safer
path; #7: don't silently change intent). (2) at per-entry granularity with click-to-reveal was
the user's pick; carrying the note *on the entry* (`data-why`) is the most robust
entry→rationale mapping and keeps the resume render clean. Reused the model/renderer/review
pane already in place — no new deps. Verified: rules emits per-entry notes, renderer emits 8
`data-why` attrs on the real résumé, the panel shows an entry's title + why on click (live,
headless Chromium), and markdown/PDF exports carry no note leak.

---

## 024 — Track stage: local SQLite store + editable Track tab

**Date:** 2026-07-04
**Status:** Accepted

**Context:** The pipeline's fifth stage (Track) needs a system of record for every
application — the fields already fixed in NEXT_STEPS.md (company, role, location, remote,
pay, portal, method, source URL, dates, status, tailored-résumé ref, notes). The store is
written **programmatically** by the (future) autonomous runner and must be **browsable and
editable by the user themselves**, with application status easy to read at a glance. This
is a "how data is stored" decision (Agent Decision Framework), so options were presented
with pros/cons before building.

**Options considered:**
| Option | Autonomous write? | Cloned-user setup | PII location | Deps | Verdict |
|---|---|---|---|---|---|
| **Local SQLite** (stdlib `sqlite3`) | ✅ native, concurrent-safe (WAL) | none — file appears on first run | local, git-ignored | **zero** | **Chosen** |
| Local JSON/CSV file | ✅ but no concurrent writes; whole-file rewrites; CSV untyped | none | local | zero | Weak for status edits + a live dashboard |
| Google Sheets (API) | ✅ | Google Cloud project + OAuth per user | Google cloud | `google-api-python-client` | Great *view*, heavy as source of truth; ~60 writes/min |
| Airtable | ✅ mature API | account + token + base per user | Airtable cloud | HTTP | Free tier caps ~1k records; required external account |
| Notion | ✅ (newer API) | account + integration token per user | Notion cloud | HTTP | API less mature; rate-limited |
| Teal / Simplify | ❌ no public API (Chrome extension, human clicks) | install extension | their cloud | — | Rejected — human-in-loop |
| Huntr | ❌ only an **Organization/recruiter** API, no personal write API | — | their cloud | — | Rejected — not for individual candidates |

**Decision:** Local **SQLite** (`applicationbot/tracker.py`, stdlib `sqlite3`, zero deps) is
the system of record — one `applications` table matching the fixed field set, `STATUSES`
lifecycle (`discovered → tailored → dry-run → applied → failed → responded`), WAL mode so
the runner can write while the UI reads. DB path `applications.db` at repo root, **git-ignored**
(application history is PII, Guideline #12; added an explicit `.gitignore` line since the
existing patterns didn't catch that exact name). The primary human view is a new **editable
"Track" tab** in the web UI: every application in a horizontally-scrollable table with
**inline editing of any cell** (auto-saves per cell), a **status dropdown** per row, clickable
**status-count pills** that double as filters ("All · dry-run 3 · applied 1 · responded 1"),
free-text search, add, and delete. Endpoints: `GET /track`, `POST /track/{add,update,delete}`.
Dedicated trackers (Teal/Huntr/Simplify) are rejected for the autonomous core because none
expose a personal write API. Google Sheets / CSV export remains an **optional, one-way mirror**
for later — not the source of truth, and never required to use the product (keeps the
clone-and-run, minimal-infra promise).

**Reasoning:** SQLite matches the actual need at the actual scale with zero dependencies and
keeps PII local — the same "match the tool to the scale/ethos" reasoning that chose `ast` over
a vector DB (decision 019) and `fpdf2` over Chromium. A real table (vs. a flat file) makes
status transitions, filtered dashboard queries, and concurrent runner-writes trivial. Putting
the source of truth in a cloud tool would force every cloned user to create an external account
+ API credentials and ship their PII off-machine by default — a direct hit to the cloneable,
minimal-infra, PII-local principles. **Verified:** store CRUD + status validation + auto
date-stamp on `applied` + search (temp DB); all `/track` endpoints over real HTTP; the rendered
page JS `node --check`-clean; and the full Track tab driven live in headless Chromium (add →
inline edit "Saved ✓" → status change updates count pills → reload persists → filter → delete),
zero console errors. See [[019-repo-map-not-vector-db]] (match tool to scale), [[016-apply-stage]]
(the runner that will write records), [[012-safety-switch]] / Guideline #3 (the `dry-run` status).

### Update (2026-07-04): Apply dry-runs now auto-record

The Apply stage writes to the tracker so records appear without manual entry. `run_apply`
(`record=True` by default; `--no-record` to opt out) calls `apply._record_dry_run(...)` after
filling: it derives (role, company) from the posting's page title (`_title_role_company`),
portal from `detect_ats`, source URL, and the uploaded résumé path, and writes a `dry-run` row.
Recording is **upserted by source URL** via the new `tracker.find_by_source_url` — re-running a
posting updates its existing row instead of duplicating it, and on a re-run only runner-owned
fields refresh (`resume_path`, `portal`, `method`; role/company only-if-empty). It **never
clobbers user-owned fields** (`status`, `notes`, `pay`), so a row the user advanced to
`applied`/`responded` or annotated survives repeated dry-runs. The call is best-effort — a
tracker failure is appended to `report.errors`, not raised, so it can't break the fill run.
Verified: insert/upsert/no-clobber/fill-if-empty logic (temp DB), and the full path through the
real `run_apply` against a live browser page — title parsed to role "Staff Backend Engineer" /
company "Wayfair", one row written, a second run updated the same row (still 1). See
[[016-apply-stage]], [[017-native-autofill]].

---

## 025 — Tailoring speed/quality tiers (extended thinking off by default)

**Date:** 2026-07-04
**Status:** Accepted

**Context:** Tailoring one résumé took ~2 minutes — unacceptable for a pipeline meant to
apply to many postings, and past the "under a minute" goal. This is a "how resumes are
tailored" decision (Agent Decision Framework #2), so the cause was measured before changing
anything.

**Diagnosis (benchmarked, real code path — `profile/resume.yaml` → `backend-mid-censys.md`,
1 page):** the cost is **extended thinking**, which Claude Code enables by default — NOT the
model, prompt size, or agent/tool overhead. With thinking on, the model burns 10–21k output
tokens *reasoning* before emitting the ~3k-token résumé JSON, and output-token generation is
the wall-clock cost. Controlled A/B (same Opus model, only thinking toggled): **113.8s → 39.5s**,
output tokens **10,224 → 3,125**. Things that did **not** help: switching model with thinking
left on (Sonnet/Haiku *think more* → 138–180s, slower than Opus); stripping the agent
system-prompt/tools/MCP (165s, and it *broke* input prompt-caching).

| Config | Model | Thinking | Wall | Out tokens |
|---|---|---|---|---|
| (old default) | Opus | on | 113.8s | 10,224 |
| Sonnet | Sonnet | on | 180.5s | 21,626 |
| Haiku | Haiku | on | 138.3s | 17,056 |
| **fast** | **Sonnet** | **off** | **29.7s** | 2,856 |
| **balanced** (new default) | **Opus** | **off** | **35–40s** | ~3,100 |
| **max** | Opus | on | ~114s | 10,224 |

**Decision:** Expose a user-chosen **speed/quality tier** rather than hard-coding one point.
`QUALITY_TIERS` in `backends.py` maps `fast → (sonnet, no-think)`, `balanced → (opus, no-think)`,
`max → (opus, think)`; **default = `balanced`** (best quality that stays under a minute).
Thinking is toggled via `MAX_THINKING_TOKENS=0` in the CLI subprocess env (`run_claude_cli(think=...)`);
`run_claude_cli` still defaults to `think=True`, so the answer-bank path is unchanged. Threaded
through `select_backend(name, quality)` → `tailor_resume(..., quality=)`. Surfaced as a **Quality**
dropdown in the web UI (each option labels its model + time estimate) and a `--quality` CLI flag;
the in-progress status names the expected wait so a Max run doesn't read as frozen (Guideline /
UI principle #5). Subscription billing via Claude Code is unchanged (decision 011); `max` reproduces
the exact previous behaviour, so nothing is lost — only a faster default is gained.

**Reasoning:** The bottleneck was empirically isolated to thinking, so the fix targets it
directly instead of guessing (cheaper models were *worse*). A tier knob keeps the user in
control of the speed/quality trade-off per Agent Guideline #2 — someone tailoring for a dream
job can pick Max; the bulk-apply runner can pick Fast — while a sane default (`balanced`) meets
the stated goal out of the box. **Verified:** end-to-end via the real CLI path at the new
default — 35.8s, valid `TailoredResume`, factually-grounded output with correct relevance
notes; all modules import; benchmark table above reproduced across 6 controlled runs. See
[[011-claude-code-cli-subscription]] (billing path, unchanged), [[008-pluggable-backends]]
(the backend interface this extends), [[023-tailoring-quality-and-why]] (quality of the tailored
content), [[021-async-status-feedback]] (the in-progress wait estimate).

---

## 026 — Discover stage: qualification-driven, pluggable sources, hybrid matcher, testing-mode first

**Date:** 2026-07-04
**Status:** Accepted

**Context:** The Discover stage (Stage 2) had to be designed from scratch — the "how do we
find jobs to apply to" scraping-strategy decision the framework requires be presented with
options first. Researched and verified the current (2026) landscape against official docs.
Two framing choices drove the design: (a) discovery is **qualification-driven, not
company-driven** — the user explicitly did not want to maintain a target-company list;
"filter based off qualifications more so than company"; (b) the Apply stage already drives
Greenhouse/Lever/Ashby (decisions 016/017), so a posting discovered on one of those ATSs
flows straight through Tailor → Apply with no new work.

**Options considered (source families):**
| Family | Verdict |
|---|---|
| Public ATS job-board APIs (Greenhouse `boards-api`, Lever `v0/postings`, Ashby `posting-api`) | **Chosen (primary).** Official, no-auth, full JD, no scraping (Guideline #4 clean); same ATSs Apply fills. Per-company (needs a board token). |
| Legitimate aggregator APIs (Adzuna, USAJobs, Muse, remote feeds) | **Chosen (one: Adzuna) as the breadth source** behind the same interface. Free key, broad, but snippet-only + attribution/poll terms. |
| Scraping Indeed / LinkedIn / Google for Jobs | **Rejected** — Indeed Publisher API closed to individuals; LinkedIn has no individual jobs API; Google has no public API. All require ToS-violating scraping (Guideline #4). |
| Meta-scrapers (JobSpy) / paid resellers (JSearch) | **Rejected/grey** — JobSpy scrapes Indeed/LinkedIn/Google with proxy evasion; JSearch resells Google-scraped data. Same ToS problems. |

**Options considered (qualification matching):**
| Option | Verdict |
|---|---|
| **Hybrid: free keyword pre-filter → Claude judges the top-N** | **Chosen** — bounded Claude cost regardless of posting count; keyword pass ranks/prunes, Claude reasons about seniority/semantics and names missing requirements. Mirrors decision 013. |
| Keyword scoring only | Kept as the offline/no-Claude fallback (`--no-claude`). |
| Claude judges every posting | Rejected — spends subscription tokens on obvious non-matches. |

**Decision:** Build Discover as a **pluggable source layer** (mirroring pluggable backends,
decision 008) feeding a **hybrid qualification matcher**, with a **testing mode** before the
autonomous runner:

- `discovery.py` — `Posting` (normalized) + a `Source` interface; `GreenhouseSource`,
  `LeverSource`, `AshbySource` (public no-auth APIs, full JD), and `AdzunaSource` (aggregator,
  self-skips without a free key). `Posting.to_job_description()`/`to_markdown()` emit the
  **exact fixture shape** (Markdown + YAML front matter), so Tailor/Apply need no changes.
  stdlib `urllib` (certifi CA bundle if present) — zero new deps. HTML→text via stdlib
  `HTMLParser`. Per-source failures are collected, never abort the run.
- `relevance.qualification_score()` — token-free skill-overlap score (which of the
  candidate's skills a posting asks for), reusing the existing `mentions`/`skill_terms`.
- `matching.py` — `keyword_rank` (drop < `min_skills`, rank by overlap) then `judge_fit`
  (Claude via the subscription CLI, `run_claude_cli`) on the top-N survivors → `{qualified,
  score 0-100, why, missing[]}`, grounded strictly in the résumé (judges fit, invents
  nothing). A Claude failure on one posting leaves it keyword-only.
- `filters.py` — `DiscoveryFilters` (git-ignored `profile/discovery.yaml`, seeded from
  `examples/discovery.example.yaml`): target `boards`, coarse gates (`remote_only`,
  `min_salary`, `title_exclude`), matcher knobs (`min_skills`, `top_n`), optional Adzuna
  config. Aggregator **search keywords are derived from the profile** (résumé recent titles +
  top skills), not hand-entered — the qualification-driven query. `apply_gates` applies the
  coarse gates (salary parser handles both `175000` and `$191K`).
- `pipeline.py` — the orchestrator. Default: discover → gate → match → print ranked matches
  (no browser). `--apply-first` = **testing mode**: take the single top match and run
  tailor → PDF → **headed dry-run apply you watch fill live** (never submits; Guideline #3),
  which also records a `dry-run` row via the tracker (decision 024). The autonomous
  many-postings runner builds on this same core.

**Reasoning:** Qualification-driven matching is what the project overview calls for
("filter-driven … the user controls what gets discovered") and removes the company-list
burden — companies fall out of the matching. ATS-first is the only fully-legitimate
full-text source and closes the discover→tailor→apply loop for free since Apply already
handles those ATSs; the pluggable interface lets the aggregator (and future USAJobs/remote
feeds) slot in without rework. The hybrid matcher is the same "cheap local pre-select, then
Claude on the bounded survivors" pattern proven in decision 013, keeping subscription cost
flat as discovery scales. Testing mode before autonomy follows Guideline #3 (watch one job
end-to-end before arming) and Guideline #6 (incremental, verifiable).

**Verified live:** 618 real postings fetched across Stripe (Greenhouse) / cin7 (Lever) /
Ramp (Ashby), 0 errors, full JD bodies; emitted markdown round-trips through the existing
`load_job_description`. Keyword pre-filter 618→143 (top ranks all engineering roles). Claude
judge discriminates correctly (Senior SWE 82/100 but flags a missing degree requirement;
sales AE 4/100 with detailed gaps). Full testing-mode loop ran end-to-end (discover → pick
top → rules-tailor → PDF → headless dry-run apply on the real Ashby form → `submitted:False`
→ recorded tracker row #1). Adzuna self-skips without a key and builds with profile-derived
keywords when configured. All PII/artifacts git-ignored. See [[016-apply-stage]],
[[017-native-autofill]], [[013-catalogue-preselection]] (the hybrid pattern),
[[008-pluggable-backends]] (the source interface), [[003-fixtures]] (the JD shape it emits),
[[024-track-stage]] (the dry-run row it records), [[004-respect-tos]] (Guideline #4).

## 027 — Experience-level discovery gate (title-based, lenient)

**Date:** 2026-07-05
**Status:** Accepted

**Context:** The user wants to filter discovery by experience level — intern, new grad,
etc. — so early-career runs stop surfacing senior/staff/manager roles. Needed a positive
level gate alongside the existing coarse gates in `filters.py` (`remote_only`, `min_salary`,
`title_exclude`), which run before the qualification matcher.

**Options considered:**

| Approach | Signal | Pros | Cons |
|---|---|---|---|
| **Title regex (chosen)** | Posting title | Free, deterministic; seniority reliably lives in the title; same philosophy as `title_exclude`; no extra Claude call | Titles that omit the level go undetected |
| Description/"X+ years" parse | Body text | Catches level-less titles | Noisy ("5+ years" ≠ a level), more code, still heuristic |
| Ask the Claude judge to gate level | Full JD | Most accurate | Spends a Claude call on obvious drops; the matcher already judges fit |

Second axis — how to treat titles with **no** detectable level (e.g. plain "Software
Engineer"): **strict** (keep only clearly-matching titles) vs **lenient** (drop only titles
that clearly name a *different* level; let undetected ones pass to the matcher).

**Decision:** Title regex, **lenient**. `_LEVEL_PATTERNS` maps 7 levels — `internship`,
`new_grad`, `junior`, `mid`, `senior`, `staff`, `manager` — to word-boundaried regexes;
`detect_levels(title)` returns the set named in a title. `apply_gates` drops a posting only
when the title names a level and **none** of the user's `experience_levels` is among them;
undetected titles pass through (same "missing data → keep" rule as the salary gate). New
`DiscoveryFilters.experience_levels` list; user values are normalized ("New Grad" →
`new_grad`) and unknown values ignored. Config in `profile/discovery.yaml` (example seeded).

**Reasoning:** The user chose lenient — undetected titles are more often the mid-level roles
a candidate still wants judged than noise, and the résumé+Claude matcher is the real fit
arbiter; this gate only strips the obvious wrong-tier postings cheaply. Word boundaries avoid
the false positives substring matching would cause ("intern" in "internal", "lead" in
"leading"). Title-only keeps it a zero-cost pre-matcher gate.

**Verified:** 15-title detection suite incl. false-positive traps (internal→manager not
intern; leading→∅) all correct; lenient early-career gate keeps intern/new-grad/ambiguous and
drops senior/manager; senior gate keeps senior+ambiguous and drops the rest; no-gate keeps
all. See [[026-discover-stage]] (the gates it joins), [[003-fixtures]] (the posting shape).

---

## 028 — Semantic question classification onto known field types

**Date:** 2026-07-05
**Status:** Accepted

**Context:** The Apply resolver answers form questions by keyword-matching a label to a
structured profile field or a saved bank answer (decision 018). Keyword matching misses
semantic variants: "Are you willing to work either out of our NYC office or San Francisco
office 2-3 days per week?" is functionally the same as the structured **remote/onsite**
question but shares no keywords with it, so it was captured as a brand-new blank "needs your
answer" instead of being answered. The user asked that Claude classify novel questions so they
either reuse an existing answer type or become a genuinely new one.

**Options considered:**
| Option | Verdict |
|---|---|
| Claude classifies a missed question onto a known field type; answer live from that field; **cache the mapping** | **Chosen** — correct answers survive profile edits; one Claude call per novel question, then cached |
| Cache the classified **answer** string (like generated answers) | Rejected — goes stale if the profile changes (e.g. relocate Yes→No); a mapping stays live |
| Expand keyword lists to cover more phrasings | Rejected — unbounded; can't anticipate office-specific/company-specific paraphrases |
| Embed + nearest-neighbour match to field types | Rejected — new dependency/index for a handful of fields; the LLM already available does it better |

**Decision:** Add a semantic layer **after** keyword resolution. `answer_bank.classify_question`
sends the question + a fixed set of classifiable structured types (work_authorized,
requires_sponsorship, us_citizen, willing_to_relocate, open_to_remote, desired_salary,
earliest_start_date, years_experience, how_heard, location, country) to Claude (subscription
CLI, no thinking) and returns the matching type key or None. Company-specific and demographic
questions are gated out (never auto-mapped). The resolver's `resolve_semantic()` runs on a
keyword miss for non-open-ended fields, answers **live** via `answer_for_type(key)`, and caches
the result as a `QA(maps_to=key)` in the answer bank — so future runs answer it instantly and it
tracks profile edits (a mapped entry's `answer` is intentionally blank; `resolve()` reads the
live field when `maps_to` is set). Open-ended prose questions still go to the grounded drafting
path (decision 018), not classification. The Profile UI shows mapped entries as "↔ Auto-answered
from your profile (type)" and preserves `maps_to`/`generated` through save. The Claude reply is
parsed robustly (it may reason before answering — take the last type key mentioned, unless it
concludes "none").

**Reasoning:** Directly extends the self-improving bank (decision 018) from "learn answers" to
"learn how a question maps to what we already know," which is where most repetition lives —
work-eligibility, location/remote, salary, and start-date questions are asked a hundred ways.
Caching the **mapping** rather than the answer keeps every reuse correct if the profile changes,
matching the system's truthfulness-by-construction stance. Cost stays bounded: one classification
per genuinely-novel question, then free. **Verified:** the user's office-days example →
`open_to_remote`; sponsorship/start-date variants classify correctly; company-specific and
no-type questions → None; the mapped entry answers live and flips Yes→No when the profile field
changes; UI save round-trips `maps_to`. See [[018-self-improving-answer-bank]] (the bank this
extends), [[011-claude-code-cli-subscription]] (billing path), [[016-apply-stage]] (the resolver).

## 029 — Persist tailored résumé PDFs to a stable, bounded store

**Date:** 2026-07-05
**Status:** Accepted

**Context:** Each dry-run tailors a résumé and writes the PDF the Apply form uploads. That PDF
was written to `$TMPDIR/tailored_*.pdf` via `tempfile.NamedTemporaryFile(delete=False)`, and the
Track row's `resume_path` pointed at it. macOS purges `$TMPDIR`, so the file backing a recorded
application would eventually vanish — you could not go back and see the résumé a given
application used, which is a Track-stage requirement (NEXT_STEPS lists "tailored resume used" as a
tracked field). The user wanted to review dry-run output quality but also flagged a real concern:
persisting a PDF per application could bloat storage.

**Sizing (measured, not assumed):** one tailored PDF is ~4.7 KB (fpdf2, real text, no embedded
fonts). Discovery/apply already **upserts by `source_url`**, so files scale with *unique postings
applied to*, not runs: 1,000 → 4.6 MB, 10,000 → 46 MB, 50,000 → 230 MB. Bloat is a minor concern
at this scale (the base résumé PDF alone is 281 KB, 60× one tailored file); the goal is a bounded,
self-cleaning store, not crisis-aversion.

**Options considered:**
| Question | Choice | Rejected alternatives |
|---|---|---|
| What to store per application | **The exact PDF uploaded** (~5 KB) | Structured JSON + regenerate PDF — a regenerated PDF wouldn't match what was actually submitted once the base résumé is edited (drift), losing the exact-record property; JSON-only has the same drift problem |
| How to bound growth | **Per-posting overwrite + cascade delete + size cap** | Cascade-only (no hard ceiling); upsert-only (files linger after a row is deleted) |

**Decision:** New leaf module `applicationbot/resume_store.py` (imported by both `pipeline` and
`tracker`, imports neither — no cycle):
- **Location:** `profile/tailored/`, git-ignored (covered by `profile/*` and `*.pdf`).
- **Naming:** `<company-slug>-<role-slug>-<sha1(source_url)[:8]>.pdf` — deterministic on the
  posting URL (the same dedup key the tracker upserts on), so a re-run **overwrites** the same
  file rather than accumulating. `pipeline._apply_one` now calls `resume_store.write_pdf(...)`
  instead of `tempfile.NamedTemporaryFile`.
- **Cascade delete:** `tracker.delete_application` deletes the row's file, but only via
  `resume_store.delete_if_managed`, which unlinks **only** paths resolving under
  `profile/tailored/` — a user-supplied `--pdf` outside the store is never touched.
- **Size cap:** `prune()` drops the oldest PDFs (by mtime) once the folder passes `MAX_BYTES`
  (100 MB ≈ 20k files); runs on each write, never removes the file just written. A backstop that
  shouldn't trip given the first two mechanisms.
- **Migration:** `scripts/migrate_tailored_pdfs.py` (idempotent) copies any existing row's
  `$TMPDIR` PDF into the store and repoints `resume_path`; skips already-managed rows and reports
  missing files.

**Reasoning:** The exact PDF is the honest record of what a form received and is cheap; JSON
regeneration would drift from what was submitted the moment the base résumé changes. Growth is
bounded structurally (one file per posting) with a hard ceiling as insurance, so the store stays
tied to what's actually in the tracker. **Verified:** deterministic naming + re-run overwrite;
`is_managed` refuses to delete an external file; prune drops oldest and keeps the newest; cascade
delete through a temp-DB tracker removes the managed PDF and leaves a user-supplied one intact;
the migration moved the 3 real dry-run rows into `profile/tailored/` and is a no-op on re-run.
See [[024-track-stage-sqlite]] (the store this feeds `resume_path`), [[026-discover-stage]]
(`_apply_one`, where the PDF is written), [[016-apply-stage]] (upload of the uploaded file).

## 030 — More discovery sources: broaden the ATS layer (SmartRecruiters + Recruitee), not aggregators

**Date:** 2026-07-05
**Status:** Accepted

**Context:** The user asked to improve web-scraping/discovery breadth, naming **hiring.cafe**
and **LinkedIn** as candidates, with an explicit goal: *"expose ourselves to as many job
postings as possible to train our autofill to work on any site/system"* — i.e. breadth is
wanted primarily to exercise the Apply autofill across **diverse ATS form systems**, not just
Greenhouse/Lever/Ashby (decisions #016/#017/#026). Researched the 2026 landscape (two parallel
web-research passes) **and probed every candidate API live** rather than trusting third-party
docs — which proved essential, because the headline candidates had changed.

**Options considered (verified live this session):**

| Candidate | Live probe result | Verdict |
|---|---|---|
| **hiring.cafe** (the user's #1) | `POST /api/search-jobs` → **405**; `GET` → **401 Unauthorized**. Frontend now calls `/ssr/search-jobs` with `Authorization: Bearer ${token}` where the token comes from a **session auth call** (not a public constant). The scraper repos the research cited are **stale**. | **Rejected.** Using it requires replaying an auth token issued to their logged-in frontend = circumventing an access control, against Guideline #4 + their ToS "don't reproduce/redistribute" clause. |
| **LinkedIn** | No public/candidate jobs API; partner Job Posting API is post-only **and closed to new partners**; scraping breaches their User Agreement (hiQ v. LinkedIn). | **Rejected** (confirms #026). |
| **The Muse** | Works; full JD (`contents`), but `landing_page` → **themuse.com pages, not the underlying ATS** (extra hop to the real form); heavily international. | Deferred — weak for the ATS-form-diversity goal. |
| **USAJobs** | Full JD, clean, but routes into non-autofillable government portals. | Deferred — discovery/tracking only, not an Apply target. |
| **SmartRecruiters** | `GET api.smartrecruiters.com/v1/companies/{company}/postings` (+ `/{id}` detail) → full JD in `jobAd.sections`, real `jobs.smartrecruiters.com` apply URL. **Verified:** PublicStorage 5/5, BoschGroup 3/3 full JD. | **Chosen.** A distinct form system; public, no-auth, full JD, direct apply. |
| **Recruitee** | `GET {company}.recruitee.com/api/offers/` → one call, full JD inline (`description`+`requirements`), `careers_apply_url`. **Verified:** bunq 16/16. | **Chosen.** Distinct form system; cleanest (single call, like GH/Lever/Ashby). |
| **Workable** | Anonymous widget `apply.workable.com/api/v1/widget/accounts/{sub}` returned **0 jobs for every slug tried**; reliable path needs an SPI token. | **Deferred** — couldn't verify a working no-auth endpoint; don't ship unverified (Guideline #11). |

**Decision:** Instead of adding an aggregator (whose apply links are indirect or ToS-encumbered),
**broaden the ATS source layer itself** — add `SmartRecruitersSource` and `RecruiteeSource` as new
`Source` subclasses in `discovery.py`, registered in `ATS_SOURCES`. **No schema change**: the
existing `Board{ats, token}` model already accepts any `ats` string, so config is just
`{ats: smartrecruiters, token: <Company>}` / `{ats: recruitee, token: <company>}`. SmartRecruiters'
list endpoint omits the JD body, so it fetches per-posting detail (an N+1) bounded by
`_SR_MAX_POSTINGS = 100` per company. Both normalize to the same `Posting` shape and flow straight
through Tailor → Apply; postings on these ATSs hit the Apply driver's **generic** per-field path
(no native adapter yet), which is exactly the "test autofill on new systems" the user wants.

**Reasoning:** The user's goal is autofill robustness across form systems, and a *new ATS* delivers
that far more directly than an aggregator that dumps the applicant on a listing page or an
ATS-we-already-handle. Both chosen sources are fully compliant (public, documented-shape, no-auth,
full JD), reuse the entire pipeline, and add zero dependencies (stdlib `urllib`, like #026).
hiring.cafe and LinkedIn were rejected on Guideline #4 — and the hiring.cafe finding is a reminder
to **probe live, not trust research**: its API had moved behind auth since the cited scrapers were
written. Caveat surfaced: not every SmartRecruiters company exposes its postings API publicly (many
big names return 0 postings — surfaced cleanly, not as an error); Workable and The Muse remain
available follow-ups behind the same interface.

**Verified live:** SmartRecruiters (PublicStorage 5/5, BoschGroup 3/3) + Recruitee (bunq 16/16)
return full JD, direct apply URLs, and round-trip through `to_job_description()`/`to_markdown()`;
the full pipeline ran discover → gate → match over 505 postings (recruitee:bunq + greenhouse:stripe)
with 0 errors. See [[026-discover-stage]] (the source interface + pipeline this extends),
[[016-apply-stage]] (the generic autofill these new ATSs exercise), [[017-native-autofill]],
[[004-respect-tos]] (Guideline #4, why hiring.cafe/LinkedIn are out), [[015-linkedin-import]]
(the compliant LinkedIn path).

## 032 — Workable source + aggregator→ATS bridge (turn search-only hits into auto-apply candidates)

**Date:** 2026-07-05
**Status:** Accepted

**Context:** Continuing decision #030's "broaden the ATS layer for autofill diversity." Two
follow-ups: (a) add **Workable** (the one gap in the common auto-apply ATS set: Greenhouse,
Lever, SmartRecruiters, Workable); (b) evaluated **Adzuna / USAJobs / Jooble** and ChatGPT's
source recommendations. Verified live that the aggregators are **search-only for us**: Adzuna's
`redirect_url` and Jooble's `link` both point at the aggregator's *own* domain, so the API
response never reveals the destination ATS — and ChatGPT's "partner ecosystem" row (SEEK / Indeed
/ LinkedIn) is **inapplicable**: all three are employer/partner-gated and un-onboardable by a solo
dev (Indeed's Publisher API 301s to partners.indeed.com; SEEK needs a hirer relationship; LinkedIn
is partner-gated + post-only). So aggregators can only feed auto-apply if we **resolve the
redirect and detect the ATS** — the bridge.

**Decision:**
- **`WorkableSource`** (`discovery.py`, registered in `ATS_SOURCES`): `POST
  apply.workable.com/api/v3/accounts/{account}/jobs` (token-paginated) + `GET api/**v2**/…/{shortcode}`
  for the full JD (list omits the body — an N+1 like SmartRecruiters, bounded by
  `_DETAIL_MAX_POSTINGS`). Apply URL constructed as `apply.workable.com/{account}/j/{shortcode}/`.
  `fetch_json` extended with optional `method`/`body` so it can POST (backward-compatible).
- **Aggregator→ATS bridge** (`discovery.py`): `resolve_redirect(url)` follows the 30x chain
  (HEAD→GET) to the real destination; `bridge_aggregator_postings(postings)` — for each posting
  whose `ats` is an aggregator (`adzuna`/`jooble`) — resolves the link, and when it lands on a
  recognized ATS (`detect_ats_from_url`, extended here to cover recruitee + workable) **rewrites
  `ats` + `apply_url`** so the hit flows into Apply, records `extra['bridged_from']` /
  `['auto_applyable']`, and — for the ATSs with a public JD API (Greenhouse/Lever/Ashby, via the
  curated-list `_resolve_jd` resolvers) — **upgrades the aggregator's snippet body to the full
  JD**. Bounded by `_BRIDGE_MAX = 60` redirect resolutions/run. Wired into `pipeline.discover_and_match`
  (new `bridge=True` param + `PipelineResult.bridged`), before matching so the matcher ranks on the
  upgraded JD; a **no-op when no aggregator postings are present** (zero added latency on ATS-only runs).

**Reasoning:** Workable completes the practical auto-apply ATS set and is a new form system for the
autofill (decision #030's goal). The bridge is the only compliant way an aggregator (which just
hands back a redirect) can feed auto-apply — it also **solves Adzuna/Jooble being snippet-only** by
re-fetching the full JD from the real ATS, so a bridged hit tailors/matches as well as a native ATS
hit. Reused the parallel agent's `detect_ats_from_url` + `_resolve_jd` (built for the early-career
curated feeds, #031) rather than duplicating — coordinated via the agent bus (claimed
`discovery.py`/`pipeline.py`). USAJobs/Jooble/Muse remain deferred behind the same interface;
the partner ecosystem is out (Guideline #4).

**Verified live:** Workable (mlabs 4/4 full JD, correct apply-URL format, JD round-trip). Bridge:
`detect_ats_from_url` correct across all 6 ATSs + workday + aggregator; `resolve_redirect` follows a
real 30x; a synthetic Adzuna hit → **greenhouse**, snippet **upgraded to the full 7.5k-char JD**
(`jd_upgraded=True`, `auto_applyable=True`); non-aggregator postings untouched; and the full
`discover_and_match` bridged an injected aggregator posting in-pipeline (→ greenhouse, 11.7k-char JD)
through to a match. See [[030-more-ats-sources]] (the layer this extends), [[026-discover-stage]]
(the pipeline + `detect_ats_from_url`/`_resolve_jd` it reuses), [[016-apply-stage]] (where bridged
apply URLs land), [[014-agent-bus]] (parallel-work coordination), [[004-respect-tos]].

**Update (same session):** (1) **JD upgrade extended to all six ATSs.** `_resolve_jd` now also
resolves SmartRecruiters (`api.smartrecruiters.com/…/postings/{id}`), Workable
(`api/v2/accounts/{acct}/jobs/{shortcode}`), and Recruitee (`{co}.recruitee.com/api/offers/{slug}`)
— so a bridged aggregator hit on any of our fillable ATSs gets its snippet replaced with the full
JD (previously only GH/Lever/Ashby). This also broadens what the curated early-career feeds (#031)
can resolve. *Verified live:* SmartRecruiters 5601, Workable 6130, Recruitee 4133 chars; bridge
upgraded a SmartRecruiters snippet → full JD. (2) **Dashboard "Sources" section.** Added a live,
read-only **"Where your postings come from"** overview at the top of the Discover tab (new
`GET /sources`) — target boards grouped by ATS, Adzuna status (**active via your key /
environment variables / not set up**), early-career feeds on/off, the bridge, and the list of
auto-fillable ATSs. Fixed the board-picker to offer **all six** ATSs (it only listed
greenhouse/lever/ashby, so the SmartRecruiters/Recruitee/Workable sources built in #030/#032 were
unselectable). Wired the **Adzuna setup path**: a clickable `developer.adzuna.com` free-key link in
the settings editor, keeping the **own-key option** (paste your `app_id`/`app_key`, or set
`ADZUNA_APP_ID`/`ADZUNA_APP_KEY` env vars — `build_aggregator` reads either). *Verified live:*
served JS `node --check`-clean, `/sources` HTTP round-trip reflects a saved config (real
`discovery.yaml` backed up + restored byte-for-byte), and a headless-Chromium drive of the Discover
tab renders the overview + all-six-ATS dropdown + free-key link with zero console errors.

---

## 031 — Early-career discovery via community-curated JSON feeds

**Date:** 2026-07-05
**Status:** Accepted

**Context:** With senior-heavy target boards (e.g. Stripe), the Claude fit-judge correctly
denied every posting for a junior/intern résumé — 0 of 10 judged cleared the fit cutoff. The
user asked for boards curated toward early career. Verified the 2026 landscape: the dedicated
early-career platforms (RippleMatch, Handshake, WayUp) are all login/partner-gated with no
individual API, and Adzuna's ToS only licenses a 14-day trial. The community, however,
maintains daily-updated machine-readable lists of new-grad and internship roles.

**Options considered:**
| Option | Verdict |
|---|---|
| **SimplifyJobs new-grad + internship `listings.json` feeds** | **Chosen** — early-career by construction (no senior roles), ~2,000 active new-grad + ~1,250 intern, ~40% link to Greenhouse/Lever/Ashby (we fetch JD + fill), free, daily-updated |
| Adzuna with "new grad"/"intern" keywords | Rejected as a persistent source — ToS licenses only a 14-day trial; keep evaluation-only |
| RippleMatch / Handshake / WayUp | Rejected — no individual public API (login/partner-gated) |
| USAJobs Pathways (GRADUATES/STUDENT) | Deferred — clean + full JD, but federal portals aren't autofillable (discovery/tracking only) |

**Decision:** New `CuratedListSource` (`discovery.py`, `DiscoveryFilters.early_career`,
off by default). It fetches the SimplifyJobs New-Grad + Summer2026-Internships feeds, keeps
`active==true` roles whose apply URL is a **resolvable + fillable ATS (Greenhouse/Lever/Ashby)**,
dedupes by URL, ranks them by **title-relevance to the résumé** (role-word + skill overlap,
excluding generic level tokens), and **resolves the full JD for the top `max_resolve`** via that
ATS's single-job endpoint (Greenhouse `/jobs/{id}`, Lever `/postings/{site}/{id}`, Ashby board
index by uuid) — emitting normal full-JD `Posting`s so the matcher/apply pipeline is unchanged.
The lists are URL-only (title + link, no JD text), which is why JD resolution is needed;
resolution failures fall back to a title-only body. Because a verbose senior board JD's larger
skill overlap would otherwise crowd curated roles out of the judged top-N, **curated postings are
ranked ahead of raw board postings** in `keyword_rank` (they're already pre-vetted to the user's
level). Config exposed in the Discover-settings editor (enable + kinds + how many to resolve).
Personal-use only: the feeds carry no explicit redistribution license, so this reads public job
links to apply for oneself, not to redistribute (Guideline #4).

**Reasoning:** It's the only clean, no-scraping way to get *early-career-specific* breadth — the
platforms built for it are all gated. Resolving full JD from the linked ATS (rather than judging
on title alone) keeps fit-judging accurate, and reuses ATS endpoints we already trust. Verified
end-to-end: enabling early-career on the same senior-heavy config took the run from **0 cleared**
to **4 cleared** (AppLovin New-Grad 82, MARGO 78, Blitzy 68, Evolver 68), while the senior board
roles still correctly denied (≤42) — exactly the intended effect. See [[026-discover-stage]] (the
source interface + matcher), [[027-experience-level-gate]] (complementary title-level gate),
[[016-apply-stage]] (fills the linked ATS), [[004-respect-tos]] (Guideline #4, personal-use only).

---

## 033 — Self-improving dropdown resolver

**Date:** 2026-07-05
**Status:** Accepted

**Context:** Dropdown fields kept breaking one at a time — country ("US" vs "United States"),
degree ("Bachelor's Degree" vs the verbose résumé string), and then school (a big searchable
list). Each was patched with a hardcoded option-hint. The user's point: this is exactly what the
system should learn automatically as it runs more autofills, not something to hardcode per field.

**Options considered:**
| Option | Verdict |
|---|---|
| Keep hardcoding per-field option hints | Rejected — doesn't scale; a new dropdown always breaks until patched |
| **Claude picks the option at fill time when literal/hint match fails, and we cache the value→option mapping** | **Chosen** — generic across any dropdown; self-improves; one Claude call per novel value, then free |
| Ship a static aliases table (schools/degrees/countries) | Rejected — huge, stale, still misses site-specific option text; the learned cache subsumes it |

**Decision:** Extend the combobox filler into a self-improving resolver (extends the answer-bank
decisions 018/028). `_fill_combobox` now: (1) literal-matches the answer + hints + **learned
aliases** against the options shown on first open; (2) if no match and it's a static list, has
**Claude pick the best option from those FRESH options** (`answer_bank.pick_dropdown_option`) —
done before any typing pollutes the react-select filter; (3) for searchable lists, types to
filter and Claude-picks from the results. Every Claude pick is guarded by a deterministic
**token-overlap check** (the chosen option must share a meaningful, non-generic word with the
answer) so it can never commit an unrelated same-category option ("Harvard" for "Penn State").
The chosen mapping is stored on `AnswerResolver.learned_options`, persisted to
`ApplicationProfile.dropdown_aliases` (normalized value → matched option texts) after the run,
and consulted first on future fills — so a value that once needed Claude matches instantly with
no call. The pick prompt is decisive about "same institution / campus variant / broader-narrower
degree → pick the primary one; different entity → none".

**Reasoning:** Matches how the answer bank already learns (cache what Claude resolved, reuse it),
applied to the last brittle surface — dropdown option matching. It removes the need to hardcode a
hint per dropdown while staying safe (the token guard prevents confident-wrong picks, which on a
submitted form are worse than a flagged gap). Verified live on the Stripe embedded Greenhouse
form: country still "US" and gender "Male" (no regression from the rewrite); a hint-less degree
resolved to "Bachelor's Degree" via the Claude pick and was **learned**; a subsequent fill with
generation OFF matched "Bachelor's Degree" from the learned alias with no Claude call. Picker
unit tests: "The Pennsylvania State University" → "…-Main Campus", "Rutgers University" →
"…-New Brunswick", and Penn-State-vs-Harvard/MIT/Stanford → none (guard). See
[[018-self-improving-answer-bank]], [[028-semantic-question-classification]] (the learning
pattern this extends), [[016-apply-stage]] (the filler), [[011-claude-code-cli-subscription]].

## 034 — Strip the headless Claude session; batch fit judging; schema-enforced JSON

**Date:** 2026-07-06
**Status:** Accepted

**Context:** Tailoring and fit judging were burning through subscription credits and running
slowly. Measured root cause: every `run_claude_cli` call spawned a **full default Claude Code
session** — the coding-agent system prompt, all tool schemas, MCP servers, skills, settings, and
this project's 16KB CLAUDE.md. A trivial "reply ok" call carried ~40,000 tokens of context
(3,432 input + 11,804 cache-write + 24,787 cache-read; $0.089 cost-equivalent). Fit judging
multiplied this by N: `match()` judged the top 10 postings serially, one spawn each (~400k tokens
of pure overhead per discovery run), and `judge_fit` passed no `--model`, silently inheriting the
CLI's default model (typically Opus) for a one-sentence JSON verdict.

**Options considered:**
| Option | Verdict |
|---|---|
| **Strip the session (`--system-prompt`, `--tools ""`, `--strict-mcp-config`, `--setting-sources ""`)** | **Chosen** — same prompt text reaches the model minus irrelevant coding-agent context; measured 184 tokens vs ~40,000 per call (74x, $0.0012 vs $0.089), and ~1s faster |
| Switch to the `anthropic` API SDK | Rejected — bills the metered API; subscription-only usage is a standing constraint (#011) |
| Keep per-posting judge calls, parallelize with threads | Rejected — fixes latency only; still pays N spawns of overhead |
| **Batch fit judging: one call per 5 postings, JSON array back** | **Chosen** — résumé sent once per chunk; 10 postings = 2 spawns instead of 10; chunking keeps failure blast-radius at 5 postings (degrade to keyword-only, never abort — Guideline #11) |
| **Pin the judge to Sonnet (`JUDGE_MODEL = "sonnet"`)** | **Chosen** — a strict 0-100 JSON verdict is a classification task; previously the model was undefined (CLI default) |
| **`--json-schema` structured output for tailor + judge** | **Chosen** — CLI guarantees schema-valid JSON; removes the tailor's retry-on-bad-JSON loop double-spend risk and the 4.8k-char schema dump from the prompt |

**Decision:** `run_claude_cli` now always runs a stripped headless session and accepts `system`
(replaces the system prompt) and `json_schema` (CLI-enforced output shape). The tailor backend
passes `SYSTEM_PROMPT` and the `TailoredResume` schema through those flags instead of embedding
them in the prompt. `matching.judge_fit_batch` judges up to `JUDGE_BATCH_SIZE=5` postings per
call on `JUDGE_MODEL="sonnet"`; `match()` chunks the top-N through it, mapping verdicts back by
index — a failed call or skipped verdict leaves those postings keyword-only with a recorded
error. `judge_fit` (single posting) remains as a thin wrapper. The answer bank benefits from the
stripped session automatically. Quality tiers (fast/balanced/max) are unchanged.

**Reasoning:** Prompts, judge instructions, and tier semantics are byte-for-byte preserved where
they matter — the only removed context was Claude Code scaffolding irrelevant (arguably
distracting) to tailoring/judging. Verified end-to-end with a synthetic résumé: batched judge
returned correct verdicts for a match (88, qualified) and a deliberate non-match (2, unqualified)
in one 7.1s call; tailor via `--json-schema` produced a valid drift-free résumé in 13.9s on the
fast tier (previously ~30s). Net effect for a 10-posting discovery run: ~400k+ overhead tokens →
~15k, judging wall-clock from minutes to well under a minute. See [[011-claude-code-cli-subscription]],
[[013-catalogue-preselection]], [[025-hybrid-qualification-matching]].

## 035 — Submit stage: safety architecture, build order, and fixture-based verification

**Date:** 2026-07-06
**Status:** Accepted (user-approved)

**Context:** A full-system audit (2026-07-06, four parallel deep-dives) found the submit
half of the product unbuilt: `apply.py` hardcodes `submitted = False`, there is no armed
mode, no kill switch, no loop beyond one application per run, and no support for
account-gated portals (Workday ≈32% / iCIMS ≈10% of US enterprise postings). The user
directed: fold findings into NEXT_STEPS.md, delegate UI/UX to parallel agents, focus on
the heaviest engine work, minimize token-heavy live dry-runs, and build toward
"fill AND submit any application format/site."

**Options considered (safety switch representation):**

| Option | Pros | Cons |
|--------|------|------|
| **`profile/safety.yaml` + `profile/KILL` file (chosen)** | Works identically for CLI, web UI, and a future scheduled runner; state inspectable on disk; kill switch checked before every submit; git-ignored under `profile/` | A config file a user could leave armed |
| CLI flag only (`--arm`) | Explicit per-run intent | No standing kill switch; web/scheduled runs can't arm without plumbing the flag everywhere |
| Config AND flag (double gate) | Maximum deliberateness | More friction/plumbing than the product's "arm once, then autonomous" intent |

**Options considered (build order):**

| Option | Pros | Cons |
|--------|------|------|
| **Submit-first (chosen)** | Fillability gate (hours) → submit path + safety on Greenhouse → runner + Claude-cap resilience → multi-page → Workday; each step verifiable offline | Scale (runner) lands second |
| Runner-first | Scale earlier | Every run ends in a no-op until submit exists |
| Workday-first | Attacks the largest market gap | Multi-week with nothing shippable; submit/runner still missing on ATSs we already fill |

**Decision:** (1) Arming lives in git-ignored `profile/safety.yaml` — `armed: false` by
default plus `max_submissions_per_run`; a real submit additionally requires the absence of
`profile/KILL`, which is checked immediately before every submission and halts the whole
queue when present (the future web STOP button just creates it). (2) Build order:
fillability gate → Greenhouse submit path with a pre-submit gate (any unresolved REQUIRED
field aborts and records a blocked outcome instead of pausing for a human) → autonomous
runner + usage-cap resilience → multi-page navigation → account-gated portals (Workday).
(3) Verification policy: submit logic is developed and tested against **local HTML form
fixtures** driven by Playwright (zero tokens, zero real postings — Guideline #3); one
consolidated live dry-run per milestone at most.

**Reasoning:** The safety file + kill file is the only representation that serves all
three entry points (CLI, web, scheduled runner) without new plumbing, and it makes
Guideline #3's "deliberate arming" a visible artifact rather than a transient flag.
Submit-first converts the existing, verified fill engine into the actual product on the
~35-40% of postings we can already reach, before spending multi-week effort on Workday.
See [[016-apply-per-ats-playwright]], [[026-discover-qualification-driven]].

## 036 — Semantic answer-bank matching (reuse a saved answer for any rewording)

**Date:** 2026-07-07

**Context:** Banked custom answers were only reused when a new form's question matched the
saved phrasing exactly or as a substring (`apply.py` resolver). The Claude fallback
(decision 028) only classified questions onto the 11 structured profile fields, never
against the user's own answer bank — and free-text inputs skipped it entirely. Result:
questions the user had already answered ("How many years of experience do you have with
React?" → "3") were skipped and re-captured whenever a form reworded them ("Years of React
experience"), defeating the answer bank's answer-once purpose.

**Options considered:**

| Option | Pros | Cons |
|--------|------|------|
| **Claude matches the question against banked Q→A pairs (chosen)** | Handles arbitrary rewording; judges the *answer's* fitness, not just question similarity (a saved "Yes" to "travel up to 25%?" is correctly refused for "what percentage of travel?"); same pattern as decisions 028/033; match learned as an alias so repeats cost no Claude call | One extra Claude call the first time a reworded question is seen |
| Fuzzy string matching (token overlap / edit distance) | No Claude call; deterministic | Misses true paraphrases and false-matches near-strings with opposite meaning ("willing to relocate?" vs "willing to travel?") — confident-wrong answers on an outward-facing form |
| Embeddings + similarity threshold | Fast at scale | New dependency + index to maintain for a bank of tens of entries; topical similarity ≠ functional equivalence |

**Decision:** On a literal bank miss (and after the decision-028 structured classify),
`answer_bank.match_banked_question` sends the new question plus the banked (question,
answer-preview) pairs to Claude, which returns the pair whose *saved answer correctly
answers the new question* — functional equivalence, else `none`. Wired into both
`resolve_semantic` (selects/radios/checkboxes/comboboxes) and `freetext_answer` (before
drafting, covering short text fields too). A hit is cached to the bank as an alias — the
new phrasing with the same answer (or the same `maps_to`, keeping mapped entries live from
the profile) — so the next encounter matches literally with zero Claude calls.
Company-specific and demographic questions are never bank-matched (unchanged handling).

**Reasoning:** The bank's contract is "answer once, reuse everywhere"; exact-phrasing reuse
silently broke it for every reworded repeat. Claude-judged functional equivalence is the
only option that both catches paraphrases and refuses same-topic-different-question traps,
and the learned alias keeps steady-state cost identical to the old literal match. Verified
offline (`tests/test_bank_semantic.py`, mocked CLI) and live: reworded banked questions
resolve, unbanked ones stay captured for the user.
See [[018-answer-bank]], [[028-semantic-question-classification]], [[033-dropdown-resolver]].

## 037 — Discovery snapshot cache (skip the re-search on repeated dry-runs)

**Date:** 2026-07-07

**Context:** Every run of `discover_and_match` re-fetched all configured boards over the
network and re-ran the Claude fit judge on the top-N postings — *every time*. Postings
the user applied to are recorded in the tracker and dropped next run (`skip_seen`), but
every posting that was discovered and judged yet **not** applied to (everything below the
top match, or beyond a run/submission cap) got rediscovered and rejudged from scratch on
the next dry-run. Repeated dry-runs — the normal way you iterate before arming the
runner — therefore paid the full network + Claude cost each time to surface the same
postings before the autofill even started. The user asked to save those un-used postings
so future dry-runs don't search every time.

**Options considered:**

| Option | Pros | Cons |
|--------|------|------|
| **Snapshot cache, skip search if fresh (chosen)** | Reuses the *whole ranked list + Claude verdicts*; a fresh dry-run does zero network + zero Claude; simplest data model (one JSON file) | Won't see brand-new postings until the freshness window expires or `--fresh` is passed |
| Always search, reuse cached verdicts | Still finds new postings each run; only re-judges the new ones | Doesn't remove the board-search latency the user complained about; needs per-posting verdict store keyed by résumé |
| Cache leftovers, merge into each run | Closest to "save the ones not used" literally | Still hits every board each run; merge/dedup complexity; smallest speed win |

**Decision:** After a live discovery, `discovery_cache.save` writes the full ranked result
(postings + Claude verdicts + coarse counts) to git-ignored
`profile/discovery_cache.json`. On the next call, unless `force_fresh` (CLI `--fresh`) or
`cache_ttl_hours=0`, `discovery_cache.load` returns that snapshot **iff** it is younger
than `cache_ttl_hours` (default 12h) **and** a fingerprint over the résumé, the exact
source set (board tokens / aggregator config / curated kinds, via source names), the
gate/matcher filters, and the effective Claude-availability flag all match — otherwise a
clean miss falls through to a real search. A cache hit skips the board fetch **and** the
Claude judge entirely; the only per-run work is re-applying `skip_seen` against the
*current* tracker, so a role applied to since the snapshot was saved still drops out.
Wired once in `discover_and_match`, so the pipeline CLI, autonomous runner, and web UI
all benefit; both CLIs gained `--fresh`. Caching is disabled when `extra_sources` are
injected (the fingerprint can't capture ad-hoc sources). The effective-Claude flag is in
the fingerprint so a keyword-only snapshot (CLI absent) is never served once Claude is
available.

**Reasoning:** The user's iteration loop is repeated dry-runs, and the dominant cost —
board latency + Claude tokens — was being paid to re-derive an identical ranked list.
The snapshot is résumé/filters-fingerprinted so a real change invalidates it (never
serving verdicts judged against a stale résumé), TTL-bounded so postings don't go stale
silently, and `--fresh` is always available for an on-demand re-search. The tracker
remains the source of truth for "already applied," re-applied on every hit, so the cache
can only ever *save* work, never re-surface a used role. It holds discovered postings and
match notes (PII), so it lives under git-ignored `profile/` (Agent Guideline #12).
Verified offline (`tests/test_discovery_cache.py`, stubbed network + Claude): a second
run reuses without a search, `--fresh`/TTL-0/résumé-change all force a re-search, and
skip_seen prunes a now-tracked role from a cache hit.
See [[026-discover-qualification-driven]], [[035-submit-stage-safety-switch]].

---

## 038 — Salary expectation follows the posting's advertised pay band (midpoint)

**Date:** 2026-07-07 · **Status:** Accepted

**Context:** In a dry-run the bot filled `85000` for a posting whose JD stated a *CA Base
Pay Range of $124,000 – $186,000* — ~$40k below the floor. The salary rule returned the
static profile figure (`desired_salary`) verbatim, blind to the posting, so any posting
whose band sits above the stored number was actively under-asked.

**Options considered:**

| Option | Pros | Cons |
|--------|------|------|
| Top of band | Never undersells; standard negotiation advice | Can read as inflexible; overshoots when the band is wide |
| **Midpoint of band (chosen)** | In-band by construction; neither undersells nor caps at the ceiling; a defensible neutral ask | Not the maximum obtainable figure |
| Bump stored figure up to the floor | Preserves the user's number when already in-band | A below-band stored figure still lands at the very bottom |
| Keep static figure | No new parsing | The reported bug — undersells every above-band posting |

**Decision (user choice):** When the posting advertises a pay band, fill its **midpoint**;
otherwise fall back to `desired_salary`. The band is parsed by `AnswerResolver`
(`_posting_pay_range`) from a specific `$X – $Y` pattern (dash or "to"; `K` notation
handled) — from the structured `Posting.compensation` string first, then the JD body.
Bands are accepted only when both figures are ≥ 1000, which excludes hourly rates
("$40 – $60"). The resolver gained a `pay` field, wired from `p.compensation` in the
pipeline. Both the keyword salary rule and the classified `desired_salary` type route
through one `_salary_expectation()` helper so live and cached answers agree. The
standalone `apply` CLI (no posting metadata) passes no band and keeps the stored figure.

**Reasoning:** Under-asking is a silent, per-application loss with no signal to the user,
so it must be fixed in the autonomous path, not left for review. The midpoint keeps the
answer inside whatever the employer already published — grounded, never fabricated — and
degrades safely to the user's own figure when nothing is advertised. Parsing prefers the
reliable structured field and only falls back to prose with a tight two-`$`-figure
pattern, avoiding stray numbers in the JD. Verified with seven cases (JD-body band,
compensation-string band, `K` notation, "to" separator, no-band fallback, hourly excluded,
and `resolve()`/`answer_for_type` routing); full suite 67/67 green.
See [[016-apply-stage-automation]], [[028-semantic-question-classification]].

---

## 039 — Dynamic salary fallback when a posting advertises no pay band

**Date:** 2026-07-07 · **Status:** Accepted

**Context:** Decision 038 fixed the under-ask *when a posting publishes a band* (the resolver
fills its midpoint). When a posting publishes **nothing**, it still fell back to the single
static `desired_salary` — the same figure for a junior role in a low-cost metro and a senior
role in SF. The user asked for that fallback to be dynamic: "a saved number based on location
and position … generated by looking at average salaries in areas," with two sources that
"agree on a number, saved until it looks extremely wrong."

**Options considered:**

| Axis | Options | Choice |
|------|---------|--------|
| Data source | Claude estimate · external API · static location×role table | **Mix:** Claude + Adzuna cross-check |
| External API | Adzuna (role+location salary averages, free key) · BLS OES (no key, coarse SOC codes) · Claude-only-first | **Adzuna** (already integrated for discovery — reuses the same keys) |
| On disagreement (>20%) | take lower · trust API · average anyway | **Take the lower** (never over-ask on a shaky estimate) |

**Decision:** New `applicationbot/salary.py`. When a posting advertises no band, the pipeline
pre-computes a market estimate for (title, location, `years_experience`) and injects it into
the resolver as `market_salary`; the resolver's precedence is **advertised band midpoint →
market estimate → stored `desired_salary`**. The estimate is the cross-check of two sources
(`reconcile`): Claude's median range estimate and Adzuna's mean advertised salary for the
query. Both within 20% → their mean; wider → the **lower**, with the divergence recorded.
Results are cached per (title, location) in git-ignored `profile/salary_cache.json` (mirrors
decision 037's cache location) with a 30-day TTL — a cache hit makes **zero** network/Claude
calls. When a *later* posting for the same (title, location) *does* advertise a real band,
`validate_against_band` opportunistically checks the cached estimate against it and drops the
entry if it sits >40% outside the band ("extremely wrong"), so real market data corrects a
stale guess over time. Band parsing (`advertised_band`, the `$X–$Y` regex from decision 038)
moved into `salary.py` so the resolver and pipeline parse bands identically. Wired once in
`run_testing_mode`, so the pipeline CLI, autonomous runner, and web UI all benefit.

**Reasoning:** A location/role-aware number beats one static figure for the postings that
publish nothing (still common outside pay-transparency states). Two independent sources guard
against either being wrong, and taking the lower on disagreement keeps an uncertain estimate
conservative — the applicant can always negotiate up, but a too-high number can screen them
out. Everything is best-effort and degrades cleanly: no Adzuna keys → Claude-only; no Claude →
Adzuna-only; neither → the stored `desired_salary` (never worse than before 039). Adzuna reuses
the exact `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` the discovery source already reads, so no new
onboarding. The cache holds only role/location→number pairs (no PII) but lives under
git-ignored `profile/` anyway (Agent Guideline #12). Verified offline
(`tests/test_salary.py`, stubbed Claude + Adzuna, 9 cases): reconcile policy, cache
reuse/TTL-recompute/no-source-None, band-validation invalidate-vs-keep, and resolver
precedence; full suite 76/76 green.
See [[038-salary-expectation-advertised-band]], [[037-discovery-snapshot-cache]], [[026-discover-qualification-driven]].

## 040 — Autofill determinism hardening (corpus pin, write-time gates, schema-constrained decisions, no mid-DOM Claude)

**Date:** 2026-07-09 · **Status:** Accepted

**Context:** The user asked to make autofill "as deterministic as possible." The answering
layer was already rule-first with learn-once Claude fallbacks (decisions 018/033/036), but four
non-determinism gaps remained: (1) nothing pinned the keyword resolver — its rules are order-
and substring-sensitive, so an edit could silently flip an answer on a form we already fill
correctly; (2) learned mappings were persisted **unvalidated** — the polluted-answer-bank
incident (a wrong Claude `maps_to` banked, then overriding the corrected rules) was only
repairable after the fact via `scripts/prune_answer_bank.py`; (3) the three fill-time decision
calls parsed free-text replies (`.strip().lower()` + regex), leaving room for reasoning
preambles and mis-reads; (4) `_fill_combobox` called Claude (a 5–60s subprocess) **while the
react-select menu was open** — a staleness race (menus re-render, indexes shift) and the main
timing-dependent behavior in the driver.

**Options considered:** full package vs. safety-net-only (corpus + write gate) vs. also
restructuring to a batched two-pass fill (scan → one Claude call → deterministic fill). User
chose the full package minus two-pass; two-pass batching stays a candidate follow-up (it can't
cover async typeaheads anyway, so the closed-menu restructure was needed regardless).

**Decision:**
1. **Regression corpus** — `fixtures/resolver_corpus.yaml` (65 cases: real labels from the
   SpaceX/Stripe/Robinhood/Instacart/GitLab/Discord/Ramp/cin7 live sweeps, incl. per-country
   work-auth overrides, pay-band midpoint, and 6 must-stay-null enumerated questions) +
   `tests/test_resolver_corpus.py` (synthetic Jordan Avery profile, zero PII) assert the exact
   `resolve()` answer, and `option_hints()` where pinned. A flipped case = a wrong change, or
   a deliberate corpus update in the same commit.
2. **Write-time gates** — `answer_bank.valid_mapping(question, key)` (known key; not
   demographic/company-specific/enumerated; non-garbage question) is enforced in
   `apply_profile.remember_answers` before any `maps_to` is banked (invalid mapping dropped,
   answer text kept); `capture_questions` refuses garbage-length questions; the prune script
   now reuses the same gate for old data. `AnswerResolver.learn_option` never learns aliases
   for generic booleans (yes/no/true/false) — aliases are keyed by value alone, so a "yes" →
   descriptive-option alias learned on one question would leak into every future Yes/No dropdown.
3. **Schema-constrained decisions** — `classify_question` replies `{"type": <enum of
   CLASSIFIABLE_TYPES + none>}`, `match_banked_question` `{"match": <int>}`,
   `pick_dropdown_option` `{"choice": <int>}` via the CLI's `--json-schema` (the decision-034
   mechanism); the token-overlap guard on dropdown picks stays.
4. **No mid-DOM Claude** — `_fill_combobox` reads the options, **closes the menu**, decides,
   then `_commit_option_text` reopens (retyping the search query if any) and clicks by exact
   text. Every combobox fill now records HOW it matched — `FilledField.source =
   option:literal|learned|hint|claude|substring` — the per-field determinism audit trail.

**Reasoning:** Determinism per effort. The corpus makes regressions loud instead of silent (the
enforcement mechanism for everything else); write-time gates make bank pollution impossible
instead of repairable; schema enforcement moves output validation from our regexes into the CLI;
the closed-menu commit removes the one place where model latency could interact with live DOM
state. Repeat encounters were already deterministic via learning — these changes make the
learning itself safe and the first encounter auditable. **Verified offline, zero tokens:** new
`tests/test_determinism_gates.py` (10), `tests/test_resolver_corpus.py` (3, 65 cases),
`tests/test_combobox_fill.py` (4, driving `_fill_combobox` against a new react-select-shaped
fixture `fixtures/apply_forms/combobox.html` — asserts the menu is CLOSED at decide time, exact
recommit, boolean-alias refusal, and no typed-text residue); full suite 93/93. *Remaining:* one
consolidated live dry-run to confirm the closed-menu recommit on real Greenhouse/Ashby
react-selects.
See [[033-self-improving-dropdown-resolver]], [[036-semantic-answer-bank-matching]], [[034-claude-cli-cost-latency]], [[018-answer-bank]].

## 041 — Two-pass page fill: batch all fill-time Claude decisions (≤3 calls per page)

**Date:** 2026-07-09 · **Status:** Accepted

**Context:** After decision 040, each *novel* field still cost its own CLI spawn mid-fill
(classify, bank-match, dropdown pick — ~184-token overhead and 5–15s latency each), and a slow
decision sat between DOM interactions. The user asked to proceed with the deferred two-pass
batching.

**Decision:** `_fill_page` runs each form page twice around ONE batched decision step.
Round 1 is the existing deterministic loop, but with `AnswerResolver.pending` set it DEFERS
unresolved decisions (novel questions with their control kind/options; static-list dropdown
picks with their read options) instead of spawning Claude per field. `_resolve_pending` then
makes at most 3 batched, schema-constrained calls — `classify_questions` (enum array),
`match_banked_questions` (bank sent once, index array), `pick_dropdown_options` (index array +
the per-item token guard) — and injects the results (in-memory bank entries;
`decided_options[label]`). Round 2 is the SAME deterministic loop: injected answers resolve
via the normal bank path, batch-picked options commit by exact text (tier `option:claude`),
and anything unadjudicated is captured for the user — `semantic_done`/`picks_done` guarantee
no per-field fallback calls. Typeahead searches stay inline (their options only exist as you
type); generation-off remains a single pass, byte-identical to before. Batch failure degrades
to plain captures.

**Reasoning:** Claude cost becomes per PAGE, not per field, and no model call ever runs
between dependent DOM interactions — the fill sequence itself is fully deterministic given the
decided answers. **Verified:** `tests/test_two_pass_fill.py` against a new fixture
(`fixtures/apply_forms/two_pass.html`): classify + bank-match + pick all fill in EXACTLY 3
stubbed calls; generation-off = zero calls; failed batch = captures. Full suite 99/99.

**Live dry-run (consolidated, AppLovin Greenhouse):** 17→16 filled, 0 errors, submit probe
found, all 12 react-selects committed `option:literal` (deterministic). The audit trail
exposed a REAL bug: with `desired_salary` unset, the salary question fell to the drafting path
and Claude **fabricated a figure** ("80000 USD"; a prior run had likewise banked "85000").
Fixed three layers deep: numeric-fact questions (salary/GPA/test scores) are never
`is_open_ended` (never drafted, even as textareas); the salary rule falls THROUGH to the bank
on no-data instead of short-circuiting `resolve()`; `prune_answer_bank` drops previously
drafted numeric-fact answers (ran it: the banked "85000" is gone). Re-ran the same dry-run:
salary now cleanly captured ("needs attention"), 0 AI-drafted, all other fields identical —
a deterministic repeat. *User action:* set **desired salary** in the Profile tab (or let the
decision-039 market estimate cover pipeline runs).
See [[040-autofill-determinism-hardening]], [[034-claude-cli-cost-latency]], [[039-dynamic-salary-fallback]].

## 042 — Tailoring token diet (delta output) + measured one-page guarantee

**Date:** 2026-07-09 · **Status:** Accepted

**Context:** The user asked to tighten résumé generation: optimize token usage and make
résumés always one page. Measured: the tailor call sent the résumé JSON with `indent=2` and
null/empty fields (~12% waste; the real résumé is ~17.6k chars), the full JD including
trailing EEO/benefits boilerplate (2–10k chars), and a 4.8k-char TailoredResume schema — and
the model **echoed the entire TailoredResume back** (education, skills, orgs, dates, certs
verbatim), the largest single spend. One-page was only a count heuristic (`LengthBudget`:
3 entries / 4 bullets) with `auto_page_break=True` — overflow silently spilled to page 2
(a known audit gap).

**Decision:**
1. **Delta output (user-approved):** the Claude backend now returns a `TailorDelta` — entries
   referenced by 0-based index with rewritten bullets + `tailor_note`, reordered skills,
   summary, notes — and `_delta_to_tailored` reconstructs the full `TailoredResume` in Python.
   Orgs/roles/dates/locations/project tech/education/certifications are copied VERBATIM from
   the base résumé: never mangled (structural drift-proofing) and never paid for as output
   tokens. Bad indices ignored, duplicates deduped, empty bullets fall back to the entry's
   base bullets, summary gated on the base having one. Schema shrank 4.8k→1.5k chars.
   `TailoredResume` stays the external shape — web/render/drift-check untouched.
2. **Input diet:** résumé JSON compact (`exclude_none/exclude_defaults`, no indent) in the
   tailor prompt AND `generate_answer`; new `job_description.trim_for_prompt` strips trailing
   legal/EEO boilerplate (markers searched only in the last 40% so requirements are never cut)
   and caps at 8k chars on a paragraph boundary. Stored JD untouched (pay-band parsing and the
   fit judge read the full body).
3. **Measured one-page guarantee:** `pdf.page_count` renders and counts; `pdf.fit_to_pages`
   loops render→measure→trim until the PDF actually fits — one bullet at a time from the last
   (least-relevant) entry of activities→projects→experience down to a 2-bullet floor, then
   whole trailing entries (≥1 experience always kept) — zero tokens, deterministic, with a
   user-facing note naming exactly what was dropped (UI Principle #5). Wired at the end of
   `tailor_resume`, so web preview, CLI, pipeline, and both backends all emit guaranteed-fit
   content.

**Reasoning:** Output tokens were the dominant cost and structural echo carried zero
information — reconstruction makes it free and safer at once. Page fit must be measured, not
estimated: only the renderer knows where lines wrap. **Verified:** 8 new tests
(`tests/test_resume_fit.py`: fit no-op/overflow/end-to-end, delta fidelity/bad-index/summary
gate, stubbed backend parse, JD trim safety + cap), full suite 107/107 — plus one LIVE tailor
(real résumé × 10.3k-char JD, fast tier): valid delta first try, sensible tailoring notes,
PDF measured at exactly 1 page. *Open (pre-existing):* unicode TTF embedding (latin-1
`?`-mangling of non-Western names).
See [[040-autofill-determinism-hardening]], [[034-claude-cli-cost-latency]], [[013-resume-length-budget]], [[002-structured-resume]].

## 043 — Adoptions from the ai-job-search survey: ATS PDF verify, per-application archive, dimension rubric, outcome calibration

**Date:** 2026-07-09 · **Status:** Accepted

**Context:** The user asked for a review of [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search)
(17.7k-star Claude Code job-application framework). It has no Apply stage (human submits
manually) and its LaTeX/Danish-portal stack doesn't fit us, but four of its quality
mechanisms do. Options considered per idea are in the survey summary (session 2026-07-09);
the reviewer-agent tailoring pass was deliberately NOT adopted (doubles tailor cost against
decision 034; its cheap subset — keyword coverage — comes free with the ATS check), nor were
LaTeX/moderncv, manual submission, or the ToS-flagged LinkedIn scraper (Guideline #4).

**Decision (four adoptions, all zero-token/deterministic at run time):**
1. **ATS text-layer verification** (`ats_check.py`, new dep `pypdf`): after every PDF
   export, extract the text layer and verify what an ATS parser sees — readable text,
   name/email/phone literal (catches the known latin-1 `?`-mangling), and JD keyword
   coverage split *covered* vs *dropped-by-tailoring* (in the base résumé but cut from the
   PDF; genuine gaps stay the judge's `missing` list). Wired after PDF export in
   `pipeline.run_testing_mode` (notes flow to the Discover tab via status_cb) and `cli.py
   --out *.pdf`.
2. **Per-application archive** (`archive.py`): git-ignored
   `profile/applications/<company>-<role>-<urlhash>/` (same key as `resume_store`) holding
   `posting.md` (JD as fetched), `resume.pdf` (exact bytes), `report.json` (fill outcome).
   Dry-runs overwrite the dir root; a REAL submission freezes a `submitted-<date>/` copy
   never touched again. Best-effort call next to the tracker record in `run_apply`
   (`jd_body` threaded via meta from the pipeline).
3. **Multi-dimension fit rubric** (`matching.py`): the judge returns 0-100 **skills /
   experience / seniority** per posting; the overall `fit_score` is now computed in code —
   `weighted_fit` over `FIT_WEIGHTS` {skills .45, experience .35, seniority .20},
   renormalized over present dimensions — not model-reported. Dimensions ride `Match`,
   the discovery cache (pre-043 snapshots load with `{}`), the pipeline CLI listing, and
   the Discover tab (judged rows + chosen match). ai-job-search's culture/career
   dimensions are deferred until the Configure preference schema exists.
4. **Outcome calibration groundwork** (`tracker.py`): statuses gain the post-application
   lifecycle **interview / offer / rejected / no-response** (absorbing the queued "Track
   lifecycle" statuses; follow-up date still open), a `fit_score` column (stamped from the
   judge at apply time; additive `ALTER TABLE` migration for pre-043 DBs) and
   `calibration_report()` + `python -m applicationbot.tracker calibration` — response rate
   by fit band (75-100 / 60-74 / <60), with a hint to raise `min_fit` when a band has ≥5
   resolved outcomes and zero responses. Track tab shows a Fit column; status dropdowns
   pick the new statuses up automatically from `tracker.STATUSES`.

**Reasoning:** These four give the autonomous pipeline the feedback loops the manual
framework relies on a human for: verify what the ATS will actually parse before submitting,
keep evidence of what was submitted, make fit verdicts auditable, and let real outcomes tune
the threshold. **Verified:** 19 new tests (`test_ats_check`, `test_archive`,
`test_matching_dimensions`, `test_calibration` — incl. pre-043 DB migration and
mangled-name detection), full suite **126/126**; served JS `node --check`-clean; live: CLI
PDF export prints the ATS notes, real `applications.db` migrated in place (12 rows intact),
`/track` serves the new statuses + `fit_score`.
See [[034-claude-cli-cost-latency]], [[024-tracking-store]], [[025-hybrid-matching]], [[035-submit-safety]].

### Update (2026-07-09): min_fit auto-calibration + follow-up date (043 follow-ups)

The two items 043 left open, user-approved: (1) **`tracker.recommended_min_fit(current)`**
turns the dead-band hint into a value — a band with ≥5 resolved outcomes and zero responses
recommends `hi+1`; it only ever raises, never acts on thin/positive data, and never
recommends past the top band (a dead 75-100 band means the strategy is failing, which no
threshold fixes). **`pipeline.effective_min_fit(filters)`** applies it wherever the CONFIG
default is used — pipeline CLI, runner, web test-run — each surfacing a loud note
("min_fit raised 50→75 by outcome calibration (…)"); an explicit `--min-fit` always wins,
and a new **`DiscoveryFilters.calibrate_min_fit`** toggle (default on, editable in the
Discover settings) turns the behaviour off — the user stays in control of their filters.
Any tracker error keeps the configured value (a broken DB must never change matching).
`tracker calibration` also prints the recommendation + whether it's being applied.
(2) **`follow_up_date`** tracker column (ISO date, same additive migration path as
`fit_score`) + a "Follow up" Track-tab column — closes the queued "Track lifecycle" item.
**Verified:** 6 new tests (recommendation floors/never-lowers/positive-band/top-band,
effective wiring + kill switch + error fallback, follow-up roundtrip), suite **132/132**,
served JS clean, live: real DB migrated (follow_up_date in `/track` fields),
`calibrate_min_fit` served to the Discover settings editor.
## 044 — Auto-answer readiness/commitment closers and ITAR/export-control gates

**Date:** 2026-07-09 · **Status:** Accepted

**Context:** Live forms end with commitment closers — "Are you up for it?", "Are you
ready?", "Does this sound like you?" — that matched no keyword rule and no classifiable
type, so they always fell to the needs-attention queue. The user also flagged ITAR gates
(the standard "(i) U.S. citizen or national, (ii) green card holder…" blurb): as a U.S.
citizen he qualifies as an ITAR "U.S. person" and is eligible to apply for a secret
clearance, so these should never block an autonomous run.

**Decision:**
1. **Readiness closers → "Yes"** (`apply.py resolve()`): a keyword rule ("are you up
   for", "up for the challenge", "ready to take on", "sound like you", "are you ready", …)
   answers Yes — applying IS the commitment, same honesty rationale as the existing ADA
   essential-functions rule. Guarded against logistical "ready" phrasings (start,
   relocate, remote/onsite, travel, commute, when), which keep resolving from their
   profile fields or stay captured. Plus a **`role_commitment`** entry in
   `CLASSIFIABLE_TYPES` (answered live as "Yes") so the batched classifier catches
   rephrasings the keywords miss.
2. **ITAR / export-control gates → "Yes" iff `us_citizen` is True** (rule ordered before
   the citizen rule so the multi-status blurb resolves as ITAR): a citizen is a "U.S.
   person", so the gate is met. A non-citizen falls THROUGH — not `return None` — so a
   banked answer still applies (green-card holders/refugees/asylees also qualify, which
   the profile can't derive; the salary rule's skipped-bank lesson, decision 041).
   Matching pitfall fixed en route: "itar" must match as a whole word — as a substring it
   hit "mil-ITAR-y status" and flipped the veteran-status corpus case. Option hints map
   status-style ITAR dropdowns to the citizen/national / "U.S. Person" option. Plus an
   **`itar_us_person`** classifiable type (Yes iff citizen, else None → capture).
3. **Clearance eligibility vs possession:** "eligible/able/willing to obtain a
   clearance" → Yes for a citizen; "do you HAVE an active clearance" stays captured
   (pinned null in the corpus, and "clearance" remains in `_ENUMERATED` so no
   classification/banking path can map it onto a blanket Yes).

**Reasoning:** Both families are gates whose truthful answer is derivable (commitment from
the act of applying; ITAR from citizenship) — leaving them to the needs-attention queue
stalled otherwise-autonomous runs. All deterministic at run time; the two new types cost
nothing extra (they ride the existing batched classify call). **Verified:** 11 new corpus
cases (closers, guards like "When are you ready to start?" → null and "Are you ready to
relocate?" → No, the verbatim ITAR blurb, clearance eligibility vs possession), full suite
**132/132**.
See [[041-two-pass-page-fill]], [[040-autofill-determinism-hardening]], [[018-answer-bank]].

## 045 — Rank projects by technical impressiveness so the résumé leads with the strongest work

**Context:** Which projects survive a tailored résumé's length budget was decided by JD
keyword relevance alone (`text_score` in `catalogue.select_relevant`, the rules engine, and
the Claude tailoring order). Relevance has no notion of *technical impressiveness*, so a
weak keyword-matching project (a low-code Retool dashboard) could crowd out a genuinely deep
one (a Rust/macOS systems overlay). The user wanted the pipeline to focus on more
technically impressive projects.

**Options considered:** (a) manual impressiveness field the user sets; (b) **Claude
auto-scores** each project, cached; (c) treat list order as the ranking. And for how the
score interacts with JD relevance: relevance-first w/ rank as tiebreak, rank-dominates, or
relevance-filters-then-rank-orders.

**Decision:** (b) + relevance-first-with-tiebreak. `impact.py` makes **one Claude pass**
(subscription CLI, `think=False`, schema-constrained — same path as tailoring, decision 034)
scoring every project 1–5 on engineering depth/difficulty only (not job fit, not prose),
written back to a new optional `Project.impact` in the git-ignored resume.yaml (the cache).
The Profile UI orders projects by that score, shows a ★ badge in each card's collapsed
header, carries the score through the save round-trip (hidden field), and adds a "Rank by
impressiveness" button (shared spinner + live elapsed, UI principle #5) that saves current
edits then re-scores. Selection stays **relevance-first**: `impact` is only a secondary sort
key in `select_relevant`'s trim and the rules engine, and the tailoring system prompt is told
to prefer higher-impact projects *among comparably-relevant ones* and drop low-impact
low-relevance ones first when the budget is tight — so an impressive but off-topic project is
never forced onto the résumé.

**Reasoning:** Auto-scoring removes manual bookkeeping and calibrates consistently; caching in
resume.yaml means the cost is paid once per catalogue change, not per tailor. Keeping relevance
primary preserves existing tailoring behaviour (Guideline #7) — the feature only changes the
*tiebreak*, which is exactly where crowding-out happened. **Verified live:** scored the real
7-project résumé — the two deep projects (AgentStatus, ApplicationBot) got 5, the low-code
dashboard got 2; save/reload stays schema-valid; select_relevant and the rules engine surface
the high-impact projects first; full suite **132/132**.
See [[042-tailoring-token-diet]], [[013-catalogue-token-efficiency]], [[034-stripped-claude-cli]].

## 046 — Discovery feedback loop: learn from past judgments to surface higher-fit postings

**Context:** The recurring failure the user hits is "can't find a posting above the fit
threshold to run a dry-run." Grounded in the real data: a run discovered 673 postings → 301
after gates → 91 keyword-matched → **but only 10 got a Claude fit score** (`top_n`), of which
exactly one cleared `min_fit=60`. Two compounding causes: (1) the free keyword pre-filter
ranks by raw skill-term overlap, which floats **verbose senior JDs** to the top — the exact
postings an early-career résumé scores *lowest* on (experience dimension averaged 23) — so the
judge's scarce slots are spent on roles that always score ~20, while any higher-fit early-career
roles sit unjudged at rank 11–91. (2) The only existing "learning" (decision 043
`recommended_min_fit`) **only ever RAISES** `min_fit`, which makes "nothing clears" worse; nothing
steered the *supply* of high-fit postings. The user asked for a loop that learns from past runs
and tweaks itself so each new run surfaces postings that score higher.

**Options considered:** (a) learn-to-rank the pre-filter from accumulated judged history
(steer *which* postings get judged); (b) auto-tune the discovery filters from history + a
"why nothing clears" diagnostic (steer the *pool*, transparent, user-in-loop); (c) both. User
chose **(c)**.

**Decision:** New `fit_learning.py`. **Store:** every judged Match is appended to git-ignored
`profile/fit_history.jsonl` (url, ats/board, title, detected level(s), fit, per-dimension
scores, matched skills, missing) after each live `discover_and_match`; `load()` de-dups by
canonical URL keeping the latest verdict. **Engine:** a `Predictor` estimates a not-yet-judged
posting's fit as the average of two **shrinkage-blended** bucket means — its seniority level(s)
and its board — each pulled toward the global mean by `_SHRINK_K=4` pseudo-counts so a
rarely-seen bucket can't swing the rank on noise; `active` only at ≥ `MIN_HISTORY=5` rows.
`matching.match(..., predictor=)` re-sorts the survivors by predicted fit (curated feeds still
first, keyword score as tiebreak) **before** slicing `top_n`, so the judge sees the postings most
like past winners. It never changes the final best-first ordering (still the judge's fit_score) —
only which postings get judged. Zero extra Claude tokens (prediction is arithmetic over stored
verdicts); a no-op with thin/no history (keeps today's keyword ordering). **Diagnosis:**
`analyze()` reports dimension means + weakest dimension, per-level and per-board fit segments,
recurring missing requirements, and `Recommendation`s: narrow `experience_levels` to the winning
bands (needs a clear ≥2-sample winner/loser split among *detected* levels), lower `min_fit` to
best-achievable **only when nothing cleared** (surfaces the reality, never auto-lowers on partial
success), flag chronically-dead boards, and list recurring missing reqs as résumé gaps. Surfaced
in the pipeline CLI and a Discover-tab panel (`GET /fit-insights`) with one-click apply
(`POST /fit-insights/apply`, whitelisted to `experience_levels`/`min_fit`, re-validated).
**Visible improvement over time:** each live run also appends a one-line summary (best/mean
fit, how many cleared) to `profile/fit_runs.jsonl`; the Discover panel charts it as an inline
SVG sparkline (best + mean fit vs the dashed min_fit bar, per-run hover) under an "▲ improving"
headline, and the CLI prints the best-fit series — so the user watches results climb as the
loop learns, not just a static snapshot.

**Reasoning:** The bottleneck is the pre-filter starving the judge, so the highest-leverage fix
is spending the judge's slots better — an engine that needs no tokens and self-sharpens each run
(a). The diagnosis (b) makes the "why" legible and gives the user auditable, one-click control
rather than a silent model tweak. Shrinkage + a min-history gate keep it from overfitting to a
handful of samples. It complements, not conflicts with, 043: 043 tunes the *bar* from real
interview outcomes; 046 steers the *supply* of postings that reach the bar. Preserves existing
behaviour (Guideline #7): `predictor=None` / inactive ⇒ today's keyword ranking, unchanged final
ordering. **Verified:** 16 new tests (predictor flips the single judged slot from a skill-stuffed
senior posting to a bare new-grad one; diagnosis recommendations fire under the right guards);
diagnosis run on the real judged data correctly names experience (23) as the drag and greenhouse
(25) as the dead board vs ashby (54); the three endpoints driven live (apply merges into
discovery.yaml, non-applyable fields rejected); fixed a test-isolation bug where
`test_discovery_cache` wrote history into the real profile dir; full suite **148/148**.
See [[043-ai-job-search-adoptions]], [[025-qualification-driven-discovery]], [[037-discovery-snapshot-cache]], [[034-stripped-claude-cli]].

### Update (2026-07-09): cache-served dry runs don't train or chart — say so ("Fresh trains, label the rest")

**Context:** The fit trend showed only one point despite the user doing several dry runs. Root
cause: a dry run first checks the discovery snapshot cache (037); on a hit it returns the stored
matches early and never reaches the `append()` (per-posting training) or `record_run()` (one
trend point) calls — those run *only* on a live, judged (`force_fresh`) run. With a 12h cache
TTL, every dry run after the first reused the same 91 matches, so nothing new was judged, learned,
or charted. **Options:** (a) chart a point on every dry run including cache hits — but a cache hit
is byte-identical prior data, so it plots flat duplicates and adds no training signal, misrepresenting
"results improving"; (b) keep chart/training tied to fresh judged runs, but stop the silence — tell
the user when a run was cache-served and that fresh is what adds a point + trains; (c) make the
default dry run always bypass the cache (full judge + board scrape every run). **Decision (b).** The
cache-reuse note in the test panel now states the run "added no point to the fit chart and taught
the search nothing. Re-search fresh to judge live, add a chart point, and train" — the existing
one-click **Re-search fresh** button is the fix (UI Principles #3, #5: don't silently drop the
user's action; the message names what didn't happen and the button that makes it happen). No
data-model change; fresh runs already record correctly. Also (unrelated, same session) the fit
chart now defaults to **Lifetime** with a Show: Lifetime/Last 30/Last 10 window toggle, and
`/fit-insights` returns the full run history instead of the last 30.

## 047 — JSON-LD → CSS → LLM enrichment cascade + career-site discovery source (ApplyPilot survey)

**Context:** Surveyed [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)
(an agentic auto-apply agent — Claude Code + Playwright MCP *drives the browser*) against our
system. Its most portable, principle-aligned win is its **Enrich** stage: a three-tier
extraction cascade (JSON-LD → CSS selectors → AI) that reads a full job description off an
arbitrary posting page. Our discovery is limited to ~5 known ATSs with public JSON APIs; any
company career page outside that set can't be discovered or read. Reading a page's published
`JobPosting` structured data (the same data Google for Jobs indexes) is ToS-clean (Guideline
#4) and needs no LLM for the common case. User approved this as item #1 of the ApplyPilot
adoption queue.

**Options considered:** (a) build it as one more `Source` (a career-site scraper); (b) build
it as a **reusable enrichment module** that a new career-site `Source` consumes *and* that can
backfill a full JD anywhere an ATS-specific resolver comes up empty (curated-list resolution
failures, aggregator non-ATS hits, future sources). Chose **(b)** — the cascade is useful in
more than one place, and coupling it to a single Source would force a rewrite to reuse it.

**Decision:** New `enrich.py` — `fetch_full_jd(url, *, llm=None)` / `enrich_from_html(html,
url=, llm=)` run the cascade and return an `EnrichResult` whose `.tier` names the winning
method (`json-ld` | `css` | `llm` | `""`). **Tier 1 (JSON-LD):** regex-find every
`<script type="application/ld+json">` block, recurse through nested objects / `@graph` arrays,
keep `@type` == `JobPosting` (string or list), and normalize description (HTML-unescaped →
plaintext), apply URL (`directApply` → `applicationContact.url` → `url`), title, company,
location, `baseSalary`, `datePosted`, and `jobLocationType`==TELECOMMUTE → remote. **Tier 2
(CSS/DOM):** a stdlib `HTMLParser` (`_DescExtractor`) that captures the text of the element
whose id/class names it a description (`job-description`/`description`/… + `<article>`/`<main>`
fallback), longest block wins, `<script>`/`<style>` data skipped, a void-tag-aware stack so
nested markup closes capture at the right element; apply URL from the first `apply`-ish `<a>`.
**Tier 3 (LLM):** optional — only runs if a caller passes an `llm` callable; the default
`claude_llm_extractor` shells to the stripped Claude Code CLI (decision 034) with a
`{description, apply_url}` json-schema over the cleaned, 30k-capped page text. A description
under 50 chars is treated as "not found" so a stub page falls through. New
`discovery.CareerSiteSource(urls, *, llm=None)` fetches each configured URL once, emits one
`Posting` per JobPosting found (ATS auto-detected from the apply URL via `detect_ats_from_url`,
so a JSON-LD link to a Greenhouse/Workday posting routes into the right Apply adapter), and
tallies which tier resolved each page in `.stats` for a future "% saved" log line. Wired into
config: `DiscoveryFilters.career_sites: list[str]` → `build_sources` appends the source when
non-empty. Refactored `discovery.fetch_json` onto a shared `fetch_text` (same politeness/retry)
so the cascade fetches HTML through the existing per-host pacing + backoff. LLM tier is **off
by default** — the cascade is free and offline unless a caller opts in.

**Reasoning:** Cost and safety. On real pages the vast majority resolve at tier 1/2 (ApplyPilot
reports ~95% saved), so the LLM is rarely touched and never touched unless requested — matching
our token-discipline decisions (034/041/042). A reusable module (b) means the same cascade can
later rescue the curated-list/aggregator postings that today flow through with a degraded
title-only body. No new dependency: JSON-LD via regex + `json`, the CSS tier via stdlib
`HTMLParser` (no BeautifulSoup/soupsieve). Preserves existing behaviour (Guideline #7): empty
`career_sites` builds no source; `fetch_json` is byte-for-byte equivalent through the refactor;
a JS-rendered SPA (no server-side JSON-LD) returns an empty result rather than garbage.

**Verified:** 8 new offline tests (`tests/test_enrich.py`: each tier in isolation, `@graph` +
`@type`-list handling, the 50-char gate, script-text skipped + longest-block, LLM tier only on
fall-through *and* only when opted in, CareerSiteSource ATS-detection + per-URL failure skip).
Full suite **156/156**. **Live-verified:** the cascade on a real Lever hosted posting page →
tier `json-ld`, 5,099-char JD, correct title/company; a Stripe SPA URL (JS-rendered, no SSR
JSON-LD) correctly degrades to empty; `career_sites` round-trips through discovery.yaml and
builds the source. *Remaining:* surface the `.stats` "% saved" line in the CLI/Discover tab;
optionally wire the cascade as the fallback inside `discovery._resolve_jd`. First of the
ApplyPilot adoptions (#2 cover letters, #3 `doctor`, #4 `--continuous`, #7 CapSolver, then the
Workday hybrid) — see NEXT_STEPS.
See [[043-ai-job-search-adoptions]], [[030-more-discovery-sources]], [[025-qualification-driven-discovery]], [[034-stripped-claude-cli]], [[032-workable-aggregator-bridge]].

## 048 — `doctor` readiness command + runner `--continuous` polling (ApplyPilot survey)

**Context:** Two more ApplyPilot adoptions (survey in decision 047). (1) A fresh clone has no
guided way to tell whether it's actually ready to run — Claude signed in? Chromium installed?
profile files present? a discovery source configured? — so the first failure surfaces deep in a
run instead of up front. ApplyPilot ships `applypilot doctor` for exactly this. (2) The runner is
one-shot: to keep applying as new postings appear the user must re-run it by hand. ApplyPilot's
`--continuous` polls indefinitely. User approved both (#3, #4) and asked to do them before cover
letters (#2), which reuses existing machinery and can wait.

**Decision:** (1) **`doctor.py`** — `python -m applicationbot.doctor` runs six read-only checks
(`run_checks`) and prints each with ✓/✗/⚠ and, on failure, a one-line **actionable fix** (UI
Principles #1/#3): Claude Code CLI signed in · Playwright Chromium installed (imports the package,
then verifies `chromium.executable_path` exists) · résumé loads (+entry/skill counts) · applicant
profile loads · discovery has ≥1 source (boards / career_sites / Adzuna / early-career) · submit
safety state (armed/dry-run/kill, info-only). Exit 0 when every **required** check passes, 1
otherwise (a missing applicant profile is an optional ⚠, not a failure). It never creates or edits
files — diagnosis only; the external-tool probes are isolated in `_check_*` helpers so tests
monkeypatch them. (2) **Runner `--continuous [--interval MIN]`** (default 30) — the per-cycle
discover→judge→apply work moved into a `run_cycle()` closure returning `ok|empty|stop`; a new
module-level `continuous_loop(run_cycle, gate, *, interval_s, _sleep)` repeats it, waiting between
cycles via the existing kill-file-abortable `_wait_for_reset`, and stops on the KILL file, Ctrl-C,
or a fatal `stop` (Claude sign-in — waiting won't fix it). `continuous_loop` takes `run_cycle`/
`_sleep` injected (same pattern as `run_queue(apply_one)`) so it's testable with no network,
browser, or real waiting. Cycles reuse the discovery cache by default (cheap) and re-search every
cycle only with `--fresh` — composes with existing cache semantics (decision 037); `skip_seen`
keeps already-applied roles out each cycle. Dry-run and the safety gate are unchanged (Guideline
#3): continuous never lowers the submit bar.

**Reasoning:** `doctor` makes readiness legible and every gap one step from fixed, which is the
onboarding foundation the audit flagged missing — without pre-creating anything (that's the future
wizard). `--continuous` is the thin loop the audit/roadmap already wanted, built on the runner's
existing quota/kill/failure-isolation rather than a parallel path; forcing fresh only on `--fresh`
keeps Claude cost bounded (a naive re-judge-every-cycle would burn tokens). Both preserve existing
behaviour (Guideline #7): single-run runner output is byte-identical (the cycle body just moved
into `run_cycle`), and `doctor` is additive. **Verified:** 8 new offline tests
(`tests/test_doctor.py`: each check pass/fail + required-vs-optional + exit code; `continuous_loop`
runs until the kill file, stops immediately on fatal, never waits real time); full suite
**167/167**. Live: `python -m applicationbot.doctor` prints a green 6/6 readout (exit 0) on the real
repo; `runner --help` shows the wired `--continuous`/`--interval`. Third and fourth ApplyPilot
adoptions — remaining: #2 cover letters, #7 CapSolver (DECISIONS entry first, Guideline #4), then
the Workday hybrid (#5).
See [[047-jsonld-enrichment-cascade]], [[035-submit-stage]], [[037-discovery-snapshot-cache]].


## 051 — Park & resume blocked applications, M1+M2 (AutoApply-AI survey adoption #1)

**Context:** Surveyed [Rayyan9477/AutoApply-AI](https://github.com/Rayyan9477/AutoApply-AI-Agentic-Browser-Automation-for-Job-Search)
(full-stack FastAPI+React+Redis+Postgres+Prometheus platform on `browser-use`+Playwright).
Its live-submit path is still "active development" — we are *ahead* on Apply — so the value is
on the orchestration/UX side. Four ideas surfaced; the user approved #1/#3/#4 and rejected #2:
- **#1 Park & resume** — their `intervention.py`: when the browser hits a CAPTCHA/login/2FA it
  can't clear, the worker publishes `needs_intervention {application_id, kind, prompt}`, blocks
  on a per-app Redis queue (`BLPOP`, 300s), and the UI resolves it (`RPUSH`). This is the fix for
  three of our open gaps at once — **blocked-work routing**, **durable run state** (a restart
  orphans a mid-fill browser), and **Workday email-verification**. Our runner previously treated a
  block as a dead-end outcome (decision 016's exception-queue model): recorded and never revisited.
- **#3 Deterministic multi-factor ATS pre-score** (their `ats/scorer.py`: skills `0.7·req+0.3·pref`
  + experience + education-rank + keyword, weighted .4/.3/.2/.1) — queued to order the free
  pre-filter cheaply; Claude stays the final judge.
- **#4 Discovery→apply funnel analytics** — queued for the Track tab off `calibration_report()`.
- **#2 Exa AI semantic discovery** — **rejected**: a paid API, and results overlap our existing
  `enrich.py` JSON-LD→CSS→AI cascade (decision 047).

Explicitly **not** adopted: their FastAPI/React/Redis/Postgres/Prometheus stack (architectural
heft that fights simplicity-first and moves nothing toward the pipeline goal) and LiteLLM/Portkey
multi-provider fallback (conflicts with Claude-only, decision 004; our rate-limit pause/resume,
decision 035, already covers resilience).

**Decision (M1 — durable state + classification):** Build park & resume *without* Redis, since our
fill is deterministic (decision 040) — a resolved application resumes by simply re-driving the same
form on the same posting URL, now getting past the field that stalled it. No browser-state
serialization, no worker rendezvous, no new dependency.
- **`parking.py`** — pure `classify(report: ApplyReport) → Optional[ParkReason]`. `ParkReason` =
  `kind` (needs_answer / form_rejected / login / captcha / site_error) + human `summary` +
  `resolve` UI deep-link target ("profile-answers" / "credentials" / "") + `resumable` bool +
  `detail`. Grounded in strings the fill already produces: armed pre-submit-gate blockers
  ("unresolved required field(s): …") and the dry-run required scan ("… — REQUIRED, not filled")
  both map to **needs_answer** (deduped via `required_missing`); a login/CAPTCHA wall gates the
  whole form so it wins over individual fields; "form rejected the submit" → review answers; a
  no-submit-button / crashed-click is **site_error**, parked as a record but `resumable=False`.
  Returns None for a clean dry-run (nothing to act on).
- **`tracker.py`** — new `blocked` status + `blocked_kind`/`blocked_detail` columns (additive
  `ALTER TABLE` migration, same pattern as decision 043's `fit_score`) + `parked_applications()`
  returning only still-open resolvable rows.
- **`apply._record_run`** — an armed-blocked (or required-unanswered) fill is recorded as a
  `blocked` row carrying the reason, not a silent `dry-run`; a later resolved re-run clears
  `blocked_kind` and upserts to `applied`, so it drops out of the parked list. Never clobbers a
  user-set outcome status.

**Reasoning:** Turning a block into durable, classified, resumable state is the foundation the
autonomous runner needs — a blocked application becomes a one-click fix instead of a lost run, and
the tracker (already the system of record) is the natural home, so no new store or service.
Deterministic re-drive beats Redis worker-parking for our design: it needs no long-lived blocked
worker, survives a server restart for free, and reuses the exact fill path (Guideline #2 — simpler
than the surveyed approach). Preserves existing behaviour (Guideline #7): a clean dry-run still
records a `dry-run` row; only genuinely-blocked runs change label, and armed-blocked runs
(previously mislabelled `dry-run`) are now correctly `blocked`. **Verified:** 11 new offline tests
(`tests/test_parking.py`: classifier per kind + precedence, dedup, pre-existing-DB migration,
parked reader open-only, `blocked` status) + full suite **178/178**; drove `_record_run` end-to-end
against a temp DB — a required-field block parks as `blocked/needs_answer` naming the fields, then a
resolved submit upserts to `applied` and clears the park.

**Decision (M2 — Resolve cards, runner surfacing, one-click resume):** Surface parked applications
where the user can act, and make resume one click.
- **Resolve cards** — new `GET /parked` returns `parked_applications()` enriched with
  `parking.describe(kind, detail)` (headline · action verb · deep-link target · resumable). A
  "Applications waiting on you" panel at the top of the Discover tab renders one card each; a
  `needs_answer`/`form_rejected` card's button switches to the Profile tab and scrolls to the
  "Needs your answer" list (UI Principle #2 — one click to the fix), a `login`/`captcha` card shows
  the specific instruction. The panel hides itself when nothing is parked.
- **Runner surfacing** — `runner._report_parked()` prints, after each cycle, every parked
  application by name + what's blocking it (best-effort; a DB hiccup never breaks the run).
- **One-click resume** — a "Re-apply (dry-run)" button on each resumable card POSTs `/parked/reapply`,
  which runs `_reapply_worker` in the background: it re-drives the DETERMINISTIC fill on the same
  posting URL with the stored tailored PDF and a fresh resolver (which now picks up the answer the
  user just saved), reusing the existing test-run progress panel + Finish button. It **always
  dry-runs** (gate omitted) — the armed runner stays the only submit path (Guideline #3); a clean
  re-fill just confirms the block is cleared and the tracker upsert drops it out of the parked list.
  Guards (missing row / no source URL / vanished PDF / a run already active) return an actionable
  error before any browser launch.

**Reasoning (M2):** The card + deep-link is the payoff of M1's durable state — a blocked application
is now a one-step fix instead of a lost run, satisfying blocked-work routing. Reusing the test-run
worker/panel (rather than a second progress UI) keeps the surface consistent (UI Principle #5) and
the code small (Guideline #2). Keeping resume dry-run honours the safety switch: no UI button ever
fires an irreversible submit; the user arms the runner for that. **Verified:** 10 more offline tests
(`describe()` per kind, runner `_report_parked` names/silence, all four re-apply guard paths) — full
suite **209/209**; drove the live HTTP server against a temp DB (page renders the parked panel + the
re-apply JS, `GET /parked` returns the right card excluding a resolved row, `/parked/reapply` busy-
guard fires). **Remaining:** an armed one-click resume (behind an explicit per-click arm), and a
credentials UI for the `login` deep-link target (today it shows the instruction). See
[[035-submit-stage]], [[040-autofill-determinism]], [[024-tracking-store]].

## 049 — CAPTCHA auto-solving on the armed submit path (CapSolver) — user-directed, fenced

**⚠ Compliance tension (logged before building, per the commitment + Guideline #4).** Solving a
CAPTCHA to submit a form circumvents a site's anti-bot control and may breach that site's terms
of service. In my survey recommendation I put this in the **reject** column on exactly those
grounds. The user overrode that for their **own** job applications (personal use, toward the
product's fully-autonomous end goal) and directed us to build it. This entry records the
disagreement and the decision: we build it, but **fenced** so it cannot run silently or broadly,
and it is the user's call for their own tool. A second finding from reading ApplyPilot's source:
its README advertises CapSolver but the **code has none** — it detects a CAPTCHA and fails
gracefully — so there was nothing to port; this is a from-scratch build.

**Options considered:** (a) don't build it (my survey rec) — rejected by the user; (b) build it
always-on — rejected (unfenced circumvention, irreversible); (c) build it **fenced**: off by
default, per-site opt-in, armed-only, key from the environment, every attempt logged. Chose (c).

**Decision:** New `captcha.py`. `build_submit_hook(config, url)` returns `hook(frame) ->
(handled, detail)`; `apply._attempt_submit` calls it **after** `gate.may_submit()` passes —
which is reached only on the armed path, so dry-run never solves (Guideline #3). The five gates:
(1) **off by default** — `captcha.enabled` in profile/safety.yaml must be true; (2) **per-site
opt-in** — the URL host must suffix-match a domain in `captcha.sites` (empty ⇒ nothing);
(3) **armed-only** — enforced by the single call site; (4) **key from env** `CAPSOLVER_API_KEY`,
never YAML (Guideline #12); (5) **every attempt logged** (site, type, outcome) via the injected
`log`. If any gate is unmet the hook returns `(False, actionable-reason)` and the caller records
a **blocked** outcome with the fix — it never falls back to submitting a protected form.
Detection (`_DETECT_JS`) reads a reCAPTCHA-v2 / hCaptcha / Turnstile sitekey off the widget
container or its iframe `src`; the solve goes through CapSolver's `createTask`/`getTaskResult`
REST API (urllib — no new dependency, `_post`/`_sleep` injectable); `inject_token` writes the
response token into the form's `g-/h-captcha-response` / `cf-turnstile-response` field and fires
input/change. Config lives in safety.yaml (co-located with arming — it is a submission-safety
setting):

    captcha:
      enabled: false
      sites: []            # e.g. [greenhouse.io, ashbyhq.com]

`doctor`'s safety line now reports the CAPTCHA state (off / on + allowlist + whether the key is
set). `_attempt_submit` gained an optional `solve_captcha=None` param — existing callers/tests
are byte-identical when it's None (Guideline #7).

**Reasoning:** Given the user's directive, the responsible build is one that can only act with
deliberate, per-site, keyed, armed opt-in and leaves an audit trail — the same
safety-switch philosophy as decision 035 (real submission is gated, logged, killable), extended
to the CAPTCHA that gates it. Defaulting everything off means a fresh clone, a dry-run, or an
un-allowlisted site behaves exactly as before: a CAPTCHA is a recorded blocker, not a silent
bypass. **Verified:** 12 offline tests (`tests/test_captcha.py`, zero CapSolver calls): each gate
blocks with its reason, all-gates-pass solves + injects the token, the CapSolver client polls
processing→ready and raises on an API error, site-allowlist suffix match rejects
`evil-greenhouse.io`. Full suite **190/190**; `doctor` prints "CAPTCHA auto-solve off" by default;
existing submit tests pass unchanged. *Remaining:* one live armed dry-run against a real
CAPTCHA-gated form once the user sets a key + allowlists a site (no repo test can exercise the
real CapSolver path without spending). Fifth ApplyPilot adoption; remaining: #2 cover letters,
then the Workday hybrid (#5).
See [[035-submit-stage]], [[047-jsonld-enrichment-cascade]], [[048-doctor-continuous]].

## 050 — Workday hybrid: agentic→deterministic (Option C), keyring credentials, M1 begun

**Context:** Account-gated portals are the largest open blocker — Workday alone is ≈32% of US
enterprise postings and today `pipeline._is_fillable` drops it entirely (no adapter). It was
surfaced by the ApplyPilot survey (decisions 047–049): ApplyPilot fills Workday by having
Claude Code + a Playwright-MCP server **drive the browser agentically** — robust to any layout
but token-heavy, non-deterministic, and (critically) its dry-run safety lives **in the prompt**,
which an agentic model can ignore. We want Workday coverage without giving up the determinism
and Python-side safety the rest of Apply relies on (decisions 034/040/041/035).

**Options considered:** (A) pure deterministic adapter on Workday's `data-automation-id`
selectors — cheapest/most-deterministic but brittle on per-tenant custom questions; (B) pure
agentic (ApplyPilot) — robust but expensive, non-deterministic, prompt-only safety; (C) **hybrid**
— deterministic adapter first, an agentic worker only for pages the adapter doesn't recognize,
distilling each agentic fill into a replayable recipe so agentic use trends to 0, with the final
submit always behind the Python `SafetyGate`. **User approved (C).** Key enabling fact: Workday's
`data-automation-id` attributes are **stable across every tenant** (shared widget system), so the
standard wizard is fillable by exact id — no label matching, no Claude.

**Settled specifics (2026-07-09, user):** (1) account creation via a **dedicated bot-owned email**
(IMAP/Gmail read for verification links), but **every tenant password persisted** so the user can
log in manually later; (2) recipes = a **shared committed** library (selectors + question labels
only, no PII) so every clone inherits learned pages; (3) **M1 = deterministic login + standard
fields, dry-run only** (agentic fallback = M2, armed submit = M3); (4) custom questions reuse the
existing `AnswerResolver`/bank/Claude-draft path; (5) page identity = hash of the page's
`data-automation-id` set; (6) new `workday.py` behind the apply interface, and stop `_is_fillable`
dropping Workday. Build order (bricks): 1 credential store · 2 adapter field-fill core · 3 wizard
navigation · 4 account-create/sign-in + email verification · 5 wire-in.

**Decision (this session — bricks 1 & 2):** (1) **`credentials.py`** — per-tenant credential store:
passwords in the **OS keychain via `keyring`** (new dep, pre-approved), never plaintext YAML
(Guideline #12); a git-ignored non-secret index `profile/workday_accounts.json` records tenant→email
so the store is listable (keyring can't enumerate) and the user can see/retrieve every account
(`python -m applicationbot.credentials list|get|delete`). Tenant key = the Workday host. `backend`
injected so tests use an in-memory fake. (2) **`workday.py`** — `fill_standard_fields(frame, resume,
profile, report)` maps Workday's stable automation ids (`legalNameSection_firstName/_lastName`,
`addressSection_city`, `email`, `phone-number`) to profile-first-then-résumé values, empties dropped
(never filled blank), each recorded on `report.filled` as `source="workday"`. Handles both wrapped
inputs (id on a container) and direct inputs (id on the `<input>`). DRY-RUN only — nothing here
submits. **Brick 3 (same session):** `fill_wizard` walks the multi-page wizard via the VISIBLE
`pageFooterNextButton`, filling text + custom dropdowns per page and stopping at Review (no Next) —
it NEVER clicks Submit; page identity is the md5 of the visible `data-automation-id` set (advance
detection now, recipe key in M2). `fill_dropdowns`/`_fill_dropdown` handle Workday's custom
button/listbox dropdowns deterministically (open → read visible options → match in code
exact-then-substring → click by index): country/state (abbrev→full-name via a `_US_STATES` map) and
the EEO dropdowns. All fills are `:visible`-scoped — never interacting with hidden fields from other
wizard pages (also removed a 3s-per-hidden-field actionability timeout).

**Reasoning:** Inverting ApplyPilot (deterministic-first, agentic-only-for-the-unknown) keeps the
Workday path cheap, reproducible, and Python-safety-gated while still promising "any page" coverage
once the agentic fallback (M2) lands. Building against local Workday-shaped HTML fixtures (real
automation ids) — the same method that validated every other ATS — means M1 progresses with **zero
tokens and zero contact with a real Workday** (Guideline #3), deferring the one unavoidable live
step (a real tenant needs an account) to the account-creation brick. Preserves existing behaviour
(Guideline #7): `workday.py` is new and unwired; `_is_fillable` still drops Workday until brick 5,
so no current flow changes. **Verified:** 12 offline tests (`tests/test_credentials.py` ×6 with a
fake keychain + temp index; `tests/test_workday.py` ×6 — value/dropdown mapping,
option-matching, a headless fill of `workday_myinfo.html`, and a full headless walk of the 3-page
`workday_wizard.html` proving text + custom dropdowns fill across pages and Submit is NEVER clicked).
Full suite **221/221**; the credential store round-trips through the **real macOS keychain** live;
`profile/workday_accounts.json` confirmed git-ignored. *Remaining M1:* brick 4 (account-create/
sign-in + IMAP verification), brick 5 (apply dispatch + tracker row + stop `_is_fillable` dropping
Workday). Then M2 (agentic fallback + recipe distillation), M3 (armed submit).
See [[049-captcha-autosolve]], [[035-submit-stage]], [[040-autofill-determinism]], [[017-native-ats-autofill]], [[047-jsonld-enrichment-cascade]].


## 052 — Deterministic multi-factor pre-score orders the judge queue (AutoApply-AI survey #3)

**Context:** The free keyword pre-filter (`relevance.qualification_score`) ranks postings by a raw
count of how many of the candidate's skills a JD mentions, and `matching.match` sends only the top
`top_n` to the Claude judge. Decision 046 identified the failure this count causes: a verbose senior
JD mentions many skills, so it floats to the top of the queue and consumes the judge's scarce slots,
while early-career-fit roles sit unjudged below the cut. Decision 046's `Predictor` fixes this once
there's outcome history; nothing improved the **cold-start** ordering. AutoApply-AI's `ats/scorer.py`
is a deterministic multi-factor resume-vs-JD score (skills/experience/education/keyword, weighted
.40/.30/.20/.10) - user approved adopting it (survey #3) to order the queue.

**Decision:** New `ats_score.py` - `ats_prescore(resume, title, jd_text, matched_count=...)` returns a
zero-token 0-100 pre-score:
- **skills (.40)** - the matched-skill count, saturated at 6 (= full marks). The surveyed
  required-vs-preferred split isn't extractable from raw JD text, so a saturated overlap count
  replaces it honestly.
- **experience (.30)** - candidate career-span years (earliest experience start -> latest end, 'Present'
  = today) / the JD's floor experience bar (smallest "N years", ranges read as their low end, absurd
  >40 ignored). This is the factor that sinks an over-bar senior role for an early-career resume.
- **education (.20)** - candidate max degree rank (HS=1...PhD=5) / the JD's floor degree.
- **keyword (.10)** - overlap of the posting TITLE's distinctive tokens with the resume (a distinct
  signal from skill mentions: catches a 'Sales Engineer' title against a software resume).

A factor whose requirement the JD doesn't state returns None and is renormalized out of the weighted
average (missing != zero - same principle as `matching.weighted_fit`). `matching.keyword_rank`
computes it once (reusing the keyword pass's matched count, no re-scan), stores `Match.ats_score`, and
sorts survivors by `(curated, ats_score, keyword_score)`; the predictor-active path uses `ats_score`
as the tiebreak below predicted fit.

**Reasoning:** This spends the judge's fixed budget on better candidates from the very first run,
before any outcome history exists, directly fixing 046's crowd-out - cheaply and deterministically
(Guideline #2, no tokens, no new dependency). **Claude remains the only fit verdict** (`fit_score`):
`ats_score` changes *which* postings are judged, never the final best-first ordering (still by
`fit_score`) nor the `min_skills` gate, so observable output is unchanged except that the judged set
is better-chosen (Guideline #7). Adapted, not ported: dropped the required/preferred skill split we
can't extract. Cache-safe: `ats_score` defaults to 0 on pre-052 snapshots. **Verified:** 9 offline
tests (`tests/test_ats_score.py`: each factor extractor, renormalization of absent requirements, the
experience factor sinking an under-qualified candidate, and the integration assertion that
`keyword_rank` now orders a fitting new-grad role ABOVE a higher-keyword senior role; cache round-trip
+ pre-052 load). Full suite **221/221**; live drive on the sample 7-yr resume - the Full-Stack role
leads (ats 87) over a Staff role with higher keyword overlap (kw 6 vs 4, ats 86), the nursing posting
gate-dropped. *Optional follow-up:* feed `ats_score` into the `Predictor` as a feature (today it only
tiebreaks) and surface it in the Discover tab. See [[051-park-and-resume]], [[046-fit-learning]],
[[043-multidimension-fit]], [[025-hybrid-matching]].

## 053 — Workday M1 brick 4: account create / sign-in + IMAP email verification

**Context:** Workday gates the application behind a per-tenant candidate **account** — the wizard
adapter (bricks 2–3, decision 050) can't run until we're past sign-in. The settled design: a
dedicated **bot-owned email** receives verification, but **every tenant password is stored** so the
user can log in manually later (brick 1, `credentials.py`). Brick 4 adds the account flow and the
email-verification reader. Constraint: no real Workday during dev (Guideline #3) and no real inbox
in tests — so everything is built against fakes/fixtures, with the single real-inbox path flagged.

**Decision:** (1) **`mailbox.py`** — IMAP reader for the bot inbox. `extract_verification` (pure,
tested) pulls a portal-looking verification **link** (URL containing verify/activate/myworkdayjobs/…)
or, failing that, a 6–8 digit **code** from an email body. `fetch_verification` does one IMAP pass
(newest-first, filtered by From) and `wait_for_verification` polls until one arrives or times out;
the IMAP connection (`_connect`) and sleep are injected so tests use a fake IMAP with real
`email`-module bytes — no network. Credentials come from the **environment** (secrets, Guideline
#12): `MAILBOX_IMAP_HOST` / `MAILBOX_EMAIL` / `MAILBOX_PASSWORD` (+ optional `_PORT`); missing ⇒
`load_config` returns None and callers degrade to "verify manually". (2) **`workday.py` account
functions** — `sign_in` fills+submits the sign-in form; `create_account` reveals the create form
(toggle), fills email/password/verify-password, ticks the terms checkbox, submits;
`generate_password` makes a complexity-meeting random password via `secrets`. `ensure_account`
orchestrates: stored account ⇒ sign in; else ⇒ create on the **bot email** (so verification lands in
our inbox; falls back to the profile email without a mailbox), **persist immediately** (never lose a
password even if verification lags), then complete verification via `mailbox` (open the link or type
the code) when configured. All account controls are `:visible`-scoped (sign-in and create forms
share `email`/`password` ids — only the shown one is touched, as on real Workday). Nothing here
submits an application (M1 dry-run). `ensure_account` takes `backend`/`index_path` so tests use a
fake keychain + temp index (no real profile writes — the isolation lesson from decision 046).

**Reasoning:** Splitting the parser (pure) from the IMAP/browser I/O makes the risky parts
(link/code extraction, create-vs-sign-in branching, bot-email selection, store-before-verify) fully
unit-testable offline, leaving exactly one thing that inherently needs the real world — a live inbox
receiving a real Workday email — as the flagged live step, not a blocker to progress. Storing the
password before verification directly serves the settled "never lose a password" requirement.
Preserves existing behaviour (Guideline #7): `mailbox.py` is new; the account functions are unwired
(brick 5 navigates to the account screen and calls them) so no current flow changes. **Verified:**
16 offline tests (`tests/test_mailbox.py` ×11 — link-preferred/code-fallback extraction, punctuation
trim, config gating, newest-matching-sender fetch over a fake IMAP, poll-until-present + timeout;
`tests/test_workday.py` +5 — password complexity, `sign_in`/`create_account` driven headless against
`fixtures/apply_forms/workday_account.html` [reveals create form, ticks terms, clicks], and
`ensure_account` branching: stored⇒sign-in, unstored⇒create+persist+manual-verify flag, and bot-email
+ verification-applied). Full suite **237/237**. *Live step (flagged):* create→verify→login against a
real tenant with `MAILBOX_*` set. *Remaining M1:* brick 5 (apply dispatch: navigate to the account
screen → `ensure_account` → `fill_wizard`; stop `_is_fillable` dropping Workday; dry-run tracker row).
See [[050-workday-hybrid]], [[035-submit-stage]], [[012-pii-out-of-git]].


## 054 — Discovery→offer funnel on the Track tab (AutoApply-AI survey #4)

**Context:** The Track tab showed per-status pill counts but no view of the pipeline as a
*journey* — how many discovered postings actually get filled, submitted, and heard back from.
AutoApply-AI's dashboard has a conversion funnel; user approved adopting it (survey #4).

**Decision:** `tracker.funnel_report()` returns six stages, each a count of applications whose
current status falls in that stage's set, plus the conversion from the previous stage:
Discovered (all) -> Filled (dry-run/blocked/submitted) -> Applied (actually submitted) ->
Responded (a human replied, a rejection included; `no-response` excluded) -> Interview -> Offer.
The stage sets are deliberately NESTED (each later set is a subset of the earlier), so counting a
row by its single latest status still yields a monotone funnel — "reached this stage or beyond" —
without needing per-row history. Served in the `/track` response, rendered as labeled horizontal
bars (width relative to Discovered, count + % of top + conversion) above the Track table, and a
`python -m applicationbot.tracker funnel` CLI command.

**Reasoning:** A funnel over the current status column is honest and cheap — no schema change, no
history table, read-only. Counting a rejection as a "response" (a human engaged) but `no-response`
as applied-not-responded matches how the calibration report already frames outcomes (decision 043).
In dry-run-default mode the funnel correctly shows Applied≈0, making the safety switch visible
rather than hiding it. Preserves existing behaviour (Guideline #7): additive `funnel` key + new UI
panel; nothing else changes. **Verified:** 4 offline tests (`tests/test_funnel.py`: monotone
counts, the rejected-vs-no-response split, conversion rates, empty-DB no-divide-by-zero); full suite
**241/241**; drove `funnel_report` + the CLI + the live `/track` payload. See [[043-multidimension-fit]],
[[024-tracking-store]], [[051-park-and-resume]].


## 055 — Feed the deterministic pre-score into the fit-learning predictor

**Context:** Decision 052 added `ats_score`, a deterministic 0-100 pre-score that orders the judge
queue. Decision 046's `Predictor` re-ranks the free pre-filter by fit learned from history, but from
only two features — seniority level and board. The pre-score is a strong, cheap third signal, and —
more importantly — its *reliability* varies per résumé: the heuristic may over- or under-credit
certain postings. Letting the predictor learn the pre-score→actual-fit relationship calibrates it.

**Decision:** (1) `fit_learning._record` now stores each judged posting's `ats_score` in
`fit_history.jsonl`. (2) `Predictor` builds a third shrunk bucket — the pre-score band (`_prescore`,
band width 20 → five bands 0-19…80-100) — and `predict(posting, ats_score=None)` averages it with
the level and board estimates when an `ats_score` is supplied AND history carries pre-scores.
(3) `matching.match` passes `m.ats_score` into `predict`.

**Reasoning:** This makes the predictor *calibrate* the heuristic instead of blindly trusting it: if
high-pre-score postings have historically judged low for this résumé, the 80-100 band's learned mean
is low and the rank is tempered — observed Claude verdicts win over the deterministic guess (shown
live: a history where ats-90 postings judged 25 makes `predict(ats=90)=43` < `predict(ats=40)=52`).
Fully back-compatible (Guideline #7): pre-053 history has no `ats_score`, so `_prescore` is empty and
`predict` returns the exact old level+board average; the `ats_score` arg is optional so existing
callers and tests are untouched; the band shrinks toward the global mean like every other bucket, so
a thinly-seen band can't swing the rank. **Verified:** 4 new tests in `tests/test_fit_learning.py`
(band separation, misleading-band tempering, pre-053 no-op equality, `_record` carries the score);
full suite **244/244**; drove the calibration case end-to-end.

**Surfacing (added):** `fit_learning.prescore_calibration(records)` groups judged history by
pre-score band and reports each band's sample count + **mean actual Claude fit**;
`prescore_insight()` adds a one-line read of the direction (higher quick-score → higher/lower/flat
actual fit). `/fit-insights` returns it and the Discover-tab fit-insights panel renders it as a
mini bar chart ("how well the quick pre-score predicts fit") with the interpretation line — so the
user (and future agents) can *see* whether the heuristic is trustworthy for this résumé and that the
learner is calibrating it. Hidden until pre-score history exists (post-053 runs). 2 more tests
(bands report mean fit; the note reads direction incl. inverted/empty); suite **257/257**; drove the
live `/fit-insights` payload (bands 0-19→20, 40-59→48, 80-100→78, well-calibrated note). *(Also
fixed a concurrent Workday regression: the new optional bot-email `doctor` check had broken
`test_all_required_pass`, which asserted all(ok); scoped it to required checks.)* See
[[052-ats-prescore]], [[046-fit-learning]], [[025-hybrid-matching]].

## 056 — Seen-openings ledger: a preview shows only NEW openings on a re-run

**Context.** The user reported that dry-run/list searches "come back with the same openings
even on re-runs." Two independent causes compound:

1. **The snapshot cache (decision 037)** reuses the last discovery result verbatim for
   `cache_ttl_hours` (default 12h) — same postings, same order, no board search, no re-judge.
2. **Nothing you only *previewed* is remembered.** The only suppression is `skip_seen`, which
   drops postings already in the **applications tracker** (`pipeline._seen_canonical_urls`). But
   the tracker is written only when Apply actually runs on a posting — the single top match in
   `--apply-first`, or an armed runner. A plain list/dry-run records nothing, so `skip_seen` can
   never fire and the whole ranked list re-surfaces every run.

So the cache makes repeats *exact*, and the missing "seen" memory makes them *persist even on a
`--fresh` re-search* (the boards still hold the same postings).

**Options considered.**

| Option | What | Verdict |
|---|---|---|
| A — Seen ledger (separate store) | Record each surfaced posting the first time it's shown; suppress on re-runs; `--all` to show everything | **Chosen** (user-selected) |
| B — Just bust the cache in list mode | Re-search + re-rank every run; still shows repeats, just freshly judged | Rejected — doesn't stop repetition, only re-pays for it |
| C — Write `status='discovered'` tracker rows | Reuse `skip_seen` as the seen-set | Rejected — bloats the applications tracker with hundreds of never-applied rows; pollutes `status_counts`/calibration |

**Decision.** New `applicationbot/discovery_seen.py` — a git-ignored `profile/discovery_seen.json`
mapping each shown posting's **canonical URL** → first-seen timestamp. `discover_and_match` gains
`only_new: bool = False`; when True, `_hide_already_shown` drops matches whose canonical URL is in
the ledger, then records the survivors so the next preview hides them too. It is layered **on top
of** the cache and `skip_seen` and re-applied fresh each run — the cached snapshot still holds the
FULL ranked result, so `--all`/reset can always recover everything.

Kept deliberately **separate from the tracker**: a ledger entry means "shown once", a tracker row
means "applied / acted on", so previewing never touches application history or the outcome-
calibration stats built from it (decision 043).

**Wiring.**
- CLI `pipeline` list path: `only_new=True` by default; `--all` shows everything (no suppress, no
  record), `--reset-seen` clears the ledger then runs; `python -m applicationbot.discovery_seen
  {count,clear}` inspects/resets it. Summary line reports how many were hidden.
- Web testing worker: normal run = new-only; **"Re-search fresh"** (`force_fresh`) shows all again
  (`only_new=False`) — the user explicitly asked to see the full board result. The empty-result
  message names how many were hidden and points at "Re-search fresh".
- **Autonomous runner: unchanged** (`only_new` stays False) — it applies to matches, which land in
  the tracker, so `skip_seen` already keeps it from repeating; suppressing not-yet-applied matches
  there could starve its queue.

**PII (Guideline #12).** The ledger holds URLs of roles you're targeting → git-ignored `profile/`,
never committed, never leaves the machine.

**Verified.** 6 new offline tests (module round-trip/dedup/canonicalize/clear + bad-file tolerance;
pipeline new-only hides on re-run via the cache-hit path, surfaces just the genuinely new posting,
`--all` ignores+doesn't-record, off-by-default writes no ledger). Full suite **250/250**. See
[[037-discovery-cache]], [[025-hybrid-matching]] (skip_seen), [[024-tracking-store]].

## 057 — Link the bot email inbox (secure store + Profile-tab UI + CLI)

**Context:** Brick 4 (decision 053) made Workday account verification hands-off *if* a bot inbox is
configured — but the only way to configure it was env vars (`MAILBOX_*`), with nothing persisted and
no UI. The user asked for "a place to link the email account" before wiring Workday into the pipeline
(brick 5). It has to be secure (an IMAP password is a live secret, Guideline #12) and usable from the
web UI (UI Principle #1: setup is a working surface, not a to-do list).

**Decision:** A proper linking surface across three faces, one secure store. **Store:** the
**password lives in the OS keychain** (`keyring`, service `applicationbot-mailbox`, decision 050's
pattern) — never on disk; only host/email/port go in git-ignored `profile/mailbox.yaml`.
`mailbox.save_link/load_link/clear_link/link_status/is_linked` manage it; `load_config` now prefers a
stored link, then falls back to the environment (headless still works). `test_connection` does a real
IMAP login + INBOX select and returns an **actionable** (ok, message) (UI Principle #3);
`suggest_host` guesses the IMAP host from common email domains. `link_status` is non-secret (never
returns the password). **Web UI:** a "Bot email — for Workday verification (optional)" panel in the
Profile tab (`GET /mailbox`, `POST /mailbox/link`, `POST /mailbox/unlink`) with email/host/port/
app-password fields, a **Link & test** button (shared spinner, UI Principle #5) that **tests before
it saves — bad credentials are never stored**, an Unlink button, and a live linked/unlinked status.
**CLI:** `python -m applicationbot.mailbox link|status|test|unlink` (password prompted, not echoed) for
headless. `doctor` gains an optional Bot-email check reporting linked/unlinked + how to fix.

**Reasoning:** Keychain-for-secret + git-ignored-file-for-config mirrors the Workday credential
decision (050) and avoids the plaintext-password-in-YAML anti-pattern the audit already flagged for
Greenhouse. Test-before-save means a "Linked ✓" is a *working* link, not a stored guess. Three faces
(UI/CLI/doctor) over one store means the web user gets the "place to link" they asked for while
headless/cron runs keep working via env or the same keychain link. Preserves existing behaviour
(Guideline #7): `load_config` still returns the env config when nothing is linked, so decision-053
callers are unchanged; env-only tests still pass. **Verified:** 8 new offline tests
(`tests/test_mailbox.py`: host suggestion, save/load with the password in the keychain and NOT in the
file, link-over-env precedence + env fallback, status/clear round-trip, test_connection ok + actionable
failure). Full suite **257/257**; served JS `node --check`-clean; the three endpoints driven live (fresh
status → a bad-host link **fails the test and does not save** → still unlinked → 400 on empty fields →
unlink); `doctor` shows the ⚠ line; `profile/mailbox.yaml` confirmed git-ignored. Unblocks brick 5:
Workday's `ensure_account` calls `mailbox.load_config()`, which now finds the linked inbox.
See [[053-workday-brick4-accounts]], [[050-workday-hybrid]], [[049-captcha-autosolve]], [[012-pii-out-of-git]].


## 058 — Per-click armed resume for parked applications (park & resume M3)

**Context:** Decisions 051 (M1/M2) made a blocked application resumable, but the "Re-apply" button
was always dry-run — the autonomous runner (armed via `profile/safety.yaml`) was the only path that
actually submitted. That forced an all-or-nothing choice: to submit even ONE reviewed application
you had to globally arm the system, which also lets the runner submit everything. The user asked to
"look into one-click resume"; after presenting the trade-offs they chose a **per-click arm**: submit
one specific application on demand, without touching the global arm.

**Options considered (arming model):** (A) **per-click arm + confirm** — the card submits THIS
application even when `safety.yaml` is disarmed, gated by a confirm dialog; (B) respect the global
`safety.yaml` only (button submits solely when already globally armed); (C) keep it dry-run only.
**User chose (A).** It gives a deliberate, human-in-the-loop, one-at-a-time submit — arguably the
safest *real*-submit path — without arming the fire-and-forget runner.

**Decision:** A red "Submit for real ▶" button on each resumable parked card. `_reapply_gate(arm)`
returns a one-shot `SafetyGate(armed=True, max_submissions_per_run=1)` (or `None` when not armed);
`_reapply_worker(app_id, arm=True)` passes it into the SAME `run_apply` armed path the runner uses,
so the pre-submit required-field gate, confirmation detection, and the tracker `applied`/`blocked`
recording (decision 035) are all reused — no new submit logic. `/parked/reapply` takes an `arm`
flag; `start_reapply(arm=…)` threads it through.

**Safety architecture (this adds a second arming path, so it is fenced):**
- The per-click gate is armed for exactly ONE submission (cap 1) and is independent of
  `safety.yaml` — but the global `profile/KILL` file STILL halts it (checked in `SafetyGate.may_submit`
  immediately before the click). Verified live: with `KILL` present the armed gate refuses.
- `run_apply`'s pre-submit gate still blocks the click while any REQUIRED field is unresolved, so an
  unresolved block records `blocked`, never a bad submit.
- A client-side `confirm()` names the company before the POST — no accidental one-click submit.
- Because a POST can now trigger an irreversible submission on the loopback server, the ARMED branch
  of `/parked/reapply` requires a same-origin request (`_same_origin`: a present Origin/Referer must
  be a loopback host; absent passes) — closing the drive-by cross-site-submit hole for the one
  endpoint that can now submit. The dry-run branch is unaffected.

**Reasoning:** Reusing `run_apply`'s armed path (not a parallel submit) keeps the single audited
submit implementation and its safety gate; the per-click arm is strictly *narrower* than the global
arm (one application, cap 1, still KILL-able), so it doesn't weaken the safety model — it adds a more
granular, more deliberate option. The same-origin check is scoped to the newly-dangerous branch
rather than a broad CSRF retrofit (the audit's general CSRF item stays open, tracked separately).
Preserves existing behaviour (Guideline #7): dry-run re-apply is byte-identical; nothing submits
unless the user clicks the red button and confirms. **Verified:** 6 new tests in
`tests/test_parking.py` (gate armed/cap/kill-file identity; the `_same_origin` matrix incl. a
cross-origin Referer; and a live-server assertion that an armed cross-origin POST gets 403 with NO
run started, while same-origin starts one) + JS `node --check`-clean; full suite **260/260**; drove
the KILL-halts-the-armed-gate invariant end-to-end. *Remaining (optional):* a credentials UI for the
`login`-kind parked cards (still instruction-only). See [[051-park-and-resume]], [[035-submit-stage]],
[[040-autofill-determinism]].

## 059 — Workday M1 brick 5: end-to-end wire-in (dry-run). M1 complete.

**Context:** Bricks 1–4 built the Workday pieces in isolation (credentials, field-fill, wizard
nav + dropdowns, account create/sign-in + IMAP verify) but nothing was wired into the pipeline —
`_is_fillable` still dropped every Workday posting before the matcher, and `run_apply` had no path
to the adapter. Brick 5 connects them so the whole Workday flow runs end-to-end through the
existing Discover → Apply → Track pipeline, DRY-RUN (M1 never submits).

**Decision:** (1) **`workday.apply_workday(page, url, resume, profile, report, …)`** orchestrates
the flow: `start_application` (click Apply → Apply Manually to reach the account screen;
`:visible`/role-based, no-op if already there) → `ensure_account` (decision 053) → best-effort
résumé upload if a file field is present → `fill_wizard` (bricks 2–3). It records progress on the
report and **never submits** — there is no armed/submit branch in the Workday path at all.
(2) **`run_apply` dispatch:** when `detect_ats(url) == "workday"`, route to `apply_workday` instead
of `_open_application_form` + native-autofill + `_fill_all_pages` + `_attempt_submit`; the
non-Workday path is byte-identical, just indented under the `else` (Guideline #7). The bot mailbox
is loaded via `mailbox.load_config()` (the linked inbox from decision 057, or env). (3) **Gate
opened:** `pipeline._is_fillable` now returns True for `ats == "workday"`, and the aggregator→ATS
bridge marks a resolved Workday posting `auto_applyable=True` — so Workday postings discovered via
curated feeds / the aggregator bridge reach the matcher and the adapter instead of being dropped.
The existing tracker-record path logs the run as a `dry-run` row unchanged.

**Reasoning:** A single dispatch point in `run_apply` keeps every downstream consumer (pipeline
testing-mode, the autonomous runner, the web Discover tab) working through the same entry with no
per-caller changes. Routing Workday to its own path (rather than teaching the generic filler about
Workday's custom-widget DOM + account gate) keeps both paths simple and independently testable.
DRY-RUN-only for the whole Workday path means opening the fillability gate is safe: an unverified
real-tenant navigation records a `blocked`/`failed` outcome (the parking model), never a submission
and never a crash (`apply_workday` catches and reports). Preserves existing behaviour (Guideline
#7): the six public-API ATSs are untouched; only Workday changes, and it was previously a no-op
(dropped). **Verified:** new end-to-end fixture `workday_full.html` (job page → Apply → Apply
Manually → account **create** → 3-page wizard → Review) driven headless: account created + stored
with the profile email, all standard fields + custom dropdowns filled across 3 pages, résumé
attached, and `window.__submitted` stays **False** — Submit is never clicked. Plus a dispatch test
(`run_apply` on a Workday URL calls `apply_workday`, NOT `_open_application_form`) and
`_is_fillable(workday) is True`. Updated the decision-035 fillability test to the new behaviour.
Full suite **264/264**. **Live step (flagged, unchanged from brick 4):** the whole flow against a
real Workday tenant with a linked bot inbox — the button labels / automation ids are the tuning
surface. **M1 (deterministic login + standard fields, dry-run only) is complete.** Next: M2
(agentic fallback for unrecognized pages + recipe distillation), M3 (armed submit).
See [[053-workday-brick4-accounts]], [[057-mailbox-link]], [[050-workday-hybrid]], [[035-submit-stage]], [[024-tracking-store]].


## 060 — MyGreenhouse password: plaintext YAML → OS keychain

**Context:** `ApplicationProfile.greenhouse_password` (the MyGreenhouse native-autofill login,
decision 017) was a **plaintext field in `profile/application_profile.yaml`** AND was **sent to the
browser on every `GET /profile`** and round-tripped through `/profile/update`. The full-system audit
(2026-07-06) flagged both ("stop serving the plaintext Greenhouse password via GET /profile";
Guideline #12 — PII/secrets must not sit in readable files). The codebase already had the correct
pattern twice — `credentials.py` (050) and `mailbox.py` (057) put the secret in the OS keychain via
`keyring` and keep only non-secret metadata in a file — so this aligns Greenhouse to it.

**Decision:** The password now lives in the OS keychain; the email stays in the YAML (non-secret,
as mailbox keeps host/email).
- **`apply_profile`**: `set_greenhouse_password`/`get_greenhouse_password` (keychain, service
  `applicationbot-greenhouse`, injectable backend for tests); `greenhouse_linked(profile)` = email +
  stored password; `greenhouse_credentials(profile)` = (email, keychain-password, falling back to a
  not-yet-migrated plaintext value). `save_profile` drops `greenhouse_password` from the YAML dump.
- **One-time auto-migration** in `load_profile`: a legacy plaintext value is moved into the keychain
  and scrubbed from the YAML (idempotent; best-effort — if `keyring` is unavailable the plaintext is
  left and still works via the fallback). No manual step (Guideline #8).
- **web**: `GET /profile` strips the password and adds `greenhouse_linked`; `/profile/update` routes
  a typed password to the keychain (**blank = keep the stored one**, so an ordinary save never wipes
  it) and never persists it; new `POST /profile/greenhouse/unlink` clears it. The Profile field is
  **write-only** (placeholder shows saved-state, value never prefilled) with a **Disconnect** button.
- **apply**: `_greenhouse_native_autofill` reads `apply_profile.greenhouse_credentials(profile)`.

**Reasoning:** Removes BOTH exposures (plaintext-on-disk and served-over-HTTP) using the proven
keychain pattern — no new dependency (`keyring` already in for 050), no new store to maintain. The
email staying in YAML keeps the connected/not-connected UI honest without a secret. Write-only +
"blank keeps existing" avoids the classic bug where re-saving a form wipes a password the UI can't
display. Preserves behaviour (Guideline #7): autofill still uses the same credentials, just sourced
from the keychain; the migration means existing users lose nothing on upgrade. **Verified:** 6 tests
(`tests/test_greenhouse_creds.py`, in-memory keyring fake: round-trip, linked logic, keychain-then-
plaintext precedence, YAML never carries the password, migration scrubs + is idempotent) + full suite
**270/270**; drove `GET /profile` + migration + unlink over the live HTTP server — the password is
absent from the payload, the plaintext is scrubbed from the YAML on load, the keychain holds it, and
Disconnect flips `greenhouse_linked` to false. `profile/*` is already git-ignored; the keychain is
OS-level (no file). *Note:* a generic credentials UI for `login`-kind parked cards was investigated
and rejected the same day (near-empty trigger set; Workday already uses the keychain) — this is the
real credential-hygiene win instead. See [[057-mailbox-link]], [[050-workday-credentials]],
[[017-native-ats-autofill]], [[058-armed-resume]].

## 061 — Workday M2 (part 1): recipe backbone + agentic-fallback distillation

**Context:** M1 fills Workday's standard fields by stable `data-automation-id`, but a tenant's
custom "Application Questions" have unknown ids the deterministic adapter can't handle. Option C's
answer (decision 050): an agentic worker fills such a page ONCE, and we distill a **recipe** so
the same page replays deterministically forever after — agentic use trends to 0. This decision is
the offline-testable core (recipe store + detection + replay + distillation); the live agentic
invocation (Claude over CDP) is built but flagged, and the pipeline wire-in is M2 part 2.

**Decision:** (1) **`workday_recipes.py`** — a **shared, committed, PII-free** library
(`applicationbot/workday_recipes.json`, ships as `{}`): `{page_signature: [{automation_id, control,
question}]}`. A recipe stores only the selector + control kind + question label — **never an answer
value** (the answer is re-resolved per user at replay), so it's safe to commit and share across
clones. `load_recipes`/`get_recipe`/`save_recipe` (upsert, dedupe by automation_id). Page signature
= the md5 of the visible `data-automation-id` set (`_page_signature`, from M1). (2) **Detection:**
`workday.unrecognized_fields(page)` returns the VISIBLE, still-EMPTY, fillable controls whose id
isn't in the adapter's known set (`_KNOWN_IDS`) — each `{automation_id, control, question}` (label
from `<label>`/aria-label). (3) **Replay:** `replay_recipe(page, recipe, resolver, report)` fills a
learned page deterministically — each field's answer re-resolved via the existing `AnswerResolver`
(text/dropdown/checkbox), source `workday-recipe`, no Claude. (4) **Agentic worker + distillation:**
`run_agent_fill` hands the page's unrecognized fields to a Claude-Code + Playwright-MCP worker bound
to OUR browser over CDP (`_agent_argv`/`_agent_mcp_config` mirror ApplyPilot's launcher; `agent_prompt`
carries the fields + compact applicant facts + HARD RULES: never navigate, never fabricate
citizenship/work-auth/education), then **distills the recipe by DIFFING which fields went
empty→filled** — robust, with **no dependence on parsing opaque MCP element refs**. `_spawn` is
injectable so tests drive a fake agent with no Claude/CDP.

**Reasoning:** The diff-based distillation is the key insight: rather than reverse-engineering the
agent's MCP tool calls (which reference accessibility refs like "e5", not stable selectors), we
observe the DOM outcome — the fields that became filled ARE the recipe, keyed on their stable
`data-automation-id`. This makes the whole learn→replay loop deterministic and offline-testable, and
keeps recipes PII-free (selectors + labels only) so the committed library is shareable. Reusing
`AnswerResolver` at replay (not storing the agent's answers) means a learned page fills correctly for
*every* user, not just the one whose agent learned it. Preserves existing behaviour (Guideline #7):
all new code is unwired into the pipeline until M2 part 2; M1 paths unchanged. **Verified:** 8 offline
tests (`tests/test_workday_recipes.py`): store round-trip + **PII-free assertion** + merge-dedupe;
`unrecognized_fields` finds custom questions and skips known + already-filled (headless on new
`workday_custom.html`); `replay_recipe` fills resolvable + skips open-ended; distillation captures
exactly the diff (agent filled 2 of 3 → recipe has those 2); and the **full learn-once → replay
loop** (agent learns a field → persist → fresh page → deterministic replay re-resolves it per user,
**no agent**). Full suite **278/278**. **Remaining (M2 part 2):** wire into `fill_wizard`/
`apply_workday` (deterministic → recipe replay → agentic fallback → persist), launch the browser with
a CDP endpoint in `run_apply`, gate the agentic fallback (off by default, needs Claude); then the
flagged live step — a real tenant's custom page.
See [[059-workday-brick5-wirein]], [[050-workday-hybrid]], [[040-autofill-determinism]], [[034-stripped-claude-cli]].


## 062 — General CSRF/origin guard on state-changing POSTs

**Context:** The full-system audit flagged "CSRF/origin guard on state-changing POSTs" as open.
Decision 058 added a same-origin check to the ONE endpoint that could then fire an irreversible
submit (`/parked/reapply` armed), but every other POST to the localhost UI — `/profile/update`,
`/discovery/update`, `/track/*`, `/mailbox/*`, `/resume/*`, `/pdf`, `/tailor`, `/test-run`,
`/fit-insights/apply`, the dry-run reapply — is also state-changing (writes files, launches a
browser). Without a guard, a web page on another site the user has open in the same browser could
POST to `http://127.0.0.1:8000/...` and drive the server (the response is CORS-blocked, but the
side effect already happened server-side).

**Decision:** One choke point at the top of `do_POST`: `if not _same_origin(self): 403` before any
body read or dispatch — so every current and future POST is covered by construction. `_same_origin`
generalized from the 058 version:
- missing Origin/Referer → pass (same-origin fetches often omit Origin; non-browser clients
  curl/CLI/tests send none, and they are not the CSRF threat model);
- a loopback Origin host (127.0.0.1/localhost/::1) → pass;
- otherwise the Origin host must equal the `Host` header the client addressed — so the guard is
  correct when the server is bound to a LAN IP or hostname via `--host`, not only 127.0.0.1.

A browser sets `Origin` itself on cross-site POSTs, so a remote attacker page cannot forge it to a
loopback value. The now-redundant per-endpoint check in `/parked/reapply` (058) was removed (clean
up my own mess). GET requests stay unguarded — they are read-only here.

**Reasoning:** A single wrapper is the right altitude for a blanket policy — per-endpoint checks rot
as endpoints are added (exactly how 058 left the rest exposed). Matching Origin against the actual
`Host` (not a hardcoded loopback list) fixes a real correctness bug the naive version had: a
`--host 0.0.0.0` bind accessed over the LAN would have blocked the app's OWN POSTs. Allowing a
missing Origin keeps the CLI/tests and legit same-origin fetches working while still blocking
browser-driven cross-site POSTs (browsers always send Origin cross-origin). Preserves behaviour
(Guideline #7): same-origin UI use is unchanged; only cross-origin POSTs — which never had a
legitimate purpose — now 403. This also retro-guards the parallel agent's `/mailbox/*` endpoints,
a strict improvement, without touching their code. **Verified:** 6 tests
(`tests/test_web_csrf.py`: a real cross-origin POST to `/track/add` gets 403 with the handler never
called, a cross-site Referer likewise, no-Origin and loopback-Origin reach the handler, a
cross-origin GET still works; `_same_origin` unit matrix incl. the LAN-bind Host match) + full suite
**280/280**. Together with decision 060 (greenhouse password → keychain), the audit's
"CSRF/origin guard … stop serving the plaintext Greenhouse password" item is now fully closed. See
[[058-armed-resume]], [[060-greenhouse-keychain]], [[035-submit-stage]].

## 063 — Workday M2 (part 2): agentic fallback + recipe replay wired into the pipeline

**Context:** Decision 061 built the M2 core (recipe store, unrecognized-field detection, replay,
agentic distillation) but left it unwired. Part 2 activates it inside the Workday apply path and
opens the browser's CDP endpoint so the agentic worker can attach — completing M2 except the
flagged live Claude-over-MCP run.

**Decision:** (1) **Per-page handling** — `workday._resolve_unrecognized(page, resolver, report,
…)` runs after each page's deterministic fill: replay a learned recipe (free, deterministic) first;
only if custom fields still remain AND the agentic fallback is armed does it call `run_agent_fill`
(decision 061) and persist the learned recipe. `fill_wizard`/`apply_workday` gained optional
`resolver`/`agentic`/`cdp_port`/`store_path`/`_agent_spawn` params and call it per page — **no
resolver ⇒ pure M1, unchanged** (Guideline #7). (2) **CDP endpoint** — `run_apply` computes
`wd_agentic = (ats == "workday") and workday.agentic_enabled()`, and for such runs launches Chromium
with `--remote-debugging-port=<free port>` (`workday._free_port()`) so the Playwright-MCP worker can
`connectOverCDP` to the SAME page; the port + resolver + `agentic` flag thread into `apply_workday`.
(3) **Gating** — `workday.agentic_enabled()` reads `workday_agentic` from profile/safety.yaml,
**off by default** (the fallback spends Claude tokens on novel pages; opt-in, mirroring the CapSolver
fencing of decision 049). Recipe **replay is always on** when a resolver is present (it's free);
only learning a NEW page via Claude is gated, and it also degrades to a recorded note if Claude Code
isn't signed in.

**Reasoning:** Replay-before-agent means the expensive path runs at most once per distinct page
across all runs/users (then the committed recipe covers it) — the "agentic → 0" property, now
actually in the loop. Gating only the agentic *learning* (not replay) keeps steady-state cost at
zero while still letting a fresh page be learned when the user opts in. Threading everything as
optional kwargs keeps the M1 path and all its tests byte-identical. Opening CDP only for armed
Workday runs avoids the free-port dance + extra Chrome surface on every other apply. **Verified:**
4 new tests — `agentic_enabled` off-by-default/opt-in; `fill_wizard` **learn-once (agentic on) →
replay (agentic off), agent runs exactly ONCE**, customGithub re-resolved per user via the recipe;
`fill_wizard` with no resolver stays pure M1; and a **live drive of `run_apply`** (agentic armed,
adapter stubbed) confirming Chromium launches with a real `--remote-debugging-port` and
`agentic=True` + a real free `cdp_port` + the resolver reach `apply_workday`. Full suite **283/283**.
**M2 is complete** but for the one flagged live step: a real tenant's custom page driven by the
actual Claude-Code + Playwright-MCP worker (needs Claude signed in, npx, and `workday_agentic: true`).
Next: M3 (armed submit).
See [[061-workday-m2-recipes]], [[059-workday-brick5-wirein]], [[049-captcha-autosolve]], [[035-submit-stage]].

## 064 — Workday M3: armed submit (gated by the SafetyGate)

**Context:** M1/M2 fill a Workday application end-to-end but `apply_workday` never clicked the final
Submit — dry-run only. M3 extends the armed submit path (decision 035) to the Workday wizard so an
armed run actually applies, with the same safety architecture the open ATSs use. Submission is
irreversible (Guideline #3), so it stays behind the SafetyGate — armed, kill-switchable, capped,
re-checked at the last moment.

**Decision:** `workday._attempt_workday_submit(page, report, gate)` — reached only from
`apply_workday` when `gate.armed`, after `fill_wizard` has walked to the Review page. Order: (1) a
visible `pageFooterSubmitButton` must be present (else `blocked` — not the Review page); (2)
`_workday_unmet_required(page)` scans visible `aria-required`/`required` fields (and custom
dropdowns still on their "Select One" placeholder) that are empty → `blocked` with the field names,
BEFORE any click; (3) `gate.may_submit()` (armed + no profile/KILL + under the per-run cap) checked
immediately before the click → `blocked` with the reason otherwise; (4) click Submit and
`gate.record_submission()` (count the click, not the confirmation — conservative vs. the cap); (5)
confirmation detection — reuse `apply._confirmation_evidence` (URL/text) → `submitted`; a visible
Workday error (`role=alert` / `data-automation-id*=error`) → `blocked` "rejected the submit"; and if
the Submit control is gone with neither → `unconfirmed`-but-submitted so a re-run never double-submits
(decision 035's rule). `apply_workday` gained a `gate` param; `run_apply` passes its gate into the
Workday branch (so the Workday path owns its own submit and the generic `_attempt_submit`/dry-run
branch is skipped). Any doubt is a recorded `blocked` outcome, never a prompt.

**Reasoning:** Reusing the exact SafetyGate semantics (may_submit/record_submission, decision 035)
means Workday inherits every existing guarantee — `armed: false` default, the `profile/KILL` global
halt, the per-run cap, and the `--dry-run` force-disarm on the CLIs — with no parallel safety logic
to keep in sync. Workday-specific bits (required-field scan, validation-error detection, Review-page
gating) are the only new surface, and they fail *closed* (block, don't submit) on any uncertainty.
Preserves existing behaviour (Guideline #7): no gate / unarmed ⇒ M1/M2 dry-run, unchanged; the six
open ATSs are untouched. **Verified:** 5 new tests driving fixtures headless — armed happy path
submits (confirmation detected, cap incremented); an empty required field blocks BEFORE the click
(cap untouched, `window.__submitted` False); the KILL file blocks; an unarmed gate blocks; and the
**full armed flow** (`workday_full.html`: Apply → create account → wizard → Review → **Submit**)
sets `submitted`/`submit_state=submitted`/cap=1. Full suite **289/289**. **Workday M1+M2+M3 are now
code-complete;** the sole remaining item is the flagged live run on a real tenant (armed dry-run
first, then a real submit once the user arms it) — which also exercises the M2 Claude-over-MCP agent.
See [[035-submit-stage]], [[059-workday-brick5-wirein]], [[063-workday-m2-part2]], [[049-captcha-autosolve]].

## 065 — One-click Gmail connect via OAuth

**Context:** The bot-email link (decision 057) was the friction point the user called out: to connect
Gmail you had to enable 2FA, dig through Google account settings to mint a 16-char **app password**,
and hand-type email + IMAP host + port. That is the opposite of "one-click." The genuine one-click
way to connect Gmail is OAuth — "Sign in with Google": a button → a browser consent screen → done.

**Options considered:** (A) **OAuth + IMAP-over-XOAUTH2** — smallest code change (reuse the whole
IMAP fetch path) but Gmail's IMAP only accepts the **full `mail.google.com` scope** (read *and* send
*and* delete) — far more access than reading a verification email needs. (B) **OAuth + Gmail REST API
with `gmail.readonly`** — read-only, least-privilege, but the fetch path moves off imaplib. (C) keep
app-password, just deep-link the user to Google's app-password page — smoother, still not one-click.
Chosen: **(B)**. The user picked "OAuth (true one-click)"; read-only is the correct grant for a bot
that only reads verification mail (Guideline #5), and both restricted scopes carry the same Google
verification burden anyway, so readonly is strictly better than the IMAP-forced full scope.

**Decision:** `mailbox.connect_gmail(client_id, client_secret)` runs the loopback consent flow
(`google-auth-oauthlib` `InstalledAppFlow.from_client_config(...).run_local_server`, with
`access_type=offline`+`prompt=consent` so Google returns a refresh token *every* run — it omits it on
silent re-consent otherwise), reads the connected address from the Gmail `/profile` endpoint, then
**tests the connection before saving** (the link-before-save rule of 057): nothing is persisted unless
a refresh token comes back AND a read succeeds. Storage mirrors 057/060 — refresh token + client
secret in the OS keychain (service `applicationbot-gmail-oauth`, one JSON entry), and only non-secret
email / client_id / `auth: oauth` in git-ignored `profile/mailbox.yaml`. Reads go through the Gmail
REST API (`gmail.readonly`) with `urllib` + a Bearer token minted from the refresh token
(`google.oauth2.credentials.Credentials.refresh`); `_gmail_fetch_verification` lists `from:<sender>`
newest-first, pulls each message `format=raw`, and reuses the existing pure `_body_text` +
`extract_verification`. `MailboxConfig` gained `auth`/`refresh_token`/`client_id`/`client_secret`;
`test_connection` and `fetch_verification` branch to the `_gmail_*` helpers when `auth == "oauth"` and
are **byte-identical on the IMAP/env path** (Guideline #7). Web: the Profile "Bot email" panel now
leads with a **Connect Gmail** button (client_id/secret fields + a collapsible 3-step setup guide;
the old IMAP app-password form moved into an "Advanced" `<details>`); `POST /mailbox/gmail/connect`
(CSRF-guarded by 062, threaded so the blocking consent doesn't freeze the UI, with an elapsed-time
"waiting for Google" state per UI Principle #5); `GET /mailbox` also returns the non-secret `auth` and
`client_id` so a reconnect pre-fills the id and is one click. CLI `python -m applicationbot.mailbox
connect-gmail --client-id … --client-secret …`; doctor/`status` report "Gmail, read-only".

**Reasoning:** OAuth is the only real one-click for Gmail, and read-only via the REST API keeps the
grant minimal while reusing all the tested parsing. Keeping the password/env IMAP path unchanged means
other providers and headless runs still work, and the secret-storage pattern is the same keychain-only
one already audited (057/060) — no plaintext, `profile/*` already git-ignored. The one unavoidable
cost is a **one-time Google Cloud "Desktop app" OAuth client** (client_id/secret) and setting the
project to "In production" so refresh tokens don't expire on Google's 7-day Testing-mode clock —
surfaced directly in the panel's setup steps and in the "did Google return a reusable token?" error.

**Verified:** 9 new offline tests (fake keyring + injected flow/token/get) — secrets live only in the
keychain (yaml holds neither the refresh token nor the client secret), read-only fetch reads the
newest matching message, `test_connection`/`fetch_verification` route to the OAuth path without
touching imaplib, `connect_gmail` saves on success and saves **nothing** when Google returns no token
or the test read fails. Full suite **298/298**; served JS node-clean; endpoints driven live (`GET
/mailbox` returns the new fields, `POST /mailbox/gmail/connect` 400s with an actionable message on
missing creds). **Live step flagged:** the actual Google consent needs the user's Cloud client +
a browser — like the real-inbox step of 053. New deps: `google-auth`, `google-auth-oauthlib`.
See [[057-link-bot-email]], [[060-greenhouse-password-keychain]], [[062-csrf-origin-guard]],
[[053-workday-account-verification]].

**Amendment (2026-07-13) — UI now leads with the app password, not OAuth.** On seeing the OAuth
setup (register a Google Cloud app, copy Client ID + secret), the user asked "what happened to just
pasting the email and password?" Reality check: Google blocked normal-password IMAP login in May
2022, so pasting a real Gmail password never worked — but the **app-password** flow (email + a
generated 16-char code) is fewer, more familiar steps than the OAuth app for a single user, and it is
what "paste email + password" actually means today. OAuth is one-click only *at connect time*; its
one-time setup is heavier. So the Profile panel was flipped: the **app-password form is now primary**
(Gmail address + app password, with a "How to get an app password" guide linking 2-Step Verification
+ App passwords, and a collapsed "Not Gmail? Set your mail server" for host/port — auto-detected
otherwise), and **OAuth moved into a collapsed "Prefer read-only access?" `<details>`** (its trade-off
— full-mailbox app password vs read-only OAuth — stated inline). No backend change: both `/mailbox/link`
and `/mailbox/gmail/connect` and all of `mailbox.py` are unchanged; this is UI emphasis + copy only.
JS node-clean; both paths driven live (app-password `/mailbox/link` 400s on missing fields; panel copy
served). Decision 065's "primary path is Sign in with Google" is superseded by this: **app password is
primary, OAuth is the read-only alternative.**

---

## 067 — Weak-model draft for required unmapped free-text fields

**Context:** An armed submit is gated on every REQUIRED field being resolved (decision 035). The user's
concrete case: WHOOP's "Why are you interested in working at WHOOP?" is a **single-line
`<input type="text" required>`**, not a textarea. `answer_bank.is_open_ended` only returns True for a
textarea or a >25-char question containing an explicit open-ended phrase ("describe", "how would you",
…) — "why are you interested" is none of those — so `freetext_answer` returned `None`, the field was
recorded as "no saved answer", and it blocked the submit. Any required custom question phrased outside
the heuristic hit the same wall. The user asked: fill **all** required fields, and where there's no
mapped/banked answer, draft one with "a very weak claude model."

**Options considered:** (A) broaden `is_open_ended` globally to draft more short fields — over-broad,
would draft *optional* short fields too and risks fabricating where silence is correct. (B) draft only
when the field is **required** and safe to draft — targeted at exactly the blocking case. (C) do nothing
and keep parking these for the user — safe but leaves the user hand-filling every off-heuristic required
question, against the "fully automated" goal (Guideline #0). Chosen: **(B)**.

**Decision:** Two changes.
1. **Weak model by default.** `answer_bank.DRAFT_MODEL = "haiku"` (alias, not a pinned snapshot — the
   CLI resolves the latest Haiku, consistent with the `backends.py` tier aliases). `generate_answer`
   now defaults its `model` to `DRAFT_MODEL`; a caller override is still honored. A grounded,
   résumé-only paragraph doesn't need a frontier model, and Haiku is cheaper/faster.
2. **Force-draft required fields.** `AnswerResolver.freetext_answer` gained `required: bool`. When a
   free-text field is required, it drafts even if `is_open_ended` is False — **but** only through the
   new `answer_bank.is_draftable_required`, which refuses **numeric-fact** questions (salary, GPA, test
   scores — fabricating them invents data, the AppLovin incident that motivated `_NUMERIC_FACT`) and
   **demographic/EEO** questions (self-identification is the applicant's to make). Those stay empty and
   the pre-submit gate parks them for the user (honesty, Guideline #7). Per-field required-ness is read
   live in the fill loop via new `_IS_REQUIRED_JS`/`_is_required` — the element's `required`/
   `aria-required`, or an enclosing `<label>`/`label[for]`/`.application-question`/`fieldset` marked
   with a required glyph (`*`/`✱`/`★`) or the word "required" (covers Greenhouse's `*` and Lever's `✱`).

**Reasoning:** The blocker is specifically *required* fields; gating the broadened drafting on
required-ness keeps optional fields untouched (preserved behavior) while closing the automation gap.
The numeric/demographic exclusions mean "fill all required fields" is honored *except* where filling
would mean fabricating — there the correct outcome is still to park for the user, unchanged. Weak-model
default is the user's explicit ask and a token win; it applies to all free-text drafting (including the
already-working open-ended path), which only makes those cheaper.

**Verified:** 4 new tests (`tests/test_required_draft.py`) — the `is_draftable_required` gate
(drafts an ordinary short question, refuses salary/GPA/gender/empty); a short company-specific question
drafts only when `required=True` and is left `(None, "")` otherwise; a required numeric-fact never
drafts; `generate_answer` sends `--model haiku` by default. Drove the **real committed**
`fixtures/apply_forms/lever_custom_cards.html` headless (stubbed draft, zero tokens): the WHOOP
required text input now fills with `source=generated` where before it was skipped/blocking. Full suite
green except the one pre-existing mailbox test-isolation failure (leaks the user's real
`profile/mailbox.yaml`, unrelated to apply.py). See [[066]] (the label fix that made this field's
question readable in the first place), and the decision-035 submit-gate this unblocks.

**Amendment (2026-07-14) — extend the same idea to required DROPDOWNS/SELECTS.** The free-text fix
left a sibling gap the user flagged: a **required dropdown/select** with no mapped answer still blocked
submit. Two sub-cases: (a) no answer at all — the resolver, semantic classify, and hints all miss (a
combobox was captured "no saved answer"; a native `<select>` reads as `is_free` because it has no
`type` attribute, so it fell to `_fill_select(None)` and *errored*); (b) an answer that matches no
option — comboboxes already Claude-pick this (`pick_dropdown_option`), native selects didn't. The honest
move for (a) is **not** to invent an option but to let the weak model **choose the best-fitting OFFERED
option**, grounded in the résumé — which is exactly what a human does. New
`answer_bank.choose_required_option(question, options, resume, …)`: résumé-grounded, uses `DRAFT_MODEL`,
returns an option **verbatim** or None, and **refuses** the same class the free-text path does —
demographic/EEO (`is_draftable_required`) plus fact-owning **enumerated** questions (`_ENUMERATED`:
clearance, GPA, scores). `AnswerResolver.choose_option` wraps it (off when generation is disabled). In
the fill loop the capture branch now (i) includes native selects, (ii) in round 1 **defers** an unmapped
dropdown/select to the two-pass batch (so a free classification attempt runs first), and (iii) in round 2
/ single pass tries `choose_option` for a **required** control before capturing — committing via the
existing paths (combobox through `decided_options`→`_fill_combobox`, reported tier `option:claude` and
learned; native select via `_fill_select`). `_selectable_options` feeds the picker only options with a
non-empty `value`, so a "Select…" placeholder is never chosen. For sub-case (b) the native-select
dispatch now mirrors the combobox: on no-match it tries `pick_dropdown_option` before recording a skip.
Honesty unchanged from the parent decision: filling a required box is honored *except* where it means
guessing a fact the applicant owns — those stay captured for the user. **Verified:** 4 new tests
(`tests/test_required_dropdown.py`) — the gate (picks an answerable option, refuses clearance/gender/
empty, declines on `-1`), generation-off is a no-op, and an **end-to-end headless drive** of the new
committed `fixtures/apply_forms/required_dropdowns.html` (stubbed CLI, zero tokens): the two answerable
required dropdowns fill with `source=option:claude` (never the placeholder), while the clearance and
gender dropdowns are refused and captured. Full suite **309 passed**, same one pre-existing mailbox
failure. ([answer_bank.py](applicationbot/answer_bank.py), [apply.py](applicationbot/apply.py))

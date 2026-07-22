# DECISIONS.md — Architecture & Tooling Decisions

> Every significant choice — architecture, tooling, service model, data layout,
> integration method, or a reversal of a prior decision — is logged here with its
> context, the options considered, the choice, and the reasoning (Agent Guideline #9).
> Code and scripts capture *what* the system does; this file captures *why*.

---

## Decision Index

| # | Date | Decision | Status |
|---|------|----------|--------|
| 124 | 2026-07-22 | **Two-stage judging: a cheap Haiku pre-rank widens the judged pool, then the Sonnet judge scores only the best `top_n` of it.** Decision 123 cleared the staffing spam; the remaining ceiling on cleared applications was `top_n=10` — the full judge (already Sonnet, not Opus — decision 034) only scored the top 10 keyword/ats-ranked survivors, so real early-career roles ranked 11+ never got a fit score and could never clear `min_fit`. Raising `top_n` alone scales the (Sonnet) token cost linearly. **Chosen design:** a new `prerank_n` knob (0 = off, preserves the single-stage behaviour exactly). When `prerank_n > top_n`, `matching._prerank_select` coarse-scores the top `prerank_n` survivors with **Haiku** (`prerank_fit_batch`: one 0-100 fit per posting, batched 12/call, shorter 1,800-char JD slices, `activity="prerank"`), reorders that pool by the coarse score (curated-first preserved), and passes only the best `top_n` to the existing Sonnet `judge_fit_batch`. So we *consider* far more postings (Haiku is ~⅓ the cost + faster and returns a single int) but spend the expensive judge only on the finalists. Best-effort: a Haiku failure records a `pre-rank failed …` note and falls back to the keyword/predictor order (`ranked[:top_n]`) — it never aborts or loses coverage (Guideline #11). `Match.prerank_score` carries the coarse score (None when not preranked); it's cache-roundtripped. **Plumbing:** `filters.prerank_n` (default 0), threaded `pipeline.match(..., prerank_n=filters.prerank_n)`; **`backends._API_MODELS` gained the missing `haiku` → `claude-haiku-4-5-20251001` mapping** (the API-fallback path silently used Opus for any unknown alias — a latent cost bug now that a third model is judged); **`discovery_cache._FILTER_FINGERPRINT_KEYS` gained `prerank_n` AND the decision-122/123 `company_exclude`/`filter_staffing_spam`** (those three all change the matched/judged set but weren't invalidating the cache — a staleness bug fixed here). **Verified:** the `haiku` CLI alias resolves live (senior-staff-ML posting → coarse 15, new-grad-Java → 78 — correct discrimination); end-to-end fresh two-stage run at `prerank_n=50, top_n=20` (user config, git-ignored) → **cleared 3** (was 2 at top_n=10), the new one being *Ramp — Software Engineer, Onboarding* (coarse 80) which the keyword top-10 would have missed; the Sonnet judge correctly caught Haiku's over-scores (several coarse-68 senior Stripe roles judged 14-19). **Measured arc 121→124: cleared 0 → 1 → 2 → 3.** **Tests:** 3 new (`test_matching_prerank.py`: prerank picks the best top_n for the full judge; disabled never calls the cheap model; a prerank failure falls back to keyword order + records the note) — Claude CLI stubbed, dispatching on the `model` kwarg, zero tokens. Full suite green (pre-existing `test_parking.py` bot_wall + `test_nav_recipes` failures unrelated). **Still open:** `prerank_n`/`top_n` cost/coverage is now a dial the user can push further; Haiku's coarse scores are noisy (it over-scores some senior roles) but the Sonnet judge is the backstop, so precision is unaffected — only which postings earn a full judge. | Accepted |
| 123 | 2026-07-22 | **Heuristic staffing-spam down-ranking + push the cheap gates into the curated source before it spends its resolve budget.** Follow-through on decision 122's open item: the early-career curated feed, ranked by title-relevance to a Java/Python résumé, is dominated by staffing/body-shop reposts that out-rank real new-grad roles. Two mechanisms: (1) keyword-stuffed **titles** ("Java Developer - Need Locals - Need GC and USC"); (2) **clean-titled** agency dupes (Consultadd/DellFor/USM posting "Python Developer" many times). **Changes — (1) title-tell detector (`filters.py`, code):** `is_staffing_spam(title)` — high-precision, a real employer never titles a role by its work-authorization filter or "corp to corp". Multi-word phrases matched as substrings (`need locals`, `gc and usc`, `corp to corp`, `third party`, `contract to hire`, `multiple positions`, …); a few unambiguous tokens on **word boundaries** (`c2c`, `c2h`, `w2`, `1099`) so `opt` can't match `option`. Weak signals (agency-name guessing) deliberately left to `company_exclude` — auto-dropping on name patterns ("Technologies"/"Systems") would false-positive real companies. New `DiscoveryFilters.filter_staffing_spam: bool = True` toggle (off for a contractor who wants C2C). Applied in `apply_gates` as `gate_spam`. **(2) Cheap gates pushed INTO `CuratedListSource` pre-resolution (`discovery.py`+`filters.py`, code):** the source now applies title_exclude + company_exclude + is_staffing_spam + `(company, normalized-title)` dedup to the raw listings **before** the title-relevance sort + `max_resolve` JD-fetch — previously those gates only ran in `apply_gates` AFTER resolution, so a repost burned a scarce JD-fetch slot and pushed a real role past the `max_resolve` cutoff. `build_sources` passes the filter's title/company excludes + spam toggle down; the class takes them as ctor args (primitives, not the `DiscoveryFilters` object — no import cycle; `is_staffing_spam`/`_norm_title` lazily imported in `fetch` since `filters` imports `discovery`). Funnel gains a `staffing spam` row. **Measured (fresh Claude-judged runs across the 121→122→123 arc): cleared applications 0 → 1 → 2** at min_fit 60; the curated resolve set went from Instructor + Consultadd/DellFor×3-4 dupes to real roles (Canonical "Graduate Level Python Cloud", Buyers Edge "Junior Developer Python & Go", Vestmark "Associate Java SWE"). Seeded the user's git-ignored `company_exclude` with the observed clean-titled body-shops (USM/Testing Xperts/Career Guidant/SA Technologies/HSSSoft/Proximate/Atria) — NOT committed. **Still open (the real throughput lever, not spam):** `top_n=10` — the judged pool is dominated by the direct ATS boards (Stripe/Ramp/cin7/Hartford/Motorola), so real curated roles rank 11+ and never get judged. Raising `top_n` (paired with a cheaper Haiku pre-rank so it doesn't just cost more) is what turns "2 cleared" into "many" — queued in NEXT_STEPS. **Tests:** 2 new (`is_staffing_spam` flags body-shop tells only + `opt`-in-`option` guard; `gate_spam` drops + toggles off). Full suite green (pre-existing `test_parking.py` bot_wall + `test_nav_recipes` failures unrelated). | Accepted |
| 122 | 2026-07-22 | **Fix the "nothing to re-prepare" lie + widen early-career match quality: honest loop messages, `company_exclude` gate, repost dedup, and résumé-noise removal in the aggregator queries.** User hit *"Nothing recently scored to re-prepare. Start a normal auto-apply loop first…"* on the loop. **Diagnosed (not an empty cache):** the discovery snapshot was fresh with **100 matches**, but only 10 were Claude-judged (`top_n`) and the **best fit was 41 vs `min_fit` 60**, so `cleared_queue` returned 0 and the rescan bailed with a message that **claimed nothing was scored** — sending the user to re-run a loop that can't help when the real problem is the threshold. Root cause of the low fit: the 10 judged slots were spent on noise. (a) Adzuna/Google/remote queries are `derive_keywords`-built from the résumé's top-2 **job titles** + skills → `Junior Software Engineer, Instructor, Java` (the user's real *Instructor* experience leaked a teaching query; `Java` is a bare-language term). (b) The curated early-career feed ranks by **title-relevance to the résumé**, so `Java & Python Instructor` ranked **#1** and staffing-agency reposts (Consultadd/DellFor/USM/…, keyword-stuffed "Java Developer" titles) flooded the top-K, each posted many times under **distinct URLs**. **Changes — (1) honest messages (`web.py`, code):** the rescan bail now distinguishes an *empty* cache from a *full cache where nothing clears `min_fit`*, and both the rescan and normal `caught_up` paths report the real reason + fix — *"N match(es) are cached, but the best fit Claude scored was 41, below your min_fit of 60. Lower min_fit in Discovery settings (try 41), or broaden your boards."* A `best_seen` fit tracker + `_below_bar_msg` back it (UI Principle #3 / Guideline #11). **(2) `company_exclude` gate (`filters.py`, code):** new `DiscoveryFilters.company_exclude` — drop postings whose COMPANY contains any listed substring (staffing agencies), mirrored on `title_exclude`, counted as `gate_company` in the funnel. **(3) Repost dedup (`filters.py`, code):** `apply_gates` now collapses near-identical reposts keyed on `(company, _norm_title(title))` (normalized: lowercase, non-alphanumerics→space) — keyed on TEXT not URL precisely because the dups carry distinct apply URLs; counted as `gate_duplicate`. Both wired into `pipeline._print_funnel`. Trade-off accepted: `(company,title)` dedup can rarely merge two genuinely-distinct same-title roles at one company, but freeing a scarce judged slot from an obvious repost is the better default for a `top_n`-bound funnel; the one existing staleness test that used identical `(Acme, SWE)` fixtures was given distinct titles so it isolates the staleness gate. **(4) Board config (user's git-ignored `profile/discovery.yaml`, local — NOT committed):** explicit `keywords` (`Junior/Entry Level/New Grad/Associate Software Engineer`), `title_exclude += instructor/teacher/teaching/tutor`, `company_exclude: Consultadd/DellFor/KRG Technologies/Direct Staffing`. **Measured end-to-end (fresh Claude-judged run, user-approved):** best fit **41 → 72** — *The Hartford — Associate Software Engineer - Java* now scores 72 and **clears min_fit 60** (was 0 cleared); funnel drops `gate_title −92, gate_company −11, gate_duplicate −20`. **Still open (deeper, not config):** the curated feed is saturated with staffing spam whose keyword-stuffed titles out-rank real new-grad roles for a Java/Python résumé; `company_exclude` is whack-a-mole. Structural fix = heuristic spam down-ranking (tells: "Need Locals", "GC and USC", "C2C", "W2", agency-name patterns) and/or raising `top_n`. **Tests:** 4 new (`company_exclude` drop, repost dedup with distinct URLs, + the 2 below-bar/goal message tests from 121's file); staleness fixture updated. Full suite green (pre-existing `test_parking.py` bot_wall + `test_nav_recipes.py` failures unrelated). | Accepted |
| 121 | 2026-07-22 | **Goal mode for the web auto-apply loop: prepare until N applications are ready to review/submit, with a Stop-vs-Maintain toggle.** The user asked for a mode that "loops discovery and applications until there are at least X applications ready for review and actual submission." Mapped "ready for review and actual submission" onto the concept that already exists — the decision-069 **Ready-to-apply queue** (`_LOOP_STATE["ready_ids"]`: clean **dry-run** fills awaiting the user's one-click Apply ▶; a *blocked*/parked fill never counts as ready). So goal mode is not a new pipeline — it's a **stop condition** on the existing loop. **Home:** the web app loop (user-picked over the CLI runner or both), because that loop already produces exactly the reviewable Ready queue and is where the user reviews + submits. **On reaching N:** the user asked to **toggle** between two behaviours, so both ship: *Stop* (reach N clean-ready, end the loop) and *Maintain N* (hold at N — as the user applies to ready ones and the count drops, resume discovering + preparing to top the pool back up; ends only on Stop or board exhaustion). **Implementation, keeping the pure core pure:** `autoloop.auto_apply_loop` gained four optional injected params — `ready_count()`, `goal`, `maintain`, `wait()` — and a new `"goal_reached"` return. Before each prepare (and at the top of each round) it checks `ready_count() >= goal`; in Stop mode that returns `goal_reached`, in Maintain mode it idles via `wait()` (a **stop-responsive** sleep) and re-checks. `goal=None` makes every goal check inert, so the pre-goal behaviour is **byte-identical** (the runner and all existing callers are untouched). **Web glue (`web.py`):** `_loop_worker`/`start_loop`/`/loop/start` accept `goal`+`maintain`; the worker supplies `ready_count=len(ready_ids)` and `wait=lambda: _LOOP_STOP.wait(2.0)` (so Stop ends a Maintain idle within 2s), stores `goal`/`maintain` in `_LOOP_STATE` for the status payload, adds a `holding` phase message for Maintain and a `goal_reached` finish message, and honestly reports when Maintain caught up **below** the goal (boards had no more new matches). A non-positive goal is coerced to "no target"; Maintain is forced off with no goal. **UI (Discover tab loop panel):** a number input ("Goal: stop when N application(s) are ready to apply", blank = ∞ = prepare everything) + a "Keep topping up to the goal" toggle (live-gated on the goal being set, since it's meaningless without one); the Ready-to-apply header shows progress ("Ready to apply (2 of 5 goal[, topping up])"). Token cost is unchanged per posting — goal mode only bounds **how many** get prepared, and discovery stays `only_new` (no re-judging). **Verified:** 5 new core tests (`test_autoloop.py`: stop-at-N before any prepare, stop-at-N mid-batch, `goal=None` inert/backward-compat, Maintain idle-until-Stop, Maintain refill-after-a-submit-drop) + 2 new web tests (`test_autoloop_web.py`: `goal=1` stops after one prepare with phase `goal_reached`; `goal=0`/negative+maintain coerced to no-target/off). Full suite **396 passed** (2 pre-existing `test_parking.py` bot_wall failures + the `test_nav_recipes.py` collection error are the standing `_bot_wall_evidence`-missing failures, unrelated); served JS `node --check` clean. | Accepted |
| 120 | 2026-07-22 | **Adzuna auto-apply: click its "Apply for this job" gate in the real browser at apply time — DON'T resolve it server-side (impossible + would mean evading a WAF, Guideline #4).** The user flagged that an Adzuna posting doesn't link straight to the employer form — you must first click "Apply for this job" on Adzuna's own page. First plan was to parse that gate's HTML in the bridge and rewrite `apply_url` to the real ATS. **Live investigation killed that plan:** the Adzuna API (`api.adzuna.com`, keyed) is reachable, but the apply gate lives on `www.adzuna.com/land/ad/…`, which is fronted by a **CloudFront WAF that 403s "Access Denied"** to every automated client tested from this environment — `urllib` (bot UA *and* full browser headers + Referer), **real headless Chromium**, and even **headed Chromium with a warmed cookie session**; the adzuna.com **homepage itself** 403s here (`X-Cache: Error from cloudfront`), i.e. the block is **IP/edge-level**, not UA-level. So there is no HTML to parse without defeating active bot detection (TLS-fingerprint/impersonation) — prohibited by Guideline #4. `resolve_redirect` also can't help: the land URL isn't an HTTP 30x, so it returns unchanged and `detect_ats_from_url`→"other", which is why Adzuna hits were being marked `auto_applyable=False` and **silently dropped before Apply** ever saw them. **Chosen approach (user picked "click it in the real browser at apply time"):** the Apply stage already drives real Chromium (Playwright) and already reveals click-gated forms via `_open_application_form`'s `\bapply\b` reveal regex (decision 076) — "Apply for this job" already matches it, and `_ats_from_frame` re-derives the true ATS after the click. So: (1) **`discovery.py` bridge** — new `_BROWSER_GATED_ATS={"adzuna"}`; a pre-pass tags these `extra['bridged_from']`+`extra['browser_gated']=True` and **skips the (WAF-blocked) HTTP resolve**, leaving `ats`/`apply_url` as the land URL; they're excluded from the network-resolve `to_bridge` list so they don't consume the polite `_BRIDGE_MAX` budget or make a doomed request. `auto_applyable` is left **unset** (we have no dedicated adapter — the ATS is unknown until the click) so `_is_fillable` keeps them in the funnel via `ats in _AGGREGATOR_ATS`. (2) **`apply.py`** — `_open_application_form` gains an up-front `_is_adzuna_access_wall(page)` check (host contains adzuna.com **and** title is "Access Denied") that **fails fast** (0.0s vs the full 25s form-load timeout) with an actionable error naming the WAF block + the fix ("open the posting in your own browser… or retry from a residential network", UI Principle #3 / Guideline #11) — so a *user* whose IP is also blocked sees the real reason, not a vague timeout. **Verified:** live Adzuna API call → land URLs; confirmed 403 across urllib/headless/headed/warmed + homepage (IP-level); bridge unit-checked (Adzuna → `browser_gated`, ats/apply_url unchanged, `_is_fillable`=True; Himalayas untouched); fail-fast path tested against a **live** access-wall page (0.00s, exact error string); `test_open_application_form` + `test_fillability` (9) and `test_fillability`+`test_autoloop_web` (13) pass; pre-existing `test_nav_recipes.py` collection error (`_bot_wall_evidence`) confirmed unrelated (present with changes stashed). **NOT verifiable from this environment (IP is WAF-blocked):** the happy path — land page loads → click "Apply for this job" → reach the real ATS → fill. Must be validated on the user's residential machine (see NEXT_STEPS). | Accepted |
| 119 | 2026-07-22 | **"Test aggregators" button in Discovery settings — a live per-source probe — and made the Himalayas/RemoteOK toggles editable in the form (closing a decision-117 save-wipe gap).** Aggregators need keys (Adzuna), can be non-functional (Google Jobs, decision 116), and can silently return nothing, so users need to verify one works before a full run. New `POST /aggregators/test` → `test_aggregators(data)` in `web.py`: builds ONLY the aggregator sources (excludes per-company ATS boards) from the editor's current values **merged over** the saved config (so aggregators the form doesn't expose — e.g. Google — keep their saved settings and are still probed), clamps the expensive breadth knobs (`adzuna.max_pages/max_queries→1`, `google.max_queries→1/results_wanted→10`, `remote_boards.max_results→25`, `early_career.max_resolve→5`) so the probe returns in seconds, then calls each source's `fetch()` and reports per source `{name, ok, count, sample}` or the exact `DiscoveryError`/unexpected-error string. UI (in the "Broad aggregators" lead section): a **Test aggregators** button + `testAggregators()` that posts `collectDisc()`, shows the shared spinner + live elapsed (UI Principle #5), and renders one ✓/✗ row per source — green/red dot, source name, and either "N postings in a quick sample — e.g. '…'" / "reachable, but 0 postings…" or the exact error (UI Principle #3); empty result → "No aggregators configured to test. Add an Adzuna key, or enable early-career feeds / Himalayas / RemoteOK". **Also fixed a latent data-loss bug:** `collectDisc()` (posted by BOTH Save and Test) omitted `remote_boards`, so saving the Discovery form silently reset decision-117's Himalayas/RemoteOK toggles to off — they were only settable by hand-editing `discovery.yaml`. Added a "Remote aggregators (keyless)" section (Himalayas + RemoteOK checkboxes + `max_results`) to the form and `remote_boards` to `collectDisc`, so they're now editable, testable, and save-safe. Google Jobs is deliberately NOT surfaced in the form (documented non-functional, off by design; the merge still lets a hand-set `google.enabled` be probed). **Read-only consistency:** the "Where your postings come from" panel (`GET /sources` + `loadSources`) now also reports a **Remote aggregators (keyless)** row ("on — Himalayas, RemoteOK" / "off") and, only when hand-enabled, a **Google Jobs** row stating it's currently non-functional — so every configurable source appears in both the editor and the read-only overview. **Verified via live probes:** Himalayas returned 20 postings, RemoteOK 25, early-career 5 (all with sample titles); Adzuna reachable/0 under the clamp; Google surfaced its exact non-functional error; a bad `max_pages` returned a readable pydantic validation error; Playwright screenshots confirmed the ✓ rows render and Save stays pinned (see decision 118) | Accepted |
| 118 | 2026-07-22 | **Discovery-settings UI: renamed "Target boards" → "Target companies", reordered the settings form so broad aggregators lead and the company list follows, and pinned "Save settings" to a non-scrolling modal footer.** User feedback: the "Target boards" label read like a general job board (Indeed/LinkedIn) when each entry is really *one company's* public ATS board (`greenhouse:stripe` = Stripe's board), so users read the field as "restrict a board to one company." Relabeled to **Target companies** everywhere user-facing (section header, "+ Add company", row placeholder/remove title, the read-only "Where your postings come from" overview row and its intro). Reordered `renderDiscForm` so the **broad aggregators** (a new lead intro + Adzuna + early-career feeds) render at the **top** — they search across many companies and are the biggest lever on how much discovery finds — with the specific **Target companies** list, then Filters, then Matching below. Pinned the save button: the Discovery-settings modal is now a flex column capped at `calc(100vh - 88px)` with an internally-scrolling `.modal-body` and a new non-scrolling `.modal-foot` holding "Save settings" + status (the same shared save affordance, always in reach — no scroll-to-bottom). **Deliberately UI-only:** the underlying `boards` YAML key and the `Board(ats, token)` model are unchanged, so existing `profile/discovery.yaml` files keep working (Guideline #7). Two backend guidance messages that said "No target boards" / "Discovering from your target boards" were reworded to "discovery sources" / "your sources" since discovery also draws from aggregators. **Verified:** `web.py` parses; server boots and serves the page with `modal-foot`, "Broad aggregators", "Target companies", "+ Add company" present and "Target boards"/"+ Add board" gone; `/discovery` still returns `filters.boards`; `/sources` returns its keys unchanged | Accepted |
| 117 | 2026-07-22 | **Added two keyless remote-job aggregators — Himalayas + RemoteOK — as opt-in discovery sources (no signup, no scraping, no JS).** After decision 116 found Google Jobs unreadable keyless, the user chose "keyless JSON aggregators" over a Playwright render path. Of decision 114's roadmap, Himalayas and RemoteOK are the only truly keyless ones (USAJobs + Findwork need API keys — deferred to a future opt-in-with-key source). Both live-verified: **Himalayas** `GET himalayas.app/jobs/api?limit&offset` → `{jobs:[…]}` with title/companyName/description/min-maxSalary/seniority/`applicationLink` (remote by construction; salary **annualized** via new `_annualize()` so a monthly figure can't misread as an annual one and trip the salary gate). **RemoteOK** `GET remoteok.com/api?tag={skill}` → JSON list whose element 0 is a legal/attribution notice (skipped); filtered by profile-derived **single-word skill tags** (multi-word role titles aren't valid RemoteOK tags), results merged+deduped by id; their ToS requires a backlink, so the posting `url` is the remoteok.com listing (which IS that backlink — Guideline #4). New `HimalayasSource`/`RemoteOKSource` in `discovery.py`, both tagged as aggregators (added to `_AGGREGATOR_ATS`) so they ride the existing bridge/fillability path like Adzuna. New `RemoteBoardsConfig(himalayas=False, remoteok=False, max_results=100)` on `DiscoveryFilters`; wired into `build_sources` (Himalayas needs no query; RemoteOK derives up to 4 single-word tags). Opt-in/off by default, consistent with every other source. **Verified:** imports clean; wiring test (both self-skip when off, wire in with derived tags when on); **live fetch of both** returned valid Postings (Himalayas: real roles + `USD 190,000-210,000` annualized salary + remote=True; RemoteOK: rich descriptions, deduped); 101 discovery/pipeline/salary/match/funnel tests pass | Accepted |
| 116 | 2026-07-22 | **Discovery fine-tuning: expanded Adzuna to multi-query server-side search (works), and vendored JobSpy's Google Jobs scraper (found non-functional — Google moved Jobs behind client-side JS).** Continuing decision 114/115's "widen the funnel" with the user's pick: *expand Adzuna AND vendor the Google scraper* (not `pip install python-jobspy` — that ships pandas + the proxied LinkedIn/Indeed/Glassdoor scrapers into the app bundle, the bot-evasion stack Guideline #4 says not to ship; the user chose "Google Jobs only, no proxies"). **Adzuna (delivered, tested):** was one broad `what_or` blob, `max_pages=1` (50 results), no recency/sort — confirmed by live research to badly underuse the richest search API we have. Now `AdzunaSource` runs each profile-derived term as its OWN focused `what` query and merges+dedups by redirect_url (breadth + per-term relevance), pushes recency server-side via `max_days_old` (from the `max_posting_age_days` gate) and `sort_by=date`, and defaults raised to `max_pages=3` + `max_queries=4`. New `AdzunaConfig` fields `max_queries`/`sort_by`. **Google Jobs (vendored but non-functional):** adapted only the ~120-line Google slice of JobSpy (MIT, attributed) into a new isolated `applicationbot/google_jobs.py` — proxy-free, no pandas, one honest UA, backs off on 429, returns plain dicts — wrapped as `GoogleJobsSource` (tagged `ats="google"`, added to `_AGGREGATOR_ATS` so it rides the existing aggregator bridge/fillability path like Adzuna) behind an opt-in `GoogleJobsConfig(enabled=False)`. **Live test finding (Guideline #11):** a real keyless GET of `google.com/search?udm=8` — with both trimmed and JobSpy's full fingerprint headers — returns a 91 KB **JavaScript-only shell with zero embedded job data** (no `520084652` arrays, no `Yust4d`/`data-async-fc` cursor). Google renders the Jobs vertical client-side now, so the keyless/proxy-free scraper (and by extension JobSpy's `requests` path) **cannot read current Google Jobs**. Made it fail LOUDLY (raises `GoogleJobsError` → surfaced as a source error) rather than silently return "0 jobs" (UI Principle #3/#5), documented NON-FUNCTIONAL in the config, and left it opt-in/off so it never affects a default run. A working Google path would need a headless-browser render — **Playwright is already a project dependency (Apply stage)**, so that's the viable fix if the user wants it; the alternative is pivoting to keyless JSON aggregators that don't need JS (Himalayas/RemoteOK/USAJobs/Findwork, decision 114 roadmap). **Verified:** Adzuna multi-query/dedup/params unit-tested through `build_aggregator` (4 focused queries, `max_days_old`/`sort_by` on the URL, `what` not `what_or`, dedup); `google_jobs` parser/query-builder/Source-mapping unit-tested; live Google call confirmed the JS-shell finding and the loud failure; `GoogleJobsSource` self-skips when disabled and wires in (profile-derived queries) when enabled; 101 discovery/pipeline/salary/match/funnel tests pass | Accepted |
| 115 | 2026-07-22 | **Diagnose-first: a per-stage discovery→match funnel breakdown, before changing any thresholds.** Executing decision 114's step (1) "widen the funnel first" — but the user's explicit call was to **diagnose with logging first**, so we can see *where* their postings actually die before touching thresholds. Today the funnel line collapses five coarse gates into one `after_gates` number and never reports the keyword-pre-filter drop, so "lots found, few through" can't be attributed to a stage. Added: `apply_gates(postings, filters, stats=None)` optional out-param that records a per-gate drop count (`gate_remote`/`gate_title`/`gate_level`/`gate_salary`/`gate_stale`) — signature back-compatible, all three callers unchanged. `pipeline.discover_and_match` now builds a `funnel` dict counting survivors after each stage (discovered → per-gate drops → after_gates → skip-seen → fillability → keyword min_skills → top_n judged) and attaches it to `PipelineResult.funnel` (empty on cache hits, which don't recompute stages). New `pipeline._print_funnel()` renders the vertical breakdown in the CLI preview and the autonomous runner; `web.py` carries `funnel` (incl. `min_skills`/`top_n` for labels) in the scan state and a collapsible **visual bar funnel** `renderScanFunnel()` under the dry-run summary — one horizontal bar per stage (width ∝ postings still alive) narrowing Discovered → gates → skip-seen → fillability → keyword floor → judged → cleared-min-fit, each narrowing annotated with "−N + reason" (per UI Principle #5 — never hide that work was skipped/truncated). **Bug fixed same session:** the first cut named the function `renderFunnel`, colliding with the tracker's pre-existing `renderFunnel(funnel)` (decision 108 KPI tiles) — JS hoisting made the scan call hit the tracker function and render the literal string "undefined" under the summary; renamed to `renderScanFunnel` and upgraded from a text list to the bar chart the user asked for. This confirmed the two biggest silent losses on a synthetic 240-posting run: the **fillability gate** (Workday/iCIMS dropped pre-judge) and the **top_n cap** (matched-but-never-judged). Diagnostic only — **no gate thresholds changed** this step; the fillability-gate removal, keyword-softening, and top_n lift the user also approved are the next changes, now measurable against this baseline. **Verified**: synthetic pipeline run through the real `discover_and_match` produces correct per-stage counts (title_exclude, fillability, keyword all attributed); `_print_funnel` renders; `renderScanFunnel` JS rendered in node on the user's real numbers (688→6, no "undefined") and on a synthetic run; served-page client JS passes `node --check`; page serves 200; 85 pipeline/funnel/gate/discovery/match/web tests pass (pre-existing `test_nav_recipes.py` collection error unrelated) | Accepted |
| 114 | 2026-07-22 | **Discovery-expansion strategy: widen the funnel FIRST, then wire in more FREE sources; big consumer boards stay excluded.** Research (2026-07-22, four+ agents, live-tested endpoints) into APIs/boards/crawling to broaden discovery. Findings + turnkey source catalog recorded in NEXT_STEPS ("Discovery source expansion — research 2026-07-22"). Key reframe (confirms decision 073): the binding constraint today is **funnel throughput, not source count** — built-in feeds already carry ~2.2k fillable postings but only `early_career.max_resolve=40` are resolved+judged per run, so the pool is ~50x oversubscribed and adding sources yields ~zero real gain until the funnel widens. User's call: **(1) widen the funnel first** (raise/auto-tune `max_resolve`, cheapen/batch the Claude judge, surface "40 of N judged" per UI Principle #5), **then (2) wire in new sources**, **(3) free sources only** (no paid JSearch/SerpApi tiers). Roadmap, free-only: Tier-1 new public keyless ATS JSON in the lane we already trust — **Workday CxS** (biggest enterprise coverage; complete as a discovery source), **BambooHR**, **Rippling**, **Breezy** (all live-verified public JSON), **Personio**/**Teamtailor** (XML/RSS only — their JSON is token-gated); Tier-2 free aggregators with real apply links — **JSearch** free tier, **USAJobs** (federal; discovery/track-only, routes to non-autofillable gov portals), **Himalayas**/**RemoteOK**/**Findwork** (remote, direct apply), **HN "Who is hiring"** via Algolia (unstructured, NLP-parse). The **durable scaling lever** is a free-built **`company → {ats, token, cluster, site}` table** (Common Crawl grep + career-page fingerprinting + dorks/crt.sh/OSS seed lists, live-validated + re-validated on a schedule) — there is **no public tenant directory** for any ATS, so this table (not any single fetcher) is what unlocks reach beyond the handful of hand-listed boards. **Excluded (unchanged from 030/032):** LinkedIn/Indeed/Glassdoor/ZipRecruiter/Wellfound/Monster — public APIs are dead or partner-gated (Indeed Publisher retired ~2024, Glassdoor closed 2021, LinkedIn Jobs API is posting-only); scraping public pages survives CFAA (hiQ/Van Buren) but breach-of-contract is a live, winning claim (hiQ paid LinkedIn $500k) and defeating DataDome/Cloudflare violates Guideline #4. Research only — no code written this session | Accepted |
| 113 | 2026-07-22 | **Tracker page: drop the table-era description blurb, and surface a blocked application's reason on its feed card.** The `<p class="page-sub">` under "Application tracker" described table-only behaviors (inline cell edit, column resize/hide) that no longer apply now the default view is the card feed — removed. For blocked applications, the card now shows a short "what blocked" line (⚠ + the parking label, e.g. "Needs your answers" / "A CAPTCHA is in the way"), with the stored `blocked_detail` specifics as its hover tooltip. Single source of truth: the label/detail come from `parking.describe(blocked_kind, blocked_detail)` — the same call that drives the Resolve cards — attached server-side in `/track` as `app.blocker`/`app.blocker_detail` only when `status == "blocked"` and a `blocked_kind` is set. New `.fc-blocker` CSS uses `--warn`. Also reworded the discovery/judging token-spend caption: confirmed the tracking is entirely local (all `usage_events` reads come from the git-ignored local `applications.db` / `DEFAULT_DB` — no cross-user or external aggregation), and the old "shared across candidates" wording read as if spend were pooled across multiple people; now "Your Claude spend on discovery & judging (all-time), tracked locally on this machine — it covers finding and scoring postings, so it isn't charged to any one application." | Accepted |
| 112 | 2026-07-21 | **Branch & release model: `master` (main) is the source of truth for releases; `development` is the working branch. New releases push to main; day-to-day work happens on `development`.** Made explicit while cutting v0.1.0. The repo's established pattern (GitHub PRs #1–5 are all "Merge pull request from development") is: commit work on **`development`**, then bring it onto **`master`** to release — either a GitHub PR `development → master` or, when the gh CLI is authed as a non-owner account, an equivalent local `git merge --no-ff development` on master pushed over SSH (the outcome is the same "Merge development into master" commit). Releases are cut from `master` with `scripts/release.sh --publish`, which tags `v{applicationbot.__version__}` at HEAD, pushes the tag, and creates the GitHub Release (source zip) — and the built **`ApplicationBot.app.zip`** is attached as a release asset (`gh release upload`). Rules that follow: never develop directly on `master`; bump `applicationbot.__version__` before each `--publish` (the script refuses to reuse an existing tag); the release artifact is GitHub's auto source zip **plus** the self-contained `.app.zip` (rebuild it with `scripts/build_macapp.sh` — which bundles all runtime deps incl. `anthropic` — before uploading). Documented in CLAUDE.md ("Branching & releases") so future agents follow it. **Applied**: v0.1.0 tagged at `master` and released; `.app.zip` attached | Accepted |
| 111 | 2026-07-21 | **Claude connection is a hybrid: the Claude subscription (via Claude Code) is PRIMARY, a keychain-stored Anthropic API key is the FALLBACK — because a third-party app cannot use a Claude subscription any other way.** User wanted ApplicationBot to have its *own* Claude connection — sign in / log out / "which account" in the bottom-left panel, "separate from the Claude Code CLI." Investigated whether a separate OAuth (`ant auth login`) could drive tailoring on the user's **subscription** and reported back (user asked to "verify OAuth billing first"). **Finding (with sources):** Anthropic's [auth docs](https://platform.claude.com/docs/en/manage-claude/authentication) list only **API keys** and **Workload Identity Federation** for the Messages API — subscription OAuth is not an API auth method; the Messages API rejects OAuth tokens (*"OAuth authentication is currently not supported"* / *"This credential is only authorized for use with Claude Code"*); and Anthropic **officially banned** subscription OAuth (Free/Pro/Max) in any third-party product/tool/SDK in Feb 2026 (a Consumer-ToS violation, enforced from 2026-01-09). So a "log in with Claude" that bills the subscription is **impossible and prohibited** for this app (Guideline #4) — the *only* sanctioned subscription path is shelling out to the real `claude` binary (Claude Code itself), which is what the app already does. User chose the hybrid with **subscription primary, API key fallback**, and to state it explicitly in the README and walkthrough. **Implementation (DOM/CSS/JS + Python, no subscription-OAuth anywhere):** (1) `auth.py` gains a keychain-backed fallback-key store (`get/set/clear/api_key_masked`, service `applicationbot-anthropic-api` — never YAML/git, like the Workday/Gmail secrets) and a richer `status()` (`claude_code`, `api_key_set`, `api_key_masked`, resolved `engine`). (2) `backends.py`: new `run_anthropic_api()` + `AnthropicAPIBackend` (metered Anthropic SDK with the user's key; same `ClaudeAuthError`/`ClaudeRateLimitError`/`ClaudeUnavailableError` taxonomy; usage recorded via the decision-095 envelope shape; thinking left off — structured-JSON task); `select_backend("auto")` now resolves **Claude Code → API key → rules**; `anthropic>=0.40` added to `requirements.txt` (was intentionally absent) and `--collect-all anthropic` to the mac build. (3) `web.py`: `POST /auth/apikey` validates the key with a free `models.list()` call **before** storing it (never saves a key that doesn't work) and `POST /auth/apikey/disconnect` clears it; the bottom-left `#account` box became a **button** opening a "Claude connection" modal — **Claude subscription [Primary]** (✓ via Claude Code / install-and-`/login` hint, with a note on *why* sign-in isn't in-app) over **Anthropic API key [Fallback]** (masked `…{last4}` + Disconnect, or a `sk-ant-…` input + Connect; copy: metered, pay-per-token, separate from the subscription, keychain-stored); the Review engine dropdown gained an `anthropic-api` option and the `auto` label now reads "subscription → API key → rules". The old "no `anthropic` dependency" claims in `backends.py`/`requirements.txt` were corrected. **Verified**: `import` clean; stubbed `select_backend("auto")` returns ClaudeCodeBackend / AnthropicAPIBackend / RulesBackend across the three states; a real bogus-key call maps 401 → `ClaudeAuthError`; `POST /auth/apikey` with a bogus key returns `ok:false` "rejected (401)" (and does **not** store it); disconnect + status return the right shape; Playwright (dark) shows the panel + modal with **no console errors**. Docs updated: README "Tailoring engines" (subscription-primary/API-fallback table + why-no-OAuth paragraph) and the first-run walkthrough gained a "How Claude tailors your résumé" step, both stating the priority explicitly (user request) | Accepted |
| 110 | 2026-07-21 | **The desktop app widens its own `PATH` at startup so it can find the user's `claude` CLI — a GUI-launched `.app` inherits a minimal PATH that omits `~/.local/bin` etc., which made Claude Code read as "not found" in the native app even when it works in a terminal.** User asked, while verifying the reinstalled app, whether Claude OAuth is fresh per user — which surfaced the real mechanism: ApplicationBot stores **no** Claude credentials; it shells out to the locally-installed **`claude` CLI** (Claude Code), whose availability is `shutil.which("claude")` ([backends.py](applicationbot/backends.py)) and whose auth lives in Claude Code's own store (per-machine, per-user — each user must install Claude Code and `/login`; nothing is baked into the repo or bundle). The bug: a Finder/launchd-launched `.app` gets PATH ≈ `/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` (from `/etc/paths`), which excludes `~/.local/bin` (where the official claude installer puts the CLI — verified: `/Users/…/.local/bin/claude`), Homebrew, npm-global, and nvm. So `shutil.which("claude")` returned `None` in the frozen app → it silently fell back to the no-account `rules` engine, while the browser build (launched from a terminal with the full PATH) showed "Claude ready". Fix: new `_augment_path()` in [app.py](applicationbot/app.py), called first thing in `run()` — appends the well-known user tool dirs that exist (`~/.local/bin`, `/opt/homebrew/{bin,sbin}`, `/usr/local/{bin,sbin}`, `~/bin`, `~/.npm-global/bin`, `~/.nvm/versions/node/*/bin`), and *only if `claude` is still unresolved* falls back to adopting the login shell's `$PATH` (`$SHELL -lc`, 5s timeout) for exotic setups (asdf/custom prefixes) — so the common case pays no shell-spawn cost. Additions are **appended** (a user binary never shadows a system one), and it's a no-op from a terminal. In the frozen app the web server runs **in-thread**, so mutating `os.environ["PATH"]` once at startup is inherited by every downstream `shutil.which` and the `run_claude_cli` subprocess `env`. The `.app` does **not** bundle `claude` (0 binaries) — reliance on the user's own Claude Code is intended (subscription billing, decision 011). **Verified**: unit-simulated the minimal launchd PATH from source — `shutil.which("claude")` `None` → `/Users/…/.local/bin/claude` after `_augment_path()` (fast static path, no shell spawn); rebuilt + reinstalled + relaunched the `.app`, and its own `desktop.log` now logs `PATH prepared for CLI resolution; claude found` (was effectively "NOT found" pre-fix). No credential handling changed — PATH resolution only | Accepted |
| 109 | 2026-07-21 | **Track tab rebuilt as an infra-console dashboard (hero scorecards · card feed · sliding context drawer · unified status system) and the Discover page decluttered (aligned headers · concise toggles · 2-col info · settings in a modal).** User gave a detailed spec citing Apify/Bright Data/Railway/Zeabur: an embedded terminal, tight `•`-metadata strings, a three-panel split view (feed → click → right drawer), a muted color-coded status system, exactly 3–4 hero scorecards, and a Discover cleanup (full width, buttons aligned with labels, no walls of text, settings as a popup). Two architecture forks were put to the user (AskUserQuestion): they chose **drawer-within-current-tabs** (keep Review/Discover/Profile/Track — don't restructure the nav/shell) and **existing run history as the drawer's terminal log** (live per-application streaming deferred to a future feature). Loaded the `dataviz` skill (stat-tile/KPI-row contract) before building the scorecards. **Track (`web.py`, DOM + CSS + JS only, no server change):** (1) **Four hero scorecards** at the top (`renderScores` → `.scorecard`: Total processed / Applied [applied+responded+interview+offer] / Blocked–needs-you / Failed) — identical border, big proportional value, muted label. (2) **Unified status system** (`statusMeta` + `.stbadge` dot+label, shared colour map with the table's `st-*` cells): neutral=pending (discovered/tailored/dry-run/rejected/no-response), blue=applied, green=responded/interview/offer, amber(+pulse)=blocked, red=failed — the muted "no massive green checks" palette; `.stbadge.live` pulses the dot. (3) **Card feed** (`renderFeed`/`feedCard` → `.fcard`): one uniform row per application — company · role, a tight `•`-metadata string (`metaString`, mono: portal • location • N runs), fit, and the status badge — behind a **Feed | Table segmented toggle** (`setTrackView`, remembered in `localStorage`); the full editable spreadsheet is preserved as the Table view (nothing lost). (4) **Sliding context drawer** (`openDrawer`/`closeDrawer` → `.drawer` + scrim): title, `•`-meta, status, action buttons (Open posting ↗ / View résumé ↗ / Re-run ▶ reusing `rerunDry`), and the **run-history terminal** (`loadDrawerRuns` → reusable `.terminal` window: dim `›`-prompt timestamps, colour-coded outcome levels err/warn, résumé path) — ARIA dialog: focus-in, **Tab trapped**, Esc / X / scrim close, focus restored. (5) The funnel + token tiles moved into a collapsed `<details>` ("Pipeline funnel & spend") to keep the top uncluttered. **Discover:** panel headers became `.panel-head` (title left, primary button lined up right); the wall-of-text `.editing` paragraphs cut to 1–2 lines (`.editing.tight`); the two loop options are now **concise switch rows** (bold label + one muted hint) — multi-select level filters stay plain checkboxes; sources-overview + fit-insights sit in a 2-col `.disc-grid` using full width; and **Discovery settings moved into a centered modal** (`#disc-modal`, opened from a gear button in a `.disc-actions` bar or the first-visit nudge — repointed from the old scroll-to-`#disc-settings`; the form keeps its `#disc-form`/`#save-disc` ids so render/save are unchanged). Also fixed a latent CSS bug the terminal styling exposed: `#loop-status.loopstat.hidden` still rendered because `.loopstat{display:flex}` out-specified `.hidden{display:none}` at equal specificity — added `.loopstat.hidden{display:none}`. **Verified**: `import applicationbot.web` clean; server 200; Playwright in **light + dark** — scorecards, feed with dotted status badges, drawer with terminal run-log, Feed↔Table toggle (table still fully editable), settings modal, and the decluttered Discover panels all render with **no console/page errors**; `#loop-status` confirmed `display:none` when idle. Docs updated (`UI_UX_GUIDELINES.md`, `NEXT_STEPS.md`) | Accepted |
| 108 | 2026-07-21 | **Two shadcn-refactor follow-ups from decision 107 taken on: the tracker funnel + token spend became bordered metric tiles (KPI row), and the auto-apply loop status + run-history rows became monospace terminal-log streams.** User picked follow-ups #2 (deeper terminal treatment for loop status + run history) and #3 (metric tiles for funnel/token-spend) from the list I surfaced after 107; left the brand logomark alone. Loaded the repo's `dataviz` skill first (it governs stat tiles / KPI rows / meters). **Metric tiles (#3):** `renderFunnel` no longer draws horizontal bar rows (`.fn-row`/`.fn-bar`) — it builds a responsive grid of stat cards (`.mtiles`/`.mtile`), one per funnel stage: uppercase label, the count as a big **proportional** sans value (per the skill: tabular-nums makes display numbers read loose — tabular is reserved for aligned columns/sub-lines), a mono sub-line ("% of top · % conv", conversion in `--ok-text`), and a thin **meter** (`.mtile-meter-fill` accent on an `--accent-weak` track — the lighter step of the same ramp) whose width = count ÷ top-stage, so the funnel's drop-off still reads at a glance. `renderUsageDiscovery` likewise became a 3-tile cluster (Total tokens / Input / Output, compact value + exact grouped sub-line) under a caption, with the per-activity breakdown moved behind a "Show per-activity breakdown ▾" `.linklike` toggle (was: the whole one-line summary was the toggle). New shared `mtile(label,value,sub,meterPct)` helper. The dead `.fn-*` bar CSS was removed (its only consumer was `renderFunnel`); `.funnel-empty` kept. **Terminal streams (#2):** `.loopstat` (loop status) → recessed `--field` background + `--mono` + a colored `›` prompt glyph (`::before`, red on error); the per-posting run-history sub-row (`.runsrow`/`.runsbox`/`.runline`) → `--field` background + `--mono` with a dim `--faint` timestamp, a bold color-coded outcome tag (existing `st-*` classes), and `--muted` detail — one log line per run, the Railway/Apify "live run log" look. Also fixed a latent `color:var(--text)` (an undefined token) on `.runline .rundetail` → `--muted`. No server/data changes and no change to what the funnel/usage/loop/runs endpoints return — DOM structure + CSS only (guideline #7); the run-history expand/collapse and usage-breakdown expand still work, just re-skinned. **Verified**: `import applicationbot.web` clean; live server 200; Playwright in **light + dark** shows the funnel as a 6-tile KPI row with working meters, token spend as 3 tiles + breakdown toggle, the loop status as a prompt+mono stream, and an expanded run-history row as a mono log — **no console/page errors**. Docs updated: `UI_UX_GUIDELINES.md` §9 gained a "Metric tiles (KPI row)" entry, "Metrics as data" clarified (big values proportional, columns tabular), and "Live-run log" extended to loop status + run history; §6 typography note aligned | Accepted |
| 107 | 2026-07-21 | **UI restyled to a shadcn "zinc" aesthetic — near-black monochrome dark mode, crisp borders-lead shape, lucide SVG chrome icons — done at the token layer so the whole app re-themes from `:root`.** User: "refactor the ui to be a more modern, simple look that still feels polished and production ready," citing shadcn/ui, erikuus/good-ui, awesome-css-frameworks, and infra-console dashboards (Apify Console, Bright Data, Zeabur/Railway) for inspiration — live run logs, toggle switches, metric tiles, monochrome terminal streams. Three design forks were put to the user (AskUserQuestion) and chosen: **(1) dark palette = shadcn "zinc" near-black** (not the prior Google-gray `#202124`); **(2) shape = crisp, borders-lead** (tighter radius, shadow reduced to a hairline); **(3) icons = lucide inline SVG set** (not labeled emoji). Because the entire embedded UI in `web.py` is token-driven, the transformation was made primarily in the `:root`/dark token blocks rather than a rewrite. **Tokens (both `:root` light and the two byte-identical dark blocks):** dark → zinc-950 `--bg:#09090b`, zinc-900 `--surface:#18181b`, zinc-800 `--surface-2:#27272a`, recessed `--field:#101013`, borders `--line:#2e2e33`, text `--strong:#fafafa`/`--ink:#ededef`/`--muted:#a1a1aa`; light → `--bg:#fafafa`, white cards, zinc-200 `--line:#e4e4e7`, zinc text `--ink:#18181b`/`--muted:#71717a`. One restrained blue accent both themes: `--accent:#2563eb` (fills, white-on-it 5.17:1), `--accent-text` `#1d4ed8` light / `#60a5fa` dark (links/on-dark text). Semantic families retuned to the same Tailwind-ish scale (`--ok` green-600/500, `--bad` `#c81e1e`/`#f87171`, `--warn` amber). `--radius` 14px→**10px**; inputs/buttons/tabs 10px→**8px**; `--shadow` collapsed from a two-layer elevation to a single hairline (`0 1px 2px rgba(0,0,0,.05)` light / `.4` dark) — structure now reads from 1px borders + spacing, not shadow. New theme-independent `--mono` token (`ui-monospace,…`) for data. **Chrome icons:** nav tabs (square-pen/search/user-round/chart-column), theme toggle (sun/moon), and "Take the tour" (sparkles) are now lucide-style inline SVGs (`stroke="currentColor"`, 24-viewBox, round caps) sized via new `.ic` (17px) / `.btn-ic` (15px) classes, each still paired with a text label (a11y) — this **reverses** the prior "emoji-only, always with a label" chrome rule; status still uses dot+label. **Dashboard components (infra-console references):** on/off options render as **toggle switches** (real `<input type=checkbox>` restyled with `appearance:none` + a sliding `::after` thumb, `.loop-rescan` — JS still reads `.checked`, so behavior is unchanged); metrics use `--mono`+tabular-nums (count pills, funnel figures, fit chart); the dry-run progress panel (`.testprog`) became a recessed monospace **terminal-style run-log** stream on `--field`. Multi-select filter lists (seniority levels) stay plain checkboxes. **Verified**: proposed palette run through the repo's WCAG-AA contrast checker before writing — every text/background pair the user reads passes ≥4.5:1 (the only sub-floor pairs are decorative surface hairlines, exempt, matching shadcn's intentionally low-contrast borders and the app's prior behavior); `import applicationbot.web` clean; live server → 200; Playwright screenshots in **light and dark** across Review/Discover/Track render the new look with **no console/page errors**; toggle switches, lucide icons, and mono metrics confirmed in both themes. Docs updated (`ui.md`, `UI_UX_GUIDELINES.md`) with the new token values, radius/shadow, lucide-icon rule, and the switch/terminal/metrics patterns. Behavior and copy unchanged — tokens, chrome markup, and CSS only (guideline #7) | Accepted |
| 106 | 2026-07-21 | **Brand imagery is theme-matched and embedded by a re-runnable script: dark-background logo tile in dark mode, light-background tile in light mode, and the blue app icon always as the tab favicon.** User: "when using dark mode use the dark logo, light logo in light mode; always use the blue app icon for the tab favicon," and dropped two new ChatGPT-generated tiles into `assets/`. Before this, `web.py` swapped two **transparent** logomarks (`logomark-{dark,light}.png`) by *contrast* — white cutout in dark mode, navy cutout in light — so neither had a background; the request is for logos **with a mode-matched background tile** (a mini app badge), which we didn't have. Renamed the dropped files to `assets/logo-lightmode.png` (light-bg tile, navy page — for light mode) and `assets/logo-darkmode.png` (dark-bg tile, white page — for dark mode). Rewired the `.brand-logo` CSS var: `--lm-lightmode`/`--lm-darkmode` (replacing the confusingly-named `--lm-dark`/`--lm-light`), with `:root[data-theme="dark"]`/`prefers-color-scheme:dark` → `--lm-darkmode` and `[data-theme="light"]` → `--lm-lightmode`. Favicon `<link rel="icon">` now carries `assets/app-icon-blue.png`. Per Guideline #8 (no one-off manual steps) the base64 is **not** hand-pasted: new `scripts/embed_brand_assets.py` (PIL) resizes each source PNG (logos 96px, favicon 64px), base64-encodes, and regex-injects into `web.py` — anchored on the `/* Brand logomark */` comment block and the icon `<link>` so it's idempotent (re-run after swapping any PNG). **Verified**: script ran, `web.py` still `ast.parse`s, a second run is a no-op that still parses, and decoding the embedded payloads back to images confirms favicon=blue tile, light-slot=light tile (navy page), dark-slot=dark tile (white page) | Accepted |
| 105 | 2026-07-21 | **Success-green split into a fill token and a text token so it passes WCAG AA — mirroring the existing `--accent`/`--accent-text` pattern.** User: "make sure the app follows those principles" (the new `UI_UX_GUIDELINES.md`, which makes AA contrast a hard, verified requirement). Audited the shipped UI in `web.py` with the repo's own contrast checker: focus-visible, reduced-motion, the dialog/tour pattern, byte-identical dark-token blocks, single font stack, and `btnBusy`/`btnDone` all already conform. The one real failure was the success color: `--ok` (`#1a9d54` light) was used both as a **fill** (status dots) and as **foreground text** ("Saved ✓", table status, funnel conversion, answered-question labels), but as text it measured **3.5:1 — below the 4.5 floor** (and the removed setup-overlay's white-✓-on-green disc hit 2.25:1 in dark). Root cause: green had no fill-vs-text split, unlike blue (`--accent` for fills, dark enough for white text; `--accent-text` for text, light enough on dark). Fix: keep `--ok` as the fill/dot green; add **`--ok-text`** (`#127a3e` light → 5.0–5.4:1, `#4cc282` dark → 6.3:1) and repoint every `color:var(--ok)` foreground use (`.msg.ok`, `.rowsaved`, `.st-responded`, `.fn-conv`, `.tstep.done`, `.tjscore`, and the inline `qaStatus`/mailbox-status styles). Also tokenized stray hardcodes: `#fff` → `--accent-ink` on `.pill.active`/`.fitpill`/`#loop-stop`, and `#dl-pdf` `background:#111` → `--btn-dark` (theme-aware). Left the real-submit red `#b3261e` as-is (passes at 6.54:1; a deliberately theme-constant danger signal). No markup or logic changed — color tokens only, behavior preserved (guideline #7). **Verified**: contrast checker shows every changed pair ≥4.5 (dots still ≥3:1 graphical); `web.py` parses; Playwright screenshots in light **and** dark render with no regression. Note: mid-audit a parallel agent's onboarding rework (decision 104) landed in the working tree and removed the `#setup-overlay` I'd targeted for the disc fix — that white-on-green case no longer exists (the tour uses text glyphs, already covered by `--ok-text`), so a briefly-added `--ok-solid` token was removed as orphaned. Coordinated on the agent bus (claimed `web.py`) | Accepted |
| 104 | 2026-07-21 | **First-run onboarding replaced: a spotlight tour of what each section does, plus first-visit nudges — no more up-front chore checklist.** User: "make first-time setup more streamlined; since résumé import auto-populates fields, move the 'add details' step to the first time going into Profile; 'choose jobs' can move to the first Find/Profile visit; I'd prefer a walkthrough quickly pointing out what each section does over a checklist." The old onboarding (decision-era `_setup_status` + `#setup-overlay`) was a modal **checklist of 6 chores** (add details / add résumé / choose jobs / install browser / connect Claude / run dry-run) shown before the user could look around — front-loading work. Replaced with two pieces in `web.py` (server unchanged except intent): **(1) a spotlight tour** (`#tour-overlay` dim backdrop + `#tour-pop` popover; the sticky `aside.nav` gets `z-index` above the backdrop via a `body.tour-on` class so the highlighted tab glows *through* the dim) — 5 steps: a centered welcome, then one card per real nav tab (Profile → Discover → Review → Track), each switching to that view and ring-highlighting its tab (`.tab.tour-spot`) with a one-line "what it does"; the popover is `position:fixed`, JS `place()` anchors it beside the spotted tab with a left arrow and clamps to the viewport; Back/Next (last = "Get started →" lands on Profile), Skip, Esc-to-close, focus-trapped; auto-runs once per browser (`localStorage ab-tour-done`), reopenable from a nav **"✨ Take the tour"** button (`#tour-open`, replacing the old "Finish setup"). **(2) First-visit nudges** (`maybeShowNudge`): the "add details" and "choose jobs" chores moved here — a dismissible accent banner shown the first time the user opens Profile ("Start here — import your résumé and it fills these in… → Import my résumé", scrolls to the LinkedIn import block) or Discover ("First, tell the bot what jobs to find… → Set what jobs to find", scrolls to `#disc-settings`), each shown **once** (`localStorage ab-nudge-<view>`) and **only while that section is still incomplete** (gated on the cached `/setup/status` `ok` flags, which the tour fetches on load into a `SETUP` global); suppressed while the tour is running (`TOUR_ACTIVE`) so they don't flash behind it. `/setup/status` kept as the readiness source for the nudge gating (its `ok` booleans); its now-unused `action`/`cmd`/`flash` fields left in place (harmless, doctor-parity). **Verified** with Playwright against the live server: tour auto-opens centered, Next spotlights the real Profile tab + shows its view (`tour-spot` applied, `#view-profile` visible), steps through Discover; skip hides the overlay and restores the prior tab; simulating a fresh unconfigured clone (`SETUP` steps `ok:false`), the Profile and Discover nudges render, dismiss persists (`ab-nudge-discover=1`), and do **not** reappear on revisit; served-page JS `node --check` clean (with the `/*OPTIONS*/` placeholder stubbed); `import applicationbot.web` clean; `GET /` → 200 with all new ids present. Coordinated on the agent bus (claimed `web.py`, touching only the onboarding overlay/nudge markup + walkthrough JS) since a parallel agent also works the UI | Accepted |
| 103 | 2026-07-21 | **The macOS desktop app is now a fully self-contained, drag-to-Applications bundle (PyInstaller), superseding the venv-based .app of 102.** User: "I want the install flow to be the same as any other macOS app: drag to Applications and everything installs itself." Confirmed the trade-off with the user: a self-contained bundle is a *production snapshot* (can't mirror live repo edits) — so it's the release artifact, while development stays on localhost (`dev.sh`/`run.sh --window`, which run the live repo). Chose **PyInstaller** (spiked first: a 24 MB pywebview app built + launched self-contained from /tmp) to bundle its own Python + all deps + code into `ApplicationBot.app` (~190 MB). New `desktop_main.py` entry sets `APPLICATIONBOT_DATA` to `~/Library/Application Support/ApplicationBot` before import. **Data-dir refactor**: new `applicationbot/paths.py` exposes `DATA_ROOT` (the env var, else the repo root — so dev/localhost is unchanged) and a frozen-aware `BUNDLE_ROOT` (`sys._MEIPASS`); the 8 modules that rooted data at the package dir (tracker DB, discovery_seen/cache, fit_learning, archive, safety, resume_store, salary) now use `DATA_ROOT`; the frozen app serves **in-thread** (a frozen binary can't re-spawn `python -m applicationbot.web`) with cwd = `DATA_ROOT`. Build (`scripts/build_macapp.sh` rewritten): a `.build-venv` with deps + pyinstaller, `--collect-all` for playwright/pywebview/keyring/google_auth_oauthlib, `--add-data` for fixtures/examples and the package's nav/workday recipe JSON, ad-hoc `codesign --deep`. **Chromium** (Apply stage) isn't bundled — `_ensure_chromium_bg()` downloads it in a background thread on first launch via the bundled Playwright driver. This also **eliminates the 102 TCC problem**: a self-contained app reads nothing from ~/Documents, so there's no permission prompt at all. **Verified**: `ApplicationBot.app` copied to /tmp (no repo/Python/venv nearby) launches via `open`, serves the full UI (GET / → 200, title present) in its native window, shows the fresh-install walkthrough (`ready=false`, profile step incomplete), writes `applications.db` to Application Support, and the background Chromium install logged "finished"; 389 tests still pass after the data-dir refactor (only the 2 pre-existing `bot_wall` failures remain). Distribution to other Macs without a right-click→Open still needs Apple notarization (paid account) — out of scope | Accepted |
| 102 | 2026-07-21 | **ApplicationBot is now a standalone desktop app (own native window), not only a browser tab — pywebview + a hand-rolled macOS .app, forward-compatible (native arm64).** User: "use this as its own app I double-click that opens its own window, not through Chrome," then "make sure it's forward compatible (Rosetta/Python)." **Windowing:** new `applicationbot/app.py` runs the *same* local web UI inside a native OS webview (pywebview 6 → WKWebView/WebView2; added `pywebview>=5` to requirements). Everything carries over unchanged because it's the identical UI: dark theme, walkthrough, and dev auto-reload (the window reloads itself via the existing `/dev/reload-token` poller). Chose pywebview over Electron/Tauri (heavy; Electron bundles Chromium) and over a chromeless-browser hack (still a browser). The server runs as a **subprocess** (not an in-process thread) that the window points at — robust against pywebview's macOS GUI/event-loop quirks; on close the window terminates it. **macOS .app:** `scripts/build_macapp.sh` hand-rolls a bundle (Info.plist + a shell exec, no py2app), **ad-hoc code-signs** it (arm64 wants signing; fixes Gatekeeper's "no usable signature" reject so double-click launches). Launchers gained `run.sh --window` (+ `--window --dev`); `.command`/`.bat` point at windowed mode. **Forward-compatibility:** `scripts/_native.sh` guard refuses to build/run on a Rosetta/x86_64 Python and prints the fix (verified it passes native, rejects x86_64) — the machine is already arm64-native (universal2 Python, `proc_translated=0`). **macOS privacy (TCC) — the hard part:** a Finder-launched app can't read `~/Documents` during early Python startup, so the venv's `pyvenv.cfg` read failed (`Operation not permitted`). Fix (user chose it over moving the project): the venv now lives OUTSIDE the repo at `~/Library/Application Support/ApplicationBot/venv` (non-protected) via new `scripts/_venv.sh`, sourced by run.sh/build_macapp.sh/update.sh. That clears the un-promptable init failure; the app still reads the user's résumé/profile/data (in the Documents repo), which triggers a normal **one-time "access your Documents folder" prompt** (Info.plist carries `NSDocumentsFolderUsageDescription`). **Verified**: windowed app works end-to-end from the Terminal path (`run.sh --window`: server subprocess up, window opens, support-dir venv); pywebview + WKWebView backend import; server-in-thread and subprocess both serve 200; native guard both directions; bundle plist lints + ad-hoc signs. NOT verifiable from automation: the one-time Documents-consent click on a pure Finder double-click (auto-denies headless) — handed to the user to confirm. Zero-prompt alternative documented: keep the project outside ~/Documents | Accepted |
| 101 | 2026-07-21 | **Dev auto-reload + a one-command GitHub update, both stdlib/no new deps.** The user wanted local edits to be picked up without manual restarts, and an easy way to pull the latest from GitHub. Because the whole UI is a module-level `INDEX_HTML` string built at import, *any* change (Python or UI) needs a process restart to show — today only the manual `restart.sh`. **Reload:** a tiny stdlib supervisor `scripts/dev_reload.py` (spawned by `run.sh --dev` / `scripts/dev.sh`) polls mtimes of `applicationbot/**/*.py` every 1s and restarts the server child on any change; it also sets `APPLICATIONBOT_DEV=1`, which makes web.py serve a `/dev/reload-token` heartbeat (the process boot time) and inject a ~10-line poller that calls `location.reload()` when the token changes — so the browser refreshes itself after a restart. Chose an out-of-process supervisor over in-process `os.execv` (keeps the server code a plain server; no threading/exec fragility) and over adding `watchdog` (Guideline #2 — no new dep; mtime polling over ~50 files is cheap). Prod runs are unaffected: the endpoint returns a harmless token and nothing is injected unless the env flag is set. **Update:** `scripts/update.sh` fast-forwards the clone (`git fetch` + `merge --ff-only @{u}`), reinstalls deps + `playwright install chromium` when it advances, then applies it — if the dev reloader is running it lets it restart (browser self-refreshes), else it bounces a running `applicationbot.web` via `restart.sh`. It **refuses on a dirty tree** (Guideline #7 — never clobber the user's edits; prints the `git stash` recipe) and errors clearly with no upstream / on a non-ff divergence. README "Developing / Getting updates" section added. **Verified**: dev mode restarts on a real `touch applicationbot/usage.py` (boot token changes) and injects the poller; normal mode injects nothing yet still serves the token; `run.sh --dev` bootstraps + serves 200 (browser-open shadowed in the test); `update.sh` correctly refuses on the current dirty tree; all scripts pass `bash -n` / `ast.parse` | Accepted |
| 100 | 2026-07-21 | **UI best-practices / accessibility pass to WCAG 2.1 AA before the v0.1 release (HIG-aligned).** Prompted by "make sure we're following UI best practices (Apple HIG etc.)" ahead of a git push. A contrast audit of the just-retinted neutral-gray dark theme found real failures: white-on-primary-button 3.94:1, and the blue used as link/active-tab **text** 3.5–4.1:1 — both under the 4.5:1 AA floor. Root insight: a fill and text-on-dark need **opposite** lightness, so one accent token can't satisfy both. Chosen fix: **split the accent into two tokens** — `--accent` (fills/borders, paired with white text; dark `#5b7fca`→`#4a70c2` so white-on ≥4.5) and new `--accent-text` (accent-colored text/links; dark `#82a6ef`, light `#2258d8`). Swapped only `color:var(--accent)` → `--accent-text` (15 usages) via a space/`{`-anchored sed so `border-color`/`border-top-color` (7) stayed as fills. Also: `:focus-visible` outline on all interactive controls; `@media (prefers-reduced-motion: reduce)` drops the decorative flash + smooth-scroll but **keeps functional spinners**; the setup dialog now moves focus in on open, traps Tab, restores focus on close (ARIA dialog pattern, already had Escape + click-out + `aria-labelledby/describedby`); status never relies on color alone (✓ glyph on done rows); `--faint` nudged up and documented as **incidental-only** (info the user needs goes in `--muted`, which passes). Options weighed: (a) darken the single accent — rejected, fixes buttons but worsens link text and undoes the user's "more blue" tuning; (b) accept ~4:1 as "close enough" — rejected, the user explicitly asked for best practices; (c) two tokens — chosen. Wrote **[ui.md](ui.md)** as the standing UI reference (token system, the two-accent rule, the contrast checker script, a11y requirements, HIG takeaways). **Verified**: WCAG checker shows all text pairs now pass AA (dark button 4.79, dark link 5.8–6.6, light 4.75/5.3–6.1); Playwright confirms focus moves into the dialog and Tab stays trapped; 18 doctor/web/tracker tests pass; light + dark screenshots reviewed | Accepted |
| 099 | 2026-07-21 | **Package v0.1 as a versioned GitHub release + a one-step idempotent launcher (not a native binary), with an in-app skippable first-run walkthrough.** The app is a stdlib-`http.server` Python UI whose heaviest dep is Playwright's Chromium (~170 MB, installed separately) and whose Claude engine shells out to the user's own `claude` CLI (un-bundlable). Options for "downloadable app": (a) PyInstaller/py2app native binary — rejected for v0.1 (Chromium-in-PyInstaller is fragile, needs per-OS/arch builds + macOS notarization, still can't bundle `claude`); (b) pipx wheel — devs only, no double-click; (c) **GitHub release + one-step launcher — chosen** (cross-platform, low-risk, reuses `run.sh`). Deliverables: `__version__="0.1.0"` + `--version`; `scripts/run.sh` extended into a full bootstrap (python check → venv → deps → `playwright install chromium` → non-fatal `doctor` → open browser → serve); double-click wrappers `ApplicationBot.command` (macOS) and `ApplicationBot.bat` (Windows), with Linux on `run.sh` (user chose macOS+Linux+Windows); `scripts/release.sh` that **dry-runs by default** and only tags+`gh release`s under `--publish` (submission/outward-facing safety, Guideline #3), with a PII guard (`git ls-files` must not include `profile/*`, `.env`, `*.db`…). Walkthrough (user chose "in-app checklist, skippable"): `GET /setup/status` reuses `doctor.py`'s checks; a `#setup-overlay` renders one row per step, each with a one-click deep-link to its fix (Principle #2); auto-opens on a fresh/unfinished clone unless skipped (`localStorage`), reopenable from the nav. Rejected: a permanent Setup tab (useless once done) and a forced step-by-step wizard (rigid, duplicates the Profile forms). **Verified**: `run.sh` bootstraps and serves (GET / → 200, doctor all-green); release dry-run changes nothing (no tag created); walkthrough auto-open/deep-link/skip/persist/reopen all pass in Playwright; incomplete + done states screenshotted. Not yet published — `release.sh --publish` is the user's call | Accepted |
| 098 | 2026-07-21 | **The form-reveal click now matches "I'm interested", not just "Apply" — SmartRecruiters postings no longer time out with 0 fields.** A live dry run on `jobs.smartrecruiters.com/Consultadd4/87644936` failed with "Application form did not load within 25s … so no fields were filled" — not the DataDome bot-wall refusal, so the page loaded fine; the form was simply gated behind a control the reveal never clicked. Root cause: decision **076 documented** adding a `_REVEAL_CONTROL` matching SmartRecruiters' **"I'm interested"** button (anchored so "Not interested" can't match), but that code was **never actually committed to `apply.py`** — it lived only in the unrelated in-progress branch that also carries `_bot_wall_evidence`/`_distil_nav` (the source of the standing `test_nav_recipes.py` collection error + `test_parking.py bot_wall` failures). The shipped `_open_application_form` still revealed the form only via `re.compile(r"\bapply\b", re.I)`, so on SmartRecruiters it clicked nothing and polled an empty posting page until the 25s timeout. Fix (surgical, `apply.py` only): a module-level `_REVEAL_CONTROL = re.compile(r"\bapply\b|(?<!not )\binterested\b", re.I)` used in the reveal loop instead of the inline apply-only regex. The negative lookbehind excludes a "Not interested" dismiss button; `\bapply\b` word-boundaries keep "Applied filters" from matching. Deliberately did **not** wire the full nav-recipes learn/replay machinery from 076 (nav_recipes.json is empty and learning needs the off-by-default agentic path) — the reveal-label breadth is the one change that unblocks the reported failure. Rejected special-casing SmartRecruiters (the fix is a general reveal-label, no ATS branch). **Verified**: regex unit-checked (matches Apply/Apply for this Job/I'm interested/I am interested/Interested/Apply now; rejects Not interested/I'm not interested/Applied filters/uninterested); 2 new `tests/test_open_application_form.py` cases (a fake that honors the name regex: "I'm interested" reveals the form and is clicked; "Not interested" is never clicked and times out) + the 2 pre-existing SPA-timing cases still green (4 passed); apply/fill/parking/required/submit suite 93 passed (only the 2 pre-existing `test_parking.py bot_wall` failures + the `test_nav_recipes.py` collection error from the unrelated in-progress branch remain, unchanged). Live end-to-end confirmation flagged for the user's next SmartRecruiters dry run (this environment's egress IP is the DataDome-named one from 076, so it can't drive the real page) | Accepted |
| 097 | 2026-07-20 | **A submit the user clicks themselves during the dry-run review pause is now tracked as a real `applied` application (method `manual`), not left as a `dry-run` row.** The user's rule: keep it a dry-run *unless the bot or a human actually clicked Submit*, in which case track it as a regular application. The bot-armed click was already covered (`_attempt_submit` → `report.submitted` → `_record_run` writes `applied`). The gap was a **human** click: a dry-run fills the form and leaves the browser open for review; `_record_run` writes the `dry-run` row and the archive *before* the review pause, so a submit the user then clicks was never detected and the row stayed `dry-run`. Fix (surgical to `apply.py`): new `ApplyReport.manual_submit` flag + `_detect_manual_submit(page, frame, report)`, which flips the report to `submitted`/`submit_state="submitted"` the first time a submission confirmation appears — reusing the exact `_confirmation_evidence` (URL `confirmation`/`/thank` or body confirmation text) the armed path already trusts, **positive confirmation only** (never the weaker "form gone" heuristic, so navigating away mid-review is not mistaken for a submit; idempotent once submitted). It's polled every 400 ms inside the web `hold` review loop and once after the terminal review `input()`, so it catches the click regardless of how the user ends the session. After the pause, if `manual_submit`, `run_apply` re-calls `_record_run`, which **upserts the same posting row** (keyed by source URL) from `dry-run`→`applied` and appends the submitted attempt to the run log — the append-only history then shows both the dry-run fill and the manual submit. `_record_run`'s `method` now distinguishes `manual` (human) from `auto` (armed bot) from `dry-run`. `frame` is initialised to `None` up front so the Workday/early-exit paths can reach the pause check safely (`_confirmation_evidence` reads `page.url` regardless of frame). Web "done" message reflects the manual submit ("recorded as Applied") instead of always claiming a dry-run row. **Verified**: new `tests/test_manual_submit.py` (4 cases) — `_detect_manual_submit` flips on a real Chromium click of the local confirm fixture and is a no-op with no confirmation / once already submitted; a re-record after a manual click upserts the dry-run row to `applied`/`manual`, stamps `date_applied`, clears the parked block, and leaves both attempts in the run log; an armed-bot submit still records `method="auto"`. `test_submit.py` (7) green; broader apply/parking/multipage/runner/workday/calibration/usage suites pass (only the pre-existing `test_parking.py` `bot_wall` + `test_nav_recipes.py` failures from an unrelated in-progress branch remain, unrelated to this change). Detection relies on the site reaching a recognizable confirmation page — flagged for end-to-end confirmation on the next live dry run where the user hand-submits | Accepted |
| 096 | 2026-07-17 | **A combobox commit is now verified to have actually stuck, and the "Filled — review" UI notification fires the moment filling finishes instead of after the screenshot/archive.** Two live-SpaceX (Greenhouse) dry-run bugs. (1) `report.json` recorded `School → "Pennsylvania State University" [option:claude]` but the browser field was blank: `_commit_option_text` clicked the exact-text option in the **async** School react-select (decision 080) and returned `True` on the click alone — but when the search XHR re-renders the option list *as* the click lands, the node is replaced under the cursor, the click "succeeds" at the coordinates yet react-select never fires its select handler, so the field stays empty and we reported a false success (Guideline #7/#11). Fix: after clicking, wait 200 ms and read the control's rendered value via `_VALUE_JS` (react-select `single-value`); empty ⇒ retry the whole open→type→click up to 3×; if it never renders, return `False` so the field is recorded **unfilled** for the user, not a fake `option:claude` fill (can't read the value back ⇒ assume stuck, don't loop). (2) The web "Filled — review" phase (`on_filled`) fired only after `page.screenshot(full_page=True)` + answer-bank learning + tracker write + archive; on a long form the full-page screenshot dominates (many seconds), so the notif trailed the visibly-filled fields by ~a minute. The filled-panel renders only `report.summary()` text (never the screenshot image), so `run_apply` now fires `on_filled` **right after the fill's in-browser done banner**, before those four steps (they still run, just after the UI is told). Cosmetic trade-off: the filled-panel summary loses the "[answer bank] saved…/learned…" line (`_persist_learning` now runs after the callback); the final "done" `_set` still carries the full summary. Surgical to `apply.py`; imports clean; apply/fill/parking/required/combobox suite 72 passed (only the pre-existing `test_parking.py` `bot_wall` + `test_nav_recipes.py` failures from an unrelated in-progress branch remain, failing identically on the clean tree). School-commit fix flagged for end-to-end confirmation on the next live dry run | Accepted |
| 095 | 2026-07-17 | **The Track table now shows Claude token spend per application, split by activity, with batched-judge/discovery spend as one separate aggregate.** The user asked to see how many tokens each application cost so they can gauge how much Claude is doing (tailoring, form entry, etc.). Every Claude call already funnels through `backends.run_claude_cli`, whose `--output-format json` envelope carries a `usage` block (input/output/cache tokens) + `total_cost_usd` — previously discarded. New `usage.py` captures it via two contextvars: `activity` (WHAT — tailoring/form-entry/judging/enrichment/salary/impact, tagged at the call site or inherited from the block) and `for_posting(source_url)` (WHICH application — set around `pipeline.tailor_and_render` and inside `apply.run_apply`, both keyed on the same posting URL the tracker upserts on). `run_claude_cli` parses the envelope and best-effort writes one row to a new append-only `usage_events` table (`posting_key`, `activity`, in/out/cache tokens, cost) — a logging failure never sinks the Claude call. Attribution is honest: the **fit judge is one batched call over many candidates** (`matching.judge_fit_batch`), so it runs OUTSIDE any `for_posting` block → `posting_key=''` → reported as one separate "discovery & judging" all-time aggregate, **never divided across rows** (user's explicit choice over splitting it per-candidate). `tracker.usage_by_application()` groups events by source URL (with a per-activity sub-map + in/out/cost/calls); `usage_discovery_summary()` aggregates the unkeyed ones; deleting an application cascades its usage by URL so a re-discovered URL never inherits stale tokens. Track tab: a new **Tokens** column shows the compact total (e.g. "3.8k"); clicking it expands an inline sub-row with the in/out split and per-activity breakdown, and a one-line discovery aggregate sits above the table (click to expand its activities). No new dependency; additive schema (migration-free `CREATE TABLE IF NOT EXISTS`). **Verified**: one real `claude` call recorded 390 in/4 out attributed to a posting under `tailoring`; `tests/test_usage.py` (5 cases: activity routing, block-default inheritance, explicit-override, discovery bucket, no-usage no-op, delete-cascade) with synthetic envelopes; the live `/track` HTTP endpoint serves the per-app `tokens` + `usage_discovery` payload; served-page JS `node --check` clean; all touched-module imports + backend/matching/salary/enrich suites green (only the pre-existing `test_parking.py` bot_wall + `test_nav_recipes.py` failures from an unrelated in-progress branch remain) | Accepted |
| 094 | 2026-07-17 | **Work-location answers are now preference- and commute-aware, driven by a new adjustable `work_arrangement` profile setting — not a single global `open_to_remote` boolean.** In a dry run the bot signalled it would work remotely for a company with NY offices the user is within the posting's 35-mile range of and would rather commute to. Root cause (traced): every work-location question resolved from one boolean — `AnswerResolver.resolve` returned `_yn(open_to_remote)` for anything matching "remote", and `_office_prefs` appended "Remote" whenever `open_to_remote` was set, so an empty `preferred_locations` + `open_to_remote=True` ranked **Remote first** even for a "preferred office location" dropdown on a commutable posting. The boolean can't distinguish *willingness* ("I'm open to remote") from *preference* ("I'd rather be in this office"). Per the user's answers to a design question: (1) make it an **adjustable Profile setting** — new `ApplicationProfile.work_arrangement` (`""`=no preference/legacy · `in_office_if_commutable` · `hybrid` · `in_office` · `remote`) + `max_commute_miles`, surfaced as a Profile-tab dropdown + miles field; (2) decide "commutable" by **Claude judging** the posting's office location(s) from the JD against the applicant's home + radius (`AnswerResolver._commutable_office`, one cached call per posting, best-effort → falls through when unavailable); (3) a bare yes/no "Are you open to remote?" **still answers Yes** (the applicant IS open) — the preference is expressed only on arrangement/preference questions and office-location dropdowns. New `_preferred_arrangement()` maps the setting (+ commutability for `in_office_if_commutable`) to a target arrangement ("On-site"/"Hybrid"/"Remote"); a new `resolve()` branch (guarded by `_is_arrangement_pref_q`, before the bare-remote yes/no) and an `option_hints` branch return it so the form-side dropdown matcher maps it onto the posting's own wording; `_office_prefs` no longer offers Remote for an in-office/commutable preference and ranks Remote first only for a remote preference. `answer_bank.CLASSIFIABLE_TYPES` gains `work_arrangement` (preference) and sharpens `open_to_remote` (willingness) so the semantic classifier separates them. **Verified**: 13 new tests in `tests/test_work_arrangement.py` (bare yes/no unchanged; each explicit preference; commutable→On-site / non-commutable→Remote via a patched Claude; one cached commutability call per posting; unknown→falls through; the office-dropdown Remote-ranking bug fixed) + profile save/load round-trip of the two new fields (int coercion, empty→None) + served-page JS `node --check` clean + `web.py` imports; related apply/answer suite green (only the pre-existing `test_parking.py` bot_wall failures + `test_nav_recipes.py` collection error, both from an unrelated in-progress branch change, remain) | Accepted |
| 093 | 2026-07-16 | **Years-of-experience is a numeric fact the applicant owns — never guessed from the résumé; set the profile to reflect new-grad status.** In a dry run, a required dropdown "How many years of experience full time, industry… (not counting internships or school work)" was autofilled **"1 - 5 years"** for the user, a new grad with **0** full-time non-internship years. Root cause (traced, not guessed): the user's `profile/application_profile.yaml` had `years_experience: ''`, so `AnswerResolver.resolve` returned None; because the field is a **required** dropdown, the fallback `answer_bank.choose_required_option` asked the weak Claude model to pick the option "TRUE per the résumé" — and it counted résumé internships/projects as experience, picking 1-5 despite the question's explicit "not counting internships." This is exactly the confident-wrong-guess failure the codebase already blocks for salary/GPA/test-scores via `_NUMERIC_FACT`. Two complementary fixes: **(1) code** — added `"years of experience"`/`"years experience"` to `answer_bank._NUMERIC_FACT`, so `is_draftable_required` and `is_open_ended` return False and the field is **captured for the user** instead of guessed when the profile value is blank; the `valid_mapping`/classify path to the `years_experience` structured type is unaffected, so a *set* profile value still answers it. **(2) data** — set `years_experience: '0'` in the profile so `resolve` now returns `'0'` deterministically (→ maps to the "0-1 years"/"less than 1 year" option) and never reaches the guessing path. Chosen over leaving it blank-and-captured-only (the user shouldn't have to hand-answer every years question) and over drafting-from-résumé (fabrication of a fact the applicant owns). **Verified**: `is_draftable_required`/`is_open_ended` now False for the exact reported question while `valid_mapping(q,'years_experience')` stays True; `resolve` returns `'0'` with the set profile and `None` when blank; added a regression case to `tests/test_required_draft.py`; test_required_draft/test_required_dropdown/test_determinism_gates green (23 passed) | Accepted |
| 092 | 2026-07-16 | **Tailoring now closes an ATS feedback loop: a deterministic "dummy ATS" built from the JD grades each draft, and drops are re-tailored back in (bounded).** The user asked to optimize tailoring for the posting's ATS and "test against a dummy ATS based on the job description we pulled." Evaluated the referenced repo `sauravhathi/atsresume` first: it is a Next.js résumé-builder UI whose "ATS scoring" is only an outbound link to resumego.net — no scoring logic to reuse, and its résumé schema/template we already cover (`models.py`, `render.py`, `pdf.py`, `ats_check.py`). We already had both halves of the ask: `ats_score.py` (deterministic 0–100 pre-score from the JD) and `ats_check.py` (post-export PDF text-layer keyword coverage). The **gap** was that tailoring was one-shot — the dropped-keyword signal only became an advisory note, never fed back. Chosen (user-approved from an options table): **deterministic** keyword+knockout extraction (no per-JD Claude call) + a **bounded retry** loop. New `ats_requirements.py`: `extract(resume, jd)` → required keywords (`relevance.skill_terms(base)` ∩ JD mentions — the same honest universe `ats_check` uses, so the loop can never chase a skill the candidate lacks) + knockouts (years/degree evaluated against the résumé via `ats_score` parsers; security-clearance / citizenship-or-no-sponsorship detected via conservative regex and flagged as unverifiable blockers). `grade(resume_text, requirements)` → `AtsGrade` (keyword score 0–100, present/missing, knockout verdicts; `passed` gates on any failed knockout, mirroring a real ATS auto-reject). `tailor_resume` now grades the rendered draft and, for non-deterministic backends only, re-tailors up to `_MAX_RETAILOR=2` times feeding the exact missing keywords back as backend `emphasis` — stopping the instant a pass fails to shrink the gap (never regresses, never wastes Claude calls). The rules engine is skipped (deterministic → a retry yields the same output). The grade is appended to `relevance_notes`, so CLI/web/pipeline all display it with no per-surface change; `TailorResult.ats_grade` carries the structured verdict. Grading targets the rendered résumé text (not a fresh PDF per pass) for speed; the existing `verify_pdf` stays as the final post-export text-layer safety check. Backend signature change: `tailor(..., emphasis=None)` on the Protocol + both backends (rules ignores it). **Verified**: new `tests/test_ats_requirements.py` (6 cases) + full offline end-to-end `tailor_resume` on the real xAI JD (grade 100/100, years knockout evaluated, grade note surfaced) + a stub-backend test proving the loop detects a dropped keyword, feeds it back as emphasis, adopts the improved draft, and reaches 100; 60 related tests (tailor/backend/ats/cli/web) pass, no regression | Accepted |
| 091 | 2026-07-16 | **Track table columns are now drag-to-reorder, persisted per browser alongside the existing width/visibility prefs.** The user wanted to drag and reorder Track-table columns. The table already had per-browser column widths (`ab_track_colw`), show/hide (`ab_track_hidden`), and edge-drag resize; order was fixed by the `TRACK_COLS` array. Added a third localStorage pref `ab_track_order` (array of column keys) and an `orderedCols()` resolver that renders `TRACK_COLS` in the saved order, then appends any key not in that order in its canonical position — so a stale saved order can never drop or duplicate a column when `TRACK_COLS` changes (a newly added column just shows up at its default spot). `visibleCols()` now filters `orderedCols()` instead of raw `TRACK_COLS`, so order + hide compose. Each `<th>` is `draggable` (grab cursor; `.dragging` dims the source, `.dropto` shows an inset accent insertion bar on the target); HTML5 dragstart/dragover/drop handlers call `reorderCol(from,to)`, which moves the dragged key to sit directly before the drop target, saves, and re-renders. The resize handle's `mousedown` already `preventDefault()`s, which suppresses the native drag, so grabbing the right edge still resizes and never starts a reorder. "Reset columns" now also clears the order. All in `web.py`; no endpoint, schema, or data-flow change, no new dependency. **Verified**: `node --check` clean on the served page JS; unit-tested `orderedCols`/`reorderCol` in node — moves land correctly and a stale order referencing a removed key plus missing new keys yields no dupes, no drops, new columns appended in canonical position; `import applicationbot.web` clean; server boots and serves `/` 200 | Accepted |
| 090 | 2026-07-16 | **Backfill the per-PDF JD sidecar for pre-existing tracked rows so the Track "Re-tailor ▶" button appears for them — resolving decision 088's "reuse-only until re-dry-run once" caveat.** The user reported they still couldn't find where to re-run a dry run with résumé retailoring. Root cause, confirmed against the real DB (not guessed): the Track "Re-tailor ▶" button (decision 088) is gated on `resume_store.has_jd(resume_path)`, and **all 17 dry-run rows had `has_jd=False`** — every one predates the JD sidecar (086/088), so only "Re-run ▶" (reuse) ever rendered. The CLI fallback `scripts/retailor_tracked.py` reads the JD from the rolling discovery cache, which had since rolled over: only **1 of 17** rows (id 22, Stripe) was still cache-fresh. So 16 rows had no reachable re-tailor path at all. The user chose backfilling sidecars (over always showing the button + re-fetching the JD live at re-tailor time) so the existing gate stays honest — the button appears only when a JD is truly on disk. Added `scripts/backfill_jd.py` (Guideline #8, idempotent): for each `dry-run`/`tailored`/`blocked` row **missing** a sidecar (per the user's explicit instruction, it fetches only when one doesn't already exist — never overwrites), it obtains the JD **cache-first** (`profile/discovery_cache.json`, no network, exact bytes) then falls back to a **live re-fetch** of `source_url` via `enrich.fetch_full_jd(url, llm=claude_llm_extractor)` (the same json-ld→css→llm cascade discovery uses), and writes it with `resume_store.write_jd`. A row whose JD can't be obtained either way is reported FAILED (posting removed / nothing extractable), never silently skipped. This does **no** tailoring and touches no résumé — it only attaches the JD so decision 088's button can light up. **Verified end-to-end on the real DB**: cache path (id 22, 4366 chars via cache) and live-fetch path (id 6 Stripe, 4446 chars via llm) both write a valid sidecar; re-running id 22 reports HAVE and leaves it untouched (idempotent); `--all` wrote 15, already-had 3, **failed 0** — every one of the 17 dry-run rows now has `has_jd=True`, so "Re-tailor ▶" now renders for all of them on the next `/track` load. **Flagged honestly:** one sidecar came back thin — MARGO (id 9, 93 chars) — too short to tailor meaningfully; re-tailoring that row will produce a weak résumé until a fuller JD is captured (the other 16 are 1.2k–13k chars). Complements `retailor_tracked.py`, which can now also use `read_jd` for any saved-JD row | Accepted |
| 089 | 2026-07-16 | **Dashboard deslop pass driven by ibelick/ui-skills `baseline-ui` — Track table + Discover checkbox labels; behavior-preserving CSS/HTML in `web.py`.** The user asked to "install github.com/ibelick/ui-skills and use it to improve the dashboard." Clarified (told to user): `ui-skills` is an npm CLI (`npx ui-skills get <skill>`) that serves design-guidance skill docs to an AI agent — there is nothing to add as a runtime dependency to our zero-dep stdlib `http.server` UI; pulled ibelick's own `baseline-ui` "deslop" ruleset and applied it as an audit. Audited all four tabs live (Playwright screenshots, light + dark). **Review and Profile were already clean** (sentence-case field labels, empty states with a clear action) and were left untouched (Guideline #7 / Karpathy surgical — no manufactured churn). Two real `baseline-ui` violations fixed: **(1) Discover** — the auto-apply-loop "Re-prepare postings…" / "Re-tailor from scratch" checkboxes rendered as ALL-CAPS letter-spaced *paragraphs* because the global `label{text-transform:uppercase;letter-spacing:.03em}` bled onto the long descriptive `.loop-rescan` labels (unlike `.chkrow`/`.fld label`, which already opt out); fix = `.loop-rescan{text-transform:none;letter-spacing:normal;font-weight:400}` (baseline-ui: never uppercase body text). **(2) Track table** — `table-layout:fixed` + `width:auto` let the three native `<input type=date>` cells (hard ~138px min-width) hog space while text columns collapsed to 53–83px (`dr…`, `Te…`, `Consult…`). Three compounding fixes: (a) `.ttable{width:max-content}` so fixed layout honors each `<col>` width and overflows into the existing `.twrap` horizontal scroll instead of squeezing; (b) new `dateCell()` renders dates as plain tabular text (muted `—` when empty) and swaps in a real date input on click — saves on change, reverts on blur — mirroring the existing `urlCell` idiom; kills the empty-picker-in-every-row noise and reclaims ~300px (date col defaults 120→104); (c) status renders as a **badge** — `<select class="stcell st-*">` styled as a rounded pill tinted from its own status color via `color-mix(in srgb, currentColor 14%, transparent)`, still inline-editable; empty text cells get a `—` placeholder. No save endpoints, editability, or data flow changed; no new dependency. **Verified**: Playwright screenshots of all four tabs in light + dark — Track now shows full Company/Role/Location, colored status badges, and `—` for empties; 57 date buttons (19 rows × 3), 20 rendered as `—`, filled as MM/DD/YYYY, click swaps to a working date input; Discover checkboxes read as normal sentence case; `node --check` clean on the page JS | Accepted |
| 088 | 2026-07-16 | **Track "Re-run" lets you choose reuse-résumé vs re-tailor; re-tailor regenerates from a saved per-PDF job-description sidecar — resolving decision 086's structural follow-up.** The user wanted to decide, per re-run, whether to reuse the stored résumé or regenerate it. Re-tailoring needs the JD, which wasn't persisted per posting (decision 086 point 3, whose follow-up asked exactly for this). Chosen over re-fetching the JD from the posting URL (network-dependent, can fail on expired/changed postings): each dry-run now saves the JD to a `<pdf>.jd` JSON sidecar beside the tailored PDF (`resume_store.write_jd/read_jd/has_jd`, mirroring the `.stamp` sidecar; pruned/deleted with its PDF). `run_testing_mode`'s tailor+render half is extracted to `pipeline.tailor_and_render(resume, profile, jd, company, role, url)` (behavior-preserving) and reused by the re-run path. Track shows "Re-run ▶" (reuse; fast, no Claude) always, plus "Re-tailor ▶" only when `has_jd` is true — `_reapply_worker(retailor=True)` reads the saved JD and re-tailors against the user's CURRENT base résumé/prompt/layout (picking up 087's logic fingerprint), writes a fresh PDF (overwriting per-posting path), then fills; the `/track/resume` `Cache-Control: no-store` from 086 makes the new PDF actually display. `has_jd` is surfaced per row in `/track`; `retailor` threads `/parked/reapply`→`start_reapply`→`_reapply_worker`. Caveat (told to user): the pre-existing dry-runs predate the sidecar so they show reuse-only until re-dry-run once; a re-tailor with no saved JD returns an actionable error. Complements cursor's `scripts/retailor_tracked.py`, which can now use `read_jd` to re-tailor ANY saved-JD row, not just cache-fresh ones. **Verified**: JD sidecar write/read/has_jd round-trip; `tailor_and_render` (Claude+render stubbed) writes PDF+stamp; `/track` returns `has_jd`; drove the Track tab headless — with one seeded JD, exactly one "Re-tailor ▶" renders (on that row, not on JD-less rows); page JS `node --check` clean; 357 passed, 2 pre-existing `test_parking.py` bot_wall failures unrelated (updated that file's `start_reapply` mock for the new `retailor` kwarg) | Accepted |
| 087 | 2026-07-16 | **Guarantee tailoring changes always show after a restart+rerun: the reuse-stamp now fingerprints the tailoring code's SOURCE, not a hand-bumped version.** The user asked to be sure future résumé-tailoring changes show immediately on a rerun. Decision 082's stamp caught prompt edits (content-hashed `SYSTEM_PROMPT`) but gated layout on a manual `pdf.LAYOUT_VERSION` int (forget to bump it → stale résumé) and caught NOTHING in `length`/`catalogue`/`tailor` (e.g. a `line_chars` or selection-cap change silently no-op'd). Replaced both with `pipeline._tailoring_logic_fingerprint()` — a SHA1 over the source bytes of every module that determines a tailored PDF (`backends`, `catalogue`, `length`, `pdf`, `tailor`) — folded into `tailor_stamp` as one `logic` key. ANY edit to any of them changes the stamp, so a re-prepare re-tailors; nothing to remember. Computed once at import so it's pinned to the code actually running (Python needs a process restart for a source edit to take effect anyway), which is why the guarantee is precisely "restart the dashboard, then rerun". Over-invalidation (a comment-only edit forces a re-tailor) is the deliberate safe direction — the user prioritized "changes always show" over saving the occasional Claude call, and a real submit always re-tailors regardless. Removed the now-redundant `LAYOUT_VERSION`. **Caveat kept honest**: this makes the *reuse gate* re-tailor, but the normal loop still skips already-seen postings (`only_new=True`, decision 086) — seen postings refresh only via the "Re-prepare postings I've already seen" rescan (or `scripts/retailor_tracked.py`); NEW postings always get the current logic. **Verified**: the live fingerprint matches a hand-recomputed hash of the five module sources and changes on any source edit; the Stripe row 22 on-disk stamp no longer encodes the current logic → its reuse gate now re-tailors; 24 pipeline/pdf/tailor/length/web/track tests green | Accepted |
| 085 | 2026-07-16 | **Keep one tailored résumé per posting (overwritten on re-tailor); do NOT snapshot a résumé per run.** Asked whether each dry run should preserve its own viewable résumé, the user chose to keep the current storage. The tailored PDF path is keyed on the posting URL only (`resume_store.path_for` = `sha1(source_url)[:8]`), so every run of a posting resolves to the same file and overwrites it. Consequence, accepted as fine: when a re-run reuses the cached PDF (decision 082 stamp matched — unchanged JD/prompt/layout) every run's résumé is byte-identical, so the run-history "résumé ↗" link (→ `/track/resume?id=`, the posting's current PDF) correctly shows what each run used; when a re-run re-tailored, the new PDF overwrites the old, so an older run's résumé is not recoverable. Rejected the alternative — content-addressed per-run snapshots (`<name>-<contentHash>.pdf`, identical runs dedup to one file, differing runs each keep their own) — because it grows disk (one PDF per distinct tailoring) for a case the user doesn't need to revisit. No code change: the run-log's `resume_path` and the résumé link already point at the single per-posting PDF | Accepted |
| 084 | 2026-07-16 | **Track tab: the "Dry-run" date shows the LATEST run, and each posting expands to a per-run history log.** Follow-up to decision 083. Two changes. (1) **Latest, not first**: `apply.py._record_run`'s update path (a re-run of an already-tracked posting keeps status `dry-run`, so the tracker's stamp-once auto-stamp never fires) now explicitly sets `date_dry_run = today` on every dry-run outcome — so the column reflects when the posting was *last* dry-run filled; a plain field edit still never moves it (rejected surfacing `updated_at` for exactly that drift, 083). (2) **Per-run history**: a new append-only `application_runs` table (one row per apply run: `application_id`, denormalized company/role/portal/source_url, `outcome` dry-run/blocked/applied, `resume_path`, `detail`, `ran_at` datetime) records every `_record_run`; the `applications` table stays one-row-per-posting (dedup/funnel/ready-to-apply loop unchanged — the user chose this over one-Track-row-per-run, which would inflate the funnel and duplicate postings). Track shows a "Runs" column with an "N runs ▾" toggle that lazy-loads `/track/runs?id=` into an inline sub-row (timestamp · outcome · fill summary · résumé link); 0 runs → "—". `delete_application` cascades to the run log; a one-time migration seeds one run per pre-existing dry-run posting from `created_at`+`notes` (defensive SELECT: references only columns an old schema actually has, so it can't fail on a partially-migrated DB — the bug the parking migration test caught). **Verified end-to-end**: temp-DB insert+re-run logs 2 runs on 1 posting and refreshes the date, notes byte-identical; real DB seeded 17 runs (prefix normalized); `/track` returns `run_count`, `/track/runs` returns the log; drove the Track tab headless (Chromium) — "Runs" column renders, "1 run ▾" expands to `2026-07-16 11:29:29 · dry-run · 26 field(s) filled… · résumé ↗`, re-click collapses; page JS `node --check` clean; 356 passed, only the 2 pre-existing `test_parking.py` bot_wall failures (confirmed identical on a `git stash` clean tree) + the unrelated nav_recipes collection error | Accepted |
| 083 | 2026-07-16 | **Track tab shows when each dry run ran via a dedicated `date_dry_run` column, not the generic `updated_at`.** The user wanted to see when dry runs happened; the DB stored the time only in `created_at`/`updated_at`, neither surfaced. A dry-run row leaves `date_applied` blank and `date_discovered` only marks when the posting was found — so nothing on the Track tab said when the form was actually dry-run filled. Fix mirrors the existing `date_applied` precedent: new `date_dry_run` column auto-stamped `date.today()` whenever a row's status is/becomes `dry-run` (in `tracker.add_application` + `update_application`), a Track-tab "Dry-run" column (between Discovered and Applied) rendered as an editable date input like the others, and a one-time migration that ALTERs the column in and **backfills** existing dry-run rows from `substr(created_at,1,10)` so the 17 pre-existing dry-run rows show a real date, not a blank. Chosen over surfacing `updated_at` (rejected: it's bumped by any field edit, so it drifts from the true dry-run time). Shows the **latest** dry-run (per the user's follow-up, decision 084 wires the re-run refresh in `apply.py`); a plain field edit never moves it. No new-code path in apply.py for the first stamp — dry-run rows are created via `add_application`, which now stamps. **Verified**: fresh dry-run insert, discovered→dry-run flip, and applied-row (no stamp) all correct on a temp DB; real DB migrated + all 17 dry-run rows backfilled (Stripe 2026-07-16, Ramp 2026-07-14, …), 0 blank; `/track` API returns the field; funnel/calibration/runner/web-CSRF suites (50) green (2 pre-existing `test_parking.py` bot_wall failures + nav_recipes collection error unrelated) | Accepted |
| 086 | 2026-07-16 | **Why a re-tailor still showed the old résumé: the normal loop never revisits seen postings, `/track/resume` was browser-cacheable, and JDs aren't persisted per PDF — plus a re-tailor script.** After decisions 081/082 the user re-ran a Stripe dry run repeatedly and still saw the old résumé. Three separate causes, found by evidence not guessing: (1) **primary** — the normal loop calls `discover_and_match(only_new=True)` (`web.py` `_loop_worker`), which returns only postings never judged before, so an already-tracked posting is never re-processed and its PDF is never rewritten (proved: every stored Stripe PDF's mtime predated the code change; re-tailoring only happens on the opt-in **rescan** path); (2) **latent** — `/track/resume` streamed the PDF with no `Cache-Control`, and its URL is a stable `?id=<row>`, so even once a PDF is overwritten in place the browser's viewer serves the cached old bytes — fixed by adding `Cache-Control: no-store` to that response; (3) **structural** — the JD is not persisted per PDF/row, so only rows whose JD is still in the rolling discovery cache can be re-tailored offline (1 of 18 tracked rows here). Added `scripts/retailor_tracked.py` (Guideline #8): re-tailors tracked rows whose JD is still cached with the CURRENT code, overwriting the stored PDF + stamp in place; idempotent (skips when the stamp already matches unless `--force`); reports rows it can't re-tailor rather than silently skipping. **Verified end-to-end on the user's real Stripe posting** (row 22, Technical Support Engineer, `--backend auto`): regenerated via claude-code, and the new PDF's extracted text confirms all three earlier changes now render — experience order `Ninth Wave → Jaguar → Kumon` (software above tutoring), preserved metrics (`1M+ transactions/100% faster`, `618 postings/0 errors`, `~11,100 lines`, `$170K+ sales`, `20% wait-time cut`), 34pt margins, 1 page. Follow-up logged: persist each posting's JD (or the tailored structured data) beside its PDF so ANY tracked row can be re-rendered after a logic change, not just cache-fresh ones. Suites: web/pipeline/tracker/stamp 13 passed | Accepted |
| 082 | 2026-07-16 | **The tailoring reuse-stamp must include the tailoring LOGIC (prompt + PDF layout), not just the data — otherwise prompt/layout changes silently no-op.** Surfaced by decision 081: after changing the tailoring prompt and PDF margins, a Stripe dry run showed an unchanged résumé in the tracker. Root cause: a dry run reuses the stored per-posting PDF (`profile/tailored/<…>.pdf`) whenever its `.stamp` sidecar matches (`pipeline.run_testing_mode`, decision 069), skipping the Claude tailor call AND the PDF render. But `pipeline.tailor_stamp` hashed only résumé + profile links + JD — NOT the prompt or the renderer — so a logic change left the stamp identical and served the stale PDF. Fix: fold `backends.SYSTEM_PROMPT` and a new `pdf.LAYOUT_VERSION` constant (bumped on any layout change; started at 2 for the 081 margin/header change) into the stamp payload, so any prompt or layout edit invalidates every cached PDF automatically and the next dry run re-tailors — no manual "Re-tailor from scratch" checkbox needed. Prompt is hashed by content (auto-invalidates on edit); layout uses a hand-bumped version (renderer code can't be content-hashed cheaply). A real armed submit already always re-tailors, so submissions never rode on a stale artifact — this only affected the dry-run preview. Immediate unblock that already existed: the loop panel's "Re-tailor from scratch" checkbox (`force_retailor`). **Verified**: toggling either `SYSTEM_PROMPT` or `LAYOUT_VERSION` changes the stamp hash; pipeline/stamp/store suites green (11 passed); full suite 357 passed (2 pre-existing `test_parking.py` bot_wall failures + nav_recipes collection error unrelated, confirmed failing on a clean tree) | Accepted |
| 081 | 2026-07-16 | **Tighten résumé tailoring: preserve existing metrics + slightly narrower margins.** The user reported "not seeing enough quantifiable metrics" in tailored résumés. Root cause is NOT a thin source — the base résumé is dense with real figures (1M+ transactions, ~70% latency cut, 5× latency, 618 postings/0 errors, 40+ screening questions, etc.); tailoring was dropping/softening them. Since numbers can only be fabricated at the cost of truthfulness (Guideline #7 / safety), the fix strengthens **preservation**, not the anti-fabrication guard: the `SYSTEM_PROMPT` QUANTIFY rule now makes keeping a base bullet's number the model's FIRST duty (rephrase words freely, never drop the metric), and directs it to lead each entry with its most-quantified job-relevant bullet and prefer metric-bearing bullets when the length budget forces a cut — the "use only base-résumé numbers, truthful-no-metric beats fabricated" guard is kept intact. **Experience ordering** is now explicitly relevance-first with recency as the tiebreak: the prompt directs the model to rank experience by relation to the posting (a matching domain outranks an unrelated one regardless of dates — a software role above tutoring/retail for an SWE job) and only break ties among comparably-relevant entries by recency (most recent nearer the top); same rule for projects/activities. Formatting: PDF page margins reduced 42/40/42 → 34/34/34 pt (`pdf._Resume`, epw 528→544pt) for a slightly wider text column; `LengthBudget.line_chars` default 100 → 103 to match the wider column so "fill the line" guidance stays accurate (bullets don't leave a ~3% sliver empty). **Header overflow fixed**: the contact name/details were rendered with a fixed-size `cell`, which neither wraps nor shrinks — a contact line wider than the column (sample = 610pt vs 544pt usable) overprinted ~33pt past each page edge. New `pdf._fit_font` picks the largest Helvetica size that fits the content width (name 20→floor 12pt, contact 9→floor 6.5pt), so the header can never spill past the margins. No schema/interface change. **Verified**: sample résumé renders 1 page at the new margins with the contact line shrunk 9→8pt to fit (542≤544pt) and name held at 20pt; profile résumé unchanged (contact already fit at 9pt); prompt/budget wiring confirmed (line_chars=103, preserve-metrics + relevance-first-experience blocks present); 23 pdf/tailor/length/render tests green | Accepted |
| 080 | 2026-07-16 | **A searchable combobox the batch declined still gets its round-2 typeahead Claude pick — so the school picker prefers the MAIN campus, not a branch (decision-033/079 follow-up).** After decision 079 fixed School submitting empty, it committed via the `substring` fallback (first fuzzy match) rather than the Claude pick that's meant to prefer the primary campus. Root cause, found by driving the live SpaceX Greenhouse form: the School react-select is an **async search** whose OPEN list is the **first 60 schools alphabetically** (Acadia, Adamson… — never the applicant's), and typing `Pennsylvania State` returns `Pennsylvania State University` **and** `…- Schuylkill Campus`. Round 1 defers a batch pick over the alphabetical open list; the batch (correctly) declines it — but `_resolve_pending` marked the label `picks_done`, and `picks_done` gated **both** Phase 1's static open-list pick **and** Phase 2b's article-stripped typeahead pick. So round 2 skipped Phase 2b (the one built for async school pickers, told to prefer the main campus) and fell through to Phase 2c's substring fallback, which takes the first fuzzy match — a branch campus if the async lists it first. Fix: split the gate — Phase 2b now runs on `gen_on` (generation + a value), **not** `use_claude` (which still respects `picks_done`), because Phase 2b's options come from the per-query async results, not the open list the batch already saw. Phase 1's static re-ask stays suppressed (no wasted calls); a genuinely static undecidable dropdown types to zero options in Phase 2b and makes no extra call. Rejected freeing `picks_done` wholesale (would re-ask Phase 1's static pick every round 2). **Verified live on SpaceX**: School commits `Pennsylvania State University` (main) via the Claude typeahead. New async-picker fixture + two-pass test lists the branch campus first and fails without the fix (`'…- Schuylkill Campus'`, `source=substring`), passes with it (`main`, `source=option:claude`); combobox/two-pass/required-dropdown/multipage/fillability/lever/determinism suites green | Accepted |
| 079 | 2026-07-16 | **Skip `aria-hidden` inputs when filling — react-select's requiredInput mirror was hijacking its own dropdown (School submitted empty).** A SpaceX dry run left the Greenhouse **School** field on `Select…` while Degree/Discipline filled — yet the report logged School as *filled* (`source=resolver`, plain text). Root-caused by driving the real form: Greenhouse renders each react-select as **two** inputs sharing one label — the real combobox (`role=combobox`) and an **`aria-hidden="true"` `requiredInput` shadow** (empty `type`, `tabindex=-1`) used only for HTML required-validation. When the résumé value (`The Pennsylvania State University`) doesn't literally match an option on open — the decision-033 article-prefix case — the combobox **defers** its pick to the batched Claude decision and returns via a `continue` that **doesn't mark the label done**. The loop then reaches the mirror, whose empty `type ∈ _TEXTLIKE` and `role != combobox` classify it as **free text**, so it's `.fill()`'d — writing into an invisible input, but **marking "School" done**. Round 2 then skips the label, so the real dropdown is never committed → submits empty, while the report falsely reads filled. Fix is one guard in `_fill_all_fields`: **skip any `aria-hidden="true"` input** (never a field a user fills), so the mirror can't claim the label and round 2 recommits the actual selection. General across every react-select field/ATS, not SpaceX-specific. Rejected marking the label `done` on defer (breaks round 2's recommit, which relies on the label staying open). **Verified on the live SpaceX form**: School now commits through the combobox (`control=combobox`, a real Penn State option) instead of the phantom text fill; Degree/Discipline unchanged. New fixture reproduces the dual-input structure and the regression test fails without the guard (`FilledField(control='text', source='resolver')`) and passes with it; related fill suites (combobox, two-pass, multipage, fillability, lever, corpus) green | Accepted |
| 078 | 2026-07-15 | **The tracker's Source URL is the link itself; editing moves behind an ✎ toggle.** The cell rendered as a text `<input>` with a 12px `↗` beside it, so the URL *looked* like plain text and the only clickable target was the glyph — the obvious affordance did nothing (UI Principle #1). The URL is now an `<a>` whose text is the URL (`target=_blank`, `rel=noopener noreferrer`); an `✎` swaps in the same input, and committing it saves via the existing `saveCell` and returns to the link. Editing is **kept**, not dropped: the cell has always been editable and a manually-added row needs a way to set its URL (Guideline #7). The link renders only for `http(s)` — anything else (empty, or a stored `javascript:`/`data:` string) falls back to the input, a guard inherited unchanged from the `↗`. Only the **cell** re-renders on save, never the row: `saveCell` writes "Saved ✓" after its `await`, so replacing the row would land the confirmation on a detached node (UI Principle #5). **Surfaced a real layout bug**: a long URL is unbreakable text, so as a link it inflated the column to **583px** — past the 220px default and the resize handle — squeezing every other column (Company → 83px); an `<input>` never did this because its intrinsic width is small. `contain:inline-size` on the link keeps its text out of the table's intrinsic width so `table-layout:fixed` honours the `<col>` again, **reproducing the baseline geometry exactly** (measured against a `git stash` baseline: table 1370px, Source URL 98px, all 16 columns identical). **Verified in the real UI on the real tracker**: 18/18 rows render as links with correct hrefs and zero inputs; a full `✎` → edit → `Saved ✓` → link-with-new-href round trip on live row 21, with the original value written back so `applications.db` is untouched. Suite **375/375**. Known gap: the column is 98px, so links show truncated (`https://j…`) with the full URL on hover — the input truncated identically and the squeeze is pre-existing (`width:auto` + `table-layout:fixed` ignores the 220px default even at baseline); widening it, or shortening the label to `smartrecruiters.com/…/87644936`, is a separate change | Accepted |
| 077 | 2026-07-15 | **Bot-walled applications are parked as their own kind (`bot_wall`) and retried later — fixing two bugs decision 076 introduced.** 076 made a bot wall detectable but not *routable*, and the user's own live run proved it: **real tracker row 21** came back `status='dry-run'`, `blocked_kind='captcha'`. Both wrong. (1) **Mis-parked as CAPTCHA** — `classify` scanned `"captcha" in " ".join(errors)`, and the wall's own vendor host is **`captcha-delivery.com`**, so an IP block was labelled "A CAPTCHA is in the way — solve it in the open browser": there is no puzzle, and a headless run has no browser to solve it in. Fixed by a **structured `ApplyReport.bot_wall`** flag classified **first** — deliberately not prose-matching, since prose-matching *is* the bug. (2) **Advertised as ready to apply** — a walled run never reaches submit, so `submit_state` stayed `"dry-run"`, and `web.py` treats **any** dry-run row as "ready to apply"; a posting we were *refused* on sat in the ready queue. `_record_run` now records `blocked` when `bot_wall` is set, so it lands in `parked_applications`. New `parking.BOT_WALL` is resumable **by time, not by the user** (`resolve=""`, verb **"Try again"**) — the existing `/parked` + `_reapply_worker` are kind-agnostic, so "go back and do them later" needed **no new plumbing**. Copy corrected where the new kind broke it (Guideline #11 / UI #3-4): the tracker note says **"Refused"** not "Dry-run: 0 field(s) filled" (the exact line that made row 21 unreadable); the runner header no longer tells every parked row it is "waiting on you — resolve" (a wall waits on the **site**) and each line now carries its own verb; the web card no longer calls a refusal a "site error". **Submit for real** is deliberately KEPT on the card — if the block has lifted, the armed retry is exactly the "do it later" path. **Verified on the user's real data**: row 21 re-driven live → `blocked`/`bot_wall`/`blocked by captcha-delivery.com`; the card **rendered and screenshotted** in the real UI via a real Discover-tab click; both fixes **mutation-checked**. 5 new tests, suite **374/374**. Known gap: an upsert never clobbers user-owned `notes`, so row 21's pre-existing note still reads "Dry-run" — `blocked_kind`/`detail` carry the truth and drive every surface | Accepted |
| 076 | 2026-07-15 | **Agentic nav fallback + host-keyed nav recipes, and bot walls reported as refusals — three distinct causes behind one "couldn't find the application".** The reported failure (tracker row 21, SmartRecruiters, 0 fields) was investigated rather than assumed, and was **not one bug**: (1) `detect_ats` had no SmartRecruiters branch (the gap decision **074 flagged**); (2) `_open_application_form` revealed forms only via `/\bapply\b/i`, but SmartRecruiters' control says **"I'm interested"** → new `_REVEAL_CONTROL`, anchored so "Not interested" can't match; (3) **the real blocker, found only by driving it live**: the site answers with **HTTP 403 + a DataDome wall** (`geo.captcha-delivery.com`) rendered **inside an iframe over an empty host page** — so the run misreported a **refusal** as "form did not load", and a first main-frame-only detector saw nothing. New `_bot_wall_evidence` walks every frame (text signals + vendor hosts); a wall now yields a precise error and **suppresses the agentic fallback** — an agent hits the identical wall from the same IP, and aiming one at a bot wall is evasion (Guideline #4). Layer 2 (the user's ask) mirrors decisions 061/063 exactly: when `nav_agentic: true` (**off by default**; replay always free) a Claude+Playwright-MCP worker over CDP reaches the form **once**, and `nav_recipes.py` distils a **host-keyed, PII-free, committed** recipe (`{host: {url_suffix, reveal_labels}}`) by **DIFFING the DOM** (what navigated / what vanished) — never parsing MCP refs — so every later posting on that host replays deterministically with no Claude. Host is the key, so one learned posting unblocks the whole site. `_distil_nav` verb-filters vanished controls (a cookie banner must never become a recipe) and yields **nothing** rather than a wrong recipe on an opaque route; `is_shareable_host` keeps loopback/private hosts out of the shared library (a live drive really did try to commit `127.0.0.1`). Rejected: agent-only (first run on every site costs Claude, and this posting stays blocked) and on-by-default (spends tokens unasked, diverges from `workday_agentic`). **Verified:** the flagged live Claude-over-MCP step of 061/063 is now **actually driven** — a real worker opened the fixture form, learned `"Join our team"`, and a replay opened it with the agent asserted to run exactly once; the real posting now reports the DataDome refusal precisely (was: a misleading timeout); the bot-wall guard is **mutation-checked** through `run_apply`. 18 new tests (fixtures reproduce the real page + the real iframe wall); suite **369/369**. **Honest limit: the SmartRecruiters fix could not be confirmed end-to-end — this environment's cloud egress IP is the one DataDome named** | Accepted |
| 075 | 2026-07-15 | **Mailbox secrets are `repr=False`; the env-vs-link test pins an unlinked path.** `test_load_config_needs_all_three` asserted `load_config({...}) is None`, but `load_config` prefers a stored **link** over env (decision 057) and defaults to the real `profile/mailbox.yaml` — so on any machine with a linked mailbox it returned the live config, failed, and pytest printed the **real Gmail app-password** in the assertion diff. Two distinct bugs: (1) the test was environment-dependent (passed only on an unlinked box, e.g. CI) — fixed by passing `backend=_FakeKeyring(), path=_link_path()`, the isolation idiom the rest of the file already used and this one test predated; mutation-checked that it still catches the regression it exists for. (2) **any** repr of a `MailboxConfig` leaked a live credential — a traceback, log line, or diff would do it. `password`/`refresh_token`/`client_secret` are now `field(repr=False)` on the dataclass: values stay fully usable in code and `asdict()` is unchanged, they simply never render in `repr`/`str`; non-secret fields still print so repr stays useful for debugging. `link_status()` remains the safe view. Rejected leaving it at (1): that closes only the one known leak path and the exposure recurs on the next failure that prints a config. Behaviour change flagged and approved (Guideline #7): debug output no longer shows those three values. Suite **351/351 green** (was 349/1 with this pre-existing failure). Related: the bot-inbox address was scrubbed from `DECISIONS.md`/`NEXT_STEPS.md` before the 069-074 commit — it was absent from HEAD and is contact detail (Guideline #12) | Accepted |
| 074 | 2026-07-15 | **Resolve Workday (and SmartRecruiters) JDs in the curated feeds, unlocking +1,053 candidates the filter was discarding.** `_CURATED_ATS` gated curated listings to Greenhouse/Lever/Ashby — the only ATSs with a hand-written `_resolve_jd` helper. Measured against the live feeds, that discarded **755 active Workday** and **298 SmartRecruiters** postings *the Apply stage can already submit to* (`pipeline._is_fillable` explicitly allows `workday`; `workday.apply_workday` is a complete backend). The gate was never about fillability — it was about JD resolution. **Workday resolves with no new code**: it renders client-side but still ships schema.org JSON-LD in the initial HTML, so `enrich.fetch_full_jd` (the existing cascade from #047) returns a full JD on a plain GET — **10/10 live postings via the `json-ld` tier, no browser, no LLM call** (called without `llm=`, so it stops at the free tiers). Rejected Playwright (my initial assumption — the spike disproved it: unnecessary cost/fragility) and Workday's `/wday/cxs/` JSON API (works 15/15 but returns *less* text than the JSON-LD and needs a bespoke tenant/site URL rewrite). SmartRecruiters needed only a tuple entry — `_resolve_smartrecruiters_jd` already existed. Flagged: SmartRecruiters routes to **generic autofill** (`apply.detect_ats` doesn't know it — decision #030), which is less proven than the Workday backend. Verified live end-to-end: real config → `build_sources` → `discover` returned 6 postings / 0 errors including a **Workday** JD (Northrop Grumman, 5,521 chars) and a SmartRecruiters JD, all clearing `_is_fillable` and routing to the right backend | Accepted |
| 073 | 2026-07-15 | **Any GitHub repo job board is drop-in config, not code** (`early_career.feeds`). The curated source hard-coded two SimplifyJobs URLs. Investigated adding more repos and **measured the marginal yield first**: the two live `vanshb03` boards add only **103 new fillable postings** over Simplify's 1,093 (8 overlap) — while `max_resolve` truncates the pool to 40, so more repos ≈ **zero** real gain. Conclusion recorded: **the funnel neck (40 of 1,130), not feed count, is the binding constraint** — so the value here is optionality (drop in a board when one matters), not breadth, and no extra repos ship enabled by default. Chose to **extend `early_career` with `feeds:`** over a new top-level `github_boards:` block — backwards compatible, no migration, `kinds` keeps naming the built-ins. A feed is a bare string (built-in name or raw URL) or `{name, url}`; feeds are named `<owner>/<repo>` so they read cleanly in logs and the cache fingerprint. **No per-feed field mapping**: SimplifyJobs' `listings.json` is the de-facto schema (vansh is a fork) and every field was already read via `.get()`, so a URL alone suffices. Kept **one source** rather than one-source-per-feed (which would get per-feed error isolation free from `discover()`) so ranking stays **global** across feeds and `max_resolve` stays a whole-run budget — per-feed sources would have silently multiplied Claude cost per board added (Guideline #7). A bad feed therefore **fails loudly** naming the feed and the fix, rather than silently shrinking results (UI Principle #5); `discover()` still isolates the source so the run continues. Web UI gained the feeds field — without it `readDiscForm` would have **silently wiped** `feeds` on every save. Verified live: a dropped-in `vanshb03/New-Grad-2026` URL flowed config → discovery → full-JD Postings, 0 errors | Accepted |
| 072 | 2026-07-15 | **LinkedIn job alerts as a discovery source, ingested by email forwarding into the already-linked bot inbox.** LinkedIn has no compliant live-link (API is partner-gated; scraping `linkedin.com/jobs/view` is robots-disallowed — Guideline #4, same conclusion `linkedin.py` already reached for profile data), but the alert emails LinkedIn sends *to the user* are the user's own data and carry the lead: company + title + location. Ingest path: a Gmail filter in the user's personal account forwards `jobalerts-noreply@linkedin.com` to the bot inbox (the address linked in `profile/mailbox.yaml`), which `mailbox.py` already reads — so **no new auth, no second link slot, and the personal inbox is never exposed to the bot**. Rejected: OAuth read-only on the personal account (needs a ~10min Google Cloud setup, and still grants the bot standing access to a personal inbox) and an IMAP app-password on it (2min but grants full read/write/delete on personal mail — fails Guideline #5 least-privilege). Alerts yield **leads, not applyable postings** (the email links to `linkedin.com/comm/jobs/view/<id>`, which redirects to LinkedIn — not an ATS — so `bridge_aggregator_postings` dead-ends and the body is a card snippet with no JD). Staged build: **(A)** `LinkedInAlertSource` parses alerts → `Posting(ats="linkedin_alert", auto_applyable=False)` leads, shaped on `AdzunaSource`; **(B)** a company→ATS-board resolver (does not exist today) matches each lead to the employer's public Greenhouse/Lever/Ashby board for a full JD + fillable apply URL, so leads reach Apply and the pipeline stays fully automated (Guideline #0) — A alone would add a human triage step and is a stepping stone only | Accepted |
| 071 | 2026-07-14 | Draft short, optional **"Why &lt;Company&gt;?"** prompts. Ramp's Ashby form renders "Why Ramp?" as a short, OPTIONAL single-line `<input>`; it was left blank because `is_open_ended` needs either a textarea or a >25-char question containing an open-ended phrase, and the bare "Why Ramp?" matched none — nor any `_COMPANY_SPECIFIC` phrase, so it wasn't even recognized as company-specific. Fix (`answer_bank.py`): `is_company_specific` now also matches any prompt that simply opens with "why " (the company name is dynamic and can't be listed) → excluded from structured mapping + caching; and `is_open_ended` treats a company-specific prompt as draftable even when short/single-line. Net: "Why Ramp?" is now grounded-drafted (résumé + company + JD, weak model) whenever company/JD context exists (the pipeline sets both), and never cached. Refines decision 067 (which drafted such short fields only when REQUIRED). Behaviour change flagged (Guideline #7): an optional "Why <company>?" that used to be left for the user is now auto-drafted — the reported failure. Verified: live probe of the real Ramp form's fields + `tests/test_determinism_gates.py`/`test_required_draft.py` (gate + end-to-end draft/decline, updated required-draft contract) | Accepted |
| 070 | 2026-07-14 | Fix SPA Apply-reveal timing: retry the "Apply" reveal-click inside the form-load poll loop instead of once before it. Ashby (and other JS-mounted ATS whose real form lives at a separate route, e.g. `<posting>/application`) mount the "Apply for this Job" control *after* domcontentloaded; the old one-shot pre-loop click fired before the button existed, never navigated, and the loop then watched an empty posting page until the 25s timeout — the exact "Application form did not load" failure on a Ramp/Ashby posting. Now the reveal-click retries each poll pass until the control appears (`revealed` latch prevents re-clicking once done). General fix, no ATS special-casing; behaviour preserved when a form is already rendered. `apply.py` `_open_application_form` only. Verified live against the reported Ramp Ashby URL (loads `/application`, 12 fields, no errors) + `tests/test_open_application_form.py` (2, fake-page regression: fails on old code with the exact 25s error, passes on the fix) | Accepted |
| 068 | 2026-07-14 | Web UI revamp: left **nav rail** + design-token system with **dark mode**. The Review-only tailoring sidebar (résumé/engine/quality/length/Tailor) was permanently docked on the left of *every* tab — dead, confusing space on Discover/Profile/Track (a navigation bug). Now the left column is a persistent app **nav rail** (Review · Discover · Profile · Track, with icons + a Claude-status badge + theme toggle at the foot); the tailoring controls moved into the Review view as a compact top control bar (only shown where they apply). All colors migrated to CSS custom-property **tokens** with a full **light/dark** theme (`prefers-color-scheme` default + a persisted `data-theme` toggle stored in `localStorage`; `color-scheme` set so native selects/date-pickers/scrollbars follow). Pure presentation — **no server code, no element IDs, and no JS wiring changed** (Guideline #7); the only functional touch is a `.controls .ctrl.hidden` rule so the paste-a-posting toggle still hides. `web.py` INDEX_HTML only. Verified live via Playwright across all four tabs in both themes (console clean), the fixture/paste toggle both ways, and a full `rules`-engine tailor → render → PDF-button flow; `test_web_csrf.py` green | Accepted |
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

## 068 — Web UI revamp: left nav rail + design tokens with dark mode

**Context.** The web UI (`web.py`, a single stdlib `http.server` serving one inline
`INDEX_HTML`) had grown four tabs — Review, Discover, Profile, Track — selected by a row
of plain-button "tabs" at the top of `<main>`. But the **left `<aside>` held only the
Review tab's tailoring controls** (résumé picker, job source, engine, quality, length,
Tailor button) and was rendered *outside* the tab-switching logic, so it stayed docked on
the left of **every** tab. On Track and Profile that 320px column was pure dead weight and
actively confusing (a "Tailor résumé" button while looking at the application tracker).
The user asked to "make it a bit more modern and easier to navigate."

**Options considered.**
- **A — Left nav rail (chosen).** Convert the left column into a persistent app-nav rail
  (the four destinations, with icons, a Claude-status badge, and a theme toggle at the
  foot) and move the tailoring controls *into* the Review view. Fixes the dead-sidebar
  bug directly and reads as a conventional modern app shell.
- **B — Top nav bar.** Drop the sidebar entirely; a top bar holds nav + status. More
  horizontal content room but a bigger restructure and loses the natural home for the
  persistent Claude-status badge.
- Visual scope: full refresh **with** dark mode (chosen) vs. light-only.

Both chosen by the user (nav model = rail; scope = refresh + dark mode).

**Decision / implementation.** `web.py` `INDEX_HTML` only — no server code, no route, no
element ID, and no JS behavior changed (Guideline #7 — pure presentation).
- **Tokens + theming.** The `:root` now defines semantic CSS custom properties
  (`--bg/--surface/--surface-2/--ink/--strong/--muted/--faint/--line/--accent/
  --accent-weak/--ok/--bad/--warn/…/--track/--shadow/--radius`). Every hardcoded color in
  the component CSS was migrated to a token (`color:#fff` left literal — white text on
  accent/red fills is correct in both themes; only backgrounds moved). A **dark** palette
  is applied via `@media (prefers-color-scheme: dark) :root:not([data-theme=light])` **and**
  `:root[data-theme="dark"]`, and `color-scheme` is set per theme so native selects,
  date-pickers, and scrollbars follow. A small footer toggle flips `data-theme` and
  persists the choice in `localStorage` (`ab-theme`); default follows the OS.
- **Nav rail.** `<aside class="nav">` = brand, `.navlist` of the existing `.tab`
  `data-view` buttons (restyled as vertical nav items with icons — the original
  tab-switch JS is untouched), then a `.nav-foot` with the `#account` badge and
  `#theme-toggle`.
- **Controls moved.** The résumé/job/engine/quality/length/Tailor fields moved into
  `#view-review` as a `.controls` flex bar (only visible on Review). One CSS rule added —
  `.controls .ctrl.hidden { display:none }` — because `.controls .ctrl` out-specifies the
  utility `.hidden`, which the paste-a-posting ↔ saved-fixture toggle relies on.

**Verification.** Drove the running server headless with Playwright: all four tabs
screenshotted in **both** light and dark (nav highlight, cards, Track table + funnel bars +
date inputs, Profile forms, Discover panels all adapt); the job-source toggle confirmed
both ways (fixture picker ↔ paste box); a full `rules`-engine (zero-token) run
tailored → rendered the résumé + "why this tailoring" panel + Download-PDF button; browser
console clean; `tests/test_web_csrf.py` green (server behavior unchanged).

**Follow-up done (same session) — charts wired to the theme + redesigned.** The Discover
fit-trend SVG hardcoded its colors in JS (blue/gray/magenta); it's now redrawn entirely
through CSS-class tokens (`.fc-best`=--accent, `.fc-mean`=--muted, `.fc-bar`=--warn-line
dashed, grid=--line, dots surface-ringed), so it re-themes live with no JS color logic. The
redesign (dataviz method) adds a recessive 0/50/100 grid with reference labels, a
translucent area under the headline "best" series, a labelled swatch legend (identity never
color-alone), and per-point hover tooltips. The pre-score bar chart already used
`var(--accent)`/`var(--track)` (CSS-driven), so it themed already. Verified in both themes
(console clean, 6 hover targets, no label collision after dropping the redundant on-chart
threshold label).

**Then the remaining inline-colored JS was migrated too** (screening-answer status
pills/marks, the account ✓/○ rows, and the Gmail/app-password connect messages): every
hardcoded hex in the JS moved to a semantic token — `#0b7a3b`→`--ok`, `#b21f2d`→`--bad`,
`#e0a400`→`--warn-line`, `#b26a00`→`--warn-strong`, `#b0b0b0`→`--muted`, and the AI-drafted /
auto-from-profile purple got a **new `--ai` token** (`#6a4bd0` light / `#a48bf0` dark). No raw
hex remains in the JS. Verified via computed colors: pill dots resolve to the exact originals
in light and adapt in dark (amber `#b8862f`, green `#4cc282`, purple `#a48bf0`).

---

## 069 — Auto-apply loop: prepare-then-prompt mode with a per-application Apply gate

**Context.** The user asked for "the more autonomous looping mode: it should look for as
many matches as possible, then get started on them one by one and prompt me as it needs me
to start applying." Two runner modes already existed but neither matched: the dry-run
runner (`runner.run_queue`, gate off) prepares everything and prompts nothing; the armed
runner (gate on) submits everything up to a cap and prompts nothing. The ask is the
**middle** mode — prepare each match (tailor → PDF → dry-run fill) and then wait for a
per-application go-ahead before the real submit.

**Options decided with the user (AskUserQuestion).**
- **Prompt surface = the web UI queue** (not a terminal prompt or push notification):
  prepared applications stack up as "Ready to apply" cards in the Discover tab; the loop
  keeps preparing while the user decides.
- **Apply action = auto-submit that one** (not "hand me the filled browser"): clicking
  Apply arms a one-shot submit for just that application, reusing the per-click `SafetyGate`
  from decision 058 (armed, cap 1, KILL-halted, pre-submit required-field gated).
- **Breadth = search everything, and re-search ONLY when the current batch is drained**
  (user's exact constraint: "we don't run through tokens"), plus **a Stop control** to halt
  the loop while running.

**Decision / implementation.**
- **`autoloop.py` (new, pure, tested).** `auto_apply_loop(discover_batch, prepare_one,
  take_submit_requests, submit_one, should_stop, on_event)` — the ordering brain, fully
  injected (no browser/network/threading), returns `"stopped"` | `"caught_up"`. Each round:
  drain the user's queued submits FIRST (they're waiting), then discover a batch, then
  prepare each match — re-checking stop + new submit requests between every application, so
  an Apply click is never blocked by more than one in-flight preparation.
- **Token frugality by construction.** `discover_batch` calls
  `pipeline.discover_and_match(only_new=True)` (decisions 053/056), so each search returns
  only postings never judged before — no posting is ever re-judged, and when a search
  returns nothing new the loop reaches `caught_up` and stops rather than re-searching into
  the void. This is what satisfies the "don't burn tokens" requirement.
- **Single browser, serialized.** The web server drives one browser at a time (the existing
  `_TEST_STATE` slot). The loop worker OWNS that browser for its lifetime: preparation runs
  headless in the background (no window pops up), and user Apply clicks are **enqueued**
  (`_LOOP_SUBMITS`) and drained by the loop thread itself — never a second concurrent
  browser. `start_test_run`/`start_reapply` are refused while the loop runs (they'd fight
  for the browser). When the loop is idle, `queue_submit` falls back to the existing
  per-click armed `start_reapply(arm=True)`.
- **Web glue (`web.py`).** `_LOOP_STATE`/`_LOOP_LOCK`/`_LOOP_STOP`/`_LOOP_SUBMITS`;
  `_loop_worker` (builds the four callables from `pipeline`/`runner`, refuses to start
  without the Claude judge — never auto-applies on keyword rank alone, or without boards);
  `_loop_prepare` records a `dry-run` tracker row and, if clean (not parked), adds its id to
  the ready list; `_loop_submit` does the armed headless submit and drops the row from ready
  whatever the outcome; `start_loop`/`stop_loop`/`queue_submit`. Routes: `GET /loop/status`
  (state + a live-resolved ready list — a row since submitted or edited drops out
  automatically), `POST /loop/start|stop|apply`, all under the decision-062 origin guard.
- **UI (`INDEX_HTML`).** An "Auto-apply loop" panel at the top of the Discover tab: Start /
  Stop, a live status line (shared `.spin` while working + phase message + prepared count),
  and "Ready to apply" cards (company — role, fit · portal) each with a red **Apply ▶** that
  `confirm()`s then submits just that one. Polls `/loop/status` every 2s while running; a
  blocked preparation still routes to the existing parked panel (parking.py), so it never
  shows as "ready".

**Why this shape.** It reuses everything already proven — `run_testing_mode` for prepare,
the decision-058 per-click armed gate for submit, `only_new` discovery for token frugality,
the parked panel for blocked fills — and adds only a thin, pure ordering core plus web glue.
Full automation stays gated (Guideline #3): nothing submits without an explicit per-app
click, the KILL file and pre-submit gate still apply, and the default remains dry-run.

**Verification.** `tests/test_autoloop.py` (6) — ordering, stop-mid-batch, submit draining,
caught-up, events. `tests/test_autoloop_web.py` (5) — the real worker thread driven with
fakes (no browser/Claude/network): prepares a batch, populates the ready queue, reaches
`caught_up`, rejects a double start, routes `queue_submit` correctly. Served JS node-clean;
`/loop/*` endpoints driven live on the running server. Suite 331 pass (the one pre-existing
`test_mailbox` failure is environmental — the real linked inbox leaks into it — and predates
this change). **Flagged live step (needs the user):** one real loop run — Claude signed in,
boards configured — to watch discovery → prepare → a real Apply ▶ submit end-to-end. Not run
here to avoid spending Claude usage / a live submission uninvited (Guideline #3, NEXT_STEPS
token policy).

**Follow-up (2026-07-14) — "re-prepare postings I've already seen" opt-in (reuse cached
scores).** The token-frugal default (`only_new=True`) means once the loop has judged a
posting it is never re-considered, so there was no way to re-run auto-apply over postings
already dry-run (e.g. to re-tailor/re-fill after a résumé/PDF change). Added an opt-in
checkbox on the loop panel that starts the loop with `rescan=True`
(`start_loop(rescan)` → `_loop_worker(rescan)`).

*First cut re-judged (rejected):* rescan via `discover_and_match(only_new=False)` would
re-run the Claude fit judge on the whole set. The user's point — "there's not many reasons
we'd get a different fit score the second time" — makes that pure token waste, and with
`only_new=False` discovery also never empties (it would re-prepare forever without a manual
one-shot guard). *Chosen:* reuse the discovery snapshot's cached scores instead. New
`pipeline.cached_matches(resume, filters, profile)` returns the freshest snapshot's full
ranked matches — postings + cached fit scores (decision 037) — with **no board re-search and
no Claude re-judge**, and, unlike a normal cache hit, **without** applying `skip_seen` or the
seen-openings ledger, so already-prepared postings are included (that's the point). The loop
computes this pool once up front, serves it as a single bounded batch (re-prepare the set
once → `caught_up`), and if nothing is cached bails immediately with an actionable message
("Start a normal auto-apply loop first … then re-check while the cache is fresh," UI
principle #3) rather than silently doing nothing. So rescan spends zero judge tokens and only
re-does the preparation (tailor → PDF → dry-run fill).

*Reuse the tailored PDF too (2nd follow-up).* The user: "if nothing has changed in profile
since last time running the dry run, no need to retailor the résumé either." The tailor
(`tailor_resume`) is the remaining Claude call in `run_testing_mode`. mtime comparison is
unusable here: the fill auto-writes the profile file (learned screening answers via
`remember_answers`), so its mtime is always newer than the PDF even with no user edit. So we
stamp instead: `pipeline.tailor_stamp(resume, profile, jd)` hashes exactly the inputs that
determine the PDF — the résumé, the three profile *link* fields flowed onto the header
(LinkedIn/GitHub/portfolio), and the JD — and `resume_store.write_stamp` writes it to a
`<pdf>.stamp` sidecar. Deliberately hashing only the PDF-relevant fields means the fill's own
answer-learning never spuriously invalidates a reusable PDF mid-batch. Stamps are
cascade-cleaned by `delete_if_managed` and `prune`.

*Applies to every dry run, not just rescan (3rd follow-up).* Per the user ("this should also
take effect when doing dry runs"), the reuse is keyed on the run being a **dry run** rather
than on an explicit rescan flag: `run_testing_mode` reuses the stamped PDF (skipping tailor +
render) whenever `gate` is absent/unarmed and the stamp matches; a **real armed submit always
re-tailors**, so an actual submission never rides on a reused artifact (behaviour preserved).
This covers the web "Test run" button, the auto-apply loop's preparation, the autonomous
runner's dry-run mode, and the CLI `--apply-first` — re-running any of them on an unchanged
posting no longer re-tailors. (The earlier `reuse_if_unchanged` parameter is gone, folded into
the gate check.) Net: an unchanged rescan — and any repeated dry run of an unchanged posting —
spends **zero** Claude tokens end-to-end (no re-judge, no re-tailor); only the local re-fill
runs.

*Escape hatch (4th follow-up).* `run_testing_mode(force_retailor=True)` overrides the reuse and
regenerates the résumé even when the stamp matches, for the occasional "re-tailor anyway" —
surfaced as a second loop-panel checkbox ("Re-tailor from scratch") wired through
`start_loop(rescan, force_retailor)` → `_loop_worker` → the loop's `prepare_one`. Off by
default; the user expects to use it rarely.

Also fixed the loop/parked panels' accent bar overlapping the text (added `padding-left:18px`
— the `border-left:4px` had no inner padding). Verification: `tests/test_discovery_cache.py`
(+2 — `cached_matches` reuses the snapshot without re-search and ignores `skip_seen`; empty
when no fresh snapshot), `tests/test_autoloop_web.py` (+2 — rescan reuses cached scores without
ever calling `discover_and_match`; empty-cache bails with the actionable message), and
`tests/test_rescan_reuse.py` (new, 7 — stamp is stable/input-sensitive and ignores non-PDF
profile fields; sidecar roundtrip + cascade cleanup; `run_testing_mode` skips the tailor on an
unchanged dry run, re-tailors when a profile link changed, always re-tailors for an armed
submit, and `force_retailor` regenerates despite a matching stamp). Suite 332 pass (the one
pre-existing `test_mailbox` env failure predates this).

## 070 — Fix SPA Apply-reveal timing (Ashby "form did not load")

**Context.** Applying to a Ramp posting on Ashby (`jobs.ashbyhq.com/Ramp/<id>`) failed with
"⚠ ApplicationBot could not fill this application. Application form did not load within 25s at
<posting URL>" — no fields filled. The posting page and the application form are on **different
routes**: the form lives at `<posting>/application`.

**Root cause (reproduced live).** `_open_application_form` tried to reveal the form with a
single "Apply" click **before** its poll loop. Ashby is a SPA that mounts the "Apply for this
Job" control *after* `domcontentloaded`, so at click time `get_by_role(...).count()` was `0` —
the click never fired, the page never navigated to `/application`, and the loop then polled the
posting page (0 form fields) until the 25s deadline, reporting the timeout against the posting
URL. Probes confirmed: posting page = 0 fields; direct `/application` = 12 fields; the same
click path with even a 2s pre-wait navigates and finds 11 fields in 0.5s.

**Options.**
1. **Ashby special-case** — navigate directly to `<posting>/application`. Deterministic, but
   ATS-specific knowledge baked into the loader; doesn't help other SPA ATS with the same
   timing.
2. **Retry the reveal-click inside the poll loop** (chosen) — attempt the "Apply" click on each
   pass until the control appears, latched by `revealed` so it fires at most once. Fixes the
   whole *class* of "reveal control mounts late" bugs with no per-ATS code.

**Decision.** Option 2. The reveal-click moved from a one-shot pre-loop attempt into the poll
loop. Behaviour is preserved when a form is already rendered (fields present → returns before
any click). `apply.py` `_open_application_form` only; no interface change (Guideline #7).

**Verification.** Live: driven through the real `_open_application_form` against the reported
Ramp Ashby URL — returns `loaded=True`, frame URL `…/application`, 12 fields, no errors.
`tests/test_open_application_form.py` (2, fake page modelling the late-mounting button + form):
the regression test fails on the pre-fix code with the exact 25s "form did not load" error and
passes on the fix; a form-already-present case asserts no click fires. Rest of suite green
(301 pass excluding the pre-existing environmental `test_mailbox` failure).

## 071 — Draft short, optional "Why <Company>?" prompts (Ramp "Why Ramp?" left blank)

**Context.** A Ramp dry-run (after decision 070 fixed the form loading) filled every field
except **"Why Ramp?"**, which the applicant would want answered. On Ashby the field is a short,
**OPTIONAL** single-line `<input type=text>` labelled exactly "Why Ramp?".

**Root cause.** Three gates each rejected it:
- `is_open_ended("Why Ramp?", is_textarea=False)` → False: not a textarea, and the fallback
  needs `len > 25` **and** an `_OPEN_ENDED` phrase ("describe", "tell us", …) — "why ramp?" is 9
  chars and contains none.
- It matched no `_COMPANY_SPECIFIC` phrase (all are "why do you want to work", "why us", "why
  this company", … — none catch a bare "Why <Company>?"), so `is_company_specific` was False.
- It's **not required**, so `freetext_answer`'s `required and is_draftable_required(...)`
  fallback (decision 067) didn't apply either.
So `freetext_answer` returned `("", "")` — unanswered. (Being unrecognized as company-specific
also meant it was eligible for structured mapping/caching, a latent mis-map risk.)

**Decision (`answer_bank.py`).**
1. `is_company_specific` also returns True for any prompt that simply opens with "why " (plus a
   bare "why"/"why?"). The company name is dynamic and can't be enumerated, but a "why …" prompt
   is inherently employer/role-specific — so it is excluded from structured mapping
   (`_classifiable`) and from the answer bank (`valid_mapping`), and is never cached.
2. `is_open_ended` treats a company-specific question as draftable even when it's a short,
   single-line input (before the length/keyword heuristic).

Result: "Why Ramp?" is now drafted by the grounded weak-model path (résumé + company + JD) — the
pipeline already constructs the resolver with `company=p.company` and `jd=jd.body`, so the
`company_specific and not (company or jd)` guard passes. The draft is not cached.

**Behaviour change (Guideline #7, deliberate).** An OPTIONAL "Why <company>?" that decision 067
left for the user is now auto-drafted. This is the requested fix; `test_required_draft.py` was
updated to encode it (and still asserts a genuinely arbitrary optional short field stays blank,
drafting only when required).

**Verification.** Live probe of the real Ramp `/application` form enumerated its fields and
confirmed "Why Ramp?" is an optional single-line input now classified open-ended +
company-specific + non-classifiable. `tests/test_determinism_gates.py`: gate assertions +
end-to-end `freetext_answer` drafts with company context and declines without it (never banked).
`tests/test_required_draft.py`: updated contract. Suite 303 pass (excl. the pre-existing
environmental `test_mailbox` failure + slow Workday). Not driven against a live submit (dry-run,
Guideline #3) and no live Claude call spent uninvited — the draft path is proven with a stubbed CLI.

---

## 072 — LinkedIn job alerts as a discovery source (email-forwarded into the bot inbox)

**Context.** The user asked whether their personal Gmail could be wired in so the bot could parse
their LinkedIn job alerts. LinkedIn's saved searches are already tuned to what they want, so the
alerts are a high-signal stream the pipeline currently ignores.

**Constraint that shapes everything.** There is no compliant way to read LinkedIn *jobs* directly:
the API is partner-gated and `linkedin.com/jobs/view` is robots-disallowed, so scraping it fails
Guideline #4. `linkedin.py` already reached this exact conclusion for profile data (its compliant
path is the user-requested data export). But an alert email **sent to the user** is the user's own
data — parsing their own inbox raises none of those issues.

**Options considered (ingest).**

| Option | Cost | Access granted |
|---|---|---|
| Gmail OAuth read-only on the personal account | ~10min one-time Google Cloud setup (no OAuth client exists — the bot inbox links via IMAP app-password, so decision 065's path has never been exercised) | Standing read access to a personal inbox |
| IMAP app-password on the personal account | ~2min; reuses the proven bot-inbox code path | **Full read/write/delete** on personal mail — fails Guideline #5 |
| **Gmail filter forwards alerts → bot inbox** (chosen) | ~3min of clicking, no code | **None.** Bot only ever sees forwarded alerts |

**Decision.** Forwarding. A filter on `jobalerts-noreply@linkedin.com` in the personal account
forwards to the linked bot inbox (`profile/mailbox.yaml`, git-ignored), which `mailbox.py` already reads. This needs
**no new auth, no second link slot, and no change to `mailbox.py` at all** — the second link slot
scoped at the start of this task was dropped once forwarding was chosen. Gmail's auto-forward
preserves the body verbatim and the original `From` header, so both the LinkedIn links and
sender-matching survive. Verified the bot inbox currently receives zero LinkedIn mail (probe over
the live IMAP link), confirming a forward is required rather than already-present mail.

*Residual manual step (Guideline #8).* Creating the Gmail filter cannot be scripted: an IMAP
app-password cannot manage filters, which need the Gmail API's `gmail.settings.basic` scope — and
acquiring that scope would reintroduce exactly the personal-inbox access this decision avoids. The
step is one-time and documented above; the cost of scripting it is strictly worse than the cost of
doing it once. Gmail's forwarding-address confirmation code *is* automatable from our side — it
lands in the bot inbox and `extract_verification` already parses it.

**What alerts actually yield: leads, not applyable postings.** The email links to
`linkedin.com/comm/jobs/view/<id>`, which redirects to a LinkedIn job page — not an ATS. So
`detect_ats_from_url` returns `"other"`, `bridge_aggregator_postings` dead-ends with
`auto_applyable=False`, and the body is only the card snippet (title/company/location, no JD).
Recovering the JD or the true apply URL from LinkedIn would require the scraping ruled out above.

**Staged build (approved).**
- **(A)** `LinkedInAlertSource` reads the bot inbox, parses alert cards → `Posting(ats=
  "linkedin_alert", …, extra={"snippet_only": True})`. Shaped directly on `AdzunaSource`, which is
  already a snippet-only, redirect-linked, bridged source — the pattern exists and needs no new
  abstraction.
- **(B)** A **company→ATS-board resolver** (grepped for; does not exist today) matches each lead's
  company to its public Greenhouse/Lever/Ashby board — all three already supported with full JD and
  a fillable apply URL — so the lead re-enters the pipeline as an auto-applyable posting. Coverage
  will be partial: the resolver is a fuzzy company-name → board-token guess.

**Reasoning for the staging.** A alone produces leads a human triages, which cuts against the
fully-automated goal (Guideline #0) — it is a stepping stone, not a destination, and is built first
only because it is independently verifiable (Guideline #6). B is what makes the source earn its
place. The honest open question, recorded for reassessment after A: **what LinkedIn alerts add over
Adzuna**, which already aggregates many boards and is already bridged. The gain is that the user's
saved searches are pre-tuned; the loss is materially worse data on arrival. If A's lead quality
does not beat Adzuna's recall on the same filters, B should not be built.

**Status.** Approach approved; no code written yet. Blocked on the user creating the Gmail filter
(a real alert corpus is needed to build the parser against real markup rather than assumed markup).

---

## 073 — Any GitHub repo job board is drop-in config, not code

**Context.** The user asked whether we could scrape GitHub repo job boards. We already did:
`CuratedListSource` (#031) pulls two SimplifyJobs `listings.json` feeds off
`raw.githubusercontent.com`. The real question was which *other* repos to add — and whether
adding repos is the change that matters.

**Measured before building.** Probing the live feeds:

| Stage | Count |
|---|---|
| Active postings in the two Simplify feeds | 3,459 |
| Pass `_CURATED_ATS` | 1,130 |
| Actually resolved + judged (`max_resolve`) | **40** |

The two live `vanshb03` boards contribute 111 fillable postings, of which **103 are new** (only 8
overlap Simplify) — a ~9% wider pool feeding a stage that already discards 97% of what it fetches.
`Ouckah`/`coderQuad` are dead (404); `speedyapply` is README-markdown only.

**Conclusion.** More feeds ≈ zero marginal applications. **The funnel neck — 40 of 1,130 — is the
binding constraint, not feed count.** So this decision buys *optionality*, not breadth: dropping in
a board is now free when a specific board matters, and **no extra repos ship enabled by default**.
The real yield came from #074 (widening the neck) and from `max_resolve` itself, which is the
Claude-judge budget knob.

**Options considered.**

| Option | Verdict |
|---|---|
| Extend `early_career` with `feeds:` | **Chosen** — backwards compatible, no migration, `kinds` still names the built-ins |
| New top-level `github_boards:` block | Rejected — cleaner conceptually, but makes `early_career.kinds` legacy and needs a migration path for no functional gain |
| Hard-code the vansh feeds as new built-ins | Rejected — bakes in a choice the user should make; the drop-in mechanism subsumes it |

**Schema.** A feed is a bare string (built-in name, or a raw URL) or an explicit `{name, url}`.
Bare URLs are named `<owner>/<repo>` so they read cleanly in logs and in the discovery-cache
fingerprint. **No per-feed field mapping exists or is needed**: SimplifyJobs' `listings.json` is
the de-facto standard for these boards (vansh is a fork of it), feeds differ only in optional
extras (`category`/`degrees` vs `season`), and every field was already read with a `.get()`
default. A URL alone is the whole configuration.

**One source, not one-per-feed.** Making each feed its own `Source` would inherit per-feed error
isolation from `discover()` for free. Rejected: `max_resolve` would become **per feed**, so the two
built-ins alone would double the Claude judge cost and each added board would multiply it again — a
silent behaviour change (Guideline #7). Keeping one source preserves **global ranking** across all
feeds (pick the 40 most relevant overall) and keeps `max_resolve` a whole-run budget.

**Consequence: a bad feed fails loudly.** With one source, a broken drop-in feed would otherwise
silently shrink results — the "ignored my input" failure UI Principle #5 calls a bug. So
`_listings()` validates each feed and raises `DiscoveryError` naming the feed, the URL, and the fix
(a 404/repo-page URL, or a non-listings schema, reports the missing keys). `discover()` still
isolates the source, so the rest of the run proceeds. Trade-off accepted: a typo'd custom feed
costs the built-ins for that run — correct, because it is a config error the user fixes once, and
`fetch_json` already retries transient failures 3×.

**Web UI.** `readDiscForm()` rebuilt `early_career` from scratch on save, so without a matching
field it would have **silently wiped `feeds`** on every settings save. Added the field (reusing the
existing `area()`/`linesOf` pattern from `keywords`/`career_sites`), which is also the natural home
for "paste a board URL".

**Verified.** Live, no stubs: a YAML config carrying a `vanshb03/New-Grad-2026` URL →
`build_sources` → `discover` produced 6 full-JD Postings, 0 errors, with the custom feed merged
alongside the built-in and named `vanshb03/New-Grad-2026`. Plus `tests/test_curated_feeds.py`
(13 tests: drop-in, merge/dedup, global `max_resolve` budget, both bad-feed errors, config
coercion, fingerprint name).

---

## 074 — Resolve Workday + SmartRecruiters JDs in the curated feeds

**Context.** `_CURATED_ATS` gated curated listings to Greenhouse/Lever/Ashby. Measured against the
live feeds, that discarded **755 active Workday** and **298 SmartRecruiters** postings — from feeds
we already fetch, pointing at ATSs the Apply stage can already submit to (`pipeline._is_fillable`
explicitly allows `workday`; `workday.apply_workday` is a complete backend with recipes).

**Root cause.** The gate was never about *fillability* — it was about *JD resolution*.
`_CURATED_ATS` listed exactly the ATSs with a hand-written `_resolve_jd` helper. Anything else was
dropped rather than emitted with an empty body.

**The spike (which reversed my recommendation).** I flagged upfront that Workday renders
client-side and might need Playwright. **That was wrong, and testing it before building is what
caught it.** Workday still ships schema.org JSON-LD in the initial HTML, so the existing enrichment
cascade (#047) resolves it on a plain GET. Running the repo's own `enrich.fetch_full_jd` against 10
real Workday postings: **10/10 resolved via the `json-ld` tier**, 2–11k chars each, no browser and
no LLM call (no `llm=` → it stops at the free JSON-LD/CSS tiers).

| Approach | Result | Verdict |
|---|---|---|
| `enrich.fetch_full_jd` (existing cascade) | 10/10 live, json-ld tier, free | **Chosen** — one branch, no new subsystem |
| Workday `/wday/cxs/` JSON API | 15/15 live, but *less* text than JSON-LD | Rejected — bespoke tenant/site URL rewrite for a worse body |
| Playwright | not tested | Rejected — the spike proved it unnecessary |

SmartRecruiters needed only a tuple entry: `_resolve_smartrecruiters_jd` already existed and was
simply never reachable.

**Flagged (Guideline #2).** SmartRecruiters is fillable but `apply.detect_ats` doesn't know it, so
it routes to **generic autofill** rather than a dedicated backend (consistent with #030) — less
proven than the Workday path. Accepted deliberately by the user when scoping.

**Failure mode preserved.** A resolver failure still returns `''`, so the posting degrades to a
title-only body and still flows — it does not kill the run (Guideline #7).

**Verified.** Live end-to-end: real config → `build_sources` → `discover` returned 6 postings /
0 errors including a **Workday** posting (Northrop Grumman, 5,521-char real JD) and a
SmartRecruiters posting (2,382 chars). Both clear `_is_fillable` and route correctly
(`workday` → `apply_workday`, `smartrecruiters` → generic). Nothing was submitted (Guideline #3).

---

## 075 — Mailbox secrets never render; the env-vs-link test pins an unlinked path

**Context.** `test_load_config_needs_all_three` failed on the developer's machine while passing in
CI, and its failure output printed the user's **real Gmail app-password**.

**Root cause — two independent bugs.**

1. **The test was environment-dependent.** `load_config` prefers a stored **link** over the
   environment (decision 057) and defaults to `path=profile/mailbox.yaml`. The test passed an
   `env` dict but no `path`/`backend`, so on any machine with a linked mailbox `load_link` read
   the real file + keychain and returned the live config — never reaching the env dict. It passed
   only on an unlinked box.
2. **Any repr of a `MailboxConfig` leaked a live credential.** The failing assert printed the
   config; the same would happen in any traceback or log line. Bug (1) is what fired it, but (2)
   is the actual exposure and would recur on the next failure that prints a config.

**Not a product bug.** Link-over-env precedence is the intended, documented behaviour (057/065).
Only the test and the model's rendering were wrong.

**Decisions.**

| | Choice |
|---|---|
| Test isolation | Pass `backend=_FakeKeyring(), path=_link_path()` — the idiom the rest of `test_mailbox.py` already used and which this one test predated (it sits above the helpers). No new fixture invented. |
| Secret rendering | `password` / `refresh_token` / `client_secret` → `field(repr=False)` on the dataclass. |
| Rejected | Fixing only the test: closes the one known leak path and leaves the exposure live for every other failure that prints a config. |

**Scope of the repr change.** Values stay fully usable in code and `dataclasses.asdict()` is
unaffected — they simply never appear in `repr`/`str`. Non-secret fields still render, so repr
stays useful for debugging. `link_status()` remains the safe, explicit view (it already asserted
`"password" not in st`). Behaviour change flagged and approved (Guideline #7): debug output no
longer shows those three values.

**Verified.** `test_config_repr_never_prints_secrets` guards all three fields against a future
edit silently undoing the masking, and asserts the values still work. The isolation fix was
**mutation-checked**: with `_env_config` stubbed to drop the all-three requirement, the test still
fails — so it was isolated, not neutered. Suite **351/351 green** (was 349 passed / 1 failed, this
being the pre-existing failure).

**Related.** The bot-inbox address was scrubbed from `DECISIONS.md`/`NEXT_STEPS.md` before the
069–074 commit: it was not present in HEAD, and it is contact detail that must not enter git
(Guideline #12) — the repo must carry no user's data.

---

## 076 — Agentic nav fallback + host-keyed nav recipes; bot walls reported as refusals

**Context:** A dry-run "failed because it couldn't find the application" (tracker row 21 —
SmartRecruiters, 0 fields filled). The ask: let a Claude agent watch, get to the application, and
save that so future applications through a similar site aren't blocked. Investigating the actual
run — rather than building to the description — showed **one symptom over three distinct causes**,
only one of which the requested feature addresses.

**What was actually wrong**

| # | Cause | Evidence | Fix |
|---|---|---|---|
| 1 | `detect_ats` didn't know SmartRecruiters → `generic` | the gap decision **074 already flagged** | a `smartrecruiters` branch (+ `_ats_from_frame`) |
| 2 | The reveal only matched `/\bapply\b/i` | the real page says **"I'm interested"** ×5, "apply" ×0 | `_REVEAL_CONTROL`, anchored so **"Not interested"** can't match |
| 3 | **The real blocker** — the site *refuses* us | live drive: **HTTP 403** + DataDome wall, page named **our own egress IP** | `_bot_wall_evidence` → report a refusal, never call an agent |

Cause 3 was invisible to tests and to the tracker row; it appeared only by driving the real URL and
reading the screenshot. The first detector still missed it: the wall is served by
`geo.captcha-delivery.com` **into an iframe while the host page's body is empty**, so a main-frame
text scan returned `""`. It walks every frame now (text signals + vendor hosts).

**Decision**

1. **Deterministic first** (free): SmartRecruiters detection + a reveal pattern that isn't
   hardcoded to the word "apply".
2. **Bot walls are refusals, not missing forms.** A wall produces a precise error ("the site
   refused the request… ApplicationBot will not try to evade the block") and **suppresses the
   agentic fallback**: a Claude worker drives the same browser from the same IP into the identical
   wall, so it would burn tokens to fail — and aiming an agent at a bot wall is evasion
   (Guideline #4). The misleading timeout error is dropped when a wall is found.
3. **Agentic nav + learned recipes** (the ask), mirroring decisions 061/063 so there is **one
   mental model**: `nav_agentic: true` in `profile/safety.yaml`, **off by default**; a
   Claude+Playwright-MCP worker over CDP reaches the form **once**; `nav_recipes.py` stores
   `{host: {url_suffix, reveal_labels}}` — **PII-free and committed**, so a clone inherits every
   site already learned. **Replay is always on and free; only learning a new host is gated.**

**Why these shapes**

- **Host is the key.** Every SmartRecruiters posting shares `jobs.smartrecruiters.com`, so learning
  *one* posting unblocks the site — precisely "future applications through a similar site are not
  blocked". Per-posting keys would learn nothing reusable.
- **Distil by DOM diff, not by reading the agent.** Like 061: we observe what *navigated* and what
  *vanished*, so we never parse opaque MCP element refs, and the loop stays offline-testable.
- **A recipe records the route only** — a path suffix and control labels. No answers (those are
  re-resolved per user by `AnswerResolver`), so the shared library is PII-free by construction.
- **Refuse to guess.** `_distil_nav` verb-filters vanished controls (a dismissed cookie banner must
  never become a recipe replayed on every posting) and returns **nothing** rather than a wrong
  recipe when the route is opaque. `is_shareable_host` keeps loopback/private hosts out of the
  committed library — not hypothetical: the live drive learned `127.0.0.1` from a local fixture.
- **The nav worker is forbidden to fill.** Its prompt is the inverse of the Workday worker's:
  navigation is the job, filling/uploading/submitting/account-creation are hard-barred, so the
  fallback can never touch an answer or reach the submit path.

**Options rejected**

| Option | Why not |
|---|---|
| Agent-only (no deterministic fix) | The first run on *every* site costs a Claude call, and the reported posting stays blocked until the learner works. |
| On by default | Spends tokens without asking and diverges from how `workday_agentic` is gated (061/063). |
| Ask per site | Breaks the no-human-in-the-loop goal (Guideline #3) and stalls autoloop runs. |

**Verified** (driving the real thing, not only tests):

- **The live Claude-over-MCP step left flagged by 061/063 is now actually driven** — a real Claude
  Code + Playwright MCP worker attached to our browser over CDP, opened the fixture form, and the
  route was distilled to `"Join our team"` (cookie-banner decoy correctly excluded); 5 fields
  filled, nothing submitted. Replay then opened it with **the agent asserted to run exactly once**.
- The **real posting** now reports `smartrecruiters blocked automated access … 'captcha-delivery.com'`
  instead of a misleading 25s timeout, with the "arm the agent" advice correctly withheld.
- The bot-wall guard is **mutation-checked through `run_apply`**: neutering `_bot_wall_evidence`
  makes the test fail (an earlier version of that test passed vacuously — it called
  `_open_application_form`, which never invokes the fallback — and was rewritten to drive the real
  branch).
- 18 new tests; fixtures reproduce the real posting **and** the real iframe-over-empty-body wall.
  Suite **369/369**.

**Honest limit / open item.** The SmartRecruiters fix is **not confirmed end-to-end**: this build
environment's egress IP (an AWS address) is the exact IP DataDome named, so every live attempt is
403'd here regardless of the code. Causes 1 and 2 are proven against committed fixtures of the real
page; whether they alone unblock that posting from a **home network** is unverified — and if
SmartRecruiters walls the user's IP too, no amount of nav work fixes it and the correct behaviour is
the refusal message this decision adds. Re-drive from the user's machine to settle it.

---

## 077 — Bot-walled applications are parked as `bot_wall` and retried later

**Context:** Decision 076 taught the run to *detect* a bot wall, but not what to *do* with one. The
request — "flag applications that restrict because they find bot activity so we can go back and do
them later" — exposed that 076 left the wall detectable but unroutable. The user's **own live run**
proved it in production data: real tracker **row 21** read `status='dry-run'`,
`blocked_kind='captcha'`. Both fields were wrong, and both were 076's doing.

**The two bugs**

| # | Bug | Why it happened | Fix |
|---|---|---|---|
| 1 | An IP block was parked as a **solvable CAPTCHA** ("solve it in the open browser") | `classify` scanned `"captcha" in " ".join(errors)`; the wall's vendor host is literally **`captcha-delivery.com`**. There is no puzzle, and a headless run has no browser to solve it in. | Structured `ApplyReport.bot_wall`, classified **first** |
| 2 | A posting we were **refused** on showed as **"ready to apply"** | A walled run never reaches submit → `submit_state` stayed `"dry-run"`, and `web.py` marks **any** dry-run row ready | `_record_run` records `blocked` when `bot_wall` is set |

**Decision**

1. **`ApplyReport.bot_wall`** carries the evidence as a field, and `parking.classify` reads *that*,
   **before** the CAPTCHA branch. Deliberately **not** prose-matching our own error text —
   prose-matching is precisely what produced bug 1.
2. **`parking.BOT_WALL`** is a first-class kind that is **resumable by time, not by the user**:
   `resolve=""` (no setting fixes it), verb **"Try again"**. `/parked` and `_reapply_worker` are
   kind-agnostic, so the flag/list/retry loop the request asked for needed **no new plumbing** —
   the runner lists it and the UI offers it automatically.
3. **Copy corrected wherever the new kind broke it** (Guideline #11, UI Principles #3–#4): the
   tracker note now says **"Refused: … the site refused automated access"** instead of
   `"Dry-run: 0 field(s) filled"` — the exact line that made row 21 unreadable to the user; the
   runner's header no longer claims every parked row is "waiting on you — resolve" (a wall waits on
   the **site**) and each line carries its own verb; the web card no longer calls a refusal a
   "site error".

**Why `Submit for real` stays on the card.** It looks like a dead button on an unreachable posting,
but it isn't: if the block has lifted, the armed retry fills *and* submits — which is exactly the
"go back and do it later, for real" path. Removing it would delete the feature being asked for.
A wall never risks a bad submit: with no form served, the pre-submit gate has nothing to click.

**Why not auto-retry on a timer.** Deferred, not rejected — the request was to *flag so we can go
back*, and the one-click retry delivers that. Automatic retry needs a backoff policy and a rule for
when a site is hopeless (Guideline #4: repeatedly hammering a host that has refused us is exactly
the abusive request pattern we must not build). Logged in NEXT_STEPS as a decision to make.

**Verified — on the user's real data, not only fixtures:**

- Row 21 re-driven live → `blocked` / `bot_wall` / `blocked by captcha-delivery.com` (was
  `dry-run` / `captcha`).
- The Resolve card **rendered and screenshotted in the real web UI**, reached by a real click on
  the Discover tab: headline "The site blocked automated access", the precise note, and both retry
  buttons. JS syntax-checked with `node --check` (after filling serve-time placeholders).
- Both fixes **mutation-checked**: neutering the `bot_wall` classify branch, and reverting the
  `blocked` status, each make their test fail.
- 5 new tests; suite **374/374**.

**Known gap.** An upsert never clobbers user-owned fields, so row 21's *pre-existing* note still
reads "Dry-run: 0 field(s) filled". Only newly-inserted rows get the "Refused" note. The
`blocked_kind`/`blocked_detail` carry the truth and drive every surface (runner, card, API), so the
stale note is cosmetic — and weakening the never-clobber-user-edits rule to fix it would be a worse
trade (Guideline #7).

---

## 078 — The tracker's Source URL is the link itself; editing moves behind an ✎ toggle

**Context.** The Source URL cell rendered as a text `<input>` with a small `↗` link beside it
(decision 068's table). The URL *looked* like plain text: the only clickable target was a 12px
glyph, while the obvious affordance — the URL — did nothing. The user asked for the URLs in the
Track table to be actual links instead of plain text boxes.

**Decision.** The cell now renders the URL as an `<a>` whose text *is* the URL: click anywhere on
it to open the posting (`target=_blank`, `rel=noopener noreferrer`). An `✎` button beside it swaps
in the same text input; committing it saves through the existing `saveCell` and returns to the
link. The link view is used only for an `http(s)` value — anything else (empty, or a stored
`javascript:`/`data:` string) renders as the input, which both gives a manually-added row a way to
type its URL and keeps a non-http scheme from ever becoming a clickable payload. That http-only
guard is inherited unchanged from the `↗` it replaces.

**Why keep an editing path at all.** The cell has always been editable and a manually-added row
needs a way to set its URL — making it a bare link would silently delete that (Guideline #7). The
toggle keeps the capability while letting the default state be the one the user actually wants.

**Why re-render only the cell.** `saveCell` writes "Saved ✓" into the row *after* its `await`, so
re-rendering the row on save would detach that span and land the confirmation (or the error) on a
dead node — a silent failure (UI Principle #5). `render()` rewrites the `.urlcell` only.

**The layout bug this surfaced.** A long URL is unbreakable text, so as a link it inflated the
column to **583px** — past the 220px default and the resize handle — squeezing every other column
(Company 83px). The old `<input>` never did this: an input has a small intrinsic width. Fixed with
`contain:inline-size` on the link, which keeps its text out of the table's intrinsic width so
`table-layout:fixed` honours the `<col>` width again. Verified by measuring the real table: the
fixed version reproduces the pre-change geometry **exactly** (table 1370px, Source URL 98px, all
16 columns identical to baseline).

**Verified — driven in the real web UI on the user's real tracker, not fixtures:**

- All **18** rows render as `<a>` with the URL as its text, correct `href`, `target=_blank`,
  `rel="noopener noreferrer"`; **zero** inputs in the default view.
- **Edit round trip on real row 21**: `✎` → input pre-filled with the current URL → changed →
  `Saved ✓` → back to a link carrying the **new** href. The original value was then written back,
  so `applications.db` is unchanged.
- Column geometry measured against a `git stash` baseline (above). The resize handle behaves
  identically to baseline under the same synthetic drag.
- Suite **375/375**.

**Known gap.** The column renders at 98px, so the link shows truncated (`https://j…`) with the full
URL on hover — the same truncation the input had, and pre-existing (the table is `width:auto` +
`table-layout:fixed`, which squeezes columns proportionally; the 220px default is not honoured even
at baseline). Widening the default or shortening the label to something readable (`smartrecruiters.com/…/87644936`)
is a separate change, not folded into this one.

---

## 079 — Skip `aria-hidden` inputs when filling (react-select requiredInput mirror hijacked its dropdown)

**Context.** A dry run against **SpaceX — New Graduate Engineer, Software (Starlink)**
(Greenhouse) left the **School** field on its `Select…` placeholder while Degree and Discipline
filled correctly. The run's `report.json` nonetheless listed School as *filled*
(`value="The Pennsylvania State University"`, `source=resolver`) — so the field looked done but
would submit empty. The user asked why, and for a fix general enough to keep hands-off runs
correct.

**Investigation (driven, not assumed).** A read-only DOM capture of the live form showed School is
**not** misclassified — its input is a proper `role="combobox"` react-select, structurally
identical to Degree/Discipline. Reproducing the real `_fill_page` against the live form revealed
the actual mechanism: Greenhouse renders each react-select as **two** inputs sharing one label:

1. the real combobox input (`role="combobox"`), and
2. an **`aria-hidden="true"` `requiredInput` shadow** — empty `type`, `tabindex="-1"`, `role=""` —
   used only for the browser's native required-field validation.

The résumé value `The Pennsylvania State University` matches no option on open (the async list
indexes it under *"Pennsylvania State University-…"* — the decision-033 article-prefix problem), so
`_fill_combobox` **defers** its pick to the batched Claude decision and returns `None` via a
`continue` that **does not add the label to `done`**. The fill loop then reaches the hidden mirror;
its empty `type` is in `_TEXTLIKE` and its `role != "combobox"`, so it's classified as a **free-text
field** and `loc.fill()`'d with the school name — writing into an invisible input that shows
nothing, but **marking "School" done**. In round 2 the deferred combobox pick would normally be
recommitted, but the label is already `done`, so it's skipped and the real dropdown is never
committed. Degree/Discipline escaped this only because their values matched an option literally on
first open (`option:hint`/`option:literal`), resolving in round 1 before the mirror was reached.

**Decision.** Skip any input with `aria-hidden="true"` in `_fill_all_fields`, right after the
existing `type in ("hidden", …)` skip. An `aria-hidden` input is removed from the accessibility
tree — it is never a field a human fills — so skipping it is safe in general, and it stops the
mirror from claiming the label. With the mirror gone, round 1's combobox defer leaves the label
open, and round 2 recommits the real selection. The fix is one guard plus one extra property in the
per-control `evaluate`; it is general to **every** react-select field on Greenhouse (and any ATS
using the same requiredInput pattern), not a SpaceX special-case.

**Rejected.** Marking the label `done` when a combobox defers its pick — that would suppress round
2's recommit, which deliberately relies on the deferred label staying *un-done* so it can be
revisited. The mirror, not the defer, is the defect.

**Verified.**
- **Live SpaceX form, before:** `_fill_combobox` called once for School → returns `None`; School
  recorded `control='text'`, `source='resolver'` (the mirror); the visible dropdown stays `Select…`.
- **Live SpaceX form, after:** `_fill_combobox` called **twice** (round 1 defers → `None`; round 2
  recommits) → School filled `control='combobox'` with a real Penn State option; Degree/Discipline
  unchanged.
- **Regression test** (`tests/test_required_input_mirror.py` + fixture
  `fixtures/apply_forms/react_select_required_mirror.html`) reproduces the dual-input structure and
  a deferred pick. It **fails without the guard** (`FilledField(control='text', source='resolver')`)
  and **passes with it** (`control='combobox'`, `source='option:claude'`, the mirror never typed
  into).
- Related fill suites green: combobox, two-pass, multipage, fillability, lever-labels,
  resolver-corpus.

**Known limit.** In the live after-run the round-2 pick committed via the `substring` tier rather
than the Claude pick (the batched pick didn't literally match, so Phase 2c's substring fallback
landed "Pennsylvania State University"). That still commits a *real* option (not plain text), so the
field submits validly; tightening the async school picker to prefer the main-campus option every
time is the separate decision-033 follow-up, not folded in here.

---

## 088 — Track "Re-run": choose reuse vs re-tailor; re-tailor from a saved per-PDF JD sidecar

**Context.** The user wanted, when re-running a dry run, to decide whether to **reuse** the stored
résumé or **re-tailor** a fresh one. The Track "Re-run ▶" always reused (`_reapply_worker` re-drove the
fill with `app['resume_path']`, never re-tailoring). Re-tailoring needs the job description — and the
tracker never persisted it per posting. This is exactly the structural gap decision **086** identified
(point 3) and whose follow-up asked to "persist each posting's JD beside its PDF"; this decision
implements that.

**JD source — saved sidecar, not re-fetch.** Offered the choice, the user picked saving the JD over
re-fetching it from the posting URL (which re-scrapes and fails on expired/changed/blocked postings).
Each dry-run now writes a `<pdf>.jd` JSON sidecar (`{body, meta}`) beside the tailored PDF —
`resume_store.write_jd/read_jd/has_jd`, mirroring the existing `.stamp` sidecar and pruned/deleted with
its PDF. `run_testing_mode` stores it in both the reuse and fresh branches, so even a reused-PDF posting
gets its JD saved.

**Shared tailor helper.** `run_testing_mode`'s tailor→render→write→stamp→ATS-check block is extracted
verbatim to `pipeline.tailor_and_render(resume, profile, jd, company, role, url)` (returns the PDF path)
so the re-run path can regenerate a résumé the same way the first dry-run did. The helper does not write
the JD sidecar — its caller owns that (a re-tailor already has the JD).

**Flow + UI.** `_reapply_worker(retailor=True)` reads the saved JD (`resume_store.read_jd`), calls
`tailor_and_render` against the user's **current** base résumé/prompt/layout (so it picks up decision
087's logic fingerprint), writes a fresh PDF at the per-posting path, then fills. `retailor` threads
`/parked/reapply` → `start_reapply` → `_reapply_worker`. Track shows "Re-run ▶" (reuse; fast, no Claude)
on every dry-run row, plus "Re-tailor ▶" **only when `has_jd`** is true (surfaced per row in `/track`),
so the button appears only when it can run offline. Decision 086's `Cache-Control: no-store` on
`/track/resume` is what makes the freshly-overwritten PDF actually display instead of a cached copy.

**Rejected / caveats.** Re-fetching the JD (rejected, above). Pre-existing dry-runs have no sidecar (they
predate it), so they show reuse-only until re-dry-run once; a re-tailor with no saved JD returns an
actionable error, not a crash. Complements cursor's `scripts/retailor_tracked.py` — it can now read the
sidecar to re-tailor ANY saved-JD row, not only rows whose JD is still in the rolling discovery cache.

**Verified.** JD sidecar write/read/`has_jd` round-trip; `tailor_and_render` with `tailor_resume`+
`render_pdf` stubbed writes the PDF + stamp and returns its path; `/track` returns `has_jd` per row
(true only for a seeded posting). Drove the Track tab headless (Chromium): with one seeded JD sidecar,
exactly one "Re-tailor ▶" button renders — on that row (id 22), not on JD-less rows (17 "Re-run"
buttons, 1 "Re-tailor"). Page JS passes `node --check`. Full suite (minus the unrelated
`test_nav_recipes.py` collection error) = **357 passed**; the only failures are the 2 pre-existing
`test_parking.py` bot_wall tests. One test needed updating for the new kwarg — `test_parking.py`'s
`start_reapply` mock lambda now accepts `retailor=False` (the endpoint passes it); the cross-origin
behavior it checks is unchanged.

---

## 087 — Guarantee tailoring changes show after a restart+rerun: fingerprint the tailoring source

**Context.** Follow-up to the user's request: make sure *future* résumé-tailoring changes show
immediately on a rerun, not just this one. Decision 082 had folded the tailoring logic into the
reuse-stamp, but partially: the Claude prompt was content-hashed (`backends.SYSTEM_PROMPT`, so prompt
edits auto-invalidate), while the PDF layout was gated on a hand-bumped `pdf.LAYOUT_VERSION` integer,
and changes to `length` (e.g. `line_chars`, the per-section caps), `catalogue` (selection), or
`tailor` (orchestration) were not captured at all. Two live footguns: forget to bump `LAYOUT_VERSION`
after a render change, or edit any of those other modules, and the stamp stays identical → the dry-run
reuse gate serves the stale PDF — exactly the "nothing changed" symptom, re-armed.

**Decision.** Replace the prompt-string + manual-int scheme with `pipeline._tailoring_logic_fingerprint()`:
a SHA1 over the **source bytes** of every module that determines a tailored PDF's content —
`backends`, `catalogue`, `length`, `pdf`, `tailor`. It is folded into `tailor_stamp` as a single
`logic` key (alongside résumé/links/JD). Any edit to any of those modules changes the fingerprint, so
the stamp changes, so a re-prepare re-tailors — with nothing to remember to bump. `LAYOUT_VERSION` is
removed as redundant.

**Why import-time, and what "immediately" means.** The fingerprint is computed once as a module-level
constant, against the loaded (running) modules' source files. Python doesn't hot-reload edited source
into a running process, so a tailoring change only takes effect after the dashboard restarts anyway;
pinning the fingerprint at import keeps it consistent with the code that will actually run. So the
precise guarantee is: **edit tailoring code → restart the dashboard → rerun**, and changed postings
re-tailor. Computing per-call instead would risk a fingerprint that says "changed" while the process
still runs the old code — a false signal — so import-time is deliberate.

**The honest caveat.** This guarantees the *reuse gate* re-tailors; it does not change that the normal
loop skips already-seen postings (`only_new=True`, decision 086). So after a logic change: NEW postings
get the current logic automatically; already-tracked postings refresh via the "Re-prepare postings I've
already seen" rescan (which now re-tailors on any logic change) or `scripts/retailor_tracked.py`. This
is intentional — re-tailoring the whole backlog every run would blow the token budget `only_new` exists
to protect.

**Trade-off.** Fingerprinting whole module sources over-invalidates: a comment-only edit to any of the
five modules forces a re-tailor on the next rescan. Accepted deliberately — the user prioritized
"changes always show" over saving an occasional Claude call, under-invalidation is the dangerous
direction (stale résumé), and a real armed submit always re-tailors regardless of the stamp.

**Rejected.** (a) Keep hashing `SYSTEM_PROMPT` + bump `LAYOUT_VERSION` by hand — the footgun that
caused this. (b) A curated list of specific constants (margins, `line_chars`, caps) instead of whole
sources — precise but itself a list to keep in sync with every future change; source-hashing needs no
maintenance.

**Verified.** The live `_TAILORING_LOGIC` equals a hand-recomputed SHA1 of the five module source
files, and appending any byte changes it; the covered set is exactly `[backends, catalogue, length,
pdf, tailor]`. The Stripe row 22 on-disk stamp (written before this change) no longer encodes the
current logic, so its reuse gate now re-tailors rather than reusing. `pytest -k "pipeline or stamp or
pdf or tailor or length or web or track or resume_store"` = 24 passed.

---

## 086 — Why a re-tailor still showed the old résumé: seen-skip, browser cache, un-persisted JDs

**Context.** After decisions 081/082, the user re-ran a Stripe dry run several times and restarted the
dashboard, and the Track tab still showed the old résumé. Investigated by evidence, not guesswork —
and it turned out to be **three independent causes** stacked on top of each other.

**Cause 1 (primary) — the normal loop never revisits a seen posting.** `web.py._loop_worker`'s default
path calls `pipeline.discover_and_match(..., only_new=True)`, which by design returns only postings
never judged before (decisions 053/056 — spend judge tokens only on genuinely new openings). An
already-tracked posting is therefore never re-processed, so `run_testing_mode` never runs for it and
its stored PDF is never rewritten. Proof: every stored Stripe PDF's mtime (newest 11:24) predated the
code change (11:56), despite the reruns. Re-tailoring a *seen* posting only happens on the opt-in
**rescan** path (“Re-prepare postings I've already seen”), which with decision 082's stamp now
re-tailors automatically when the logic changed. This is working-as-designed, not a bug — but it's the
reason “just rerun” didn't help, so it's documented.

**Cause 2 (latent bug, fixed) — `/track/resume` was browser-cacheable.** The endpoint streamed the PDF
with a `Content-Disposition` but no `Cache-Control`, and its URL is a stable `?id=<row>`. When a
posting is re-tailored the PDF is overwritten *in place* (same deterministic path, decision 029), so
the URL is unchanged and the browser's PDF viewer serves the cached old bytes — a re-tailor looks like
it did nothing. Fixed by adding `Cache-Control: no-store` to that one response.

**Cause 3 (structural limitation) — JDs aren't persisted per PDF.** The tailored PDF and its stamp are
stored per posting, but the job description that produced it is not. So a row can only be re-tailored
offline while its JD is still in the rolling discovery cache — here just 1 of 18 tracked rows. Logged
as a follow-up: persist each posting's JD (or the tailored structured `TailoredResume`) beside its PDF
so ANY tracked row can be re-rendered after a future logic change, independent of cache freshness.

**Decision / tooling.** Added `scripts/retailor_tracked.py` (Guideline #8 — the manual “delete the PDF
and rerun” becomes one re-runnable command): re-tailors tracked rows whose JD is still cached using the
CURRENT code, overwriting the stored PDF + stamp in place. Idempotent (skips a row whose stamp already
matches unless `--force`); `--id` / `--company` / `--all`; `--backend` defaults to `auto` (the real
Claude path) with `rules` for a free deterministic pass. Rows it can't re-tailor (JD rolled out of
cache) are reported, never silently skipped (UI principle #3 / Guideline #11).

**Verified end-to-end on the user's real Stripe posting** (row 22, Technical Support Engineer,
`--backend auto`): regenerated via claude-code (3761 → 3791 bytes, 12:46). Extracted the new PDF's text
— all three earlier changes now render: experience order `Ninth Wave → Jaguar → Kumon` (software above
tutoring, decision 081 ordering), preserved metrics (`1M+ transactions / 100% faster`, `618 postings /
0 errors`, `~11,100 lines`, `3 releases / ~3,500 lines`, `1000% more storage`, `$170K+ sales`,
`20% wait-time cut`), 34pt margins, 1 page. `pytest -k "web or pipeline or track or resume_store or
stamp"` = 13 passed.

**Rejected.** (a) Making the normal loop re-tailor seen postings — would re-judge/re-tailor the entire
backlog every run, blowing the token budget the `only_new` design exists to protect; rescan is the
right opt-in. (b) Cache-busting the résumé URL with a query param instead of `no-store` — the file is
tiny (~4 KB) and always local, so `no-store` is simpler and can't go stale.

---

## 082 — The reuse-stamp must include the tailoring logic (prompt + PDF layout), not just the data

**Context.** After decision 081 changed the tailoring prompt and PDF margins, a Stripe dry run showed
the **same résumé** in the tracker — the changes appeared to have no effect. They weren't broken; the
tailoring never re-ran.

A dry run reuses a posting's already-tailored PDF (`profile/tailored/<company>-<role>-<hash>.pdf`)
when the `.stamp` sidecar beside it still matches (`pipeline.run_testing_mode`, decision 069) —
skipping both the Claude tailor call and the PDF render, so a rescan of unchanged postings spends zero
tokens. The stamp is computed by `pipeline.tailor_stamp`, which hashed only:

    resume.model_dump()  +  [linkedin, github, portfolio]  +  jd.body

i.e. only the **data**. The tailoring **logic** — the Claude prompt and the PDF renderer — was absent.
So editing the prompt or the layout left the stamp byte-identical, the stored PDF matched, and the
dry run served the stale artifact. Every future prompt/layout tweak would have silently no-op'd the
same way until the résumé or JD changed.

**Decision.** Fold the logic into the stamp payload:
- `backends.SYSTEM_PROMPT` — the tailoring prompt, hashed by content, so ANY prompt edit invalidates
  every cached PDF automatically (no manual bump).
- `pdf.LAYOUT_VERSION` — a new integer constant, hand-bumped whenever the PDF layout changes (margins,
  fonts, header, spacing). Started at `2` to invalidate PDFs built under the 081 margin/header change.
  (Renderer code can't be content-hashed as cheaply as a string constant; a version int is the
  low-friction equivalent.)

Now any change to how a résumé is tailored or rendered changes the stamp, so the next dry run
re-tailors on its own — the user never has to remember the "Re-tailor from scratch" checkbox after a
logic change.

**Scope / safety.** This only affects the **dry-run preview** reuse path. A real armed submit already
always re-tailors (decision 069), so no submission ever rode on a stale artifact. The existing
`force_retailor` escape hatch (loop panel checkbox, `web.py`) is unchanged and remains the way to force
a regenerate when the *data* is identical but you want a fresh Claude pass anyway.

**Rejected.** (1) Leaving the stamp as-is and relying on the checkbox — puts the burden on the user to
remember after every prompt tweak, and silently produces wrong previews when they forget (the exact
bug reported). (2) Deleting the whole `profile/tailored/` cache on every code change — throws away
valid PDFs for postings whose data and logic are unchanged, defeating the zero-token rescan.

**Verified.** Toggling either `SYSTEM_PROMPT` (append a char) or `LAYOUT_VERSION` (+1) changes the
stamp hash while the data is held fixed; the stamp docstring/source now reference both. `pytest -k
"pipeline or stamp or testing or resume_store"` = 11 passed. Full suite = 357 passed; the only failures
(`test_parking.py` two `bot_wall` tests + `test_nav_recipes.py` collection error) are pre-existing and
unrelated — confirmed identical on a `git stash` clean tree.

---

## 083 — Track tab shows when each dry run ran via a dedicated `date_dry_run` column

**Context.** The user asked to see, on the Track tab, when they ran dry runs. The information was in
the database — every row carries `created_at`/`updated_at` (ISO datetime, `tracker._now()`) — but
neither is surfaced in the UI (`TRACK_COLS`, `web.py`). A dry-run row leaves `date_applied` blank (it
is only stamped when status flips to `applied`) and `date_discovered` records only when the posting
was *found*, not when its form was dry-run filled. So the Track tab had no answer to "when did I dry-run
this?".

**Decision.** Add a dedicated `date_dry_run` column, mirroring the existing `date_applied` mechanism
exactly:
- **Schema** (`tracker.py`): new `date_dry_run TEXT NOT NULL DEFAULT ''`, placed between
  `date_discovered` and `date_applied`.
- **Auto-stamp**: `add_application` stamps `date.today()` when the inserted status is `dry-run`;
  `update_application` stamps it when a row's status *flips* to `dry-run` and the field is still blank
  — the same first-time, stamp-once pattern `date_applied` uses. No `apply.py` change is needed: the
  Apply stage records dry-runs via `add_application`, which now stamps.
- **UI** (`web.py`): a "Dry-run" column in `TRACK_COLS` between Discovered and Applied, rendered as an
  editable `<input type="date">` like the other two dates. The `/track` API already returns the field
  (it does `SELECT *`).
- **Migration + backfill**: the `_connect` migration ALTERs the column in for pre-existing DBs and,
  on the same pass, backfills existing dry-run rows from `substr(created_at, 1, 10)` (the row's insert
  time = when the dry-run was recorded), so history shows a real date, not a blank.

**Semantics.** First-dry-run date, stamped once and stable — matches `date_applied` (which is the
first-applied date). A re-run keeps the original dry-run date; editing other fields never moves it.

**Rejected.** Surfacing the existing `updated_at` as the column (no schema change): rejected because
`updated_at` is bumped by *any* field edit on the row, so it drifts away from the true dry-run time as
soon as the user touches the row — it would answer "when did I last edit this", not "when did I dry-run
this".

**Verified.** On a temp DB: a fresh `dry-run` insert stamps today with `date_applied` blank; a
`discovered`→`dry-run` flip stamps it; an `applied` insert leaves `date_dry_run` blank. On the real
`applications.db`: the migration ran and backfilled all **17** existing dry-run rows from `created_at`
(Stripe 2026-07-16, Ramp 2026-07-14, Whoop 2026-07-13, …), **0 left blank**; `tracker.list_applications`
returns the `date_dry_run` key; the served page defines the "Dry-run" column and its date-input render
branch. `pytest tests/test_funnel.py tests/test_calibration.py tests/test_runner.py
tests/test_web_csrf.py tests/test_parking.py` = 50 passed, 2 failed — the 2 (`test_parking.py`
`bot_wall`) are pre-existing and unrelated (a stale `ApplyReport(bot_wall=…)` kwarg the current class
doesn't define), same family as the `test_nav_recipes.py` collection error; neither touches
`tracker.py`/`web.py`.

---

## 084 — "Dry-run" date shows the latest run; each posting expands to a per-run history log

**Context.** Follow-up to decision 083. The user wanted two refinements: (1) the "Dry-run" date should
show when a posting was *last* dry-run filled, not the first time; (2) each dry run should be visible as
"its own row."

**(1) Latest, not first.** `apply.py._record_run` upserts a posting by source URL; on a re-run the row
keeps `status='dry-run'`, so the tracker's stamp-once auto-stamp (which only fires when the status
*flips* to dry-run) never updates the date. So the update path now explicitly sets
`changes["date_dry_run"] = date.today()` on any dry-run outcome. A dry-run *event* is the only thing
that moves the date; a plain field edit in the Track UI does not (this is exactly why 083 rejected
surfacing `updated_at`, which any edit bumps).

**(2) Per-run history — a separate log, not one Track row per run.** The user was offered two shapes
and chose the log. The `applications` table stays **one row per posting** (its current state) — dedup
(`find_by_source_url`/`seen_source_urls`), the discovery→offer funnel, and the "ready to apply" loop all
depend on that invariant, and one-row-per-run would inflate the funnel's "Filled" count and scatter a
posting across duplicate rows. Instead a new append-only `application_runs` table records one row per
`_record_run` call:

    id, application_id, source_url, company, role, portal, outcome (dry-run|blocked|applied),
    resume_path, detail ("N field(s) filled (…); M need attention" + any blocker), ran_at (datetime)

It is never edited or deduped — a pure audit trail, indexed by `application_id`. `_record_run` computes
the fill summary once (reused for both the posting's `notes` and the run's `detail`) and appends a run
in a best-effort `try/except` so a logging failure can never sink an otherwise-complete run.
`delete_application` cascades to the log so no orphans linger.

**UI.** Track gains a "Runs" column: `N runs ▾` toggles an inline sub-row that lazy-loads
`/track/runs?id=` (timestamp · outcome badge · fill summary · résumé link), so the main table stays
light and details load only on expand; a posting with no runs shows `—`. `/track` attaches a
`run_count` per posting from a single `GROUP BY` (`tracker.run_counts`).

**Migration.** One-time seed of one run per pre-existing dry-run posting from `created_at` + `notes`
(prefix `"[auto] "` stripped so it reads like a live run). The seed `SELECT` references **only columns
the applications table actually has** (`src(col) = col if present else ''`) — an old DB may predate
`role`/`portal`/`notes`, and the parking migration test (a deliberately minimal old schema) caught the
naïve version failing with `no such column: role`. The seed runs after the column migration and only
when the runs table is first created.

**Rejected.** One Track row per run (the other option shown): literal but breaks dedup, inflates the
funnel, and clutters the table with duplicate postings.

**Verified.** Temp DB: an insert + a re-run of the same posting log **2** runs against **1** posting and
refresh `date_dry_run`, with the posting's `notes` byte-identical to before. Real `applications.db`:
seeded 17 runs (one per dry-run posting), `"[auto] "` prefix normalized off. `/track` returns
`run_count`; `/track/runs?id=` returns the newest-first log. Drove the Track tab headless (Chromium):
the "Runs" column renders, `1 run ▾` expands to `2026-07-16 11:29:29 · dry-run · 26 field(s) filled (1
native, 0 AI-drafted); 1 need attention · résumé ↗`, and a second click collapses it. Page JS passes
`node --check`. Full suite (minus the unrelated `test_nav_recipes.py` collection error) = **356 passed**;
the only failures are the 2 pre-existing `test_parking.py` `bot_wall` tests, confirmed identical on a
`git stash` clean tree.

---

## 085 — Keep one tailored résumé per posting; do not snapshot a résumé per run

**Context.** Follow-up to decision 084's run history. The user asked whether each dry run saves its own
résumé so it can be viewed per run — "unless they are the same".

**Finding.** We save **one** tailored PDF per posting, not per run. `resume_store.path_for` keys the file
on the posting URL alone (`sha1(source_url)[:8]`), so every run of a posting resolves to the same path
and `write_pdf` overwrites it. So: a re-run that **reused** the cache (decision 082 stamp matched —
unchanged JD/prompt/layout) produced a byte-identical résumé, and a re-run that **re-tailored**
overwrote the previous PDF at that path.

**Decision.** Keep this storage. Given the finding, the run-history "résumé ↗" link (→ `/track/resume?id=`,
the posting's current PDF) already shows what each run used **whenever the runs matched** — the common
reuse case, which is exactly the "unless they are the same" the user cared about. The only thing it can't
show is an older résumé that a later re-tailor overwrote, and the user accepted that.

**Rejected.** Content-addressed per-run snapshots — on each run, copy the résumé to
`<name>-<sha1(bytes)>.pdf`, store that per-run path on the run row, and serve it from a per-run résumé
endpoint. Identical résumés across runs would dedup to one file automatically (satisfying "unless they
are the same" for free) and differing re-tailors would each stay viewable. Rejected because it grows
`profile/tailored/` by one PDF per *distinct* tailoring per posting for a case the user doesn't need, and
would interact with `prune()`'s size-cap eviction (a run's link could 404 after eviction). No code
change: the run log's `resume_path` and the UI link already point at the single per-posting PDF.

---

## 081 — Tighten résumé tailoring: preserve existing metrics + slightly narrower margins

**Context.** The user reported the tailored résumés weren't surfacing enough quantifiable metrics,
and asked for slightly tighter formatting (less margin). A grep of the base résumé
(`profile/resume.yaml`, `examples/sample_resume.yaml`) showed the source is *not* the bottleneck: it
is dense with real figures — `1M+ transactions`, `~70% latency cut (2 min → 35–40s)`, `5× latency`,
`618 postings / 0 errors`, `40+ screening questions`, `30+ students`, `97% analysis-time cut`, etc.
So tailoring was dropping or softening numbers that already exist, not lacking raw material.

**Options considered.**
1. Loosen the anti-fabrication guard so the model can estimate/round numbers — **rejected**: violates
   Guideline #7 (truthfulness) and Agent Guideline #5/#3 safety posture; a fabricated metric on a real
   application is worse than a missing one.
2. Strengthen *preservation* of metrics already in the base résumé, leaving the "use only base numbers"
   guard intact — **chosen**.

**Decision.**
- `backends.SYSTEM_PROMPT`, QUANTIFY rule: the model's FIRST duty on every bullet is now to PRESERVE a
  base bullet's number (rephrase the words freely, never drop the figure). It's told to lead each entry
  with its most-quantified, job-relevant bullet, and — when the length budget forces a cut — to prefer
  metric-bearing bullets over metric-less ones. The prior guard is kept verbatim: use only numbers
  present in / safely implied by the base résumé; a truthful bullet with no metric beats a fabricated
  figure; never silently strip a metric the base résumé supports.
- `backends.SYSTEM_PROMPT`, ordering rule: experience is ordered by **relevance to the posting first**,
  not chronologically — a role whose domain matches the job outranks an unrelated one regardless of
  dates (the user's example: for a software-engineering job, a software role must sit above a tutoring
  role even if the tutoring role is more recent). Recency is only the **tiebreak** among
  comparably-relevant entries (most recent nearer the top); the same rule is stated for projects and
  activities. (The `RulesBackend` already sorts entries by relevance score with base-résumé order —
  conventionally reverse-chronological — as the index tiebreak, so it needed no change.)
- `pdf._Resume` margins reduced 42/40/42 → 34/34/34 pt (auto-page-break margin 40 → 34), widening the
  text column epw 528 → 544 pt.
- `LengthBudget.line_chars` default 100 → 103 to track the wider column, so the "fill the line, don't
  leave it half empty" instruction stays accurate (usable bullet width 516 → 532 pt, ~3% more).
- `pdf._fit_font` (new helper) fixes the **header spilling past the page sides**: `_contact` rendered
  the name and the `location | email | phone | links` line with a fixed-size `cell`, which neither
  wraps nor shrinks — when that line is wider than the column (sample résumé = 610pt vs 544pt usable)
  fpdf overprints it ~33pt past each page edge. `_fit_font` now picks the largest Helvetica size that
  fits the content width before drawing (name 20pt → floor 12pt, contact 9pt → floor 6.5pt).

No schema, interface, or module-boundary change — prompt text + three constants + one render helper.

**Verified.** Rendered `examples/sample_resume.yaml` and `profile/resume.yaml` through the PDF path:
sample renders 1 page at the new margins (`l/t/r = 34`, `epw = 544`) with the contact line auto-shrunk
9 → 8pt so it fits (542 ≤ 544pt) and the name held at 20pt; the profile contact already fit and stays
9pt. Confirmed wiring: `LengthBudget().line_chars == 103` and the budget prompt now says "one line
holds about 103 characters"; `SYSTEM_PROMPT` carries both the preserve-metrics and relevance-first
experience-ordering blocks. `pytest -k "pdf or tailor or length or render or contact or backend"` =
23 passed (nav-recipes module has a pre-existing unrelated collection error, ignored). The
metric-density and ordering improvements are prompt-quality changes observable on the next live
tailoring run, not unit-testable.

---

## 080 — A searchable combobox the batch declined still gets its round-2 typeahead Claude pick

**Context.** Decision 079 stopped School from submitting empty, but it then committed via the
`substring` tier — the non-Claude first-fuzzy-match fallback — instead of the Claude pick that is
explicitly told to prefer the primary/main campus. On the live SpaceX form the substring pick
happened to land on the right entry, but it takes *whatever the async lists first*, so it can commit
a **branch campus** ("…- Schuylkill Campus") over the main one. The user asked to make the picker
reliably prefer the main campus.

**Investigation (driven).** Read-only capture of the live Greenhouse School react-select showed it
is an **async search**, not a static list:

- On **open** (empty query) it returns the **first 60 schools alphabetically** — `Acadia
  University`, `Adamson University`, `Adelphi University`, … — never the applicant's school.
- Typing `Pennsylvania State` returns two options after ~3s: `Pennsylvania State University` and
  `Pennsylvania State University - Schuylkill Campus`. (A short 1.1s read returned **zero** — the
  async XHR hadn't come back yet.)

So round 1's batch pick is made over the alphabetical open list and **correctly declines** (no
option fits "The Pennsylvania State University"). The bug: `_resolve_pending` then added the label
to `picks_done`, and `picks_done` gated **both** Phase 1's static open-list Claude pick **and**
Phase 2b's article-stripped typeahead Claude pick. Round 2 therefore skipped Phase 2b — the path
built for async school pickers (decision 033), which types progressively shorter, article-stripped
queries and lets Claude pick the primary campus from the real per-query results — and fell through
to Phase 2c's substring fallback.

**Decision.** Split the gate. Phase 2b now runs on a new `gen_on` (generation enabled + a non-empty
value), **not** `use_claude` (which still ANDs in `label not in picks_done`). Rationale: Phase 2b's
candidates come from what the async search returns **per query**, which is unrelated to the open
list the batch already declined — so re-running it in round 2 is not a redundant re-ask. Phase 1's
static open-list pick stays gated by `picks_done`, so a genuinely static dropdown the batch couldn't
resolve is not re-asked (and in Phase 2b a static list filtered by the full value typically yields
zero options → no Claude call, so no extra cost there either). The primary-campus preference itself
already lives in the pick prompt (`'The Pennsylvania State University' → 'Pennsylvania
State University-Main Campus'`); this change just lets that prompt actually run.

**Rejected.** Freeing `picks_done` wholesale (only marking labels the batch *decided*) — that would
re-run Phase 1's static open-list pick every round 2 for any declined dropdown, spending Claude
calls to re-derive the same decline. Splitting the gate targets exactly the async case.

**Verified (live SpaceX form).** Driving the real `_fill_page` against the SpaceX Greenhouse
posting, `_fill_combobox` is called twice for School — round 1 defers (`None`), round 2 commits
`('Pennsylvania State University', 'claude')` — and School is recorded
`control='combobox'`, `source='option:claude'` with the **main campus** value. Before this fix the
same run committed via `source='option:substring'`; Degree/Discipline are unchanged.

**Verified (fixture).** New `fixtures/apply_forms/async_school_picker.html` mimics the real picker:
open list is alphabetical decoys, the full "The …" query is a prefix miss (zero options), and the
article-stripped query returns the two campuses **with the branch listed first**. The two-pass test
(`tests/test_async_school_pick.py`) drives the real `_fill_page`: without the fix School commits
`Pennsylvania State University - Schuylkill Campus` (`source=substring`, the wrong branch); with it,
`Pennsylvania State University` (`source=option:claude`, the main campus). Regression suites green:
combobox, two-pass, required-dropdown, multipage, fillability, lever-labels, determinism-gates.

## 089 — Dashboard deslop pass (ibelick/ui-skills `baseline-ui`): Track table + Discover labels

**Date:** 2026-07-16
**Status:** Accepted

**Context:** The user asked to "install github.com/ibelick/ui-skills and use it to improve
our dashboard UI." `ui-skills` is an npm CLI (`npx ui-skills categories|list|get`) that serves
design-guidance **skill docs** to an AI agent — not a code library. Our dashboard is hand-written
HTML/CSS/JS embedded in a zero-dependency stdlib `http.server` app (`web.py`), so there is nothing
to add as a runtime dependency. The useful move is to pull ibelick's own **`baseline-ui`** "deslop"
ruleset (`npx ui-skills get baseline-ui`) and apply it as an audit against the live UI.

**Method:** Launched the dashboard and captured all four tabs (Review, Discover, Profile, Track)
in light **and** dark via Playwright, then graded each against `baseline-ui` (typography, hierarchy,
consistent placeholders, badges over bare colored text, no uppercase body copy).

**Findings & scope:**
- **Review, Profile — already clean.** Sentence-case field labels, tidy forms, empty states with a
  clear next action. Left untouched (Guideline #7 / Karpathy "surgical" — no drive-by churn just to
  touch every tab).
- **Discover — real violation.** The auto-apply-loop reuse/retailor checkboxes rendered as ALL-CAPS,
  letter-spaced *paragraphs*: the global `label{text-transform:uppercase;letter-spacing:.03em}` bled
  onto the long descriptive `.loop-rescan` labels (whereas `.chkrow` and `.fld label` already opt out).
- **Track table — the worst slop.** `table-layout:fixed` + `width:auto` let the three native
  `<input type=date>` cells (each with a hard ~138px min intrinsic width) hold their width while the
  text columns were squeezed to 53–83px, truncating to `dr…` / `Te…` / `Consult…`. Status was bare
  colored text; empty cells were blank; every row showed three empty native date pickers.

**Changes (all in `web.py`, behavior-preserving CSS/HTML/JS):**
1. `.loop-rescan{text-transform:none;letter-spacing:normal;font-weight:400}` — descriptive checkbox
   labels now read as normal sentence text.
2. `.ttable{width:max-content}` — fixed layout now honors each `<col>` width and overflows into the
   existing `.twrap` horizontal scroll instead of squeezing text columns.
3. New `dateCell(app,key)` — dates render as plain `tabular-nums` text (muted `—` when empty); a click
   swaps in a real `<input type=date>` that saves on change and reverts on blur. Mirrors the existing
   `urlCell` idiom; removes the empty-picker-in-every-row noise and reclaims ~300px. Date column
   defaults trimmed 120→104.
4. Status badge — `statusCell` selects now carry `class="stcell st-*"`; `.ttable select.stcell`
   styles them as rounded pills tinted from their own status color via
   `color-mix(in srgb, currentColor 14%, transparent)`, still inline-editable.
5. Empty generic text cells get a `—` placeholder.
6. **Columns show/hide menu fixes** (surfaced when the user asked to add column hiding — the feature
   already existed but was half-broken). (a) The menu was anchored `left:0` from a button near the
   viewport's right edge, so it opened off-screen and got clipped; re-anchored `right:0; left:auto`
   so it opens inward. (b) Its checkboxes inherited `.trackbar input{flex:1;min-width:200px}` and
   stretched to 229px, shoving each label to the far edge; `.colmenu .menu input{flex:0 0 auto;
   min-width:0}` restores the natural 13px checkbox so label text sits beside it.

No save endpoints, editability, or data flow changed; no new dependency; the column show/hide, resize,
and reset machinery is otherwise untouched (each column already had a persist-per-browser visibility
toggle — these were just display bugs in that menu).

**Verified:** Playwright screenshots of all four tabs in light + dark. Track now shows full
Company/Role/Location, colored status badges (`dry-run` amber, `blocked` grey, `applied` blue) that
survive both themes, and `—` for empty cells. Programmatic check: 57 date buttons (19 rows × 3),
20 rendered as `—`, filled ones as `MM/DD/YYYY`, and clicking a filled one swaps to a working
`<input type=date>`. Discover checkboxes render as normal sentence case. Page JS `node --check` clean.

---

## 090 — Backfill the per-PDF JD sidecar so pre-existing rows show "Re-tailor ▶"

**Date:** 2026-07-16
**Status:** Accepted

**Context:** The user said they still couldn't find where to re-run a dry run with résumé
retailoring. The Track tab's "Re-tailor ▶" button (decision 088) renders per dry-run row only
when `resume_store.has_jd(resume_path)` is true — i.e. a `<pdf>.jd` sidecar exists. Checked
against the real `applications.db`: **all 17 dry-run rows had `has_jd=False`** (they predate the
086/088 sidecar), so only "Re-run ▶" (reuse) ever drew — the button the user was looking for was
never visible for any of their data. The CLI fallback `scripts/retailor_tracked.py` reads the JD
from the rolling discovery cache, but the cache had rolled over: only **1 of 17** rows (id 22)
was still cache-fresh. So 16 rows had no reachable re-tailor path.

**Decision:** The user chose to **backfill the missing sidecars** rather than always show the
button and re-fetch the JD live at re-tailor time — keeping the `has_jd` gate honest (button shows
only when a JD is genuinely on disk). Per the user's explicit follow-up, the backfill fetches a JD
**only when a sidecar doesn't already exist** (never overwrites an existing one).

**Implementation — `scripts/backfill_jd.py` (Guideline #8, idempotent):** for each
`dry-run`/`tailored`/`blocked` row missing a sidecar, obtain the JD **cache-first**
(`profile/discovery_cache.json` — no network, exact bytes), else **live re-fetch** of `source_url`
via `enrich.fetch_full_jd(url, llm=enrich.claude_llm_extractor)` (the same json-ld→css→llm cascade
discovery uses), then `resume_store.write_jd(resume_path, jd)`. A row whose JD can't be obtained
either way is reported **FAILED** (posting removed / nothing extractable), never silently skipped.
It does **no** tailoring and rewrites no résumé — it only attaches the JD so decision 088's button
can light up. `--id` / `--company` / `--all` select scope, mirroring `retailor_tracked.py`.

**Verified end-to-end on the real DB:** cache path (id 22, 4366 chars via cache) and live-fetch
path (id 6 Stripe, 4446 chars via `fetch:llm`) both write a valid sidecar; re-running id 22 reports
HAVE and leaves it untouched (idempotent); `--all` wrote 15, already-had 3, **failed 0** — every one
of the 17 dry-run rows now has `has_jd=True`, so "Re-tailor ▶" renders for all of them on the next
`/track` load.

**Flagged honestly:** one sidecar came back thin — MARGO (id 9, 93 chars) — too short to tailor
meaningfully; re-tailoring that row will produce a weak résumé until a fuller JD is captured. The
other 16 range 1.2k–13k chars. Complements `retailor_tracked.py`, which can now also use `read_jd`
for any saved-JD row rather than only cache-fresh ones.

---

## 091 — Track table columns are drag-to-reorder (persisted per browser)

**Date:** 2026-07-16
**Status:** Accepted

**Context:** The user asked to be able to drag and reorder the Track-table columns. The table
already remembered per-browser column **widths** (`ab_track_colw`) and **visibility**
(`ab_track_hidden`), and supported edge-drag **resize** and a show/hide menu — but column **order**
was hard-coded by the `TRACK_COLS` array with no way to change it.

**Change (all in `web.py`, no endpoint/schema/data change, no new dependency):**
1. New localStorage pref `ab_track_order` — an array of column keys — with `saveOrder()`, mirroring
   the existing width/hidden prefs.
2. `orderedCols()` resolves render order: emit `TRACK_COLS` entries in the saved order, then append
   any key not present in that order in its canonical `TRACK_COLS` position. This makes a stale saved
   order self-healing — if a column is later added to or removed from `TRACK_COLS`, the saved order
   can neither drop nor duplicate a column; an unknown saved key is ignored and a new column appears
   at its default spot.
3. `visibleCols()` now filters `orderedCols()` (was raw `TRACK_COLS`), so ordering and hiding compose.
4. Each header `<th>` is `draggable="true"` with `cursor:grab`. `attachColDrag(th,key)` wires HTML5
   `dragstart`/`dragover`/`dragleave`/`drop`/`dragend`; `.dragging` dims the grabbed header and
   `.dropto` draws an inset accent insertion bar on the hovered target. `drop` calls `reorderCol(from,
   to)`, which removes the dragged key and reinserts it directly before the drop-target key, saves,
   and re-renders.
5. "Reset columns" now also clears the order (`TRACK_ORDER=[]; saveOrder()`), alongside the existing
   width/visibility reset.

**Why drag doesn't fight resize:** the right-edge resize handle's `mousedown` already calls
`preventDefault()`, which suppresses the browser's native drag start — so grabbing the edge resizes
and never begins a column reorder, and grabbing anywhere else on the header reorders.

**Verified:** `node --check` clean on the served page JS; `orderedCols`/`reorderCol` unit-tested in
node — sequential moves land in the expected order, and a stale saved order (`["role","GONE","status"]`
against a 5-column set) yields no dupes, no drops, with the unlisted columns appended in canonical
position; `import applicationbot.web` clean; server boots and serves `/` HTTP 200.

---

## 092 — ATS feedback loop: grade each tailored draft against a JD-derived "dummy ATS", re-tailor the drops

**Date:** 2026-07-16

**Context:** The user pointed at `github.com/sauravhathi/atsresume` and asked whether tailoring
should optimize for the posting's ATS — specifically to "tailor our resume and test against a dummy
ATS based on the job description that we pulled."

**What atsresume actually is (evaluated, not adopted):** a Next.js/Tailwind résumé-*builder* UI.
Its "ATS scoring" is only an outbound link to resumego.net — **no scoring logic to reuse.** Its
résumé JSON schema and single-column template we already cover with `models.py` / `render.py` /
`pdf.py`, and `ats_check.py` already verifies our PDF's text layer is parseable. Nothing worth
importing; pulling in a JS/Next stack would add a runtime we don't need.

**The real gap:** we already had both halves of the ask — `ats_score.py` (deterministic 0–100
pre-score from the JD, used to order the judge queue) and `ats_check.py` (post-export PDF
keyword coverage) — but tailoring was **one-shot**: the dropped-keyword signal only became an
advisory note, never fed back into a re-tailor. The loop was open.

**Options put to the user (Agent Decision Framework #2 — affects how résumés are tailored):**

| ATS fidelity | Loop |
|---|---|
| Reuse `ats_score` (deterministic) | Bounded retry on gaps |
| Claude-extracted JD requirements (tokens/JD) | Always iterate to a target score |
| Both, layered | Score-only, no auto-loop |

**Chosen:** deterministic keyword+knockout extraction (no per-JD Claude call) + **bounded retry**.

**Change:**
1. New `applicationbot/ats_requirements.py` — the deterministic dummy ATS:
   - `extract(resume, jd_text)` → `Requirements(keywords, knockouts)`. `keywords` =
     `relevance.skill_terms(base)` ∩ JD mentions — the **same honest universe** `ats_check` uses,
     so a "missing" keyword is always a skill the candidate genuinely has (the loop can never
     invent one; true gaps stay the Claude fit judge's job). `knockouts` = years + degree
     (evaluated against the résumé via `ats_score`'s existing parsers) plus security-clearance and
     citizenship/no-sponsorship, detected with conservative regexes and flagged unverifiable.
   - `grade(resume_text, requirements)` → `AtsGrade(keyword_score 0–100, present, missing,
     knockouts)`; `passed` is False on any failed knockout, mirroring an ATS auto-reject.
2. `tailor.py` — `tailor_resume` extracted its single pass into `_one_pass(emphasis)` (unchanged
   length-enforce + `fit_to_pages` + notes), then grades the rendered draft and, **for
   non-deterministic backends only**, re-tailors up to `_MAX_RETAILOR = 2` times feeding the exact
   `missing` keywords back — breaking the instant a pass fails to shrink the gap (never regresses,
   never wastes Claude calls). Grade notes are appended to `relevance_notes` (so CLI/web/pipeline
   all show it with no per-surface change) and the structured verdict rides on
   `TailorResult.ats_grade`.
3. `backends.py` — `tailor(..., emphasis=None)` on the Protocol + both backends; `_user_message`
   appends an ATS-screen instruction listing the dropped keywords when emphasis is set. The rules
   engine accepts and ignores it (deterministic — a retry changes nothing).

**Why grade the rendered text, not a fresh PDF each pass:** the dropped-keyword failure is a
length-budget trim, which the rendered résumé text reflects exactly and far faster than a per-pass
PDF export. The existing `verify_pdf` stays as the final post-export text-layer check (font
mangling, readability) after export.

**Verified:** `tests/test_ats_requirements.py` (6 cases: keyword universe is candidate-truthful,
years/degree knockouts evaluate correctly, clearance/citizenship flagged unverifiable, grade scores
coverage, `passed` gates on a failed knockout). Full offline end-to-end `tailor_resume` on the real
xAI senior-data JD via the rules engine → grade 100/100, years knockout evaluated, grade note
surfaced in `relevance_notes`. Stub-backend test of the loop itself: first pass drops the skills
section, grader finds `SQL, REST APIs` genuinely missing (Python/Kafka counted present from summary
text — the grader sees what the ATS sees), feeds exactly those back as emphasis, retry includes
them, improved draft adopted → 100/100. 60 related tests (tailor/backend/ats/cli/web) pass, no
regression. (Pre-existing unrelated collection error in `test_nav_recipes.py` from the in-progress
`apply.py` edits — untouched here.)

---

## 095 — Track table shows Claude token spend per application, split by activity

**Context.** The user asked: when filling out applications, show in the tracker table how many
tokens (if any) each one used, so they can see how much Claude is doing for them — tailoring, form
entry, etc. Nothing captured token usage before; the Claude CLI's `usage` data was discarded.

**Where the data comes from.** Every Claude call in the pipeline funnels through the single choke
point `backends.run_claude_cli`, which invokes the CLI with `--output-format json`. That envelope
already carries `usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`) and `total_cost_usd` — the function just returned `result` and threw
the rest away. Confirmed against a live call before designing. So capture needed no new API,
subprocess, or dependency — only to stop discarding what we already receive.

**Two design decisions the user made** (asked before building, per the Agent Decision Framework —
this touches data storage):
1. *Judging tokens.* The fit judge is **one batched Claude call over many candidates**
   (`matching.judge_fit_batch`), so its tokens are not cleanly per-application. The user chose to
   keep the per-row table strictly per-application (tailoring + form-entry + per-posting
   enrichment) and show batched judging/discovery spend as **one separate aggregate**, never
   divided across rows.
2. *Metric.* One row shows the total tokens; clicking it fans out into the input/output split and
   the per-activity breakdown.

**Mechanism (ambient attribution — no call-site signature churn).** New `usage.py` holds two
context variables: `_activity` (WHAT the call is doing) and `_posting` (WHICH application it belongs
to, its source URL). `run_claude_cli` gained an `activity=` arg (default None → inherit the ambient
activity) and, after parsing the envelope, calls `usage.record(env, activity=...)`, which
best-effort writes one row to a new append-only `usage_events` table. Attribution points are just
two functions: `pipeline.tailor_and_render` wraps its tailoring call in `for_posting(url)` (the
backend tags `activity="tailoring"`), and `apply.run_apply` pushes `for_posting(source_url,
"form-entry")` across its fill (so the untagged answer-drafting calls inherit form-entry) — both
keyed on the same posting URL the tracker upserts on. Enrichment/salary/impact calls are tagged
explicitly. The batched judge runs during discovery, outside any `for_posting` block, so its events
land with `posting_key=''` → the discovery aggregate. Best-effort throughout: a token-logging
failure can never break a working Claude call.

*Two follow-ups folded in same day:* (a) the **Workday agentic worker** runs Claude via a separate
`--output-format stream-json` subprocess (not `run_claude_cli`); its final result line carries the
same cumulative `usage`, so `workday._record_agent_usage` parses it and records under form-entry —
it runs inside `run_apply`'s `for_posting`, so it attributes to the Workday posting. (b) the
**salary market-estimate** call in `run_testing_mode` sits between tailor and apply, outside the two
leaf wraps, so it was wrapped in `for_posting(p.url)` to attribute to the posting instead of the
discovery bucket. The only remaining discovery-bucket-by-design per-app-untied Claude use is a
standalone Tailor-tab/CLI tailor not tied to any application.

**Storage + reads.** `usage_events` (id, posting_key, activity, model, the four token counts,
cost_usd, ran_at) is additive and migration-free (`CREATE TABLE IF NOT EXISTS`).
`tracker.usage_by_application()` groups by source URL into per-activity sub-maps + totals;
`usage_discovery_summary()` aggregates the unkeyed events; `delete_application` cascades usage by the
row's source URL so a re-discovered URL never inherits stale tokens.

**UI.** The `/track` payload attaches each application's `tokens` (joined by source_url) and a
top-level `usage_discovery`. Track tab: a new **Tokens** column shows the compact total (e.g.
"3.8k"), and clicking expands an inline sub-row with the in/out split + per-activity table; a
one-line "Discovery & judging (all-time)" figure sits above the table and expands to its activities.

**Alternatives rejected.** Threading `(text, usage)` return values through ~15 call sites across 6
modules (rejected — ambient contextvars touch nothing). Dividing judge tokens per-candidate
(rejected by the user as a misleading estimate). Storing dollar cost as the headline metric
(rejected — it's a subscription, so cost is an as-if-API estimate; tokens are the honest unit,
though the cost is still captured in the events for reference).

**Verified.** One real `claude` call recorded 390 in / 4 out attributed to a posting under
`tailoring`. `tests/test_usage.py` (7 cases, synthetic envelopes): activity routing, block-default
inheritance, explicit-override-wins, discovery bucket for unkeyed calls, no-usage/all-zero no-op,
delete-cascade, and the Workday `stream-json` parser (records the result line under form-entry;
no-result-line is a no-op). Workday + usage suites 39/39. The live `/track` HTTP endpoint serves the per-app `tokens` + `usage_discovery`
payload. Served-page JS `node --check` clean; all touched-module imports + the
backend/matching/salary/enrich suites green. (Pre-existing unrelated failures remain: the
`test_parking.py` `bot_wall` cases and the `test_nav_recipes.py` collection error, both from an
in-progress `apply.py` branch — fail identically on the clean tree.)

## 096 — Verify a combobox commit actually stuck; fire the "filled" UI notification before the screenshot/archive

**Context.** Two problems from a live SpaceX (Greenhouse) dry run:

1. **School reported filled but was blank.** The run's `report.json` recorded
   `School → "Pennsylvania State University" [option:claude]`, yet the browser field was empty.
   So the decision-080 pipeline *chose* the right option, but the commit never landed — and we
   reported a success that didn't happen (violates Guideline #7/#11: report what actually
   happened).
2. **The "Filled — review" notification lagged the fields by ~a minute.** All fields were
   visibly populated long before the notif bar flipped.

**Root causes (traced from `report.json` + the code path).**

1. `_commit_option_text` reopens the async School combobox, retypes the query, waits 900 ms,
   finds the option whose text is exactly the Claude pick, clicks it, and returns `True`. But
   Greenhouse's School search is **async** (decision 080): its XHR results re-render the option
   list. When results arrive *as* the click lands, the option node is replaced under the cursor —
   Playwright's click still "succeeds" (it hit the coordinates) but react-select never fires its
   select handler, so the field stays empty. The function returned `True` on the click alone,
   never checking that a value rendered.
2. `on_filled` (which drives the web "Filled — review" phase) fired only *after*
   `page.screenshot(full_page=True)`, answer-bank learning, the tracker write, and the archive.
   On a long form the **full-page screenshot dominates** (many seconds), so the notification
   trailed the fields by the combined cost of all four. The web filled-panel renders only
   `report.summary()` text — it never shows the screenshot image — so nothing the user sees
   required the screenshot to run first.

**Fix.**

1. `_commit_option_text` now **verifies the commit**: after clicking the exact-text option it
   waits 200 ms and reads the control's rendered value via `_VALUE_JS` (react-select
   `single-value`). Empty ⇒ the click was swallowed ⇒ retry the whole open→type→click, up to 3×;
   if it never renders a value, return `False` so the caller records the field **unfilled**
   (surfaced for the user) instead of a false `option:claude` success. (If the value can't be
   read back, assume the click stuck rather than loop forever.)
2. `run_apply` now fires `print(report.summary())` + `on_filled(report)` **immediately after the
   fill finishes** (right after the in-browser done banner), *before* the screenshot, learning,
   tracker, and archive. Those still run — just after the UI has already been told "Filled —
   review." One cosmetic trade-off: the filled-panel summary no longer includes the
   "[answer bank] saved…/learned…" line (added by `_persist_learning`, which now runs after the
   callback); the final "done" `_set` after the browser closes still carries the complete summary.

**Verification.** Both changes are surgical to `apply.py`; `applicationbot.apply/web/pipeline`
import clean; the apply/fill/parking/required/combobox suite is 72 passed (the only failures are
the pre-existing `test_parking.py` `bot_wall` cases + `test_nav_recipes.py` collection error from
an unrelated in-progress branch, which fail identically on the clean tree). The school-commit fix
needs a live Greenhouse async School field to confirm end-to-end — flagged for the next real dry
run.

---

## 097 — A user-clicked submit during the dry-run review pause is tracked as a real `applied` application

**Context.** The user's rule, verbatim: *"keep as dry run unless bot or human clicked the submit
button, in which case track it as a regular application."* A dry-run already records itself to the
tracker (`_record_run`, decision 024) — but as a `dry-run` row, deliberately distinct from a real
submission (Guideline #3: dry-run does everything *except* submit).

The bot-armed click was already handled: when a `SafetyGate` is armed, `_attempt_submit` clicks
Submit, sets `report.submitted`, and `_record_run` writes an `applied` row (method `auto`). The gap
was the **human** click. A dry-run fills the form and leaves the browser open for review
(`pause=True`, a web `hold` event or a terminal `input()`). `_record_run` and the archive run
*before* that pause. So if the user reviewed the filled form and clicked the site's own Submit
button, nothing re-checked the page — the row stayed `dry-run` and the real application went
untracked.

**Options considered.**

| Option | Verdict |
|---|---|
| Count every "decided-to-apply" dry-run as `applied` | Rejected — a dry-run explicitly did **not** submit; calling it applied breaks the safety model and lies about outcomes. |
| Add a `would-apply` marker distinct from `applied` | Rejected — the user's rule is specifically about a *click*, not a decision. |
| Detect an actual submit (bot or human) and only then record `applied` | **Chosen** — matches the rule exactly; the bot half already worked, so only the human-in-the-pause half was new. |

**Change (surgical to `apply.py`, one message tweak in `web.py`).**

- New `ApplyReport.manual_submit` flag — distinguishes a human click (method `manual`) from the
  armed bot (`auto`) and from a never-submitted dry-run.
- New `_detect_manual_submit(page, frame, report)`: on the first appearance of a submission
  confirmation it flips the report to `submitted` / `submit_state="submitted"` / `manual_submit`.
  It reuses `_confirmation_evidence` — the same URL (`confirmation`/`/thank`) or body
  confirmation-text signal the armed path trusts. **Positive confirmation only** — never the weaker
  "form gone" heuristic — so merely navigating away during review can't be mistaken for a submit.
  Idempotent: a no-op once the report is already submitted.
- It's called **inside the web `hold` review loop** (every 400 ms) and **once after the terminal
  review `input()`**, so a human submit is caught however the user ends the session.
- After the pause, if `manual_submit`, `run_apply` re-calls `_record_run`. Because the tracker
  upserts by source URL, this **flips the same posting row** `dry-run`→`applied` (method `manual`,
  `date_applied` stamped, parked block cleared) and appends the submitted attempt to the
  append-only run log (decision 084) — so the history shows both the dry-run fill and the manual
  submit.
- `frame` is initialised to `None` at the top of `run_apply` so the Workday / early-exit paths can
  reach the pause check safely (`_confirmation_evidence` reads `page.url` regardless of frame).
- `_record_run`'s `method` now resolves to `manual` (human) / `auto` (armed bot) / `dry-run`.
- The web dry-run "done" message now says "you submitted this application manually; it's recorded
  as Applied in Track" when `report.submitted`, instead of always claiming a dry-run row.

The pre-pause `dry-run` record is kept as-is (so a run killed during the long review pause is still
captured); the manual submit just re-records over it.

**Verification.** New `tests/test_manual_submit.py` (4 cases): `_detect_manual_submit` returns False
with the form still showing (no false positive), flips the report to submitted after a real
headless-Chromium click of the local `submit_confirm.html` fixture (never a real posting —
Guideline #3), and is a no-op once already submitted; a re-record after a manual click upserts the
`dry-run` row to `applied`/`manual`, stamps `date_applied`, clears the parked block, and leaves both
attempts (`dry-run` + `applied`) in the run log; an armed-bot submit (`manual_submit=False`) still
records `method="auto"`. `test_submit.py` (7) green; the broader
apply/parking/multipage/runner/workday/calibration/usage suites pass (only the pre-existing
`test_parking.py` `bot_wall` + `test_nav_recipes.py` failures from an unrelated in-progress branch
remain — they reference an `ApplyReport(bot_wall=…)` field absent on this tree). Detection depends on
the site reaching a recognizable confirmation page after the user's click — flagged for end-to-end
confirmation on the next live dry run where the user hand-submits.

## 099 — Package v0.1 as a GitHub release + one-step launcher; add a skippable first-run walkthrough

**Context.** First user-facing packaging milestone: "make the first-time walkthrough and look into
bundling everything together as a downloadable app/release (v0.1)." The app is a stdlib
`http.server` Python UI (no framework, no build step). Its heaviest dependency is Playwright's
Chromium (~170 MB, installed via `playwright install chromium`), and the Claude tailoring engine
shells out to the user's own `claude` CLI — which is their subscription and cannot be bundled.

**Distribution options considered.**

| Option | Verdict |
|---|---|
| PyInstaller / py2app native binary | Rejected for v0.1 — bundling Chromium in PyInstaller is fragile, needs separate per-OS/arch builds + macOS notarization, and still can't ship the `claude` CLI. A v0.2+ goal. |
| pipx-installable wheel | Devs only; needs Python + a playwright step; no double-click. |
| **Versioned GitHub release + one-step launcher** | **Chosen** — cross-platform, low-risk, reuses the existing `run.sh`; a real "download and run" release without the native-binary fragility. |

The release's downloadable archive is GitHub's auto-generated source zip (built from the git tag =
tracked files only), so git-ignored PII/secrets can't ride along (Guideline #12); `release.sh`
re-verifies this.

**Platforms (user chose macOS + Linux + Windows).** `ApplicationBot.command` (macOS double-click),
`ApplicationBot.bat` (Windows double-click), and `scripts/run.sh` (Linux / CLI).

**Delivered.**
- `applicationbot.__version__ = "0.1.0"`; `python -m applicationbot.web --version`.
- `scripts/run.sh` extended into a full idempotent bootstrap: python-3 check → venv → deps →
  `playwright install chromium` (the step a manual clone most often forgets) → non-fatal `doctor`
  readiness report → open browser → serve.
- `scripts/release.sh`: **dry-run by default** (verifies + prints the plan, changes nothing); only
  `--publish` tags HEAD and runs `gh release create`. Gated because a release is outward-facing and
  irreversible (Guideline #3 / Decision Framework #4). Includes a PII guard that aborts if
  `git ls-files` contains `profile/*` (except README), `.env*`, `*.db`, `*.session`, etc.
- README "Quick start — download & run (v0.1)" section.

**Walkthrough (user chose "in-app checklist, skippable").** New `GET /setup/status` reuses
[doctor.py](applicationbot/doctor.py)'s readiness checks (one source of truth) and adds a "first
dry-run done?" signal (any tracked application). A `#setup-overlay` renders one row per step; each
unfinished row has exactly one button that deep-links to where it's fixed — tab switch + scroll +
flash (UI Principle #2). Auto-opens on a fresh, unfinished clone unless the user skipped
(`localStorage["ab-setup-skipped"]`); reopenable from the nav's "✨ Finish setup (N left)" button.
Rejected: a permanent Setup tab (dead weight once configured) and a forced step-by-step wizard
(rigid, duplicates the Profile forms).

**Verification.** `run.sh` bootstraps and serves (GET / → 200; doctor all-green on this clone);
`release.sh` dry-run creates no tag and changes nothing; Playwright confirms the walkthrough
auto-opens when not ready, each action deep-links and closes the overlay, skip persists across
reload, and reopen works; incomplete + done states screenshotted. Not published — running
`release.sh --publish` is left to the user.

## 100 — UI accessibility pass to WCAG 2.1 AA before v0.1 (HIG-aligned); split the accent token; add ui.md

**Context.** "Before pushing to git, make sure we're following UI best practices (Apple HIG etc.)
and write takeaways in a ui.md." Ran a WCAG contrast audit over the freshly retinted neutral-gray
dark theme (decisions to soften + go Google-neutral were earlier cosmetic changes). It found real
AA failures: **white-on-primary-button 3.94:1**, and the blue used as **link / active-tab text**
3.5–4.1:1 — both below the 4.5:1 normal-text floor.

**Key insight.** A color used as a **fill** (with white text on it) must be *dark enough*; the same
color used as **text on a dark surface** must be *light enough*. These pull in opposite directions,
so a single accent token cannot satisfy both.

**Decision.** Split the accent:
- `--accent` — fills & borders, paired with white text. Dark `#5b7fca` → `#4a70c2` (white-on = 4.79,
  still clearly the user's blue, just a touch deeper). Light stays `#2f68f5` (4.75).
- `--accent-text` (new) — accent-colored text/links/active-tab. Dark `#82a6ef`, light `#2258d8`.

Only `color:var(--accent)` usages were switched to `--accent-text` (15 of them), via a sed anchored
on a preceding space or `{` so `border-color`/`border-top-color` fills (7) were left untouched;
verified by before/after counts.

**Other best-practice fixes (all verified).**
- `:focus-visible { outline:2px solid var(--accent-text); outline-offset:2px }` — a visible keyboard
  focus ring on every interactive control (shows for keyboard, not mouse).
- `@media (prefers-reduced-motion: reduce)` — drops the decorative flash pulse + smooth-scroll but
  **keeps functional spinners** (a frozen "working…" is worse than motion). JS scrolls check
  `matchMedia` too.
- The setup dialog implements the full ARIA dialog pattern: move focus in on open, **trap Tab**,
  restore focus on close (already had `role/aria-modal/aria-labelledby`, Escape, click-out); added
  `aria-describedby`.
- Don't rely on color alone (WCAG 1.4.1): done rows show a ✓ glyph, not just green.
- `--faint` (fails 4.5:1 by design) nudged up and **documented as incidental-only** — anything the
  user needs to read uses `--muted`, which passes.

**Rejected.** (a) Darkening the single accent — fixes buttons but worsens link text and undoes the
user's "more blue, less gray" tuning. (b) Accepting ~4:1 as close enough — the user explicitly asked
for best practices.

**ui.md.** Added [ui.md](ui.md) as the standing UI reference for future agents: where the UI lives
(embedded in web.py, dark tokens duplicated in two blocks that must stay in sync), the token system,
the two-accent rule, a runnable contrast checker, the AA requirements (contrast, color-independence,
focus, reduced motion, dialogs, target size), the shared feedback/waiting pattern, and HIG takeaways.

**Verification.** WCAG checker: every text/background pair now passes AA (dark button 4.79, dark link
5.8–6.6, light button 4.75, light link 5.3–6.1). Playwright: focus moves into the dialog and Tab
stays trapped. 18 doctor/web/tracker tests pass. Both light and dark screenshots reviewed — the blue
still reads as the user's blue; the focus ring is clearly visible.

## 101 — Dev auto-reload and a one-command GitHub update (stdlib only, no new deps)

**Context.** The user wanted two dev-loop ergonomics: (1) local edits should be picked up without
manually restarting, and (2) an easy way to pull the latest from GitHub. Constraint: the entire UI
is a module-level `INDEX_HTML` string built at **import time** and served per request, so *any*
change — Python logic or the embedded HTML/CSS/JS — only shows after a full process restart. Until
now the only path was the manual `scripts/restart.sh`.

**Auto-reload — decision.** A small stdlib supervisor, `scripts/dev_reload.py`, spawned by
`run.sh --dev` (alias `scripts/dev.sh`):
- polls the mtimes of every `applicationbot/**/*.py` once a second and restarts the server child
  process on any add/edit/remove (also relaunches it if it crashes);
- runs the child with `APPLICATIONBOT_DEV=1`. web.py, when that env is set, serves a
  `/dev/reload-token` heartbeat (the process boot timestamp) and injects a ~10-line poller into the
  page that calls `location.reload()` when the token changes — so the **browser refreshes itself**
  after each restart. The edit→see-it loop needs no terminal interaction.

Options weighed: (a) in-process `os.execv` self-restart — rejected, it entangles the server with
threading/exec and socket-reuse fragility; keeping the server a plain server and supervising it
from outside is simpler and more robust. (b) `watchdog`/`watchmedo` — rejected, a new dependency
for something ~50-file mtime polling does for free (Guideline #2). Production is unaffected:
`/dev/reload-token` returns a harmless token and nothing is injected unless `APPLICATIONBOT_DEV=1`.

**Update — decision.** `scripts/update.sh`: `git fetch` + `git merge --ff-only @{u}`, and when it
advances, reinstall deps + `playwright install chromium`, then apply it — if the dev reloader is
running, let it restart (browser self-refreshes); else bounce a running `applicationbot.web` via
`restart.sh`; else tell the user to start it. It **refuses on a dirty working tree** (Guideline #7 —
never overwrite the user's edits; it prints the `git stash → update → git stash pop` recipe), and
fails with a clear message when there's no upstream or the branch has diverged (no `--ff` possible).
README gained a "Developing / Getting updates" section.

**Verification.** Dev mode restarts on a real `touch applicationbot/usage.py` (the boot token
changed T1→T2) and injects the poller; normal mode injects nothing but still serves the token;
`run.sh --dev` bootstraps and serves 200 (the browser-open shadowed with a fake `open` on PATH so
the test didn't pop a tab); `update.sh` correctly refused on the current dirty tree; every script
passes `bash -n` / `ast.parse`.

## 102 — Standalone desktop app (native window) via pywebview + a macOS .app; forward-compatible, TCC-aware

**Context.** The user wants ApplicationBot to be its own double-clickable application that opens
its own window ("not through Chrome"), and — in a follow-up — to be forward-compatible given Apple
is winding down Rosetta (https://support.apple.com/en-us/102527).

**Windowing (chosen: pywebview).** New `applicationbot/app.py` runs the exact same local web UI as
`applicationbot.web`, but inside a native OS webview (pywebview 6 → WKWebView on macOS, WebView2 on
Windows). Added `pywebview>=5` to requirements. Rejected Electron/Tauri (heavy shell; Electron
bundles ~150 MB Chromium; Tauri needs Rust) and a chromeless-browser window (still a browser).
Because it's the same UI, the dark theme, first-run walkthrough, and dev auto-reload all carry over
unchanged — in `--dev` the window reloads itself through the existing `/dev/reload-token` poller.

The server runs as a **subprocess** (`applicationbot.web`, or the dev supervisor in `--dev`) that
the window points at, not an in-process thread — this proved far more robust than an in-process
server against pywebview's macOS GUI/event-loop behavior. On window close the subprocess is
terminated.

**macOS .app (hand-rolled).** `scripts/build_macapp.sh` builds `ApplicationBot.app` (Info.plist +
a shell exec, no py2app), resolving the repo as the folder containing it, and **ad-hoc code-signs**
it. Ad-hoc signing matters: arm64 macOS expects signed executables, and it fixes Gatekeeper's "no
usable signature" rejection so a double-click launches (a *downloaded* copy still needs the one-time
right-click → Open). Launchers gained `run.sh --window` and `--window --dev`; `ApplicationBot.command`
and `.bat` now open the window. The generated bundle is git-ignored (rebuild, don't commit).

**Forward-compatibility (native arm64, no Rosetta).** `scripts/_native.sh` (`require_native`,
sourced by the launchers) refuses to build/run on a Rosetta-translated or Intel-only Python and
prints the fix. Verified it passes a native interpreter and rejects an `arch -x86_64` one. The
machine is already arm64-native (universal2 python.org build, `sysctl.proc_translated=0`), so
nothing is on the Rosetta path today; the guard keeps it that way on any machine.

**macOS privacy (TCC) — the crux.** A Finder/LaunchServices-launched app cannot read `~/Documents`
during *early* Python startup, so reading the venv's `pyvenv.cfg` failed with
`PermissionError: Operation not permitted` — a failure that happens before the app can even prompt.
Fix (the user's choice, over relocating the whole project): the virtualenv now lives OUTSIDE the
repo at `~/Library/Application Support/ApplicationBot/venv` (a non-protected location), resolved by
new `scripts/_venv.sh` and used by run.sh / build_macapp.sh / update.sh. On Linux/Windows the venv
stays in-repo as `.venv` (no such restriction). This clears the un-promptable init failure. The app
still reads the user's résumé/profile/application data, which live in the repo under ~/Documents, so
macOS shows the normal **one-time "access your Documents folder" prompt** on first launch — the
bundle declares `NSDocumentsFolderUsageDescription` so that prompt is clear.

**Verified.** Windowed app works end-to-end from the Terminal path (`run.sh --window`): server
subprocess comes up, the window opens, using the support-dir venv. pywebview imports with the cocoa
(WKWebView) backend; the server serves 200 both in-thread and as a subprocess; the native-arch
guard behaves in both directions; the bundle's plist lints and ad-hoc-signs. **Not** verifiable from
automation: the one-time Documents-consent click on a pure Finder double-click (it auto-denies in a
headless context) — handed to the user to confirm on their screen.

**Trade-off / alternative.** Keeping the project under ~/Documents means one Documents-access prompt
on first double-click (and, with an ad-hoc signature, a possible re-prompt after a rebuild). The
zero-prompt alternative is to keep the clone outside ~/Documents,~/Desktop,~/Downloads — proven to
launch with no prompt. Development with live auto-reload uses the Terminal path (`./scripts/dev.sh`
/ `run.sh --window`), which always has file access.

## 103 — The macOS app is a fully self-contained, drag-to-Applications bundle (PyInstaller)

**Context.** "I want the install flow to be the same as any other macOS app: drag to Applications
and everything installs itself." This supersedes the hand-rolled, venv-based `.app` of decision 102.

**Production vs. development (confirmed with the user).** A self-contained bundle embeds a *snapshot*
of the code, so it can't mirror live repo edits. That's fine: the bundle is the **production release
artifact** (updated by rebuilding / a new GitHub release), and **development stays on localhost**
(`./scripts/dev.sh`, `./scripts/run.sh --window`) which runs the live repo with auto-reload.

**Bundler: PyInstaller.** Spiked first — a minimal pywebview app built into a 24 MB `.app` that
launched self-contained from /tmp with no system Python. The real build bundles its own Python +
all dependencies + the code into `ApplicationBot.app` (~190 MB). `desktop_main.py` is the frozen
entry point; it sets `APPLICATIONBOT_DATA` to `~/Library/Application Support/ApplicationBot` before
importing the app.

**Data-directory refactor (the enabling change).** New `applicationbot/paths.py`:
- `DATA_ROOT` = `$APPLICATIONBOT_DATA` if set, else the repo root — so running from source is
  unchanged (verified: 389 tests pass, only the 2 pre-existing `bot_wall` failures remain).
- `BUNDLE_ROOT` = `sys._MEIPASS` when frozen, else the package parent — for read-only product
  resources (fixtures/examples).
The 8 modules that had rooted user data at the package directory (tracker's `applications.db`,
discovery_seen/discovery_cache, fit_learning, archive, safety, resume_store, salary) now use
`DATA_ROOT`. The frozen app serves the web UI **in-thread** (a frozen binary is its own
`sys.executable` and can't be re-invoked as `python -m applicationbot.web`) with cwd = `DATA_ROOT`,
so the remaining cwd-relative `profile/…` paths resolve there too.

**Build script.** `scripts/build_macapp.sh` rewritten: a `.build-venv` (deps + PyInstaller),
`--collect-all` for playwright/pywebview/keyring/google_auth_oauthlib, `--add-data` for
fixtures/examples and the package's nav/workday recipe JSON (the `--collect-all applicationbot`
data step is skipped because the source dir isn't an installed dist), then `plutil` version stamp
and ad-hoc `codesign --deep`.

**Chromium.** Not bundled (keeps the app lean). `_ensure_chromium_bg()` runs on first launch in a
background thread and installs Chromium via the Playwright driver bundled in the app; the window
opens immediately regardless.

**Bonus: the 102 TCC problem disappears.** A self-contained app reads nothing from ~/Documents, so
there is no "access your Documents folder" prompt — the thing I couldn't verify in 102 is now moot.

**Verified.** `ApplicationBot.app` copied to /tmp (no repo, Python, or venv nearby) launches via
`open`, serves the full UI (GET / → 200, correct `<title>`) in its native window, shows the
fresh-install walkthrough (`/setup/status` ready=false, profile step incomplete), writes
`applications.db` into Application Support, and logged the background Chromium install as
"finished". The nav/workday JSON is present in the bundle.

**Out of scope.** Distributing the `.app` to *other* people's Macs without a one-time
right-click → Open needs Apple notarization (a paid Apple Developer account). The 102 assets
(`_venv.sh`, `_native.sh`, `run.sh --window`) remain in service for the localhost/dev path.

# Discovery Source Expansion — Research & Wire-In Reference

> **Status:** Research complete 2026-07-22 (decision 114). No code written.
> **Purpose:** self-contained reference for the agent wiring in new discovery sources.
> **Scope of this doc:** *Step 1+* (new sources + the company→token table). *Step 0* (widening
> the funnel) is being done in parallel by another agent — see [Read this first](#read-this-first).
> **Companion:** the terse task list lives in [NEXT_STEPS.md](../NEXT_STEPS.md) →
> "Discovery source expansion — research 2026-07-22"; the rationale is decision 114 in
> [DECISIONS.md](../DECISIONS.md).

---

## Read this first — the funnel reframe

The goal was "uncover far more jobs." Research plus our own **decision 073** show the real
constraint today is **funnel throughput, not source count**:

- Built-in curated feeds already carry **~3.4k active / ~2.2k fillable** postings.
- `early_career.max_resolve` (default **40**) truncates that to 40 resolved + judged per run.
- The pool is **~50x oversubscribed** — so **adding sources yields ~zero real gain until the
  funnel widens.**

**Therefore: Step 0 (widen the funnel) must land before new sources matter.** That work —
raise/auto-tune `max_resolve`, add a cheap Haiku pre-rank, surface "judged 40 of N" (UI
Principle #5) — is owned by a parallel agent. Everything in *this* doc (Steps 1–2) becomes
high-value **only after** Step 0 opens the neck. Coordinate via the agent bus before starting.

**Hard constraints (from CLAUDE.md):**
- **Free sources only** — no paid API tiers (no paid JSearch/SerpApi).
- Respect `robots.txt`, honest identifying User-Agent, ~1–2 req/s per host, back off on 429/503,
  cache aggressively (Guideline #4).
- Real submits stay behind the `dry_run`/arm safety switch (Guideline #3).
- No PII/secrets in git (Guideline #12).

---

## Current state (what already exists)

`applicationbot/discovery.py` (`Source` base class, `ATS_SOURCES` registry, `discover()`
fan-out+dedup) already implements, all via stdlib `urllib`:

- **Public no-auth ATS APIs (full JD):** Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee,
  Workable.
- **Aggregator (snippet):** Adzuna (self-skips without a free key).
- **Career pages:** `CareerSiteSource` — JSON-LD → CSS → optional-LLM cascade (`enrich.py`).
- **Curated GitHub feeds:** SimplifyJobs new-grad / intern `listings.json` (`CuratedListSource`),
  ranked by title-relevance, top-`max_resolve` resolved to full JD.
- **Aggregator→ATS bridge** (`bridge_aggregator_postings`): follows an aggregator redirect,
  detects the real ATS, rewrites `apply_url`, upgrades snippet → full JD.

Filters live in `filters.py` (`DiscoveryFilters`, loaded from `profile/discovery.yaml`). The
posting model is `discovery.Posting`; it renders to the exact JD fixture shape Tailor/Apply
already consume. **Requirements are not a separate field** — they live inside `body`.

---

## Step 1 — new FREE sources, tiered by bang-for-buck

Fit each into the existing `Source` interface. **Most are discovery/track-only** — see the
[apply-capability matrix](#apply-capability-matrix). ✅ = endpoint live-tested during research.

### Tier 1 — new public ATS feeds (keyless, employer-direct apply URL, the lane we already trust)

These are the highest breadth-per-effort adds: clean structured data straight from employers,
same low-risk posture as our existing six ATSs.

#### Workday CxS — *biggest enterprise coverage; complete this one*
- **List:** `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
  body `{"appliedFacets":{}, "limit":20, "offset":0, "searchText":""}`
- **Detail:** `GET https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}`
- **Auth:** none (careers-site backend, not a sanctioned API — treat as gray; rate-limit hard,
  it 429s and can IP-block).
- **Fields:** list → `title`, `externalPath`, `locationsText`, `postedOn`, `bulletFields` (req id);
  detail → `jobPostingInfo` with `jobDescription` (HTML), `location`+`additionalLocations`,
  `timeType`, `remoteType`, `jobRequisitionId`, `externalUrl`.
- **Discovery is the hard part — three vars per employer:** `tenant`, `wd{N}` cluster
  (wd1/wd3/wd5/wd103/…), and `site` path. All three are in the `myworkdayjobs.com` URL — resolve
  by following a company's careers link and parsing the final URL. Dork `site:myworkdayjobs.com`.
- **Submit:** no clean API (multi-step CxS flow ≈ browser automation). **Discovery-only.**
- **Note:** we already resolve Workday *JDs* (decision 074, via `enrich`); this is about adding
  Workday as a first-class *discovery* source.

#### BambooHR — ✅ huge SMB footprint
- **List:** `GET https://{co}.bamboohr.com/careers/list`
- **Detail:** `GET https://{co}.bamboohr.com/careers/{id}/detail`
- **Auth:** none (careers-widget backing API; the documented `api.bamboohr.com/...` REST API
  needs an account key — don't use it).
- **Fields:** list → `jobOpeningName`, `departmentLabel`, `employmentStatusLabel`,
  `location{city,state}`, `isRemote`, `locationType`; detail → `description` (HTML),
  **`compensation`** (e.g. "$30 Hour"), `datePosted`, `minimumExperience`, `jobOpeningShareUrl`
  (apply URL), and a `formFields` map of the apply form.
- **Gotchas:** **soft-404** — returns HTTP 200 with an empty/404-ish body for missing tenants, so
  inspect the JSON, not the status code. **Bot-UA detection** — blocks User-Agents containing
  "JobBot"; use a normal browser-style UA. Field names change without notice.
- **Discovery:** no directory. `site:bamboohr.com/careers` dorks, BuiltWith, aggregator seed
  lists, or your own target list.
- **Submit:** no public API. **Discovery/track-only.**

#### Rippling — ✅ clean two-call feed
- **List:** `GET https://ats.rippling.com/api/v2/board/{slug}/jobs?page=0&pageSize=100`
- **Detail:** `GET https://ats.rippling.com/api/v2/board/{slug}/jobs/{uuid}` (has `description`)
- **Auth:** none (public board backing `ats.rippling.com/{slug}/jobs`; the documented
  `api.rippling.com/...` is OAuth/partner — don't use it).
- **Fields:** `id`, `name`, `url`, `department.name`, `locations[]` with
  **`workplaceType` = ON_SITE/REMOTE/HYBRID**; `description` (HTML) on detail. `totalItems`/
  `totalPages` for paging.
- **Discovery:** `site:ats.rippling.com`; ~1.3k companies. No directory.
- **Submit:** no public API. **Discovery/track-only.**

#### Breezy HR — ✅ single-request, richest-for-least
- **Endpoint:** `GET https://{slug}.breezy.hr/json?verbose=true` (**follow the 302** — the bare
  URL redirects; wrong slug → 404).
- **Auth:** none (the `api.breezy.hr/v3/...` official API needs a token — don't use it).
- **Fields (verbose=true, full descriptions in one call):** `id`, `friendly_id`, `name`,
  `url` (`/p/{id}` apply page), `published_date`, `type`, `location{country,city,is_remote,
  remote_details}`. Null-check everything (fields are per-company optional).
- **Discovery:** `site:breezy.hr`; ~2k companies / ~45k positions. Slug = careers subdomain.
- **Submit:** no public API. **Discovery/track-only.**

#### Personio — ✅ XML only (DACH-heavy)
- **Endpoint:** `GET https://{co}.jobs.personio.de/xml?language=en` (also a `.com` twin — resolve
  which per company; don't assume).
- **Auth:** none. **`search.json` is NOT public** (307-redirects to personio.com) — use `/xml`.
- **Fields (XML tags):** `id`, `subcompany`, `office`, `department`, `recruitingCategory`,
  `name` (title), `jobDescriptions/jobDescription{name,value(CDATA HTML)}`, `employmentType`,
  `seniority`, `schedule`, `yearsOfExperience`, `occupation`, `createdAt`, `keywords`.
  **No salary field.**
- **Gotchas:** heavily German/DACH; `?language=en` may still return German. Blind slug guessing
  fails silently (307 → personio.com) — verify each subdomain returns `<workzag-jobs>`.
- **Discovery:** `site:jobs.personio.de` / `site:jobs.personio.com`.
- **Submit:** needs company token. **Discovery/track-only.**

#### Teamtailor — ✅ RSS only (JSON is token-gated)
- **Endpoint:** `GET https://{slug}.teamtailor.com/jobs.rss?per_page=200&offset=N` (custom
  domains: `https://jobs.{company}.com/jobs.rss`).
- **Auth:** none on RSS. The JSON:API (`api.teamtailor.com/v1/jobs`) needs an **admin-minted,
  account-bound token** — no third-party access, so RSS is the only read path.
- **Fields (RSS item):** `title`, `link`, `description` (HTML), `pubDate`, `tt:locations`,
  `tt:department`, `tt:role`, `remoteStatus`. Salary lives in the body (no field).
- **Discovery:** `site:teamtailor.com`, `"powered by Teamtailor"`, CT-log/DNS for
  `*.teamtailor.com`; needs per-company hostname (often custom domain). 12k+ companies, no
  directory.
- **Submit:** needs company token. **Discovery/track-only.**

#### Comeet — documented public API, but needs a scraped uid+token (medium-high effort)
- **Endpoint:**
  `GET https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}&details=true`
- **Auth:** needs `company_uid` + `token` — **both non-secret**, shipped in the client-side
  careers-widget init (`//www.comeet.co/careers-api/api.js`), so scrape them from any Comeet
  careers page's HTML/JS. (Research could not fully live-verify a uid/token pair — confirm the
  view-source step on a real page before building.)
- **Fields:** `uid`, `name`, `status`, `department`, `employment_type`, `experience_level`,
  workplace type, `location{city,state,country}`; description/requirements with `details=true`.
- **Submit:** private OAuth API only. **Discovery/track-only.** *Lower priority — the uid/token
  scrape makes it more work than the others for similar payoff.*

### Tier 2 — free aggregator / search APIs (top-of-funnel breadth with REAL apply links)

Unlike Adzuna/Jooble/Careerjet (which hide the source apply URL behind a redirect), these expose
a usable apply link. Use them to widen the *top* of the funnel with keyword/salary/remote filters.

#### JSearch (RapidAPI) — broadest, but tiny free quota
- **Endpoint:** `GET https://jsearch.p.rapidapi.com/search?query=...&remote_jobs_only=true`
  (header `X-RapidAPI-Key`).
- **Free tier:** ~200–500 req/mo — **use sparingly, cache hard.** (Paid tiers are out of scope —
  free only.)
- **Filters:** free-text `query` (role+location), `date_posted`, `remote_jobs_only`,
  `employment_types`, `job_requirements`, `radius`, `country`.
- **Apply:** ✅ `job_apply_link` + `apply_options[]` (each with `is_direct` flag). Aggregates
  Google-for-Jobs (surfaces LinkedIn/Indeed/etc. *results* — not their APIs — so it's a legit way
  to reach that inventory without scraping those boards).

#### USAJobs — ✅ free, unlimited, authoritative (federal only)
- **Endpoint:** `GET https://data.usajobs.gov/api/search` with headers
  `User-Agent: <your registered email>` (NOT a browser UA — #1 cause of auth failure),
  `Authorization-Key: <free key>`, `Host: data.usajobs.gov`.
- **Filters:** `Keyword`, `LocationName`+`Radius`, `RemunerationMinimum/MaximumAmount`,
  `PayGradeLow/High`, `RemoteIndicator` (real remote filter), `DatePosted`,
  `ResultsPerPage` (max 500).
- **Apply:** ✅ `ApplyURI` (starts the real federal flow) — **but it routes into
  non-autofillable government portals, so this is discovery/track-only** (matches the existing
  decision-030 note). Open-data ToS: caching/storing is fine.

#### Himalayas — free remote feed, direct apply
- **Endpoint:** `GET https://himalayas.app/jobs/api?limit=20&offset=0`
- **Apply:** ✅ `applicationLink`. Structured salary + seniority. Filter client-side.

#### RemoteOK — free, direct apply (attribution required)
- **Endpoint:** `GET https://remoteok.com/api?tag=python` — **skip element 0** (legal/attribution
  notice).
- **Apply:** ✅ `apply_url`. Attribution + backlink required if displayed; tech-heavy.

#### Findwork.dev — free key, often direct
- **Endpoint:** `GET https://findwork.dev/api/jobs/?search=...&remote=true`
  (header `Authorization: Token <free key>`).
- **Apply:** ✅ usually a direct source URL. Daily cap on the free tier.

#### The Muse — low priority (apply is a hop)
- **Endpoint:** `GET https://www.themuse.com/api/public/jobs?category=...&level=...` (optional
  free key raises the limit to 3,600/hr).
- **Apply:** ⚠ `refs.landing_page` is a themuse.com page, one hop short of the ATS. Curated/narrow.

#### HN "Who is hiring" — free, high-signal, unstructured
- **Endpoint (Algolia):** find the monthly thread via
  `https://hn.algolia.com/api/v1/search?query=Ask HN Who is hiring&tags=story&author_whoishiring`,
  then comments via `https://hn.algolia.com/api/v1/search?tags=comment,story_{ID}&hitsPerPage=1000`.
- **Apply:** ❌ free text — NLP-parse the email/URL out of each `comment_text`. ~10k req/hr.

### Do NOT add more redirect-only aggregators

Adzuna is already wired and the aggregator→ATS **bridge** (decision 032) resolves its redirects
to the real ATS. **Jooble** and **Careerjet** hide the true apply URL the same way and add
nothing the bridge doesn't already cover — skip them. (Careerjet's affiliate ToS also expects
real end-user traffic, not silent harvesting.)

---

## Step 2 — the durable scaling lever: a free `company → {ats, token, cluster, site}` table

**This is the highest-leverage item for actual reach.** Every Tier-1 ATS needs per-company
slugs, and **no ATS publishes a tenant directory.** Today `profile/discovery.yaml` lists a
handful of boards by hand. A validated token table turns "a few companies" into "thousands," and
the per-ATS fetchers become thin wrappers over it. Build it free, in leverage order:

1. **Common Crawl (cheapest, biggest yield).** Grep the index for endpoint fingerprints —
   `boards-api.greenhouse.io/v1/boards/`, `api.lever.co/v0/postings/`, `myworkdayjobs.com/wday/cxs`,
   `.bamboohr.com/careers`, `ats.rippling.com`, `.breezy.hr`, `jobs.ashbyhq.com`,
   `apply.workable.com`, `.recruitee.com` — → thousands of real tokens without touching any site.
   Free dataset; you pay only AWS compute (Athena over the index).
2. **Career-page fingerprinting (highest precision).** Given a company domain, fetch `/careers`
   (or `/jobs`), detect the ATS from embed markers, and extract the token from the embed URL. This
   is how you convert *a list of companies you care about* → tokens. Reuse
   `discovery.detect_ats_from_url` logic (but see the detector-unification note below).
3. **Seeds:** search dorks (`site:boards.greenhouse.io`, `site:ats.rippling.com`, …), **crt.sh /
   CT logs** for `careers.company.com → {ats}` CNAMEs, and OSS token lists (validate — they rot).
4. **Validate every token live** before trusting it (hit the list endpoint, check for real
   postings), and **re-validate on a schedule** — companies switch ATS.

Persist the table somewhere git-ignored if it grows large; it is data, not code.

---

## Excluded — big consumer boards (do NOT build)

**LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Monster.** Unchanged from decisions
030/032. Two independent reasons:

- **No usable API.** Public job-search APIs are dead or partner-gated: Indeed Publisher API
  retired ~2024, Glassdoor's public API closed 2021, LinkedIn's Jobs API is posting-only
  (can't query the job DB), Google-for-Jobs never had one.
- **Scraping is the wrong kind of risk.** Scraping *public* pages survives CFAA post-*hiQ v.
  LinkedIn* / *Van Buren*, but **breach-of-contract is a live, winning claim — hiQ paid LinkedIn
  $500k** and conceded liability. These sit behind DataDome/Cloudflare, and defeating bot
  detection violates Guideline #4. *(Note: JSearch/USAJobs surface some of this inventory
  legitimately — JSearch via Google-for-Jobs results, not by scraping the boards.)*

---

## Apply-capability matrix

Discovery breadth ≠ apply capability. Autofill still happens via the browser for the ATSs Apply
already handles; a new *discovery* source does not mean we can submit to it.

| Source | Discover | Autofill (browser) | Public submit API |
|---|---|---|---|
| Greenhouse / Ashby / Workable *(existing)* | ✅ | ✅ (existing) | Greenhouse/Ashby need employer key; Workable needs token |
| **Lever** *(existing)* | ✅ | ✅ (existing) | ✅ open `POST /v0/postings/{co}/{id}/apply` |
| **SmartRecruiters** *(existing)* | ✅ | ✅ (existing) | ✅ but OAuth+consent — not anonymous |
| **Recruitee** *(existing)* | ✅ | ✅ (existing) | ✅ **open `POST .../offers/{slug}/candidates` — REAL irreversible submit, emails applicant → must sit behind dry_run/arm (Guideline #3)** |
| Workday CxS *(new)* | ✅ | existing Workday autofill | ❌ (multi-step flow) |
| BambooHR / Rippling / Breezy / Personio / Teamtailor / Comeet *(new)* | ✅ | only if browser autofill covers the form | ❌ |
| JSearch / Himalayas / RemoteOK / Findwork *(new)* | ✅ | only after resolving to a fillable ATS via the bridge | n/a |
| USAJobs *(new)* | ✅ | ❌ (gov portals) → **discovery/track-only** | n/a |

**Takeaway:** treat every new source as **discovery/track-only** until browser autofill is
proven on its form. The only *new* real submit path is Recruitee's public POST — and it's a live
submit, so gate it.

---

## Implementation traps (carry over 073/074)

- **Don't overload `ATS_SOURCES`.** It doubles as the fillability predicate
  (`pipeline._is_fillable`). Registering a discovery-only source there silently asserts an apply
  adapter exists. Separate "can discover" from "can autofill" before adding sources.
- **Unify the two ATS detectors first.** `discovery.detect_ats_from_url` (knows
  smartrecruiters/recruitee/workable, returns `"other"`, no iCIMS) and `apply.detect_ats` (knows
  iCIMS, returns `"generic"`) already drift. One shared detector with an explicit
  fillable/resolvable capability map avoids a growing trap as sources multiply.
- **Salary is unreliable everywhere** except **Ashby** (structured) and, when the employer fills
  it, **Lever**/**Recruitee** (often null). Parse from HTML or expect it absent — don't build UI
  that assumes structured comp.
- **These are widget/careers-site backing surfaces, not contractually-guaranteed APIs** (except
  Comeet's documented careers API). Paths and field names can change without notice — wrap
  defensively, null-check, and cache.
- **Politeness = compliance.** robots.txt, honest UA, ~1–2 req/s per host, backoff on 429/503.
  This is both the legal posture and how we avoid IP blocks (especially Workday).

---

## Recommended sequencing

0. **[parallel agent] Widen the funnel** — prerequisite; nothing below pays off until it lands.
1. **Workday CxS** as a discovery source — single biggest breadth win, reuses existing Workday JD
   resolution.
2. **BambooHR + Rippling + Breezy** — easy keyless JSON, big SMB coverage, all live-verified.
3. **The `company → token` table** (Common Crawl + fingerprinting) — the durable reach unlock;
   makes #1–#2 scale from "a few companies" to "thousands."
4. **JSearch (free) + USAJobs** — top-of-funnel breadth with real apply links / federal coverage.
5. **Himalayas / RemoteOK / Findwork / HN** — cheap remote breadth.
6. **Personio / Teamtailor (XML/RSS) + Comeet** — regional / higher-effort, add as needed.

---

*Sources: live-tested endpoints (July 2026) + official ATS/aggregator docs; legal points from
hiQ v. LinkedIn (9th Cir. + 2022 settlement), Van Buren v. United States (SCOTUS 2021), Meta v.
Bright Data (N.D. Cal. 2024). Full citations are in the research-agent transcripts for this
session.*

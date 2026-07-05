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

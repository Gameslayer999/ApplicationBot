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

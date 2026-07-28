<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-darkmode.png">
    <img src="assets/logo-lightmode.png" alt="ApplicationBot" width="200">
  </picture>
</p>

<h1 align="center">ApplicationBot</h1>

<p align="center"><b>The job search that runs on your machine.</b><br>
Discovers matching openings, tailors your résumé to each one, applies for you, and tracks everything — with a dry-run safety switch so nothing is ever submitted until you say go.</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20app%20%C2%B7%20source%20anywhere-blue">
  <img alt="Python" src="https://img.shields.io/badge/python-3-blue">
  <img alt="Safety" src="https://img.shields.io/badge/submit-dry--run%20by%20default-brightgreen">
  <img alt="Powered by Claude" src="https://img.shields.io/badge/powered%20by-Claude-8A63D2">
</p>

ApplicationBot is a personalized, end-to-end job-application pipeline you run yourself. You supply
your profile, a base résumé, and filters describing the jobs you want; it finds matching postings,
customizes your résumé for each, fills out and submits the applications, and records every one. It is
built to be **downloaded and used by anyone** — nothing about you is baked into the repo; your data
lives in a local, git-ignored folder and never leaves your machine except the minimal text a
tailoring or matching call sends to Claude.

> [!WARNING]
> **Submitting an application is irreversible, so real submission is gated behind a deliberate safety switch.**
> - **Dry-run is the default.** Out of the box, ApplicationBot does everything *except* the final submit — it
>   discovers, tailors, fills the form, and records what it *would* have sent. Nothing is submitted.
> - **Arming is explicit.** Real submission happens only after you set `armed: true` in `profile/safety.yaml`
>   (with a per-run submission cap).
> - **A global kill switch stops everything.** Creating a `profile/KILL` file halts all submission immediately;
>   it is re-checked right before every click.
>
> You are responsible for how you use it. Automated applications may conflict with some job boards' terms of
> service and can put your account at risk on those sites — review each site's rules and decide what you're
> comfortable running.

---

## How it works

Five stages, each of which you can run on its own or chain into one autonomous loop:

```
Configure  →  Discover  →  Tailor  →  Apply  →  Track
```

1. **Configure** — set up your profile: contact details, a base résumé (structured YAML, the source of
   truth), and filters (roles, keywords, location/remote, pay range, seniority, company type). Filters
   drive both what gets discovered and what gets auto-applied to. Edit it all from the web UI or in
   `profile/*.yaml`.
2. **Discover** — pull openings that match your filters from public ATS APIs
   (Greenhouse · Lever · Ashby · SmartRecruiters · Recruitee · Workable), keyless aggregators
   (Adzuna · Jooble · Remotive and other JSON sources), and forwarded job-alert emails. A cheap keyword
   pre-filter narrows the pool, a two-stage judge (Haiku pre-rank → Sonnet) ranks the survivors by
   qualification fit and names your missing requirements, and a funnel view shows exactly how many
   postings reached each stage.
3. **Tailor** — Claude rewrites your résumé for each posting — selecting, reordering, and rephrasing what
   you already have. A drift check flags any skill, role, or certification that isn't in your base résumé,
   so it stays factual, and every exported PDF is re-checked to confirm its text layer is machine-readable.
4. **Apply** — a real browser (Playwright) fills and submits the application through the posting's own
   ATS, including multi-page wizards and account-gated **Workday** (automated account creation, credentials
   in your OS keychain). Applications that get blocked (a question it can't answer, a login, a CAPTCHA) are
   *parked* so you can resolve and resume them. Every submit is gated by the safety switch above.
5. **Track** — every application is recorded in a local SQLite database with company, role, location, pay,
   portal, status, date, fit score, and the exact tailored résumé used — viewable and editable in the Track
   tab, with funnel and calibration reports.

Discovery, tailoring, filling, and submission run with **no human in the loop** once you arm the system —
that is the point of the tool. Until then, everything is a dry run.

---

## Requirements

- **macOS** to run the prebuilt app (Apple Silicon). Any OS to run from source.
- **Python 3** for the from-source path. The prebuilt macOS app bundles its own — you need nothing installed.
- **A browser for the Apply stage** — Chromium, downloaded automatically on first run.
- **Claude, one of three ways** (for the Discover judge and Tailor stage):
  - a **Claude Pro/Max subscription** via [Claude Code](https://claude.com/product/claude-code) — recommended, not metered;
  - your own **Anthropic API key** — pay-per-token, stored in your OS keychain; or
  - **nothing at all** — the built-in `rules` engine reorders/selects by keyword with no account and no network.

---

## Install

### Option A — download the macOS app (recommended)

The easiest way to run ApplicationBot — no clone, no build, no Python.

1. Download **`ApplicationBot.app.zip`** from the **[latest release](../../releases)**.
2. Double-click to unzip, then **drag `ApplicationBot.app` into your Applications folder**.
3. **First launch only** — because the app isn't signed by an Apple-registered developer, macOS blocks it
   once. Get past it one of these ways (after that it opens normally, forever):
   - **Right-click** (or Control-click) the app → **Open** → **Open**; **or**
   - macOS Sequoia and later: open **System Settings → Privacy & Security**, scroll to
     *"ApplicationBot was blocked…"*, click **Open Anyway**, then confirm; **or**
   - in Terminal: `xattr -dr com.apple.quarantine /Applications/ApplicationBot.app`
4. Double-click to launch. It's fully self-contained (its own Python, all dependencies, the Apply-stage
   Chromium downloads quietly on first run) and reads nothing from your Documents folder. Your data lives in
   `~/Library/Application Support/ApplicationBot/`.

> The app is **ad-hoc signed** (free) — that first-launch prompt is the only cost of skipping Apple's paid
> notarization; nothing else changes.

### Option B — run from source (CLI + web UI, any OS)

For developers, or Windows/Linux users.

```bash
git clone https://github.com/Gameslayer999/ApplicationBot.git
cd ApplicationBot
./scripts/run.sh            # sets up the venv + Chromium, starts http://127.0.0.1:8000, opens your browser
```

- **Windows:** run **`ApplicationBot.bat`**. **Linux:** `./scripts/run.sh`. **macOS from source:** `ApplicationBot.command`
  (first launch: right-click → **Open**).
- The launcher is idempotent — safe to re-run any time. It creates the virtualenv, installs dependencies, and
  downloads the automation browser on first run.
- Prefer a native desktop window over a browser tab? `./scripts/run.sh --window`.

---

## Quick start

Whichever way you installed, the flow is the same:

1. **Finish setup.** Follow the in-app **✨ Finish setup** walkthrough — add your details and résumé, and choose
   which jobs to find. (From source you can instead copy the templates in [`examples/`](examples/) into `profile/`:
   `sample_resume.yaml`, `discovery.example.yaml`, `safety.example.yaml`.)
2. **Connect Claude (optional but recommended).** Sign in with Claude Code for the best tailoring on your
   subscription, or add an Anthropic API key in the bottom-left **"Claude connection"** panel. With neither, the
   free `rules` engine works with no account.
3. **Discover + dry-run apply.** Hit **Find & fill one application (dry-run)** in the Discover tab (or, from the
   CLI, `python -m applicationbot.pipeline --apply-first`). Watch it discover a match, tailor your résumé, and
   fill the form live. **It never submits.**
4. **Arm it when you're ready.** Set `armed: true` in `profile/safety.yaml` (with a submission cap) to let it
   submit for real. Drop a `profile/KILL` file to stop everything instantly.

---

## Command reference

The web UI covers everything, but each stage is also a module you can run directly.

| Command | What it does |
|---|---|
| `./scripts/run.sh [PORT]` | Set up and start the web UI (default `http://127.0.0.1:8000`) |
| `./scripts/dev.sh` | Dev mode: auto-restart on save, page auto-reloads |
| `./scripts/update.sh` / `restart.sh` / `stop.sh` | Pull latest + reinstall + restart · restart · stop |
| `python -m applicationbot.web [--port 8000]` | Start the web UI directly (stdlib, binds `127.0.0.1` only) |
| `python -m applicationbot.pipeline --apply-first` | Discover → judge → tailor → **dry-run** fill one top match |
| `python -m applicationbot.runner [--max N] [--continuous]` | Autonomous loop over every cleared match (dry-run by default) |
| `python -m applicationbot.cli JD.md --resume R.yaml --out out.pdf` | Tailor a résumé to one job description (CLI) |
| `python -m applicationbot.apply URL --resume profile/resume.yaml --dry-run` | Fill one application by URL |
| `python -m applicationbot.doctor` | Read-only health check (Claude sign-in, Chromium, résumé, safety state) |
| `python -m applicationbot.tracker [funnel\|calibration]` | Inspect tracked applications and reports |
| `python -m applicationbot.mailbox link\|status\|test` | Link the bot inbox (Workday email verification, job-alert ingest) |

**Tailoring engines** (`--backend`, defaults to `auto`):

| `--backend` | Needs | Quality |
|---|---|---|
| `claude-code` | Claude Code signed in — your **subscription**, not the metered API | Best — rewrites bullets to match the posting |
| `anthropic-api` | Your own **Anthropic API key** (OS keychain) — **metered** | Same rewriting, billed to your API account |
| `rules` | **Nothing** — no LLM, no account, no network | Reorders/selects by keyword; doesn't reword |
| `auto` (default) | — | Subscription → else API key → else rules |

> **Why can't it "log in with Claude" in the app?** Anthropic restricts subscription login to Claude Code and
> Claude.ai, so a third-party app can't use your subscription directly. The best path shells out to the Claude
> Code CLI (which *is* on your subscription); the API key is the metered fallback.

---

## Configuration & your data

Everything specific to you lives in the git-ignored **`profile/`** folder (from source) or
`~/Library/Application Support/ApplicationBot/` (the app):

- `resume.yaml` — your base résumé, the factual source of truth.
- `discovery.yaml` — filters, boards, and sources (roles, keywords, location, pay, seniority, gates).
- `safety.yaml` — the arm switch and per-run submission cap.
- `notifications.yaml`, `mailbox.yaml` — optional desktop/phone push and the bot inbox link.
- `applications.db` + `applications/` — your tracked history and per-application archives.

Template versions of these live in [`examples/`](examples/). Run `python -m applicationbot.doctor` any time to
confirm your setup is healthy.

---

## Privacy & safety

Your résumé, contact details, credentials, and application history are sensitive and are treated that way:

- **Personal data never enters git.** Everything above is covered by `.gitignore` and stays on your machine.
  Only the minimal text a matching or tailoring call needs is ever sent to Claude.
- **Credentials go in your OS keychain**, never in plaintext YAML — the Anthropic API key, and any Workday
  account passwords.
- **Submission is safety-gated** — dry-run by default, explicit arming, global kill switch (see the warning at
  the top).
- **Scraping respects each site's terms and rate limits.** ApplicationBot does not build functionality whose
  purpose is to evade bot detection.

---

## Project docs

- [CLAUDE.md](CLAUDE.md) — onboarding guide and working agreement for anyone (human or agent) contributing. Read first.
- [NEXT_STEPS.md](NEXT_STEPS.md) — living build queue: current state, what's next, open decisions.
- [DECISIONS.md](DECISIONS.md) — every architecture and tooling decision with its rationale.

## Status

Actively developed. All five stages have working implementations; a few live paths (some Workday tenants, the
Adzuna apply click-through) are verified against fixtures and pending confirmation on a real residential network
— see [NEXT_STEPS.md](NEXT_STEPS.md).

## License

No license file is currently included, so default copyright applies. A license will be added before a public
release — open an issue if you need clarity in the meantime.

---

<sub>ApplicationBot is an independent, open-source project. It is not affiliated with, endorsed by, or maintained
by Anthropic; "Claude" and "Claude Code" are referenced only to describe the toolchain it runs on. There is no
associated token, cryptocurrency, or paid offering.</sub>

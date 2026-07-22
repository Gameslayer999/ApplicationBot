# ApplicationBot

A personalized, end-to-end job-application pipeline. **[Download the latest release](../../releases)**,
set up your profile and filters, and ApplicationBot discovers matching job openings, tailors your
resume to each one, applies with no human intervention, and tracks every application it submits.

> **Status:** Early development — high-level design only. Architecture, tech stack, and
> module boundaries are still being defined.

## What it does

1. **Configure** — you supply a profile: contact details, a base resume, and filters
   describing the jobs you want (roles, keywords, location/remote, pay range, seniority,
   company type, etc.). Filters drive both discovery and auto-apply.
2. **Discover** — scrapes job boards and company career pages for openings that match
   your filters, extracting each posting's details (title, company, location,
   description, requirements, pay, application portal, application method).
3. **Tailor** — automatically customizes your resume (and optionally a cover letter) for
   each posting based on its job description.
4. **Apply** — fully fills out and submits the application through the posting's
   form/portal, with no human intervention.
5. **Track** — records every application with notes: pay rate, application portal,
   location, company, role, status, date applied, and the tailored resume used.

## Built to be cloned

ApplicationBot is meant to be used by anyone. Nothing about a specific user is baked
into the repo — your profile, resume, filters, and application history all live in your
own local (git-ignored) config. Download a release, configure, run.

## Safety

Real applications are irreversible, so submission is gated by a deliberate safety
switch:

- **`dry_run` is the default** — the pipeline does everything except the final submit,
  recording what it *would* have sent. Real submission requires you to explicitly arm
  the system.
- A **global kill switch** halts all submission immediately.

See Agent Guideline #3 in [CLAUDE.md](CLAUDE.md).

## Repository docs

- [CLAUDE.md](CLAUDE.md) — onboarding guide and working agreement for anyone (human or
  agent) contributing to the project. Read this first.
- [NEXT_STEPS.md](NEXT_STEPS.md) — living build queue: current state, what's next, and
  open decisions.
- [DECISIONS.md](DECISIONS.md) — architecture and tooling decisions with their rationale.

## Getting started

### Get it — download the latest release (recommended)

**The easiest way to run ApplicationBot** — no need to clone the repo or build anything. Grab it
from the **[latest release](../../releases)**:

- **macOS — the desktop app (no Python needed):** download **`ApplicationBot.app.zip`**, unzip, drag
  **`ApplicationBot.app`** into your Applications folder, and double-click. First launch after
  downloading needs one quick trust step — see [Install the desktop app](#install-the-desktop-app-macos)
  below.
- **Windows / Linux (or macOS from source):** download the release's **source zip**, unzip, and run
  the launcher — **`ApplicationBot.bat`** (Windows), **`./scripts/run.sh`** (Linux), or
  **`ApplicationBot.command`** (macOS; first launch: right-click → **Open**). Needs **Python 3**; the
  launcher sets up the virtualenv, dependencies, and the automation browser (Chromium) on first run.
  It's idempotent — safe to re-run any time.

Then, in the app, follow the **✨ Finish setup** walkthrough: add your details and résumé, choose what
jobs to find, and run your first dry-run. Nothing is ever submitted until you deliberately arm the
safety switch.

> **Claude connection (optional).** Tailoring uses your **Claude subscription** via
> [Claude Code](https://claude.com/product/claude-code) (recommended — not metered; sign in
> inside Claude Code). No Claude Code? Add your own **Anthropic API key** as a fallback
> (pay-per-token, kept in your OS keychain) from the app's bottom-left "Claude connection"
> panel. With neither, the free `rules` engine works with no account at all.

### Install the desktop app (macOS)

ApplicationBot ships as a self-contained Mac app — its own window, no browser, no Python, no setup.

**Option A — download the app (most people):**

1. Download **`ApplicationBot.app.zip`** from the [latest release](../../releases).
2. Double-click the zip to unzip it, then **drag `ApplicationBot.app` into your Applications folder**.
3. **First launch only** — because the app isn't signed by an Apple-registered developer, macOS
   blocks it once. Get past it one of these ways (after that it opens normally, forever):
   - **Right-click** (or Control-click) the app → **Open** → **Open**; **or**
   - if that's greyed out (macOS Sequoia and later): open **System Settings → Privacy & Security**,
     scroll to *"ApplicationBot was blocked…"*, click **Open Anyway**, then confirm; **or**
   - in Terminal: `xattr -dr com.apple.quarantine /Applications/ApplicationBot.app`
4. Double-click to launch. The in-app **✨ walkthrough** sets you up (details, résumé, filters), and
   the Apply-stage browser (Chromium) downloads quietly in the background on first run.

> Why the block? The app is **ad-hoc signed** (free), which lets it run but isn't Apple-*notarized*
> (which needs a paid Apple Developer account). Notarization would remove that first-launch prompt;
> nothing else changes. The app is **Apple-Silicon (arm64)**.

**Option B — build it yourself from source:**

```bash
./scripts/build_macapp.sh      # builds a self-contained ApplicationBot.app in this folder
```

A locally-built copy has no download quarantine, so it launches with no warning — just drag it to
Applications (or double-click in place).

**What the app is:**

- **Fully self-contained** — bundles its own Python, all dependencies, and the code. No Python
  install, no setup step, and **no file-access prompts** (it reads nothing from your Documents folder).
- **Your data** lives in `~/Library/Application Support/ApplicationBot/` (profile, résumé, filters,
  application history) — independent of any source checkout.
- It's a **production snapshot**: it does *not* reflect live repo edits — rebuild (or ship a new
  release) to update it.

**Developing / testing?** Use localhost, which runs your *live* repo with auto-reload:
`./scripts/dev.sh` (browser) or `./scripts/run.sh --window` (native window).

To cut a release yourself: `./scripts/release.sh` (dry run — prints the plan and changes
nothing) then `./scripts/release.sh --publish`.

### For developers (CLI)

The first stage — **resume customization (Tailor)** — is implemented. It takes a base
resume (structured YAML, the source of truth) and a job description, produces a tailored
resume that stays factual and keeps your resume's format, and renders it to Markdown.

```bash
pip install -r requirements.txt

python -m applicationbot.cli path/to/job_description.md \
    --resume examples/sample_resume.yaml --out tailored.md
```

- The base resume is structured data, so tailoring **selects, reorders, and rephrases** —
  it can't invent experience. A drift check flags any skill/role/certification that
  isn't in the base resume, whatever engine ran.
- Output preserves your resume's section order, categorized skills, and layout.
- `examples/sample_resume.yaml` is synthetic test data. Drop in your own resume in
  `profile/` (git-ignored) and use `--resume profile/resume.yaml`.

### Tailoring engines (`--backend`) — subscription primary, API key fallback

The engine is pluggable and defaults to `auto`:

| `--backend` | Needs | Quality |
|---|---|---|
| `claude-code` | Claude Code installed + signed in — uses your **Claude subscription** (Pro/Max), **not** the paid API | Best — rewrites bullets to match the posting |
| `anthropic-api` | Your own **Anthropic API key** (console.anthropic.com), stored in the OS keychain — **metered**, pay-per-token | Same rewriting as `claude-code`, billed to your API account |
| `rules` | **Nothing** — no LLM, no account, no network | Reorders/selects by keyword; doesn't reword |
| `auto` (default) | — | **Claude subscription** (Claude Code) → else your **API key** → else rules |

**Claude subscription is primary.** The best path shells out to the Claude Code CLI
(`claude -p`), which runs on your Claude Pro/Max **subscription** — not the metered API.
Sign-in happens inside Claude Code (`claude`, then `/login`), **not** in this app: Anthropic
restricts subscription login to Claude Code and Claude.ai, so a third-party app like this one
**cannot "log in with Claude"** on your subscription (the Messages API rejects subscription
OAuth). **The API key is the fallback.** If Claude Code isn't available, connect your own
Anthropic API key in the app's bottom-left **"Claude connection"** panel — it's stored in your
OS keychain (never in git or a config file) and billed pay-per-token to your API account,
separate from your subscription. With neither, the `rules` engine needs nothing at all, so the
tool works out of the box with zero setup.

### Reviewing in the browser (web UI)

For easier review than the CLI, start the local web app:

```bash
./scripts/run.sh          # sets up the venv, starts on http://127.0.0.1:8000, opens your browser
./scripts/run.sh 9000     # ...on a different port
./scripts/dev.sh          # DEV: auto-restart on code changes; the browser refreshes itself
./scripts/update.sh       # pull the latest from GitHub, reinstall deps, and restart
./scripts/restart.sh      # stop + start again (picks up code changes)
./scripts/stop.sh         # stop it
```

(Or run it directly: `python -m applicationbot.web [--port 8000]`.)

**Editing the code?** Run `./scripts/dev.sh` (same as `run.sh --dev`). It watches
`applicationbot/` and restarts the server on every save, and the open page reloads itself — so
your changes show up without touching the terminal. The whole UI lives in
[applicationbot/web.py](applicationbot/web.py); see [ui.md](ui.md) before changing it.

**Getting updates.** `./scripts/update.sh` fast-forwards your clone to the latest GitHub commit,
reinstalls dependencies if they changed, and restarts (or lets the dev auto-reloader pick it up).
It refuses to run if you have uncommitted local changes, so it never overwrites your work — `git
stash` first, update, then `git stash pop`. (Windows: `git pull` then re-run `ApplicationBot.bat`.)

Pick a resume, pick a saved job fixture (or paste your own posting), choose an engine, and
hit **Tailor** — the tailored resume renders in the browser (styled to resemble a real
single-column resume) alongside the relevance notes, any factual-drift warnings, and which
engine ran. Zero dependencies (Python stdlib), binds to `127.0.0.1` only, and only reads
files from `profile/`, `examples/`, and `fixtures/job_descriptions/`.

The **Résumé data** tab lets you grow your source-of-truth beyond the uploaded resume — add
experience, activities, or projects that weren't on it, or add bullets to an existing entry.
Tailoring then selects the relevant parts per job.

The other four stages (Configure, Discover, Apply, Track) are still design-only — see
[NEXT_STEPS.md](NEXT_STEPS.md).

## Notes

- Your resume, contact details, application history, and account credentials are
  sensitive and must never be committed to git. See `.gitignore` and Agent Guideline #12
  in [CLAUDE.md](CLAUDE.md).
- Scraping respects each site's terms of service and rate limits.

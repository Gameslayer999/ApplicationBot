<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-darkmode.png">
    <img src="assets/logo-lightmode.png" alt="ApplicationBot" width="200">
  </picture>
</p>

<h1 align="center">ApplicationBot</h1>

<p align="center"><b>The job search that runs on your machine.</b><br>
Discovers matching openings, tailors your résumé to each one, applies for you, and tracks everything — with a dry-run switch and a global kill switch for when you want to watch before it sends.</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20app%20%C2%B7%20source%20anywhere-blue">
  <img alt="Python" src="https://img.shields.io/badge/python-3-blue">
  <img alt="Safety" src="https://img.shields.io/badge/submit-you%20start%20it%20%C2%B7%20kill%20switch-brightgreen">
  <img alt="Powered by Claude" src="https://img.shields.io/badge/powered%20by-Claude-8A63D2">
</p>

ApplicationBot is a personalized, end-to-end job-application pipeline you run yourself. You supply
your profile, a base résumé, and filters describing the jobs you want; it finds matching postings,
customizes your résumé for each, fills out and submits the applications, and records every one. It is
built to be **downloaded and used by anyone** — nothing about you is baked into the repo; your data
lives in a local, git-ignored folder and never leaves your machine except the minimal text a
tailoring or matching call sends to Claude.

> [!WARNING]
> **Submitting an application is irreversible, so every path that submits is one you started deliberately.**
> - **An application is sent when you ask for one.** Every **Apply** button applies — the one on a
>   prepared application and the one on any judged posting in the search breakdown — each confirming
>   first and sending that one. **Starting the auto-apply loop** confirms once and then tailors, fills
>   and submits every match it finds with no further clicks. That is the product.
> - **The loop has a dry-run switch.** Tick **Dry run** on it and it prepares everything and submits
>   nothing: each application waits under *Ready to apply* for your click. The one-off **Find & fill one
>   (dry-run)** panel never submits either.
> - **You can watch a real submit happen.** **Watch it apply ▶** in a prepared application's review submits
>   that one in a browser you watch, and the window stays open on the result; **Show the browser while it
>   applies** does the same for every submit in a run. Both are the same submit as **Apply** — same
>   confirmation, same kill switch, same pre-submit check — only visible.
> - **The command line stays disarmed.** `python -m applicationbot.runner` and the pipeline commands submit
>   only after you set `armed: true` in `profile/safety.yaml` (with a per-run submission cap).
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
   drive both what gets discovered and what gets auto-applied to. A **Location** section on the Profile
   page holds everything about where: your address — including **street address** and **ZIP**, which
   portals that split the address into four required boxes (Jobvite, BambooHR) need — and how the bot
   answers work-location questions: willing to relocate, open to remote, preferred work arrangement
   (with a commute radius for "in-office when the office is commutable"), and ranked preferred office
   locations. No preference? Leave them at "—" / "No preference" — none of it is required. Edit it all from the web UI or in
   `profile/*.yaml`. Already have a résumé? **Upload the PDF or Word (.docx) file** in the import box at the
   top of the Profile page (LinkedIn export import lives behind a button in the same box) and
   Claude reads it into your sections — merging in anything new and leaving what you've already filled
   untouched (or import from a LinkedIn data export). Uploading a second, differently-worded résumé does
   **not** duplicate your history: a role already on file is recognised through its re-wording ("Acme
   Corp." = "Acme Corporation", "SWE Intern" = "Software Engineer Intern"), and the page names every
   entry it matched instead of adding, plus any blanks it filled in on them. A genuine second stint at
   the same employer — different years — is still kept as its own entry. The LinkedIn import dedupes
   the same way, so an export imported on top of your résumé won't re-add a job under LinkedIn's
   wording of the company name or title. Screening questions the bot couldn't answer are
   listed on the same page in the form's own controls — a dropdown question as a dropdown, and a
   **"check all that apply"** question as checkboxes, so you can pick every option that applies and all of
   them get ticked at fill time. A **Languages** section holds the languages you speak and how well
   ("Spanish — Conversational"), which forms ask for constantly and no résumé field carries: a
   *"Language Skill(s) (check all that apply)"* group gets every one of yours ticked, and a
   proficiency question gets the level for the language it names. Questions about **programming**
   languages are never answered from it.
2. **Discover** — pull openings that match your filters from public ATS APIs
   (Greenhouse · Lever · Ashby · SmartRecruiters · Recruitee · Workable), keyless aggregators that
   need no signup (Himalayas · RemoteOK · the Google Jobs vertical), curated early-career GitHub
   feeds, **Adzuna** when you add its free API key, and forwarded job-alert emails — each one opt-in
   in Discovery settings, and each self-skipping if it isn't configured. A cheap keyword
   pre-filter narrows the pool, a two-stage judge (Haiku pre-rank → Sonnet) ranks the survivors by
   qualification fit and names your missing requirements, and a funnel view shows exactly how many
   postings reached each stage — during the auto-apply loop as well as a one-off dry run.
   A posting whose application you **never opened** is brought back by the next search rather than
   buried, so anything prepared while you were away gets a second look. When the source scout stages
   new company boards, **Add all** wires every one of them into discovery in a single click.
   **The fit score filters the automatic queue — it does not overrule you.** Every judged posting in
   that breakdown carries its own button: **Apply** on one that cleared your cutoff, **Apply anyway**
   on one Claude scored below it. Either one applies: it tailors your résumé, fills the form and
   submits it, confirming first. (A loop running in **Dry run** serves the click its own way —
   prepared and held under **Ready to apply** for you, submitting nothing.) Each row carries a second
   button — **Apply as-is** — that does the same thing with your résumé exactly as it stands (your
   uploaded file if you have one, otherwise your base résumé): no Claude call, nothing rewritten. The
   tailoring choice is per posting, on the button you click, not a mode you set first.
3. **Tailor** — Claude rewrites your résumé for each posting — selecting, reordering, and rephrasing what
   you already have. A drift check flags any skill, role, or certification that isn't in your base résumé,
   so it stays factual, and every exported PDF is re-checked to confirm its text layer is machine-readable.
   To save tokens, when a new posting demands essentially the same skills as one it already tailored for,
   it **reuses that résumé** instead of making another Claude call — how similar the two must be is the
   reuse setting in **⚙ Loop settings**, where you can also make the loop re-tailor from scratch every
   time (Track's *Re-run → re-tailor* redoes a single application).
   A dry run can stop here: pick **Tailor the résumé only** and it searches, ranks, and tailors for the best
   match without opening a browser or touching a form — or paste a posting nobody found for you and tailor
   against that. The rendered résumé, its drift warnings, and the PDF appear right there.
   **Your own résumé outranks any generated one:** upload a PDF résumé on the Profile tab and any job whose
   demanded skills it already covers is sent that exact file — no tailoring at all. Kept résumés are listed
   under the upload box and removable in one click.
   Track, the ready-to-apply notification, and the review panel each show whether a submission used a
   **freshly tailored** résumé, a **reused** one, **your uploaded file**, or an **untailored** one you
   asked to send as-is, so it is never a surprise.
4. **Apply** — a real browser (Playwright) fills and submits the application through the posting's own
   ATS: Greenhouse · Lever · Ashby · SmartRecruiters · Recruitee · Workable · **Jobvite** · **BambooHR**,
   including multi-page wizards and account-gated **Workday** (automated account creation, credentials
   in your OS keychain). Greenhouse forms are filled without any account; if you have a MyGreenhouse
   account you can optionally turn on **Quick Apply** under Profile → Native autofill logins, which
   signs in with the security code Greenhouse emails — that needs your MyGreenhouse address to be the
   inbox you linked in Settings, and stores no password. Portals that put the form behind an account it cannot create yet —
   **iCIMS, Taleo, Avature** — are named as needing a sign-in and parked there rather than half-filled,
   and are kept out of the search so they don't spend judging on openings that can't be applied to.
   A **honeypot** field (a box the form expects to come back empty, used to catch bots) is left alone
   and reported, never filled, and a **cookie-consent banner** covering the form is dismissed with the
   narrowest choice it offers — refusing non-essential cookies wherever refusing is on the menu.
   Applications that get blocked (a question it can't answer, a login, a
   CAPTCHA) are *parked* so you can resolve and resume them. Every submit is gated by the safety switch above.
   A site that **refuses automated traffic** (a bot wall, e.g. DataDome's "Access is temporarily restricted")
   is reported as exactly that — not as a missing form and not as a CAPTCHA you could solve — and parked as
   **Try again**, since nothing on your side is broken. ApplicationBot never tries to get around such a wall.
   Sites word the button that opens their form differently ("Apply", "I'm interested", "Join our team"); the
   known wordings are built in, and setting `nav_agentic: true` in `profile/safety.yaml` (off by default,
   spends Claude tokens) lets a Claude worker open an unknown one **once** and remember the route, so every
   later posting on that site opens for free.
   Dropdowns don't have to spell things your way: a school picker that lists *"Penn State
   University-University Park"* still matches a résumé that says *"The Pennsylvania State University"*
   (abbreviations and typos included, main campus preferred over a branch), and when a school genuinely
   isn't in the list it picks the form's own **"Other"** — then tells you in the application's review
   panel that your real answer wasn't offered, instead of leaving a required field blank.
   Before you sign off, that **review panel** shows the exact answers it will submit — and every one of them is
   **editable**. Type over any answer (or fill in one it couldn't answer) and that value is what gets
   submitted the next time this application is filled, including the real submit; unsaved edits are saved
   for you when you click *Watch it fill*, *Watch it apply* or *Apply*. Clearing a box hands the field back
   to the bot. **Re-tailor résumé** there (or **Tailor this résumé**, when the application is set to send
   yours as-is) rewrites the résumé *that one application* submits, from the posting's saved job
   description, then re-fills the form with it — every other application keeps its own, and the loop's
   own default stays whatever ⚙ Loop settings says. **Review opens as a popup over the page**, so the
   lists themselves stay slim one-line rows — Discover and Notifications alike — and every review
   looks and works the same wherever you opened it. Close it with *Close review* at the bottom, the
   ✕, `Esc`, or a click outside; nothing is lost, and reopening reloads it. Ready applications sit
   **above** the list of judged postings, so the review is never buried under a long search breakdown.
   **That ready list outlives the run that built it.** Discover and the **Notifications** tab show the
   same one: an application prepared in an earlier run — or before you quit and reopened
   ApplicationBot — is still sitting under *Ready to apply* in Discover, with its full review and its
   **Apply ▶**, instead of disappearing the moment the loop stops or the next run starts.
   A **"check all that apply"** question is edited there as checkboxes too — the same widget as the
   Profile page — so every option you tick is ticked on the form. A **dropdown** (or a Yes/No
   question) is edited as that dropdown, offering the form's own options, so you can't accidentally
   type an answer no option matches — and "Type a different value…" is always there when the list
   doesn't carry your answer.
   **A field whose label says nothing is read in context.** `Date`, `Name`, `Other`, *"If yes,
   please explain"* — these name a format, not a question, and the form says what they mean in the
   heading above them and the field before them. The fill reads that neighbourhood and answers from
   it: a `Date` after a **Signature** gets the day you apply, the same `Date` inside an education or
   employment block is left for you rather than stamped with today, and anything it has to ask
   Claude about is asked *with* the surrounding text so the question is the one the form is really
   asking. The review panel prints what it read under each such answer ("on the form: Applicant
   certification · follows the field: Signature").
   **Repeated fields are all filled.** A form that shows the same label twice — two education blocks,
   a wizard asking again on a later page — used to get the first one filled and the rest silently
   skipped. Each control is now identified in its own right (the second appears as `School #2` in
   review, with its own answer and its own edit box) while still being answered as the question it
   asks.
   **Answers that don't fit their question are called out.** A filled box can still be wrong: a form's
   bottom-of-page **Date** once got *"I'm available immediately…"* — the right answer to a different
   question. Every answer is checked against the shape its question asks for (a date field answered with
   prose, a "how many" with no number, an email without an `@`, a Yes/No answered with a paragraph, a
   *which/why* answered *"Yes"*), and a mismatch turns the row amber with **Check this** and the reason
   printed right above the box that fixes it. Answers Claude drafted or picked are labelled
   **AI-drafted** / **AI-picked** for the same reason. That bottom **Date** now fills with the day you
   apply, and a native date picker is filled too.
   Every question is badged **Required** or **Optional** exactly as the form marks it, with the counts
   above the list, so you can see at a glance what actually has to be answered to submit — and the
   unanswered list says how many of *those* are required and therefore blocking. **Rescan questions**
   re-reads the live form in the background (no window opens, nothing is submitted) and refreshes the
   whole panel: the questions the posting asks now, their control types, their required marks, and the
   answers the bot produces today. Use it when a posting changes its form, or on an application prepared
   before a feature landed.
   **It also learns from your edits:** a reusable answer ("How many years of Python do you have?") is added
   to your answer bank, so the next posting that asks it is filled in instead of coming back blank — and
   an answer you correct replaces the one it got wrong. Company-specific answers ("Why Acme?") and EEO
   questions stay on that posting alone, and if the field is one your apply profile owns (email, work
   authorization) the panel says so and links you to the profile, rather than pretending it was learned.
   The web UI's **auto-apply loop applies for you by default**: it finds a match, tailors, fills and
   **submits** it, then moves to the next one — no click per application. Starting it confirms once; **Stop**
   ends it after the current step, and `profile/KILL` halts every submit instantly. Tick **Dry run** on the
   loop to prepare everything and submit nothing. **Show the browser while it applies**, next to it, fills
   and submits every application in a window you can watch before it closes itself and moves on. To watch
   just one, open a prepared application's **Review** and use **Watch it apply ▶** — that window stays
   open on the result until you close it. **⚙ Loop settings** (on the panel) holds what governs a
   run, set before you start it: which résumé each application gets (tailor and reuse when the skills match ·
   always re-tailor · **only tailor when the fit is under N/100**, sending your résumé as-is above that ·
   never tailor), the minimum fit, how similar two postings must be before an already-tailored résumé is
   reused, and a **submission cap** — a ceiling on how many applications one run may send, whatever the goal
   says. It can run to a goal — *"keep going until 5 applications are done"* — and it means it: when a pass turns up nothing new it backs off (1 min, then longer, up to
   30 min) and searches again, each pass judging the next-best postings it hasn't scored yet, until that many
   are submitted (or, in a dry run, ready) or you hit **Stop**. The status line always says how close it is and when the next pass runs, and each
   pass shows its own search breakdown — the funnel plus every posting Claude judged, accepted or denied.
5. **Track** — every application is recorded in a local SQLite database with company, role, location, pay,
   portal, status, date, fit score, and the exact tailored résumé used — viewable and editable in the Track
   tab, with funnel and calibration reports. Applied to things by hand, or before you started using this?
   Forward those emails to your linked inbox and click **Import from inbox**: every "thank you for applying"
   becomes a row, and every rejection or interview invite moves an existing one's status. Imported rows are
   flagged with the email they came from, and the whole import undoes in one click.

Discovery, tailoring, filling, and submission run with **no human in the loop** once you start the
auto-apply loop — that is the point of the tool. Its **Dry run** switch, the one-off dry-run panel, and
`profile/KILL` are there for when you want to watch it work before it sends anything — and **Show the
browser while it applies** / **Watch it apply ▶** are there for when you want to watch it send.

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

> [!NOTE]
> **The published app lags this README.** The latest release is **v0.1.0** (2026-07-21); features added
> since — Jobvite/BambooHR fill, the Oracle work, MyGreenhouse Quick Apply, the unattended night run — are
> in the source, not in that download. Run from source (Option B) if you want them.

### Option B — run from source (CLI + web UI, any OS)

For developers, or Windows/Linux users.

```bash
git clone https://github.com/Gameslayer999/ApplicationBot.git
cd ApplicationBot
./scripts/run.sh            # sets up the venv + Chromium, serves http://127.0.0.1:8000
```

- It does **not** open a browser — visit or reload `http://127.0.0.1:8000` yourself.

- **Windows:** run **`ApplicationBot.bat`**. **Linux:** `./scripts/run.sh`. **macOS from source:** `ApplicationBot.command`
  (first launch: right-click → **Open**).
- The launcher is idempotent — safe to re-run any time. It creates the virtualenv, installs dependencies, and
  downloads the automation browser on first run.
- Prefer a native desktop window over a browser tab? `./scripts/run.sh --window`.

---

## Quick start

Whichever way you installed, the flow is the same:

1. **Finish setup.** A 20-second tour runs on first launch (reopen it any time with **Take the tour** at the
   bottom of the nav) and points at the two things to do: add your details and résumé on **Profile**, and choose
   which jobs to find on **Discover** — each also prompted by a first-visit note on the page itself. (From source you can instead copy the templates in [`examples/`](examples/) into `profile/`:
   `sample_resume.yaml`, `discovery.example.yaml`, `safety.example.yaml`.)
2. **Connect Claude (optional but recommended).** Sign in with Claude Code for the best tailoring on your
   subscription, or add an Anthropic API key in the bottom-left **"Claude connection"** panel. With neither, the
   free `rules` engine works with no account.
3. **Discover + dry-run apply.** The app opens on **Discover** — hit **▶ Find & fill one (dry-run)**
   there (or, from the CLI, `python -m applicationbot.pipeline --apply-first`). Watch it discover a match, tailor your résumé, and
   fill the form live. **It never submits.**
4. **Let it apply.** Hit **▶ Start applying** on the auto-apply loop and confirm once: it then finds, tailors,
   fills and **submits** application after application on its own. Tick **Dry run** first if you'd rather it
   prepared them for your click. Drop a `profile/KILL` file to stop every submit instantly. (The CLI is
   separate: it submits only with `armed: true` in `profile/safety.yaml`.)
5. **Or let it run overnight.** `./scripts/night.sh --goal 100 --until 07:00 --arm` runs a whole unattended
   session against a target and reviews its own work in the morning — nothing is ever asked of you mid-run.
   Arming lasts only for that night: `profile/safety.yaml` is put back exactly as it was when the run ends.
   The agent contract for driving it is [docs/AGENT_NIGHT_RUN.md](docs/AGENT_NIGHT_RUN.md).

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
| `python -m applicationbot.runner [--max N] [--continuous]` | Autonomous loop over every cleared match (dry-run by default). `--continuous` = a **watch**: re-checks your boards every `--interval` min and sends a desktop/phone notification each cycle a role is ready to apply |
| `python -m applicationbot.night --goal 100 --until 07:00 --arm` | **Unattended night run**: keeps discovering, tailoring, filling and submitting across cycles until 100 applications land or 07:00 arrives, never asking anything. Writes `nights/<timestamp>/` (`events.jsonl`, `summary.json`, `report.md`) and exits with a code that says how it ended. `--dry-run` rehearses it without submitting; `--preflight-only` just checks readiness |
| `python -m applicationbot.night --goal 100 --until 07:00 --arm --no-tailor` | Same night, but sends **your** résumé exactly as it stands — no Claude tailoring call per application, so nothing you have never read goes out (and the biggest per-application token cost disappears). Without the flag the night follows the résumé policy saved in **⚙ Loop settings** |
| `python -m applicationbot.night review` | Audit the last night: were the submissions real (tracker-confirmed), what failed and is it getting worse, is `min_fit` calibrated. Writes `review.md` + a ranked `findings.json` |
| `./scripts/night.sh --goal 100 --until 07:00 --arm` | Preflight → night → review as one idempotent command (what a cron line calls) |
| `python -m applicationbot.cli JD.md --resume R.yaml --out out.pdf` | Tailor a résumé to one job description (CLI) |
| `python -m applicationbot.apply URL --pdf resume.pdf --dry-run` | Fill one application by URL with an already-rendered résumé PDF (`--pdf` is required; add `--resume profile/resume.yaml` for the contact details) |
| `python -m applicationbot.doctor` | Read-only health check (Claude sign-in, Chromium, résumé, safety state) |
| `python -m scripts.prune_seen_ledger [--apply]` | One-time repair: drop postings from the "already shown" ledger that Claude never actually judged, so discovery can consider them again (dry-run without `--apply`) |
| `python -m applicationbot.tracker [funnel\|calibration]` | Inspect tracked applications and reports |
| `python -m applicationbot.inbox_import run [--days 30] [--limit 50]` | Import application emails from the linked inbox into the tracker; `status` shows what's been imported, `undo RUN_ID` reverses one run |
| `python -m applicationbot.mailbox link\|status\|test` | Link the bot inbox (Workday email verification, job-alert ingest, application-email import) |

**Tailoring engines** (`--backend`, defaults to `auto`):

| `--backend` | Needs | Quality |
|---|---|---|
| `claude-code` | Claude Code signed in — your **subscription**, not the metered API | Best — rewrites bullets to match the posting |
| `anthropic-api` | Your own **Anthropic API key** (OS keychain) — **metered**. Not a `--backend` value on the CLI: `auto` picks it up when a key is stored | Same rewriting, billed to your API account |
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
- `safety.yaml` — the arm switch, the per-run submission cap (edited in the UI's **⚙ Loop settings**,
  and honoured by both the loop and the command-line runner), and the opt-in agentic fallbacks
  (`nav_agentic`, `workday_agentic` — both off by default; they spend Claude tokens to learn a site once).
- `notifications.yaml`, `mailbox.yaml` — optional desktop/phone push (also logged in the
  **Notifications** tab, so every alert is kept and dismissible) and the bot inbox link.
- `applications.db` — your tracked history. It sits beside `profile/`, not inside it (the repo root from
  source; the data folder itself in the app), and `profile/applications/` holds the per-application archives.
- `uploads/` — the PDF résumés you uploaded, kept so a closely-matching job can be sent your own file
  instead of a tailored one (remove any of them under the Profile tab's upload box).
- `inbox_import_seen.json` — which inbox messages have already been imported into the tracker, so a
  re-scan never duplicates a row (and each import stays undoable).

Template versions of these live in [`examples/`](examples/). Run `python -m applicationbot.doctor` any time to
confirm your setup is healthy.

---

## Privacy & safety

Your résumé, contact details, credentials, and application history are sensitive and are treated that way:

- **A question is only left for you after Claude has checked your own data.** Anything the rules and
  your answer bank can't fill is re-read against your résumé, profile facts and previous answers in one
  batched call — it answers only what that data actually settles, never a guess, and never demographic
  questions. Those answers are marked `derived` in the report so you can see what was inferred, and
  every fill says "Re-read your résumé and profile for N question(s) — answered M, left K for you".
- **Personal data never enters git.** Everything above is covered by `.gitignore` and stays on your machine.
  Only the minimal text a matching or tailoring call needs is ever sent to Claude.
- **Credentials go in your OS keychain**, never in plaintext YAML — the Anthropic API key, and any Workday
  account passwords.
- **Submission only happens on paths you start** — an Apply click, the auto-apply loop you confirmed, or a night
  run you armed with `--arm`, each with a dry-run switch and a global kill switch (see the warning at
  the top). A night run's arming is temporary: `profile/safety.yaml` is restored when it ends, so an armed
  night cannot carry over into the next day.
- **An unattended night keeps its own record.** Every application it attempts is written to `nights/<timestamp>/`
  (git-ignored, like the tracker), and `python -m applicationbot.night review` re-checks every submission it
  claimed against the tracker before that number is reported to you.
- **Scraping respects each site's terms and rate limits.** ApplicationBot does not build functionality whose
  purpose is to evade bot detection.

---

## Project docs

- [CLAUDE.md](CLAUDE.md) — onboarding guide and working agreement for anyone (human or agent) contributing. Read first.
- [NEXT_STEPS.md](NEXT_STEPS.md) — living build queue: current state, what's next, open decisions.
- [DECISIONS.md](DECISIONS.md) — every architecture and tooling decision with its rationale.
- [docs/AGENT_NIGHT_RUN.md](docs/AGENT_NIGHT_RUN.md) — the contract an agent follows to run a night alone:
  preflight, arming, the exit codes, the self-review, and how far it may improve the code on its own.

## Status

Actively developed. All five stages have working implementations; a few live paths (some Workday tenants, the
Adzuna apply click-through) are verified against fixtures and pending confirmation on a real residential network
— see [NEXT_STEPS.md](NEXT_STEPS.md). **Oracle Recruiting Cloud** (the largest single source of postings in the
curated feeds) is reached and filling but not yet finished: its address block needs a street address and ZIP in
your profile, so its postings are still held back from the search until that path is confirmed end-to-end.

Two paths are shipped but not yet proven against the real thing: **MyGreenhouse Quick Apply** is written
against the emailed-code sign-in but has never run on a live MyGreenhouse account (its selectors are
best-effort — Greenhouse forms fill without it, which is the default), and the **unattended night run** is
covered by unit tests and a real dry-run night, but no armed night has been run yet, so its numbers at a
100-application scale are unproven. Start smaller than 100 the first time.

## License

No license file is currently included, so default copyright applies. A license will be added before a public
release — open an issue if you need clarity in the meantime.

---

<sub>ApplicationBot is an independent, open-source project. It is not affiliated with, endorsed by, or maintained
by Anthropic; "Claude" and "Claude Code" are referenced only to describe the toolchain it runs on. There is no
associated token, cryptocurrency, or paid offering.</sub>

# CLAUDE.md — ApplicationBot Onboarding Guide

> Read this file completely before taking any action on this project.
> This file is the single source of truth for any new agent continuing development.

---

## Project Overview

**ApplicationBot** is a personalized, end-to-end job-application pipeline. It is meant
to be **cloned and used by anyone**: each user supplies their own profile, resume, and
job-search filters, and the program discovers matching openings, applies to them with
no human intervention, and tracks every application it submits.

At a high level, the system has five stages:

1. **Configure** — the user sets up a profile: personal/contact details, a base resume,
   and **filters** describing what they want (roles, keywords, location/remote, pay
   range, seniority, company type, etc.). These filters drive both what gets discovered
   and what gets auto-applied to.
2. **Discover** — scrape job boards and company career pages for openings that match the
   user's filters, and extract each posting's structured details (title, company,
   location, description, requirements, pay, application portal, application method).
3. **Tailor** — automatically customize the resume (and optionally a cover letter) for
   each posting based on its job description.
4. **Apply** — fully fill out and submit the application through the posting's
   application form/portal, with no human intervention.
5. **Track** — record every application in a tracking system with notes: pay rate,
   application portal, location, company, role, status, date applied, and the tailored
   resume used.

**Key properties:**
- **Personalized & cloneable** — no user's data is baked into the repo; everything
  specific to a user lives in their own (git-ignored) config/profile.
- **Filter-driven** — the user controls what is discovered and what is auto-applied to.
- **Fully automated** — discovery, form-filling, submission, and tracking run without a
  human in the loop (subject to the safety switch in Agent Guideline #3).

> **Status:** Early development — high-level design only. Architecture, tech stack,
> and module boundaries are still being defined. This overview will be fleshed out
> as the project takes shape.

---

## ⚠ Agent Guidelines — Read First

These rules apply to every agent working on this project:

**0. Always build toward the final product.** Keep the end goal — the five-stage,
fully-automated, cloneable job-application pipeline in the Project Overview — in mind at
all times. Before starting any task, be able to state how it moves the project toward
that final product. If you don't understand how the current piece factors into the end
result — why it exists, which stage it serves, what depends on it — **stop and figure
out why before writing code.** Ask the user if the connection is still unclear. Never
build something just because it was requested or because it seems locally reasonable; a
task that doesn't advance the final product, or that you can't tie back to it, is a
signal to pause and reassess, not to proceed. Every change should measurably move the
pipeline closer to working end-to-end.

1. **Get approval before large architecture changes.** If a decision affects file
   structure, database schema, module boundaries, the scraping strategy, or the
   application-submission method — stop and explain the options to the user before
   writing any code.

2. **Flag better alternatives.** If you see a simpler, cheaper, or more robust way to
   accomplish something than what's currently planned, say so. Don't just silently
   implement what was previously decided if there's a meaningfully better path.

3. **Full automation, gated by a safety switch.** The product's intended behaviour is
   to fill out and submit applications with no human intervention. But because
   submission is irreversible and outward-facing, real submits must be gated by a
   deliberate safety switch, not fired by default:
   - **`dry_run` is the default.** In dry-run, the pipeline does everything *except* the
     final submit — it fills the form, tailors the resume, and records what it *would*
     have submitted. Real submission requires the user to explicitly arm the system
     (e.g. a config flag / CLI flag).
   - Provide a **global kill switch** that halts all submission immediately.
   - During development, never submit against a real posting; use dry-run or a test
     target. Once armed by the user, per-application confirmation is **not** required —
     that is the point of the product.

4. **Respect sites and the law.** Follow each site's terms of service and robots
   directives, rate-limit scraping, and avoid abusive request patterns. Do not build
   functionality whose primary purpose is to evade bot detection for prohibited use.

5. **Protect personal data.** The applicant's resume, contact details, and account
   credentials are sensitive. Never commit personal information or secrets to git
   (see Agent Guideline #12 and `.gitignore`), and never send more personal data to
   external services than a task actually requires.

6. **Test incrementally.** Don't write 500 lines of new code and ask the user to test
   it all at once. Build in small, verifiable steps.

7. **Preserve existing behaviour.** When changing code, keep its observable behaviour
   the same unless the user explicitly asks you to change it. Bug fixes, refactors, and
   performance work should fix the defect without altering inputs, outputs, side effects,
   or interfaces that callers rely on. If you believe a behaviour change is warranted,
   stop and propose it first — don't fold it silently into an unrelated change.

8. **Everything must be replicable — no one-off manual steps.** If a task required human
   intervention once (clicking through a UI, running ad-hoc commands, hand-editing a
   service, capturing coordinates), capture it in a single re-runnable script before
   considering the task done. Doing it a second time should mean running one script, not
   repeating the manual steps. Scripts must be idempotent and safe to re-run in any
   system state. Manual intervention is a bug to be scripted away, not a workflow.

9. **Record every decision in `DECISIONS.md`.** Any significant choice — architecture,
   tooling, service model, data layout, integration method, or a reversal of a prior
   decision — must be appended to `DECISIONS.md` with its context, the options
   considered, the choice, and the reasoning. Update the Decision Index there too.
   Code and scripts capture *what* the system does; `DECISIONS.md` captures *why*.
   If you make a decision and don't log it, the task isn't finished.

10. **Keep `NEXT_STEPS.md` current.** At the end of every session where you add, change,
    or remove functionality, update `NEXT_STEPS.md`:
    - Move finished work to **Recently completed** (with date).
    - Add newly discovered work to **Now**, **Next**, or **Later**.
    - Refresh **Current state** if something material changed (services, counts, blockers).
    - Record unresolved choices in **Decisions needed** (then log the decision in
      `DECISIONS.md` once the user chooses).
    Read `NEXT_STEPS.md` at the start of each session to pick up where the last agent
    left off. If the task isn't finished, the next-steps update isn't finished either.

11. **Be precise, descriptive, and concise.** Say exactly what happened — no vague
    summaries, no hand-waving. This applies to everything: user-facing messages, logs,
    commit messages, code comments, and status updates. Prefer the specific fact over
    a general impression; cut filler that doesn't help someone act on the information.
    When something fails — in the app, a script, or a service — report the exact error
    (message, code, or observable symptom), what triggered it, and the root cause once
    you know it. Do not say "something went wrong" or "there was an issue" when you can
    state what actually failed and why.

12. **Personal information stays out of git.** The applicant's PII and PII-shaped data
    must be covered by `.gitignore` and must never be staged or committed. This
    includes: resumes and cover letters, contact details, application history,
    scraped-posting databases that may contain personal notes, and credentials
    (`.env`, site logins, API keys, session cookies). Before any commit, verify with
    `git status` that no PII or secret paths appear. If `.gitignore` is missing such a
    location, add it before committing code that writes there.

---

## UI Design Principles

These rules apply to any user-facing interface in ApplicationBot — dashboards, status
screens, application queues, and review workflows.

1. **Intuitive by default.** Every interface should be usable without explanation. Each
   button, link, and control must be clearly labeled with exactly what it does ("Log in
   with Claude", not "Continue"; "Tailor résumé", not "Go") — a user should never have to
   guess what an action will do, or hunt for how to start it. Prefer one obvious path over
   several ambiguous ones. Setup a user needs should already be done (script it — see
   Agent Guideline #8) so the interface presents a working button, not a to-do list.

2. **Blocked-work buttons go straight to the fix.** If the UI shows that work is
   blocked (e.g. an application needs manual input, a login expired), the action to
   resolve it must take the user exactly where they need to be — one click, no detours.
   The button label should say what is blocked; the click should land on the specific
   field, setting, or step that unblocks it.

3. **Errors and notifications are actionable and lead to the fix.** Whenever something
   stops the system or a user must be notified, the message must state exactly what went
   wrong and exactly how to fix it — and, wherever possible, take the user straight to the
   place where they can act on it (the specific field, setting, button, or step), not just
   describe it. No vague errors ("something went wrong"), no bare error codes. The user
   should finish reading knowing both the problem and the next step — and be one click from
   taking it.

4. **User instructions must be precise and concise.** Labels, tooltips, empty states,
   onboarding copy, and inline help should say exactly what the user needs to know —
   nothing more. Prefer the specific fact over filler; cut words that do not help
   someone act. Every instruction should earn its place on screen.

5. **Never leave the user waiting in silence.** Any action that isn't instant — tailoring a
   résumé, exporting a PDF, saving, importing, loading, submitting an application — must show
   its status the whole time it runs, using **one consistent pattern** across the whole app:
   - **Disable the trigger** and show a spinner + a *specific* working label on it ("Tailoring…",
     "Generating PDF…", "Saving…") — never a dead, clickable-looking button.
   - **Show an in-place status** (spinner + what is happening) where the result will appear.
   - **For anything that can take more than a couple of seconds** (e.g. a Claude call), show
     **elapsed time or real progress** so it never looks frozen.
   - **End in a definite state:** the result, a clear success marker ("Saved ✓"), or an
     actionable inline error (Principle #3) — never just silently stop.
   - When the system decides to **drop, skip, or truncate** something the user provided (e.g. a
     résumé entry omitted to fit the length budget), **say so** — silence reads as "ignored my
     input" and is a bug, not a clean result.
   Do not invent a new spinner/toast/label style per feature; reuse the shared one so waiting
   always looks and behaves the same.

---

## AI Coding Guidelines (Karpathy)

Follow these principles on every coding task. They complement the Agent Guidelines
above and take precedence over default model instincts toward over-building.

### 1. Think Before Coding

- **Never assume blindly.** If a requirement has multiple interpretations, ask for
  clarification instead of silently guessing.
- **Surface confusion.** State assumptions explicitly and name what is unclear before
  writing a single line of code.
- **Push back.** If a request is technically overcomplicated or redundant, suggest a
  simpler approach before implementing it.

### 2. Simplicity First

- **Write minimum code.** Do not add unrequested features, speculative
  "future-proofing," or single-use abstractions.
- **Ruthless compression.** If 50 lines solve the problem, 200 lines are unacceptable.
- **Avoid over-configurability.** Do not add configurations or flexibilities unless
  they were explicitly requested.

### 3. Surgical Changes

- **Touch only what is necessary.** Modify strictly the lines mandatory for the
  current task.
- **No drive-by refactoring.** Do not improve adjacent formatting, comments, or
  refactor existing code that is not broken.
- **Clean up only your own mess.** Remove unused variables or imports that your own
  changes introduced; leave pre-existing dead code untouched.

### 4. Goal-Driven Execution

- **Use verifiable success criteria.** Turn vague instructions like "fix the bug"
  into declarative goals: e.g. write a test that reproduces the bug, then make it pass.
- **Tighten the leash.** Work from a clear objective, boundaries, and metric — then
  loop until met. Weak criteria ("make it work") inevitably require human intervention.

---

## Agent Decision Framework

When you encounter a choice during development, follow this process:

1. **Is it a small implementation detail?** (variable name, minor refactor, print formatting)
   → Decide and implement. No approval needed.

2. **Does it affect scraping strategy, how data is stored, how resumes are tailored, or
   how applications are submitted?**
   → Stop. Present a table of options with pros/cons and your recommendation.
   → Wait for explicit user approval before writing code.

3. **Is there an easier way than what's planned?**
   → Say so before implementing the planned approach. Example:
   *"The plan calls for X, but Y would achieve the same result with less code
   and no additional dependencies. My recommendation is Y — want me to proceed that way?"*

4. **Would the action be outward-facing or irreversible?** (submitting an application,
   sending an email, creating an account)
   → Respect the safety switch (Agent Guideline #3): default to `dry_run`, and only
   perform real submissions when the user has explicitly armed the system. Never submit
   against a real posting during development.

---

## Quick Start Checklist for a New Agent

- [ ] Read this entire file
- [ ] Read `NEXT_STEPS.md` — current build queue and blockers (once it exists)
- [ ] Read `DECISIONS.md` for architecture rationale (once it exists)
- [ ] Confirm the current state of the project with the user
- [ ] Ask the user what specific task they want to work on today
- [ ] Never submit against a real posting during development — default to `dry_run` and
      only submit when the user has explicitly armed the system (Agent Guideline #3)
- [ ] Never stage or commit PII or secrets — confirm `.gitignore` covers new data paths
      (Agent Guideline #12)
- [ ] Before ending the session: update `NEXT_STEPS.md` if anything changed
      (Agent Guideline #10)

---

## Branching & releases

**`master` (main) is the source of truth for releases. `development` is the working branch.**
(Decision 112.)

- **Do all work on `development`** — never commit directly to `master`.
- **Releases push to main:** bring `development` onto `master` (a GitHub PR `development → master`,
  or an equivalent local `git merge --no-ff development` on `master` pushed over SSH), then cut the
  release **from `master`**.
- **Cut a release** with `./scripts/release.sh` (dry-run by default; `--publish` to tag + release).
  It tags `v{applicationbot.__version__}` at HEAD, pushes the tag, and creates the GitHub Release.
  **Bump `applicationbot.__version__` before each `--publish`** — the script refuses to reuse an
  existing tag.
- **Release artifacts:** GitHub's auto-generated **source zip** *plus* the self-contained
  **`ApplicationBot.app.zip`** — rebuild it with `./scripts/build_macapp.sh` (bundles every runtime
  dep, incl. `anthropic`) and attach it with `gh release upload <tag> ApplicationBot.app.zip`.
- The `gh` CLI must be authed as an account with **push** access to the repo before `--publish`
  (`gh auth status` / `gh auth switch`); plain `git push` may work over SSH even when `gh` cannot.

---

## Parallel agents (Cursor ↔ Claude VS Code)

When working alongside Cursor in parallel, use the **agent bus** — git-ignored
`.agent-bus/` plus the committed CLI in `applicationbot/agent_bus.py`. Full guide:
[docs/AGENT_COLLAB.md](docs/AGENT_COLLAB.md).

**Start of every session (Claude in VS Code):**

```bash
python -m applicationbot.agent_bus context --agent claude
python -m applicationbot.agent_bus read --agent claude --unread
```

**While working:** claim paths before editing shared files; post `handoff` / `task` /
`blocker` messages to `cursor` or `broadcast`; run `watch --agent claude` in a side
terminal for canary alerts.

**End of a chunk:** ack handled messages, release claims, set status idle:

```bash
python -m applicationbot.agent_bus ack <id>
python -m applicationbot.agent_bus release --agent claude
python -m applicationbot.agent_bus status --agent claude --set-status idle
```

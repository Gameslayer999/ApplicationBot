# ApplicationBot

A personalized, end-to-end job-application pipeline. Clone the repo, set up your
profile and filters, and ApplicationBot discovers matching job openings, tailors your
resume to each one, applies with no human intervention, and tracks every application it
submits.

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
own local (git-ignored) config. Clone, configure, run.

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

### Tailoring engines (`--backend`) — no API key, uses your Claude subscription

The engine is pluggable and defaults to `auto`:

| `--backend` | Needs | Quality |
|---|---|---|
| `claude-code` | Claude Code installed + signed in — uses your **Claude subscription** (Pro/Max), **not** the paid API | Best — rewrites bullets to match the posting |
| `rules` | **Nothing** — no LLM, no account, no network | Reorders/selects by keyword; doesn't reword |
| `auto` (default) | — | Claude Code if available → else rules |

The Claude engine runs on your **Claude subscription** by shelling out to the Claude Code
CLI (`claude -p`) — it never calls the metered Claude API, and there is **no `anthropic`
dependency**. Sign-in happens inside Claude Code (`claude`, then `/login`), not in this
app. The `rules` engine needs nothing at all, so the tool works out of the box with zero
setup.

### Reviewing in the browser (web UI)

For easier review than the CLI, start the local web app:

```bash
./scripts/run.sh          # sets up the venv, starts on http://127.0.0.1:8000, opens your browser
./scripts/run.sh 9000     # ...on a different port
./scripts/restart.sh      # stop + start again (picks up code changes)
./scripts/stop.sh         # stop it
```

(Or run it directly: `python -m applicationbot.web [--port 8000]`.)

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

# Running ApplicationBot overnight — the agent contract

> For any agent (Claude Code, Hermes, a cron job) told something like
> **"send 100 applications tonight"**. Read this file, then follow it top to bottom.
> The rule that governs the whole night: **never come back to the user.**
> Every decision below has a documented default so you never need to ask.

Decision 185. Companion to [CLAUDE.md](../CLAUDE.md) (which still applies in full) and
[NEXT_STEPS.md](../NEXT_STEPS.md).

---

## 0. The one thing you may not decide alone

**Arming.** Real submissions need `armed: true`. You may pass `--arm` **only when the user's
instruction for this session was to really apply** ("send 100 applications tonight", "apply for
real"). If the instruction was to test, rehearse, or "see if it works", run `--dry-run` instead.
You may never arm to work around a failing preflight check, and you may never edit `safety.py`,
the `SafetyGate`, or the kill-switch logic — not tonight, not as a "fix" in the morning.

`--arm` writes `armed: true` and sets the cap to the goal, then **restores `profile/safety.yaml`
exactly as it was** when the night ends (including on Ctrl-C and on a crash). An armed night
cannot bleed into tomorrow.

The user can stop everything at any moment by creating `profile/KILL`. Anything that halts
submission is final for the night — do not delete that file.

---

## 1. Preflight — before anything else

```bash
python -m applicationbot.night --preflight-only --goal 100
```

Prints each check with its fix and a `PREFLIGHT {json}` line. **Exit 6 means do not start.**
Every failing check names the one command or file that fixes it — fix what you can (an unlinked
bot inbox, a missing Playwright browser) and re-run. If a check can only be fixed by the user
(Claude sign-in, no résumé), stop and leave a note; do not start a night that cannot work.

Then rehearse once, for free:

```bash
python -m applicationbot.night --dry-run --goal 1 --until 5m --max-per-cycle 1
```

This fills and records a real application and submits nothing. If it comes back `dry-run`, the
pipeline works and you may start the real night.

---

## 2. Run the night

```bash
python -m applicationbot.night --goal 100 --until 07:00 --arm
```

| flag | what it does |
|---|---|
| `--goal N` | stop once **N submissions** land. Dry-run outcomes never count toward it. |
| `--until 07:00` \| `8h` | hard deadline: a wall-clock time (next occurrence) or a duration. |
| `--arm` | arm for this night, cap = goal, restore the switch afterwards. |
| `--dangerously-unlimited` | with `--arm`: **no submission cap** — apply until the deadline, the kill switch, the breaker, or the Claude usage limit. There is no ceiling. Use only when the user asked for one. |
| `--dry-run` | fill and record, submit nothing, whatever `safety.yaml` says. |
| `--no-tailor` | send the user's **own** résumé verbatim — no Claude tailoring call per application. Overrides the saved ⚙ Loop settings résumé policy for this run only, and writes nothing back. |
| `--max-per-cycle N` | cap applications per cycle (default: the whole cleared queue). |
| `--interval N` | minutes to idle after a cycle that found nothing; doubles up to 2h. |
| `--max-consecutive-failures` / `--max-same-failure` | the circuit breaker (defaults 5 / 10). |
| `--fresh` | re-search every board each cycle instead of reusing the discovery cache (costs tokens). |

**What the night does on its own, so you don't have to watch it:**

- Blocked application (expired login, captcha, an unanswerable required question) → recorded with
  its parking reason, skipped, next application. Never waited on.
- Claude usage limit → pauses and retries the same application (up to 3 waits), then stops.
- Nothing new on the boards → backs off and searches again; an unmet goal never ends the night
  early (decision 146).
- Systemic failure (5 failures in a row, or 10 of one kind) → the night **stops itself**. That is a
  bug report, not a failure of nerve.
- Email verification (Workday-style account walls) → handled from the linked read-only bot inbox.
  Check it is linked in preflight; it is how an unattended run gets past those.

**Which résumé goes out.** Without `--no-tailor` the night follows the résumé policy the user saved in
⚙ Loop settings (`smart` / `always` / `under N` / `never`) — the same policy the in-app loop uses, so
the CLI and the app never disagree. With `--no-tailor` it sends their own résumé as it stands: no
tailoring call per application, and nothing the user has never read is submitted on their behalf.
Either way the run says which it used, on the console, in `summary.json` (`resume_policy`), and in
`report.md`. If the user asks for a cheap night, or says they tailored the résumé themselves, use
`--no-tailor` — and say so in your report, because it changes what was sent.

**Where the tokens go.** `--no-tailor` removes the per-application tailoring call, which is the
largest slice. Discovery still judges postings with Claude, and unanswered form questions are still
generated per application — and each form page spends one extra batched call re-reading the
applicant's data for whatever is still unanswered (decision 187) — so a no-tailor night is much
cheaper, not free. `--fresh` re-searches
every board each cycle and costs the most; leave it off unless the boards are stale.

**Exit codes — branch on these, don't parse the log:**

| code | meaning | what you do |
|---|---|---|
| 0 | goal reached | review, report, improve |
| 3 | deadline arrived first | review, report, improve — say how far it got |
| 4 | kill switch / interrupted | review what did land; do **not** restart |
| 5 | circuit breaker | review; the top finding is the bug — fix it |
| 6 | preflight failed, nothing ran | fix the named check, re-run preflight |
| 7 | fatal mid-run (Claude sign-in, dead browser) | fix it, then start a new night if time remains |

Everything lands in `nights/<timestamp>/`: `events.jsonl` (every cycle and application),
`summary.json` (what you branch on), `report.md` (what the user reads). Also printed as a final
`NIGHT_SUMMARY {json}` line.

---

## 3. Review your own work — before you report anything

```bash
python -m applicationbot.night review
```

Never report the night's number without running this first. The number the loop printed is how
many times it clicked submit; the review checks how many actually landed. It answers three
questions and writes `review.md` + `findings.json` into the session directory:

1. **Were the submissions real?** Each claimed submission is matched to its tracker row (status +
   `date_applied`). Anything unconfirmed, unrecorded, or contradicted by the tracker is flagged.
   **Exit code 2 means the night's own count cannot be trusted — say so plainly in your report,
   lead with it, and use the confirmed number, not the claimed one.**
2. **What failed, and is it getting worse?** Every non-submit grouped by failure kind and by ATS,
   compared against the previous nights — so a regression reads differently from a standing gap.
3. **Is the fit judge calibrated?** Whether `min_fit` should move, from real outcomes (decision 043).

`findings.json` is a ranked backlog: biggest cost first, each item naming its evidence and the file
to change.

One thing worth reading beyond the findings: each application's report carries "Re-read your résumé
and profile for N question(s) — answered M, left K for you". A question that keeps coming back as
*left for you* across many postings is a gap in the user's profile, not a bug — collect those and
tell them which answers to add once, in the report's "what needs the user" section.

---

## 4. Improve — the part that makes the next night better

Work the findings in rank order, and only these three tiers:

**Tier 1 — data, always safe to do unattended.**
Answers missing from the answer bank, `min_fit` when the calibration recommends a move, a nav
recipe for a form step that needed one, dropping a board that failed every attempt. These change
`profile/` and the stores, not the engine.

**Tier 2 — code fixes, gated by tests.** Allowed on `development` only, and only like this:

1. Write a test that **reproduces the failure first** and fails for the right reason.
2. Make the smallest change that fixes it (Karpathy §3 — no drive-by refactoring).
3. `python -m pytest` — the **whole** suite green, not just your new test.
4. Commit to `development` with the finding id in the message. Never commit to `master`.
   Never commit PII (`git status` before every commit — Guideline #12).

If the suite does not go green, revert your change and demote the finding to Tier 3. A red suite is
never handed to the user.

**Tier 3 — propose, don't do.** Anything touching submission safety, module boundaries, the
scraping strategy, or the storage schema (Guideline #1); anything needing a live account or a
human decision; anything you could not reproduce with a test. Write it into `NEXT_STEPS.md` under
**Now** with the evidence and your recommendation, and leave it.

Then finish the paperwork, every time (Guidelines #9, #10, #13):
- `DECISIONS.md` — one entry for anything you decided, with options and reasoning.
- `NEXT_STEPS.md` — move finished work to Recently completed, add what you found.
- `README.md` — only if user-facing behaviour changed.

---

## 5. Report to the user — one message, in this order

1. **The real number**, from the review, not the loop: "37 submitted, 35 confirmed by the tracker,
   2 unconfirmed."
2. **Why it stopped**, in plain words: goal reached / deadline / breaker (and the bug).
3. **The top three findings** and what you did about each: fixed (with the commit), tuned, or
   proposed and why.
4. **What needs the user** — and nothing else. Blocked applications waiting on a real human
   decision, a login only they can do, an armed-run choice.
5. Links: `nights/<timestamp>/report.md` and `review.md`.

Be exact (Guideline #11). If something failed, name the failure and the count. Do not round a
partial night up into a good one.

---

## What you must never do overnight

- Ask the user anything, or stop to wait for one. Record it and move on.
- Arm a run the user did not ask to be armed, or delete `profile/KILL`.
- Edit `safety.py`, the gate, or the arming checks.
- Build anything that evades bot detection, captchas, or a WAF (Guideline #4). A blocked source
  gets dropped, not defeated.
- Commit PII, `applications.db`, `nights/`, or anything under `profile/` (Guideline #12).
- Report a clean night on the strength of a count the tracker disagrees with.

---

## Cron: the same night, every night

```bash
0 23 * * *  cd /path/to/ApplicationBot && ./scripts/night.sh --goal 100 --until 07:00 --arm >> nights/cron.log 2>&1
```

`scripts/night.sh` resolves the venv, runs preflight, runs the night, then runs the review, and
exits with the night's own exit code — so a scheduler sees the same codes as the table above. It
is idempotent and safe to re-run in any state (Guideline #8).

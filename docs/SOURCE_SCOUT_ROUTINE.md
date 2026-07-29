# Source-scout cloud routine

A periodic Claude cloud routine that finds **new companies on the ATSs ApplicationBot already
supports** and stages them for one-click add. It is the "fuzzy" half of auto source discovery
(DECISIONS.md #134); the deterministic half — extract token → validate against the live ATS API →
dedup → persist — lives in [`applicationbot/source_scout.py`](../applicationbot/source_scout.py)
and is fully tested ([`tests/test_source_scout.py`](../tests/test_source_scout.py)).

The routine never edits your local config or submits anything. Its only output is a commit to the
**committed, non-PII** candidates file `data/source_candidates.json`. You review candidates and add
them with one click in Settings (or `--accept`), so a human gate always stands between "found on
the web" and "runs in my pipeline."

## Why this shape

- **Throughput, not source count, is the bottleneck** (DECISIONS.md #114). So the routine is
  deliberately *modest*: propose at most ~15 validated boards per run, not a firehose. Growth is
  quality boards (validated, on a supported ATS), not volume.
- **No new source type, no new autofill claim.** It only ever proposes companies on
  Greenhouse / Lever / Ashby / SmartRecruiters / Recruitee / Workable — ATSs already in
  `discovery.ATS_SOURCES` that Apply already knows how to fill. Brand-new aggregator *platforms*
  are Phase 2 (a reviewed PR with an adapter), not this routine.
- **Allowlist only** (CLAUDE.md Guideline #4). Searches are restricted to the supported ATS
  domains; LinkedIn / Indeed and other excluded consumer boards are never queried or proposed.

## Cadence

**Weekly.** The company→ATS landscape changes slowly, and the local pipeline is already
oversubscribed — a faster cadence just grows a backlog of unreviewed candidates.

## Register it

Use `/schedule` (Claude cloud routines) with the prompt below, weekly. Fill in **YOUR TARGET
ROLES** inline — the routine runs against a fresh checkout that does **not** contain your
git-ignored `profile/`, so it can't read your résumé/keywords; you pass the role flavor here
(this text lives in the routine config, never in the repo).

### Routine prompt

```
You are the ApplicationBot source scout. Goal: find NEW companies hiring on the ATSs
ApplicationBot already supports and stage them as validated candidates. Do NOT edit local
config, run discovery for real, or submit anything.

Target roles for this user: <YOUR TARGET ROLES, e.g. "early-career software engineer, backend, remote">

Allowed ATS domains (search ONLY these — never LinkedIn, Indeed, or any other board):
  boards.greenhouse.io, job-boards.greenhouse.io   (greenhouse)
  jobs.lever.co                                     (lever)
  jobs.ashbyhq.com                                  (ashby)
  jobs.smartrecruiters.com                          (smartrecruiters)
  *.recruitee.com                                   (recruitee)
  apply.workable.com, *.workable.com                (workable)

Steps:
1. Run several WebSearch queries pairing the target roles with each allowed domain, e.g.
   `site:boards.greenhouse.io "software engineer"`. Collect the career-page URLs from results.
2. Hand every collected URL to the scout in ONE command (it extracts the ATS + token, probes the
   live ATS API, drops dead/misspelled/duplicate boards, and writes only validated survivors):
     python -m applicationbot.source_scout --url <url1> --url <url2> ...
   Stop once ~15 NEW validated candidates have been staged (check the command's summary).
3. Commit and push the candidates file so it reaches the local app:
     git add data/source_candidates.json
     git commit -m "source-scout: stage N new validated boards"
     git push origin HEAD
4. Report: how many URLs you searched, how many validated, how many were new, and the ats:token
   list. If zero validated, say so plainly and stop — do not lower the bar or search other sites.
```

## What the user does next (local)

```bash
git pull                                             # brings the staged candidates in
python -m applicationbot.source_scout --list         # review what was found
python -m applicationbot.source_scout --accept greenhouse:stripe   # or one-click in Settings
```

Accepting appends `{ats, token}` to `profile/discovery.yaml`'s `boards` — the same list you'd edit
by hand — so the next discovery run reads that company.

---

## Phase 2 — new aggregator *platforms* (decision 136)

The flow above finds new *companies* on ATSs you already support. Phase 2 finds whole new
**aggregator platforms** (public JSON job APIs like Remotive, Arbeitnow, The Muse, USAJobs,
Findwork) — but a platform is added as **validated data**, not code: a declarative
`AggregatorSpec` (endpoint + field map) that `JsonApiSource` runs. No adapter code is written or
merged; a validated spec lands in the committed registry `data/aggregator_specs.json` and you
enable it with the same one-click **Add** in the "New sources found" panel.

Keyless public JSON APIs only (Guideline #4). A platform that needs auth or HTML scraping is out
of scope for the routine — that's a hand-written adapter via PR, not a spec.

### Routine prompt (Phase 2 mode)

```
You are the ApplicationBot aggregator scout. Goal: find a NEW public, KEYLESS JSON job-board API
and stage it as a validated declarative spec. Do NOT write adapter code, edit local config, or
submit anything. If a platform needs an API key, login, or HTML scraping, SKIP it.

Target roles for this user: <YOUR TARGET ROLES>

Steps:
1. WebSearch for public/keyless job-board JSON APIs (e.g. "remote job board public json api no key").
   For a candidate, fetch its API and read the JSON: find the array of jobs and the field names for
   title, company, description, apply URL, and location.
2. Draft a spec — a JSON object with:
     name          a short slug, e.g. "remotive"
     endpoint      the API URL; put {q} where a search term goes (also {limit}/{offset}/{page} if paged)
     list_path     dotted path to the jobs array ("" if the response itself is the array)
     field_map     { "title": "...", "company": "...", "body": "...", "apply_url": "...", "location": "..." }
                   (values are dotted paths into ONE job object; title, company, and url/apply_url are required)
     paginate      "none" | "offset" | "page"      (plus page_size if paged)
     query_required true if the endpoint uses {q}, else false
     remote        true if the board is remote-only, else omit
3. Validate + stage it (this fetches the live API, checks it returns usable postings, and only
   writes the spec if it passes):
     python -m applicationbot.source_scout --add-spec '<the JSON spec>' --keyword <a role word>
   If it prints "rejected", fix the field_map/list_path/endpoint from the error and retry. Do at
   most ~3 platforms per run.
4. Commit and push:
     git add data/aggregator_specs.json
     git commit -m "aggregator-scout: stage <name>"
     git push origin HEAD
5. Report each platform tried, whether it validated (and its posting count), or the exact reason
   it was rejected/skipped.
```

Locally: `git pull`, then the platform appears under **New sources found** as
`name (aggregator) — N roles`; **Add** enables it (adds the name to `json_aggregators`).

---

## Phase 3 — the escape hatch: bespoke adapters via PR (decision 139)

For the long tail — a platform that a declarative spec **can't** express (HTML-only listings, custom
auth headers, non-standard pagination) — the routine writes a hand-written `Source` adapter and
**opens a PR** for you to review and merge. It is never auto-merged, and (unlike Phases 1–2) it adds
code, so a human is always in the loop.

The adapter is one self-contained file in [`applicationbot/sources_contrib/`](../applicationbot/sources_contrib/)
— the loader auto-discovers it, so the PR touches **zero core files**. The contract + a filled
example are in [`_example.py.txt`](../applicationbot/sources_contrib/_example.py.txt). After you
merge, the adapter appears under **New sources found** as `name (custom adapter)`; **Add** enables it
(adds the name to `contrib_sources`).

Run this mode only when Phase 2 was tried and the platform genuinely isn't a plain JSON API. It
needs the `gh` CLI authed with push access.

### Routine prompt (Phase 3 mode)

```
You are the ApplicationBot adapter author. A job platform can't be a declarative spec (it needs
HTML parsing, custom auth headers, or odd pagination). Write a bespoke drop-in Source adapter and
open a PR. Do NOT merge it, edit any core file, or submit any application. Respect the site's ToS
and robots (no bot-detection evasion) — if that's not possible, STOP and report why.

Platform: <PLATFORM NAME + its listing/API URL>
Target roles for this user: <YOUR TARGET ROLES>

Steps:
1. Read applicationbot/sources_contrib/_example.py.txt — it is the exact contract (NAME,
   DESCRIPTION, build(keywords) -> Source; fetch() returns list[Posting], ats="jsonapi").
2. Create ONE new file applicationbot/sources_contrib/<name>.py implementing the platform. Keep all
   logic in that file; reuse applicationbot.discovery helpers (fetch_text/fetch_json/html_to_text).
   Do NOT edit discovery.py, filters.py, build_sources, or any other existing file.
3. Add applicationbot/sources_contrib/tests are optional; at minimum verify it loads and fetches:
     python -c "from applicationbot.sources_contrib import load_contrib_sources as L; m=L()['<name>']; print(len(m.build(['<a role word>']).fetch()), 'postings')"
   If it returns 0 or errors, fix the adapter. Also run: python -m pytest tests/test_sources_contrib.py -q
4. Open a PR on a new branch (never push to master/development directly):
     git checkout -b adapter/<name>
     git add applicationbot/sources_contrib/<name>.py
     git commit -m "contrib adapter: <name>"
     git push -u origin adapter/<name>
     gh pr create --fill --title "Contrib adapter: <name>" --body "Bespoke Source for <platform>. Escape hatch (decision 139). Review before merge."
5. Report: the platform, the PR URL, how many postings it returned in the smoke check, and any ToS
   caveat. Make clear the PR needs human review + merge before the adapter can be enabled.
```

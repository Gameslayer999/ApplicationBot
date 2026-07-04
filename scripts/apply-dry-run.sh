#!/usr/bin/env bash
#
# End-to-end DRY-RUN test of the Apply stage. Sets up ALL dependencies, tailors a résumé to
# a fixture job, exports a PDF, and opens a VISIBLE browser to watch it fill an application
# form in real time. It NEVER submits (Guideline #3).
#
# Idempotent — safe to re-run. Usage:
#   ./scripts/apply-dry-run.sh                 # preselected app (first in fixtures/applications.txt)
#   ./scripts/apply-dry-run.sh random          # pick a random app on file — test form flexibility
#   ./scripts/apply-dry-run.sh <application-url>   # a specific posting
#   ./scripts/apply-dry-run.sh <random|url> resume.yaml   # + a specific résumé YAML
#
# The pool of applications lives in fixtures/applications.txt (one URL per line).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APPS_FILE="fixtures/applications.txt"
SEL="${1:-preselected}"
RESUME="${2:-examples/sample_resume.yaml}"

apps() { grep -vE '^[[:space:]]*(#|$)' "$APPS_FILE"; }
case "$SEL" in
  preselected|"") URL="$(apps | head -n1)"; echo "Preselected application: $URL" ;;
  random)         N="$(apps | wc -l | tr -d ' ')"
                  URL="$(apps | sed -n "$(( RANDOM % N + 1 ))p")"
                  echo "Randomly selected application ($N on file): $URL" ;;
  *)              URL="$SEL"; echo "Specified application: $URL" ;;
esac
JD="fixtures/job_descriptions/backend-mid-censys.md"
PDF="/tmp/applicationbot_dry_run.pdf"
PY="$ROOT/.venv/bin/python"

echo "→ [1/4] Python virtualenv + dependencies…"
[ -x "$PY" ] || python3 -m venv .venv
"$ROOT/.venv/bin/pip" install -q -r requirements.txt

echo "→ [2/4] Chromium browser for Playwright (one-time ~150 MB download; skipped if present)…"
"$PY" -m playwright install chromium

echo "→ [3/4] Tailoring a résumé to the job and exporting a PDF (rules engine — no account needed)…"
"$PY" -m applicationbot.cli "$JD" --resume "$RESUME" --backend rules --out "$PDF"

echo "→ [4/4] Opening a browser to fill the application form — DRY RUN, will NOT submit."
echo "        URL: $URL"
echo "        Watch it fill, review the form, then press Enter in this terminal to close."
"$PY" -m applicationbot.apply "$URL" --pdf "$PDF" --resume "$RESUME" --debug

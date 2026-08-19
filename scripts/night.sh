#!/usr/bin/env bash
#
# Run one unattended night, then review it — the single command an agent or a cron job runs
# (decision 185; the agent contract is docs/AGENT_NIGHT_RUN.md).
#
#   ./scripts/night.sh --goal 100 --until 07:00 --arm     # apply for real until 100 land or 7am
#   ./scripts/night.sh --dry-run --goal 5 --until 30m     # rehearsal: fills and records, submits nothing
#   ./scripts/night.sh --preflight-only --goal 100        # just the readiness checks
#
# Every flag is passed straight through to `python -m applicationbot.night` — see its --help.
#
# Three phases, in order: preflight (stops here if anything is not ready), the night itself, then
# the self-review. The script exits with the NIGHT's exit code, not the review's, so a scheduler
# sees the codes documented in docs/AGENT_NIGHT_RUN.md (0 goal · 3 deadline · 4 kill · 5 breaker ·
# 6 preflight · 7 fatal). The review's own verdict is on stdout and in the session directory.
#
# Idempotent (Agent Guideline #8): it creates the virtualenv and installs dependencies if they are
# missing, and is safe to re-run in any state. It never arms anything on its own — arming happens
# only if you pass --arm, and `python -m applicationbot.night` puts safety.yaml back afterwards.
#
set -uo pipefail   # NOT -e: a non-zero night is an outcome to report, not a crash

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=/dev/null
. "$ROOT/scripts/_venv.sh"
VENV="$(venv_dir "$ROOT")"
PY="$VENV/bin/python"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required and was not found. Install it from https://www.python.org/downloads/ and re-run."
  exit 6
fi
if [ ! -x "$PY" ]; then
  echo "→ Creating the virtualenv at $VENV…"
  python3 -m venv "$VENV" || exit 6
fi
"$VENV/bin/pip" install -q -r requirements.txt || { echo "Dependency install failed — fix the error above and re-run."; exit 6; }
"$PY" -m playwright install chromium >/dev/null 2>&1 || true   # already-installed is the common case

echo "→ [1/3] Preflight…"
"$PY" -m applicationbot.night --preflight-only "$@"
PRE=$?
if [ "$PRE" -ne 0 ]; then
  echo "Preflight failed (exit $PRE) — nothing was run. Each check above names its fix."
  exit "$PRE"
fi

# --preflight-only in the caller's own flags means they wanted the checks and nothing else.
case " $* " in *" --preflight-only "*) exit 0 ;; esac

echo "→ [2/3] Running the night…"
"$PY" -m applicationbot.night "$@"
NIGHT=$?

echo "→ [3/3] Reviewing the night's work…"
"$PY" -m applicationbot.night review
REVIEW=$?
if [ "$REVIEW" -eq 2 ]; then
  echo "⚠ The review could NOT confirm every submission the night claimed — read review.md above"
  echo "  and report the CONFIRMED number, not the claimed one."
fi

exit "$NIGHT"

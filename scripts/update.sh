#!/usr/bin/env bash
#
# Update ApplicationBot to the latest version from GitHub, then apply it to the running app.
# One command: pulls new commits, reinstalls dependencies if needed, and restarts.
#
# Idempotent and safe (Agent Guideline #8): it never overwrites uncommitted local changes — if
# you have any, it stops and tells you how to set them aside first.
#
#   ./scripts/update.sh          # update, then restart on http://127.0.0.1:8000
#   ./scripts/update.sh 9000     # ...restart on a specific port
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="${1:-8000}"
# shellcheck source=/dev/null
. "$ROOT/scripts/_venv.sh"
VENV="$(venv_dir "$ROOT")"
PY="$VENV/bin/python"

command -v git >/dev/null 2>&1 || { echo "✗ git is not installed."; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "✗ This folder isn't a git clone, so it can't self-update from GitHub."
  echo "  Get updates by cloning the repo:  git clone <repo-url>   (then run from there)."
  exit 1
}

# 1. Never clobber local edits (Guideline #7 — preserve the user's work).
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠ You have uncommitted local changes — updating could overwrite them:"
  git status --short | sed 's/^/    /'
  echo ""
  echo "  Set them aside, update, then bring them back:"
  echo "     git stash"
  echo "     ./scripts/update.sh"
  echo "     git stash pop"
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "→ Fetching latest for '${BRANCH}'…"
git fetch --quiet origin

UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [ -z "$UPSTREAM" ]; then
  echo "✗ '${BRANCH}' has no upstream to pull from."
  echo "  Set one:  git branch --set-upstream-to=origin/${BRANCH}"
  exit 1
fi

LOCAL="$(git rev-parse @)"
REMOTE="$(git rev-parse '@{u}')"
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "✓ Already up to date (${BRANCH} @ $(git rev-parse --short HEAD))."
else
  BEHIND="$(git rev-list --count 'HEAD..@{u}')"
  echo "→ ${BEHIND} new commit(s) on ${UPSTREAM}. Updating…"
  if ! git merge --ff-only '@{u}'; then
    echo "✗ Can't fast-forward — '${BRANCH}' has diverged from ${UPSTREAM}."
    echo "  Reconcile manually (git status / git log), then re-run."
    exit 1
  fi
  # Dependencies may have changed with the new code — reinstall (idempotent) so the restart
  # picks them up. requirements-only updates still land here.
  echo "→ Reinstalling dependencies…"
  "$VENV/bin/pip" install -q --disable-pip-version-check -r requirements.txt
  "$PY" -m playwright install chromium
  echo "✓ Updated to $(git rev-parse --short HEAD)."
fi

# 2. Apply it to whatever's running.
if pgrep -f "dev_reload.py" >/dev/null 2>&1; then
  echo "→ Dev auto-reload is running — it restarts the app for you (the browser refreshes itself)."
elif pgrep -f "applicationbot.web" >/dev/null 2>&1; then
  echo "→ Restarting the running app…"
  exec "$ROOT/scripts/restart.sh" "$PORT"
else
  echo "→ Not currently running. Start it with:  ./scripts/run.sh"
fi

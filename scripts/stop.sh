#!/usr/bin/env bash
#
# Stop the ApplicationBot local review UI.
#
# Idempotent (Agent Guideline #8): does nothing (and exits 0) if nothing is running.
#
# Usage:
#   ./scripts/stop.sh           # stop any running review UI (by process signature)
#   ./scripts/stop.sh 9000      # stop only the instance on a specific port
#
set -euo pipefail

PORT="${1:-}"

if [ -n "$PORT" ]; then
  PIDS="$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)"
  what="review UI on port ${PORT}"
else
  PIDS="$(pgrep -f 'applicationbot.web' || true)"
  what="review UI"
fi

if [ -z "$PIDS" ]; then
  echo "No ${what} running."
  exit 0
fi

echo "→ Stopping ${what} (pid: ${PIDS})…"
# shellcheck disable=SC2086
kill $PIDS 2>/dev/null || true
sleep 0.5
# Force-kill anything that ignored SIGTERM.
for pid in $PIDS; do
  kill -9 "$pid" 2>/dev/null || true
done
echo "→ Stopped."

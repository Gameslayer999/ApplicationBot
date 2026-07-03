#!/usr/bin/env bash
#
# Restart the ApplicationBot local review UI: stop any running instance, then start it
# again (picking up code changes). Delegates to stop.sh + run.sh, so it stays idempotent.
#
# Usage:
#   ./scripts/restart.sh          # restart on http://127.0.0.1:8000
#   ./scripts/restart.sh 9000     # restart on a specific port
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8000}"

"$ROOT/scripts/stop.sh"          # stop any running instance (frees the port)
sleep 0.5
exec "$ROOT/scripts/run.sh" "$PORT"

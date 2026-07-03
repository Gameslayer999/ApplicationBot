#!/usr/bin/env bash
#
# Start the ApplicationBot local review UI.
#
# Idempotent (Agent Guideline #8): creates the virtualenv and installs dependencies on
# first run, reuses them afterward. Safe to re-run in any state.
#
# Usage:
#   ./scripts/run.sh            # http://127.0.0.1:8000
#   ./scripts/run.sh 9000       # choose a port
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${1:-8000}"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "→ Creating virtualenv (.venv)…"
  python3 -m venv "$VENV"
fi

echo "→ Installing dependencies…"
"$VENV/bin/pip" install -q -r requirements.txt

# The Claude engine uses your Claude subscription via the Claude Code CLI (`claude`), not
# the paid API. Just report whether it's available (UI Design Principle #1) — installing
# Claude Code is a deliberate user step, not something to auto-run.
check_claude() {
  if command -v claude >/dev/null 2>&1; then
    echo "→ Claude Code (claude) present — the 'claude-code' engine uses your subscription."
  else
    echo "→ Claude Code (claude) not found. The app will use the free 'rules' engine."
    echo "  To get Claude-quality tailoring on your subscription (not the paid API),"
    echo "  install Claude Code and sign in: https://claude.com/product/claude-code"
  fi
}
check_claude

URL="http://127.0.0.1:${PORT}"
echo "→ Starting review UI at ${URL}  (Ctrl-C to stop)"

# Open the browser shortly after the server comes up (macOS `open` / Linux `xdg-open`).
(
  sleep 1
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
) >/dev/null 2>&1 &

# Run the server in the foreground so this terminal owns it and Ctrl-C stops it.
exec "$PY" -m applicationbot.web --port "$PORT"

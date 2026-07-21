#!/usr/bin/env bash
#
# Start ApplicationBot — the one command that sets everything up and launches the app.
#
# Idempotent (Agent Guideline #8): on first run it creates the virtualenv, installs
# dependencies, and downloads the automation browser (Chromium); afterward it reuses them.
# Safe to re-run in any state. macOS/Linux; Windows users run scripts\run.bat.
#
# Usage:
#   ./scripts/run.sh            # browser tab on http://127.0.0.1:8000
#   ./scripts/run.sh 9000       # choose a port
#   ./scripts/run.sh --window   # standalone desktop window (native, not a browser tab)
#   ./scripts/run.sh --dev      # auto-reload on code changes (dev mode)
#   ./scripts/run.sh --window --dev   # desktop window that reloads on code changes
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Args in any order: a bare number sets the port; --dev enables auto-reload; --window opens the
# standalone native window instead of a browser tab.
PORT="8000"
DEV=0
WINDOW=0
for arg in "$@"; do
  case "$arg" in
    --dev) DEV=1 ;;
    --window) WINDOW=1 ;;
    ''|*[!0-9]*) echo "Ignoring unknown argument: $arg" ;;
    *) PORT="$arg" ;;
  esac
done
# Resolve the venv location (off ~/Documents on macOS so the double-click app can read it).
# shellcheck source=/dev/null
. "$ROOT/scripts/_venv.sh"
VENV="$(venv_dir "$ROOT")"
PY="$VENV/bin/python"

# 0. Python 3 is the one prerequisite we can't install for the user — fail with the fix.
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ Python 3 is required but was not found on your PATH."
  echo "  Install it, then re-run this launcher:"
  echo "    macOS:  brew install python   (or https://www.python.org/downloads/macos/)"
  echo "    Linux:  sudo apt install python3 python3-venv   (or your distro's package)"
  exit 1
fi

# 0b. Forward-compatibility: build/run natively on Apple Silicon, never under Rosetta.
# shellcheck source=/dev/null
. "$ROOT/scripts/_native.sh"
require_native python3 || exit 1

# 1. Virtualenv (create once, reuse after).
if [ ! -x "$PY" ]; then
  echo "→ Creating virtualenv ($VENV)…"
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv "$VENV"
fi

# 1b. A pre-existing venv built with an Intel/x86_64 Python relies on Rosetta — refuse and rebuild.
require_native "$PY" || { echo "  Rebuild native:   rm -rf .venv && ./scripts/run.sh"; exit 1; }

# 2. Python dependencies.
echo "→ Installing dependencies…"
"$VENV/bin/pip" install -q --disable-pip-version-check -r requirements.txt

# 3. Automation browser. Playwright's Apply stage drives a real Chromium; `install` is
#    idempotent (it no-ops when the browser is already present). This is the step a manual
#    clone most often forgets — so the launcher does it, not the user.
echo "→ Ensuring the automation browser (Chromium) is installed…"
"$PY" -m playwright install chromium

VERSION="$("$PY" -c 'import applicationbot; print(applicationbot.__version__)' 2>/dev/null || echo '0.1.0')"

# 4. Claude Code is optional (the free `rules` engine works without it) — just report state.
if command -v claude >/dev/null 2>&1; then
  echo "→ Claude Code (claude) present — the 'claude-code' engine uses your subscription."
else
  echo "→ Claude Code (claude) not found. The app will use the free 'rules' engine."
  echo "  For Claude-quality tailoring on your subscription (not the paid API), install"
  echo "  Claude Code and sign in: https://claude.com/product/claude-code"
fi

# 5. Readiness report (non-fatal). A fresh clone has no profile yet — that's expected; the
#    app's in-window "Finish setup" walkthrough guides the rest. Never abort startup on it.
echo "→ Checking readiness…"
"$PY" -m applicationbot.doctor || \
  echo "  (Setup is incomplete — the app's ✨ Finish setup guide will walk you through it.)"

echo ""

# --window: standalone native window (pywebview). The app owns its own HTTP server, so we don't
# open a browser or serve here — just hand off to applicationbot.app. --dev makes the window
# auto-reload on code changes (supervisor restarts the server; the window refreshes itself).
if [ "$WINDOW" = 1 ]; then
  if [ "$DEV" = 1 ]; then
    echo "→ ApplicationBot v${VERSION} (desktop window · dev auto-reload) — opening…  (close the window to quit)"
    exec "$PY" -m applicationbot.app --dev --port "$PORT"
  else
    echo "→ ApplicationBot v${VERSION} (desktop window) — opening…  (close the window to quit)"
    exec "$PY" -m applicationbot.app
  fi
fi

URL="http://127.0.0.1:${PORT}"
if [ "$DEV" = 1 ]; then
  echo "→ ApplicationBot v${VERSION} (dev auto-reload) — starting at ${URL}  (Ctrl-C to stop)"
else
  echo "→ ApplicationBot v${VERSION} — starting at ${URL}  (Ctrl-C to stop)"
fi

# Open the browser shortly after the server comes up (macOS `open` / Linux `xdg-open`).
(
  sleep 1
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
) >/dev/null 2>&1 &

# Foreground so this terminal owns the process and Ctrl-C stops it. In --dev, the supervisor
# watches applicationbot/ and restarts the server on every edit (browser refreshes itself).
if [ "$DEV" = 1 ]; then
  exec "$PY" "$ROOT/scripts/dev_reload.py" --port "$PORT"
else
  exec "$PY" -m applicationbot.web --port "$PORT"
fi

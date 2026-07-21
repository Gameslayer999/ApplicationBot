#!/usr/bin/env bash
# macOS double-click launcher (opens a Terminal window too). For a cleaner standalone app with
# no Terminal, build the bundle once — `./scripts/build_macapp.sh` — and double-click
# ApplicationBot.app instead. This launcher opens the same standalone window (--window).
# First launch after downloading: if macOS blocks it, right-click → Open, then Open once to trust it.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
exec "$DIR/scripts/run.sh" --window

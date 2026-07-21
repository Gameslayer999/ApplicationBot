#!/usr/bin/env bash
# Resolve the virtualenv directory, sourced by the launchers.
#
# On macOS the venv lives OUTSIDE the repo — under ~/Library/Application Support (which is NOT a
# privacy-protected folder) — so a Finder/double-click launch of ApplicationBot.app isn't blocked
# by macOS file-access protection when the clone sits under ~/Documents, ~/Desktop, or ~/Downloads.
# (Python reads the venv's pyvenv.cfg during early startup, before the app can prompt for access;
# keeping the venv non-protected avoids that.) On Linux/Windows there's no such restriction, so the
# venv stays in the repo as the familiar .venv.
#
#   venv_dir <repo-root>   # prints the venv path
venv_dir() {
  local root="$1"
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "$HOME/Library/Application Support/ApplicationBot/venv"
  else
    echo "$root/.venv"
  fi
}

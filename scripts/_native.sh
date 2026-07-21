#!/usr/bin/env bash
# Sourced by the launchers. Forward-compatibility guard: on Apple Silicon, ApplicationBot must
# run on a NATIVE arm64 Python, never an x86_64 one under Rosetta — Apple is winding Rosetta down
# (https://support.apple.com/en-us/102527), so a translated interpreter would stop working on
# future macOS. No-op on Intel Macs, Linux, and Windows.
#
#   require_native [python-executable]   # default: python3 on PATH. Returns non-zero + prints the fix.
require_native() {
  [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || return 0

  if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ]; then
    echo "✗ This shell is running under Rosetta (x86_64 translation)."
    echo "  ApplicationBot must run natively on Apple Silicon to keep working on future macOS."
    echo "  Start a native shell and retry:   arch -arm64 zsh"
    echo "  (or uncheck 'Open using Rosetta' on Terminal in Finder → Get Info)."
    return 1
  fi

  local py="${1:-python3}"
  local arch
  arch="$("$py" -c 'import platform; print(platform.machine())' 2>/dev/null || echo unknown)"
  if [ "$arch" = "x86_64" ]; then
    echo "✗ '$py' is Intel-only (x86_64), so it would run under Rosetta — which Apple is phasing out."
    echo "  Install a native arm64 Python, then retry:"
    echo "    brew install python                       # Apple-Silicon Homebrew installs arm64"
    echo "    or the universal2 build: https://www.python.org/downloads/macos/"
    return 1
  fi
  return 0
}

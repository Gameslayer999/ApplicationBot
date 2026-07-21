#!/usr/bin/env bash
#
# Build ApplicationBot.app — a fully self-contained macOS application. It bundles its own Python
# runtime, all dependencies, and the app code (via PyInstaller), so it installs like any other Mac
# app: double-click, or drag to /Applications. No system Python, no venv, no setup step, and no
# file-access prompts (it reads nothing from ~/Documents).
#
#   • User data (profile, résumé, filters, applications.db) lives in
#     ~/Library/Application Support/ApplicationBot — independent of any source checkout.
#   • The Apply-stage browser (Chromium) is downloaded on first use, keeping the app lean.
#   • This bundle is a *production snapshot*: it does NOT reflect live repo edits. For development
#     with auto-reload, use ./scripts/dev.sh or ./scripts/run.sh --window (they run the live repo).
#
# Idempotent (Agent Guideline #8): re-run any time; it rebuilds from scratch.
#
#   ./scripts/build_macapp.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ "$(uname)" != "Darwin" ]; then
  echo "✗ This builds a macOS .app and only runs on macOS. On Linux/Windows use ./scripts/run.sh --window."
  exit 1
fi

# 0. Forward-compatibility: build on a native arm64 Python so the app is native (Apple is winding
#    Rosetta down). Refuses a translated/Intel interpreter.
# shellcheck source=/dev/null
. "$ROOT/scripts/_native.sh"
require_native python3 || exit 1

# 1. Isolated build environment: runtime deps + PyInstaller (a build-only tool, not a runtime dep).
BVENV="$ROOT/.build-venv"
BPY="$BVENV/bin/python"
echo "→ Preparing the build environment (.build-venv)…"
[ -x "$BPY" ] || python3 -m venv "$BVENV"
require_native "$BPY" || { echo "  Rebuild native: rm -rf .build-venv && re-run."; exit 1; }
"$BVENV/bin/pip" install -q --disable-pip-version-check -r requirements.txt pyinstaller
VERSION="$("$BPY" -c 'import applicationbot; print(applicationbot.__version__)' 2>/dev/null || echo '0.1.0')"

# 2a. App icon: (re)generate the .icns from the master art.
ICON_ARG=()
if [ -f "$ROOT/assets/icon-master-1024.png" ]; then
  "$ROOT/scripts/make_icon.sh" >/dev/null && ICON_ARG=(--icon "$ROOT/assets/ApplicationBot.icns")
fi

# 2b. Freeze the app. --collect-all pulls each package's submodules + data files (playwright's node
#     driver, pywebview/pyobjc, and our own fixtures/recipe JSON); the browser binary is excluded
#     (installed on first Apply-use). --windowed makes it a GUI .app (no console).
echo "→ Building ApplicationBot.app v${VERSION} with PyInstaller (this takes a minute)…"
rm -rf "$ROOT/ApplicationBot.app" "$ROOT/dist" "$ROOT/build-pyi"
"$BVENV/bin/pyinstaller" --noconfirm --clean --windowed \
  --name ApplicationBot \
  --osx-bundle-identifier com.applicationbot.macos \
  "${ICON_ARG[@]}" \
  --paths "$ROOT" \
  --collect-all applicationbot \
  --collect-all anthropic \
  --collect-all playwright \
  --collect-all webview \
  --collect-all keyring \
  --collect-all google_auth_oauthlib \
  --add-data "$ROOT/fixtures:fixtures" \
  --add-data "$ROOT/examples:examples" \
  --add-data "$ROOT/applicationbot/nav_recipes.json:applicationbot" \
  --add-data "$ROOT/applicationbot/workday_recipes.json:applicationbot" \
  --distpath "$ROOT/dist" --workpath "$ROOT/build-pyi" --specpath "$ROOT/build-pyi" \
  "$ROOT/desktop_main.py"

APP="$ROOT/dist/ApplicationBot.app"
[ -d "$APP" ] || { echo "✗ PyInstaller did not produce $APP — see output above."; exit 1; }

# 3. Stamp the version and mark high-DPI (PyInstaller writes a minimal Info.plist), and add the
#    classic PkgInfo file that standard Mac apps carry (PyInstaller omits it).
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist" 2>/dev/null || true
plutil -replace CFBundleVersion -string "$VERSION" "$APP/Contents/Info.plist" 2>/dev/null || true
plutil -insert NSHighResolutionCapable -bool true "$APP/Contents/Info.plist" 2>/dev/null || true
printf 'APPL????' > "$APP/Contents/PkgInfo"

# 4. Ad-hoc code-sign (arm64 requires signed binaries; ad-hoc needs no developer account and lets
#    it launch locally). A copy DOWNLOADED to another Mac still needs a one-time right-click → Open
#    (only Apple notarization removes that, which needs a paid Developer account).
echo "→ Ad-hoc code-signing…"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 \
  && echo "   signed." || echo "   ⚠ codesign failed — the app may need manual approval on first launch."

# 5. Put the finished bundle at the repo root and clean up build intermediates.
mv "$APP" "$ROOT/ApplicationBot.app"
rm -rf "$ROOT/dist" "$ROOT/build-pyi"

echo ""
echo "✓ Built ApplicationBot.app (v${VERSION}) — self-contained, no Python needed."
echo "  Install it like any Mac app: drag ApplicationBot.app to your Applications folder, then"
echo "  double-click. (First launch: if macOS warns it's from an unidentified developer,"
echo "  right-click → Open, then Open once.)"

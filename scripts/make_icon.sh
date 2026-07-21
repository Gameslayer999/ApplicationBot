#!/usr/bin/env bash
#
# Generate the macOS app icon (assets/ApplicationBot.icns) from assets/icon-master-1024.png,
# a 1024×1024 transparent-corner PNG of the icon art. Uses only built-in macOS tools (sips,
# iconutil); no dependencies. Idempotent. Called by scripts/build_macapp.sh.
#
#   ./scripts/make_icon.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MASTER="$ROOT/assets/icon-master-1024.png"
ICONSET="$ROOT/assets/ApplicationBot.iconset"
ICNS="$ROOT/assets/ApplicationBot.icns"

[ -f "$MASTER" ] || { echo "✗ missing $MASTER"; exit 1; }
command -v iconutil >/dev/null 2>&1 || { echo "✗ iconutil not found (macOS only)."; exit 1; }

rm -rf "$ICONSET"; mkdir -p "$ICONSET"
# Apple's required iconset members: <px> <iconset-name>
for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" \
            "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" \
            "512 512x512" "1024 512x512@2x"; do
  # shellcheck disable=SC2086
  set -- $spec
  sips -z "$1" "$1" "$MASTER" --out "$ICONSET/icon_${2}.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$ICNS"
rm -rf "$ICONSET"
echo "✓ wrote $ICNS"

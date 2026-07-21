#!/usr/bin/env bash
#
# Start ApplicationBot in DEV mode: the server auto-restarts whenever you edit a file in
# applicationbot/, and the browser refreshes itself — so local changes show up immediately.
# Thin alias for `run.sh --dev`. Optional port argument.
#
#   ./scripts/dev.sh          # dev mode on http://127.0.0.1:8000
#   ./scripts/dev.sh 9000     # ...on a specific port
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/scripts/run.sh" --dev "$@"

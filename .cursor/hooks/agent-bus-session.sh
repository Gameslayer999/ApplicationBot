#!/usr/bin/env bash
# Inject unread agent-bus messages at Cursor session start.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
python3 - <<'PY'
import json
try:
    from applicationbot.agent_bus import format_context_for_agent
    print(json.dumps({"additional_context": format_context_for_agent("cursor")}))
except Exception:
    print("{}")
PY
exit 0

#!/usr/bin/env bash
# After Cursor finishes a turn, nudge if unread bus mail exists.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
python3 - <<'PY'
import json
from applicationbot.agent_bus import list_messages

unread = list_messages(agent="cursor", unread_only=True)
if not unread:
    print("{}")
else:
    print(json.dumps({
        "followup_message": (
            f"Agent bus: {len(unread)} unread message(s) for cursor. "
            "Run `python -m applicationbot.agent_bus read --agent cursor`, act on them, "
            "and `ack <id>` when done. Post a handoff to claude if parallel work continues."
        )
    }))
PY
exit 0

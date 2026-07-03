"""Claude access status for the web UI.

The app tailors via the Claude Code CLI, which runs on the user's Claude **subscription**
(Pro/Max included usage) — not the metered API (see DECISIONS.md #011). So "auth" here is
simply: is Claude Code installed? Sign-in happens inside Claude Code itself (`claude` then
/login), not in this app. If a tailoring call fails because Claude Code isn't signed in,
that surfaces as a clear error at call time.
"""

from __future__ import annotations

import shutil

INSTALL_HINT = (
    "Claude tailoring uses your Claude subscription via Claude Code. Install Claude Code "
    "and sign in (https://claude.com/product/claude-code). Until then, the app uses the "
    "free, no-account 'rules' engine."
)


def claude_code_installed() -> bool:
    return shutil.which("claude") is not None


def status() -> dict:
    """Report whether the subscription-based Claude engine is available."""
    installed = claude_code_installed()
    return {
        "available": installed,
        "engine": "claude-code" if installed else "rules",
        "installed": installed,
        "hint": None if installed else INSTALL_HINT,
    }

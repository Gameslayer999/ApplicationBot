"""Claude access for the web UI — subscription-primary, API-key fallback (decision 111).

Two ways this app can reach Claude, in priority order:

1. **Claude subscription, via the Claude Code CLI** (PRIMARY). Tailoring shells out to
   `claude -p`, which runs on the user's Claude Pro/Max subscription — not the metered API
   (see DECISIONS.md #011). Sign-in happens inside Claude Code itself (`claude` then /login),
   not in this app. This is the only sanctioned way to use the subscription: Anthropic
   restricts subscription OAuth to Claude Code/Claude.ai and the Messages API rejects it, so
   a third-party app cannot "log in with Claude" on the subscription (decision 111).

2. **Anthropic API key** (FALLBACK). When Claude Code isn't installed/signed in, the app can
   tailor via the metered Anthropic API using the user's own key. The key is stored in the OS
   **keychain** (never plaintext YAML, never git — like the Workday/Gmail secrets), and billed
   pay-per-token to their API account, not their subscription.

If neither is available, the free, no-account `rules` engine runs.
"""

from __future__ import annotations

import shutil

import keyring

# OS-keychain slot for the fallback Anthropic API key (never git, never YAML).
_KR_SERVICE = "applicationbot-anthropic-api"
_KR_USER = "api_key"

INSTALL_HINT = (
    "Claude tailoring uses your Claude subscription via Claude Code (recommended). Install "
    "Claude Code and sign in (https://claude.com/product/claude-code) — or add an Anthropic "
    "API key as a fallback. Until then, the app uses the free, no-account 'rules' engine."
)


def claude_code_installed() -> bool:
    """Is the `claude` CLI (subscription engine) on PATH?"""
    return shutil.which("claude") is not None


# ---- Fallback Anthropic API key (OS keychain) ------------------------------------------

def get_api_key() -> str | None:
    try:
        return keyring.get_password(_KR_SERVICE, _KR_USER) or None
    except Exception:
        return None


def set_api_key(key: str) -> None:
    keyring.set_password(_KR_SERVICE, _KR_USER, (key or "").strip())


def clear_api_key() -> None:
    try:
        keyring.delete_password(_KR_SERVICE, _KR_USER)
    except Exception:
        pass  # already absent


def api_key_set() -> bool:
    return bool(get_api_key())


def api_key_masked() -> str | None:
    """A display-safe hint for 'which account' — last 4 chars only, never the full key."""
    k = get_api_key()
    if not k:
        return None
    return f"…{k[-4:]}" if len(k) >= 4 else "set"


# ---- Status for the UI -----------------------------------------------------------------

def active_engine() -> str:
    """Which Claude engine `auto` will use: subscription first, API key next, else rules."""
    if claude_code_installed():
        return "claude-code"
    if api_key_set():
        return "anthropic-api"
    return "rules"


def status() -> dict:
    """Report the subscription-primary / API-key-fallback state for the account panel."""
    installed = claude_code_installed()
    key_set = api_key_set()
    engine = active_engine()
    return {
        "available": installed or key_set,   # some Claude engine (not just rules) is available
        "engine": engine,                    # what `auto` resolves to right now
        "claude_code": installed,            # PRIMARY: subscription via Claude Code
        "api_key_set": key_set,              # FALLBACK: metered API key present
        "api_key_masked": api_key_masked(),  # "which account" hint (last 4 only)
        "installed": installed,              # back-compat with older callers
        "hint": None if (installed or key_set) else INSTALL_HINT,
    }

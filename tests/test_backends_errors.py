"""Claude CLI failure-taxonomy tests (decision 035 follow-up) — no subprocess, no network.

Run:  python -m tests.test_backends_errors   (also pytest-compatible)
"""
from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager

from applicationbot.backends import (
    ClaudeAuthError,
    ClaudeRateLimitError,
    ClaudeUnavailableError,
    _classify_cli_failure,
    run_claude_cli,
)


@contextmanager
def _fake_cli(returncode=1, stderr="", stdout="", which="/usr/local/bin/claude",
              raise_timeout=False):
    """Patch shutil.which + subprocess.run so run_claude_cli never spawns anything."""
    real_which, real_run = shutil.which, subprocess.run

    def fake_run(cmd, **kw):
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 300))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    shutil.which = lambda cli: which
    subprocess.run = fake_run
    try:
        yield
    finally:
        shutil.which, subprocess.run = real_which, real_run


def _raises(exc_type, fn):
    """Run fn, assert it raises exactly exc_type (not a subclass), return the exception."""
    try:
        fn()
    except exc_type as e:
        assert type(e) is exc_type, f"expected {exc_type.__name__}, got {type(e).__name__}"
        return e
    raise AssertionError(f"{exc_type.__name__} not raised")


def test_exceptions_subclass_runtimeerror():
    # Existing callers catch RuntimeError — the taxonomy must stay inside it.
    assert issubclass(ClaudeUnavailableError, RuntimeError)
    assert issubclass(ClaudeAuthError, ClaudeUnavailableError)
    assert issubclass(ClaudeRateLimitError, ClaudeUnavailableError)


def test_classifier_rate_limit_markers():
    for detail in [
        "Rate limit exceeded, retry later",
        "You've hit your usage limit",
        "usage cap reached for this subscription",
        "You have hit your limit for the 5-hour window",
        "Too many requests",
        "HTTP 429 from api",
        "The service is overloaded",
        "quota exceeded",
        "we are at capacity right now",
        "Your limit will reset at 3pm",
        "You are out of extended usage",
    ]:
        assert _classify_cli_failure(detail) is ClaudeRateLimitError, detail


def test_classifier_auth_markers():
    for detail in [
        "You are not logged in",
        "Please run /login to authenticate",
        "run /login first",
        "authentication failed",
        "Unauthorized request",
        "server returned 401",
        "invalid API key provided",
        "your session has expired",
    ]:
        assert _classify_cli_failure(detail) is ClaudeAuthError, detail


def test_classifier_defaults_to_unavailable():
    for detail in ["segmentation fault", "unknown flag --frobnicate", ""]:
        assert _classify_cli_failure(detail) is ClaudeUnavailableError, detail


def test_run_claude_cli_raises_rate_limit_error():
    with _fake_cli(returncode=1, stderr="You've hit your usage limit — resets 6pm"):
        e = _raises(ClaudeRateLimitError, lambda: run_claude_cli("hi"))
    msg = str(e)
    assert "usage limit" in msg.lower() and "exit 1" in msg  # actionable + exit preserved
    assert "wait" in msg.lower()  # states the next step


def test_run_claude_cli_raises_auth_error():
    with _fake_cli(returncode=1, stderr="Not logged in. Please run /login."):
        e = _raises(ClaudeAuthError, lambda: run_claude_cli("hi"))
    msg = str(e)
    assert "/login" in msg and "exit 1" in msg


def test_run_claude_cli_raises_unavailable_for_unrecognized_failure():
    with _fake_cli(returncode=2, stderr="boom: internal panic"):
        e = _raises(ClaudeUnavailableError, lambda: run_claude_cli("hi"))
    assert "exit 2" in str(e) and "internal panic" in str(e)


def test_run_claude_cli_missing_cli_is_auth_error():
    with _fake_cli(which=None):
        e = _raises(ClaudeAuthError, lambda: run_claude_cli("hi"))
    assert "not found" in str(e)


def test_run_claude_cli_timeout_is_unavailable():
    with _fake_cli(raise_timeout=True):
        e = _raises(ClaudeUnavailableError, lambda: run_claude_cli("hi", timeout=7))
    assert "timed out" in str(e)


def test_run_claude_cli_success_still_returns_result():
    with _fake_cli(returncode=0, stdout='{"result": "hello"}'):
        assert run_claude_cli("hi") == "hello"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} backend-error test(s) passed.")

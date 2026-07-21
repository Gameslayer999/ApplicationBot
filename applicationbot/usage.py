"""Claude token accounting (decision 095).

Every Claude call in the pipeline funnels through `backends.run_claude_cli`, whose
`--output-format json` envelope carries a `usage` block (input / output / cache tokens)
and `total_cost_usd`. That data was previously discarded. This module captures it and
attributes it to an *activity* (tailoring / form-entry / judging / …) and, when the call
is made while processing one posting, to that posting — so the Track tab can show how many
tokens each application cost, split by what Claude was doing.

Attribution is ambient, via two context variables, so no call-site signature changes:

- `activity(name)` / the `activity=` arg on `run_claude_cli` tags WHAT the call is doing.
- `for_posting(source_url)` tags WHICH application the call belongs to. It is set around
  the per-posting flow (tailoring in `pipeline.tailor_and_render`, form-fill in
  `apply.run_apply`), both keyed on the same posting URL the tracker rows use.

The batched fit judge (`matching.judge_fit_batch`) runs during discovery, across many
candidates at once, OUTSIDE any `for_posting` block — so its tokens land with no posting
key and are reported as one separate "discovery & judging" aggregate, never divided
across individual application rows (decision 095, user's choice).

Each captured call is written immediately to the `usage_events` table (append-only,
best-effort — a logging failure never sinks the Claude call that produced it).
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Iterator, Optional

# The posting a Claude call is being made for (its source URL), or None during discovery /
# standalone use. Matches `applications.source_url` so the Track tab can join by it.
_posting: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("posting", default=None)
# What the current call is doing — one of the ACTIVITIES labels below.
_activity: contextvars.ContextVar[str] = contextvars.ContextVar("activity", default="other")

# Known activity labels (for display grouping). Anything else records as-is under "other".
ACTIVITIES = ("tailoring", "form-entry", "judging", "enrichment", "salary", "impact", "other")


@contextlib.contextmanager
def for_posting(source_url: Optional[str], activity: Optional[str] = None) -> Iterator[None]:
    """Attribute every Claude call made in this block to `source_url` (an application), and
    optionally set the default activity for the block. Restores the prior context on exit."""
    tok_p = _posting.set((source_url or "").strip() or None)
    tok_a = _activity.set(activity) if activity else None
    try:
        yield
    finally:
        _posting.reset(tok_p)
        if tok_a is not None:
            _activity.reset(tok_a)


def push_posting(source_url: Optional[str], activity: Optional[str] = None):
    """Manual (non-`with`) form of `for_posting`, for code that can't wrap a block in a context
    manager (e.g. `apply.run_apply`, whose fill spans a large try/finally). Returns an opaque
    token to hand back to `pop_posting` from the paired `finally`."""
    return (_posting.set((source_url or "").strip() or None),
            _activity.set(activity) if activity else None)


def pop_posting(token) -> None:
    """Undo a `push_posting`, restoring the prior attribution context. Best-effort."""
    try:
        tok_p, tok_a = token
        _posting.reset(tok_p)
        if tok_a is not None:
            _activity.reset(tok_a)
    except Exception:
        pass


@contextlib.contextmanager
def activity(name: str) -> Iterator[None]:
    """Tag Claude calls in this block with `name` (tailoring / form-entry / …)."""
    tok = _activity.set(name)
    try:
        yield
    finally:
        _activity.reset(tok)


def _extract(usage: dict) -> dict:
    """Pull the four token counts out of a Claude CLI `usage` block (0 if absent)."""
    def n(key: str) -> int:
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return {
        "input_tokens": n("input_tokens"),
        "output_tokens": n("output_tokens"),
        "cache_read_tokens": n("cache_read_input_tokens"),
        "cache_creation_tokens": n("cache_creation_input_tokens"),
    }


def record(envelope: dict, *, activity: Optional[str] = None) -> None:
    """Record one Claude call's token usage from its parsed JSON envelope. `activity` overrides
    the ambient activity for this call. Best-effort: any failure (no usage block, DB error) is
    swallowed so token accounting can never break a working Claude call."""
    try:
        usage = envelope.get("usage")
        if not isinstance(usage, dict):
            return
        counts = _extract(usage)
        if not any(counts.values()):
            return
        model = ""
        mu = envelope.get("modelUsage")
        if isinstance(mu, dict) and mu:
            model = next(iter(mu))  # the single model id used for the call
        from . import tracker
        tracker.record_usage_event({
            "posting_key": _posting.get() or "",
            "activity": (activity or _activity.get() or "other"),
            "model": model,
            "cost_usd": float(envelope.get("total_cost_usd") or 0.0),
            **counts,
        }, path=tracker.DEFAULT_DB)  # read at call time so tests can redirect the DB
    except Exception:
        pass

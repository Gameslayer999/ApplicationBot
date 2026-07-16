"""Autonomous auto-apply loop (decision 069) — the "prepare-then-prompt" mode.

The user's ask: "look for as many matches as possible, then get started on them one by one
and prompt me as it needs me to start applying." It sits between the two runner modes:
  - the dry-run runner (`runner.run_queue`, gate off) prepares everything, prompts nothing;
  - the armed runner (gate on) submits everything up to a cap, prompts nothing.
This one prepares each cleared match as a dry-run and then waits for a per-application
go-ahead from the user before the (armed, one-shot) submit.

Token-frugal (an explicit user requirement — "we don't run through tokens"): every search
asks discovery for ONLY-NEW postings, so a posting is never re-judged; when a search returns
nothing new the world is exhausted and the loop stops rather than re-searching into the void.

A single browser drives everything on the web server (one worker slot), so this core
SERIALIZES preparation and user-requested submits through one thread — no concurrency. It is
pure and fully injected: no browser, no network, no threading policy lives here (the web
layer supplies the callables and the stop check), which keeps it unit-testable.
"""

from __future__ import annotations

from typing import Callable, Optional


def auto_apply_loop(
    discover_batch: Callable[[], list],
    prepare_one: Callable[[object], None],
    take_submit_requests: Callable[[], list],
    submit_one: Callable[[object], None],
    should_stop: Callable[[], bool],
    *,
    on_event: Optional[Callable[[str, object], None]] = None,
) -> str:
    """Run until the user stops it or the boards are exhausted. Returns ``"stopped"`` or
    ``"caught_up"``.

    Callables (all injected so this is testable with fakes):
      - ``discover_batch()`` → the cleared, only-new matches to prepare now; ``[]`` when
        nothing new remains anywhere (⇒ caught up, stop).
      - ``prepare_one(match)`` → tailor + PDF + headless dry-run fill for one match; records
        a tracker row. Never submits.
      - ``take_submit_requests()`` → the app-ids the user has clicked "Apply" on since the
        last check (and clears that queue).
      - ``submit_one(app_id)`` → armed one-shot submit of that one prepared application.
      - ``should_stop()`` → True once the user hit Stop.

    Ordering each round: honor pending submits FIRST (the user is waiting on those), then
    discover a fresh only-new batch, then prepare each match — re-checking for stop and for
    new submit requests between every application, so an Apply click is never blocked by more
    than one in-flight preparation."""
    on_event = on_event or (lambda kind, payload=None: None)

    def _drain_submits() -> bool:
        """Submit everything the user has queued, in click order. Returns False if a stop
        landed mid-drain (so the caller breaks out immediately)."""
        for app_id in take_submit_requests():
            if should_stop():
                return False
            on_event("submitting", app_id)
            submit_one(app_id)
            on_event("submitted", app_id)
        return True

    while not should_stop():
        if not _drain_submits():
            break
        on_event("searching", None)
        batch = discover_batch()
        if should_stop():
            break
        if not batch:
            on_event("caught_up", None)
            return "caught_up"
        on_event("batch", batch)
        for match in batch:
            if should_stop():
                break
            if not _drain_submits():
                break
            on_event("preparing", match)
            prepare_one(match)
            on_event("prepared", match)

    on_event("stopped", None)
    return "stopped"

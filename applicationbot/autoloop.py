"""Autonomous auto-apply loop (decision 069) — prepare, then apply.

The loop finds matches and prepares each one (tailor · export · fill) as a dry run. What
happens next is the caller's choice (decision 176):
  - ``apply_immediately=True`` (the loop's default in the UI) — each prepared application is
    submitted right away, no per-application click. That is the product: full automation.
  - ``apply_immediately=False`` (the "dry run" switch) — nothing is submitted; each prepared
    application waits in "Ready to apply" for the user's per-application go-ahead, which
    arrives through ``take_submit_requests``.
Either way the submit itself runs through the caller's ``submit_one``, so the safety gate
(the KILL file, the pre-submit required-field check) is unchanged.

Token-frugal (an explicit user requirement — "we don't run through tokens"): every search
asks discovery for ONLY-NEW postings, so a posting is never re-judged. With no goal set, a
search that returns nothing new means the world is exhausted and the loop stops rather than
re-searching into the void. With a goal set, an empty search instead backs off and searches
again (decision 146) — a target the user typed is a commitment, not a hint — and it is still
token-frugal because each pass judges only postings no earlier pass has scored.

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
    ready_count: Optional[Callable[[], int]] = None,
    goal: Optional[int] = None,
    maintain: bool = False,
    wait: Optional[Callable[[], None]] = None,
    take_watch_requests: Optional[Callable[[], list]] = None,
    watch_one: Optional[Callable[[object], None]] = None,
    take_rescan_requests: Optional[Callable[[], list]] = None,
    rescan_one: Optional[Callable[[object], None]] = None,
    take_prepare_requests: Optional[Callable[[], list]] = None,
    prepare_requested_one: Optional[Callable[[object], None]] = None,
    watch: bool = False,
    watch_wait: Optional[Callable[[], None]] = None,
    hunt_wait: Optional[Callable[[int], None]] = None,
    apply_immediately: bool = False,
) -> str:
    """Run until the user stops it, the boards are exhausted, or (goal mode) a target number
    of applications are ready for the user to review and submit. Returns ``"stopped"``,
    ``"caught_up"``, or ``"goal_reached"``.

    Callables (all injected so this is testable with fakes):
      - ``discover_batch()`` → the cleared, only-new matches to prepare now; ``[]`` when
        nothing new remains anywhere (⇒ caught up, stop).
      - ``prepare_one(match)`` → tailor + PDF + headless dry-run fill for one match; records
        a tracker row. Never submits. Returns the prepared application's id when it came out
        clean and submittable, else ``None`` (blocked, or nothing to submit) — that id is what
        ``apply_immediately`` submits.
      - ``take_submit_requests()`` → the app-ids the user has clicked "Apply" on since the
        last check (and clears that queue).
      - ``submit_one(app_id)`` → armed one-shot submit of that one prepared application.
      - ``take_watch_requests()`` → the app-ids the user has clicked "Watch the autofill" on
        since the last check (and clears that queue); optional, defaults to none.
      - ``watch_one(app_id)`` → open a VISIBLE dry-run of that one prepared application so the
        user can watch it fill; never submits. Optional, defaults to a no-op.
      - ``take_rescan_requests()`` → the app-ids the user has clicked "Rescan questions" on
        since the last check (and clears that queue); optional, defaults to none.
      - ``rescan_one(app_id)`` → HEADLESS dry-run re-fill of that one application, refreshing
        what its review panel knows about the form; never submits. Optional, no-op by default.
      - ``take_prepare_requests()`` → the postings the user has clicked "Apply"/"Apply anyway"
        on in the search breakdown since the last check (and clears that queue); optional,
        defaults to none.
      - ``prepare_requested_one(req)`` → prepare that one hand-picked posting (tailor + PDF +
        headless dry-run fill), exactly like ``prepare_one`` but for a posting the user chose
        rather than one ``discover_batch`` yielded — so it also serves postings below the fit
        cutoff (decision 174). Never submits itself; returns the prepared application's id like
        ``prepare_one``, so ``apply_immediately`` submits it too. Optional, no-op by default.
      - ``should_stop()`` → True once the user hit Stop.

    Apply mode (``apply_immediately=True``, decision 176): every application this loop prepares
    is submitted as soon as it is prepared — ``submit_one(app_id)`` on the id ``prepare_one`` /
    ``prepare_requested_one`` returned, on this same thread, before the next match is prepared.
    A ``None`` id (a blocked fill) is never submitted; it stays for the user. ``should_stop`` is
    re-checked between the prepare and the submit, so a Stop lands before an unwanted send.
    ``apply_immediately=False`` is the pre-decision-176 behaviour: prepare only, and submit
    exactly what the user asks for through ``take_submit_requests``.

    Goal mode (decision 121): when ``goal`` is set, ``ready_count()`` reports how many
    applications are currently prepared and ready for review/submission. The loop stops
    preparing once that reaches ``goal``:
      - ``maintain=False`` → reaching the goal ends the loop (returns ``"goal_reached"``).
      - ``maintain=True`` → the loop holds at the goal, idling via ``wait()`` (a
        stop-responsive short sleep the caller supplies) until the user submits some ready
        ones — dropping the count back below ``goal`` — then resumes discovering/preparing to
        top the pool back up. Ends only on stop or board exhaustion.
    With ``goal=None`` the goal checks are inert, so the pre-goal behaviour is unchanged.

    Keep hunting toward an unmet goal (decision 146): a goal is a target, not a hint — a search
    that comes back empty while the goal is still short must NOT end the run. The loop fires
    ``hunting`` with the number of consecutive empty searches, idles via ``hunt_wait(n)`` (a
    stop-responsive backoff the caller supplies, growing with ``n``), then searches again. It
    ends only on stop or on reaching the goal. Without a goal, an empty batch still means
    "boards exhausted" ⇒ ``"caught_up"``, unchanged.

    Watch mode (``watch=True``): the loop does NOT end when the boards are exhausted. Instead of
    returning ``"caught_up"`` on an empty batch, it fires ``caught_up`` (so the UI can say
    "watching, will re-check"), idles via ``watch_wait`` (a long, stop-responsive sleep the caller
    supplies — the between-poll interval), then re-searches. It keeps preparing each newly-posted
    match and holding it for the user's review; it ends ONLY on stop. This is the "autofill every
    new role but never submit until a human approves, forever" watch. ``watch=False`` is unchanged.

    Ordering each round: honor pending submits, watch, rescan and hand-picked prepare requests
    FIRST (the user is waiting on those), then — unless the goal is already met — discover a fresh
    only-new batch and prepare each match, re-checking for stop, for new user requests, and for
    the goal between every application, so an Apply, Watch, Rescan or Apply-anyway click is never
    blocked by more than one in-flight preparation."""
    on_event = on_event or (lambda kind, payload=None: None)
    wait = wait or (lambda: None)
    hunt_wait = hunt_wait or (lambda n: wait())
    take_watch_requests = take_watch_requests or (lambda: [])
    watch_one = watch_one or (lambda app_id: None)
    take_rescan_requests = take_rescan_requests or (lambda: [])
    rescan_one = rescan_one or (lambda app_id: None)
    take_prepare_requests = take_prepare_requests or (lambda: [])
    prepare_requested_one = prepare_requested_one or (lambda req: None)

    def _goal_met() -> bool:
        return goal is not None and ready_count is not None and ready_count() >= goal

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

    def _drain_watches() -> bool:
        """Open a visible dry-run for each app the user asked to watch, in click order. Never
        submits. Returns False if a stop landed mid-drain (so the caller breaks out)."""
        for app_id in take_watch_requests():
            if should_stop():
                return False
            on_event("watching", app_id)
            watch_one(app_id)
            on_event("watched", app_id)
        return True

    def _drain_rescans() -> bool:
        """Re-read the form of each app the user asked to rescan, in click order. Headless and
        never submits. Returns False if a stop landed mid-drain (so the caller breaks out)."""
        for app_id in take_rescan_requests():
            if should_stop():
                return False
            on_event("rescanning", app_id)
            rescan_one(app_id)
            on_event("rescanned", app_id)
        return True

    def _submit_prepared(app_id) -> None:
        """Submit one just-prepared application in apply mode. `app_id` is None when the fill
        came out blocked — that one waits for the user instead of being sent half-filled."""
        if not apply_immediately or app_id is None or should_stop():
            return
        on_event("submitting", app_id)
        submit_one(app_id)
        on_event("submitted", app_id)

    def _drain_prepares() -> bool:
        """Prepare each posting the user asked for by hand, in click order. A dry-run fill like
        `prepare_one` — then submitted immediately in apply mode, exactly like a match the loop
        found itself. Returns False if a stop landed mid-drain."""
        for req in take_prepare_requests():
            if should_stop():
                return False
            on_event("preparing_requested", req)
            app_id = prepare_requested_one(req)
            on_event("prepared_requested", req)
            _submit_prepared(app_id)
        return True

    def _serve_requests() -> bool:
        """Honor pending submits, then watches, rescans, and hand-picked prepares. False on a
        mid-drain stop."""
        return (_drain_submits() and _drain_watches() and _drain_rescans()
                and _drain_prepares())

    dry_searches = 0  # consecutive searches that returned nothing (drives the hunt backoff)
    while not should_stop():
        if not _serve_requests():
            break
        if _goal_met():
            on_event("goal_reached", ready_count() if ready_count else goal)
            if not maintain:
                return "goal_reached"
            # Maintain: hold at the goal, idling until the user submits some (dropping the
            # count) or stops. wait() is stop-responsive, so a Stop ends the idle promptly.
            wait()
            continue
        on_event("searching", None)
        batch = discover_batch()
        if should_stop():
            break
        if not batch:
            on_event("caught_up", None)
            if watch:
                # Watch mode: the boards are exhausted for now, but a new role could post any time.
                # Idle the poll interval (stop-responsive), then loop back to re-search — never stop
                # on our own. wait defaults to watch_wait; both are stop-responsive.
                (watch_wait or wait)()
                continue
            if goal is not None:
                # Goal set and still short (a met goal already returned/idled above): keep hunting.
                # Back off (stop-responsive, growing with the dry-search count) and search again —
                # the caller's discover_batch is expected to widen/refresh each pass so a retry can
                # actually surface something the last pass didn't.
                dry_searches += 1
                on_event("hunting", dry_searches)
                hunt_wait(dry_searches)
                continue
            return "caught_up"
        dry_searches = 0
        on_event("batch", batch)
        for match in batch:
            if should_stop():
                break
            if not _serve_requests():
                break
            if _goal_met():
                # Hit the goal mid-batch — stop preparing and re-evaluate at the top of the
                # loop (end the run, or idle if maintaining). Leftover matches this round are
                # simply not prepared; the next search (only_new) won't re-surface them.
                break
            on_event("preparing", match)
            app_id = prepare_one(match)
            on_event("prepared", match)
            _submit_prepared(app_id)

    on_event("stopped", None)
    return "stopped"

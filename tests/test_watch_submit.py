"""Watching the bot actually SEND an application, and sending untailored on purpose (decision 179).

Two things the UI promises and this pins:
  - "Watch it apply" runs the SAME armed submit, only headed and holding its window open — one
    application, not the whole run;
  - "Show the browser while it applies" makes every submit in a run visible (and is inert in a dry
    run, which submits nothing).

No browser and no Claude: `_armed_submit` (the only thing that drives Playwright here) is stubbed,
so what's under test is the loop's own decision about what to show.
"""

import pytest

from applicationbot import web


@pytest.fixture(autouse=True)
def loop_state():
    """A fresh, idle loop state per test — these poke module state directly."""
    with web._LOOP_LOCK:
        web._LOOP_STATE.clear()
        web._LOOP_STATE.update(web._loop_reset())
        web._LOOP_STATE["running"] = False
        web._LOOP_SUBMITS.clear()
        web._LOOP_WATCH_SUBMITS.clear()
    web._LOOP_STOP.clear()
    web._LOOP_WATCH_HOLD.clear()
    yield
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False
        web._LOOP_SUBMITS.clear()
        web._LOOP_WATCH_SUBMITS.clear()
    web._LOOP_STOP.clear()
    web._LOOP_WATCH_HOLD.clear()


def _record_submits(monkeypatch) -> list:
    """Capture how each submit was run instead of driving a browser."""
    calls: list = []

    def fake(app_id, note, *, headed=False, hold=None):
        calls.append({"id": app_id, "headed": headed, "hold": hold})
        return "ok", True

    monkeypatch.setattr(web, "_armed_submit", fake)
    return calls


# --- what the loop thread actually shows ----------------------------------------------------

def test_default_submit_is_invisible(monkeypatch):
    calls = _record_submits(monkeypatch)
    web._loop_submit(1)
    assert calls == [{"id": 1, "headed": False, "hold": None}]


def test_watch_it_apply_shows_the_browser_and_holds_the_window(monkeypatch):
    calls = _record_submits(monkeypatch)
    with web._LOOP_LOCK:
        web._LOOP_WATCH_SUBMITS.add(7)
    web._loop_submit(7)
    assert calls[0]["headed"] is True
    assert calls[0]["hold"] is web._LOOP_WATCH_HOLD   # stays open on the result until closed
    # One click, one visible submit: the next application is back to headless.
    web._loop_submit(8)
    assert calls[1]["headed"] is False and calls[1]["hold"] is None


def test_show_browser_makes_every_submit_visible_without_holding_windows(monkeypatch):
    calls = _record_submits(monkeypatch)
    web._loop_set(show_browser=True)
    web._loop_submit(1)
    web._loop_submit(2)
    assert [c["headed"] for c in calls] == [True, True]
    # A whole run of visible submits must not stall on a window the user has to close.
    assert [c["hold"] for c in calls] == [None, None]


def test_a_stop_is_not_re_held_by_a_watched_submit(monkeypatch):
    """Stop releases the watch hold. Arming the next watched window must not undo that, or the
    Stop would sit behind a window nothing can close."""
    calls = _record_submits(monkeypatch)
    with web._LOOP_LOCK:
        web._LOOP_WATCH_SUBMITS.add(3)
    web._LOOP_STOP.set()
    web._LOOP_WATCH_HOLD.set()
    web._loop_submit(3)
    assert web._LOOP_WATCH_HOLD.is_set()


def test_the_cap_still_governs_a_watched_submit(monkeypatch):
    calls = _record_submits(monkeypatch)
    web._loop_set(cap=1)
    with web._LOOP_LOCK:
        web._LOOP_WATCH_SUBMITS.add(2)
    web._loop_submit(1)
    web._loop_submit(2)              # over the cap — watched or not, it is not sent
    assert [c["id"] for c in calls] == [1]
    assert web._LOOP_STATE["cap_hit"] is True


# --- the queue the click lands in ----------------------------------------------------------

def test_watch_apply_queues_for_the_running_loop(monkeypatch):
    monkeypatch.setattr(web, "_mark_reviewed", lambda app_id: None)
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = True
    assert web.queue_watch_submit(5) == {"ok": True, "queued": True}
    with web._LOOP_LOCK:
        assert web._LOOP_SUBMITS == [5]          # it IS a submit, so the cap/queue see it
        assert web._LOOP_WATCH_SUBMITS == {5}    # …and the loop thread runs it headed


def test_watch_apply_with_the_loop_idle_runs_the_visible_armed_reapply(monkeypatch):
    """Idle: `start_reapply(arm=True)` already fills headed and pauses on the result, which is
    exactly what watching a submit means — so the click must route there, armed."""
    got = {}
    monkeypatch.setattr(web, "_mark_reviewed", lambda app_id: None)
    monkeypatch.setattr(web, "start_reapply", lambda app_id, **kw: got.update(id=app_id, **kw) or {"ok": True})
    assert web.queue_watch_submit(9)["ok"] is True
    assert got == {"id": 9, "arm": True}


# --- the show-the-browser run switch --------------------------------------------------------------

def _started(monkeypatch, **kw) -> dict:
    """start_loop with the worker stubbed; returns the worker's positional args by name."""
    import time
    got = {}
    monkeypatch.setattr(web, "_loop_worker", lambda *a: got.update(args=a))
    assert web.start_loop(**kw)["ok"] is True
    for _ in range(200):
        if "args" in got:
            break
        time.sleep(0.01)
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False
    names = ("rescan", "force_retailor", "goal", "maintain", "watch", "watch_interval", "dry_run")
    return dict(zip(names, got["args"]))


def test_show_browser_is_recorded_for_the_run(monkeypatch):
    _started(monkeypatch, show_browser=True)
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["show_browser"] is True


def test_show_browser_is_refused_in_a_dry_run(monkeypatch):
    """A dry run submits nothing, so there is no submit to watch — the flag must not be stored as
    if a window were going to open."""
    _started(monkeypatch, dry_run=True, show_browser=True)
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["show_browser"] is False

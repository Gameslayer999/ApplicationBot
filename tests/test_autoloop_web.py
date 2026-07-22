"""Web glue for the auto-apply loop (decision 069): drive the real worker thread with fakes —
no browser, no Claude, no network. Verifies start_loop prepares matches, populates the ready
queue, stops cleanly, and that queue_submit routes correctly."""

import time
from types import SimpleNamespace as NS

import pytest

from applicationbot import web


def _wait_until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def fake_pipeline(monkeypatch):
    """Patch every heavy dependency the loop worker reaches so it runs end-to-end offline.
    Discovery yields one batch of two matches, then nothing (⇒ caught up)."""
    import applicationbot.backends as backends
    import applicationbot.filters as filters
    import applicationbot.pipeline as pipeline
    import applicationbot.runner as runner

    monkeypatch.setattr(backends, "claude_code_available", lambda: True)
    monkeypatch.setattr(filters, "load_filters",
                        lambda *a, **k: NS(boards=[NS(token="acme", ats="lever")],
                                           adzuna=NS(app_id="", app_key="")))
    monkeypatch.setattr(web, "load_resume", lambda *a, **k: NS())
    monkeypatch.setattr(web.apply_profile, "load_profile", lambda *a, **k: NS())

    calls = {"discover": 0, "cached": 0, "prepared": [], "only_new": []}
    matches = [NS(posting=NS(company="Acme", title="Backend Eng", url="http://x/1"), fit_score=88),
               NS(posting=NS(company="Bolt", title="Full-Stack", url="http://x/2"), fit_score=81)]

    def discover(*a, **k):
        calls["discover"] += 1
        calls["only_new"].append(k.get("only_new"))
        return NS(matches=matches if calls["discover"] == 1 else [], errors=[])

    def cached(*a, **k):
        calls["cached"] += 1
        return list(matches)

    monkeypatch.setattr(pipeline, "discover_and_match", discover)
    monkeypatch.setattr(pipeline, "cached_matches", cached)
    monkeypatch.setattr(pipeline, "effective_min_fit", lambda f: (70, None))
    monkeypatch.setattr(runner, "cleared_queue", lambda ms, mf: list(ms))

    def prepare(resume, m, *a, **k):
        calls["prepared"].append(m.posting.url)

    monkeypatch.setattr(pipeline, "run_testing_mode", prepare)
    # Each prepared posting yields a clean dry-run tracker row (id derived from the URL tail).
    monkeypatch.setattr(web.tracker, "find_by_source_url",
                        lambda url, **k: {"id": int(url[-1]), "status": "dry-run"})
    return calls


def test_loop_prepares_batch_and_reaches_caught_up(fake_pipeline):
    assert web.start_loop()["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    assert fake_pipeline["prepared"] == ["http://x/1", "http://x/2"]
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["prepared"] == 2
        assert sorted(web._LOOP_STATE["ready_ids"]) == [1, 2]
        assert web._LOOP_STATE["phase"] == "caught_up"


def test_goal_stops_after_reaching_target(fake_pipeline):
    # goal=1 (decision 121): prepare until one application is ready, then stop — the second
    # match in the batch is never prepared, and the phase is goal_reached (not caught_up).
    assert web.start_loop(goal=1)["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    assert fake_pipeline["prepared"] == ["http://x/1"]
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["ready_ids"] == [1]
        assert web._LOOP_STATE["phase"] == "goal_reached"
        assert web._LOOP_STATE["goal"] == 1


def test_goal_zero_and_negative_mean_no_target(fake_pipeline):
    # A non-positive goal is treated as "no target" (runs the boards to exhaustion), and
    # maintain is forced off when there's no goal.
    assert web.start_loop(goal=0, maintain=True)["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    assert fake_pipeline["prepared"] == ["http://x/1", "http://x/2"]
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["phase"] == "caught_up"
        assert web._LOOP_STATE["goal"] is None
        assert web._LOOP_STATE["maintain"] is False


def test_rescan_reuses_cached_scores_without_rejudging(fake_pipeline):
    # rescan=True re-prepares the whole cached set once, reusing cached fit scores — it must
    # NOT call the Claude judge (discover_and_match) and must be a bounded one-shot.
    assert web.start_loop(rescan=True)["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    assert fake_pipeline["prepared"] == ["http://x/1", "http://x/2"]
    assert fake_pipeline["cached"] == 1     # scores pulled from the snapshot, once
    assert fake_pipeline["discover"] == 0   # never re-judged via Claude
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["phase"] == "caught_up"


def test_rescan_with_nothing_cached_bails_with_actionable_message(fake_pipeline, monkeypatch):
    import applicationbot.pipeline as pipeline
    monkeypatch.setattr(pipeline, "cached_matches", lambda *a, **k: [])
    assert web.start_loop(rescan=True)["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    assert fake_pipeline["prepared"] == []
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["phase"] == "caught_up"
        assert "normal auto-apply loop" in web._LOOP_STATE["message"]


def test_default_start_keeps_only_new(fake_pipeline):
    assert web.start_loop()["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    assert fake_pipeline["only_new"][0] is True  # default path re-judges only new openings
    assert fake_pipeline["cached"] == 0          # default never touches the score cache


def test_double_start_is_rejected(fake_pipeline, monkeypatch):
    # Freeze discovery so the loop stays running long enough to reject a second start.
    import applicationbot.pipeline as pipeline
    gate = {"go": False}
    monkeypatch.setattr(pipeline, "discover_and_match",
                        lambda *a, **k: (_wait_until(lambda: gate["go"], 2.0),
                                         NS(matches=[], errors=[]))[1])
    assert web.start_loop()["ok"] is True
    assert _wait_until(lambda: web._loop_running())
    assert web.start_loop() == {"ok": False, "error": "The auto-apply loop is already running."}
    gate["go"] = True
    assert _wait_until(lambda: not web._loop_running())


def test_stop_when_idle_is_noop():
    web._LOOP_STOP.clear()
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False
    assert web.stop_loop() == {"ok": True, "already": True}


def test_queue_submit_falls_back_to_reapply_when_loop_idle(monkeypatch):
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False
    seen = {}
    monkeypatch.setattr(web, "start_reapply",
                        lambda app_id, arm=False: seen.update(id=app_id, arm=arm) or {"ok": True})
    assert web.queue_submit(7) == {"ok": True}
    assert seen == {"id": 7, "arm": True}


def test_queue_submit_enqueues_while_loop_running():
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = True
        web._LOOP_SUBMITS.clear()
    try:
        assert web.queue_submit(5) == {"ok": True, "queued": True}
        assert web.queue_submit(5) == {"ok": True, "queued": True}  # de-duped
        with web._LOOP_LOCK:
            assert web._LOOP_SUBMITS == [5]
    finally:
        with web._LOOP_LOCK:
            web._LOOP_STATE["running"] = False
            web._LOOP_SUBMITS.clear()

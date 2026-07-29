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

    calls = {"discover": 0, "cached": 0, "prepared": [], "only_new": [], "force_fresh": [],
             "revisit": []}
    # Shaped like a real Match/Posting: the loop publishes a search breakdown (decision 149),
    # which reads the same judged-row fields the dry-run panel shows.
    def _match(n, company, title, fit):
        return NS(posting=NS(company=company, title=title, url=f"http://x/{n}",
                             location="Remote", compensation="", ats="lever"),
                  fit_score=fit, qualified=True, dimensions=None, why="", missing=[])

    matches = [_match(1, "Acme", "Backend Eng", 88), _match(2, "Bolt", "Full-Stack", 81)]

    def discover(*a, **k):
        calls["discover"] += 1
        calls["only_new"].append(k.get("only_new"))
        calls["force_fresh"].append(k.get("force_fresh"))
        calls["revisit"].append(k.get("revisit"))
        found = matches if calls["discover"] == 1 else []
        return NS(matches=found, errors=[], from_cache=False, discovered=len(found),
                  funnel={"discovered": len(found), "matched": len(found), "judged": len(found)})

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
    # Neutralize the notification side effect (decision 145): the real _record_and_push writes to
    # the default (real) tracker DB and fires a real desktop notification. These tests exercise the
    # loop mechanics only — notifications are covered in test_notifications.py — so stub it, or
    # running this file would spam the developer with pushes and pollute applications.db.
    monkeypatch.setattr(web, "_record_and_push", lambda *a, **k: None)
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


def test_rescan_cached_but_below_min_fit_names_the_real_reason(fake_pipeline, monkeypatch):
    # The cache is full but nothing clears min_fit (best 41 < 70). The message must NOT claim
    # "nothing scored" — it must name the min_fit gap + the fix (UI Principle #3).
    import applicationbot.pipeline as pipeline
    import applicationbot.runner as runner
    from types import SimpleNamespace as NS
    low = [NS(posting=NS(company="Acme", title="Sr Eng", url="http://x/9"), fit_score=41)]
    monkeypatch.setattr(pipeline, "cached_matches", lambda *a, **k: low)
    # Use the real min_fit filter here (the fixture stubs it to pass everything) so the
    # below-bar match (41 < 70) is actually excluded and the below-bar branch is exercised.
    monkeypatch.setattr(runner, "cleared_queue",
                        lambda ms, mf: [m for m in ms if m.fit_score is not None and m.fit_score >= mf])
    assert web.start_loop(rescan=True)["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    assert fake_pipeline["prepared"] == []
    with web._LOOP_LOCK:
        msg = web._LOOP_STATE["message"]
    assert "best fit" in msg and "41" in msg and "70" in msg
    assert "min_fit" in msg
    assert "nothing scored" not in msg.lower()


def test_unmet_goal_keeps_searching_live_until_it_is_met(fake_pipeline, monkeypatch):
    """Decision 146 (the reported bug): with goal=2, a search that finds nothing must not end the
    run with "no new matches" — the loop backs off and searches again until the goal is met. The
    retries must go LIVE (force_fresh), since replaying the cached snapshot can only re-serve
    postings the seen-ledger already hides."""
    import applicationbot.pipeline as pipeline

    monkeypatch.setattr(web, "_hunt_backoff", lambda n: 0.01)  # no real 60s backoff in tests
    first = [NS(posting=NS(company="Acme", title="Backend Eng", url="http://x/1"), fit_score=88)]
    later = [NS(posting=NS(company="Bolt", title="Full-Stack", url="http://x/2"), fit_score=81)]

    def discover(*a, **k):
        fake_pipeline["discover"] += 1
        fake_pipeline["force_fresh"].append(k.get("force_fresh"))
        n = fake_pipeline["discover"]
        batch = first if n == 1 else (later if n == 4 else [])  # passes 2 and 3 find nothing
        return NS(matches=batch, errors=[], from_cache=False)

    monkeypatch.setattr(pipeline, "discover_and_match", discover)

    assert web.start_loop(goal=2)["ok"] is True
    assert _wait_until(lambda: not web._loop_running(), timeout=5.0), "loop did not finish"
    assert fake_pipeline["prepared"] == ["http://x/1", "http://x/2"]
    assert fake_pipeline["discover"] == 4          # kept hunting through two empty passes
    assert fake_pipeline["force_fresh"] == [False, True, True, True]
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["phase"] == "goal_reached"
        assert sorted(web._LOOP_STATE["ready_ids"]) == [1, 2]


def test_hunt_message_names_progress_and_the_next_search(fake_pipeline, monkeypatch):
    # While hunting, the status must say how far along the goal is and when the next pass runs
    # (UI Principles #3/#5) — never "caught up, no new matches", which is what looked broken.
    import applicationbot.pipeline as pipeline

    monkeypatch.setattr(web, "_hunt_backoff", lambda n: 120)
    monkeypatch.setattr(pipeline, "discover_and_match",
                        lambda *a, **k: NS(matches=[], errors=[], from_cache=False))
    assert web.start_loop(goal=5)["ok"] is True
    assert _wait_until(lambda: web._LOOP_STATE.get("phase") == "hunting", timeout=5.0), \
        "loop never reported hunting"
    msg = web._LOOP_STATE["message"]
    web.stop_loop()
    assert _wait_until(lambda: not web._loop_running(), timeout=5.0)
    assert "0 of 5 ready" in msg and "2 min" in msg and "caught up" not in msg.lower()


def test_hunt_backoff_escalates_then_caps():
    assert [web._hunt_backoff(n) for n in (1, 2, 3, 4, 5, 9)] == [60, 120, 300, 900, 1800, 1800]


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


def test_watch_keeps_running_after_caught_up_then_stops(fake_pipeline):
    # Watch mode (decision 143): after preparing the first batch and exhausting the boards, the
    # loop must NOT stop — it enters the "watching" phase and idles, re-checking on the interval.
    # It ends only when the user stops it. This is the "autofill new roles, hold for review,
    # never submit, forever" watch.
    assert web.start_loop(watch=True, watch_interval=1)["ok"] is True
    assert _wait_until(lambda: web._LOOP_STATE.get("phase") == "watching"), "never entered watch idle"
    assert web._loop_running()  # still alive, unlike the non-watch caught_up path
    assert fake_pipeline["prepared"] == ["http://x/1", "http://x/2"]
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["watch"] is True
        assert web._LOOP_STATE["watch_interval"] == 1
    # Re-searched the boards fresh each poll so a newly-posted role would be seen.
    assert fake_pipeline["discover"] >= 2
    web.stop_loop()  # wakes the idle immediately
    assert _wait_until(lambda: not web._loop_running()), "watch loop did not stop"


def test_rescan_forces_watch_off(fake_pipeline):
    # A one-shot rescan can't also "keep watching" — watch is forced off so it stays bounded.
    assert web.start_loop(rescan=True, watch=True)["ok"] is True
    assert _wait_until(lambda: not web._loop_running()), "loop did not finish"
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["watch"] is False
        assert web._LOOP_STATE["phase"] == "caught_up"

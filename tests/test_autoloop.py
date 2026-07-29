"""Tests for the auto-apply loop core (decision 069) — fully injected, no browser/network."""

from applicationbot.autoloop import auto_apply_loop


class _Match:
    """Minimal stand-in for matching.Match — the core only passes it through."""
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"M({self.name})"


def _driver(batches, *, submit_scripts=None, stop_after=None):
    """Build the four callables + a log. `batches` is a list of batches discover returns in
    order (a trailing [] means "caught up"). `submit_scripts[i]` is the list of app-ids the
    user has queued when take_submit_requests() is called for the i-th time. `stop_after`
    stops once the log has that many entries."""
    log: list = []
    state = {"discovered": 0, "took": 0}
    submit_scripts = submit_scripts or []

    def discover_batch():
        i = state["discovered"]
        state["discovered"] += 1
        batch = batches[i] if i < len(batches) else []
        log.append(("search", len(batch)))
        return batch

    def prepare_one(m):
        log.append(("prepare", m.name))

    def take_submit_requests():
        i = state["took"]
        state["took"] += 1
        return list(submit_scripts[i]) if i < len(submit_scripts) else []

    def submit_one(app_id):
        log.append(("submit", app_id))

    def should_stop():
        return stop_after is not None and len(log) >= stop_after

    return log, discover_batch, prepare_one, take_submit_requests, submit_one, should_stop


def test_prepares_whole_batch_then_stops_when_caught_up():
    batch = [_Match("a"), _Match("b"), _Match("c")]
    log, *cbs = _driver([batch, []])
    reason = auto_apply_loop(*cbs)
    assert reason == "caught_up"
    # First search returns the batch, each is prepared in order, second search is empty ⇒ stop.
    assert log == [
        ("search", 3),
        ("prepare", "a"), ("prepare", "b"), ("prepare", "c"),
        ("search", 0),
    ]


def test_multiple_batches_until_dry():
    log, *cbs = _driver([[_Match("a")], [_Match("b")], []])
    reason = auto_apply_loop(*cbs)
    assert reason == "caught_up"
    assert log == [
        ("search", 1), ("prepare", "a"),
        ("search", 1), ("prepare", "b"),
        ("search", 0),
    ]


def test_stop_halts_mid_batch():
    batch = [_Match("a"), _Match("b"), _Match("c")]
    # Stop once 2 log entries exist: search + prepare(a), so b/c are never prepared.
    log, *cbs = _driver([batch], stop_after=2)
    reason = auto_apply_loop(*cbs)
    assert reason == "stopped"
    assert log == [("search", 3), ("prepare", "a")]


def test_submits_are_drained_before_search_and_between_preps():
    batch = [_Match("a"), _Match("b")]
    # take() is called: [0] top-of-loop, [1] before prepare(a), [2] before prepare(b),
    # [3] top-of-loop round 2. Queue a submit at the first and third calls.
    log, *cbs = _driver([batch, []], submit_scripts=[[10], [], [20], []])
    reason = auto_apply_loop(*cbs)
    assert reason == "caught_up"
    assert log == [
        ("submit", 10),               # drained before the first search
        ("search", 2),
        ("prepare", "a"),
        ("submit", 20),               # drained between prepare(a) and prepare(b)
        ("prepare", "b"),
        ("search", 0),
    ]


def test_empty_first_search_is_immediately_caught_up():
    log, *cbs = _driver([[]])
    assert auto_apply_loop(*cbs) == "caught_up"
    assert log == [("search", 0)]


def test_events_are_emitted():
    events: list = []
    batch = [_Match("a")]
    _, *cbs = _driver([batch, []])
    auto_apply_loop(*cbs, on_event=lambda k, p=None: events.append(k))
    kinds = [e for e in events]
    assert "searching" in kinds
    assert "preparing" in kinds
    assert "prepared" in kinds
    assert "caught_up" in kinds


# --------------------------------------------------------------------------- goal mode (121)

def _goal_driver(batches, ready_counts, *, submit_scripts=None):
    """Like `_driver`, but `ready_count()` returns successive values from `ready_counts`
    (last value repeats once exhausted), simulating the ready pool growing as prepares land
    or shrinking as the user submits. Each prepare that reaches the goal is what a real
    `ready_count` would reflect on the NEXT check."""
    log, discover_batch, prepare_one, take_submit_requests, submit_one, should_stop = _driver(
        batches, submit_scripts=submit_scripts)
    rc_state = {"i": 0}

    def ready_count():
        i = min(rc_state["i"], len(ready_counts) - 1)
        rc_state["i"] += 1
        val = ready_counts[i]
        log.append(("ready", val))
        return val

    return log, discover_batch, prepare_one, take_submit_requests, submit_one, should_stop, ready_count


def test_goal_stop_reached_before_any_prepare():
    # Two already ready ⇒ goal of 2 is met at the first check; nothing is prepared/searched.
    batch = [_Match("a")]
    log, *cbs, ready_count = _goal_driver([batch, []], [2])
    reason = auto_apply_loop(*cbs, ready_count=ready_count, goal=2)
    assert reason == "goal_reached"
    assert ("search", 1) not in log and not any(k == "prepare" for k, _ in log)


def test_goal_stop_reached_mid_batch():
    batch = [_Match("a"), _Match("b"), _Match("c")]
    # ready_count checks: top-of-loop=0, then after each prepare it climbs. It hits the goal
    # of 2 after two prepares, so c is never prepared and the loop ends goal_reached.
    log, *cbs, ready_count = _goal_driver([batch, []], [0, 0, 1, 2])
    reason = auto_apply_loop(*cbs, ready_count=ready_count, goal=2)
    assert reason == "goal_reached"
    prepared = [n for k, n in log if k == "prepare"]
    assert prepared == ["a", "b"]  # c dropped once the goal was hit


def test_goal_none_is_inert_backward_compat():
    # goal=None with a ready_count provided must behave exactly like the pre-goal loop.
    batch = [_Match("a"), _Match("b")]
    log, *cbs, ready_count = _goal_driver([batch, []], [0])
    reason = auto_apply_loop(*cbs, ready_count=ready_count, goal=None)
    assert reason == "caught_up"
    assert [n for k, n in log if k == "prepare"] == ["a", "b"]


def test_goal_maintain_idles_then_refills_then_stops():
    # Prepare 1 to reach goal=1, idle in maintain until a Stop lands during wait().
    batch = [_Match("a")]
    waits = {"n": 0}
    stop = {"flag": False}

    log: list = []
    disc = {"i": 0}

    def discover_batch():
        i = disc["i"]; disc["i"] += 1
        b = batch if i == 0 else []
        log.append(("search", len(b)))
        return b

    def prepare_one(m):
        log.append(("prepare", m.name))

    ready = {"n": 0}
    def ready_count():
        return ready["n"]

    def take_submit_requests():
        return []

    def submit_one(app_id):
        pass

    def should_stop():
        return stop["flag"]

    def prepare_and_bump(m):
        prepare_one(m)
        ready["n"] += 1  # the prepared app is now ready

    def wait():
        waits["n"] += 1
        log.append(("wait", waits["n"]))
        if waits["n"] >= 2:
            stop["flag"] = True  # user Stops after a couple of idle ticks

    reason = auto_apply_loop(discover_batch, prepare_and_bump, take_submit_requests,
                             submit_one, should_stop, ready_count=ready_count, goal=1,
                             maintain=True, wait=wait)
    # Maintain never returns goal_reached: it idles at the goal then ends on Stop.
    assert reason == "stopped"
    assert ("prepare", "a") in log
    assert ("wait", 1) in log and ("wait", 2) in log


def test_goal_maintain_refills_after_a_submit_drop():
    # Reach goal=1, idle once, then a submit drops the count so the loop searches again. Since
    # decision 146 the dry search that follows does NOT end the run (the goal is short again) —
    # it hunts, so this ends on Stop rather than "caught_up".
    batch1, batch2 = [_Match("a")], [_Match("b")]
    log: list = []
    disc = {"i": 0}
    ready = {"n": 0}
    waited = {"n": 0}
    stop = {"flag": False}

    def discover_batch():
        i = disc["i"]; disc["i"] += 1
        b = [batch1, batch2, []][i] if i < 3 else []
        log.append(("search", [m.name for m in b]))
        return b

    def prepare_one(m):
        log.append(("prepare", m.name))
        ready["n"] += 1

    def take_submit_requests():
        return []

    def submit_one(app_id):
        pass

    def should_stop():
        return stop["flag"]

    def ready_count():
        return ready["n"]

    def wait():
        waited["n"] += 1
        ready["n"] -= 1  # simulate the user applying to a ready one during the idle

    def hunt_wait(n):
        log.append(("hunt", n))
        stop["flag"] = True  # user Stops rather than let it keep hunting

    reason = auto_apply_loop(discover_batch, prepare_one, take_submit_requests, submit_one,
                             should_stop, ready_count=ready_count, goal=1, maintain=True,
                             wait=wait, hunt_wait=hunt_wait)
    # a → goal met → idle drops count → b prepared → goal met → idle → boards dry ⇒ hunt, not quit.
    assert reason == "stopped"
    assert [n for k, n in log if k == "prepare"] == ["a", "b"]
    assert ("hunt", 1) in log


def test_watch_keeps_recycling_after_caught_up_until_stop():
    # Watch mode (decision 143): batch 1 has a match, then the boards are dry forever. The loop
    # must NOT return "caught_up" on the empty batch — it idles via watch_wait and re-searches,
    # ending only on stop. Proves the "keep watching, autofill new roles, never stop" contract.
    log: list = []
    state = {"searches": 0, "waits": 0}

    def discover_batch():
        state["searches"] += 1
        log.append(("search", state["searches"]))
        return [_Match("A")] if state["searches"] == 1 else []

    def prepare_one(m):
        log.append(("prepare", m.name))

    def watch_wait():
        state["waits"] += 1
        log.append(("wait", state["waits"]))

    reason = auto_apply_loop(
        discover_batch, prepare_one, lambda: [], lambda i: None,
        should_stop=lambda: state["waits"] >= 3,  # stop after 3 idle cycles
        watch=True, watch_wait=watch_wait)

    assert reason == "stopped"                     # never returned "caught_up"
    assert ("prepare", "A") in log                 # prepared the one real match
    assert state["searches"] >= 3 and state["waits"] >= 1  # kept re-searching + idling


def test_watch_false_still_stops_at_caught_up():
    # Backward-compat: without watch=True, an empty batch still ends the loop immediately.
    state = {"n": 0}

    def discover_batch():
        state["n"] += 1
        return [_Match("A")] if state["n"] == 1 else []

    reason = auto_apply_loop(discover_batch, lambda m: None, lambda: [], lambda i: None,
                             should_stop=lambda: False)
    assert reason == "caught_up"


# ------------------------------------------------------- keep hunting toward a goal (decision 146)

def _hunt_driver(batches, *, goal, stop_after_waits=None):
    """Driver whose ready_count reflects real prepares (each prepare makes one app ready), with a
    hunt_wait that logs the escalating dry-search counter and can trip a Stop."""
    log: list = []
    state = {"i": 0, "ready": 0, "waits": 0}

    def discover_batch():
        i = state["i"]; state["i"] += 1
        b = batches[i] if i < len(batches) else []
        log.append(("search", [m.name for m in b]))
        return b

    def prepare_one(m):
        log.append(("prepare", m.name))
        state["ready"] += 1

    def hunt_wait(n):
        state["waits"] += 1
        log.append(("hunt", n))

    def should_stop():
        return stop_after_waits is not None and state["waits"] >= stop_after_waits

    kwargs = dict(ready_count=lambda: state["ready"], goal=goal, hunt_wait=hunt_wait)
    return log, (discover_batch, prepare_one, lambda: [], lambda i: None, should_stop), kwargs


def test_goal_unmet_keeps_searching_instead_of_reporting_caught_up():
    # The reported bug: goal=2, the first search finds one match and the next finds nothing.
    # The loop must NOT stop at "no new matches" — it keeps hunting until the goal is met.
    batches = [[_Match("a")], [], [], [_Match("b")]]
    log, cbs, kwargs = _hunt_driver(batches, goal=2)
    assert auto_apply_loop(*cbs, **kwargs) == "goal_reached"
    assert [n for k, n in log if k == "prepare"] == ["a", "b"]
    # Two dry searches in a row ⇒ the backoff counter escalates, and resets once a batch lands.
    assert [n for k, n in log if k == "hunt"] == [1, 2]


def test_goal_hunt_ends_on_stop_and_never_returns_caught_up():
    # Boards dry forever with the goal short: the loop hunts until the user stops. It must never
    # return "caught_up" — that message is what made the loop look broken.
    log, cbs, kwargs = _hunt_driver([[]], goal=5, stop_after_waits=3)
    assert auto_apply_loop(*cbs, **kwargs) == "stopped"
    assert [n for k, n in log if k == "hunt"] == [1, 2, 3]


def test_goal_hunt_defaults_to_wait_when_no_hunt_wait_supplied():
    # hunt_wait is optional: it falls back to the maintain-mode wait() so the loop still idles.
    state = {"i": 0, "waits": 0}

    def discover_batch():
        state["i"] += 1
        return [_Match("a")] if state["i"] == 1 else []

    def wait():
        state["waits"] += 1

    reason = auto_apply_loop(discover_batch, lambda m: None, lambda: [], lambda i: None,
                             should_stop=lambda: state["waits"] >= 2,
                             ready_count=lambda: 0, goal=3, wait=wait)
    assert reason == "stopped" and state["waits"] == 2


def test_no_goal_still_stops_at_caught_up():
    # Backward-compat: hunting is goal-driven. With goal=None an empty batch ends the run.
    log, cbs, kwargs = _hunt_driver([[_Match("a")], []], goal=None)
    assert auto_apply_loop(*cbs, **kwargs) == "caught_up"
    assert not [n for k, n in log if k == "hunt"]

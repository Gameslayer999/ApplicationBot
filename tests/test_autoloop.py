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

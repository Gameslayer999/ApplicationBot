"""The auto-apply loop applies by default; "Dry run" is the switch that holds submission back
(decision 176).

Before this, the loop only ever PREPARED — every submission needed a per-application click. These
tests pin the new default (prepare → submit, immediately, no click), the switch that restores the
old behaviour, and the two things that must survive the change: a blocked fill is never submitted,
and a Stop lands between the prepare and the submit.
"""

import time
from types import SimpleNamespace as NS

import pytest

from applicationbot import autoloop, web


class _Match:
    def __init__(self, name):
        self.name = name


def _run(*, apply_immediately, batches, ids, stop_at=None, prepare_reqs=None):
    """Drive the core with fake prepares that return `ids[name]` (None ⇒ blocked). Returns the
    ordered log of what the loop did."""
    log: list = []
    state = {"i": 0}
    reqs = list(prepare_reqs or [])

    def discover_batch():
        i = state["i"]
        state["i"] += 1
        batch = batches[i] if i < len(batches) else []
        log.append(("search", len(batch)))
        return batch

    def prepare_one(m):
        log.append(("prepare", m.name))
        return ids.get(m.name)

    def prepare_requested_one(req):
        log.append(("prepare_requested", req))
        return ids.get(req)

    def take_prepare_requests():
        out, reqs[:] = list(reqs), []
        return out

    def submit_one(app_id):
        log.append(("submit", app_id))

    autoloop.auto_apply_loop(
        discover_batch, prepare_one, lambda: [], submit_one,
        lambda: stop_at is not None and any(e == stop_at for e in log),
        take_prepare_requests=take_prepare_requests,
        prepare_requested_one=prepare_requested_one,
        apply_immediately=apply_immediately)
    return log


# --- the core loop --------------------------------------------------------------------------

def test_apply_mode_submits_each_application_as_soon_as_it_is_prepared():
    log = _run(apply_immediately=True, batches=[[_Match("a"), _Match("b")], []],
               ids={"a": 11, "b": 12})
    # Interleaved, not batched at the end: each application is sent before the next is prepared.
    assert log == [("search", 2),
                   ("prepare", "a"), ("submit", 11),
                   ("prepare", "b"), ("submit", 12),
                   ("search", 0)]


def test_dry_run_prepares_everything_and_submits_nothing():
    log = _run(apply_immediately=False, batches=[[_Match("a"), _Match("b")], []],
               ids={"a": 11, "b": 12})
    assert ("submit", 11) not in log and ("submit", 12) not in log
    assert log == [("search", 2), ("prepare", "a"), ("prepare", "b"), ("search", 0)]


def test_a_blocked_preparation_is_never_submitted():
    # prepare_one returns None when the fill stopped on something needing the user — sending that
    # half-filled application is exactly the failure this guards.
    log = _run(apply_immediately=True, batches=[[_Match("a"), _Match("b")], []],
               ids={"a": None, "b": 12})
    assert log == [("search", 2),
                   ("prepare", "a"),
                   ("prepare", "b"), ("submit", 12),
                   ("search", 0)]


def test_stop_lands_between_the_prepare_and_the_submit():
    log = _run(apply_immediately=True, batches=[[_Match("a")], []], ids={"a": 11},
               stop_at=("prepare", "a"))
    assert log == [("search", 1), ("prepare", "a")]   # prepared, never sent


def test_a_hand_picked_posting_is_submitted_too_in_apply_mode():
    log = _run(apply_immediately=True, batches=[[]], ids={"http://x/2": 9},
               prepare_reqs=["http://x/2"])
    assert log == [("prepare_requested", "http://x/2"), ("submit", 9), ("search", 0)]


def test_a_hand_picked_posting_only_waits_in_a_dry_run():
    log = _run(apply_immediately=False, batches=[[]], ids={"http://x/2": 9},
               prepare_reqs=["http://x/2"])
    assert log == [("prepare_requested", "http://x/2"), ("search", 0)]


# --- the web layer --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_loop_state():
    with web._LOOP_LOCK:
        web._LOOP_STATE.clear()
        web._LOOP_STATE.update(web._loop_reset())
        web._LOOP_STATE["running"] = False
        web._LOOP_STATE["ready_ids"] = []
    yield
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False


def _started(monkeypatch, **kw):
    """Call start_loop with the worker stubbed out, and return the args it was handed. The stub
    returns immediately, so clear `running` afterwards the way a finished run would."""
    got = {}
    monkeypatch.setattr(web, "_loop_worker", lambda *a: got.update(args=a))
    assert web.start_loop(**kw)["ok"] is True
    for _ in range(200):                      # the worker runs on its own thread
        if "args" in got:
            break
        time.sleep(0.01)
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False
    return got["args"]


DRY_RUN_ARG = 6  # _loop_worker(rescan, force_retailor, goal, maintain, watch, interval, dry_run, …)


def test_start_loop_applies_by_default(monkeypatch):
    args = _started(monkeypatch)
    assert args[DRY_RUN_ARG] is False         # dry_run — off unless asked for
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["dry_run"] is False


def test_the_dry_run_switch_reaches_the_worker(monkeypatch):
    args = _started(monkeypatch, dry_run=True)
    assert args[DRY_RUN_ARG] is True
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["dry_run"] is True


def test_keep_topping_up_is_ignored_in_apply_mode(monkeypatch):
    # "Top up as you apply" is a dry-run idea: an apply-mode loop applies to the ready ones
    # itself, so the count never falls back below the goal and maintain could only spin.
    args = _started(monkeypatch, goal=3, maintain=True)
    assert args[3] is False
    args = _started(monkeypatch, goal=3, maintain=True, dry_run=True)
    assert args[3] is True


def test_apply_mode_does_not_ask_for_approval_it_will_not_wait_for(monkeypatch):
    pushed = []
    monkeypatch.setattr(web, "_record_and_push", lambda notifier, note, app_id=None: pushed.append(note))
    row = {"id": 7, "status": "dry-run", "resume_source": "X"}
    notifier = NS(cfg=NS(event_enabled=lambda e: True), channels=[])

    assert web._mark_ready(row, "Acme", "Eng", 80, notifier, notify_ready=False) == "ready"
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["ready_ids"] == [7]    # still tracked, just not announced
    assert pushed == []                               # no "review and submit it" push

    # A blocked one still notifies in either mode — that one really is waiting on the user.
    blocked = {"id": 8, "status": "blocked", "blocked_detail": "login needed"}
    assert web._mark_ready(blocked, "Acme", "Eng", 80, notifier, notify_ready=False) == "blocked"
    assert len(pushed) == 1 and "login needed" in pushed[0].body


@pytest.fixture
def loop_env(monkeypatch):
    """Everything `_loop_worker` touches, stubbed: no Claude, no boards, no browser, no DB, no
    push. Returns the list of run_apply calls — i.e. what was actually SUBMITTED."""
    import applicationbot.apply as apply_mod
    import applicationbot.pipeline as pipeline
    import applicationbot.runner as runner

    posting = NS(company="Acme", title="Backend Eng", url="http://x/1", ats="lever")
    match = NS(posting=posting, fit_score=88)
    served = {"done": False}

    def discover_and_match(*a, **k):
        if served["done"]:
            return NS(matches=[], errors=[], funnel={}, discovered=0, from_cache=False)
        served["done"] = True
        return NS(matches=[match], errors=[], funnel={}, discovered=1, from_cache=False)

    monkeypatch.setattr(web, "load_resume", lambda *a, **k: NS())
    monkeypatch.setattr(web.apply_profile, "load_profile", lambda *a, **k: NS())
    loaded = NS(boards=["b"], adzuna=NS(app_id=""))
    monkeypatch.setattr(web.filters, "load_filters", lambda *a, **k: loaded)
    monkeypatch.setattr(web, "_record_and_push", lambda *a, **k: None)
    monkeypatch.setattr("applicationbot.backends.claude_code_available", lambda: True)
    monkeypatch.setattr("applicationbot.notifications.build_notifier",
                        lambda *a, **k: NS(cfg=NS(event_enabled=lambda e: True), channels=[]))
    monkeypatch.setattr(pipeline, "effective_min_fit", lambda f: (70, ""))
    monkeypatch.setattr(pipeline, "discover_and_match", discover_and_match)
    prepares = []
    monkeypatch.setattr(pipeline, "run_testing_mode", lambda *a, **k: prepares.append(k))
    # No submission cap and no real safety.yaml read — the cap has its own tests.
    monkeypatch.setattr("applicationbot.safety.load_gate",
                        lambda *a, **k: NS(max_submissions_per_run=0))
    monkeypatch.setattr(runner, "cleared_queue", lambda ms, mf: list(ms))
    monkeypatch.setattr(web.tracker, "find_by_source_url",
                        lambda url, **k: {"id": 5, "status": "dry-run", "resume_source": "X"})
    monkeypatch.setattr(web.tracker, "get_application", lambda i: {
        "id": 5, "source_url": "http://x/1", "resume_path": __file__, "company": "Acme",
        "role": "Backend Eng", "fit_score": 88, "resume_source": "X"})
    monkeypatch.setattr(apply_mod, "AnswerResolver", lambda **k: NS())

    class _Submits(list):
        """The submits, carrying the prepares' kwargs and the loaded filters alongside them."""
        prepares: list = []
        filters = None

    submits = _Submits()

    def run_apply(url, pdf, resolver, **k):
        submits.append({"url": url, "gate": k.get("gate")})
        return NS(submitted=True, submit_state="submitted", confirmation="thanks",
                  blockers=[], filled=[], skipped=[])

    monkeypatch.setattr(apply_mod, "run_apply", run_apply)
    submits.prepares = prepares
    submits.filters = loaded
    return submits


def test_the_real_worker_submits_what_it_prepares(loop_env):
    web._LOOP_STOP.clear()
    web._loop_worker(dry_run=False)
    assert [s["url"] for s in loop_env] == ["http://x/1"]
    gate = loop_env[0]["gate"]
    assert gate is not None and gate.armed is True   # the armed one-shot gate, KILL still checked
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["submitted"] == 1
        assert web._LOOP_STATE["ready_ids"] == []    # sent, so no longer waiting on the user
    assert "1 application(s) submitted" in web._LOOP_STATE["message"]


def test_the_real_worker_prepares_under_the_saved_tailoring_policy(loop_env):
    # "Never tailor" + a loosened reuse bar, saved in Loop settings (decision 178), must reach the
    # prepare itself — not just be stored.
    loop_env.filters.tailor_mode = "never"
    loop_env.filters.reuse_threshold = 0.4
    web._LOOP_STOP.clear()
    web._loop_worker(dry_run=True)
    assert [(k["tailor"], k["force_retailor"], k["reuse_threshold"]) for k in loop_env.prepares] \
        == [(False, False, 0.4)]


def test_the_real_worker_submits_nothing_in_a_dry_run(loop_env):
    web._LOOP_STOP.clear()
    web._loop_worker(dry_run=True)
    assert loop_env == []
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["ready_ids"] == [5]   # prepared and waiting for the user's click
    assert "ready for you to apply" in web._LOOP_STATE["message"]


def test_the_dry_run_message_still_asks_for_the_click(monkeypatch):
    row = {"id": 7, "status": "dry-run", "resume_source": "X"}
    notifier = NS(cfg=NS(event_enabled=lambda e: True), channels=[])
    monkeypatch.setattr(web, "_record_and_push", lambda *a, **k: None)
    assert web._mark_ready(row, "Acme", "Eng", 80, notifier) == "ready"
    assert "Ready to apply" in web._prepared_msg("ready", "Acme — Eng", row)
    assert "submitting it now" in web._prepared_msg("ready", "Acme — Eng", row, live=True)

"""Apply / Apply anyway from the search breakdown (decision 174).

The fit cutoff decides what the pipeline applies to AUTOMATICALLY. These tests pin the manual
override the UI now offers: the user can prepare ANY judged posting — including one Claude scored
below `min_fit` — with or without tailoring, and it lands in the same "Ready to apply" queue where
the existing armed button is still the only thing that submits.
"""

import time
from types import SimpleNamespace as NS

import pytest

from applicationbot import autoloop, web


def _wait_until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _match(url, fit, company="Acme", title="Backend Eng"):
    return NS(posting=NS(company=company, title=title, url=url, location="Remote",
                         compensation="", ats="lever"),
              fit_score=fit, qualified=(fit is not None and fit >= 70),
              dimensions=None, why="", missing=[])


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts with empty judged/loop/test state and leaves it that way — these are
    module-level singletons shared with the real server."""
    for reset in (True, False):
        with web._JUDGED_LOCK:
            web._JUDGED_MATCHES.clear()
        with web._LOOP_LOCK:
            web._LOOP_PREPARES.clear()
            web._LOOP_STATE["running"] = False
            web._LOOP_STATE["ready_ids"] = []
        with web._TEST_LOCK:
            web._TEST_STATE.clear()
            web._TEST_STATE.update(web._test_reset())
            web._TEST_STATE["phase"] = "idle"
        if reset:
            yield


@pytest.fixture
def prepared(monkeypatch):
    """Stub everything an Apply click touches: no Claude, no browser, no tracker DB, no push.

    The list it returns records the PREPARE (`run_testing_mode`); `prepared.submits` records the
    submit that now follows it (decision 177)."""
    import applicationbot.apply as apply_mod
    import applicationbot.pipeline as pipeline

    class _Calls(list):
        """A list of prepares that also carries the submits, so the fixture stays one object."""
        submits: list = []

    calls = _Calls()
    submits = []

    def run_testing_mode(resume, m, *a, **k):
        calls.append({"url": m.posting.url, "tailor": k.get("tailor", True),
                      "gate": k.get("gate", "MISSING"), "headed": k.get("headed")})

    def run_apply(url, pdf, resolver, **k):
        submits.append({"url": url, "gate": k.get("gate")})
        return NS(submitted=True, submit_state="submitted", confirmation="thanks",
                  blockers=[], filled=[], skipped=[])

    monkeypatch.setattr(pipeline, "run_testing_mode", run_testing_mode)
    monkeypatch.setattr(web, "load_resume", lambda *a, **k: NS())
    monkeypatch.setattr(web.apply_profile, "load_profile", lambda *a, **k: NS())
    monkeypatch.setattr(web.tracker, "find_by_source_url",
                        lambda url, **k: {"id": 7, "status": "dry-run", "resume_source": "X"})
    monkeypatch.setattr(web.tracker, "get_application", lambda i: {
        "id": 7, "source_url": "http://x/2", "resume_path": __file__, "company": "Acme",
        "role": "Backend Eng", "fit_score": 31, "resume_source": "X"})
    monkeypatch.setattr(apply_mod, "AnswerResolver", lambda **k: NS())
    monkeypatch.setattr(apply_mod, "run_apply", run_apply)
    monkeypatch.setattr(web, "_record_and_push", lambda *a, **k: None)
    calls.submits = submits
    return calls


# --- the match index the buttons resolve against -------------------------------------------

def test_judged_rows_indexes_every_scored_match_for_apply():
    rows = web._judged_rows([_match("http://x/1", 88), _match("http://x/2", 31),
                             _match("http://x/3", None)], min_fit=70)
    # Both scored postings are addressable by URL — the below-bar one included, since it is
    # exactly what "Apply anyway" needs. The unscored one has no verdict to override.
    assert web._match_for_url("http://x/1").fit_score == 88
    assert web._match_for_url("http://x/2").fit_score == 31
    assert [r["cleared"] for r in rows] == [True, False]


def test_match_index_is_bounded_and_drops_the_oldest():
    for i in range(web._JUDGED_CAP + 25):
        web._judged_rows([_match(f"http://x/{i}", 80)], min_fit=70)
    with web._JUDGED_LOCK:
        assert len(web._JUDGED_MATCHES) == web._JUDGED_CAP
        assert "http://x/0" not in web._JUDGED_MATCHES        # oldest evicted
        assert f"http://x/{web._JUDGED_CAP + 24}" in web._JUDGED_MATCHES   # newest kept


def test_unknown_url_falls_back_to_the_snapshot_then_gives_up(monkeypatch):
    import applicationbot.pipeline as pipeline
    monkeypatch.setattr(web, "load_resume", lambda *a, **k: NS())
    monkeypatch.setattr(web.apply_profile, "load_profile", lambda *a, **k: NS())
    monkeypatch.setattr(web.filters, "load_filters", lambda *a, **k: NS())
    monkeypatch.setattr(pipeline, "cached_matches", lambda *a, **k: [_match("http://x/9", 55)])
    assert web._match_for_url("http://x/9").fit_score == 55     # served from the snapshot
    assert web._match_for_url("http://x/nope") is None


# --- preparing a posting the user picked ----------------------------------------------------

def test_apply_anyway_applies_to_a_below_bar_posting(prepared):
    web._judged_rows([_match("http://x/2", 31)], min_fit=70)   # denied by the cutoff
    assert web.queue_prepare("http://x/2")["ok"] is True
    assert _wait_until(lambda: web._TEST_STATE.get("phase") == "done"), web._TEST_STATE

    # Two steps, in order: the fill is still a dry run (gate=None) …
    assert prepared == [{"url": "http://x/2", "tailor": True, "gate": None, "headed": False}]
    # … then the click's own submit, through the armed one-shot gate (decision 177).
    assert [s["url"] for s in prepared.submits] == ["http://x/2"]
    assert prepared.submits[0]["gate"].armed is True
    with web._LOOP_LOCK:
        assert web._LOOP_STATE["ready_ids"] == []   # sent, so nothing is left waiting
    assert "Submitted to Acme" in web._TEST_STATE["message"]


def test_a_blocked_fill_is_never_submitted_by_the_click(prepared, monkeypatch):
    # The form stopped on something needing the user (a login, an unanswerable question). It parks
    # for them — submitting a half-filled application is exactly what must not happen.
    monkeypatch.setattr(web.tracker, "find_by_source_url",
                        lambda url, **k: {"id": 7, "status": "blocked", "blocked_detail": "login"})
    web._judged_rows([_match("http://x/2", 31)], min_fit=70)
    assert web.queue_prepare("http://x/2")["ok"] is True
    assert _wait_until(lambda: web._TEST_STATE.get("phase") == "done"), web._TEST_STATE
    assert prepared.submits == []
    assert "blocked" in web._TEST_STATE["message"] or "stopped" in web._TEST_STATE["message"]


def test_apply_without_tailoring_passes_the_choice_through(prepared):
    web._judged_rows([_match("http://x/1", 88)], min_fit=70)
    assert web.queue_prepare("http://x/1", tailor=False)["ok"] is True
    assert _wait_until(lambda: web._TEST_STATE.get("phase") == "done"), web._TEST_STATE
    assert prepared[0]["tailor"] is False


def test_preparing_keeps_the_breakdown_the_user_clicked_in(prepared):
    rows = web._judged_rows([_match("http://x/2", 31)], min_fit=70)
    web._set(judged=rows, min_fit=70, funnel={"discovered": 4}, phase="done")
    assert web.queue_prepare("http://x/2")["ok"] is True
    # The judged list, cutoff and funnel survive the prepare — resetting them would erase the
    # list the user is clicking in while they watch.
    assert web._TEST_STATE["judged"] == rows
    assert web._TEST_STATE["min_fit"] == 70
    assert web._TEST_STATE["funnel"] == {"discovered": 4}
    assert _wait_until(lambda: web._TEST_STATE.get("phase") == "done")
    assert web._TEST_STATE["judged"] == rows


def test_a_posting_with_no_scored_details_reports_how_to_get_them_back(monkeypatch):
    import applicationbot.pipeline as pipeline
    monkeypatch.setattr(web, "load_resume", lambda *a, **k: NS())
    monkeypatch.setattr(web.apply_profile, "load_profile", lambda *a, **k: NS())
    monkeypatch.setattr(web.filters, "load_filters", lambda *a, **k: NS())
    monkeypatch.setattr(pipeline, "cached_matches", lambda *a, **k: [])
    assert web.queue_prepare("http://x/gone")["ok"] is True
    assert _wait_until(lambda: web._TEST_STATE.get("phase") == "error"), web._TEST_STATE
    msg = " ".join(web._TEST_STATE["errors"])
    assert "Search again" in msg and "Apply" in msg      # names the fix, not just the failure


def test_empty_url_is_refused_before_anything_runs():
    r = web.queue_prepare("  ")
    assert r["ok"] is False and "no URL" in r["error"]


def test_a_run_in_progress_is_refused_rather_than_fighting_for_the_browser():
    with web._TEST_LOCK:
        web._TEST_STATE["phase"] = "running"
    r = web.queue_prepare("http://x/1")
    assert r["ok"] is False and "already in progress" in r["error"]


# --- routing while the auto-apply loop owns the browser -------------------------------------

def test_a_running_loop_takes_the_request_onto_its_own_thread():
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = True
    assert web.queue_prepare("http://x/2", tailor=False) == {"ok": True, "queued": True}
    web.queue_prepare("http://x/2")             # same posting twice ⇒ queued once
    assert web._loop_take_prepares() == [("http://x/2", False)]
    assert web._loop_take_prepares() == []      # taking clears the queue


def test_the_loop_drains_prepare_requests_before_searching():
    served, order = [], []
    reqs = [("http://x/2", False)]

    def take():
        out, reqs[:] = list(reqs), []
        return out

    autoloop.auto_apply_loop(
        discover_batch=lambda: (order.append("discover") or []),
        prepare_one=lambda m: None,
        take_submit_requests=lambda: [],
        submit_one=lambda i: None,
        should_stop=lambda: False,
        take_prepare_requests=take,
        prepare_requested_one=lambda r: (order.append("prepare") or served.append(r)),
    )
    assert served == [("http://x/2", False)]
    assert order[0] == "prepare"      # the user is waiting on it; the search can come after

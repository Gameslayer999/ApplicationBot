"""Tailoring is decided per application, not by a run-wide checkbox (decision 180).

The two mode checkboxes are gone (the loop panel's "Don't tailor" switch and the search
breakdown's "Tailor my résumé to the posting first"), so ⚙ Loop settings alone governs what the
loop prepares by itself, and a SINGLE application carries its own choice:
  - a judged posting is applied to with or without tailoring by which of its two buttons is
    clicked — the server side of that is `queue_prepare(url, tailor=…)`, unchanged but now the
    only path;
  - a prepared application's review panel re-tailors THAT one application from its saved job
    description (`retailor=True`), served on the loop thread when the loop owns the browser.

No browser and no Claude: the tailor/render call and `run_apply` are stubbed.
"""

from types import SimpleNamespace as NS

import pytest

from applicationbot import reuse, web


@pytest.fixture(autouse=True)
def loop_state():
    with web._LOOP_LOCK:
        web._LOOP_STATE.clear()
        web._LOOP_STATE.update(web._loop_reset())
        web._LOOP_STATE["running"] = False
        web._LOOP_RESCANS.clear()
        web._LOOP_RETAILORS.clear()
    yield
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False
        web._LOOP_RESCANS.clear()
        web._LOOP_RETAILORS.clear()


APP = {"id": 4, "company": "Acme", "role": "Backend Engineer", "fit_score": 82,
       "source_url": "https://boards.greenhouse.io/acme/jobs/1",
       "resume_path": "/tmp/acme.pdf", "resume_source": "Your uploaded résumé (as-is)"}


def _stub_refill(monkeypatch, tailored="/tmp/acme-tailored.pdf"):
    """Stub everything the re-fill touches; capture the PDF each fill actually used."""
    used: list = []
    monkeypatch.setattr(web.tracker, "get_application", lambda app_id, **kw: dict(APP, id=app_id))
    monkeypatch.setattr(web, "load_resume", lambda path: NS())
    monkeypatch.setattr(web.apply_profile, "load_profile", lambda *a, **k: NS())
    monkeypatch.setattr(web.Path, "is_file", lambda self: True)

    def fake_run_apply(url, pdf, resolver, **kw):
        used.append({"pdf": pdf, "source": (kw.get("meta") or {}).get("resume_source"),
                     "headed": kw.get("headed"), "gate": kw.get("gate")})
        return NS(filled=[1, 2], skipped=[], submitted=False, url=url, screenshot="",
                  summary=lambda: "")

    import applicationbot.apply as apply_mod
    monkeypatch.setattr(apply_mod, "run_apply", fake_run_apply)
    monkeypatch.setattr(apply_mod, "AnswerResolver", lambda **kw: NS())
    monkeypatch.setattr(web, "_retailor_pdf", lambda app, status_cb=None: tailored)
    return used


# --- the review panel's own tailoring control ----------------------------------------------

def test_a_plain_rescan_keeps_the_resume_it_has(monkeypatch):
    used = _stub_refill(monkeypatch)
    web._loop_rescan(4)
    assert used[0]["pdf"] == APP["resume_path"]
    assert used[0]["gate"] is None and used[0]["headed"] is False   # still a headless dry run


def test_re_tailoring_one_application_refills_with_the_new_resume(monkeypatch):
    used = _stub_refill(monkeypatch)
    with web._LOOP_LOCK:
        web._LOOP_RETAILORS.add(4)
    web._loop_rescan(4)
    assert used[0]["pdf"] == "/tmp/acme-tailored.pdf"
    # The provenance must flip too, or the panel would still call it an untailored résumé.
    assert used[0]["source"] == reuse.FRESH
    assert used[0]["gate"] is None                                  # re-tailoring never submits
    assert "Re-tailored" in web._LOOP_STATE["message"]


def test_the_re_tailor_tag_is_consumed_once(monkeypatch):
    """One click, one re-tailor: the next rescan of the same application must not spend Claude
    usage again."""
    used = _stub_refill(monkeypatch)
    with web._LOOP_LOCK:
        web._LOOP_RETAILORS.add(4)
    web._loop_rescan(4)
    web._loop_rescan(4)
    assert [u["pdf"] for u in used] == ["/tmp/acme-tailored.pdf", APP["resume_path"]]


def test_no_saved_jd_reports_the_reason_and_refills_nothing(monkeypatch):
    used = _stub_refill(monkeypatch)

    def no_jd(app, status_cb=None):
        raise LookupError("This application has no saved job description, so its résumé can't be "
                          "re-tailored (it predates that being stored).")

    monkeypatch.setattr(web, "_retailor_pdf", no_jd)
    with web._LOOP_LOCK:
        web._LOOP_RETAILORS.add(4)
    web._loop_rescan(4)
    assert used == []                                        # nothing was filled with a stale PDF
    msg = web._LOOP_STATE["message"]
    assert "no saved job description" in msg and "Back to preparing" in msg


# --- which thread serves the click ----------------------------------------------------------

def test_re_tailor_is_queued_for_the_running_loop(monkeypatch):
    monkeypatch.setattr(web, "_mark_reviewed", lambda app_id: None)
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = True
    assert web.queue_rescan(4, retailor=True) == {"ok": True, "queued": True}
    with web._LOOP_LOCK:
        assert web._LOOP_RESCANS == [4] and web._LOOP_RETAILORS == {4}


def test_a_plain_rescan_is_not_tagged_for_re_tailoring(monkeypatch):
    monkeypatch.setattr(web, "_mark_reviewed", lambda app_id: None)
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = True
    web.queue_rescan(4)
    with web._LOOP_LOCK:
        assert web._LOOP_RESCANS == [4] and web._LOOP_RETAILORS == set()


def test_with_the_loop_idle_the_re_tailor_runs_here(monkeypatch):
    got = {}
    monkeypatch.setattr(web, "_mark_reviewed", lambda app_id: None)
    monkeypatch.setattr(web, "start_rescan", lambda app_id, retailor=False: got.update(
        id=app_id, retailor=retailor) or {"ok": True})
    assert web.queue_rescan(4, retailor=True)["ok"] is True
    assert got == {"id": 4, "retailor": True}


# --- the run-wide switches really are gone --------------------------------------------------

def test_the_loop_policy_has_no_per_run_override():
    """⚙ Loop settings is the only thing that decides the loop's own résumé (decision 180)."""
    import inspect
    assert list(inspect.signature(web._loop_policy).parameters) == ["filters_obj"]
    assert "no_tailor" not in inspect.signature(web.start_loop).parameters
    saved = NS(tailor_mode="always", tailor_below_fit=65, reuse_threshold=0.8)
    assert web._loop_policy(saved)["mode"] == "always"


def test_neither_tailor_checkbox_is_still_in_the_page():
    html = web.INDEX_HTML
    assert 'id="loop-no-tailor"' not in html
    assert "jtailor-box" not in html and "JUDGED_TAILOR" not in html
    # …and each judged row carries the choice instead, as two labelled buttons.
    assert 'data-jtailor="1"' in html and 'data-jtailor="0"' in html

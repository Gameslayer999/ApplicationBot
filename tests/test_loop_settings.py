"""Loop settings — set before you start a run (decision 178).

Four settings the loop reads at start: which résumé each application gets (the tailoring policy),
the fit cutoff, how strict résumé reuse is, and how many applications one run may submit. These
tests pin the policy's per-posting decision, the save/load round-trip through the real config
files, and the cap actually stopping a run.
"""

from types import SimpleNamespace as NS

import pytest

from applicationbot import filters as filters_mod
from applicationbot import safety, web


# --- the résumé policy, per posting ---------------------------------------------------------

def _policy(mode, below=70, thr=0.9):
    return {"mode": mode, "below": below, "reuse_threshold": thr}


def test_smart_tailors_and_leaves_the_reuse_paths_alone():
    assert web._tailor_choice(_policy("smart"), 88) == (True, False)


def test_always_forces_a_fresh_tailor():
    assert web._tailor_choice(_policy("always"), 88) == (True, True)


def test_never_sends_the_resume_as_is():
    assert web._tailor_choice(_policy("never"), 12) == (False, False)


def test_under_tailors_only_what_needs_it():
    p = _policy("under", below=70)
    assert web._tailor_choice(p, 55) == (True, False)    # weaker match — tailor to close the gap
    assert web._tailor_choice(p, 70) == (False, False)   # at the bar — the résumé already fits
    assert web._tailor_choice(p, 91) == (False, False)
    # Unscored must not silently become "send it untailored".
    assert web._tailor_choice(p, None) == (True, False)


def test_an_unknown_mode_falls_back_to_the_pre_178_behaviour():
    pol = web._loop_policy(NS(tailor_mode="banana", tailor_below_fit="x", reuse_threshold=None))
    assert pol == {"mode": "smart", "below": 70, "reuse_threshold": 0.9}
    # A config written before this decision has none of the fields at all.
    assert web._loop_policy(NS()) == {"mode": "smart", "below": 70, "reuse_threshold": 0.9}


# --- the settings round-trip ----------------------------------------------------------------

@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the real loaders/savers at throwaway files, so the round-trip is the real one."""
    safe = tmp_path / "safety.yaml"
    safe.write_text("armed: true\nmax_submissions_per_run: 10\nnav_agentic: true\n")
    # `web.safety` IS the safety module, so capture the real functions before redirecting them at
    # the throwaway files — patching in terms of themselves would recurse forever.
    real_load, real_save = safety.load_gate, safety.save_max_submissions
    monkeypatch.setattr(web.filters, "load_filters", lambda *a, **k: _loaded["f"])
    monkeypatch.setattr(web.filters, "save_filters", lambda f, *a, **k: _loaded.update(f=f))
    monkeypatch.setattr(safety, "DEFAULT_SAFETY", safe)
    monkeypatch.setattr(safety, "load_gate", lambda *a, **k: real_load(safe, tmp_path / "KILL"))
    monkeypatch.setattr(safety, "save_max_submissions", lambda n, path=safe: real_save(n, safe))
    monkeypatch.setattr("applicationbot.pipeline.effective_min_fit", lambda f: (f.min_fit, None))
    _loaded["f"] = filters_mod.DiscoveryFilters()
    return NS(safety=safe, get=lambda: _loaded["f"])


_loaded: dict = {}


def test_defaults_are_todays_behaviour(config):
    s = web.loop_settings()
    assert (s["tailor_mode"], s["tailor_below_fit"], s["reuse_threshold"]) == ("smart", 70, 0.9)
    assert s["min_fit"] == 50 and s["max_submissions_per_run"] == 10


def test_saving_writes_both_files_and_returns_what_landed(config):
    out = web.save_loop_settings({"tailor_mode": "under", "tailor_below_fit": 80, "min_fit": 65,
                                  "reuse_threshold": 0.5, "max_submissions_per_run": 3})
    assert out["ok"] is True
    assert (out["tailor_mode"], out["tailor_below_fit"]) == ("under", 80)
    assert out["min_fit"] == 65 and out["reuse_threshold"] == 0.5
    assert out["max_submissions_per_run"] == 3
    assert config.get().tailor_mode == "under"           # persisted in the filters object
    assert safety.load_gate(config.safety).max_submissions_per_run == 3


def test_out_of_range_values_are_clamped_not_rejected(config):
    out = web.save_loop_settings({"min_fit": 400, "reuse_threshold": 7, "tailor_below_fit": -5})
    assert out["min_fit"] == 100 and out["reuse_threshold"] == 1.0 and out["tailor_below_fit"] == 0


def test_an_unknown_mode_is_refused_with_the_reason(config):
    out = web.save_loop_settings({"tailor_mode": "banana"})
    assert out["ok"] is False and "banana" in out["error"]
    assert config.get().tailor_mode == "smart"           # nothing was written


def test_editing_the_cap_never_arms_or_disarms(config):
    web.save_loop_settings({"max_submissions_per_run": 2})
    gate = safety.load_gate(config.safety)
    assert gate.armed is True                            # the file said armed: true — untouched
    assert gate.max_submissions_per_run == 2
    import yaml
    assert yaml.safe_load(config.safety.read_text())["nav_agentic"] is True   # and so is everything else


def test_a_missing_safety_file_is_created_disarmed(tmp_path):
    p = tmp_path / "new.yaml"
    safety.save_max_submissions(5, p)
    gate = safety.load_gate(p, tmp_path / "KILL")
    assert gate.armed is False and gate.max_submissions_per_run == 5


# --- the cap stops a run --------------------------------------------------------------------

@pytest.fixture
def loop_state():
    with web._LOOP_LOCK:
        web._LOOP_STATE.clear()
        web._LOOP_STATE.update(web._loop_reset())
    web._LOOP_STOP.clear()
    yield
    web._LOOP_STOP.clear()
    with web._LOOP_LOCK:
        web._LOOP_STATE["running"] = False


def test_the_cap_stops_the_run_instead_of_submitting(loop_state, monkeypatch):
    sent = []
    monkeypatch.setattr(web, "_armed_submit",
                        lambda app_id, note, **kw: (sent.append(app_id), ("ok", True))[1])
    web._loop_set(cap=2)

    web._loop_submit(11)
    web._loop_submit(12)
    assert sent == [11, 12] and web._LOOP_STATE["submitted"] == 2

    web._loop_submit(13)                     # over the cap
    assert sent == [11, 12]                  # not sent
    assert web._LOOP_STATE["cap_hit"] is True
    assert web._LOOP_STOP.is_set()           # and the run ends rather than preparing more
    assert "cap reached" in web._LOOP_STATE["message"]
    assert "Loop settings" in web._LOOP_STATE["message"]   # names where to raise it


def test_no_cap_means_no_ceiling(loop_state, monkeypatch):
    sent = []
    monkeypatch.setattr(web, "_armed_submit",
                        lambda app_id, note, **kw: (sent.append(app_id), ("ok", True))[1])
    web._loop_set(cap=0)
    for i in range(5):
        web._loop_submit(i)
    assert sent == [0, 1, 2, 3, 4] and not web._LOOP_STOP.is_set()

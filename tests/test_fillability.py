"""Fillability-gate tests (decision 035): non-fillable portals never reach the matcher.

Run:  python -m tests.test_fillability   (also pytest-compatible)
"""
from __future__ import annotations

from applicationbot.discovery import Posting
from applicationbot.pipeline import _is_fillable


def _p(ats: str, **extra) -> Posting:
    return Posting(company="Acme", title="SWE", body="jd", url=f"https://x/{ats}",
                   ats=ats, extra=dict(extra))


def test_public_api_atss_are_fillable():
    for ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee", "workable"):
        assert _is_fillable(_p(ats)) is True, ats


def test_workday_is_now_fillable():
    # The Workday deterministic adapter (decision 059) makes Workday fillable (M1 dry-run).
    assert _is_fillable(_p("workday")) is True


def test_remaining_account_gated_portals_are_not():
    assert _is_fillable(_p("icims")) is False


def test_unbridged_aggregator_passes_through():
    # bridge=False runs keep today's behaviour: the redirect may land on a fillable ATS.
    assert _is_fillable(_p("adzuna")) is True


def test_bridge_marked_unresolvable_is_gated():
    assert _is_fillable(_p("adzuna", auto_applyable=False)) is False
    # bridged onto workday: ats rewritten + auto_applyable False
    assert _is_fillable(_p("workday", auto_applyable=False, bridged_from="adzuna")) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} fillability test(s) passed.")

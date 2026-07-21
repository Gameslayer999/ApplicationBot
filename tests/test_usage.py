"""Token accounting (decision 095): capture, attribution, and roll-up.

Uses synthetic Claude CLI envelopes (the exact shape `--output-format json` returns) so the
whole record → aggregate → cascade path is exercised with zero real tokens.
"""
from __future__ import annotations

import pytest

from applicationbot import tracker, usage


def _envelope(inp: int, out: int, *, cache_read: int = 0, cost: float = 0.0,
              model: str = "claude-opus-4-8[1m]") -> dict:
    """A minimal copy of the Claude CLI `--output-format json` envelope."""
    return {
        "type": "result", "result": "ok", "total_cost_usd": cost,
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": 0},
        "modelUsage": {model: {"inputTokens": inp, "outputTokens": out}},
    }


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Point token recording at a throwaway DB (usage.record reads DEFAULT_DB at call time)."""
    p = tmp_path / "applications.db"
    monkeypatch.setattr(tracker, "DEFAULT_DB", p)
    return p


def test_call_under_for_posting_is_attributed_by_activity(db):
    with usage.for_posting("https://job/1"):
        usage.record(_envelope(1000, 200, cost=0.01), activity="tailoring")
        usage.record(_envelope(300, 50, cost=0.002))  # inherits ambient (unset → "other")
    with usage.for_posting("https://job/1", activity="form-entry"):
        usage.record(_envelope(400, 80, cost=0.003))  # inherits block default → form-entry

    by_app = tracker.usage_by_application(path=db)
    assert set(by_app) == {"https://job/1"}
    row = by_app["https://job/1"]
    assert row["input_tokens"] == 1700
    assert row["output_tokens"] == 330
    assert row["total_tokens"] == 2030
    assert row["calls"] == 3
    assert row["cost_usd"] == pytest.approx(0.015)
    assert set(row["by_activity"]) == {"tailoring", "other", "form-entry"}
    assert row["by_activity"]["tailoring"]["output_tokens"] == 200
    assert row["by_activity"]["form-entry"]["total_tokens"] == 480


def test_call_outside_for_posting_is_discovery(db):
    # A batched judge call during discovery — no posting context.
    usage.record(_envelope(5000, 400, cost=0.05), activity="judging")
    assert tracker.usage_by_application(path=db) == {}
    disc = tracker.usage_discovery_summary(path=db)
    assert disc["input_tokens"] == 5000
    assert disc["total_tokens"] == 5400
    assert set(disc["by_activity"]) == {"judging"}


def test_explicit_activity_overrides_block_default(db):
    with usage.for_posting("u", activity="form-entry"):
        usage.record(_envelope(10, 5), activity="tailoring")
    by_act = tracker.usage_by_application(path=db)["u"]["by_activity"]
    assert set(by_act) == {"tailoring"}


def test_envelope_without_usage_records_nothing(db):
    with usage.for_posting("u"):
        usage.record({"type": "result", "result": "hi"})  # no usage block
        usage.record(_envelope(0, 0))                      # all-zero counts
    assert tracker.usage_by_application(path=db) == {}


def test_workday_agent_stream_json_usage_is_metered(db):
    """The Workday agentic worker emits stream-json (one JSON object per line, ending in a result
    object with cumulative usage). `_record_agent_usage` should pull that and attribute it to the
    posting under form-entry (decision 095)."""
    import json as _json

    from applicationbot import workday
    stdout = "\n".join([
        _json.dumps({"type": "system", "subtype": "init"}),
        _json.dumps({"type": "assistant", "message": {"content": "filling…"}}),
        _json.dumps({"type": "result", "subtype": "success", "result": "done",
                     "total_cost_usd": 0.09,
                     "usage": {"input_tokens": 6000, "output_tokens": 700,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                     "modelUsage": {"claude-sonnet-5": {"inputTokens": 6000, "outputTokens": 700}}}),
    ])
    with usage.for_posting("https://wd/1"):
        workday._record_agent_usage(stdout)

    by_app = tracker.usage_by_application(path=db)
    assert set(by_app) == {"https://wd/1"}
    assert by_app["https://wd/1"]["total_tokens"] == 6700
    assert set(by_app["https://wd/1"]["by_activity"]) == {"form-entry"}


def test_workday_agent_usage_no_result_line_is_noop(db):
    from applicationbot import workday
    with usage.for_posting("https://wd/1"):
        workday._record_agent_usage("not json\n{\"type\":\"assistant\"}\n")  # no usage block
    assert tracker.usage_by_application(path=db) == {}


def test_delete_application_cascades_usage(db):
    app_id = tracker.add_application(
        {"company": "Acme", "status": "dry-run", "source_url": "https://job/x"}, path=db)
    with usage.for_posting("https://job/x"):
        usage.record(_envelope(100, 20), activity="tailoring")
    assert "https://job/x" in tracker.usage_by_application(path=db)

    tracker.delete_application(app_id, path=db)
    assert tracker.usage_by_application(path=db) == {}

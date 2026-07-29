"""Web routes for the source-scout candidates panel (decision 134).

GET /candidates surfaces only VALIDATED, not-already-configured boards; POST /candidates/accept
wires one into discovery.yaml via source_scout.merge_into_filters. Uses the same live-server
harness as test_web_csrf so the CSRF guard + JSON shapes are exercised for real.

Run:  python -m pytest tests/test_web_candidates.py -q
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from applicationbot import filters as filters_mod
from applicationbot import source_scout, web
from applicationbot.filters import Board, DiscoveryFilters


def _server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
        return r.status, json.loads(r.read())


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_get_candidates_returns_only_validated_and_unconfigured(monkeypatch):
    monkeypatch.setattr(filters_mod, "load_filters",
                        lambda *a, **k: DiscoveryFilters(boards=[Board(ats="greenhouse", token="stripe")]))
    monkeypatch.setattr(source_scout, "load_candidates", lambda *a, **k: [
        source_scout.Candidate(ats="greenhouse", token="stripe", validated=True, n_postings=9),   # already configured
        source_scout.Candidate(ats="lever", token="netflix", validated=True, n_postings=5,
                               sample_title="Backend Engineer"),                                   # surfaced
        source_scout.Candidate(ats="ashby", token="dead", validated=False, error="404"),          # unvalidated
    ])
    srv, port = _server()
    try:
        status, body = _get(port, "/candidates")
        assert status == 200
        assert [(c["ats"], c["token"]) for c in body["candidates"]] == [("lever", "netflix")]
        assert body["candidates"][0]["sample_title"] == "Backend Engineer"
    finally:
        srv.shutdown()


def test_post_accept_wires_the_board_and_reports_added(monkeypatch):
    calls = []
    monkeypatch.setattr(source_scout, "merge_into_filters",
                        lambda ats, token, **k: calls.append((ats, token)) or True)
    srv, port = _server()
    try:
        status, body = _post(port, "/candidates/accept", {"ats": "lever", "token": "netflix"})
        assert status == 200 and body == {"ok": True, "added": True}
        assert calls == [("lever", "netflix")]
    finally:
        srv.shutdown()


def test_post_accept_rejects_missing_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(source_scout, "merge_into_filters", lambda *a, **k: calls.append(1) or True)
    srv, port = _server()
    try:
        status, body = _post(port, "/candidates/accept", {"ats": "lever", "token": "  "})
        assert status == 400 and "required" in body["error"]
        assert calls == []  # never reached the wiring
    finally:
        srv.shutdown()


def test_get_candidates_returns_unenabled_specs(monkeypatch):
    monkeypatch.setattr(filters_mod, "load_filters",
                        lambda *a, **k: DiscoveryFilters(json_aggregators=["already-on"]))
    monkeypatch.setattr(source_scout, "load_candidates", lambda *a, **k: [])
    monkeypatch.setattr(source_scout, "load_registry_specs", lambda *a, **k: [
        {"name": "remotive", "endpoint": "https://x/{q}", "n_postings": 12, "sample_title": "SRE"},
        {"name": "already-on", "endpoint": "https://y"},   # enabled → filtered out
    ])
    srv, port = _server()
    try:
        status, body = _get(port, "/candidates")
        assert status == 200
        assert [s["name"] for s in body["specs"]] == ["remotive"]
        assert body["specs"][0]["n_postings"] == 12
    finally:
        srv.shutdown()


def test_post_accept_spec_enables_by_name(monkeypatch):
    calls = []
    monkeypatch.setattr(source_scout, "enable_json_aggregator",
                        lambda name, **k: calls.append(name) or True)
    srv, port = _server()
    try:
        status, body = _post(port, "/candidates/accept-spec", {"name": "remotive"})
        assert status == 200 and body == {"ok": True, "added": True}
        assert calls == ["remotive"]
        status, body = _post(port, "/candidates/accept-spec", {"name": "  "})
        assert status == 400 and "required" in body["error"]
    finally:
        srv.shutdown()


def test_get_candidates_lists_unenabled_contrib_adapters(monkeypatch):
    import applicationbot.sources_contrib as sc

    class _Mod:
        DESCRIPTION = "custom HTML board"

    monkeypatch.setattr(filters_mod, "load_filters",
                        lambda *a, **k: DiscoveryFilters(contrib_sources=["already-on"]))
    monkeypatch.setattr(source_scout, "load_candidates", lambda *a, **k: [])
    monkeypatch.setattr(source_scout, "load_registry_specs", lambda *a, **k: [])
    monkeypatch.setattr(sc, "load_contrib_sources",
                        lambda *a, **k: {"quirky": _Mod(), "already-on": _Mod()})
    srv, port = _server()
    try:
        status, body = _get(port, "/candidates")
        assert status == 200
        assert [c["name"] for c in body["contrib"]] == ["quirky"]  # enabled one filtered out
        assert body["contrib"][0]["description"] == "custom HTML board"
    finally:
        srv.shutdown()


def test_post_accept_contrib_enables_by_name(monkeypatch):
    calls = []
    monkeypatch.setattr(source_scout, "enable_contrib_source",
                        lambda name, **k: calls.append(name) or True)
    srv, port = _server()
    try:
        status, body = _post(port, "/candidates/accept-contrib", {"name": "quirky"})
        assert status == 200 and body == {"ok": True, "added": True}
        assert calls == ["quirky"]
        status, body = _post(port, "/candidates/accept-contrib", {"name": ""})
        assert status == 400 and "required" in body["error"]
    finally:
        srv.shutdown()

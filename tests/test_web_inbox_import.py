"""Web routes for the Track-tab inbox import (decision 151).

POST /track/import-inbox runs the import and returns the full summary the UI renders;
/track/import-undo reverses one run; /track/enable-email-alerts is the one-click fix offered when
the import finds forwarded job-alert emails discovery isn't using. Same live-server harness as
test_web_candidates, so the CSRF guard and JSON shapes are exercised for real. The importer
itself is stubbed — its behaviour is covered in test_inbox_import.py.

Run:  python -m pytest tests/test_web_inbox_import.py -q
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from applicationbot import filters as filters_mod
from applicationbot import inbox_import, web
from applicationbot.filters import DiscoveryFilters


def _server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_import_route_returns_the_summary_the_ui_renders(monkeypatch):
    summary = {**inbox_import._summary(), "scanned": 9, "examined": 3, "created": [4, 5],
               "updated": [2], "alerts": {"linkedin": 6}, "alerts_enabled": False,
               "run_id": "2026-07-29T10:00:00", "message": "scanned 9 email(s), 2 new application(s)."}
    monkeypatch.setattr(inbox_import, "run_import", lambda **k: summary)
    srv, port = _server()
    try:
        status, body = _post(port, "/track/import-inbox", {})
        assert status == 200 and body["ok"] is True
        assert body["created"] == [4, 5] and body["updated"] == [2]
        assert body["alerts"] == {"linkedin": 6} and body["run_id"] == "2026-07-29T10:00:00"
    finally:
        srv.shutdown()


def test_import_route_reports_not_ok_when_nothing_landed(monkeypatch):
    failed = {**inbox_import._summary(),
              "errors": ["No inbox is linked, so there is nothing to import. Link one on the "
                         "Profile tab (Connect with Google, or an IMAP app password)."]}
    failed["message"] = failed["errors"][0]
    monkeypatch.setattr(inbox_import, "run_import", lambda **k: failed)
    srv, port = _server()
    try:
        status, body = _post(port, "/track/import-inbox", {})
        assert status == 200 and body["ok"] is False
        assert "Profile tab" in body["message"]  # names the blocker AND the fix (Principle #3)
    finally:
        srv.shutdown()


def test_undo_route_passes_the_run_id_through(monkeypatch):
    seen = {}

    def fake_undo(run_id, **k):
        seen["run_id"] = run_id
        return {"deleted": 2, "restored": 1, "missing": 0}

    monkeypatch.setattr(inbox_import, "undo_run", fake_undo)
    srv, port = _server()
    try:
        status, body = _post(port, "/track/import-undo", {"run_id": "R1"})
        assert status == 200 and seen["run_id"] == "R1"
        assert body["deleted"] == 2 and body["restored"] == 1
    finally:
        srv.shutdown()


def test_enable_email_alerts_turns_on_exactly_the_providers_found(monkeypatch):
    saved = {}
    monkeypatch.setattr(filters_mod, "load_filters", lambda *a, **k: DiscoveryFilters())
    monkeypatch.setattr(filters_mod, "save_filters", lambda f, *a, **k: saved.update(f=f))
    srv, port = _server()
    try:
        status, body = _post(port, "/track/enable-email-alerts", {"providers": ["linkedin"]})
        assert status == 200 and body["providers"] == ["linkedin"]
        assert saved["f"].email_alerts.enabled is True
        assert saved["f"].email_alerts.providers == ["linkedin"]
    finally:
        srv.shutdown()


def test_import_is_blocked_cross_origin(monkeypatch):
    monkeypatch.setattr(inbox_import, "run_import", lambda **k: inbox_import._summary())
    srv, port = _server()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/track/import-inbox", data=b"{}",
                                     headers={"Content-Type": "application/json",
                                              "Origin": "https://evil.example"}, method="POST")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("cross-origin POST should be rejected")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.shutdown()

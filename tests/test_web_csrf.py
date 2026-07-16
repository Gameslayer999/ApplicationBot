"""CSRF/origin guard on state-changing POSTs (decision 062).

Every POST to the localhost UI is state-changing (saves, submits, launches a browser). The
`do_POST` origin guard rejects a cross-origin request so a page on another site the user has
open can't drive the server; same-origin (loopback Origin, or none) passes, and GETs (read-only)
are intentionally unguarded.

Run:  python -m pytest tests/test_web_csrf.py -q
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from applicationbot import web


def _server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


def test_cross_origin_post_is_blocked_before_the_handler_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(web.tracker, "add_application", lambda *a, **k: calls.append(1) or 1)
    srv, port = _server()
    try:
        # Cross-origin → 403, and the handler (a real DB write) never runs.
        assert _post(port, "/track/add", {"data": {}}, {"Origin": "https://evil.example.com"}) == 403
        assert calls == []
        # A cross-site Referer is likewise rejected.
        assert _post(port, "/track/add", {"data": {}}, {"Referer": "https://evil.example.com/x"}) == 403
        assert calls == []
        # Same-origin (no Origin, as a curl/CLI or same-origin fetch sends) reaches the handler.
        assert _post(port, "/track/add", {"data": {}}, {}) == 200
        assert calls == [1]
        # Loopback Origin also passes.
        assert _post(port, "/track/add", {"data": {}}, {"Origin": f"http://127.0.0.1:{port}"}) == 200
        assert calls == [1, 1]
    finally:
        srv.shutdown()


def test_get_requests_are_not_origin_guarded():
    # GETs are read-only, so a cross-origin GET still works (only POSTs are state-changing).
    srv, port = _server()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/test-run/status",
                                     headers={"Origin": "https://evil.example.com"})
        assert urllib.request.urlopen(req).status == 200
    finally:
        srv.shutdown()

"""CAPTCHA auto-solve tests (decision 049) — offline, no CapSolver calls, no browser.

The gates are the point: the hook only solves when enabled AND the site is allowlisted AND a
key is present; every other path returns (False, actionable-reason) instead of submitting. The
CapSolver client is exercised against a stub `_post`/`_sleep`, and detection/injection against a
fake Playwright frame.

Run:  python -m tests.test_captcha   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot import captcha
from applicationbot.captcha import CaptchaConfig, Challenge


class _FakeFrame:
    """Stands in for a Playwright frame: `evaluate` returns queued detect/inject results."""
    def __init__(self, detect_result=None, inject_result=True, url="https://boards.greenhouse.io/acme/jobs/1"):
        self._detect = detect_result
        self._inject = inject_result
        self.url = url
        self.injected = None

    def evaluate(self, js, arg=None):
        if "kind" in js and arg is not None:  # the inject call passes {kind, token}
            self.injected = arg
            return self._inject
        return self._detect  # the detect call


_CH = {"kind": "recaptcha_v2", "sitekey": "6Lc-abc"}


def test_config_load_and_default():
    d = Path(tempfile.mkdtemp())
    p = d / "safety.yaml"
    p.write_text("armed: true\ncaptcha:\n  enabled: true\n  sites: [Greenhouse.io, ashbyhq.com]\n")
    cfg = captcha.load_config(p)
    assert cfg.enabled and cfg.sites == ["greenhouse.io", "ashbyhq.com"]
    # missing file / missing block => disabled
    assert captcha.load_config(d / "nope.yaml").enabled is False
    (d / "bare.yaml").write_text("armed: false\n")
    assert captcha.load_config(d / "bare.yaml").enabled is False


def test_site_allowed_suffix_match():
    cfg = CaptchaConfig(enabled=True, sites=["greenhouse.io"])
    assert captcha.site_allowed("https://boards.greenhouse.io/x", cfg) is True
    assert captcha.site_allowed("https://greenhouse.io/x", cfg) is True
    assert captcha.site_allowed("https://evil-greenhouse.io/x", cfg) is False
    assert captcha.site_allowed("https://lever.co/x", cfg) is False


def test_detect_none_when_no_widget():
    assert captcha.detect(_FakeFrame(detect_result=None)) is None
    assert captcha.detect(_FakeFrame(detect_result={"kind": "unknown", "sitekey": "k"})) is None


def test_detect_returns_challenge():
    ch = captcha.detect(_FakeFrame(detect_result=_CH))
    assert ch and ch.kind == "recaptcha_v2" and ch.sitekey == "6Lc-abc"


def test_hook_no_captcha_proceeds():
    hook = captcha.build_submit_hook(CaptchaConfig(enabled=True, sites=["greenhouse.io"]),
                                     "https://boards.greenhouse.io/acme/jobs/1",
                                     api_key="k", log=lambda m: None)
    handled, detail = hook(_FakeFrame(detect_result=None))
    assert handled is True and detail == ""


def test_hook_blocks_when_disabled():
    hook = captcha.build_submit_hook(CaptchaConfig(enabled=False),
                                     "https://boards.greenhouse.io/acme/jobs/1",
                                     api_key="k", log=lambda m: None)
    handled, detail = hook(_FakeFrame(detect_result=_CH))
    assert handled is False and "OFF" in detail and "safety.yaml" in detail


def test_hook_blocks_when_site_not_allowlisted():
    hook = captcha.build_submit_hook(CaptchaConfig(enabled=True, sites=["lever.co"]),
                                     "https://boards.greenhouse.io/acme/jobs/1",
                                     api_key="k", log=lambda m: None)
    handled, detail = hook(_FakeFrame(detect_result=_CH))
    assert handled is False and "not in `captcha.sites`" in detail


def test_hook_blocks_when_no_key():
    hook = captcha.build_submit_hook(CaptchaConfig(enabled=True, sites=["greenhouse.io"]),
                                     "https://boards.greenhouse.io/acme/jobs/1",
                                     api_key="", log=lambda m: None)
    handled, detail = hook(_FakeFrame(detect_result=_CH))
    assert handled is False and "CAPSOLVER_API_KEY" in detail


def test_hook_solves_and_injects_when_all_gates_pass():
    frame = _FakeFrame(detect_result=_CH, inject_result=True)
    hook = captcha.build_submit_hook(
        CaptchaConfig(enabled=True, sites=["greenhouse.io"]),
        "https://boards.greenhouse.io/acme/jobs/1", api_key="k", log=lambda m: None,
        _solve=lambda ch, api_key: "SOLVED-TOKEN",
    )
    handled, detail = hook(frame)
    assert handled is True and detail == ""
    assert frame.injected == {"kind": "recaptcha_v2", "token": "SOLVED-TOKEN"}


def test_hook_blocks_on_solver_error():
    def boom(ch, api_key):
        raise captcha.CapSolverError("insufficient balance")
    hook = captcha.build_submit_hook(
        CaptchaConfig(enabled=True, sites=["greenhouse.io"]),
        "https://boards.greenhouse.io/acme/jobs/1", api_key="k", log=lambda m: None, _solve=boom,
    )
    handled, detail = hook(_FakeFrame(detect_result=_CH))
    assert handled is False and "insufficient balance" in detail


def test_capsolver_client_polls_to_ready():
    posts = []

    def fake_post(path, payload, **kw):
        posts.append(path)
        if path == "/createTask":
            return {"errorId": 0, "taskId": "t1"}
        # first poll processing, second ready
        return ({"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "TOK"}}
                if posts.count("/getTaskResult") >= 2
                else {"errorId": 0, "status": "processing"})

    token = captcha.solve(Challenge("recaptcha_v2", "k", "https://x"), api_key="key",
                          _post=fake_post, _sleep=lambda s: None)
    assert token == "TOK" and posts[0] == "/createTask" and posts.count("/getTaskResult") == 2


def test_capsolver_client_raises_on_error():
    def fake_post(path, payload, **kw):
        return {"errorId": 1, "errorDescription": "ERROR_KEY_DENIED"}
    try:
        captcha.solve(Challenge("hcaptcha", "k", "https://x"), api_key="key",
                      _post=fake_post, _sleep=lambda s: None)
        assert False, "expected CapSolverError"
    except captcha.CapSolverError as e:
        assert "ERROR_KEY_DENIED" in str(e)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

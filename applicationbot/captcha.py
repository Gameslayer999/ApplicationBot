"""CAPTCHA auto-solving for the armed submit path (decision 049) — CapSolver-backed.

⚠ Compliance note (Agent Guideline #4). Solving a CAPTCHA to submit a form circumvents a
site's anti-bot control and may breach that site's terms of service. This is built at the
user's explicit direction for their **own** job applications (personal use), and is fenced so
it can't run silently or broadly:

  • **Off by default.** `captcha.enabled` in profile/safety.yaml must be set true.
  • **Per-site opt-in.** Only domains listed in `captcha.sites` are ever auto-solved.
  • **Armed-only.** The one caller is `apply._attempt_submit`, reached only when the
    SafetyGate is armed (dry-run never submits, so it never solves — Guideline #3).
  • **Key from the environment**, never YAML: `CAPSOLVER_API_KEY` (Guideline #12, secrets).
  • **Every attempt logged** (site, type, outcome) via the injected `log`.

If any gate is unmet the caller records a `blocked` outcome with an actionable reason — it
never falls back to submitting a form the site is trying to protect.

Flow: `build_submit_hook(config, url)` returns `hook(frame) -> (handled, detail)`. `handled`
True means "no CAPTCHA in the way, or one was solved — proceed"; False means "a CAPTCHA is
blocking and we did not solve it — block with `detail`". Detection/injection run in the page
via Playwright `frame.evaluate`; the solve goes through CapSolver's REST API (urllib, no new
dependency). Supported: reCAPTCHA v2, hCaptcha, Cloudflare Turnstile (proxyless tasks).
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .safety import DEFAULT_SAFETY

_CAPSOLVER_ENDPOINT = "https://api.capsolver.com"
_TASK_TYPE = {
    "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
    "hcaptcha": "HCaptchaTaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
}


@dataclass
class Challenge:
    kind: str      # recaptcha_v2 | hcaptcha | turnstile
    sitekey: str
    url: str


@dataclass
class CaptchaConfig:
    enabled: bool = False
    sites: list[str] = field(default_factory=list)  # domain allowlist (host suffix match)


class CapSolverError(RuntimeError):
    """A CapSolver API call failed or timed out. Surfaced as a blocked reason, never fatal."""


def load_config(path: str | Path = DEFAULT_SAFETY) -> CaptchaConfig:
    """Read the `captcha:` block from profile/safety.yaml (co-located with arming — it is a
    submission-safety setting). Missing/unreadable ⇒ disabled (the safe default)."""
    import yaml

    p = Path(path)
    if not p.exists():
        return CaptchaConfig()
    try:
        data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("captcha") or {}
    except Exception:
        return CaptchaConfig()
    if not isinstance(data, dict):
        return CaptchaConfig()
    sites = data.get("sites") or []
    return CaptchaConfig(
        enabled=bool(data.get("enabled", False)),
        sites=[str(s).strip().lower() for s in sites if str(s).strip()],
    )


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


def site_allowed(url: str, config: CaptchaConfig) -> bool:
    """True if the URL's host is (a suffix of) an allowlisted domain — so `greenhouse.io`
    covers `boards.greenhouse.io`. Empty allowlist allows nothing."""
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in config.sites)


# --------------------------------------------------------------------------- detection

# Finds the first supported CAPTCHA widget and its sitekey. Checks explicit data-sitekey
# containers first, then falls back to reading the sitekey off the widget iframe's src.
_DETECT_JS = r"""() => {
  const pick = (sel, kind) => {
    const el = document.querySelector(sel);
    const k = el && (el.getAttribute('data-sitekey') || el.dataset.sitekey);
    return k ? {kind, sitekey: k} : null;
  };
  let r = pick('.g-recaptcha[data-sitekey]', 'recaptcha_v2')
       || pick('.h-captcha[data-sitekey]', 'hcaptcha')
       || pick('.cf-turnstile[data-sitekey]', 'turnstile');
  if (r) return r;
  for (const f of document.querySelectorAll('iframe[src]')) {
    const src = f.getAttribute('src') || '';
    let kind = null;
    if (src.includes('recaptcha')) kind = 'recaptcha_v2';
    else if (src.includes('hcaptcha')) kind = 'hcaptcha';
    else if (src.includes('turnstile') || src.includes('challenges.cloudflare')) kind = 'turnstile';
    if (!kind) continue;
    const m = src.match(/[?&]k=([^&]+)/) || src.match(/[?&]sitekey=([^&]+)/);
    if (m) return {kind, sitekey: decodeURIComponent(m[1])};
  }
  return null;
}"""


def detect(frame) -> Optional[Challenge]:
    """The CAPTCHA on the current form frame, or None. Never raises."""
    try:
        d = frame.evaluate(_DETECT_JS)
    except Exception:
        return None
    if not d or not d.get("sitekey") or d.get("kind") not in _TASK_TYPE:
        return None
    try:
        url = frame.url
    except Exception:
        url = ""
    return Challenge(kind=d["kind"], sitekey=d["sitekey"], url=url)


# --------------------------------------------------------------------------- injection

# Writes the solved token into the response field(s) the form reads on submit. Best-effort:
# sets the standard textarea/input by name and fires input/change so listeners notice.
_INJECT_JS = r"""(args) => {
  const {kind, token} = args;
  const names = kind === 'turnstile' ? ['cf-turnstile-response']
              : kind === 'hcaptcha' ? ['h-captcha-response', 'g-recaptcha-response']
              : ['g-recaptcha-response'];
  let set = false;
  for (const name of names) {
    let els = document.getElementsByName(name);
    if (!els.length) {
      const ta = document.createElement('textarea');
      ta.name = name; ta.style.display = 'none';
      (document.forms[0] || document.body).appendChild(ta);
      els = [ta];
    }
    for (const el of els) {
      el.value = token;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      set = true;
    }
  }
  return set;
}"""


def inject_token(frame, challenge: Challenge, token: str) -> bool:
    try:
        return bool(frame.evaluate(_INJECT_JS, {"kind": challenge.kind, "token": token}))
    except Exception:
        return False


# --------------------------------------------------------------------------- CapSolver API

def _post(path: str, payload: dict, *, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _CAPSOLVER_ENDPOINT + path, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        raise CapSolverError(f"CapSolver request to {path} failed: {type(e).__name__}: {e}") from e


def solve(challenge: Challenge, *, api_key: str, timeout: int = 120, poll: float = 3.0,
          _post=_post, _sleep=time.sleep) -> str:
    """Solve `challenge` via CapSolver and return the response token. Raises CapSolverError on
    an API error or if the task doesn't finish within `timeout`. `_post`/`_sleep` injectable."""
    task = {"type": _TASK_TYPE[challenge.kind], "websiteURL": challenge.url,
            "websiteKey": challenge.sitekey}
    created = _post("/createTask", {"clientKey": api_key, "task": task})
    if created.get("errorId"):
        raise CapSolverError(created.get("errorDescription") or created.get("errorCode") or "createTask error")
    task_id = created.get("taskId")
    if not task_id:
        raise CapSolverError("CapSolver createTask returned no taskId")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _sleep(poll)
        res = _post("/getTaskResult", {"clientKey": api_key, "taskId": task_id})
        if res.get("errorId"):
            raise CapSolverError(res.get("errorDescription") or res.get("errorCode") or "getTaskResult error")
        if res.get("status") == "ready":
            sol = res.get("solution") or {}
            token = sol.get("gRecaptchaResponse") or sol.get("token")
            if not token:
                raise CapSolverError("CapSolver returned a ready task with no token")
            return token
    raise CapSolverError(f"CapSolver did not solve within {timeout}s")


# --------------------------------------------------------------------------- gated hook

SubmitHook = Callable[[object], "tuple[bool, str]"]


def build_submit_hook(config: CaptchaConfig, url: str, *, api_key: Optional[str] = None,
                      log: Callable[[str], None] = print, _solve=solve) -> SubmitHook:
    """Return the gated pre-submit hook. Only the armed submit path calls it (Guideline #3),
    so `hook(frame)` need not re-check arming — it enforces the CapSolver-specific gates:
    enabled, site allowlisted, key present. Returns (handled, detail) per the module docstring."""
    api_key = api_key if api_key is not None else os.environ.get("CAPSOLVER_API_KEY", "")

    def hook(frame) -> "tuple[bool, str]":
        challenge = detect(frame)
        if challenge is None:
            return True, ""  # no CAPTCHA in the way
        host = _host(url)
        if not config.enabled:
            return False, ("CAPTCHA present; auto-solving is OFF. Set `captcha.enabled: true` in "
                           "profile/safety.yaml and CAPSOLVER_API_KEY, or solve it manually.")
        if not site_allowed(url, config):
            return False, (f"CAPTCHA present; {host} is not in `captcha.sites` in "
                           "profile/safety.yaml — add it to allow auto-solving this site.")
        if not api_key:
            return False, "CAPTCHA present; CAPSOLVER_API_KEY is not set in the environment."
        log(f"  CAPTCHA ({challenge.kind}) on {host} — solving via CapSolver…")
        try:
            token = _solve(challenge, api_key=api_key)
        except CapSolverError as e:
            log(f"  CAPTCHA solve failed: {e}")
            return False, f"CAPTCHA solve failed: {e}"
        injected = inject_token(frame, challenge, token)
        log(f"  CAPTCHA solved ({challenge.kind}); token injected={injected}.")
        if not injected:
            return False, "CAPTCHA solved but the token could not be injected into the form."
        return True, ""

    return hook

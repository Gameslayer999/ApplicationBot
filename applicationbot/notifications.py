"""Push notifications for the auto-apply loop (decision 135).

The loop prepares each cleared match as a dry-run and then WAITS for the user to sign off
before it submits (autoloop.auto_apply_loop). It also parks applications that need human
intervention (parking.py: an unanswered question, a login, a CAPTCHA). Until now both
signals were visible only while a browser tab / the app window was open and being watched —
the loop could sit "ready for your approval" for an hour with no way to know.

This module pushes those two moments to the user wherever they are:

  * ``approval_needed``     — a new application is prepared and ready to review + submit.
  * ``intervention_needed`` — an application is blocked and needs the user to act.

Delivery is a small set of pluggable **channels**, each isolated so one failing never
breaks the loop or the other channels:

  * ``DesktopChannel`` — a native OS notification (macOS ``osascript`` today; other
    platforms degrade to a no-op). The web server runs on the user's own machine, so this
    one native notification covers BOTH the packaged desktop app and a localhost browser
    session — no per-surface code.
  * ``NtfyChannel``    — an HTTP POST to an ntfy.sh topic (stdlib urllib, no new dep), so
    the user's phone buzzes even with nothing open on the Mac.

Adding another destination later (Telegram, Pushover, a Slack webhook) is a new class that
implements ``Channel.send`` and a branch in ``build_notifier`` — nothing else changes.

Pure and fully injected where it matters: the ntfy channel takes its HTTP opener and the
desktop channel its command runner, so both are unit-testable with fakes and never touch
the network or shell in a test.
"""

from __future__ import annotations

import platform
import shutil
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

import yaml

from .paths import DATA_ROOT


def _ssl_context() -> Optional[ssl.SSLContext]:
    """A cert-verifying SSL context backed by certifi's CA bundle. macOS system Pythons and the
    frozen app often lack a usable system trust store (the classic "CERTIFICATE_VERIFY_FAILED"),
    so we point verification at certifi (shipped via the anthropic dep). Returns None if certifi
    is unavailable, letting urllib fall back to its default context."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None

DEFAULT_PATH = DATA_ROOT / "profile" / "notifications.yaml"

# The two moments the loop pushes on. Kept as bare strings (not an enum) to stay trivially
# serialisable in the config file and JSON payloads.
APPROVAL_NEEDED = "approval_needed"
INTERVENTION_NEEDED = "intervention_needed"
EVENTS = (APPROVAL_NEEDED, INTERVENTION_NEEDED)


@dataclass
class Notification:
    """One thing to tell the user. ``link`` is a path into the running app (e.g.
    ``/#discover``) so a channel that supports click-through can deep-link to the fix
    (UI Principle #3); channels that can't just include it in the body."""
    event: str
    title: str
    body: str
    link: str = ""
    urgent: bool = False  # intervention is more urgent than a routine approval prompt


class Channel(Protocol):
    name: str

    def send(self, note: Notification) -> None: ...


# --------------------------------------------------------------------------- desktop


def _osascript_script(title: str, body: str) -> str:
    """The AppleScript for one notification, with title/body escaped for a string literal."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    return f'display notification "{esc(body)}" with title "{esc(title)}"'


def _post_osascript(title: str, body: str) -> None:
    subprocess.run(["osascript", "-e", _osascript_script(title, body)],
                   check=True, capture_output=True, timeout=10)


def _post_native(title: str, body: str) -> None:
    """Post via the app process itself (pyobjc / NSUserNotification). The notification then
    carries whatever icon the running process's app bundle has — i.e. the ApplicationBot icon
    in the packaged app — which ``osascript`` can't do (it always shows Script Editor's icon,
    because the icon is fixed to the *posting* process). Raises if there's no app bundle to
    attach to (e.g. a plain ``python`` on localhost), so the caller can fall back to osascript."""
    from Foundation import NSUserNotification, NSUserNotificationCenter, NSThread
    center = NSUserNotificationCenter.defaultUserNotificationCenter()
    if center is None:
        raise RuntimeError("no default NSUserNotificationCenter — process is not an app bundle")

    def _deliver() -> None:
        n = NSUserNotification.alloc().init()
        n.setTitle_(title)
        n.setInformativeText_(body)
        center.deliverNotification_(n)

    # Deliver on the main thread — in the app the loop posts from a background server thread,
    # and the notification center wants the main run loop (which pywebview is running).
    if NSThread.isMainThread():
        _deliver()
    else:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_deliver)


def _run_terminal_notifier(cmd: list) -> None:
    subprocess.run(cmd, check=True, capture_output=True, timeout=10)


def _default_poster(title: str, body: str) -> None:
    """Post a macOS notification, preferring the native app-icon path in the packaged app.
    Only the frozen bundle has an app identity to inherit an icon from; a from-source /
    localhost run has no bundle, so it goes straight to osascript (generic icon, but reliable)."""
    if getattr(sys, "frozen", False):
        try:
            _post_native(title, body)
            return
        except Exception:
            pass  # any pyobjc/bundle issue → fall back to the always-available osascript path
    _post_osascript(title, body)


@dataclass
class DesktopChannel:
    """A native desktop notification on the Mac running the loop. macOS only (other platforms no-op
    for now — Windows/Linux can be added the same way).

    Clicking the notification should open the app on THIS Mac (localhost is reachable here, unlike
    from a phone). How that's achieved depends on the runtime:
      * Packaged app — posted in-process via pyobjc: shows the ApplicationBot icon, and clicking
        activates the app window (``_default_poster`` → ``_post_native``).
      * From source — the fallback is ``osascript``, whose notification opens *Script Editor* when
        clicked (it can't carry a click action), not the app. So if ``terminal-notifier`` is on
        PATH we use it with ``-open <url>`` for a genuinely clickable notification that opens the
        app; otherwise osascript posts an informational notification (the in-app Notifications tab
        is the place to act). See decision 138.

    ``link_base`` is the running server's localhost address; ``poster``/``which``/``runner`` are
    injected so tests never spawn a process."""
    name: str = "desktop"
    poster: Callable[[str, str], None] = _default_poster
    system: str = field(default_factory=platform.system)
    link_base: str = "http://127.0.0.1:8000"
    which: Callable[[str], Optional[str]] = shutil.which
    runner: Callable[[list], None] = _run_terminal_notifier

    def send(self, note: Notification) -> None:
        if self.system != "Darwin":
            return  # unsupported OS: degrade to nothing rather than error the loop
        url = f"{self.link_base.rstrip('/')}{note.link}" if note.link else ""
        # From-source runs post via osascript (click → Script Editor, not the app). If
        # terminal-notifier is installed, use it for a clickable notification that opens the app on
        # this Mac. The frozen app already posts a clickable native notification, so only reach for
        # terminal-notifier when not frozen.
        if url and not getattr(sys, "frozen", False):
            tn = self.which("terminal-notifier")
            if tn:
                try:
                    self.runner([tn, "-title", note.title, "-message", note.body, "-open", url])
                    return
                except Exception:
                    pass  # any terminal-notifier failure → fall back to the always-available poster
        self.poster(note.title, note.body)


# --------------------------------------------------------------------------- ntfy


def _urlopen(req: urllib.request.Request) -> object:
    return urllib.request.urlopen(req, timeout=10, context=_ssl_context())


@dataclass
class NtfyChannel:
    """Push to a phone via ntfy (https://ntfy.sh). A topic is a shared secret: anyone who
    knows it can read the alerts, so the user picks an unguessable one. The HTTP opener is
    injected for testing.

    No click/deep-link is attached: the loop runs on the user's Mac and the app is bound to
    localhost, which a phone on another device can't reach — a "Click → http://127.0.0.1:…"
    action just fails on the phone. So the phone notification is informational (its body says to
    open ApplicationBot on the Mac); the user acts on the Mac, in the Notifications tab. See
    decision 138."""
    topic: str
    name: str = "ntfy"
    server: str = "https://ntfy.sh"
    opener: Callable[[urllib.request.Request], object] = _urlopen

    def send(self, note: Notification) -> None:
        url = f"{self.server.rstrip('/')}/{self.topic}"
        headers = {
            "Title": note.title,
            "Priority": "high" if note.urgent else "default",
            "Tags": "warning" if note.urgent else "briefcase",
        }
        req = urllib.request.Request(
            url, data=note.body.encode("utf-8"), headers=headers, method="POST")
        self.opener(req)


# --------------------------------------------------------------------------- config


@dataclass
class NotifyConfig:
    desktop: bool = True
    ntfy_enabled: bool = False
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    # Per-event opt-out. Both on by default — the whole point is to be told.
    approval_needed: bool = True
    intervention_needed: bool = True

    def event_enabled(self, event: str) -> bool:
        return {APPROVAL_NEEDED: self.approval_needed,
                INTERVENTION_NEEDED: self.intervention_needed}.get(event, True)

    def to_dict(self) -> dict:
        return {
            "desktop": self.desktop,
            "ntfy": {"enabled": self.ntfy_enabled, "topic": self.ntfy_topic,
                     "server": self.ntfy_server},
            "events": {APPROVAL_NEEDED: self.approval_needed,
                       INTERVENTION_NEEDED: self.intervention_needed},
        }


def load_config(path: str | Path = DEFAULT_PATH) -> NotifyConfig:
    """Load the notification config. A missing/empty/unreadable file means the defaults:
    desktop on, ntfy off, both events on."""
    p = Path(path)
    data: dict = {}
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except Exception:
            data = {}
    ntfy = data.get("ntfy") or {}
    events = data.get("events") or {}
    return NotifyConfig(
        desktop=bool(data.get("desktop", True)),
        ntfy_enabled=bool(ntfy.get("enabled", False)),
        ntfy_topic=str(ntfy.get("topic", "") or "").strip(),
        ntfy_server=str(ntfy.get("server") or "https://ntfy.sh").strip(),
        approval_needed=bool(events.get(APPROVAL_NEEDED, True)),
        intervention_needed=bool(events.get(INTERVENTION_NEEDED, True)),
    )


def save_config(cfg: NotifyConfig, path: str | Path = DEFAULT_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg.to_dict(), sort_keys=False), encoding="utf-8")


def desktop_click_status(*, which: Callable[[str], Optional[str]] = shutil.which,
                         system: Optional[str] = None) -> dict:
    """Whether clicking a desktop notification can open the app — drives a Settings hint.

    - non-macOS: desktop notifications are a no-op, so click-through isn't applicable.
    - packaged app (frozen): the in-process native notification activates the app on click.
    - from source: clickable only if ``terminal-notifier`` is installed (osascript can't carry a
      click action, so without it a click just opens Script Editor). ``install`` is the one-liner
      the UI shows when the capability is missing. See decision 138.
    """
    system = system or platform.system()
    if system != "Darwin":
        return {"applicable": False, "clickable": False, "method": "none"}
    if getattr(sys, "frozen", False):
        return {"applicable": True, "clickable": True, "method": "native"}
    if which("terminal-notifier"):
        return {"applicable": True, "clickable": True, "method": "terminal-notifier"}
    return {"applicable": True, "clickable": False, "method": "none",
            "install": "brew install terminal-notifier"}


# --------------------------------------------------------------------------- notifier


@dataclass
class Notifier:
    """Fans a Notification out to every configured channel. Per-channel isolation: a
    channel that raises is swallowed (recorded via ``on_error``) so one broken destination
    never stops the loop or the other channels. ``notify`` is a no-op when the event is
    disabled or no channels are configured."""
    channels: list = field(default_factory=list)
    cfg: NotifyConfig = field(default_factory=NotifyConfig)
    on_error: Optional[Callable[[str, Exception], None]] = None

    def notify(self, note: Notification) -> None:
        if not self.cfg.event_enabled(note.event):
            return
        for ch in self.channels:
            try:
                ch.send(note)
            except Exception as e:  # noqa: BLE001 — a channel failure must never propagate
                if self.on_error:
                    self.on_error(getattr(ch, "name", "channel"), e)


def build_notifier(cfg: Optional[NotifyConfig] = None,
                   *, link_base: str = "http://127.0.0.1:8000",
                   on_error: Optional[Callable[[str, Exception], None]] = None) -> Notifier:
    """Assemble a Notifier from config. Only enabled+valid channels are included (ntfy needs
    a topic). ``link_base`` is the running server's address so ntfy click-through lands back
    in the app."""
    cfg = cfg or load_config()
    channels: list = []
    if cfg.desktop:
        # Desktop click-through opens the app on THIS Mac, so it uses the localhost link_base.
        channels.append(DesktopChannel(link_base=link_base))
    if cfg.ntfy_enabled and cfg.ntfy_topic:
        channels.append(NtfyChannel(topic=cfg.ntfy_topic, server=cfg.ntfy_server))
    return Notifier(channels=channels, cfg=cfg, on_error=on_error)

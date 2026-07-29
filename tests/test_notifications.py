"""Tests for the auto-apply push notifications (decision 135).

Everything is injected — no real osascript, no real HTTP — so the suite is offline and fast.
"""

from __future__ import annotations

from applicationbot import notifications as N
from applicationbot import tracker


# --------------------------------------------------------------------------- desktop


def test_desktop_channel_posts_title_and_body_on_mac():
    calls = []
    ch = N.DesktopChannel(poster=lambda t, b: calls.append((t, b)), system="Darwin")
    ch.send(N.Notification(event=N.APPROVAL_NEEDED, title="Ready to apply",
                           body="Acme — Engineer (fit 88) is ready."))
    assert calls == [("Ready to apply", "Acme — Engineer (fit 88) is ready.")]


def test_desktop_channel_noop_off_mac():
    calls = []
    ch = N.DesktopChannel(poster=lambda t, b: calls.append((t, b)), system="Linux")
    ch.send(N.Notification(event=N.APPROVAL_NEEDED, title="t", body="b"))
    assert calls == []  # unsupported OS degrades to nothing, never errors


def test_osascript_script_escapes_quotes_and_backslashes():
    script = N._osascript_script('He said "hi"', 'path C:\\x and "quote"')
    assert '\\"hi\\"' in script          # inner quotes escaped
    assert "C:\\\\x" in script            # backslash escaped
    assert script.startswith("display notification ")


def test_default_poster_uses_osascript_when_not_frozen(monkeypatch):
    # localhost / from source: no app bundle, so go straight to osascript (never try native).
    monkeypatch.setattr(N.sys, "frozen", False, raising=False)
    used = []
    monkeypatch.setattr(N, "_post_native", lambda t, b: used.append("native"))
    monkeypatch.setattr(N, "_post_osascript", lambda t, b: used.append("osascript"))
    N._default_poster("t", "b")
    assert used == ["osascript"]


def test_default_poster_prefers_native_in_frozen_app(monkeypatch):
    # Packaged app: post in-process so the notification carries the ApplicationBot icon.
    monkeypatch.setattr(N.sys, "frozen", True, raising=False)
    used = []
    monkeypatch.setattr(N, "_post_native", lambda t, b: used.append("native"))
    monkeypatch.setattr(N, "_post_osascript", lambda t, b: used.append("osascript"))
    N._default_poster("t", "b")
    assert used == ["native"]


def test_default_poster_falls_back_to_osascript_if_native_fails(monkeypatch):
    monkeypatch.setattr(N.sys, "frozen", True, raising=False)
    used = []
    def boom(t, b):
        raise RuntimeError("no bundle")
    monkeypatch.setattr(N, "_post_native", boom)
    monkeypatch.setattr(N, "_post_osascript", lambda t, b: used.append("osascript"))
    N._default_poster("t", "b")
    assert used == ["osascript"]


# --------------------------------------------------------------------------- ntfy


def test_ntfy_channel_posts_topic_headers_and_body():
    sent = {}

    def fake_opener(req):
        sent["url"] = req.full_url
        sent["headers"] = dict(req.header_items())
        sent["body"] = req.data
        sent["method"] = req.get_method()
        return None

    ch = N.NtfyChannel(topic="my-secret-topic", opener=fake_opener)
    ch.send(N.Notification(event=N.INTERVENTION_NEEDED, title="Needs you",
                           body="Acme is blocked.", link="/#notifications", urgent=True))

    assert sent["url"] == "https://ntfy.sh/my-secret-topic"
    assert sent["method"] == "POST"
    assert sent["body"] == b"Acme is blocked."
    # urllib title-cases header keys.
    assert sent["headers"]["Title"] == "Needs you"
    assert sent["headers"]["Priority"] == "high"          # urgent
    # No Click/deep-link: localhost is unreachable from the phone, so mobile is informational only
    # (decision 138). A link is present on the Notification but the phone channel never uses it.
    assert "Click" not in sent["headers"]


def test_ntfy_never_sets_click_even_with_a_link():
    sent = {}
    ch = N.NtfyChannel(topic="t", opener=lambda r: sent.update(h=dict(r.header_items())))
    ch.send(N.Notification(event=N.APPROVAL_NEEDED, title="Ready", body="x", link="/#notifications"))
    assert sent["h"]["Priority"] == "default"
    assert "Click" not in sent["h"]  # phone can't reach the Mac's localhost — never link there


# --------------------------------------------------------------------------- desktop click-through


def test_desktop_uses_terminal_notifier_for_a_clickable_notification(monkeypatch):
    # From source (not frozen): a link + terminal-notifier on PATH → a clickable notification that
    # opens the app on this Mac, instead of the osascript path (which opens Script Editor on click).
    monkeypatch.setattr(N.sys, "frozen", False, raising=False)
    ran, posted = [], []
    ch = N.DesktopChannel(
        system="Darwin", link_base="http://127.0.0.1:9000",
        which=lambda name: "/usr/local/bin/terminal-notifier" if name == "terminal-notifier" else None,
        runner=lambda cmd: ran.append(cmd),
        poster=lambda t, b: posted.append((t, b)))
    ch.send(N.Notification(event=N.APPROVAL_NEEDED, title="Ready", body="Acme is ready.",
                           link="/#notifications"))
    assert posted == []                                   # did NOT fall back to osascript
    assert ran and ran[0][0] == "/usr/local/bin/terminal-notifier"
    assert "-open" in ran[0]
    assert ran[0][ran[0].index("-open") + 1] == "http://127.0.0.1:9000/#notifications"


def test_desktop_falls_back_to_poster_without_terminal_notifier(monkeypatch):
    monkeypatch.setattr(N.sys, "frozen", False, raising=False)
    posted = []
    ch = N.DesktopChannel(
        system="Darwin", link_base="http://127.0.0.1:9000",
        which=lambda name: None,                          # terminal-notifier not installed
        runner=lambda cmd: (_ for _ in ()).throw(AssertionError("should not run")),
        poster=lambda t, b: posted.append((t, b)))
    ch.send(N.Notification(event=N.APPROVAL_NEEDED, title="Ready", body="b", link="/#notifications"))
    assert posted == [("Ready", "b")]                     # informational osascript/native path


def test_desktop_click_status_needs_terminal_notifier_from_source(monkeypatch):
    monkeypatch.setattr(N.sys, "frozen", False, raising=False)
    s = N.desktop_click_status(system="Darwin", which=lambda name: None)
    assert s == {"applicable": True, "clickable": False, "method": "none",
                 "install": "brew install terminal-notifier"}


def test_desktop_click_status_clickable_with_terminal_notifier(monkeypatch):
    monkeypatch.setattr(N.sys, "frozen", False, raising=False)
    s = N.desktop_click_status(system="Darwin", which=lambda name: "/opt/homebrew/bin/terminal-notifier")
    assert s["clickable"] is True and s["method"] == "terminal-notifier"


def test_desktop_click_status_clickable_in_frozen_app(monkeypatch):
    monkeypatch.setattr(N.sys, "frozen", True, raising=False)
    s = N.desktop_click_status(system="Darwin", which=lambda name: None)
    assert s["clickable"] is True and s["method"] == "native"


def test_desktop_click_status_not_applicable_off_mac():
    s = N.desktop_click_status(system="Linux", which=lambda name: None)
    assert s == {"applicable": False, "clickable": False, "method": "none"}


# --------------------------------------------------------------------------- notifier


class _Recorder:
    def __init__(self, name, boom=False):
        self.name = name
        self.boom = boom
        self.got = []

    def send(self, note):
        if self.boom:
            raise RuntimeError("channel down")
        self.got.append(note)


def test_notifier_fans_out_to_all_channels():
    a, b = _Recorder("a"), _Recorder("b")
    Notifier = N.Notifier(channels=[a, b], cfg=N.NotifyConfig())
    note = N.Notification(event=N.APPROVAL_NEEDED, title="t", body="b")
    Notifier.notify(note)
    assert a.got == [note] and b.got == [note]


def test_notifier_isolates_a_failing_channel():
    errors = []
    good = _Recorder("good")
    bad = _Recorder("bad", boom=True)
    # bad first: the good channel must still fire, and the error is reported not raised.
    Notifier = N.Notifier(channels=[bad, good], cfg=N.NotifyConfig(),
                          on_error=lambda name, e: errors.append((name, str(e))))
    Notifier.notify(N.Notification(event=N.APPROVAL_NEEDED, title="t", body="b"))
    assert len(good.got) == 1
    assert errors == [("bad", "channel down")]


def test_notifier_respects_event_toggles():
    rec = _Recorder("r")
    cfg = N.NotifyConfig(intervention_needed=False)
    Notifier = N.Notifier(channels=[rec], cfg=cfg)
    Notifier.notify(N.Notification(event=N.INTERVENTION_NEEDED, title="t", body="b"))
    Notifier.notify(N.Notification(event=N.APPROVAL_NEEDED, title="t", body="b"))
    assert len(rec.got) == 1 and rec.got[0].event == N.APPROVAL_NEEDED


# --------------------------------------------------------------------------- config


def test_load_config_defaults_when_missing(tmp_path):
    cfg = N.load_config(tmp_path / "nope.yaml")
    assert cfg.desktop is True
    assert cfg.ntfy_enabled is False
    assert cfg.approval_needed and cfg.intervention_needed


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "notifications.yaml"
    cfg = N.NotifyConfig(desktop=False, ntfy_enabled=True, ntfy_topic="topic-x",
                         intervention_needed=False)
    N.save_config(cfg, p)
    back = N.load_config(p)
    assert back.desktop is False
    assert back.ntfy_enabled is True and back.ntfy_topic == "topic-x"
    assert back.intervention_needed is False and back.approval_needed is True


def test_unreadable_config_falls_back_to_defaults(tmp_path):
    p = tmp_path / "notifications.yaml"
    p.write_text("{[ not valid yaml", encoding="utf-8")
    cfg = N.load_config(p)
    assert cfg.desktop is True and cfg.ntfy_enabled is False


# --------------------------------------------------------------------------- build_notifier


def test_build_notifier_desktop_only_by_default():
    n = N.build_notifier(N.NotifyConfig())
    assert [c.name for c in n.channels] == ["desktop"]


def test_build_notifier_adds_ntfy_when_enabled_with_topic():
    n = N.build_notifier(N.NotifyConfig(ntfy_enabled=True, ntfy_topic="t"))
    assert sorted(c.name for c in n.channels) == ["desktop", "ntfy"]


def test_build_notifier_skips_ntfy_without_topic():
    n = N.build_notifier(N.NotifyConfig(ntfy_enabled=True, ntfy_topic=""))
    assert [c.name for c in n.channels] == ["desktop"]


def test_build_notifier_no_channels_when_all_off():
    n = N.build_notifier(N.NotifyConfig(desktop=False))
    assert n.channels == []
    # notify on an empty notifier is a harmless no-op.
    n.notify(N.Notification(event=N.APPROVAL_NEEDED, title="t", body="b"))


# ------------------------------------------------------- durable log (decision 145)


def test_notification_log_add_list_and_unread(tmp_path):
    db = tmp_path / "applications.db"
    a = tracker.add_notification("approval_needed", "Ready to apply", "Acme is ready.",
                                 link="/#notifications", application_id=7, channels="desktop,ntfy",
                                 path=db)
    tracker.add_notification("intervention_needed", "Needs you", "Beta is blocked.",
                             urgent=True, path=db)
    rows = tracker.list_notifications(path=db)
    assert [r["title"] for r in rows] == ["Needs you", "Ready to apply"]  # newest first
    assert rows[1]["application_id"] == 7 and rows[1]["channels"] == "desktop,ntfy"
    assert rows[0]["urgent"] == 1
    assert tracker.unread_notification_count(path=db) == 2
    # id is returned and usable.
    assert isinstance(a, int) and a > 0


def test_notification_log_mark_read(tmp_path):
    db = tmp_path / "applications.db"
    i1 = tracker.add_notification("approval_needed", "A", "a", path=db)
    tracker.add_notification("approval_needed", "B", "b", path=db)
    tracker.mark_notifications_read([i1], path=db)
    assert tracker.unread_notification_count(path=db) == 1
    tracker.mark_notifications_read(None, path=db)  # all
    assert tracker.unread_notification_count(path=db) == 0


def test_notification_log_dismiss_hides_but_keeps_row(tmp_path):
    db = tmp_path / "applications.db"
    i1 = tracker.add_notification("approval_needed", "A", "a", path=db)
    tracker.add_notification("approval_needed", "B", "b", path=db)
    tracker.dismiss_notifications([i1], path=db)
    assert [r["title"] for r in tracker.list_notifications(path=db)] == ["B"]
    # dismissed row still exists in the DB (audit trail).
    assert len(tracker.list_notifications(include_dismissed=True, path=db)) == 2
    assert tracker.unread_notification_count(path=db) == 1  # dismissed no longer counts
    tracker.dismiss_notifications(None, path=db)  # clear all
    assert tracker.list_notifications(path=db) == []


def test_empty_ids_lists_are_noops(tmp_path):
    db = tmp_path / "applications.db"
    tracker.add_notification("approval_needed", "A", "a", path=db)
    assert tracker.mark_notifications_read([], path=db) == 0   # [] means "none", not "all"
    assert tracker.dismiss_notifications([], path=db) == 0
    assert tracker.unread_notification_count(path=db) == 1


# ------------------------------------------------- inbox action center (decisions 138 + 145)


def test_build_inbox_ready_survives_empty_in_memory_queue(tmp_path):
    """The bug the user hit: after a restart the in-memory ready queue is empty, but a still-dry-run
    application named by an approval notification must STILL show as a ready action card."""
    from applicationbot import web
    db = tmp_path / "applications.db"
    aid = tracker.add_application(
        {"company": "Acme", "role": "Engineer", "status": "dry-run",
         "source_url": "u1", "portal": "greenhouse", "fit_score": "88"}, path=db)
    bid = tracker.add_application(
        {"company": "Beta", "role": "Analyst", "status": "blocked", "source_url": "u2",
         "portal": "lever", "blocked_kind": "needs_answer", "blocked_detail": "2 questions"}, path=db)
    did = tracker.add_application(
        {"company": "Delta", "role": "PM", "status": "applied", "source_url": "u3"}, path=db)
    tracker.add_notification("approval_needed", "Ready", "Acme ready", application_id=aid, path=db)
    tracker.add_notification("intervention_needed", "Blocked", "Beta blocked", application_id=bid, path=db)
    tracker.add_notification("approval_needed", "Ready", "Delta ready", application_id=did, path=db)

    inbox = web._build_inbox([], path=db)  # empty in-memory queue (simulates a restart)

    assert [r["company"] for r in inbox["ready"]] == ["Acme"]   # restored from the log
    assert [p["company"] for p in inbox["parked"]] == ["Beta"]
    assert inbox["count"] == 2                                   # drives the badge
    # Actionable ones are cards (not duplicated in the feed); the applied one is a plain record.
    by_body = {n["body"]: n for n in inbox["notifications"]}
    assert by_body["Acme ready"]["actionable"] is True
    assert by_body["Beta blocked"]["actionable"] is True
    assert by_body["Delta ready"]["actionable"] is False and by_body["Delta ready"]["app_status"] == "applied"


def test_build_inbox_dedupes_in_memory_and_log(tmp_path):
    """An app in BOTH the in-memory queue and the log appears once."""
    from applicationbot import web
    db = tmp_path / "applications.db"
    aid = tracker.add_application(
        {"company": "Acme", "role": "Engineer", "status": "dry-run", "source_url": "u1"}, path=db)
    tracker.add_notification("approval_needed", "Ready", "Acme ready", application_id=aid, path=db)
    inbox = web._build_inbox([aid], path=db)
    assert [r["id"] for r in inbox["ready"]] == [aid]  # not [aid, aid]

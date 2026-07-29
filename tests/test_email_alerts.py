"""Forwarded job-alert emails as a discovery source (decision 132) — no network, fake mailbox.

Covers the mailbox reader (`fetch_alerts`, IMAP + Gmail-OAuth), the structure-agnostic link
parser (`_extract_job_links`), and `EmailAlertSource.fetch`. The one thing no unit test can
cover — a real forwarded Lensa/Aflac/LinkedIn email whose exact link markup we haven't seen —
is the decision-132 flagged live step; these tests pin the parsing CONTRACT against a plausible
alert shape so tuning the `url_contains` defaults later is a one-line change.

Run:  python -m tests.test_email_alerts   (also pytest-compatible)
"""
from __future__ import annotations

import base64
from email.message import EmailMessage

from applicationbot import discovery, mailbox
from applicationbot.discovery import (
    EmailAlertSource,
    _BUILTIN_ALERT_PROVIDERS,
    _extract_job_links,
)
from applicationbot.mailbox import MailboxConfig

# A plausible Lensa-style alert: two real postings, plus a logo link (no text), an off-domain ad,
# and a footer "Unsubscribe" on the provider's own domain — the parser must keep only the two jobs.
_LENSA_HTML = """
<html><body>
  <a href="https://lensa.com/"><img src="logo.png"></a>
  <h2>New jobs for you</h2>
  <a href="https://lensa.com/j/backend-engineer-acme-123">Backend Engineer at Acme</a>
  <a href="https://lensa.com/j/data-analyst-globex-456">Data Analyst — Globex (Remote)</a>
  <a href="https://ads.example.com/promo">Sponsored: learn Python</a>
  <a href="https://lensa.com/account/unsubscribe?u=9">Unsubscribe</a>
</body></html>
"""

_AFLAC_HTML = """
<html><body>
  <a href="https://careers.aflac.com/go/job/Insurance-Sales-Agent/998">Insurance Sales Agent</a>
  <a href="https://careers.aflac.com/manage/preferences">Manage alert preferences</a>
</body></html>
"""

# Real LinkedIn alert shape (2026-07-23): postings are `/comm/jobs/view/<id>`; the alert's own
# `/comm/jobs/search-results/` link must NOT be treated as a posting.
_LINKEDIN_HTML = """
<html><body>
  <a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=software+engineer">Your job alert for software engineer</a>
  <a href="https://www.linkedin.com/comm/jobs/view/4435190376/?trackingId=aaa%3D%3D">Software Engineer</a>
  <a href="https://www.linkedin.com/comm/jobs/view/4443084540/?trackingId=bbb%3D%3D">Backend Engineer</a>
</body></html>
"""


def _email(frm: str, html: str) -> bytes:
    m = EmailMessage()
    m["From"] = frm
    m["Subject"] = "3 new jobs match your alert"
    m.set_content("Plain-text fallback")
    m.add_alternative(html, subtype="html")
    return m.as_bytes()


class _FakeIMAP:
    def __init__(self, messages):  # messages: list[bytes], oldest→newest
        self._msgs = messages

    def select(self, mailbox):  # noqa: A002 - imaplib name
        return ("OK", [b""])

    def search(self, charset, criteria):
        ids = " ".join(str(i + 1) for i in range(len(self._msgs))).encode()
        return ("OK", [ids])

    def fetch(self, mid, spec):
        return ("OK", [(b"1 (RFC822)", self._msgs[int(mid) - 1])])

    def logout(self):
        return ("BYE", [b""])


_CFG = MailboxConfig("imap.x.com", "bot@x.com", "pw")


# --------------------------------------------------------------------- link parser (pure)

def test_extract_keeps_only_provider_job_links():
    prov = _BUILTIN_ALERT_PROVIDERS["lensa"]
    links = _extract_job_links(_LENSA_HTML, prov)
    hrefs = [h for h, _ in links]
    assert hrefs == [
        "https://lensa.com/j/backend-engineer-acme-123",
        "https://lensa.com/j/data-analyst-globex-456",
    ]  # logo (no text), off-domain ad, and unsubscribe footer all dropped
    assert links[0][1] == "Backend Engineer at Acme"


def test_extract_wrong_domain_yields_nothing():
    # Aflac provider against a Lensa email → no aflac.com links present
    assert _extract_job_links(_LENSA_HTML, _BUILTIN_ALERT_PROVIDERS["aflac"]) == []


def test_extract_skips_footer_on_own_domain():
    links = _extract_job_links(_AFLAC_HTML, _BUILTIN_ALERT_PROVIDERS["aflac"])
    assert [h for h, _ in links] == ["https://careers.aflac.com/go/job/Insurance-Sales-Agent/998"]


# --------------------------------------------------------------------- mailbox.fetch_alerts

def test_fetch_alerts_newest_first_sender_filtered():
    msgs = [
        _email("noreply@other.com", "<a href='https://x'>x</a>"),   # wrong sender
        _email("jobs@lensa.com", _LENSA_HTML),                       # older lensa
        _email("Lensa <alerts@lensa.com>", _AFLAC_HTML),             # newest lensa (distinct body)
    ]
    bodies = mailbox.fetch_alerts(_CFG, sender_contains="lensa.com",
                                  _connect=lambda cfg: _FakeIMAP(msgs))
    assert len(bodies) == 2                       # both lensa messages, other-sender skipped
    assert "Insurance Sales Agent" in bodies[0]   # newest-first ordering


def test_fetch_alerts_limit_and_no_match():
    msgs = [_email("alerts@lensa.com", _LENSA_HTML) for _ in range(5)]
    assert len(mailbox.fetch_alerts(_CFG, sender_contains="lensa", limit=2,
                                    _connect=lambda cfg: _FakeIMAP(msgs))) == 2
    assert mailbox.fetch_alerts(_CFG, sender_contains="nobody",
                                _connect=lambda cfg: _FakeIMAP(msgs)) == []


def test_fetch_alerts_swallows_connect_error():
    def boom(cfg):
        raise OSError("refused")
    assert mailbox.fetch_alerts(_CFG, sender_contains="lensa", _connect=boom) == []


def test_gmail_fetch_alerts_reads_bodies():
    cfg = MailboxConfig(host="imap.gmail.com", email="bot@gmail.com", auth="oauth",
                        refresh_token="r", client_id="c", client_secret="s")
    listing = {"messages": [{"id": "new"}, {"id": "old"}]}  # API returns newest-first
    raw = {"new": base64.urlsafe_b64encode(_email("alerts@lensa.com", _LENSA_HTML)).decode(),
           "old": base64.urlsafe_b64encode(_email("alerts@lensa.com", _AFLAC_HTML)).decode()}

    def get(access, path, params=None):
        if path == "/messages":
            return listing
        return {"raw": raw[path.rsplit("/", 1)[-1]]}

    bodies = mailbox._gmail_fetch_alerts(cfg, sender_contains="lensa.com",
                                         _token=lambda c: "tok", _get=get)
    assert len(bodies) == 2 and "Backend Engineer at Acme" in bodies[0]


# --------------------------------------------------------------------- EmailAlertSource

def test_source_emits_leads_deduped():
    prov = _BUILTIN_ALERT_PROVIDERS["lensa"]
    # inject fetch_alerts: same body twice → same two jobs, deduped by canonical url
    src = EmailAlertSource(_CFG, [prov],
                           _fetch=lambda cfg, sender_contains, limit: [_LENSA_HTML, _LENSA_HTML])
    posts = src.fetch()
    assert len(posts) == 2
    p = posts[0]
    assert p.ats == "email_alert" and p.extra["snippet_only"] is True
    assert p.extra["alert_provider"] == "lensa" and p.company == "Lensa"
    assert p.apply_url == p.url == "https://lensa.com/j/backend-engineer-acme-123"


def test_source_multi_provider_and_max_links():
    provs = [_BUILTIN_ALERT_PROVIDERS["lensa"], _BUILTIN_ALERT_PROVIDERS["aflac"]]

    def fake(cfg, sender_contains, limit):
        return [_LENSA_HTML] if "lensa" in sender_contains else [_AFLAC_HTML]

    src = EmailAlertSource(_CFG, provs, _fetch=fake)
    companies = {p.company for p in src.fetch()}
    assert companies == {"Lensa", "Aflac"}

    src2 = EmailAlertSource(_CFG, provs, max_links=1, _fetch=fake)
    assert len(src2.fetch()) == 1  # capped


def _one_lead(html=_LENSA_HTML):
    src = EmailAlertSource(_CFG, [_BUILTIN_ALERT_PROVIDERS["lensa"]],
                           _fetch=lambda cfg, sender_contains, limit: [html])
    return src.fetch()[0]


def test_lead_resolving_to_ats_becomes_auto_applyable():
    # a Lensa repost whose redirect lands on a supported ATS → upgraded to auto-apply for free
    from applicationbot.discovery import bridge_aggregator_postings
    from applicationbot.pipeline import _is_fillable
    lead = _one_lead()
    orig = discovery.resolve_redirect
    discovery.resolve_redirect = lambda url: "https://boards.greenhouse.io/acme/jobs/42"
    try:
        bridge_aggregator_postings([lead], upgrade_jd=False)
    finally:
        discovery.resolve_redirect = orig
    assert lead.ats == "greenhouse" and lead.extra["auto_applyable"] is True
    assert _is_fillable(lead) is True


def test_linkedin_leads_are_view_only_manual_and_deduped():
    # /view/ postings only (the /search-results/ alert link excluded); manual-only (ToS); the same
    # posting across two sends (unique trackingId) dedups to one.
    from applicationbot.pipeline import _is_fillable
    src = EmailAlertSource(_CFG, [_BUILTIN_ALERT_PROVIDERS["linkedin"]],
                           _fetch=lambda cfg, sender_contains, limit: [_LINKEDIN_HTML, _LINKEDIN_HTML])
    posts = src.fetch()
    assert len(posts) == 2  # 2 distinct /view/ jobs, search-results link dropped, cross-send dedup
    assert all("/comm/jobs/view/" in p.url for p in posts)
    assert all(p.extra.get("manual_only") and p.extra.get("auto_applyable") is False for p in posts)
    assert all(_is_fillable(p) is False for p in posts)  # surfaced as leads, never auto-driven


def test_bridge_skips_manual_only_leads():
    # a manual-only lead must never be resolved server-side (no hit to a robots-disallowed site)
    from applicationbot.discovery import bridge_aggregator_postings
    src = EmailAlertSource(_CFG, [_BUILTIN_ALERT_PROVIDERS["linkedin"]],
                           _fetch=lambda cfg, sender_contains, limit: [_LINKEDIN_HTML])
    lead = src.fetch()[0]
    calls = []
    orig = discovery.resolve_redirect
    discovery.resolve_redirect = lambda url: calls.append(url) or url
    try:
        bridge_aggregator_postings([lead])
    finally:
        discovery.resolve_redirect = orig
    assert calls == []                                   # never resolved
    assert lead.ats == "email_alert" and not lead.extra.get("browser_gated")


def test_unresolved_lead_is_browser_gated_not_dropped():
    # decision 133: a redirect we can't resolve server-side must NOT count the posting out —
    # it stays in the funnel, deferred to apply-time browser click-through.
    from applicationbot.discovery import bridge_aggregator_postings
    from applicationbot.pipeline import _is_fillable
    lead = _one_lead()
    orig = discovery.resolve_redirect
    discovery.resolve_redirect = lambda url: url  # unresolved (tracking-wrapped / walled)
    try:
        bridge_aggregator_postings([lead], upgrade_jd=False)
    finally:
        discovery.resolve_redirect = orig
    assert lead.ats == "email_alert"                       # ats unchanged
    assert lead.extra.get("browser_gated") is True         # deferred to Apply
    assert lead.extra.get("auto_applyable") is not False    # NOT stamped non-fillable
    assert _is_fillable(lead) is True                       # kept in the funnel


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

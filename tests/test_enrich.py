"""Enrichment-cascade tests (decision 047) — JSON-LD → CSS/DOM → LLM, all offline/zero-token.

Verifies each tier resolves in isolation, the min-description gate, that the LLM tier is only
touched when tiers 1/2 fail (and only if a caller opts in), and that CareerSiteSource maps
enriched pages to Postings with the ATS detected from the apply URL.

Run:  python -m tests.test_enrich   (also pytest-compatible)
"""
from __future__ import annotations

from applicationbot import discovery, enrich

_LONG = ("We are hiring a Senior Backend Engineer to build our payments platform. "
         "You will design APIs, own services in production, and mentor engineers.")  # > 50 chars


def _jsonld_page(apply_url="https://boards.greenhouse.io/acme/jobs/123", extra=""):
    return f"""<!doctype html><html><head>
    <script type="application/ld+json">
    {{"@context":"https://schema.org/","@type":"JobPosting","title":"Senior Backend Engineer",
      "description":"<p>{_LONG}</p><ul><li>Python</li><li>Postgres</li></ul>",
      "datePosted":"2026-07-01","jobLocationType":"TELECOMMUTE",
      "hiringOrganization":{{"@type":"Organization","name":"Acme Inc"}},
      "jobLocation":{{"@type":"Place","address":{{"@type":"PostalAddress",
        "addressLocality":"New York","addressRegion":"NY","addressCountry":"US"}}}},
      "baseSalary":{{"@type":"MonetaryAmount","currency":"USD",
        "value":{{"@type":"QuantitativeValue","minValue":150000,"maxValue":200000}}}},
      "url":"{apply_url}"}}
    </script>{extra}</head><body>ignored</body></html>"""


def test_jsonld_tier_parses_all_fields():
    r = enrich.enrich_from_html(_jsonld_page(), url="https://acme.com/x")
    assert r.tier == "json-ld" and r.ok
    assert r.title == "Senior Backend Engineer" and r.company == "Acme Inc"
    assert "payments platform" in r.description and "- Python" in r.description
    assert r.location == "New York, NY, US"
    assert r.compensation == "150000-200000 USD"
    assert r.date_posted == "2026-07-01" and r.remote is True
    assert r.apply_url == "https://boards.greenhouse.io/acme/jobs/123"


def test_jsonld_graph_array_and_type_list():
    page = ("""<script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Organization","name":"Acme"},
      {"@type":["JobPosting","Thing"],"title":"Data Scientist","description":"%s","url":"https://x.co/1"}]}
    </script>""" % _LONG)
    posts = enrich.extract_jobpostings_from_jsonld(page)
    assert len(posts) == 1 and posts[0]["title"] == "Data Scientist"
    assert enrich.enrich_from_html(page).tier == "json-ld"


def test_short_description_is_not_ok():
    page = ('<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"X","description":"too short","url":"https://x.co/1"}</script>')
    # Falls through JSON-LD (too short) → CSS (none) → no llm → empty.
    assert enrich.enrich_from_html(page).ok is False


def test_css_tier_when_no_jsonld():
    page = f"""<html><body><nav>Home Jobs</nav>
      <div class="job-description"><p>{_LONG}</p><ul><li>Owns services</li></ul></div>
      <a href="/apply/123">Apply now</a></body></html>"""
    r = enrich.enrich_from_html(page, url="https://acme.com/jobs/5")
    assert r.tier == "css" and r.ok and "payments platform" in r.description
    assert r.apply_url == "https://acme.com/apply/123"


def test_css_skips_script_text_and_takes_longest_block():
    page = f"""<html><body>
      <article>short blurb</article>
      <main><script>var x = "{_LONG}";</script><p>{_LONG} And more responsibilities here.</p></main>
      </body></html>"""
    r = enrich.enrich_from_html(page)
    assert r.tier == "css"
    assert "var x" not in r.description and "responsibilities" in r.description


def test_llm_tier_only_on_fallthrough_and_opt_in():
    plain = "<html><body><div>no structured data, no description block, just text</div></body></html>"
    calls = []

    def fake_llm(text, url):
        calls.append(url)
        return {"description": _LONG, "apply_url": "https://acme.com/apply"}

    # Without llm: nothing found, LLM never invoked.
    assert enrich.enrich_from_html(plain, url="u").ok is False
    # With llm opt-in: resolves at tier 3.
    r = enrich.enrich_from_html(plain, url="https://acme.com/j/1", llm=fake_llm)
    assert r.tier == "llm" and r.ok and r.apply_url == "https://acme.com/apply"
    assert calls == ["https://acme.com/j/1"]

    # A page that resolves at tier 1 must NOT call the llm.
    calls.clear()
    enrich.enrich_from_html(_jsonld_page(), llm=fake_llm)
    assert calls == []


def test_career_site_source_maps_ats_from_apply_url(monkeypatch):
    pages = {
        "https://acme.com/gh": _jsonld_page("https://boards.greenhouse.io/acme/jobs/123"),
        "https://acme.com/wd": _jsonld_page("https://acme.wd1.myworkdayjobs.com/careers/job/1"),
    }
    monkeypatch.setattr(discovery, "fetch_text", lambda url, **kw: pages[url])
    src = discovery.CareerSiteSource(list(pages))
    out = src.fetch()
    by_ats = {p.ats for p in out}
    assert by_ats == {"greenhouse", "workday"}
    assert all("payments platform" in p.body for p in out)
    assert src.stats["json-ld"] == 2 and src.stats["empty"] == 0


def test_career_site_source_skips_unfetchable_urls(monkeypatch):
    def flaky(url, **kw):
        if url.endswith("/bad"):
            raise discovery.DiscoveryError("HTTP 404")
        return _jsonld_page()
    monkeypatch.setattr(discovery, "fetch_text", flaky)
    src = discovery.CareerSiteSource(["https://acme.com/ok", "https://acme.com/bad"])
    out = src.fetch()
    assert len(out) == 1 and src.stats["empty"] == 1 and src.stats["json-ld"] == 1


def _run_all():
    import types

    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        kw = {}
        if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
            kw["monkeypatch"] = _MP()
        orig = discovery.fetch_text
        try:
            fn(**kw)
        finally:
            discovery.fetch_text = orig  # restore after tests that patched it
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

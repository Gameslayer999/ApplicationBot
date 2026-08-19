"""Applying without tailoring (decision 174): the user can send their résumé exactly as it
stands, so an application never has to wait on a Claude call — or rewrite a résumé they are
happy with. `tailor=False` must beat every other résumé path, spend no tokens, and label the
provenance honestly (it is neither a fresh tailor nor a reuse).

Reuses the stubbed tailor/PDF/apply harness from `test_rescan_reuse` — no tokens, no browser.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from applicationbot import apply_profile, pipeline, resume, resume_docs, resume_store, reuse
from applicationbot.discovery import Posting
from applicationbot.matching import Match

from tests.test_rescan_reuse import _Stubs, _profile

REPO = Path(__file__).resolve().parent.parent
BASE = resume.load_resume(str(REPO / "examples" / "sample_resume.yaml"))

BODY = "python react postgresql docker aws kubernetes — build backend services"
DOC = b"%PDF-1.4 python react postgresql docker aws kubernetes"
DOC_TEXT = "Jane Dev — python, react, postgresql, docker, aws, kubernetes"


def _match(company="Acme", url="https://boards.greenhouse.io/acme/jobs/1", body=BODY, fit=88):
    p = Posting(company=company, title="Engineer", body=body, url=url, ats="greenhouse")
    return Match(posting=p, keyword_score=3, matched_skills=["python"], fit_score=fit,
                 qualified=True, judged_by="claude")


def _run(m, **kw):
    return pipeline.run_testing_mode(
        BASE, m, str(REPO / "examples" / "sample_resume.yaml"), apply_profile.DEFAULT_PATH,
        headed=False, pause=False, **kw)


# ------------------------------------------------------------------ the label

def test_untailored_labels_read_as_neither_fresh_nor_reused():
    assert reuse.is_untailored(reuse.UNTAILORED)
    assert not reuse.is_reused(reuse.UNTAILORED)
    doc = reuse.uploaded_asis_label("jane.pdf")
    assert "jane.pdf" in doc and reuse.is_untailored(doc) and not reuse.is_reused(doc)
    # The existing labels keep their meaning — an untailored send must not be mistaken for either.
    assert not reuse.is_untailored(reuse.FRESH)
    assert not reuse.is_untailored(reuse.exact_reuse_label())


# ------------------------------------------------------------------ untailored_pdf

def test_base_resume_is_rendered_verbatim_when_nothing_was_uploaded():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        m = _match()
        path, source = pipeline.untailored_pdf(BASE, _profile(), m.posting.to_job_description(),
                                               "Acme", "Engineer", m.posting.url)
        assert s.tailor_calls == 0                       # no Claude call at all
        assert source == reuse.UNTAILORED
        assert path == str(resume_store.path_for("Acme", "Engineer", m.posting.url))
        # No tailoring sidecars: these bytes are not a tailor of the current inputs, so a later
        # dry-run must not reuse them as this posting's tailored résumé.
        assert not Path(path + ".stamp").exists() and not Path(path + ".sig").exists()


def test_the_verbatim_render_drops_nothing_from_the_base_resume():
    """The point of "no tailoring" is that nothing is selected, reordered, or reworded. Capture
    what actually reaches the renderer and compare it to the base résumé section by section —
    a silent omission here would send a shortened résumé under the user's own name."""
    import applicationbot.pdf as pdf_mod

    seen = {}
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)):
        pdf_mod.render_pdf = lambda base, tailored: seen.update(base=base, t=tailored) or b"%PDF-1.4 x"
        m = _match()
        pipeline.untailored_pdf(BASE, _profile(), m.posting.to_job_description(),
                                "Acme", "Engineer", m.posting.url)

    t = seen["t"]
    assert t.summary == BASE.summary
    assert t.skills == BASE.skills                 # same categories, same order, nothing dropped
    assert t.experience == BASE.experience
    assert t.projects == BASE.projects
    assert t.activities == BASE.activities
    assert t.education == BASE.education
    assert t.certifications == BASE.certifications
    assert BASE.experience and BASE.skills         # the fixture actually has content to preserve
    # The user is told it was sent untailored rather than left to infer it (UI Principle #5).
    assert any("untailored" in n.lower() for n in t.relevance_notes)
    # Profile links still flow onto the header, exactly as on a tailored send.
    assert seen["base"].contact.name == BASE.contact.name


def test_an_uploaded_document_is_preferred_and_named():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)):
        resume_docs.store("jane.pdf", DOC, DOC_TEXT)
        m = _match()
        path, source = pipeline.untailored_pdf(BASE, _profile(), m.posting.to_job_description(),
                                               "Acme", "Engineer", m.posting.url)
        assert Path(path).read_bytes() == DOC
        assert "jane.pdf" in source and reuse.is_untailored(source)


def test_an_uploaded_document_is_used_even_with_no_skill_overlap():
    """`find_uploaded_match`'s coverage threshold governs the AUTOMATIC path. Here the user
    asked for their own résumé explicitly, so a zero-coverage document is still theirs to send —
    the decision is not the matcher's to veto."""
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)):
        resume_docs.store("unrelated.pdf", b"%PDF-1.4 nursing", "Jane — pediatric nursing")
        m = _match()
        path, source = pipeline.untailored_pdf(BASE, _profile(), m.posting.to_job_description(),
                                               "Acme", "Engineer", m.posting.url)
        assert Path(path).read_bytes() == b"%PDF-1.4 nursing"
        assert "unrelated.pdf" in source


# ------------------------------------------------------------------ run_testing_mode(tailor=False)

def test_no_tailoring_spends_no_tokens_and_still_fills_the_form():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        m = _match()
        _run(m, tailor=False)
        assert s.tailor_calls == 0
        assert s.applied_pdf == str(resume_store.path_for("Acme", "Engineer", m.posting.url))
        assert s.applied_meta["resume_source"] == reuse.UNTAILORED
        # The JD sidecar is still written, so a later "Re-run → re-tailor" can regenerate offline.
        assert resume_store.has_jd(s.applied_pdf)


def test_no_tailoring_beats_a_reusable_tailored_pdf_for_the_same_posting():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        m = _match()
        _run(m)                                  # tailors once and leaves a reusable PDF + stamp
        assert s.tailor_calls == 1
        assert s.applied_meta["resume_source"] == reuse.FRESH

        _run(m, tailor=False)                    # the user's choice wins over the stamped reuse
        assert s.tailor_calls == 1
        assert s.applied_meta["resume_source"] == reuse.UNTAILORED
        assert not Path(s.applied_pdf + ".stamp").exists()


def test_no_tailoring_beats_force_retailor():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        _run(_match(), tailor=False, force_retailor=True)
        assert s.tailor_calls == 0, "an explicit 'no tailoring' must not be overridden"
        assert s.applied_meta["resume_source"] == reuse.UNTAILORED


def test_tailoring_stays_on_by_default():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        _run(_match())
        assert s.tailor_calls == 1
        assert s.applied_meta["resume_source"] == reuse.FRESH


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()

"""Uploaded-résumé precedence (decision 152): a résumé document the USER uploaded is sent as-is
when it already covers what a posting demands — outranking both reuse paths and a fresh tailor.

Covers the pure coverage math (`reuse.coverage`), the kept-document store (`resume_docs`), the
scan (`pipeline.find_uploaded_match`), and the end-to-end precedence in `run_testing_mode` — with
the tailor/PDF/apply edges stubbed (no tokens, no browser). Reuses the harness from
`test_rescan_reuse`.

Run:  python -m tests.test_uploaded_resume   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from applicationbot import apply_profile, pipeline, resume, resume_docs, resume_store, reuse
from applicationbot.discovery import Posting
from applicationbot.matching import Match

from tests.test_rescan_reuse import _Stubs

REPO = Path(__file__).resolve().parent.parent
BASE = resume.load_resume(str(REPO / "examples" / "sample_resume.yaml"))

# The posting demands six of the candidate's skills; the uploaded résumé shows all six (covered)
# or only two (0.33 — below the 0.9 bar).
BODY_FULL = "python react postgresql docker aws kubernetes — build backend services"
DOC_FULL = b"%PDF-1.4 python react postgresql docker aws kubernetes"
DOC_TEXT_FULL = "Jane Dev — python, react, postgresql, docker, aws, kubernetes"
DOC_TEXT_THIN = "Jane Dev — python, react"


def _jd(body):
    return NS(body=body)


# --------------------------------------------------------------- pure coverage

def test_coverage_is_share_of_demanded_not_jaccard():
    demanded = frozenset({"python", "react"})
    # A document listing far more than the posting asks for still scores 1.0 — Jaccard would be 0.5
    # and a real résumé would never be usable.
    assert reuse.coverage(demanded, frozenset({"python", "react", "go", "rust"})) == 1.0
    assert reuse.coverage(demanded, frozenset({"python"})) == 0.5
    assert reuse.coverage(demanded, frozenset()) == 0.0
    assert reuse.coverage(frozenset(), frozenset({"python"})) == 1.0  # nothing demanded


def test_uploaded_label_names_the_file_and_percentage():
    label = reuse.uploaded_reuse_label("jane-dev.pdf", 0.923)
    assert "jane-dev.pdf" in label and "92%" in label
    assert reuse.is_reused(label)  # badges as a reuse, not a fresh tailor


# --------------------------------------------------------------- resume_docs (kept files)

def test_only_pdfs_are_kept_and_delete_is_scoped():
    with tempfile.TemporaryDirectory() as d:
        orig = resume_docs.UPLOADS_DIR
        resume_docs.UPLOADS_DIR = Path(d)
        try:
            assert resume_docs.store("resume.docx", b"PK\x03\x04word", "text") is None
            path = resume_docs.store("Jane Dev Resume.pdf", DOC_FULL, DOC_TEXT_FULL)
            assert path and Path(path).is_file()

            docs = resume_docs.all_docs()
            assert len(docs) == 1 and docs[0][1]["text"] == DOC_TEXT_FULL
            assert resume_docs.listing()[0]["filename"] == "Jane Dev Resume.pdf"

            # Re-uploading the same bytes overwrites rather than accumulating.
            resume_docs.store("Jane Dev Resume.pdf", DOC_FULL, DOC_TEXT_FULL)
            assert len(resume_docs.all_docs()) == 1

            # Delete takes a bare file name; a path or a miss is refused, never followed.
            name = Path(path).name
            assert resume_docs.delete("../../resume.yaml") is False
            assert resume_docs.delete("nope.pdf") is False
            assert resume_docs.delete(name) is True
            assert resume_docs.all_docs() == []
        finally:
            resume_docs.UPLOADS_DIR = orig


# --------------------------------------------------------------- find_uploaded_match (scan)

def test_find_uploaded_match_respects_coverage_bar_and_threshold():
    with tempfile.TemporaryDirectory() as d:
        orig = resume_docs.UPLOADS_DIR
        resume_docs.UPLOADS_DIR = Path(d)
        try:
            resume_docs.store("jane.pdf", DOC_FULL, DOC_TEXT_FULL)
            hit = pipeline.find_uploaded_match(BASE, _jd(BODY_FULL))
            assert hit is not None and hit.score == 1.0 and hit.label == "jane.pdf"

            # threshold=0 disables the uploaded-résumé path entirely.
            assert pipeline.find_uploaded_match(BASE, _jd(BODY_FULL), threshold=0) is None

            # A posting demanding nothing we can verify (e.g. an unscraped JD) never matches —
            # trivial coverage is not evidence of fit.
            assert pipeline.find_uploaded_match(BASE, _jd("")) is None
        finally:
            resume_docs.UPLOADS_DIR = orig


def test_thin_document_below_bar_does_not_match():
    with tempfile.TemporaryDirectory() as d:
        orig = resume_docs.UPLOADS_DIR
        resume_docs.UPLOADS_DIR = Path(d)
        try:
            resume_docs.store("thin.pdf", b"%PDF-1.4 python react", DOC_TEXT_THIN)
            assert pipeline.find_uploaded_match(BASE, _jd(BODY_FULL)) is None  # 2/6 = 0.33
        finally:
            resume_docs.UPLOADS_DIR = orig


# --------------------------------------------------------------- run_testing_mode precedence

def _match(company, url, body):
    p = Posting(company=company, title="Engineer", body=body, url=url, ats="greenhouse")
    return Match(posting=p, keyword_score=3, matched_skills=["python"], fit_score=88,
                 qualified=True, judged_by="claude")


def _run(m, **kw):
    return pipeline.run_testing_mode(
        BASE, m, str(REPO / "examples" / "sample_resume.yaml"), apply_profile.DEFAULT_PATH,
        headed=False, pause=False, **kw)


def test_uploaded_resume_is_sent_instead_of_tailoring():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        resume_docs.store("jane.pdf", DOC_FULL, DOC_TEXT_FULL)
        a = _match("Acme", "https://boards.greenhouse.io/acme/jobs/1", BODY_FULL)

        _run(a)
        assert s.tailor_calls == 0, "a covering uploaded résumé must not spend a tailor call"
        # The posting got the uploaded bytes, in its own per-posting slot.
        assert s.applied_pdf == str(resume_store.path_for("Acme", "Engineer", a.posting.url))
        assert Path(s.applied_pdf).read_bytes() == DOC_FULL
        # No tailoring sidecars: these bytes are not a tailor of the current inputs, so they must
        # never be reused as "this posting's tailored résumé" or offered to other postings.
        assert not Path(s.applied_pdf + ".stamp").exists()
        assert not Path(s.applied_pdf + ".sig").exists()
        assert "jane.pdf" in s.applied_meta["resume_source"]
        assert reuse.is_reused(s.applied_meta["resume_source"])


def test_uploaded_resume_outranks_a_reusable_tailored_pdf():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        a = _match("Acme", "https://boards.greenhouse.io/acme/jobs/1", BODY_FULL)
        b = _match("Beta", "https://boards.greenhouse.io/beta/jobs/9", BODY_FULL)

        _run(a)                       # tailors Acme and seeds the cross-posting reuse corpus
        assert s.tailor_calls == 1
        assert s.applied_meta["resume_source"] == reuse.FRESH

        resume_docs.store("jane.pdf", DOC_FULL, DOC_TEXT_FULL)
        _run(b)                       # a 100%-match tailored PDF exists, but the upload outranks it
        assert s.tailor_calls == 1
        assert Path(s.applied_pdf).read_bytes() == DOC_FULL
        assert "jane.pdf" in s.applied_meta["resume_source"]


def test_force_retailor_overrides_the_uploaded_resume():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        resume_docs.store("jane.pdf", DOC_FULL, DOC_TEXT_FULL)
        a = _match("Acme", "https://boards.greenhouse.io/acme/jobs/1", BODY_FULL)

        _run(a, force_retailor=True)
        assert s.tailor_calls == 1, "'Re-tailor' must still force a fresh tailor"
        assert s.applied_meta["resume_source"] == reuse.FRESH
        assert Path(s.applied_pdf).read_bytes() != DOC_FULL


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

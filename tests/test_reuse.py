"""Cross-posting résumé reuse (decision 142): when a new job demands essentially the same skills
as one we already tailored a résumé for, reuse that PDF instead of spending a Claude tailor call.

Covers the pure similarity logic (`reuse`), the store scan (`pipeline.find_reusable`), and the
end-to-end behavior in `run_testing_mode` — all with the tailor/PDF/apply edges stubbed (no
tokens, no browser). Reuses the stub harness from `test_rescan_reuse`.

Run:  python -m tests.test_reuse   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from applicationbot import apply_profile, pipeline, resume, resume_store, reuse
from applicationbot.discovery import Posting
from applicationbot.matching import Match

from tests.test_rescan_reuse import _Stubs, _profile

REPO = Path(__file__).resolve().parent.parent
BASE = resume.load_resume(str(REPO / "examples" / "sample_resume.yaml"))

# Two bodies demanding the SAME candidate skills → identical signature → reuse. A third demands a
# strict subset → Jaccard 2/6 = 0.33, below the 0.9 bar → must re-tailor.
BODY_FULL = "python react postgresql docker aws kubernetes — build backend services"
BODY_SUBSET = "python react — small frontend role"


def _jd(body):
    return NS(body=body)


# --------------------------------------------------------------- pure signature / similarity

def test_signature_is_demanded_skill_set():
    sig = reuse.signature(BASE, _jd(BODY_FULL))
    assert sig.keywords == {"python", "react", "postgresql", "docker", "aws", "kubernetes"}
    assert sig.knockouts == ()  # no years/degree/clearance/citizenship gate in the body


def test_similarity_identical_and_jaccard():
    full = reuse.signature(BASE, _jd(BODY_FULL))
    subset = reuse.signature(BASE, _jd(BODY_SUBSET))
    assert reuse.similarity(full, full) == 1.0
    assert reuse.similarity(full, subset) == 2 / 6  # {python,react} ∩ / ∪ full


def test_similarity_zero_when_knockouts_differ():
    a = reuse.JdSignature(frozenset({"python"}), (("years", True),))
    b = reuse.JdSignature(frozenset({"python"}), (("years", False),))
    assert reuse.similarity(a, b) == 0.0  # same skills, but incompatible hard gate → never reuse


def test_similarity_no_skills_same_gate_is_full():
    a = reuse.JdSignature(frozenset(), ())
    assert reuse.similarity(a, a) == 1.0


def test_signature_dict_roundtrip():
    sig = reuse.JdSignature(frozenset({"python", "aws"}), (("years", True), ("degree", None)))
    back = reuse.JdSignature.from_dict(sig.to_dict())
    assert back == sig


# --------------------------------------------------------------- find_reusable (store scan)

def _seed(dir_: Path, url: str, body: str, base_stamp: str):
    pdf = resume_store.write_pdf(b"%PDF-1.4 x", "Acme", "Eng", url)
    resume_store.write_sig(pdf, {
        "base_stamp": base_stamp,
        "signature": reuse.signature(BASE, _jd(body)).to_dict(),
        "label": f"Acme — {url}",
        "source_url": url,
    })
    return pdf


def test_find_reusable_hits_matches_and_respects_threshold_and_base():
    with tempfile.TemporaryDirectory() as d:
        orig = resume_store.TAILORED_DIR
        resume_store.TAILORED_DIR = Path(d)
        try:
            prof = _profile()
            base = pipeline.tailor_base_stamp(BASE, prof)
            seeded = _seed(Path(d), "http://x/1", BODY_FULL, base)

            # A new posting demanding the same skills → reused.
            hit = pipeline.find_reusable(BASE, prof, _jd(BODY_FULL))
            assert hit is not None and hit.path == seeded and hit.score == 1.0

            # A posting demanding only a subset (0.33 < 0.9) → no reuse.
            assert pipeline.find_reusable(BASE, prof, _jd(BODY_SUBSET)) is None

            # Same skills but a different base stamp (résumé/links/logic changed) → no reuse.
            other = pipeline.tailor_base_stamp(BASE, _profile(linkedin_url="https://li/changed"))
            assert other != base
            assert pipeline.find_reusable(BASE, _profile(linkedin_url="https://li/changed"),
                                          _jd(BODY_FULL)) is None

            # threshold=0 disables reuse entirely.
            assert pipeline.find_reusable(BASE, prof, _jd(BODY_FULL), threshold=0) is None

            # Excluding the only candidate (its own artifact) → no reuse.
            assert pipeline.find_reusable(BASE, prof, _jd(BODY_FULL), exclude_path=seeded) is None
        finally:
            resume_store.TAILORED_DIR = orig


# --------------------------------------------------------------- run_testing_mode end-to-end

def _match(company, url, body):
    p = Posting(company=company, title="Engineer", body=body, url=url, ats="greenhouse")
    return Match(posting=p, keyword_score=3, matched_skills=["python"], fit_score=88,
                 qualified=True, judged_by="claude")


def _run(m, **kw):
    return pipeline.run_testing_mode(
        BASE, m, str(REPO / "examples" / "sample_resume.yaml"), apply_profile.DEFAULT_PATH,
        headed=False, pause=False, **kw)


def test_second_posting_with_same_skills_reuses_and_skips_tailor():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        a = _match("Acme", "https://boards.greenhouse.io/acme/jobs/1", BODY_FULL)
        b = _match("Beta", "https://boards.greenhouse.io/beta/jobs/9", BODY_FULL)

        _run(a)                       # first posting: tailors + seeds the corpus
        assert s.tailor_calls == 1
        _run(b)                       # different posting, same demanded skills → reuse, no tailor
        assert s.tailor_calls == 1, "a near-identical posting must not re-tailor"

        # Beta got its OWN per-posting PDF (a copy), not Acme's path.
        assert s.applied_pdf == str(resume_store.path_for("Beta", "Engineer", b.posting.url))
        assert Path(s.applied_pdf).is_file()


def test_dissimilar_posting_retailors():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        a = _match("Acme", "https://boards.greenhouse.io/acme/jobs/1", BODY_FULL)
        c = _match("Gamma", "https://boards.greenhouse.io/gamma/jobs/7", BODY_SUBSET)

        _run(a)
        assert s.tailor_calls == 1
        _run(c)                       # only a subset of the skills (0.33) → below bar → re-tailor
        assert s.tailor_calls == 2, "a posting below the similarity bar must re-tailor"


# --------------------------------------------------------------- résumé provenance (decision 144)

def test_provenance_labels():
    assert reuse.is_reused(reuse.exact_reuse_label())
    assert reuse.is_reused(reuse.similar_reuse_label("Acme — Eng", 0.92))
    assert reuse.is_reused(reuse.stored_reuse_label())
    assert not reuse.is_reused(reuse.FRESH)
    assert not reuse.is_reused("")
    # The similar-reuse label names the source posting and the match percentage.
    assert "Acme — Eng" in reuse.similar_reuse_label("Acme — Eng", 0.923)
    assert "92%" in reuse.similar_reuse_label("Acme — Eng", 0.923)


def test_run_testing_mode_reports_fresh_vs_reused_in_meta():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        a = _match("Acme", "https://boards.greenhouse.io/acme/jobs/1", BODY_FULL)
        b = _match("Beta", "https://boards.greenhouse.io/beta/jobs/9", BODY_FULL)

        _run(a)  # freshly tailored
        assert s.applied_meta["resume_source"] == reuse.FRESH
        _run(b)  # cross-posting reuse → a "Reused …" provenance naming Acme
        assert reuse.is_reused(s.applied_meta["resume_source"])
        assert "Acme" in s.applied_meta["resume_source"]


def test_record_run_persists_resume_source_to_row_and_detail():
    # _record_run must write the provenance onto the application row AND into the run-history
    # detail — the tracker/notification/review surfaces read it from there. Tracker writes are
    # stubbed so no DB is touched.
    import applicationbot.apply as apply_mod
    from applicationbot import tracker as trk
    cap: dict = {}
    orig = (trk.find_by_source_url, trk.add_application, trk.record_run)
    trk.find_by_source_url = lambda url, **k: None
    trk.add_application = lambda data, **k: (cap.__setitem__("row", data), 1)[1]
    trk.record_run = lambda data, **k: (cap.__setitem__("run", data), 1)[1]
    try:
        report = apply_mod.ApplyReport(url="https://x/1", ats="greenhouse")
        src = reuse.similar_reuse_label("Beta — Eng", 0.95)
        apply_mod._record_run(report, "/tmp/x.pdf", "Eng", "Acme",
                              {"source_url": "https://x/1", "resume_source": src})
        assert cap["row"]["resume_source"] == src
        assert f"Résumé: {src}" in cap["run"]["detail"]
    finally:
        trk.find_by_source_url, trk.add_application, trk.record_run = orig


def test_tracker_resume_source_column_roundtrips():
    from applicationbot import tracker as trk
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        aid = trk.add_application({"company": "Acme", "role": "Eng", "status": "dry-run",
                                   "resume_source": reuse.FRESH}, path=db)
        assert trk.get_application(aid, path=db)["resume_source"] == reuse.FRESH


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

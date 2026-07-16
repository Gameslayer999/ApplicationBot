"""Rescan re-prepare reuse (decision 069 follow-up): a re-prepared posting reuses its
already-tailored PDF — skipping the Claude tailor call — when nothing that affects the PDF has
changed since it was made. Verified with the tailor/PDF/apply edges stubbed (no tokens, no
browser). Also covers the stamp helpers and their cascade cleanup.

Run:  python -m tests.test_rescan_reuse   (also pytest-compatible)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from applicationbot import apply_profile, pipeline, resume_store
from applicationbot.discovery import Posting
from applicationbot.matching import Match
from applicationbot.resume import load_resume

REPO = Path(__file__).resolve().parent.parent
BASE = load_resume(str(REPO / "examples" / "sample_resume.yaml"))


def _profile(**kw):
    base = dict(linkedin_url="https://linkedin.com/in/x", github_url="", portfolio_url="")
    base.update(kw)
    return apply_profile.ApplicationProfile(**base)


def _jd(body="build backend services in python"):
    return NS(body=body)


# ------------------------------------------------------------------ tailor_stamp (pure)

def test_stamp_stable_and_input_sensitive():
    p = _profile()
    jd = _jd()
    base_key = pipeline.tailor_stamp(BASE, p, jd)
    assert base_key == pipeline.tailor_stamp(BASE, p, jd)  # deterministic
    # A profile *link* change alters the PDF ⇒ different stamp.
    assert pipeline.tailor_stamp(BASE, _profile(linkedin_url="https://linkedin.com/in/y"), jd) != base_key
    # A different JD (tailoring target) ⇒ different stamp.
    assert pipeline.tailor_stamp(BASE, p, _jd("nurse practitioner role")) != base_key


def test_stamp_ignores_non_pdf_profile_fields():
    # Learned screening answers etc. never change the PDF; the fill re-reads them fresh, so they
    # must NOT invalidate a reusable PDF (else every fill's answer-learning would force re-tailor).
    a = pipeline.tailor_stamp(BASE, _profile(desired_salary="100000"), _jd())
    b = pipeline.tailor_stamp(BASE, _profile(desired_salary="200000"), _jd())
    assert a == b


# ------------------------------------------------------------------ resume_store stamp

def test_stamp_roundtrip_and_cascade_cleanup():
    with tempfile.TemporaryDirectory() as d:
        orig = resume_store.TAILORED_DIR
        resume_store.TAILORED_DIR = Path(d)
        try:
            pdf = resume_store.write_pdf(b"%PDF-1.4 x", "Acme", "SWE", "http://x/1")
            resume_store.write_stamp(pdf, "key123")
            assert resume_store.read_stamp(pdf) == "key123"
            assert resume_store.read_stamp(Path(d) / "missing.pdf") is None
            # Cascade delete removes the sidecar too.
            assert resume_store.delete_if_managed(pdf) is True
            assert not Path(pdf).exists() and not Path(pdf + ".stamp").exists()
        finally:
            resume_store.TAILORED_DIR = orig


# ------------------------------------------------------------------ run_testing_mode reuse

class _Stubs:
    """Stub the tailor/PDF/apply edges of run_testing_mode; count tailor calls and capture the
    PDF path handed to run_apply. Points the PDF store at a temp dir."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.tailor_calls = 0
        self.applied_pdf = None
        self._orig = {}

    def __enter__(self):
        import applicationbot.apply as apply_mod
        import applicationbot.ats_check as ats_check
        import applicationbot.backends as backends
        import applicationbot.pdf as pdf_mod
        import applicationbot.salary as salary
        import applicationbot.tailor as tailor

        def fake_tailor(resume, jd, backend="auto"):
            self.tailor_calls += 1
            return NS(backend="stub", warnings=[], tailored=NS(relevance_notes=[]))

        def fake_apply(url, resume_pdf, resolver, **kw):
            self.applied_pdf = resume_pdf
            return NS(submitted=False, submit_state="dry-run", blockers=[], confirmation="")

        self._orig = {
            "tailor": tailor.tailor_resume, "render": pdf_mod.render_pdf,
            "verify": ats_check.verify_pdf, "resolver": apply_mod.AnswerResolver,
            "apply": apply_mod.run_apply, "band": salary.advertised_band,
            "avail": backends.claude_code_available, "dir": resume_store.TAILORED_DIR,
            "load_profile": pipeline.load_profile,
        }
        tailor.tailor_resume = fake_tailor
        pdf_mod.render_pdf = lambda *a, **k: b"%PDF-1.4 stub"
        ats_check.verify_pdf = lambda *a, **k: NS(notes=lambda: [])
        apply_mod.AnswerResolver = lambda *a, **k: NS()
        apply_mod.run_apply = fake_apply
        salary.advertised_band = lambda *a, **k: (100000, 120000)  # skip the market estimate path
        backends.claude_code_available = lambda: False
        resume_store.TAILORED_DIR = self.tmp
        pipeline.load_profile = lambda *a, **k: _profile()
        self._mods = (apply_mod, ats_check, backends, pdf_mod, salary, tailor)
        return self

    def __exit__(self, *a):
        apply_mod, ats_check, backends, pdf_mod, salary, tailor = self._mods
        tailor.tailor_resume = self._orig["tailor"]
        pdf_mod.render_pdf = self._orig["render"]
        ats_check.verify_pdf = self._orig["verify"]
        apply_mod.AnswerResolver = self._orig["resolver"]
        apply_mod.run_apply = self._orig["apply"]
        salary.advertised_band = self._orig["band"]
        backends.claude_code_available = self._orig["avail"]
        resume_store.TAILORED_DIR = self._orig["dir"]
        pipeline.load_profile = self._orig["load_profile"]


def _match():
    p = Posting(company="Acme", title="Backend Eng", body="python backend",
                url="https://boards.greenhouse.io/acme/jobs/1", ats="greenhouse")
    return Match(posting=p, keyword_score=3, matched_skills=["python"], fit_score=88,
                 qualified=True, judged_by="claude")


def _run(m, **kw):
    return pipeline.run_testing_mode(
        BASE, m, str(REPO / "examples" / "sample_resume.yaml"), apply_profile.DEFAULT_PATH,
        headed=False, pause=False, **kw)


def test_dry_run_reuses_pdf_and_skips_tailor_when_unchanged():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        m = _match()
        _run(m)  # first dry-run: tailors + writes the PDF and its stamp
        assert s.tailor_calls == 1
        first_pdf = s.applied_pdf
        assert Path(first_pdf).is_file() and Path(first_pdf + ".stamp").is_file()

        # A second dry-run with nothing changed → no second tailor, same PDF reused.
        _run(m)
        assert s.tailor_calls == 1, "unchanged dry-run must not re-tailor"
        assert s.applied_pdf == first_pdf


def test_dry_run_retailors_when_inputs_changed():
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        m = _match()
        _run(m)
        assert s.tailor_calls == 1
        # A profile link changed → the stamp no longer matches → re-tailor.
        import applicationbot.pipeline as pl
        pl.load_profile = lambda *a, **k: _profile(linkedin_url="https://linkedin.com/in/changed")
        _run(m)
        assert s.tailor_calls == 2, "a changed profile link must force a re-tailor"


def test_force_retailor_regenerates_even_when_unchanged():
    # The "re-tailor anyway" escape hatch: regenerate the résumé despite a matching stamp.
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        m = _match()
        _run(m)  # dry-run seeds the PDF + stamp
        assert s.tailor_calls == 1
        _run(m)  # unchanged dry-run reuses
        assert s.tailor_calls == 1
        _run(m, force_retailor=True)  # override forces a fresh tailor
        assert s.tailor_calls == 2


def test_armed_submit_always_retailors_even_when_unchanged():
    # A real (armed) submit must never ride on a reused artifact — it re-tailors every time.
    with tempfile.TemporaryDirectory() as d, _Stubs(Path(d)) as s:
        m = _match()
        _run(m)  # dry-run seeds the PDF + stamp
        assert s.tailor_calls == 1
        _run(m, gate=NS(armed=True))
        assert s.tailor_calls == 2, "armed submit must re-tailor despite a matching stamp"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

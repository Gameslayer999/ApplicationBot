"""Drop-in bespoke adapter plugin system — the escape hatch (DECISIONS.md #139).

Covers the loader (discovers valid modules, skips underscore/broken/incomplete ones), build_sources
wiring for an enabled adapter, and the enable-by-name helper. A fixture package on a temp path
stands in for applicationbot/sources_contrib so tests never depend on shipped adapters."""
from __future__ import annotations

import importlib
import sys

import pytest

from applicationbot.sources_contrib import load_contrib_sources


def _make_pkg(tmp_path, files: dict[str, str]):
    pkg = tmp_path / "contrib_fixture"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for fname, body in files.items():
        (pkg / fname).write_text(body)
    sys.path.insert(0, str(tmp_path))
    return importlib.import_module("contrib_fixture")


_GOOD = ("from applicationbot.discovery import Source\n"
         "NAME = 'goodboard'\nDESCRIPTION = 'a good one'\n"
         "def build(keywords):\n"
         "    s = Source(); s.name = 'contrib:goodboard'; return s\n")


def test_loader_discovers_valid_and_skips_the_rest(tmp_path):
    pkg = _make_pkg(tmp_path, {
        "good.py": _GOOD,
        "_helper.py": "NAME = 'helper'\ndef build(k): raise Exception('should be skipped')\n",  # underscore → skip
        "broken.py": "raise ImportError('boom')\n",                                              # import error → skip
        "incomplete.py": "NAME = 'x'\n",                                                          # no build() → skip
    })
    try:
        reg = load_contrib_sources(pkg)
        assert set(reg) == {"goodboard"}
        assert getattr(reg["goodboard"], "DESCRIPTION", "") == "a good one"
    finally:
        sys.path.remove(str(tmp_path))


def test_build_sources_runs_an_enabled_contrib_adapter(tmp_path, monkeypatch):
    import applicationbot.sources_contrib as sc
    from applicationbot import filters as filters_mod
    pkg = _make_pkg(tmp_path, {"good.py": _GOOD})
    try:
        # build_sources does `from .sources_contrib import load_contrib_sources` at call time.
        monkeypatch.setattr(sc, "load_contrib_sources", lambda: load_contrib_sources(pkg))
        f = filters_mod.DiscoveryFilters(contrib_sources=["goodboard"])
        names = [s.name for s in filters_mod.build_sources(f)]
        assert "contrib:goodboard" in names
        # An unknown name is silently skipped (never crashes build_sources).
        f2 = filters_mod.DiscoveryFilters(contrib_sources=["nope"])
        assert not [s for s in filters_mod.build_sources(f2) if s.name.startswith("contrib:")]
    finally:
        sys.path.remove(str(tmp_path))


def test_enable_contrib_source_appends_and_is_idempotent(tmp_path, monkeypatch):
    import yaml
    import applicationbot.sources_contrib as sc
    from applicationbot import filters as filters_mod, source_scout as ss
    # enable_contrib_source resolves load_contrib_sources from the sources_contrib package at call
    # time — stub it to a fixed registry so the test needn't ship a real adapter.
    monkeypatch.setattr(sc, "load_contrib_sources", lambda *a, **k: {"goodboard": object()})

    fpath = tmp_path / "discovery.yaml"
    fpath.write_text(yaml.safe_dump({"contrib_sources": []}))
    assert ss.enable_contrib_source("goodboard", filters_path=str(fpath)) is True
    assert ss.enable_contrib_source("goodboard", filters_path=str(fpath)) is False  # already on
    assert ss.enable_contrib_source("not-loaded", filters_path=str(fpath)) is False
    assert filters_mod.load_filters(str(fpath)).contrib_sources == ["goodboard"]


def test_shipped_template_is_not_importable_and_documents_the_contract():
    # _example.py.txt is a .txt so it's never imported/run, and the shipped package ships no live
    # adapters by default (so a fresh clone runs nothing until a PR adds one).
    from applicationbot import sources_contrib
    assert load_contrib_sources(sources_contrib) == {}

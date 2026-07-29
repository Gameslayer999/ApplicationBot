"""Drop-in bespoke Source adapters — the escape hatch for platforms a declarative spec can't
express (DECISIONS.md #139).

Most new aggregators are public JSON APIs and become a declarative `AggregatorSpec` (decision 136,
zero code). The long tail — HTML-only listings, odd auth/headers, non-standard pagination — needs a
hand-written `Source`. Those live HERE, one self-contained file per platform, added via a **reviewed
PR** (the source-scout routine drafts it; a human merges it — never auto-merged). Because each
adapter is its own module the loader auto-discovers, a PR touches ZERO core code: no edits to
discovery.py, build_sources, or the config schema.

Each adapter module exposes exactly:

    NAME = "the_muse"                                  # unique slug; enable via filters.contrib_sources
    DESCRIPTION = "The Muse jobs (custom pagination)"  # one line, shown in the UI (optional)
    def build(keywords: list[str]) -> Source: ...      # returns a ready Source; see _example.py.txt

Modules whose file name starts with '_' are skipped (templates/helpers), so `_example.py.txt` is
documentation only. A module that fails to import — or lacks NAME/build — is skipped, never
crashing discovery. Adapters are opt-in per user (off until named in `filters.contrib_sources`),
like every other source.
"""
from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType


def load_contrib_sources(package: ModuleType | None = None) -> dict[str, ModuleType]:
    """Discover contrib adapter modules as {NAME: module}. Each must expose a string `NAME` and a
    callable `build`. Modules named with a leading '_' are skipped; a broken/incomplete module is
    skipped (never raised), so one bad adapter can't break discovery. `package` overridable for tests."""
    if package is None:
        package = importlib.import_module(__name__)
    registry: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{package.__name__}.{info.name}")
        except Exception:
            continue  # a broken adapter never breaks discovery
        name = getattr(mod, "NAME", None)
        if isinstance(name, str) and name and not name.startswith("_") and callable(getattr(mod, "build", None)):
            registry[name] = mod
    return registry

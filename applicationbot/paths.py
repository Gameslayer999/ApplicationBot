"""Where ApplicationBot reads and writes user data.

Two roots, kept distinct:

- `DATA_ROOT` — the user's data (profile, résumé, filters, `applications.db`, tailored PDFs, caches).
  In development (running from the git clone) this is the repo root, so everything lands in the
  checkout exactly as before. The packaged desktop app sets the `APPLICATIONBOT_DATA` environment
  variable to a per-user location (`~/Library/Application Support/ApplicationBot`) so the app is
  independent of any source checkout and never tries to write inside its own read-only bundle.

- `BUNDLE_ROOT` — read-only product resources that ship WITH the code (job-description fixtures,
  example configs, recipe tables). This is the folder that contains the `applicationbot` package,
  whether that's the repo root or `Resources/app` inside a bundle.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent          # …/applicationbot

# Read-only product resources (fixtures/, examples/) ship next to the code. In a PyInstaller
# frozen app they're unpacked under sys._MEIPASS; from source they sit at the repo root.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BUNDLE_ROOT = Path(sys._MEIPASS)
else:
    BUNDLE_ROOT = PACKAGE_ROOT.parent


def _resolve_data_root() -> Path:
    env = os.environ.get("APPLICATIONBOT_DATA")
    if env:
        p = Path(env).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return BUNDLE_ROOT   # dev / running from the clone: unchanged behavior


DATA_ROOT = _resolve_data_root()


def data_path(*parts: str) -> Path:
    """A path under the user data root, e.g. data_path('profile', 'resume.yaml')."""
    return DATA_ROOT.joinpath(*parts)

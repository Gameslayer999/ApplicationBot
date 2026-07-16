"""Shared, committed library of learned **navigation** recipes — how to get from a posting page to
its application form (decision 076).

The deterministic reveal in `apply._open_application_form` clicks a control matching
`_REVEAL_CONTROL` ("Apply", "I'm interested"). When a site words it differently, or puts the form
behind a link the poll never finds, the form never loads and the run fills 0 fields (the real
SmartRecruiters dry-run that prompted this). When the agentic nav fallback is armed, a Claude
worker drives the browser to the form ONCE, and we distill what it did into a **nav recipe** keyed
by **host** — so every later posting on that host replays deterministically, with no Claude.

A recipe stores only **how to reach the form** — a URL path suffix and/or the accessible names of
the controls that revealed it. No answers, no résumé, no contact details: it is **PII-free** and
safe to commit and share across clones. `applicationbot/nav_recipes.json` ships with the repo and
grows as hosts are learned. Format: `{host: {"url_suffix": str, "reveal_labels": [str, …]}}`.

Mirrors `workday_recipes.py` (decision 061) — same learn-once/replay-forever shape, same
PII-free-and-committed property, one mental model for both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_RECIPES_PATH = Path(__file__).with_name("nav_recipes.json")

# A reveal label is a button's accessible name ("I'm interested"). Cap length/count so a learned
# recipe can't absorb a whole page of chrome text, and so the store stays reviewable by a human.
_MAX_LABEL_LEN = 60
_MAX_LABELS = 4


@dataclass
class NavRecipe:
    host: str
    url_suffix: str = ""                            # posting URL + this ⇒ the form (e.g. "/apply")
    reveal_labels: list[str] = field(default_factory=list)  # controls that reveal it, in-page

    def is_empty(self) -> bool:
        return not self.url_suffix and not self.reveal_labels


def host_of(url: str) -> str:
    """The recipe key for a URL: its bare host, lowercased, no port/`www.` — the unit of "a similar
    site". Every SmartRecruiters posting shares `jobs.smartrecruiters.com`, so learning one posting
    unblocks all of them."""
    h = (urlsplit(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _load_raw(path: str | Path | None) -> dict:
    p = Path(path or _RECIPES_PATH)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _clean_labels(labels) -> list[str]:
    out: list[str] = []
    for raw in labels or []:
        s = " ".join(str(raw).split())
        if s and len(s) <= _MAX_LABEL_LEN and s not in out:
            out.append(s)
    return out[:_MAX_LABELS]


def load_recipes(path: str | Path = _RECIPES_PATH) -> dict[str, NavRecipe]:
    """All nav recipes, keyed by host. Empty/malformed file ⇒ {}."""
    out: dict[str, NavRecipe] = {}
    for host, spec in _load_raw(path).items():
        if not isinstance(spec, dict):
            continue
        r = NavRecipe(host=host, url_suffix=str(spec.get("url_suffix", "") or ""),
                      reveal_labels=_clean_labels(spec.get("reveal_labels")))
        if not r.is_empty():
            out[host] = r
    return out


def get_recipe(url: str, *, path: str | Path = _RECIPES_PATH) -> "NavRecipe | None":
    """The learned recipe for a posting URL's host, if any."""
    return load_recipes(path).get(host_of(url))


def is_shareable_host(host: str) -> bool:
    """Whether a host belongs in the COMMITTED library. A loopback/private/LAN host is real for the
    machine that learned it and meaningless (or misleading) to everyone else who clones the repo —
    a live drive against a local fixture really did try to commit a `127.0.0.1` recipe. A custom
    store (tests, dev) is the caller's own business and is never filtered."""
    import ipaddress

    h = (host or "").strip().lower()
    if not h or h == "localhost" or h.endswith(".local") or "." not in h:
        return False
    try:
        return not ipaddress.ip_address(h).is_private
    except ValueError:
        return True  # a normal DNS name


def save_recipe(recipe: NavRecipe, *, path: str | Path = _RECIPES_PATH) -> None:
    """Upsert a nav recipe by host (merging: new reveal labels append, a url_suffix fills in only
    if we don't already have one — so re-learning never clobbers a known-good path)."""
    if not recipe.host or recipe.is_empty():
        return
    if Path(path or _RECIPES_PATH) == _RECIPES_PATH and not is_shareable_host(recipe.host):
        return  # keep the shared, committed library free of one-machine hosts
    raw = _load_raw(path)
    existing = raw.get(recipe.host) if isinstance(raw.get(recipe.host), dict) else {}
    labels = _clean_labels(list(existing.get("reveal_labels") or []) + recipe.reveal_labels)
    merged = {"url_suffix": str(existing.get("url_suffix") or "") or recipe.url_suffix,
              "reveal_labels": labels}
    raw[recipe.host] = {k: v for k, v in merged.items() if v}
    p = Path(path or _RECIPES_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

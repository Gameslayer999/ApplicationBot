"""Shared, committed library of learned Workday page recipes (decision 061 — M2).

When the deterministic adapter (decisions 050/059) hits a Workday page it can't fully fill — a
tenant's custom "Application Questions" — an agentic worker fills it once, and we distill what it
did into a **recipe**: for that page (identified by the md5 of its visible `data-automation-id`
set), the list of fillable fields as `(automation_id, control, question)`. Next time the same page
appears we **replay the recipe deterministically** (no Claude), so agentic use trends to 0.

A recipe stores **only selectors + question labels + control kind — never an answer value** (the
answer is re-resolved per user via the existing `AnswerResolver`), so the library is **PII-free**
and safe to commit and share across clones: `applicationbot/workday_recipes.json` ships with the
repo and grows as pages are learned. Format: `{signature: [{automation_id, control, question}, …]}`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

_RECIPES_PATH = Path(__file__).with_name("workday_recipes.json")


@dataclass
class RecipeField:
    automation_id: str
    control: str          # "text" | "dropdown" | "checkbox"
    question: str = ""    # the field's label, so replay can re-resolve the answer per user


@dataclass
class Recipe:
    signature: str
    fields: list[RecipeField] = field(default_factory=list)


def _load_raw(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_recipes(path: str | Path = _RECIPES_PATH) -> dict[str, Recipe]:
    """All recipes, keyed by page signature. Empty/malformed file ⇒ {}."""
    out: dict[str, Recipe] = {}
    for sig, fields in _load_raw(path).items():
        if not isinstance(fields, list):
            continue
        rfs = [RecipeField(automation_id=f.get("automation_id", ""), control=f.get("control", "text"),
                           question=f.get("question", ""))
               for f in fields if isinstance(f, dict) and f.get("automation_id")]
        out[sig] = Recipe(signature=sig, fields=rfs)
    return out


def get_recipe(signature: str, *, path: str | Path = _RECIPES_PATH) -> "Recipe | None":
    return load_recipes(path).get(signature)


def save_recipe(recipe: Recipe, *, path: str | Path = _RECIPES_PATH) -> None:
    """Upsert a recipe by signature into the committed library (merging: a field is added only if
    its automation_id isn't already recorded, so re-learning never duplicates or reorders)."""
    if not recipe.signature or not recipe.fields:
        return
    raw = _load_raw(path)
    existing = raw.get(recipe.signature) if isinstance(raw.get(recipe.signature), list) else []
    have = {f.get("automation_id") for f in existing if isinstance(f, dict)}
    merged = list(existing)
    for f in recipe.fields:
        if f.automation_id and f.automation_id not in have:
            merged.append(asdict(f))
            have.add(f.automation_id)
    raw[recipe.signature] = merged
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

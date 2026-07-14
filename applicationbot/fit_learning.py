"""Learn from past fit judgments to steer discovery toward postings that clear the bar
(decision 046).

The problem this solves: every run judges only the top `top_n` keyword-ranked postings
(the free pre-filter picks which). The keyword pre-filter ranks by raw skill-term overlap,
which favours verbose senior JDs — the exact postings an early-career résumé scores *lowest*
on. So the judge's scarce slots get spent on roles that always score ~20, and nothing clears
`min_fit`, even when higher-fit postings sit unjudged further down the list.

This module closes the loop:

1. **Store** — after every live run, append each *judged* posting (fit + per-dimension
   scores + detected level + board) to a git-ignored history file (`profile/fit_history.jsonl`).
2. **Predict** — before the next run, build a `Predictor` from that history and use it to
   re-rank the free pre-filter by PREDICTED fit, so the judge's `top_n` slots go to the
   postings most like past winners (right seniority band, right boards). The prediction is
   plain arithmetic over stored verdicts — **zero Claude tokens**.

With no/thin history it is a no-op: `Predictor.active` is False until `MIN_HISTORY` judged
postings exist, and prediction shrinks each bucket toward the global mean, so a board or
level seen only once can't swing the rank on noise. The store is PII (the roles you target
+ your match notes), so it lives under git-ignored ``profile/`` (Agent Guideline #12).
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .discovery import Posting, canonical_url
from .filters import EXPERIENCE_LEVELS, detect_levels

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "profile" / "fit_history.jsonl"
# One summary row per live run — the trend the UI charts so the user can watch results get
# better run over run (best/mean fit + how many cleared their bar). Separate from the
# per-posting history: runs must NOT be de-duplicated, each is its own point in time.
RUNS_PATH = REPO_ROOT / "profile" / "fit_runs.jsonl"

# Judged postings needed before the predictor steers anything. Below this the pre-filter
# keeps its pure keyword ordering (today's behaviour) — too few samples to trust.
MIN_HISTORY = 5

# Shrinkage strength: a bucket's estimate is pulled toward the global mean as if it also
# held K observations at that mean. A board/level seen once (n=1) barely moves off global;
# a bucket with many samples trusts its own mean. Keeps rare buckets from ranking on noise.
_SHRINK_K = 4.0

# Key used for postings whose title names no detectable seniority level (e.g. plain
# "Software Engineer"). "No stated level" is itself a learnable bucket.
_NO_LEVEL = "_nolevel"


def _level_keys(title: str) -> set[str]:
    return detect_levels(title) or {_NO_LEVEL}


# Width of the pre-score (ats_score) bands the predictor learns over. 20 → five bands
# (0-19, 20-39, … 80-100); coarse enough that each band collects samples, fine enough to
# separate strong from weak deterministic scores.
_PRESCORE_BAND = 20


def _prescore_band(score) -> Optional[str]:
    """The band key for a 0-100 pre-score, or None when it's absent (pre-053 history)."""
    if score is None:
        return None
    return str(min(100 // _PRESCORE_BAND - 1, int(score) // _PRESCORE_BAND))


# --------------------------------------------------------------------------- store

def _record(m) -> dict:
    """One history row from a judged Match (caller guarantees fit_score is not None)."""
    p = m.posting
    return {
        "url": p.url,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ats": p.ats,
        "company": p.company,
        "title": p.title,
        "levels": sorted(_level_keys(p.title)),
        "fit_score": int(m.fit_score),
        # The deterministic pre-score (ats_score.py, decision 052) at judge time. Stored so the
        # predictor can learn how well it actually tracks Claude's verdict FOR THIS résumé and
        # calibrate accordingly (decision 053). Absent (None) on pre-053 history rows.
        "ats_score": int(getattr(m, "ats_score", 0) or 0),
        "dimensions": dict(m.dimensions or {}),
        "matched_skills": list(m.matched_skills or []),
        "missing": list(m.missing or []),
    }


def append(matches: Iterable, *, path: str | Path | None = None) -> int:
    """Append every judged match (fit_score is not None) to the history file. Best-effort:
    a write failure is swallowed (a missing history only forgoes steering, never crashes a
    run). Returns how many rows were written."""
    rows = [_record(m) for m in matches if getattr(m, "fit_score", None) is not None]
    if not rows:
        return 0
    p = Path(path or DEFAULT_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    except Exception:
        return 0
    return len(rows)


def load(*, path: str | Path | None = None) -> list[dict]:
    """All judged records, de-duplicated by canonical URL keeping the most recent (a posting
    re-judged across runs counts once, at its latest verdict). Missing file / unreadable
    lines are skipped — never raises."""
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return []
    latest: dict[str, dict] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict) or r.get("fit_score") is None:
            continue
        key = canonical_url(r.get("url", "")) or f"{r.get('company')}|{r.get('title')}"
        prev = latest.get(key)
        if prev is None or str(r.get("ts", "")) >= str(prev.get("ts", "")):
            latest[key] = r
    return list(latest.values())


# --------------------------------------------------------------------------- run trend

def record_run(matches: Iterable, *, min_fit: int, path: str | Path | None = None) -> bool:
    """Append one summary of this run's judged postings (best/mean fit, how many cleared
    `min_fit`, count) so the UI can chart improvement over time. Best-effort; a run with no
    judged postings records nothing. Returns whether a row was written."""
    judged = [int(m.fit_score) for m in matches if getattr(m, "fit_score", None) is not None]
    if not judged:
        return False
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "n_judged": len(judged),
        "best_fit": max(judged),
        "mean_fit": round(sum(judged) / len(judged), 1),
        "cleared": sum(1 for s in judged if s >= min_fit),
        "min_fit": int(min_fit),
    }
    p = Path(path or RUNS_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        return False
    return True


def runs(*, limit: Optional[int] = None, path: str | Path | None = None) -> list[dict]:
    """Per-run summaries oldest-first (optionally the most recent `limit`). Missing file /
    unreadable lines are skipped — never raises."""
    p = Path(path or RUNS_PATH)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if isinstance(r, dict) and "best_fit" in r:
            out.append(r)
    return out[-limit:] if limit else out


# --------------------------------------------------------------------------- predictor

class Predictor:
    """Predicts a posting's fit (0-100) from history, for re-ranking the free pre-filter.

    Estimate = the average of shrunk bucket means: the posting's seniority level(s), its board
    (ATS), and — once history carries it (decision 053) — its deterministic pre-score band.
    Each bucket is pulled toward the global mean by `_SHRINK_K` pseudo-counts so a rarely-seen
    bucket can't swing the rank. `active` is False until `MIN_HISTORY` rows exist — the caller
    then leaves the keyword ordering untouched.

    Feeding the pre-score in lets the predictor *calibrate* it: if high-pre-score postings have
    historically judged low for this résumé (the deterministic score over-credits them), the
    pre-score band's learned mean reflects that and tempers the rank — the model trusts observed
    Claude verdicts over the heuristic.
    """

    def __init__(self, records: list[dict]):
        self.n = len(records)
        self.active = self.n >= MIN_HISTORY
        scores = [int(r.get("fit_score", 0)) for r in records]
        self.global_mean = (sum(scores) / len(scores)) if scores else 0.0
        self._level: dict[str, list[int]] = {}
        self._ats: dict[str, list[int]] = {}
        self._prescore: dict[str, list[int]] = {}
        for r in records:
            fit = int(r.get("fit_score", 0))
            for lvl in (r.get("levels") or [_NO_LEVEL]):
                self._level.setdefault(lvl, []).append(fit)
            ats = r.get("ats") or ""
            if ats:
                self._ats.setdefault(ats, []).append(fit)
            band = _prescore_band(r.get("ats_score"))
            if band is not None:
                self._prescore.setdefault(band, []).append(fit)

    def _shrunk(self, bucket: list[int] | None) -> float:
        """Bucket mean pulled toward the global mean by _SHRINK_K pseudo-observations. An
        empty/unseen bucket returns the global mean (a neutral contribution)."""
        if not bucket:
            return self.global_mean
        return (sum(bucket) + _SHRINK_K * self.global_mean) / (len(bucket) + _SHRINK_K)

    def predict(self, posting: Posting, ats_score: Optional[int] = None) -> float:
        """Predicted fit for a not-yet-judged posting, from its level(s), board, and — when
        `ats_score` is given AND history carries pre-scores — its pre-score band. Falls back to
        the level+board estimate for pre-053 history (empty `_prescore`), so old histories and
        callers that omit `ats_score` behave exactly as before."""
        levels = _level_keys(posting.title)
        ests = [
            sum(self._shrunk(self._level.get(l)) for l in levels) / len(levels),
            self._shrunk(self._ats.get(posting.ats or "")),
        ]
        if ats_score is not None and self._prescore:
            ests.append(self._shrunk(self._prescore.get(_prescore_band(ats_score))))
        return sum(ests) / len(ests)


def predictor(*, path: str | Path | None = None) -> Predictor:
    """Load history and build a Predictor (inactive when history is thin)."""
    return Predictor(load(path=path))


# --------------------------------------------------------------------------- diagnosis

@dataclass
class Recommendation:
    """A change the history justifies. `field`/`value` set ⇒ a one-click-applyable edit to
    discovery.yaml; both None ⇒ an informational note (e.g. a résumé gap the user must judge)."""

    kind: str  # "experience_levels" | "min_fit" | "board" | "resume_gap"
    message: str
    field: Optional[str] = None
    value: object = None


@dataclass
class FitAnalysis:
    n_judged: int
    cleared: int  # how many judged postings reached min_fit
    best_fit: int
    mean_fit: float
    dim_means: dict  # {skills, experience, seniority} averaged over history
    weakest_dim: Optional[str]
    by_level: list[dict]  # [{level, n, mean, cleared}], best mean first
    by_board: list[dict]  # [{board, n, mean, cleared}], best mean first
    top_missing: list[tuple]  # [(requirement, count)] most-recurring first
    recommendations: list[Recommendation] = field(default_factory=list)

    def lines(self) -> list[str]:
        """Human-readable diagnosis for the CLI (the Discover tab renders the same fields)."""
        if self.n_judged == 0:
            return ["No judged postings yet — run discovery once to start learning."]
        out = [
            f"Learned from {self.n_judged} judged posting(s): best fit {self.best_fit}, "
            f"mean {self.mean_fit:.0f}, {self.cleared} cleared your bar.",
        ]
        if self.dim_means:
            out.append("  dimensions: " + " · ".join(
                f"{k} {self.dim_means[k]:.0f}" for k in self.dim_means))
            if self.weakest_dim:
                out.append(f"  weakest dimension: {self.weakest_dim} — the systematic drag on fit.")
        if self.by_level:
            out.append("  by level: " + " · ".join(
                f"{b['level']} {b['mean']:.0f}({b['n']})" for b in self.by_level))
        if self.by_board:
            out.append("  by board: " + " · ".join(
                f"{b['board']} {b['mean']:.0f}({b['n']})" for b in self.by_board))
        for rec in self.recommendations:
            out.append(f"  → {rec.message}")
        return out


def _weakest_dimension(dim_means: dict) -> Optional[str]:
    return min(dim_means, key=dim_means.get) if dim_means else None


def _segment(records: list[dict], key_fn, min_fit: int) -> list[dict]:
    """Group judged records by a key (level or board), returning per-group n / mean fit /
    cleared count, best mean first. Groups with a single sample are kept but the caller only
    acts on n≥2 (see the recommendation guards)."""
    groups: dict[str, list[int]] = {}
    for r in records:
        for k in key_fn(r):
            groups.setdefault(k, []).append(int(r.get("fit_score", 0)))
    out = [
        {"level": k, "board": k, "n": len(v), "mean": sum(v) / len(v),
         "cleared": sum(1 for s in v if s >= min_fit)}
        for k, v in groups.items()
    ]
    out.sort(key=lambda d: d["mean"], reverse=True)
    return out


def analyze(records: list[dict], *, min_fit: int, current_levels: list[str]) -> FitAnalysis:
    """Diagnose why postings clear (or don't) and recommend concrete, auditable filter edits
    (decision 046). Honest and non-overfit: level/board recommendations need ≥2 samples in a
    group, résumé-gap notes need a requirement to recur, and a min_fit note fires only when
    NOTHING cleared — it surfaces the reality, it never auto-lowers the bar."""
    scores = [int(r.get("fit_score", 0)) for r in records]
    n = len(scores)
    if n == 0:
        return FitAnalysis(0, 0, 0, 0.0, {}, None, [], [], [])

    dim_sums: dict[str, list[int]] = {}
    for r in records:
        for k, v in (r.get("dimensions") or {}).items():
            dim_sums.setdefault(k, []).append(int(v))
    dim_means = {k: sum(v) / len(v) for k, v in dim_sums.items()}

    by_level = _segment(records, lambda r: r.get("levels") or [_NO_LEVEL], min_fit)
    by_board = _segment(records, lambda r: [r.get("ats") or ""], min_fit)
    for b in by_board:
        b.pop("level", None)
    for b in by_level:
        b.pop("board", None)

    missing = Counter()
    for r in records:
        for m in (r.get("missing") or []):
            missing[str(m).strip().lower()] += 1

    best_fit = max(scores)
    cleared = sum(1 for s in scores if s >= min_fit)
    analysis = FitAnalysis(
        n_judged=n, cleared=cleared, best_fit=best_fit, mean_fit=sum(scores) / n,
        dim_means=dim_means, weakest_dim=_weakest_dimension(dim_means),
        by_level=by_level, by_board=by_board, top_missing=missing.most_common(6),
    )
    if n >= MIN_HISTORY:
        analysis.recommendations = _recommend(analysis, min_fit=min_fit,
                                              current_levels=current_levels)
    return analysis


def _recommend(a: FitAnalysis, *, min_fit: int, current_levels: list[str]) -> list[Recommendation]:
    recs: list[Recommendation] = []

    # (1) experience_levels: a clear per-level winner/loser split among DETECTED levels with
    # enough samples. "_nolevel" postings pass any level gate leniently, so they're excluded
    # from this lever (the predictor handles them). Winners = real levels whose mean fit is
    # near the best observed; only recommend when it actually differs from the current gate.
    real = [b for b in a.by_level if b["level"] in EXPERIENCE_LEVELS and b["n"] >= 2]
    if len(real) >= 2:
        best = real[0]["mean"]
        winners = sorted(b["level"] for b in real if b["mean"] >= best - 12)
        losers = sorted(b["level"] for b in real if b["mean"] <= min_fit - 20 and b["cleared"] == 0)
        if winners and losers and set(winners) != set(current_levels):
            recs.append(Recommendation(
                kind="experience_levels",
                message=(f"Levels {', '.join(winners)} score highest; "
                         f"{', '.join(losers)} never cleared your bar. "
                         f"Narrow experience_levels to {', '.join(winners)}."),
                field="experience_levels", value=winners,
            ))

    # (2) min_fit reality check — only when NOTHING cleared. Surfaces that the bar is above
    # what this résumé + these boards currently produce; proposes the best achievable score
    # so dry-runs can start. Never auto-applied silently, never lowers on partial success.
    if a.cleared == 0:
        recs.append(Recommendation(
            kind="min_fit",
            message=(f"None of {a.n_judged} judged postings reached min_fit={min_fit} "
                     f"(best was {a.best_fit}). Lower min_fit to {a.best_fit} to start "
                     "dry-runs on your closest matches, or close the résumé gaps below."),
            field="min_fit", value=a.best_fit,
        ))

    # (3) board signal — a board chronically below the bar wastes judge slots. Informational
    # (dropping a board is the user's call; the Discover settings edit boards directly).
    for b in a.by_board:
        if b["n"] >= 3 and b["cleared"] == 0 and b["mean"] < min_fit * 0.6:
            recs.append(Recommendation(
                kind="board",
                message=(f"Board '{b['board']}' averaged fit {b['mean']:.0f} over {b['n']} "
                         "judged postings and none cleared — consider dropping it or adding "
                         "boards that hire at your level."),
            ))

    # (4) résumé gaps — requirements that recur across postings the résumé doesn't evidence.
    # Not a search change: either add them (if you have them) or expect these roles to score low.
    for req, count in a.top_missing:
        if count >= 2:
            recs.append(Recommendation(
                kind="resume_gap",
                message=f"{count} judged postings wanted “{req}” (not on your résumé) — "
                        "add it if you have it, or these roles will keep scoring low.",
            ))
        if len([r for r in recs if r.kind == "resume_gap"]) >= 3:
            break

    return recs


def analysis(*, min_fit: int, current_levels: list[str],
             path: str | Path | None = None) -> FitAnalysis:
    """Load history and diagnose it in one call."""
    return analyze(load(path=path), min_fit=min_fit, current_levels=current_levels)


# ------------------------------------------------------ pre-score calibration (decision 052/055)

def prescore_calibration(records: list[dict]) -> list[dict]:
    """Per pre-score band (decision 052's `ats_score`): how many judged postings landed in it
    and their MEAN actual Claude fit — i.e. how well the zero-token heuristic tracks the real
    verdict for THIS résumé. Rows without a pre-score (pre-053 history) are ignored. Ascending
    by band, each labelled by its 0-100 range."""
    top_band = 100 // _PRESCORE_BAND - 1
    bands: dict[str, list[tuple[int, int]]] = {}
    for r in records:
        b = _prescore_band(r.get("ats_score"))
        if b is None:
            continue
        bands.setdefault(b, []).append((int(r.get("ats_score", 0)), int(r.get("fit_score", 0))))
    out = []
    for b in sorted(bands, key=int):
        vals = bands[b]
        lo = int(b) * _PRESCORE_BAND
        hi = 100 if int(b) == top_band else lo + _PRESCORE_BAND - 1
        out.append({
            "band": f"{lo}-{hi}",
            "n": len(vals),
            "mean_prescore": round(sum(p for p, _ in vals) / len(vals)),
            "mean_fit": round(sum(f for _, f in vals) / len(vals), 1),
        })
    return out


def prescore_insight(records: list[dict]) -> dict:
    """The pre-score calibration bands + a one-line read of whether the heuristic is worth
    trusting for this résumé (drives the Discover-tab panel). Empty bands ⇒ no note."""
    bands = prescore_calibration(records)
    note = ""
    if len(bands) >= 2:
        delta = bands[-1]["mean_fit"] - bands[0]["mean_fit"]
        if delta >= 10:
            note = ("Higher quick-scores do judge higher — the pre-filter is ordering your judge "
                    "queue well.")
        elif delta <= -5:
            note = ("Higher quick-scores judged LOWER for your résumé — the learner now down-weights "
                    "the quick score (calibration working as intended).")
        else:
            note = ("The quick score only weakly separates fit for your résumé — the learner leans "
                    "on seniority and board instead.")
    return {"bands": bands, "note": note}

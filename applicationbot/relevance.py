"""Cheap, token-free relevance scoring against a job description.

Shared by the rules engine (to reorder/select) and the catalogue pre-selection (to narrow
a large catalogue down to the relevant slice *before* sending it to Claude, so Claude
calls stay small and fast — see DECISIONS.md #013).
"""

from __future__ import annotations

import re

from .models import Resume

_WORD = re.compile(r"[a-z0-9+#.]+")


def tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def mentions(term: str, jd_text_lower: str, jd_tokens: set[str]) -> bool:
    """True if `term` genuinely appears in the job description.

    Avoids false positives from tiny/numeric tokens: a single-letter skill like "C" or a
    number like the "5" in "MetaTrader 5" must not match on substring/token noise. Drops
    parenthetical qualifiers (e.g. "(familiar)"), keeps only tokens >= 2 chars and
    non-numeric, and requires an exact token match (single-word skills) or all significant
    tokens present / the full phrase as a substring (multi-word skills).
    """
    t = re.sub(r"\(.*?\)", "", term.strip().lower()).strip()
    toks = [w for w in _WORD.findall(t) if len(w) >= 2 and not w.isdigit()]
    if not toks:
        return False
    if len(toks) == 1:
        return toks[0] in jd_tokens
    return t in jd_text_lower or all(w in jd_tokens for w in toks)


def skill_terms(resume: Resume) -> list[str]:
    return [item for cat in resume.skills for item in cat.items]


def text_score(text: str, terms: list[str], jd_text_lower: str, jd_tokens: set[str]) -> int:
    """How many of the candidate's skills the job mentions AND this text mentions."""
    tl = text.lower()
    return sum(1 for s in terms if mentions(s, jd_text_lower, jd_tokens) and s.lower() in tl)


def qualification_score(resume: Resume, jd_text: str) -> tuple[int, list[str]]:
    """Cheap, token-free signal of fit: how many of the candidate's skills a posting asks
    for, and which. Used to RANK discovered postings and cut obvious non-matches before
    spending a Claude call on the survivors — the hybrid matcher (DECISIONS.md #025), the
    same pre-select-then-Claude pattern as the catalogue (DECISIONS.md #013)."""
    jd_low = jd_text.lower()
    jd_tok = tokens(jd_low)
    matched = sorted({s for s in skill_terms(resume) if mentions(s, jd_low, jd_tok)})
    return len(matched), matched

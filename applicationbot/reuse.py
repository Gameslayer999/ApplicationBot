"""Cross-posting résumé reuse: skip a Claude tailoring call when a new job's demanded-skill
profile is close enough to one we already tailored a résumé for (decision 142).

The tailored résumé is a function of *which of the candidate's skills the JD demands* plus the
hard knockout gates — not the JD's prose (benefits, EEO boilerplate, company blurb). So two
postings whose demanded-skill sets AND knockouts match produce essentially the same tailored
résumé, and the earlier posting's PDF can be reused verbatim (the résumé carries the applicant's
own info + profile links, never the target company's, so it is company-independent).

Comparing those sets is token-free — it reuses the same `ats_requirements` extraction the
tailoring loop already runs — so deciding to skip a Claude call never itself costs a call.

This module is pure: the signature of a JD, and the similarity between two signatures. The
filesystem scan + reuse-stamp gating that turns a match into a skipped tailor lives in
`pipeline` (`find_reusable`).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ats_requirements
from .job_description import JobDescription
from .models import Resume

# Default "close enough" bar: Jaccard overlap of two postings' demanded-skill sets. 0.9 is
# deliberately strict — near-identical skill demands — so reuse never trades résumé quality for
# tokens on a merely-adjacent role. The knockout profiles must ALSO match exactly (see
# `similarity`). Set to 0 to disable cross-posting reuse entirely.
DEFAULT_THRESHOLD = 0.9


@dataclass(frozen=True)
class JdSignature:
    """The résumé-relevant fingerprint of a posting: the candidate skills it demands, and its
    hard knockout gates as (label, verdict) pairs. Everything a tailored résumé keys on; nothing
    a tailored résumé ignores."""

    keywords: frozenset[str]
    knockouts: tuple[tuple[str, object], ...]

    def to_dict(self) -> dict:
        return {"keywords": sorted(self.keywords), "knockouts": [list(k) for k in self.knockouts]}

    @classmethod
    def from_dict(cls, d: dict) -> "JdSignature":
        return cls(
            keywords=frozenset(d.get("keywords") or []),
            knockouts=tuple(tuple(k) for k in (d.get("knockouts") or [])),
        )


def signature(resume: Resume, jd: JobDescription) -> JdSignature:
    """The demanded-skill + knockout fingerprint of `jd` against `resume` — token-free."""
    req = ats_requirements.extract(resume, jd.body or "")
    keywords = frozenset(k.strip().lower() for k in req.keywords if k.strip())
    # Sort by label only: labels are unique per kind (years/degree/clearance/citizenship), and
    # `passed` can be None, so comparing the pairs directly could raise on a None-vs-bool compare.
    knockouts = tuple(sorted(((k.label, k.passed) for k in req.knockouts), key=lambda x: x[0]))
    return JdSignature(keywords=keywords, knockouts=knockouts)


def coverage(demanded: frozenset[str], present: frozenset[str]) -> float:
    """0..1 share of a posting's demanded skills that a DOCUMENT actually shows (decision 152).

    Deliberately asymmetric, unlike `similarity`: the user's own uploaded résumé lists their whole
    history, so it almost always mentions far more than any one posting demands — Jaccard would
    score it near zero and it would never be used. What matters for sending a document as-is is
    only whether it covers what the posting screens on. Knockouts are not compared here: their
    verdicts (years / degree / clearance) are facts about the candidate, identical whichever PDF
    we send, so they gate whether to apply at all — not which résumé to attach."""
    if not demanded:
        return 1.0
    return len(demanded & present) / len(demanded)


def similarity(a: JdSignature, b: JdSignature) -> float:
    """0..1 Jaccard overlap of two postings' demanded-skill sets — but 0 when their knockout
    profiles differ (a résumé that clears one posting's hard gates may fail the other's, so the
    two are never interchangeable regardless of skill overlap). Two postings that demand no skills
    and share a knockout profile score 1.0 (tailoring produces the same generic résumé)."""
    if a.knockouts != b.knockouts:
        return 0.0
    union = a.keywords | b.keywords
    if not union:
        return 1.0
    return len(a.keywords & b.keywords) / len(union)


# --- résumé provenance labels (decision 144) -------------------------------------------
# One human-readable string describing WHICH résumé a run used — freshly tailored vs reused —
# surfaced in Track, notifications, and the review panel so the user always knows whether a
# submission rode a fresh tailor or a reused one. Reused labels always start with "Reused" so a
# UI can badge on `is_reused` without parsing the rest.

FRESH = "Freshly tailored for this posting"


def exact_reuse_label() -> str:
    """This posting's own earlier PDF, reused because its inputs are unchanged (decision 069)."""
    return "Reused this posting's earlier résumé (inputs unchanged)"


def similar_reuse_label(source: str, score: float) -> str:
    """A DIFFERENT posting's PDF, reused because it demands ~the same skills (decision 142)."""
    return f"Reused a résumé tailored for {source} ({score:.0%} skill-demand match)"


def uploaded_reuse_label(name: str, score: float) -> str:
    """The user's OWN uploaded résumé, sent as-is because it already covers what the posting
    demands (decision 152) — outranks every tailored PDF."""
    return f"Reused your uploaded résumé {name} ({score:.0%} of demanded skills covered)"


def stored_reuse_label() -> str:
    """A re-apply that reused the application's already-stored PDF as-is (no re-tailor)."""
    return "Reused this application's stored résumé"


# Untailored labels (decision 174) always start with "Your ", so `is_untailored` badges them
# without parsing the rest and `is_reused` stays False — an untailored send is not a reuse.

UNTAILORED = "Your base résumé, sent as-is (you turned tailoring off for this application)"


def uploaded_asis_label(name: str) -> str:
    """The user's OWN uploaded résumé, sent verbatim because they turned tailoring off."""
    return f"Your uploaded résumé {name}, sent as-is (you turned tailoring off for this application)"


def is_reused(source: str) -> bool:
    """True iff `source` describes a reused résumé (vs a fresh tailor). Empty = unknown."""
    return (source or "").startswith("Reused")


def is_untailored(source: str) -> bool:
    """True iff `source` describes a résumé sent with NO tailoring at all (decision 174)."""
    return (source or "").startswith("Your ")

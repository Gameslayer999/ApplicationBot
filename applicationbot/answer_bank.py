"""Self-improving answer bank for the Apply stage (decision 018).

Application questions repeat across companies, so answers should be learned once and reused:

  * Generic questions ("Are you willing to travel?", "Describe your experience with X") →
    once answered — by the user, or drafted by Claude — are saved to the bank
    (`ApplicationProfile.custom_answers`) so future autofill answers them instantly.
  * Open-ended experience questions with no banked answer are drafted with the user's Claude
    **subscription** (via the Claude Code CLI), grounded strictly in the résumé so we never
    fabricate, then cached.
  * Company-specific questions ("Why do you want to work here?") are the exception: their
    answer differs per company, so they are NEVER cached to the shared bank.

Generation is best-effort: if the Claude CLI isn't available, we return None and the field
falls back to the needs-attention queue.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .models import Resume

# Phrases whose answer depends on the specific company/role — never cache these.
_COMPANY_SPECIFIC = (
    "why do you want to work", "why do you want to join", "why are you interested",
    "why this company", "why this role", "why this position", "why our", "why us",
    "what interests you", "what excites you", "what draws you", "what attracts you",
    "excited about", "excited to join", "excited to work", "excited to be",
    "drew you to", "draws you to", "attracted you to", "interested in joining",
    "want to work here", "want to work at", "why do you want", "motivates you to",
    "our mission", "our company", "our team", "our product", "our values", "our culture",
    "this company", "this role", "this position", "about our", "why here",
    "cover letter", "what do you know about", "why are you applying",
)

# Signals that a question wants a written, multi-sentence answer (vs a short field).
_OPEN_ENDED = (
    "describe", "tell us", "tell me", "explain", "walk us through", "walk me through",
    "give an example", "provide an example", "share an", "share a", "elaborate",
    "in your own words", "what is your experience", "what experience do you have",
    "how have you", "how would you", "how do you", "what are your", "what was your",
    "please provide", "please describe",
)


def _norm(q: str) -> str:
    return " ".join((q or "").lower().split())


# Voluntary EEO / demographic questions — handled by the structured EEO profile fields
# (blank = decline to self-identify), never the answer bank.
_DEMOGRAPHIC = ("gender", "race", "ethnicity", "hispanic", "latino", "veteran", "disability",
                "military", "pronoun", "lgbt", "transgender", "sexual orientation")

# Question terms whose answers are enumerated/specific (their own option set, not a Yes/No or a
# profile field) — mapping them onto a structured type produces a confident-wrong answer
# (e.g. a security-clearance dropdown mapped to a boolean → "Yes").
_ENUMERATED = ("clearance", "employment history", "gpa", "sat score", "act score",
               "gre score", "test score")


def is_company_specific(question: str) -> bool:
    n = _norm(question)
    return any(t in n for t in _COMPANY_SPECIFIC)


def is_demographic(question: str) -> bool:
    n = _norm(question)
    return any(t in n for t in _DEMOGRAPHIC)


# Questions asking for a NUMERIC FACT the applicant must own — drafting one fabricates data
# (a live AppLovin dry-run drafted a salary figure grounded in nothing). They resolve from the
# profile / the salary machinery (decisions 038/039) or are captured for the user — never drafted.
_NUMERIC_FACT = ("salary", "compensation expectation", "pay expectation", "desired pay",
                 "expected compensation", "gpa", "test score")


def is_open_ended(question: str, is_textarea: bool = False) -> bool:
    """True if the question wants a written answer Claude should draft. Textareas count, as do
    questions with an open-ended phrase that are long enough to be prose (not a short field).
    Numeric-fact questions (salary, GPA) are NEVER open-ended, whatever their phrasing."""
    n = _norm(question)
    if any(t in n for t in _NUMERIC_FACT):
        return False
    if is_textarea:
        return True
    return len(n) > 25 and any(t in n for t in _OPEN_ENDED)


_SYSTEM = """\
You draft a candidate's answer to a single job-application question. You are given the \
candidate's RÉSUMÉ (the only source of truth about them) and the QUESTION.

Hard rules:
- Use ONLY facts present in the résumé. NEVER invent employers, projects, titles, dates, \
metrics, tools, or experience the candidate does not have.
- If the résumé shows little or no relevant experience, say so honestly and briefly using \
what IS there — do not fabricate to fill space.
- Write in the first person, professional and specific, drawing on concrete résumé details.
- Answer ONLY the question. No preamble, no sign-off, no markdown, no quotes around it.
- Keep it concise: {max_chars} characters or fewer (a tight paragraph)."""


# Structured profile fields a novel question can be semantically mapped onto. The key is the
# field the resolver answers from; the text is what that field means (used in the classifier
# prompt). Demographic/EEO and company-specific questions are intentionally NOT here — they're
# handled separately and must never be auto-mapped.
CLASSIFIABLE_TYPES: dict[str, str] = {
    "work_authorized": "Already legally allowed to work in the country WITHOUT any employer "
                       "action (yes/no). NOT about needing a visa sponsored.",
    "requires_sponsorship": "Needs the EMPLOYER to sponsor a visa or work permit, now or in the "
                            "future (yes/no). Distinct from already being authorized.",
    "us_citizen": "Is a citizen of the country (yes/no).",
    "willing_to_relocate": "Willing to relocate / move to a new city for the job (yes/no).",
    "open_to_remote": "Willingness about WORK LOCATION/ARRANGEMENT — working remotely, hybrid, "
                      "or in-person from a specific office some days per week (yes/no).",
    "desired_salary": "Expected or desired salary / compensation.",
    "earliest_start_date": "When they can start / begin, their availability, or notice period.",
    "years_experience": "Total years of relevant professional experience.",
    "itar_us_person": "Qualifies as a 'U.S. person' under ITAR / U.S. export-control "
                      "regulations (citizen, national, green-card holder, refugee, or "
                      "asylee), or meets an export-compliance gate (yes/no). NOT about "
                      "holding a security clearance.",
    "role_commitment": "A readiness/commitment check — asks whether the applicant is up for, "
                       "ready for, or committed to the role or its described demands "
                       "(yes/no), e.g. 'Are you up for it?', 'Are you ready for this "
                       "challenge?'. NOT a specific logistical fact (start date, relocation, "
                       "remote/onsite, travel).",
    "how_heard": "How they heard about or found this job.",
    "location": "Their current city / where they are based.",
    "country": "The country they live in.",
}


def valid_mapping(question: str, key: str) -> bool:
    """True if banking `maps_to=key` for `question` is allowed. This is the WRITE-TIME gate:
    a banked mapping overrides the structured resolver rules on every future form, so a wrong
    Claude classification that slips through would compound (the polluted-answer-bank incident,
    decision on 2026-07-06). The classifier refuses these questions before calling Claude too;
    this re-checks at the persistence layer so no future call path can pollute the bank.
    `scripts/prune_answer_bank.py` applies the same rules retroactively to old data."""
    n = _norm(question)
    return bool(
        len(n) >= 4  # garbage capture ("yes", stray tokens)
        and key in CLASSIFIABLE_TYPES
        and not is_demographic(question)
        and not is_company_specific(question)
        and not any(t in n for t in _ENUMERATED)
    )


def _json_reply(out: str, key: str):
    """Parse a schema-constrained CLI reply and return `key`'s value, or None. The CLI enforces
    the schema, so this is normally a plain json.loads — the fallback tolerates a wrapper."""
    for candidate in (out, out[out.find("{"): out.rfind("}") + 1]):
        try:
            return json.loads(candidate).get(key)
        except Exception:
            continue
    return None


def _classifiable(question: str) -> bool:
    """True if `question` may be semantically mapped onto a structured type at all.
    Company-specific / demographic / enumerated-answer questions are handled elsewhere and
    must never be auto-mapped (mapping an enumerated question onto a boolean produces a
    confident-wrong "Yes" — e.g. security clearance)."""
    n = _norm(question)
    return bool(n) and not is_company_specific(question) and not is_demographic(question) \
        and not any(t in n for t in _ENUMERATED)


_CLASSIFY_RULES = (
    "A type matches only if answering that field would correctly answer the question "
    "(functional equivalence, not just topical similarity)."
)


def classify_question(question: str, *, model: Optional[str] = None) -> Optional[str]:
    """Use Claude to map a novel question onto a known structured field type (a key of
    CLASSIFIABLE_TYPES), or None if it doesn't correspond to any. This catches semantic
    variants the keyword resolver misses — e.g. "Are you willing to work out of our NYC or SF
    office 2-3 days per week?" → 'open_to_remote'. The reply is schema-constrained to the
    known keys (an enum), so free-text parsing can never mis-read it. Best-effort: returns
    None if the Claude CLI is unavailable, or it answers 'none'."""
    if not _classifiable(question):
        return None
    from . import backends  # lazy

    types = "\n".join(f"- {k}: {v}" for k, v in CLASSIFIABLE_TYPES.items())
    prompt = (
        "Map a job-application question to ONE of these known answer types, or 'none'.\n\n"
        f"TYPES:\n{types}\n- none: does not correspond to any type above.\n\n"
        f"QUESTION: {question!r}\n\n"
        f"{_CLASSIFY_RULES}\n"
        'Reply with JSON: {"type": "<type key or none>"}.'
    )
    schema = {"type": "object",
              "properties": {"type": {"type": "string",
                                      "enum": [*CLASSIFIABLE_TYPES, "none"]}},
              "required": ["type"], "additionalProperties": False}
    try:
        out = backends.run_claude_cli(prompt, model=model, think=False, timeout=60,
                                      json_schema=schema)
    except Exception:
        return None
    key = _json_reply(out, "type")
    return key if key in CLASSIFIABLE_TYPES else None


def classify_questions(questions: list[str], *, model: Optional[str] = None
                       ) -> dict[str, Optional[str]]:
    """Batch classify_question: ONE schema-constrained call maps every eligible question onto
    a known type (or none) — N novel questions on a form page cost one CLI spawn instead of N.
    The reply is an enum array pinned to exactly len(questions) items, so answers can't shift
    position. Best-effort: any failure → all None (each question falls back to capture)."""
    out: dict[str, Optional[str]] = {q: None for q in questions}
    askable = [q for q in dict.fromkeys(questions) if _classifiable(q)]
    if not askable:
        return out
    from . import backends  # lazy

    types = "\n".join(f"- {k}: {v}" for k, v in CLASSIFIABLE_TYPES.items())
    numbered = "\n".join(f"{i}. {q!r}" for i, q in enumerate(askable))
    prompt = (
        "Map EACH job-application question below to ONE of these known answer types, "
        "or 'none'.\n\n"
        f"TYPES:\n{types}\n- none: does not correspond to any type above.\n\n"
        f"QUESTIONS:\n{numbered}\n\n"
        f"{_CLASSIFY_RULES} Judge each question independently.\n"
        f'Reply with JSON: {{"types": [<one type key or "none" per question, '
        f"in order, {len(askable)} items>]}}."
    )
    schema = {"type": "object",
              "properties": {"types": {"type": "array",
                                       "items": {"type": "string",
                                                 "enum": [*CLASSIFIABLE_TYPES, "none"]},
                                       "minItems": len(askable), "maxItems": len(askable)}},
              "required": ["types"], "additionalProperties": False}
    try:
        reply = backends.run_claude_cli(prompt, model=model, think=False, timeout=120,
                                        json_schema=schema)
    except Exception:
        return out
    keys = _json_reply(reply, "types")
    if not isinstance(keys, list) or len(keys) != len(askable):
        return out
    for q, k in zip(askable, keys):
        if k in CLASSIFIABLE_TYPES:
            out[q] = k
    return out


def match_banked_question(question: str, banked: list[tuple[str, str]], *,
                          model: Optional[str] = None) -> Optional[int]:
    """Use Claude to find the banked Q&A whose saved answer already answers `question`, or
    None. Catches rephrasings the literal bank match misses (e.g. banked "Are you willing to
    travel up to 25%?" answering a new "How much travel are you comfortable with?"), so an
    answer the user gave once is reused instead of the field being skipped. `banked` is
    (question, answer-preview) pairs; returns the matched index. Company-specific and
    demographic questions are never matched (handled elsewhere). Best-effort: None if the
    Claude CLI is unavailable."""
    n = _norm(question)
    cands = banked[:80]  # bound the prompt for a very large bank
    if not n or not cands or is_company_specific(question) or is_demographic(question):
        return None
    from . import backends  # lazy

    numbered = "\n".join(
        f"{i}. Q: {q}\n   A: {a[:120]}" for i, (q, a) in enumerate(cands))
    prompt = (
        "A job-application form asks a question. Below are question→answer pairs the "
        "applicant has already saved. Find the saved pair that is the SAME question "
        "reworded — i.e. its saved answer is a correct, complete answer to the new "
        "question as asked.\n\n"
        f"NEW QUESTION: {question!r}\n\nSAVED PAIRS:\n{numbered}\n\n"
        "Match on functional equivalence, not topical similarity: if the new question asks "
        "for different information, a different scope, or a different answer format than "
        "the saved answer provides, it is NOT a match.\n"
        'Reply with JSON: {"match": <number of the matching pair, or -1 if none qualifies>}.'
    )
    schema = {"type": "object", "properties": {"match": {"type": "integer"}},
              "required": ["match"], "additionalProperties": False}
    try:
        out = backends.run_claude_cli(prompt, model=model, think=False, timeout=60,
                                      json_schema=schema)
    except Exception:
        return None
    idx = _json_reply(out, "match")
    return idx if isinstance(idx, int) and 0 <= idx < len(cands) else None


_BANK_MATCH_RULES = (
    "Match on functional equivalence, not topical similarity: if a question asks for "
    "different information, a different scope, or a different answer format than the saved "
    "answer provides, it is NOT a match."
)


def match_banked_questions(questions: list[str], banked: list[tuple[str, str]], *,
                           model: Optional[str] = None) -> dict[str, Optional[int]]:
    """Batch match_banked_question: ONE call matches every eligible question against the saved
    bank (the bank is sent once, not once per question). Returns question → matched bank index
    (or None). Best-effort: any failure → all None."""
    out: dict[str, Optional[int]] = {q: None for q in questions}
    cands = banked[:80]
    askable = [q for q in dict.fromkeys(questions)
               if _norm(q) and not is_company_specific(q) and not is_demographic(q)]
    if not (cands and askable):
        return out
    from . import backends  # lazy

    pairs = "\n".join(f"{i}. Q: {q}\n   A: {a[:120]}" for i, (q, a) in enumerate(cands))
    numbered = "\n".join(f"{i}. {q!r}" for i, q in enumerate(askable))
    prompt = (
        "A job-application form asks several questions. Below are question→answer pairs the "
        "applicant has already saved. For EACH new question, find the saved pair that is the "
        "SAME question reworded — i.e. its saved answer is a correct, complete answer to the "
        "new question as asked.\n\n"
        f"NEW QUESTIONS:\n{numbered}\n\nSAVED PAIRS:\n{pairs}\n\n"
        f"{_BANK_MATCH_RULES} Judge each question independently.\n"
        f'Reply with JSON: {{"matches": [<the matching pair number, or -1 if none qualifies, '
        f"one per new question in order, {len(askable)} items>]}}."
    )
    schema = {"type": "object",
              "properties": {"matches": {"type": "array", "items": {"type": "integer"},
                                         "minItems": len(askable), "maxItems": len(askable)}},
              "required": ["matches"], "additionalProperties": False}
    try:
        reply = backends.run_claude_cli(prompt, model=model, think=False, timeout=120,
                                        json_schema=schema)
    except Exception:
        return out
    idxs = _json_reply(reply, "matches")
    if not isinstance(idxs, list) or len(idxs) != len(askable):
        return out
    for q, i in zip(askable, idxs):
        if isinstance(i, int) and 0 <= i < len(cands):
            out[q] = i
    return out


def pick_dropdown_option(label: str, value: str, options: list[str], *,
                         model: Optional[str] = None) -> Optional[str]:
    """Use Claude to choose the dropdown option that best represents `value` for a field
    labelled `label` — the general fallback when literal/hint matching fails (e.g. answer
    "Rutgers University" vs option "Rutgers University-New Brunswick", or a verbose degree vs
    "Bachelor's Degree"). Returns the chosen option VERBATIM from `options`, or None if none
    genuinely fits (never force a wrong pick). Best-effort: None if the CLI is unavailable."""
    opts = [o for o in options if (o or "").strip()][:60]
    if not (value and opts):
        return None
    from . import backends  # lazy

    numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(opts))
    prompt = (
        f"A job-application dropdown labelled {label!r} must be set to the applicant's answer: "
        f"{value!r}.\nChoose the option below that best represents that answer.\n\n"
        f"OPTIONS:\n{numbered}\n\n"
        "An option MATCHES if it refers to the same institution/organization/value as the "
        "answer — i.e. it shares the answer's core name, possibly with an extra qualifier (a "
        "campus/location), or is a broader/narrower form of the same degree. Among matching "
        "options pick the primary/main/closest one (e.g. answer 'The Pennsylvania State "
        "University' → 'Pennsylvania State University-Main Campus'). If NO option shares the "
        "answer's core identity — every option names a DIFFERENT institution (answer 'Penn "
        "State' but options Harvard/MIT/Stanford) — that is not a match.\n"
        'Reply with JSON: {"choice": <number of the best option, or -1 if none fits>}.'
    )
    schema = {"type": "object", "properties": {"choice": {"type": "integer"}},
              "required": ["choice"], "additionalProperties": False}
    try:
        out = backends.run_claude_cli(prompt, model=model, think=False, timeout=60,
                                      json_schema=schema)
    except Exception:
        return None
    idx = _json_reply(out, "choice")
    if not (isinstance(idx, int) and 0 <= idx < len(opts)):
        return None
    chosen = opts[idx]
    return chosen if _plausible_pick(value, chosen) else None


def _plausible_pick(value: str, chosen: str) -> bool:
    """Deterministic guard on a Claude dropdown pick: it must share a meaningful (non-generic)
    token with the answer, so Claude can never return an UNRELATED same-category option
    ("Harvard" for "Penn State"). Booleans/short answers are EXEMPT — a "Yes" answer
    legitimately maps to a descriptive option sharing no word ("I am authorized to work for
    any employer"), and the label gives Claude the context to pick correctly; the prompt
    already makes it decline when nothing fits."""
    stop = {"university", "college", "the", "of", "school", "institute", "and", "at", "for",
            "degree", "in", "a", "an", "on", "inc", "llc", "yes", "no", "true", "false", "none"}
    vtok = {t for t in re.findall(r"[a-z]+", value.lower()) if len(t) > 2 and t not in stop}
    otok = {t for t in re.findall(r"[a-z]+", chosen.lower()) if len(t) > 2 and t not in stop}
    return not vtok or bool(vtok & otok)


_PICK_RULES = (
    "An option MATCHES if it refers to the same institution/organization/value as the "
    "answer — i.e. it shares the answer's core name, possibly with an extra qualifier (a "
    "campus/location), or is a broader/narrower form of the same degree. Among matching "
    "options pick the primary/main/closest one (e.g. answer 'The Pennsylvania State "
    "University' → 'Pennsylvania State University-Main Campus'). If NO option shares the "
    "answer's core identity, there is no match."
)


def pick_dropdown_options(items: list[tuple[str, str, list[str]]], *,
                          model: Optional[str] = None) -> list[Optional[str]]:
    """Batch pick_dropdown_option: ONE call decides every (label, value, options) dropdown on
    a form page. Returns the chosen option text (verbatim) or None per item, in order, with
    the same deterministic token guard applied to each pick. Best-effort: failure → all None."""
    results: list[Optional[str]] = [None] * len(items)
    trimmed = [(label, value, [o for o in options if (o or "").strip()][:60])
               for label, value, options in items]
    ask = [i for i, (_, value, opts) in enumerate(trimmed) if value and opts]
    if not ask:
        return results
    from . import backends  # lazy

    blocks = []
    for n, i in enumerate(ask):
        label, value, opts = trimmed[i]
        numbered = "\n".join(f"  {j}. {o}" for j, o in enumerate(opts))
        blocks.append(f"DROPDOWN {n} — labelled {label!r}, applicant's answer: {value!r}\n{numbered}")
    prompt = (
        "For EACH job-application dropdown below, choose the option that best represents the "
        "applicant's answer.\n\n" + "\n\n".join(blocks) + "\n\n"
        f"{_PICK_RULES} Judge each dropdown independently.\n"
        f'Reply with JSON: {{"choices": [<the best option number, or -1 if none fits, '
        f"one per dropdown in order, {len(ask)} items>]}}."
    )
    schema = {"type": "object",
              "properties": {"choices": {"type": "array", "items": {"type": "integer"},
                                         "minItems": len(ask), "maxItems": len(ask)}},
              "required": ["choices"], "additionalProperties": False}
    try:
        reply = backends.run_claude_cli(prompt, model=model, think=False, timeout=120,
                                        json_schema=schema)
    except Exception:
        return results
    idxs = _json_reply(reply, "choices")
    if not isinstance(idxs, list) or len(idxs) != len(ask):
        return results
    for n, i in enumerate(ask):
        _, value, opts = trimmed[i]
        j = idxs[n]
        if isinstance(j, int) and 0 <= j < len(opts) and _plausible_pick(value, opts[j]):
            results[i] = opts[j]
    return results


def generate_answer(
    question: str,
    resume: Resume,
    *,
    company: Optional[str] = None,
    jd: Optional[str] = None,
    max_chars: int = 700,
    model: Optional[str] = None,
) -> Optional[str]:
    """Draft a grounded answer via the Claude subscription. Returns None if unavailable/failed."""
    from . import backends  # lazy: avoids importing the CLI plumbing unless we generate

    context = ("RÉSUMÉ (source of truth, JSON):\n"
               f"{resume.model_dump_json(exclude_none=True, exclude_defaults=True)}\n\n")
    if company:
        context += f"COMPANY: {company}\n"
    if jd:
        context += f"JOB DESCRIPTION:\n{jd[:2000]}\n\n"
    prompt = (
        _SYSTEM.format(max_chars=max_chars)
        + "\n\n" + context
        + f"QUESTION: {question}\n\n"
        + "Write the answer now (plain text only)."
    )
    try:
        text = backends.run_claude_cli(prompt, model=model).strip()
    except Exception:
        return None
    if not text:
        return None
    # Strip accidental wrapping quotes/fences and hard-cap the length at a sentence boundary.
    text = text.strip().strip('"').strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = (cut[: end + 1] if end > max_chars // 2 else cut).strip()
    return text or None

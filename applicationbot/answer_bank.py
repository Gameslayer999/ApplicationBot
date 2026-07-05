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
_DEMOGRAPHIC = ("gender", "race", "ethnicity", "hispanic", "latino", "veteran", "disability")


def is_company_specific(question: str) -> bool:
    n = _norm(question)
    return any(t in n for t in _COMPANY_SPECIFIC)


def is_demographic(question: str) -> bool:
    n = _norm(question)
    return any(t in n for t in _DEMOGRAPHIC)


def is_open_ended(question: str, is_textarea: bool = False) -> bool:
    """True if the question wants a written answer Claude should draft. Textareas count, as do
    questions with an open-ended phrase that are long enough to be prose (not a short field)."""
    if is_textarea:
        return True
    n = _norm(question)
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
    "how_heard": "How they heard about or found this job.",
    "location": "Their current city / where they are based.",
    "country": "The country they live in.",
}


def classify_question(question: str, *, model: Optional[str] = None) -> Optional[str]:
    """Use Claude to map a novel question onto a known structured field type (a key of
    CLASSIFIABLE_TYPES), or None if it doesn't correspond to any. This catches semantic
    variants the keyword resolver misses — e.g. "Are you willing to work out of our NYC or SF
    office 2-3 days per week?" → 'open_to_remote'. Best-effort: returns None if the Claude CLI
    is unavailable, the answer isn't a known key, or it's explicitly 'none'."""
    n = _norm(question)
    if not n or is_company_specific(question) or is_demographic(question):
        return None  # these are handled elsewhere and must never be auto-mapped
    from . import backends  # lazy

    types = "\n".join(f"- {k}: {v}" for k, v in CLASSIFIABLE_TYPES.items())
    prompt = (
        "Map a job-application question to ONE of these known answer types, or 'none'.\n\n"
        f"TYPES:\n{types}\n- none: does not correspond to any type above.\n\n"
        f"QUESTION: {question!r}\n\n"
        "A type matches only if answering that field would correctly answer the question "
        "(functional equivalence, not just topical similarity). Reply with just the type key "
        "(e.g. open_to_remote) or none. If you reason, end your reply with the final key on "
        "its own line."
    )
    try:
        out = backends.run_claude_cli(prompt, model=model, think=False, timeout=60).strip().lower()
    except Exception:
        return None
    # Robust parse: the model may reason before answering, so take the LAST known type key it
    # mentions — unless it concludes 'none' after that (a rejection wins).
    key_pos = {k: out.rfind(k) for k in CLASSIFIABLE_TYPES}
    best_key = max(key_pos, key=key_pos.get)
    best_pos = key_pos[best_key]
    none_pos = max((m.start() for m in re.finditer(r"\bnone\b", out)), default=-1)
    if best_pos < 0 or none_pos > best_pos:
        return None
    return best_key


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

    context = f"RÉSUMÉ (source of truth, JSON):\n{resume.model_dump_json(indent=2)}\n\n"
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

"""Pluggable tailoring backends.

Two engines implement the same interface (see DECISIONS.md #011):

- ClaudeCodeBackend — tailors via the **Claude Code CLI** (`claude -p`), which runs on the
  user's Claude **subscription** (Pro/Max included programmatic usage), NOT the metered
  Claude API. Requires Claude Code installed and signed in. Best quality.
- RulesBackend — no LLM at all. Keyword-matches the job description and reorders/selects
  skills, experience, projects, and activities by relevance. Zero deps, no account, no
  network, deterministic, never invents — but can't reword prose.

- AnthropicAPIBackend — the **fallback** engine (decision 111). Tailors via the metered
  Anthropic **API** (`anthropic` SDK) using the user's own key from the OS keychain. Billed
  pay-per-token to their API account, NOT their subscription. Used only when Claude Code
  isn't available — because Anthropic restricts subscription OAuth to Claude Code/Claude.ai,
  a third-party app cannot use the subscription except by shelling out to Claude Code.

`select_backend("auto")` prefers Claude Code (subscription); if it's absent it uses the
Anthropic API key when one is set; else the no-account rules engine.

Both take a `LengthBudget`; the Claude engines are instructed to fit it, and it's hard-
enforced afterward in `tailor.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional, Protocol

from pydantic import BaseModel, Field

from . import relevance
from .job_description import JobDescription, trim_for_prompt
from .length import LengthBudget
from .models import Experience, Project, Resume, SkillCategory, TailoredResume

CLAUDE_CLI = "claude"

# Speed/quality tiers for the Claude tailoring backend — each maps to a (model, thinking)
# pair. Wall-clock benchmarked on a 1-page tailor (see DECISIONS.md #013). Extended thinking
# is the dominant cost: with it on, the model burns 10-21k output tokens reasoning before
# emitting the ~3k-token résumé JSON, so turning it OFF is the main speed lever; the model
# choice is secondary. Ordered fastest → highest-quality.
QUALITY_TIERS: dict[str, tuple[str, bool]] = {
    "fast": ("sonnet", False),     # ~30s  — Sonnet, no thinking
    "balanced": ("opus", False),   # ~40s  — Opus, no thinking (best quality under a minute)
    "max": ("opus", True),         # ~115s — Opus with thinking (the previous default)
}
DEFAULT_QUALITY = "balanced"

# Map the CLI quality-tier model aliases ("sonnet"/"opus") to Anthropic API model IDs for the
# API-key fallback backend (decision 111). The subscription path passes the alias to `claude
# --model`; the API path needs the full ID.
_API_MODELS = {"haiku": "claude-haiku-4-5-20251001",
               "sonnet": "claude-sonnet-5", "opus": "claude-opus-4-8"}

SYSTEM_PROMPT = """\
You are a resume-tailoring assistant. You are given a candidate's BASE RESUME (the \
single source of truth for everything true about the candidate) and a JOB DESCRIPTION. \
Produce a TAILORING PLAN — a delta, not a full resume: which entries to keep (by index), \
in what order, with rewritten bullets, reordered skills, and a rewritten summary. The \
application reconstructs the final resume from your plan; organizations, roles, dates, \
locations, project names/tech, education, and certifications are copied VERBATIM from \
the base resume, so never restate them.

Output shape (JSON):
- `experience` / `projects` / `activities`: arrays of {"i": <0-based index of the entry \
within that same section of the BASE RESUME>, "bullets": [...], "tailor_note": "..."}. \
Array order = the order entries should appear (most job-relevant first). Omit an entry's \
index entirely to drop it from the tailored resume.
- `skills`: the SAME categories as the base resume; within each you may reorder and drop \
items, but every item must appear in the base resume — never add one.
- `summary`: rewritten for this job, ONLY if the base resume has a summary; else null.

Hard rules — never break these:
- Use ONLY facts present in the base resume. Never invent or embellish organizations, \
roles, titles, dates, degrees, certifications, metrics, or skills the candidate does not \
have.
- Each entry's bullets must be truthful selections/rewordings of THAT entry's base \
bullets. You may reword, reorder, and omit to emphasize relevance and mirror the job's \
terminology where it is truthful, but you must not fabricate achievements or move a \
bullet between entries.

Bullet formatting (CRITICAL — measured by character count, which is reliable; word counts \
are not):
- STRONGLY PREFER single-line bullets. Every bullet must be EITHER a clean single line OR \
clearly 1.5+ lines — never in between. The single thing to eliminate is a bullet that \
spills just a word or two onto a second line.
- Follow the exact per-line character limits given in the length instruction below. Make \
one-line bullets FILL the line — get close to the one-line limit rather than leaving lots \
of empty space — but never cross into the forbidden slightly-over range.
- Keep every bullet within one entry the same length band (all one-line, or all 1.5+).
- Never pad with meaningless filler; if there isn't enough true content, make it a tight \
single line. Truthfulness over length.

What you SHOULD do:
- If the base resume has a summary, rewrite it to speak directly to this job (true facts \
only).
- Reorder and select experience, bullets, projects, activities, and skills so the most \
relevant material comes first. Order experience by RELEVANCE TO THIS JOB first — an entry \
whose role/domain matches the posting outranks an unrelated one regardless of dates (e.g. \
for a software-engineering job a software role must come before an unrelated tutoring or \
retail role, even if the unrelated one is more recent). Break ties by recency: among \
entries of comparable job-relevance, put the most recent nearer the top. This same \
relevance-first, recency-as-tiebreak order applies to projects and activities too.
- Each project may carry an `impact` score (1–5, technical impressiveness). Relevance to \
this job is the primary signal, but among comparably-relevant projects prefer higher-\
impact ones, and when the length budget forces you to drop projects, drop the \
lowest-impact, least-relevant ones first.
- Mirror the job description's vocabulary where the candidate genuinely has that \
experience (e.g. if the base resume says "built REST services" and the job says \
"microservices", you may say "built REST microservices" only if that is accurate).
- Make every bullet CONCRETE about the actual work and its result. Name the specific \
thing done — a feature built or shipped, a bug or class of bugs fixed, a system \
automated / migrated / integrated, a process optimized — plus the technology used and \
what changed because of it. Replace vague verbs ("worked on", "helped with", \
"responsible for", "assisted") with the specific action and outcome.
- QUANTIFY impact. The base resume is already full of real numbers — magnitudes, %, \
counts, scale, latency, revenue, time saved, users, requests/events per day, team/cohort \
size, number of features or fixes. Your FIRST duty on every bullet is to PRESERVE those \
figures: if a base bullet carries a number, the rewritten bullet MUST keep that number \
(rephrase the words freely, but never drop the metric). When the length budget forces a \
choice of WHICH bullets to keep, prefer the ones that carry a real magnitude over ones \
that don't — lead each entry with its most quantified, job-relevant bullet. Use ONLY \
numbers present in, or directly and safely implied by, the base resume: if a bullet has \
no factual basis for a metric, keep it specific and outcome-focused rather than inventing, \
estimating, or rounding up a number. A truthful bullet with no metric beats a fabricated \
figure — but never silently strip a metric the base resume already supports.
- For EVERY experience, project, and activity entry you include, set its `tailor_note` to \
ONE short sentence explaining why it's here and how you tailored it for THIS job — what \
made it relevant, why it's ordered where it is, or what you emphasized or cut. This is \
shown to the user for review and is NEVER printed on the resume.
- In `relevance_notes`, briefly explain what you emphasized or omitted and why.
"""


# --------------------------------------------------- tailoring delta (LLM I/O shape only)
# The model returns a PLAN referencing base-resume entries by index; the full TailoredResume
# is reconstructed in Python (decision 042). Structural fields (orgs, roles, dates, education,
# certifications) are copied from the base resume, so they can never be mangled and are never
# paid for as output tokens.


class DeltaEntry(BaseModel):
    i: int = Field(description="0-based index of the entry within its base-resume section.")
    bullets: list[str] = Field(default_factory=list,
                               description="The rewritten bullets for this entry, in order.")
    tailor_note: str = Field(default="", description="One short sentence: why this entry is "
                             "here and how it was tailored for THIS job (review-only).")


class TailorDelta(BaseModel):
    summary: Optional[str] = None
    experience: list[DeltaEntry] = Field(default_factory=list)
    projects: list[DeltaEntry] = Field(default_factory=list)
    activities: list[DeltaEntry] = Field(default_factory=list)
    skills: list[SkillCategory] = Field(default_factory=list)
    relevance_notes: list[str] = Field(default_factory=list)


def _delta_to_tailored(base: Resume, delta: TailorDelta) -> TailoredResume:
    """Reconstruct the full TailoredResume from the model's plan. Out-of-range or duplicate
    indices are ignored; empty bullets fall back to the entry's base bullets (the length
    budget caps them afterwards). Education/certifications are copied verbatim."""
    def pick(entries, picks, make):
        out, seen = [], set()
        for p in picks:
            if not (0 <= p.i < len(entries)) or p.i in seen:
                continue
            seen.add(p.i)
            out.append(make(entries[p.i], p))
        return out

    def exp(e: Experience, p: DeltaEntry) -> Experience:
        return Experience(organization=e.organization, role=e.role, location=e.location,
                          start=e.start, end=e.end, bullets=list(p.bullets) or list(e.bullets),
                          tailor_note=p.tailor_note or None)

    def proj(pr: Project, p: DeltaEntry) -> Project:
        return Project(name=pr.name, tech=pr.tech, bullets=list(p.bullets) or list(pr.bullets),
                       tailor_note=p.tailor_note or None)

    return TailoredResume(
        summary=(delta.summary if base.summary else None),  # summary only if the base has one
        skills=delta.skills or [c.model_copy(deep=True) for c in base.skills],
        experience=pick(base.experience, delta.experience, exp),
        projects=pick(base.projects, delta.projects, proj),
        activities=pick(base.activities, delta.activities, exp),
        education=[e.model_copy(deep=True) for e in base.education],
        certifications=list(base.certifications),
        relevance_notes=list(delta.relevance_notes),
    )


def _user_message(resume: Resume, jd: JobDescription, budget: LengthBudget,
                  emphasis: Optional[list[str]] = None) -> str:
    # Compact JSON (no indentation, null/empty fields dropped) — the résumé is the largest
    # prompt component and indent=2 was ~12% whitespace. The JD is boilerplate-trimmed.
    # `emphasis` is the ATS retry loop's feedback (ats_requirements): keywords the target
    # ATS screens for that the previous pass dropped — the candidate genuinely has them, so
    # they must reappear verbatim without inventing anything.
    emph = ""
    if emphasis:
        emph = (
            "\n\nATS SCREEN — the target applicant-tracking system screens this posting for "
            f"the following skills, which the candidate HAS but your previous draft omitted: "
            f"{', '.join(emphasis)}. Make each appear verbatim in the tailored résumé (in the "
            "skills list and, where truthful, a bullet). Do NOT invent experience — only "
            "surface what the base résumé already supports.\n"
        )
    return (
        "BASE RESUME (source of truth, JSON; reference entries by their 0-based index "
        "within each section):\n"
        f"{resume.model_dump_json(exclude_none=True, exclude_defaults=True)}\n\n"
        f"JOB DESCRIPTION — {jd.title} at {jd.company}:\n"
        f"{trim_for_prompt(jd.body)}\n\n"
        f"{budget.prompt()}"
        f"{emph}\n\n"
        "Produce the tailoring plan now."
    )


class TailorBackend(Protocol):
    name: str

    def tailor(
        self, resume: Resume, jd: JobDescription, budget: LengthBudget,
        emphasis: Optional[list[str]] = None,
    ) -> TailoredResume: ...


# ------------------------------------------------------------------ Claude (subscription)


class ClaudeUnavailableError(RuntimeError):
    """A Claude Code CLI call could not complete (failed, timed out, or CLI missing).

    Subclasses RuntimeError so existing callers that catch RuntimeError keep working."""


class ClaudeAuthError(ClaudeUnavailableError):
    """Claude Code CLI is not installed or not signed in."""


class ClaudeRateLimitError(ClaudeUnavailableError):
    """Subscription usage cap / rate limit / service overloaded — retryable after a wait."""


_RATE_LIMIT_MARKERS = (
    "rate limit", "usage limit", "usage cap", "hit your limit", "too many requests",
    "429", "overloaded", "quota", "capacity", "limit will reset",
    "out of extended usage",
)
_AUTH_MARKERS = (
    "not logged in", "please run /login", "/login", "authentication", "unauthorized",
    "401", "invalid api key", "expired",
)


def _classify_cli_failure(detail: str) -> type:
    """Map a failed CLI call's combined stderr+stdout to the exception class to raise."""
    low = detail.lower()
    if any(m in low for m in _RATE_LIMIT_MARKERS):
        return ClaudeRateLimitError
    if any(m in low for m in _AUTH_MARKERS):
        return ClaudeAuthError
    return ClaudeUnavailableError


def run_claude_cli(prompt: str, *, cli: str = CLAUDE_CLI,
                   model: Optional[str] = None, think: bool = True,
                   timeout: int = 300, system: Optional[str] = None,
                   json_schema: Optional[dict] = None,
                   activity: Optional[str] = None) -> str:
    """Run one prompt through the Claude Code CLI (subscription billing, not the API) and
    return the model's text. Raises ClaudeAuthError (CLI missing / not signed in),
    ClaudeRateLimitError (usage cap / rate limit), or ClaudeUnavailableError (anything
    else, incl. timeout) — all RuntimeError subclasses.

    `think=False` disables extended thinking (MAX_THINKING_TOKENS=0). Thinking is the
    dominant latency cost for a JSON-output task like tailoring — off is ~3-4x faster.

    The session is stripped to the bare model: no Claude Code system prompt, no tools, no
    MCP servers, no settings/CLAUDE.md. A default headless session carries ~40k tokens of
    coding-agent context per call; stripped, the same call carries only the prompt itself
    (~74x less overhead measured — see DECISIONS.md #034). `system` replaces the (empty)
    system prompt; `json_schema` makes the CLI enforce schema-valid JSON output.

    `activity` labels this call's token usage (tailoring / form-entry / judging / …) for the
    token accounting in `usage.record` (decision 095); it does not affect the call itself."""
    if shutil.which(cli) is None:
        raise ClaudeAuthError(
            "Claude Code CLI ('claude') not found. Install it and sign in to your Claude "
            "subscription (https://claude.com/product/claude-code)."
        )
    cmd = [cli, "--print", prompt, "--output-format", "json",
           "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--system-prompt", system or "You are a helpful assistant."]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if model:
        cmd += ["--model", model]
    # CLAUDESTATUS_IGNORE=1 tells the ClaudeStatus hook to ignore these headless,
    # programmatic sessions so they don't clutter its per-session light. Scoped to this
    # spawn only — interactive/human `claude` usage is unaffected.
    env = {**os.environ, "CLAUDESTATUS_IGNORE": "1"}
    if not think:
        env["MAX_THINKING_TOKENS"] = "0"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise ClaudeUnavailableError(f"Claude Code timed out ({timeout}s).") from e
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        tail = detail[-400:]
        exc = _classify_cli_failure(detail)
        if exc is ClaudeRateLimitError:
            raise ClaudeRateLimitError(
                f"Claude usage limit/rate limit hit (exit {proc.returncode}). The runner "
                f"will wait and retry; or wait for your usage window to reset. "
                f"Detail: {tail}"
            )
        if exc is ClaudeAuthError:
            raise ClaudeAuthError(
                f"Claude Code is not signed in (exit {proc.returncode}). "
                f"Run `claude` and /login, then retry. Detail: {tail}"
            )
        raise ClaudeUnavailableError(
            f"Claude Code failed (exit {proc.returncode}). Retry; if it persists, run "
            f"`claude` interactively to check it works. Detail: {tail}"
        )
    try:
        env = json.loads(proc.stdout)
        if isinstance(env, dict):
            # Capture this call's token usage (decision 095) before returning the text.
            from . import usage
            usage.record(env, activity=activity)
            if isinstance(env.get("result"), str):
                return env["result"]
    except json.JSONDecodeError:
        pass
    return proc.stdout


def run_anthropic_api(prompt: str, *, api_key: str, model: Optional[str] = None,
                      timeout: int = 300, system: Optional[str] = None,
                      activity: Optional[str] = None) -> str:
    """Run one prompt through the metered Anthropic **API** (the fallback engine, decision 111)
    with the user's own key, and return the model's text. Raises the same taxonomy as
    run_claude_cli — ClaudeAuthError / ClaudeRateLimitError / ClaudeUnavailableError — so
    callers handle either engine identically. Billed pay-per-token to the key's API account,
    NOT the Claude subscription (that path is Claude Code only). Thinking is left off: this is a
    structured-JSON task, and the 'respond with ONLY JSON' instruction + retry loop cover it."""
    try:
        import anthropic
    except ImportError as e:
        raise ClaudeUnavailableError(
            "The 'anthropic' package isn't installed. Reinstall dependencies "
            "(pip install -r requirements.txt) to use the API-key fallback."
        ) from e
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
    model_id = _API_MODELS.get((model or "opus").lower(), _API_MODELS["opus"])
    try:
        resp = client.messages.create(
            model=model_id, max_tokens=16000,
            system=system or "You are a helpful assistant.",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise ClaudeAuthError(
            "Anthropic API key was rejected (401). Re-connect the key in the account panel. "
            f"Detail: {e}"
        ) from e
    except anthropic.RateLimitError as e:
        raise ClaudeRateLimitError(
            "Anthropic API rate limit / quota hit. Wait and retry, or check your API account's "
            f"credit balance. Detail: {e}"
        ) from e
    except anthropic.APIStatusError as e:
        raise ClaudeUnavailableError(f"Anthropic API error (HTTP {e.status_code}). Detail: {e}") from e
    except Exception as e:
        raise ClaudeUnavailableError(f"Anthropic API call failed: {e}") from e
    # Token accounting (decision 095), best-effort — mirror the CLI envelope shape.
    try:
        from . import usage
        u = resp.usage
        usage.record({
            "usage": {
                "input_tokens": getattr(u, "input_tokens", 0),
                "output_tokens": getattr(u, "output_tokens", 0),
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
            },
            "modelUsage": {model_id: {}},
        }, activity=activity)
    except Exception:
        pass
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply (tolerates fences / stray prose)."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


class ClaudeCodeBackend:
    """Tailor via the Claude Code CLI — uses the Claude subscription, not the API."""

    name = "claude-code"

    def __init__(self, cli: str = CLAUDE_CLI, model: Optional[str] = None,
                 think: bool = True):
        self.cli = cli
        self.model = model
        self.think = think

    def tailor(self, resume: Resume, jd: JobDescription, budget: LengthBudget,
               emphasis: Optional[list[str]] = None) -> TailoredResume:
        if shutil.which(self.cli) is None:
            raise RuntimeError(
                "Claude Code CLI ('claude') not found. Install it and sign in to your "
                "Claude subscription (https://claude.com/product/claude-code), or use the "
                "'rules' engine."
            )
        base = (
            _user_message(resume, jd, budget, emphasis)
            + "\n\nOutput format: respond with ONLY a single JSON object for the tailoring "
            "plan — no explanation, no markdown code fences."
        )
        last_err: Optional[Exception] = None
        for attempt in range(2):
            prompt = base if attempt == 0 else (
                base + "\n\nYour previous reply was not a valid tailoring plan. Return ONLY the JSON object."
            )
            raw = self._run(prompt)
            try:
                delta = TailorDelta.model_validate_json(_extract_json(raw))
                return _delta_to_tailored(resume, delta)
            except Exception as e:  # includes pydantic + json errors
                last_err = e
        raise RuntimeError(f"Claude Code did not return a valid tailoring plan: {last_err}")

    def _run(self, prompt: str) -> str:
        return run_claude_cli(prompt, cli=self.cli, model=self.model, think=self.think,
                              system=SYSTEM_PROMPT, activity="tailoring",
                              json_schema=TailorDelta.model_json_schema())


class AnthropicAPIBackend:
    """Fallback: tailor via the metered Anthropic API with the user's key (decision 111)."""

    name = "anthropic-api"

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model  # a "sonnet"/"opus" alias; mapped to an API id in run_anthropic_api

    def tailor(self, resume: Resume, jd: JobDescription, budget: LengthBudget,
               emphasis: Optional[list[str]] = None) -> TailoredResume:
        base = (
            _user_message(resume, jd, budget, emphasis)
            + "\n\nOutput format: respond with ONLY a single JSON object for the tailoring "
            "plan — no explanation, no markdown code fences."
        )
        last_err: Optional[Exception] = None
        for attempt in range(2):
            prompt = base if attempt == 0 else (
                base + "\n\nYour previous reply was not a valid tailoring plan. Return ONLY the JSON object."
            )
            raw = run_anthropic_api(prompt, api_key=self.api_key, model=self.model,
                                    system=SYSTEM_PROMPT, activity="tailoring")
            try:
                delta = TailorDelta.model_validate_json(_extract_json(raw))
                return _delta_to_tailored(resume, delta)
            except Exception as e:  # includes pydantic + json errors
                last_err = e
        raise RuntimeError(f"Anthropic API did not return a valid tailoring plan: {last_err}")


# --------------------------------------------------------------------------- Rules


class RulesBackend:
    """Deterministic, LLM-free tailoring: reorder/select by keyword relevance."""

    name = "rules"

    def tailor(self, resume: Resume, jd: JobDescription, budget: LengthBudget,
               emphasis: Optional[list[str]] = None) -> TailoredResume:
        # `emphasis` is ignored: this engine is deterministic and already surfaces every
        # JD-mentioned skill it can, so re-running with feedback changes nothing.
        jd_lower = jd.body.lower()
        jd_tokens = relevance.tokens(jd.body)
        terms = relevance.skill_terms(resume)

        def score(text: str) -> int:
            return relevance.text_score(text, terms, jd_lower, jd_tokens)

        # Skills: within each category, move job-mentioned items to the front.
        skills = [
            SkillCategory(
                category=cat.category,
                items=sorted(
                    cat.items,
                    key=lambda s: (not relevance.mentions(s, jd_lower, jd_tokens), cat.items.index(s)),
                ),
            )
            for cat in resume.skills
        ]

        def _note(text: str) -> str:
            """Deterministic per-entry rationale (rules engine has no LLM to explain itself)."""
            hits = sorted({t for t in terms if relevance.mentions(t, jd_lower, jd_tokens) and t.lower() in text.lower()})
            if hits:
                return "Matched the job on: " + ", ".join(hits) + " — surfaced accordingly."
            return "No direct job-keyword match; kept for completeness and recency."

        def sort_entries(entries):
            scored = [
                (score(" ".join([e.role, e.organization, *e.bullets])), i, e)
                for i, e in enumerate(entries)
            ]
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [
                e.model_copy(update={"tailor_note": _note(" ".join([e.role, e.organization, *e.bullets]))})
                for _, _, e in scored
            ]

        def sort_projects(projects: list[Project]) -> list[Project]:
            scored = [
                (score(" ".join([p.name, p.tech or "", *p.bullets])), i, p)
                for i, p in enumerate(projects)
            ]
            # Relevance first; break ties toward the more technically impressive project.
            scored.sort(key=lambda t: (-t[0], -(t[2].impact or 0), t[1]))
            return [
                p.model_copy(update={"tailor_note": _note(" ".join([p.name, p.tech or "", *p.bullets]))})
                for _, _, p in scored
            ]

        matched = sorted({s for s in terms if relevance.mentions(s, jd_lower, jd_tokens)})
        notes = [
            "Rules-based tailoring (no LLM): sections were reordered/selected by keyword "
            "relevance; wording is unchanged from your resume.",
        ]
        if matched:
            notes.append("Job-matched skills surfaced: " + ", ".join(matched) + ".")

        return TailoredResume(
            summary=resume.summary,
            skills=skills,
            experience=sort_entries(resume.experience),
            projects=sort_projects(resume.projects),
            activities=sort_entries(resume.activities),
            education=resume.education,
            certifications=resume.certifications,
            relevance_notes=notes,
        )


# --------------------------------------------------------------------------- selection


def claude_code_available() -> bool:
    return shutil.which(CLAUDE_CLI) is not None


def select_backend(name: str = "auto", quality: str = DEFAULT_QUALITY) -> TailorBackend:
    """Return a backend by name, or the best available one for `auto`.

    `auto` prefers Claude Code (subscription — PRIMARY); if it's absent it uses the Anthropic
    API key (FALLBACK) when one is stored; else the no-account rules engine (decision 111).
    `quality` (fast|balanced|max) picks the Claude speed/quality tier — model + whether extended
    thinking is on (thinking applies to the Claude Code path only); no effect on rules.
    """
    from . import auth  # lazy: reads the keychain-stored fallback API key
    name = name.lower()
    model, think = QUALITY_TIERS.get(quality, QUALITY_TIERS[DEFAULT_QUALITY])
    if name in ("claude-code", "claude"):
        return ClaudeCodeBackend(model=model, think=think)
    if name in ("anthropic-api", "api"):
        key = auth.get_api_key()
        if not key:
            raise ValueError("No Anthropic API key is set. Add one in the account panel, or "
                             "install Claude Code to use your subscription.")
        return AnthropicAPIBackend(api_key=key, model=model)
    if name == "rules":
        return RulesBackend()
    if name == "auto":
        if claude_code_available():
            return ClaudeCodeBackend(model=model, think=think)      # PRIMARY: subscription
        key = auth.get_api_key()
        if key:
            return AnthropicAPIBackend(api_key=key, model=model)    # FALLBACK: metered API
        return RulesBackend()                                       # last resort: no account
    raise ValueError(f"Unknown backend {name!r} (use claude-code|anthropic-api|rules|auto).")

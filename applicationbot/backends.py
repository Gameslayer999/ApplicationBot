"""Pluggable tailoring backends.

Two engines implement the same interface (see DECISIONS.md #011):

- ClaudeCodeBackend — tailors via the **Claude Code CLI** (`claude -p`), which runs on the
  user's Claude **subscription** (Pro/Max included programmatic usage), NOT the metered
  Claude API. Requires Claude Code installed and signed in. Best quality.
- RulesBackend — no LLM at all. Keyword-matches the job description and reorders/selects
  skills, experience, projects, and activities by relevance. Zero deps, no account, no
  network, deterministic, never invents — but can't reword prose.

`select_backend("auto")` uses Claude Code if the `claude` CLI is present, else rules.

Both take a `LengthBudget`; the Claude engine is instructed to fit it, and it's hard-
enforced afterward in `tailor.py`.

Note: we deliberately do NOT call the `anthropic` SDK / `api.anthropic.com` — that would
bill the API. Subscription usage is only available through Claude's own tooling, so the
Claude path shells out to Claude Code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Optional, Protocol

from . import relevance
from .job_description import JobDescription
from .length import LengthBudget
from .models import Project, Resume, SkillCategory, TailoredResume

CLAUDE_CLI = "claude"

SYSTEM_PROMPT = """\
You are a resume-tailoring assistant. You are given a candidate's BASE RESUME (the \
single source of truth for everything true about the candidate) and a JOB DESCRIPTION. \
Produce a tailored version of the resume that maximizes relevance to the job while \
staying faithful to the candidate's own resume format.

Hard rules — never break these:
- Use ONLY facts present in the base resume. Never invent or embellish organizations, \
roles, titles, dates, degrees, certifications, metrics, or skills the candidate does not \
have.
- Skills stay grouped under the SAME categories as the base resume. Within each category \
you may reorder and drop items, but every item must appear in the base resume — never add \
one.
- Each experience and activity entry's organization, role, start, and end MUST match the \
base resume exactly. You may reword, reorder, and omit bullets to emphasize relevance and \
mirror the job's terminology where it is truthful, but you must not fabricate \
achievements.
- Projects' name and tech must match the base resume.
- Only include a `summary` if the base resume has one; otherwise leave it null.
- Do not omit relevant education.

Preserve format:
- Keep roughly the same set of sections and a similar overall length as the base resume. \
This tailors the *content*; it should still look like the same person's resume.

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
relevant material comes first.
- Mirror the job description's vocabulary where the candidate genuinely has that \
experience (e.g. if the base resume says "built REST services" and the job says \
"microservices", you may say "built REST microservices" only if that is accurate).
- In `relevance_notes`, briefly explain what you emphasized or omitted and why.
"""


def _user_message(resume: Resume, jd: JobDescription, budget: LengthBudget) -> str:
    return (
        "BASE RESUME (source of truth, JSON):\n"
        f"{resume.model_dump_json(indent=2)}\n\n"
        f"JOB DESCRIPTION — {jd.title} at {jd.company}:\n"
        f"{jd.body}\n\n"
        f"{budget.prompt()}\n\n"
        "Produce the tailored resume now."
    )


class TailorBackend(Protocol):
    name: str

    def tailor(
        self, resume: Resume, jd: JobDescription, budget: LengthBudget
    ) -> TailoredResume: ...


# ------------------------------------------------------------------ Claude (subscription)


def run_claude_cli(prompt: str, *, cli: str = CLAUDE_CLI,
                   model: Optional[str] = None, timeout: int = 300) -> str:
    """Run one prompt through the Claude Code CLI (subscription billing, not the API) and
    return the model's text. Raises RuntimeError if the CLI is missing or fails."""
    if shutil.which(cli) is None:
        raise RuntimeError(
            "Claude Code CLI ('claude') not found. Install it and sign in to your Claude "
            "subscription (https://claude.com/product/claude-code)."
        )
    cmd = [cli, "--print", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Claude Code timed out ({timeout}s).") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"Claude Code failed (exit {proc.returncode}). Are you signed in? "
            f"Run `claude` and /login if not. Detail: "
            f"{(proc.stderr or proc.stdout).strip()[-400:]}"
        )
    try:
        env = json.loads(proc.stdout)
        if isinstance(env, dict) and isinstance(env.get("result"), str):
            return env["result"]
    except json.JSONDecodeError:
        pass
    return proc.stdout


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

    def __init__(self, cli: str = CLAUDE_CLI, model: Optional[str] = None):
        self.cli = cli
        self.model = model

    def tailor(self, resume: Resume, jd: JobDescription, budget: LengthBudget) -> TailoredResume:
        if shutil.which(self.cli) is None:
            raise RuntimeError(
                "Claude Code CLI ('claude') not found. Install it and sign in to your "
                "Claude subscription (https://claude.com/product/claude-code), or use the "
                "'rules' engine."
            )
        base = (
            SYSTEM_PROMPT
            + "\n\n"
            + _user_message(resume, jd, budget)
            + "\n\nOutput format: respond with ONLY a single JSON object for the tailored "
            "resume — no explanation, no markdown code fences. It must match this JSON "
            "schema:\n"
            + json.dumps(TailoredResume.model_json_schema())
        )
        last_err: Optional[Exception] = None
        for attempt in range(2):
            prompt = base if attempt == 0 else (
                base + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object."
            )
            raw = self._run(prompt)
            try:
                return TailoredResume.model_validate_json(_extract_json(raw))
            except Exception as e:  # includes pydantic + json errors
                last_err = e
        raise RuntimeError(f"Claude Code did not return a valid tailored resume: {last_err}")

    def _run(self, prompt: str) -> str:
        return run_claude_cli(prompt, cli=self.cli, model=self.model)


# --------------------------------------------------------------------------- Rules


class RulesBackend:
    """Deterministic, LLM-free tailoring: reorder/select by keyword relevance."""

    name = "rules"

    def tailor(self, resume: Resume, jd: JobDescription, budget: LengthBudget) -> TailoredResume:
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

        def sort_entries(entries):
            scored = [
                (score(" ".join([e.role, e.organization, *e.bullets])), i, e)
                for i, e in enumerate(entries)
            ]
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [e for _, _, e in scored]

        def sort_projects(projects: list[Project]) -> list[Project]:
            scored = [
                (score(" ".join([p.name, p.tech or "", *p.bullets])), i, p)
                for i, p in enumerate(projects)
            ]
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [p for _, _, p in scored]

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


def select_backend(name: str = "auto") -> TailorBackend:
    """Return a backend by name, or the best available one for `auto`.

    `auto` uses Claude Code (subscription) when the `claude` CLI is present, else the
    no-account rules engine.
    """
    name = name.lower()
    if name in ("claude-code", "claude"):
        return ClaudeCodeBackend()
    if name == "rules":
        return RulesBackend()
    if name == "auto":
        return ClaudeCodeBackend() if claude_code_available() else RulesBackend()
    raise ValueError(f"Unknown backend {name!r} (use claude-code|rules|auto).")

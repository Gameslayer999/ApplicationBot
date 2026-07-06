"""CLI: tailor a base resume to a job description and print/write the Markdown result.

Usage:
    python -m applicationbot.cli JD_FILE [--resume RESUME.yaml] [--out OUT.md]

JD_FILE is a job-description fixture (Markdown + optional YAML front matter).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .job_description import load_job_description
from .length import LengthBudget
from .render import render_markdown
from .resume import load_resume
from .tailor import tailor_resume

DEFAULT_RESUME = "examples/sample_resume.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tailor a resume to a job description.")
    parser.add_argument("jd_file", help="Path to a job-description fixture (Markdown).")
    parser.add_argument(
        "--resume",
        default=DEFAULT_RESUME,
        help=f"Base resume YAML (default: {DEFAULT_RESUME}).",
    )
    parser.add_argument(
        "--out",
        help="Write the tailored resume here instead of stdout.",
    )
    parser.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "claude-code", "rules"],
        help="Tailoring engine. auto = Claude via your subscription (Claude Code) if "
        "available, else the no-LLM rules engine (default: auto). claude-code uses your "
        "Claude subscription, not the paid API.",
    )
    parser.add_argument(
        "--quality",
        default="balanced",
        choices=["fast", "balanced", "max"],
        help="Claude engine speed/quality tier (default: balanced). fast = Sonnet, ~30s; "
        "balanced = Opus, ~40s; max = Opus with deep reasoning, ~2 min. No effect on the "
        "rules engine.",
    )
    parser.add_argument(
        "--pages",
        type=float,
        default=1.0,
        help="Target résumé length in pages (default: 1.0). Controls how much is kept.",
    )
    parser.add_argument(
        "--line-chars",
        type=int,
        default=100,
        help="Characters that fit on one line at your resume/ATS width (default: 100). "
        "Controls one-line bullet length.",
    )
    args = parser.parse_args(argv)

    resume = load_resume(args.resume)
    jd = load_job_description(args.jd_file)

    print(f"Tailoring {resume.contact.name}'s resume to: {jd.title} @ {jd.company} …",
          file=sys.stderr)
    result = tailor_resume(
        resume, jd, backend=args.backend,
        budget=LengthBudget(pages=args.pages, line_chars=args.line_chars),
        quality=args.quality,
    )
    print(f"Backend: {result.backend} · target {result.pages:g} page(s)", file=sys.stderr)
    # Show the applicant's links (LinkedIn/GitHub/portfolio) from the apply profile when the résumé
    # header itself has none, so the rendered output carries them. Best-effort (profile may not exist).
    try:
        from .apply_profile import load_profile, resume_with_profile_links
        resume = resume_with_profile_links(resume, load_profile())
    except Exception:
        pass
    if args.out and args.out.lower().endswith(".pdf"):
        from .pdf import render_pdf
        Path(args.out).write_bytes(render_pdf(resume, result.tailored))
        print(f"Wrote {args.out}", file=sys.stderr)
    elif args.out:
        Path(args.out).write_text(render_markdown(resume, result.tailored), encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(render_markdown(resume, result.tailored))

    if result.tailored.relevance_notes:
        print("\nRelevance notes:", file=sys.stderr)
        for note in result.tailored.relevance_notes:
            print(f"  - {note}", file=sys.stderr)

    if result.warnings:
        print("\n⚠ Factual-drift warnings (review before use):", file=sys.stderr)
        for w in result.warnings:
            print(f"  - {w}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

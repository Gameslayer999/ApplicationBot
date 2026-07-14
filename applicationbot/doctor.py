"""`python -m applicationbot.doctor` — one command that checks whether this clone is ready
to discover, tailor, and apply, and points at the exact fix for anything missing (UI
Principles #1/#3: setup should already be done; every error leads to the fix).

Adopted from ApplyPilot's `doctor` (ApplyPilot survey, decision 048). Read-only — it never
creates or edits files; it diagnoses and tells you the one next step. Exit code 0 when every
*required* check passes, 1 otherwise (so it can gate CI / a first-run script).

Checks: Claude Code CLI signed in · Playwright Chromium installed · résumé loads · applicant
profile loads · discovery has at least one source · (info) submit safety state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_RESUME = "profile/resume.yaml"
_PROFILE = "profile/application_profile.yaml"
_FILTERS = "profile/discovery.yaml"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str          # what we actually found (Guideline #11: the specific fact)
    fix: str = ""        # the one next step when not ok (empty when ok)
    required: bool = True  # a failed OPTIONAL check is a ⚠ warning, not a ✗ failure

    @property
    def mark(self) -> str:
        return "✓" if self.ok else ("✗" if self.required else "⚠")


def _check_claude() -> Check:
    from . import backends

    if backends.claude_code_available():
        return Check("Claude Code CLI", True, "installed and signed in")
    return Check(
        "Claude Code CLI", False,
        "not found or not signed in",
        "Install Claude Code (https://claude.com/product/claude-code), then run `claude` and "
        "use /login. Discovery's fit judge and résumé tailoring need it; the `rules` backend "
        "works without it but with lower quality.",
    )


def _check_playwright() -> Check:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return Check("Playwright (browser autofill)", False, f"python package not importable: {e}",
                     "pip install -r requirements.txt")
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
        if path and Path(path).exists():
            return Check("Playwright (browser autofill)", True, "Chromium installed")
        return Check("Playwright (browser autofill)", False,
                     f"Chromium binary missing ({path or 'no path'})",
                     "playwright install chromium")
    except Exception as e:
        return Check("Playwright (browser autofill)", False, f"Chromium not usable: {e}",
                     "playwright install chromium")


def _check_resume(path: str) -> Check:
    p = Path(path)
    if not p.exists():
        return Check("Résumé (profile/resume.yaml)", False, f"{path} does not exist",
                     "Create it (copy examples/sample_resume.yaml to profile/resume.yaml and edit, "
                     "or use the Profile tab in `python -m applicationbot.web`). It is the source "
                     "of truth for tailoring and discovery ranking.")
    try:
        from .resume import load_resume

        r = load_resume(path)
        nexp = len(getattr(r, "experience", []) or [])
        nsk = sum(len(getattr(c, "items", []) or []) for c in (getattr(r, "skills", []) or []))
        return Check("Résumé (profile/resume.yaml)", True,
                     f"loads — {nexp} experience entr{'y' if nexp == 1 else 'ies'}, {nsk} skills")
    except Exception as e:
        return Check("Résumé (profile/resume.yaml)", False, f"{path} won't parse: {e}",
                     f"Fix the YAML in {path} (see examples/sample_resume.yaml for the shape).")


def _check_profile(path: str) -> Check:
    p = Path(path)
    if not p.exists():
        return Check("Applicant profile", False, f"{path} does not exist",
                     "Create it (the Profile tab in `python -m applicationbot.web` writes it) — "
                     "name, contact, work authorization, and answers to screening questions come "
                     "from here.", required=False)
    try:
        from .apply_profile import load_profile

        load_profile(path)
        return Check("Applicant profile", True, f"{path} loads")
    except Exception as e:
        return Check("Applicant profile", False, f"{path} won't parse: {e}",
                     f"Fix the YAML in {path}.", required=False)


def _check_discovery(path: str) -> Check:
    from .filters import load_filters

    try:
        f = load_filters(path)
    except Exception as e:
        return Check("Discovery sources", False, f"{path} won't parse: {e}", f"Fix the YAML in {path}.")
    has_adzuna = bool(f.adzuna.app_id or os.environ.get("ADZUNA_APP_ID"))
    parts = []
    if f.boards:
        parts.append(f"{len(f.boards)} board(s)")
    if f.career_sites:
        parts.append(f"{len(f.career_sites)} career site(s)")
    if has_adzuna:
        parts.append("Adzuna aggregator")
    if f.early_career.enabled:
        parts.append("early-career feeds")
    if parts:
        return Check("Discovery sources", True, ", ".join(parts) + f" in {path}")
    return Check(
        "Discovery sources", False, f"no sources configured in {path}",
        "Add at least one source to profile/discovery.yaml, e.g.:\n"
        "      boards:\n        - {ats: greenhouse, token: stripe}\n"
        "    or `career_sites:` URLs, or set ADZUNA_APP_ID/ADZUNA_APP_KEY, or enable "
        "early_career. Without a source, discovery finds nothing.",
    )


def _captcha_note() -> str:
    """One-line CAPTCHA auto-solve state (decision 049) appended to the safety check."""
    from . import captcha

    cfg = captcha.load_config()
    if not cfg.enabled:
        return " · CAPTCHA auto-solve off"
    keyed = "key set" if os.environ.get("CAPSOLVER_API_KEY") else "CAPSOLVER_API_KEY MISSING"
    sites = ", ".join(cfg.sites) if cfg.sites else "no sites allowlisted"
    return f" · CAPTCHA auto-solve ON ({sites}; {keyed})"


def _check_safety() -> Check:
    from .safety import load_gate

    gate = load_gate()
    killed = gate.kill_file.exists()
    cap = _captcha_note()
    if gate.armed and not killed:
        return Check("Submit safety", True,
                     f"ARMED — real submissions ON (cap {gate.max_submissions_per_run}/run). "
                     f"Create {gate.kill_file} to halt.{cap}", required=False)
    if killed:
        return Check("Submit safety", True,
                     f"kill switch engaged ({gate.kill_file} exists) — no submissions{cap}",
                     required=False)
    return Check("Submit safety", True,
                 f"dry-run (default) — fills and records, never submits{cap}", required=False)


def _check_mailbox() -> Check:
    from . import mailbox

    s = mailbox.link_status()
    if s["linked"]:
        how = "Gmail, read-only" if s.get("auth") == "oauth" else s["host"]
        return Check("Bot email (Workday verification)", True,
                     f"linked: {s['email']} ({how}) via {s['source']}", required=False)
    return Check(
        "Bot email (Workday verification)", False, "not linked",
        "Connect a bot inbox so Workday account verification is automatic — the Profile tab's "
        "'Bot email' section (one-click Connect Gmail), or "
        "`python -m applicationbot.mailbox connect-gmail --client-id … --client-secret …`. "
        "Optional until you apply on account-gated portals (Workday).", required=False)


def run_checks(*, resume_path: str = _RESUME, profile_path: str = _PROFILE,
               filters_path: str = _FILTERS) -> list[Check]:
    """All readiness checks, in order. Pure aside from reading the environment/filesystem —
    the external-tool probes live in `_check_*` helpers so tests can monkeypatch them."""
    return [
        _check_claude(),
        _check_playwright(),
        _check_resume(resume_path),
        _check_profile(profile_path),
        _check_discovery(filters_path),
        _check_mailbox(),
        _check_safety(),
    ]


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Check whether this ApplicationBot clone is ready to run.")
    ap.add_argument("--resume", default=_RESUME)
    ap.add_argument("--profile", default=_PROFILE)
    ap.add_argument("--filters", default=_FILTERS)
    args = ap.parse_args(argv)

    checks = run_checks(resume_path=args.resume, profile_path=args.profile, filters_path=args.filters)
    print("ApplicationBot doctor\n")
    for c in checks:
        print(f"  {c.mark} {c.name}: {c.detail}")
        if not c.ok and c.fix:
            print(f"      → {c.fix}")

    failed = [c for c in checks if not c.ok and c.required]
    warned = [c for c in checks if not c.ok and not c.required]
    print()
    if failed:
        print(f"{len(failed)} required check(s) failed — fix the → steps above, then rerun "
              "`python -m applicationbot.doctor`.")
        return 1
    print("Ready." + (f" ({len(warned)} optional warning(s) above.)" if warned else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rebuild the apply profile from the per-application archives (idempotent, safe to re-run).

Why this exists: `profile/application_profile.yaml` is git-ignored PII with no backup, and a test
that stubbed `web.load_resume` made every `profile/*.yaml` validate as a résumé, so saving the
Profile screen wrote a résumé over it and destroyed it (decision 159). The archives under
`profile/applications/*/report.json` are the only surviving record — each one lists the answers a
run actually submitted, tagged with the source that produced them, so a `resolver`-sourced answer
is a value that came *out of the profile* and can be put back.

Only fields with DIRECT evidence are restored. Preference fields with no unambiguous archived
answer (desired salary, start date, years of experience, office preferences, commute radius) are
reported as unrecoverable rather than guessed — a wrong preference is submitted to real employers.

Existing non-empty values are never overwritten, so this can be re-run after manual edits.

Run:  python -m scripts.recover_apply_profile [--apply]   (dry-run without --apply)
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from applicationbot.apply_profile import DEFAULT_PATH, load_profile, save_profile

ARCHIVES = Path("profile/applications")

# label substring -> (profile field, how to turn the archived answer into the stored value).
# Matched against the archived label, lowercased. `None` from a converter means "no evidence".
_YES = lambda v: v.strip().lower().startswith("yes")  # noqa: E731


def _first(v: str) -> str:
    return v.strip()


RULES: list[tuple[str, str, object]] = [
    ("first name", "first_name", _first),
    ("last name", "last_name", _first),
    ("email", "email", _first),
    ("phone", "phone", _first),
    ("current location", "location", _first),
    ("linkedin url", "linkedin_url", _first),
    ("linkedin profile", "linkedin_url", _first),
    ("github url", "github_url", _first),
    ("gender", "gender", _first),
    ("pronouns", "pronouns", _first),
    ("race", "race_ethnicity", _first),
    ("veteran status", "veteran_status", _first),
    ("disability status", "disability_status", _first),
    ("legally authorized to work", "work_authorized", _YES),
    ("require visa sponsorship", "requires_sponsorship", _YES),
    ("require sponsorship", "requires_sponsorship", _YES),
    ("citizenship status", "us_citizen", lambda v: "u.s. citizen" in v.lower()),
    ("willing to relocate", "willing_to_relocate", _YES),
    ("intend to work remotely", "open_to_remote", _YES),
]

# Fields this script deliberately does NOT infer — no archived answer pins them unambiguously.
UNRECOVERABLE = ["portfolio_url", "desired_salary", "earliest_start_date", "years_experience",
                 "work_arrangement", "max_commute_miles", "preferred_locations",
                 "custom_answers (the answer bank)", "dropdown_aliases (learned dropdown matches)"]


# Source ranking. A `resolver` answer is the profile value VERBATIM; an `option:*` answer is that
# value after being matched onto whatever wording the form offered ("Asian (Not Hispanic or
# Latino)" submitted as "Asian"), so it is weaker evidence of what was actually stored.
_SOURCE_RANK = {"resolver": 0, "option:hint": 1, "option:literal": 1}


def _evidence() -> dict[str, Counter]:
    """field -> Counter of candidate values, from every archived resolver-sourced answer.
    Keyed (rank, value) so a verbatim resolver answer outranks a matched-option one."""
    found: dict[str, Counter] = defaultdict(Counter)
    for rj in sorted(ARCHIVES.glob("*/report.json")):
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for f in data.get("filled", []) or []:
            # Only answers the RESOLVER produced came from the profile. A `native` answer is the
            # ATS's own résumé-parse and an `option:*` one was matched from a list, so neither is
            # evidence of a stored value — except the citizenship dropdown, whose hint IS the flag.
            src = f.get("source", "")
            label, value = (f.get("label") or "").lower(), (f.get("value") or "").strip()
            if not value or src not in ("resolver", "option:hint", "option:literal"):
                continue
            for needle, field, conv in RULES:
                if needle in label:
                    out = conv(value)
                    if out not in (None, ""):
                        found[field][(_SOURCE_RANK[src], out)] += 1
                    break
    return found


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    if not ARCHIVES.is_dir():
        print(f"No archives at {ARCHIVES} — nothing to recover from.")
        return 1
    profile = load_profile()
    found = _evidence()

    changes: list[str] = []
    for field, counter in sorted(found.items()):
        # Best rank first, then most-seen: a verbatim resolver answer always beats a matched option.
        (_, value), hits = min(counter.items(), key=lambda kv: (kv[0][0], -kv[1]))
        current = getattr(profile, field, None)
        if current not in (None, "", [], {}):
            continue  # never overwrite a value that is already set
        setattr(profile, field, value)
        rejected = {v: n for (_, v), n in counter.items() if v != value}
        others = f"  (also saw {rejected})" if rejected else ""
        changes.append(f"RESTORE {field:22} = {value!r}   [{hits} archived run(s)]{others}")

    if not changes:
        print("Nothing to restore — every recoverable field already has a value.")
    else:
        print("\n".join(changes))
        print(f"\n{len(changes)} field(s) recoverable from {len(list(ARCHIVES.glob('*/report.json')))} "
              f"archived report(s).")
    print("\nNOT recoverable — re-enter these on the Profile screen if you had them:")
    for f in UNRECOVERABLE:
        print(f"  - {f}")
    if apply and changes:
        save_profile(profile)
        print(f"\nSaved → {DEFAULT_PATH}")
    elif changes:
        print("\nDry-run. Re-run with --apply to write the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

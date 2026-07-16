"""Prune stale/wrong entries from the apply profile's answer bank (idempotent, safe to re-run).

Older runs' semantic classifier sometimes learned a WRONG `maps_to` (e.g. a SpaceX "Employment
History" dropdown mapped to `work_authorized` → answered "Yes") and banked it. A banked mapping is
consulted in `AnswerResolver.resolve`, so it OVERRIDES the corrected structured rules. This script
clears a banked `maps_to` when it's no longer valid, and drops garbage questions — leaving the
answer text untouched. Run: `python -m scripts.prune_answer_bank [--apply]` (dry-run without --apply).
"""
from __future__ import annotations

import sys

from applicationbot import answer_bank
from applicationbot.apply import AnswerResolver, _norm
from applicationbot.apply_profile import DEFAULT_PATH, load_profile, save_profile
from applicationbot.resume import load_resume


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    resume_path = next((a for a in argv if a.endswith(".yaml")), "profile/resume.yaml")
    profile = load_profile()
    # Resolver with the bank DISABLED, to test whether a question is now answered by a structured
    # rule directly (which makes a banked mapping redundant/stale).
    bankless = profile.model_copy(update={"custom_answers": []})
    resolver = AnswerResolver(resume=load_resume(resume_path), profile=bankless)

    kept, notes = [], []
    for qa in profile.custom_answers:
        qn = _norm(qa.question)
        if len(qn) < 4:  # garbage capture ("yes", "no", stray tokens)
            notes.append(f"DROP  (garbage) {qa.question!r}")
            continue
        # Claude-DRAFTED answers to numeric-fact questions (salary, GPA) are fabrications —
        # older runs banked them before is_open_ended refused these. User-entered ones stay.
        if qa.generated and (qa.answer or "").strip() and not qa.maps_to \
                and any(t in qn for t in answer_bank._NUMERIC_FACT):
            notes.append(f"DROP  (drafted numeric fact) {qa.question[:60]!r} = {qa.answer!r}")
            continue
        if qa.maps_to:
            invalid = (
                not answer_bank.valid_mapping(qa.question, qa.maps_to)  # same gate as write time
                or resolver.resolve(qa.question) is not None  # a structured rule now answers it
            )
            if invalid:
                notes.append(f"CLEAR maps_to={qa.maps_to!r} on {qa.question[:60]!r}")
                qa = qa.model_copy(update={"maps_to": ""})
        kept.append(qa)

    if not notes:
        print("Answer bank already clean — nothing to prune.")
        return 0
    print("\n".join(notes))
    print(f"\n{len(notes)} change(s); {len(kept)} of {len(profile.custom_answers)} entries kept.")
    if apply:
        save_profile(profile.model_copy(update={"custom_answers": kept}))
        print(f"Saved → {DEFAULT_PATH}")
    else:
        print("Dry-run. Re-run with --apply to write the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

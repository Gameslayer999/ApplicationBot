"""Drop never-judged openings from the seen-openings ledger (idempotent, safe to re-run).

Before decision 146, `only_new` runs recorded EVERY keyword survivor into the ledger
(`profile/discovery_seen.json`) — including the postings past the `top_n` cut that Claude never
scored. Those postings are hidden from every future search while having never been considered,
which is why a goal-mode loop reports "no new matches" with a pool of unjudged roles sitting
behind the ledger. Decision 146 fixed the recording; this script repairs ledgers written before it.

An entry is KEPT only if the posting has a judged verdict in `profile/fit_history.jsonl` (which
holds one record per Claude-scored posting). Everything else is dropped, making those postings
eligible for judging on the next loop pass. Re-running after a prune is a no-op.

Run:  python -m scripts.prune_seen_ledger            # dry-run: report what would be dropped
      python -m scripts.prune_seen_ledger --apply    # write the pruned ledger
"""
from __future__ import annotations

import json
import sys

from applicationbot import discovery_seen, fit_learning
from applicationbot.discovery import canonical_url


def judged_urls() -> set[str]:
    """Canonical URLs Claude has actually scored, from the fit-history log."""
    return {canonical_url(r["url"]) for r in fit_learning.load() if r.get("url")}


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    path = discovery_seen.DEFAULT_PATH
    ledger = discovery_seen.load()
    if not ledger:
        print(f"No ledger at {path} — nothing to prune.")
        return 0

    judged = judged_urls()
    kept = {u: ts for u, ts in ledger.items() if u in judged}
    dropped = [u for u in ledger if u not in judged]

    print(f"ledger : {path}")
    print(f"entries: {len(ledger)}   judged: {len(kept)}   never judged: {len(dropped)}")
    for u in dropped[:10]:
        print(f"  drop  {u}")
    if len(dropped) > 10:
        print(f"  … and {len(dropped) - 10} more")

    if not dropped:
        print("Nothing to prune — every ledger entry has a judged verdict.")
        return 0
    if not apply:
        print("\nDry run. Re-run with --apply to write the pruned ledger.")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": discovery_seen._SCHEMA_VERSION, "seen": kept},
                               indent=2), encoding="utf-8")
    print(f"\nPruned: {len(ledger)} → {len(kept)} entries. "
          f"{len(dropped)} posting(s) are eligible for judging again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

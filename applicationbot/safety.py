"""Submission safety switch (decision 035, Agent Guideline #3).

Dry-run is the default everywhere. A real submit happens only when ALL of:
  * the user has deliberately armed the system — `armed: true` in git-ignored
    `profile/safety.yaml` (see `examples/safety.example.yaml`);
  * the global kill switch is NOT engaged — the file `profile/KILL` does not exist
    (creating it halts all submission immediately; delete it to resume);
  * the per-run submission cap has not been reached.

The checks run immediately before every submit click (`SafetyGate.may_submit`), so
engaging the kill switch mid-run stops the very next application.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAFETY = REPO_ROOT / "profile" / "safety.yaml"
DEFAULT_KILL = REPO_ROOT / "profile" / "KILL"


@dataclass
class SafetyGate:
    armed: bool = False
    max_submissions_per_run: int = 10
    kill_file: Path = DEFAULT_KILL
    submitted_this_run: int = 0

    def may_submit(self) -> tuple[bool, str]:
        """(allowed, reason). Call immediately before each submit — never cache the result."""
        if not self.armed:
            return False, ("not armed — dry-run default (Guideline #3). To submit for real, set "
                           "`armed: true` in profile/safety.yaml.")
        if self.kill_file.exists():
            return False, (f"kill switch engaged — {self.kill_file} exists. All submission is "
                           "halted; delete the file to resume.")
        if self.submitted_this_run >= self.max_submissions_per_run:
            return False, (f"per-run submission cap reached ({self.max_submissions_per_run}; "
                           "max_submissions_per_run in profile/safety.yaml).")
        return True, "armed"

    def record_submission(self) -> None:
        self.submitted_this_run += 1


def load_gate(path: str | Path = DEFAULT_SAFETY, kill_file: str | Path = DEFAULT_KILL) -> SafetyGate:
    """Load the safety switch. A missing/empty file means DISARMED — the safe default."""
    data: dict = {}
    p = Path(path)
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except Exception:
            data = {}  # an unreadable safety file must never arm the system
    return SafetyGate(
        armed=bool(data.get("armed", False)),
        max_submissions_per_run=int(data.get("max_submissions_per_run", 10)),
        kill_file=Path(kill_file),
    )

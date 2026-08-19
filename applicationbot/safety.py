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

from .paths import DATA_ROOT
DEFAULT_SAFETY = DATA_ROOT / "profile" / "safety.yaml"
DEFAULT_KILL = DATA_ROOT / "profile" / "KILL"


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


def save_max_submissions(n: int, path: str | Path = DEFAULT_SAFETY) -> int:
    """Write `max_submissions_per_run` (decision 178, the loop's submission cap), preserving every
    other key in the file — `armed` above all: editing a cap must never arm or disarm the system.
    A missing file is created DISARMED. Returns the value written.

    Rewrites the YAML, so any comments in the user's own safety.yaml are lost; it is skipped
    entirely when the value is already what was asked for, so a no-op save costs nothing."""
    n = max(1, int(n))
    p = Path(path)
    data: dict = {}
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except Exception:
            data = {}  # an unreadable file is replaced with an explicit, disarmed one
    if data.get("max_submissions_per_run") == n:
        return n
    data["max_submissions_per_run"] = n
    data.setdefault("armed", False)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return n


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


def save_arming(armed: bool, max_submissions: int | None = None,
                path: str | Path = DEFAULT_SAFETY) -> dict:
    """Arm or disarm the system, optionally setting the per-run cap in the same write, preserving
    every other key in the file (decision 185, the unattended night run).

    Returns the PREVIOUS ``{"armed": …, "max_submissions_per_run": …}`` so a caller that armed for
    one session can put the switch back exactly as it found it. A missing file reads as disarmed
    with the default cap.

    Arming is still a deliberate act the user asks for — nothing in the pipeline calls this on its
    own; it exists so `python -m applicationbot.night --arm` can do in one command what the user
    would otherwise hand-edit. `may_submit` is unchanged: the KILL file and the cap still gate
    every single submit."""
    p = Path(path)
    data: dict = {}
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except Exception:
            data = {}  # an unreadable safety file must never silently keep a stale arming
    previous = {"armed": bool(data.get("armed", False)),
                "max_submissions_per_run": int(data.get("max_submissions_per_run", 10))}
    data["armed"] = bool(armed)
    if max_submissions is not None:
        data["max_submissions_per_run"] = max(1, int(max_submissions))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return previous

#!/usr/bin/env python3
"""Dev auto-reloader — runs the ApplicationBot web UI and restarts it whenever a source file
changes, so local edits show up without a manual restart. The browser refreshes itself too (the
server injects a tiny poller when APPLICATIONBOT_DEV=1).

Not for production — it's spawned by `scripts/run.sh --dev` (or `scripts/dev.sh`). Stdlib only,
no dependencies. Any args (e.g. --port 9000) are forwarded to `applicationbot.web`.

    python scripts/dev_reload.py --port 8000
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH_DIRS = [ROOT / "applicationbot"]
POLL_SECONDS = 1.0


def signature() -> dict[str, float]:
    """mtimes of every watched .py file — compared each tick to detect edits/adds/removals."""
    sig: dict[str, float] = {}
    for base in WATCH_DIRS:
        for p in base.rglob("*.py"):
            try:
                sig[str(p)] = p.stat().st_mtime
            except FileNotFoundError:
                pass
    return sig


def spawn(args: list[str]) -> subprocess.Popen:
    env = dict(os.environ, APPLICATIONBOT_DEV="1")
    return subprocess.Popen([sys.executable, "-m", "applicationbot.web", *args], cwd=str(ROOT), env=env)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    args = sys.argv[1:]
    # Line-buffer so "restarting…" shows immediately even when piped to a log, not just a tty.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    print("→ Dev auto-reload ON — editing files in applicationbot/ restarts the server and "
          "refreshes your browser. Ctrl-C to stop.")
    proc = spawn(args)
    last = signature()
    try:
        while True:
            time.sleep(POLL_SECONDS)
            if proc.poll() is not None:  # server exited on its own (crash / syntax error) — relaunch
                print("↻ Server exited — relaunching…")
                proc = spawn(args)
                last = signature()
                continue
            cur = signature()
            if cur != last:
                changed = sorted(k for k in set(cur) | set(last) if cur.get(k) != last.get(k))
                names = ", ".join(Path(c).name for c in changed[:5]) + ("…" if len(changed) > 5 else "")
                print(f"↻ Change detected ({names}) — restarting server…")
                _stop(proc)
                proc = spawn(args)
                last = cur
    except KeyboardInterrupt:
        print("\nStopping.")
        _stop(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

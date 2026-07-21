"""PyInstaller entry point for the standalone ApplicationBot.app (see scripts/build_macapp.sh).

This runs only inside the frozen desktop bundle. It points the app's data directory at a per-user
location OUTSIDE the read-only bundle (so profile/résumé/history/DB persist across launches and
updates), then opens the native window. Running from source uses `python -m applicationbot.app`
instead and never touches this file.
"""
import os
import traceback
from pathlib import Path

# Where the user's data lives for the installed app: standard macOS per-user support directory.
_DATA = Path.home() / "Library" / "Application Support" / "ApplicationBot"
_DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("APPLICATIONBOT_DATA", str(_DATA))


def _log(msg: str) -> None:
    try:
        with open(_DATA / "desktop.log", "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    _log("=== desktop_main start ===")
    try:
        from applicationbot.app import main
        _log("imported applicationbot.app.main; launching window")
        rc = main()
        _log(f"main() returned {rc}")
        raise SystemExit(rc)
    except SystemExit:
        raise
    except BaseException:
        _log("FATAL:\n" + traceback.format_exc())
        raise

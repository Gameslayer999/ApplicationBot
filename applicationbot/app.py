"""applicationbot.app — ApplicationBot as a standalone desktop application.

Runs the exact same local web UI as `applicationbot.web`, but inside a native OS window
(pywebview → WKWebView on macOS, WebView2 on Windows) instead of a browser tab — so the whole
app is a double-clickable window with no browser chrome. The HTTP server binds to 127.0.0.1 only.

Everything the browser build has carries over unchanged, because it is the same UI: the dark
theme, the first-run walkthrough, and the dev auto-reload (in --dev the window reloads itself
when you edit code).

    python -m applicationbot.app          # open the app window (server on an auto-picked port)
    python -m applicationbot.app --dev    # + auto-reload the window when code changes
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import __version__, paths

REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOW_TITLE = "ApplicationBot"


def _serve_in_thread(host: str, port: int):
    """Start the web UI on a background daemon thread; return (server, url). port=0 auto-picks."""
    from .web import Handler

    server = ThreadingHTTPServer((host, port), Handler)
    actual_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{actual_port}"


def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)  # noqa: S310 (loopback only)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _free_port(host: str = "127.0.0.1") -> int:
    """Pick an available localhost port (bind to 0, read it back, release)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _spawn_server(port: int) -> subprocess.Popen:
    """Run the plain web UI server as a subprocess (production windowed mode). cwd is the user
    data dir so cwd-relative paths (profile/…) resolve there, not inside a read-only bundle."""
    return subprocess.Popen([sys.executable, "-m", "applicationbot.web", "--port", str(port)],
                            cwd=str(paths.DATA_ROOT))


def _ensure_chromium_bg() -> None:
    """First-run only, non-blocking: download the Apply-stage browser (Chromium) if it's missing,
    using the Playwright driver bundled in the app. The window opens immediately; the download
    happens quietly in the background so Apply works by the time the user gets there."""
    def work() -> None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                exe = p.chromium.executable_path
            if exe and Path(exe).exists():
                _log("chromium already installed")
                return
        except Exception:
            pass  # fall through and try to install
        _log("installing chromium in the background (first run)…")
        try:
            import playwright as _pw

            drv = Path(_pw.__file__).resolve().parent / "driver"
            node, cli = drv / "node", drv / "package" / "cli.js"
            subprocess.run([str(node), str(cli), "install", "chromium"], check=False)
            _log("chromium install finished")
        except Exception as e:  # never let this crash the app — Apply just won't work until fixed
            _log(f"chromium install failed: {e!r}")

    threading.Thread(target=work, daemon=True).start()


def _spawn_dev_server(port: int) -> subprocess.Popen:
    """Dev mode: run the server under the auto-reload supervisor as a subprocess, so a code edit
    restarts only the server (the window process stays open and reloads itself via
    /dev/reload-token). Keeps the native window from flickering on every save."""
    supervisor = REPO_ROOT / "scripts" / "dev_reload.py"
    return subprocess.Popen([sys.executable, str(supervisor), "--port", str(port)], cwd=str(REPO_ROOT))


def _log(msg: str) -> None:
    line = f"[applicationbot.app] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    # A frozen --windowed app has no console, so also append to a file under the data dir.
    try:
        with open(paths.DATA_ROOT / "desktop.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _augment_path() -> None:
    """A GUI-launched app (Finder/launchd) inherits a *minimal* PATH — typically only
    /usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin — that omits the user tool dirs where CLIs are
    actually installed. So `claude` (usually in ~/.local/bin), Homebrew, npm-global, and nvm are
    invisible to the frozen app even though they work in a terminal, and Claude Code reads as "not
    found". Widen PATH so the native window resolves `claude` the same way the browser build does
    (which inherits the terminal's full PATH). Additions are *appended*, so a user binary never
    shadows a system one. This is a no-op when run from a terminal (those dirs are already present)."""
    import glob
    import shutil

    existing = os.environ.get("PATH", "").split(os.pathsep)
    seen = set(existing)
    added: list[str] = []

    def add(d: str) -> None:
        d = d.strip()
        if d and d not in seen and os.path.isdir(d):
            added.append(d)
            seen.add(d)

    home = os.path.expanduser("~")
    for d in (f"{home}/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
              "/usr/local/bin", "/usr/local/sbin", f"{home}/bin", f"{home}/.npm-global/bin"):
        add(d)
    for d in sorted(glob.glob(f"{home}/.nvm/versions/node/*/bin")):
        add(d)
    if added:
        os.environ["PATH"] = os.pathsep.join(existing + added)

    # Exotic setups (asdf, custom prefixes): only if `claude` is *still* invisible, adopt the login
    # shell's PATH as a last resort. Skipped on the common path, so it costs nothing there.
    if shutil.which("claude") is None:
        shell = os.environ.get("SHELL")
        if shell and os.path.exists(shell):
            try:
                out = subprocess.run([shell, "-lc", 'printf %s "$PATH"'],
                                     capture_output=True, text=True, timeout=5).stdout
                base = os.environ.get("PATH", "").split(os.pathsep)
                seen2 = set(base)
                more = [d for d in out.split(os.pathsep) if d and d not in seen2 and os.path.isdir(d)]
                if more:
                    os.environ["PATH"] = os.pathsep.join(base + more)
            except Exception:
                pass
    _log(f"PATH prepared for CLI resolution; claude {'found' if shutil.which('claude') else 'NOT found'}")


def run(*, dev: bool = False, port: int = 0, host: str = "127.0.0.1") -> int:
    _augment_path()  # make user-installed CLIs (notably `claude`) visible to a GUI-launched app
    import webview  # lazy import: only the windowed entry needs the GUI dependency

    # How the web server runs depends on the shell:
    #  • Frozen app (PyInstaller): serve IN-THREAD. A frozen binary is its own `sys.executable`
    #    and can't be re-invoked as `python -m applicationbot.web`, so there's no subprocess to
    #    spawn; the in-thread server is self-contained and needs no external interpreter.
    #  • From source: run the server as a SUBPROCESS (robust vs pywebview's macOS event loop; in
    #    --dev it's the auto-reload supervisor so the window reloads itself on code changes).
    frozen = bool(getattr(sys, "frozen", False))
    server_proc = None
    if frozen and not dev:
        _ensure_chromium_bg()  # bundled app: fetch the Apply browser on first run, in the background
        _server, url = _serve_in_thread(host, port or 0)
        _log(f"in-thread server on {url} (frozen)")
    else:
        if not port:
            port = 8000 if dev else _free_port()
        url = f"http://{host}:{port}"
        server_proc = _spawn_dev_server(port) if dev else _spawn_server(port)
        _log(f"server pid {server_proc.pid} on {url} (dev={dev}); waiting for it to come up…")
        if not _wait_for_server(url):
            _log("server did not come up in time — aborting.")
            server_proc.terminate()
            return 1

    _log("server is up; opening the window…")
    webview.create_window(WINDOW_TITLE, url, width=1200, height=820, min_size=(940, 640))
    try:
        webview.start()  # blocks on the main thread until the window is closed
    finally:
        _log("window closed; stopping the server.")
        if server_proc is not None:  # in-thread server is a daemon and dies with the process
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ApplicationBot — standalone desktop app (native window).")
    ap.add_argument("--dev", action="store_true", help="auto-reload the window when code changes")
    ap.add_argument("--port", type=int, default=0, help="server port (0 = auto-pick a free one)")
    ap.add_argument("--version", action="version", version=f"ApplicationBot {__version__}")
    args = ap.parse_args(argv)
    return run(dev=args.dev, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())

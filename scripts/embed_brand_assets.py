#!/usr/bin/env python3
"""Embed brand assets into applicationbot/web.py as base64 data URIs.

Single source of truth for the in-HTML brand imagery so swapping a PNG never
means hand-pasting base64. Re-runnable and idempotent — run it again after
replacing any source PNG:

    python scripts/embed_brand_assets.py

It rewrites exactly two spots in web.py:
  1. The tab favicon  -> assets/app-icon-blue.png (always the blue app icon), 64px
  2. The brand logomark CSS block -> theme-matched tile logos, 96px:
       dark mode  -> assets/logo-darkmode.png  (dark-background tile)
       light mode -> assets/logo-lightmode.png (light-background tile)
"""
from __future__ import annotations

import base64
import io
import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "applicationbot" / "web.py"
ASSETS = ROOT / "assets"

FAVICON_SRC = ASSETS / "app-icon-blue.png"
LOGO_LIGHT_SRC = ASSETS / "logo-lightmode.png"   # light-background tile, for light mode
LOGO_DARK_SRC = ASSETS / "logo-darkmode.png"     # dark-background tile, for dark mode

FAVICON_PX = 64
LOGO_PX = 96


def encode(src: Path, px: int) -> str:
    if not src.exists():
        sys.exit(f"error: missing asset {src.relative_to(ROOT)}")
    im = Image.open(src).convert("RGBA")
    im = im.resize((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    b = buf.getvalue()
    print(f"  {src.relative_to(ROOT)} -> {px}x{px}, {len(b)} bytes")
    return base64.b64encode(b).decode("ascii")


def main() -> None:
    print("Encoding brand assets:")
    favicon_b64 = encode(FAVICON_SRC, FAVICON_PX)
    light_b64 = encode(LOGO_LIGHT_SRC, LOGO_PX)
    dark_b64 = encode(LOGO_DARK_SRC, LOGO_PX)

    html = WEB.read_text()

    # 1. Favicon — swap only the base64 payload inside the icon <link>.
    fav_pat = re.compile(
        r'(<link rel="icon" type="image/png" href="data:image/png;base64,)[^"]*(")'
    )
    html, n = fav_pat.subn(rf"\g<1>{favicon_b64}\g<2>", html)
    if n != 1:
        sys.exit(f"error: expected 1 favicon link, matched {n}")

    # 2. Brand logomark CSS block — regenerate the whole block (comment + var
    #    defs + theme mapping). Anchored on the comment start and the last rule
    #    so it round-trips cleanly on every run.
    block_pat = re.compile(
        r'(?P<indent>[ \t]*)/\* Brand logomark.*?'
        r':root\[data-theme="light"\][^\n]*',
        re.DOTALL,
    )
    m = block_pat.search(html)
    if not m:
        sys.exit("error: brand logomark CSS block not found")
    ind = m.group("indent")
    block = (
        f'{ind}/* Brand logomark — theme-matched tile logos: dark-background tile in dark mode,\n'
        f'{ind}   light-background tile in light mode (base64 @{LOGO_PX}px from assets/logo-{{dark,light}}mode.png).\n'
        f'{ind}   Regenerate with: python scripts/embed_brand_assets.py */\n'
        f'{ind}:root {{ --lm-lightmode:url("data:image/png;base64,{light_b64}"); '
        f'--lm-darkmode:url("data:image/png;base64,{dark_b64}"); --lm:var(--lm-lightmode); }}\n'
        f'{ind}@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --lm:var(--lm-darkmode); }} }}\n'
        f'{ind}:root[data-theme="dark"] {{ --lm:var(--lm-darkmode); }}\n'
        f'{ind}:root[data-theme="light"] {{ --lm:var(--lm-lightmode); }}'
    )
    html = html[: m.start()] + block + html[m.end():]

    WEB.write_text(html)
    print(f"Wrote {WEB.relative_to(ROOT)} (favicon=blue app icon, logo=theme-matched tiles).")


if __name__ == "__main__":
    main()

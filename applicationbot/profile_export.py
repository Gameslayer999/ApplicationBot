"""Export the user's portable setup as a single .zip (decision 188).

A profile is the work a user does *once*: their applicant answers, their résumé(s), the
filters that decide what gets discovered, and the résumé PDFs kept for reuse. Everything
else under `profile/` is either regenerable (caches), a record of what THIS machine did
(applications, tailored PDFs, fit history), or local-only state that must never travel —
the mailbox link and the arming switch. So the export is an **allowlist**, not a copy of
the folder minus a denylist: a config file added later is left out until someone decides
it belongs, which fails closed instead of leaking.

Layout of the zip::

    manifest.json               format/version/exported_at, what's inside, what was left out
    application_profile.yaml    identity, eligibility, EEO, answer bank, dropdown aliases
    discovery.yaml              the search filters
    <name>.yaml                 every profile/*.yaml that validates as a Résumé
    uploads/<name>.pdf(+.meta)  the résumé documents kept for as-is reuse (decision 152)

The layout mirrors `profile/` exactly, so **unzipping the archive into `profile/` restores the
setup** with no import step — that is the whole reason résumés sit at the top level rather than
under a tidier `resumes/`. Uploads are content-addressed (`<slug>-<sha1>.pdf`), so they carry no
absolute path and land correctly wherever they're unpacked.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__
from .paths import data_path
from .resume import load_resume

FORMAT = 1

#: Config files that ARE the portable setup, exported at the top level of the zip.
CONFIG_FILES = ("application_profile.yaml", "discovery.yaml")

#: What is deliberately left out, and why — carried in the manifest and shown in the UI so
#: the user is never left guessing what a "profile export" did and didn't take
#: (UI Principle #5: say when we drop something).
EXCLUDED = {
    "mailbox.yaml": "the linked bot inbox is a credential tied to this machine",
    "safety.yaml": "the arming switch and submission cap must be set deliberately per machine",
    "notifications.yaml": "an ntfy topic is a readable address — anyone holding it can read your alerts",
    "applications/": "your application history belongs to this machine, not to your setup",
    "tailored/": "per-application résumés are regenerated from the base résumé",
    "caches": "discovery/salary caches and fit history rebuild themselves",
}


def resume_files(root: Path | None = None) -> list[Path]:
    """Every `profile/*.yaml` that validates as a Résumé — the user may keep several and pick
    between them on the Profile page, so an export that took only `resume.yaml` would drop the
    others silently. Validation (not a name blacklist) is how `web.list_resumes` tells a résumé
    from a config file; doing the same here keeps the two from drifting apart."""
    folder = root or data_path("profile")
    out = []
    for p in sorted(folder.glob("*.yaml")):
        try:
            load_resume(p)
        except Exception:
            continue  # a config file, or a résumé too broken to reload — not exportable
        out.append(p)
    return out


def filename(now: datetime | None = None) -> str:
    """Suggested download name, dated so successive backups don't overwrite each other."""
    return f"applicationbot-profile-{(now or datetime.now()).strftime('%Y-%m-%d')}.zip"


def build_zip(root: Path | None = None, now: datetime | None = None) -> bytes:
    """Build the export in memory and return its bytes.

    Raises `FileNotFoundError` if there is no apply profile to export — an empty zip would look
    like a successful backup of nothing, which is worse than an error naming the missing file.
    """
    folder = root or data_path("profile")
    profile_yaml = folder / "application_profile.yaml"
    if not profile_yaml.is_file():
        raise FileNotFoundError(
            f"No profile to export: {profile_yaml} does not exist. Fill in your details on the "
            f"Profile tab and press Save profile first.")

    members: list[tuple[str, Path]] = []
    for name in CONFIG_FILES:
        p = folder / name
        if p.is_file():
            members.append((name, p))
    for p in resume_files(folder):
        members.append((p.name, p))
    uploads = folder / "uploads"
    if uploads.is_dir():
        for p in sorted(uploads.iterdir()):
            if p.is_file() and p.suffix in (".pdf", ".meta"):
                members.append((f"uploads/{p.name}", p))

    manifest = {
        "format": FORMAT,
        "app_version": __version__,
        "exported_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "contents": [name for name, _ in members],
        "excluded": EXCLUDED,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, p in members:
            z.write(p, name)
    return buf.getvalue()


def export_to(dest: str | Path, root: Path | None = None) -> Path:
    """Write the export to `dest` and return the path written.

    A `dest` ending in `.zip` is the file to write; anything else is a directory the dated
    filename goes into — decided by the suffix, not by whether the directory happens to exist
    yet, so `export_to("~/Backups")` doesn't write a file literally named `Backups`.
    """
    d = Path(dest).expanduser()
    if d.suffix.lower() != ".zip":
        d = d / filename()
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_bytes(build_zip(root))
    return d


def main(argv: list[str] | None = None) -> int:
    """`python -m applicationbot.profile_export [DEST]` — the same export the Profile tab's
    button downloads, so a backup can be scripted or cron'd (Guideline #8). DEST defaults to the
    current directory; a directory gets the dated filename."""
    import argparse
    ap = argparse.ArgumentParser(description="Export your ApplicationBot profile as a .zip.")
    ap.add_argument("dest", nargs="?", default=".", help="output file or directory (default: .)")
    args = ap.parse_args(argv)
    try:
        out = export_to(args.dest)
    except FileNotFoundError as e:
        print(e)
        return 1
    print(f"Wrote {out} ({out.stat().st_size} bytes). Restore it by unzipping into profile/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

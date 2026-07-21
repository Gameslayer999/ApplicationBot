#!/usr/bin/env bash
#
# Cut a versioned GitHub release for ApplicationBot (Agent Guideline #8: one re-runnable
# script instead of manual click-ops).
#
# DEFAULT IS A DRY RUN: it verifies everything and prints exactly what it would do, but
# changes nothing. Creating a tag and a GitHub release is irreversible and outward-facing
# (Agent Guideline #3 / Decision Framework #4), so publishing is gated behind --publish.
#
#   ./scripts/release.sh            # dry run — verify + show the plan
#   ./scripts/release.sh --publish  # actually tag HEAD and create the GitHub release
#
# The release's downloadable source archive is GitHub's auto-generated one, built from the
# git tag = tracked files only. All PII/secrets are git-ignored and were never committed
# (Agent Guideline #12), so they cannot appear in it — step 1 re-verifies that.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PUBLISH=0
[ "${1:-}" = "--publish" ] && PUBLISH=1

VERSION="$(python3 -c 'import applicationbot; print(applicationbot.__version__)')"
TAG="v${VERSION}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "ApplicationBot release — ${TAG}  (HEAD on ${BRANCH})"
[ "$PUBLISH" = 1 ] && echo "MODE: PUBLISH (will tag + create the GitHub release)" \
                   || echo "MODE: dry run (nothing will change — pass --publish to release)"
echo ""

# 1. PII guard (Agent Guideline #12): no user data may ride along in the release.
LEAK="$(git ls-files -- 'profile/*' ':!profile/README.md' '.env' '.env.*' '*.local.yaml' \
        'applications.db' '*.sqlite' '*.session' 'auth_state.json' 2>/dev/null || true)"
if [ -n "$LEAK" ]; then
  echo "✗ Refusing to release — these tracked files look like PII/secrets and must not ship:"
  echo "$LEAK" | sed 's/^/    /'
  echo "  Remove them from git (git rm --cached …) and confirm .gitignore covers them."
  exit 1
fi
echo "✓ PII guard: no user-data/secret files are tracked."

# 2. gh must be installed and authenticated (only strictly needed to publish).
if ! command -v gh >/dev/null 2>&1; then
  echo "✗ GitHub CLI (gh) not found — install it: https://cli.github.com/"
  [ "$PUBLISH" = 1 ] && exit 1
else
  if gh auth status >/dev/null 2>&1; then
    echo "✓ gh authenticated."
  else
    echo "⚠ gh is installed but not authenticated — run 'gh auth login' before --publish."
    [ "$PUBLISH" = 1 ] && exit 1
  fi
fi

# 3. Working tree should be clean so the tag captures a known state.
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠ Working tree has uncommitted changes — commit them before releasing:"
  git status --short | sed 's/^/    /'
  [ "$PUBLISH" = 1 ] && { echo "  Aborting publish."; exit 1; }
fi

# 4. Tag must not already exist.
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "✗ Tag ${TAG} already exists. Bump applicationbot.__version__ first."
  exit 1
fi
echo "✓ Tag ${TAG} is free."

NOTES_FILE="$(mktemp)"
cat > "$NOTES_FILE" <<EOF
ApplicationBot ${TAG} — first tagged release.

A personalized, end-to-end job-application pipeline you run locally: it discovers matching
openings, tailors your résumé to each, fills out the application form, and tracks everything —
as a safe **dry-run** by default (nothing is submitted until you deliberately arm it).

### Get started
1. Download the source zip below and unzip it.
2. Launch it:
   - **macOS** — double-click \`ApplicationBot.command\` (first time: right-click → Open).
   - **Windows** — double-click \`ApplicationBot.bat\`.
   - **Linux** — run \`./scripts/run.sh\`.
   The launcher sets up everything (virtualenv, dependencies, the automation browser) on first
   run, then opens the app in your browser.
3. Follow the in-app **✨ Finish setup** walkthrough: add your details and résumé, choose what
   jobs to find, and run your first dry-run.

Optional: install [Claude Code](https://claude.com/product/claude-code) for higher-quality
tailoring on your subscription — the free \`rules\` engine works without any account.

Requires Python 3. Real submissions stay off until you arm the safety switch.
EOF

echo ""
echo "Release notes:"
sed 's/^/    /' "$NOTES_FILE"
echo ""

if [ "$PUBLISH" = 1 ]; then
  echo "→ Tagging ${TAG} at HEAD and pushing…"
  git tag -a "$TAG" -m "ApplicationBot ${TAG}"
  git push origin "$TAG"
  echo "→ Creating the GitHub release…"
  gh release create "$TAG" --title "ApplicationBot ${TAG}" --notes-file "$NOTES_FILE"
  echo "✓ Released ${TAG}."
else
  echo "Dry run complete. Nothing changed. Re-run with --publish to tag and release."
fi
rm -f "$NOTES_FILE"

#!/bin/sh
# git-sync.sh - Auto commit, pull, push for yt-jellyfin repo
REPO="/Users/alexander-highground/Projects/yt-jellyfin"
cd "$REPO" || exit 1

# Commit any local changes
if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  git add -A
  git commit -m "auto-sync $(date '+%Y-%m-%d %H:%M')"
fi

# Pull remote changes, then push
git pull --rebase origin main 2>/dev/null
git push origin main 2>/dev/null

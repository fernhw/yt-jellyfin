#!/bin/sh
# showSchedulerReport.sh — Append a downloaded episode to today's show report JSON.
# Called by showSchedulerSearch.py immediately after a successful download.
#
# Usage:
#   sh showSchedulerReport.sh <show_name> <folder> <season> <episode> <total> <torrent_title>
#
# Output:  showSchedulerToday.json  — reset on day change, consumed by reportMaker.sh

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
. "$SCRIPT_DIR/locations.md"

REPORT_JSON="$SCRIPT_DIR/showSchedulerToday.json"
TODAY=$(date '+%Y-%m-%d')

SHOW_NAME="$1"
FOLDER="$2"
SEASON="$3"
EPISODE="$4"
TOTAL="$5"
TORRENT_TITLE="$6"

[ -z "$SHOW_NAME" ] && exit 1

# ── Day rollover: clear file if it's from a different day ────────────────────
if [ -f "$REPORT_JSON" ]; then
  file_date=$(grep -o '"date":"[^"]*"' "$REPORT_JSON" | head -1 | cut -d'"' -f4)
  if [ -n "$file_date" ] && [ "$file_date" != "$TODAY" ]; then
    rm -f "$REPORT_JSON"
  fi
fi

# ── Find folder art from Jellyfin show folder ─────────────────────────────────
SHOW_DIR="$SHOWS_DIR/$FOLDER"
THUMB_SRC=""
for name in folder.jpg cover.jpg poster.jpg Cover.jpg; do
  if [ -f "$SHOW_DIR/$name" ]; then
    THUMB_SRC="$SHOW_DIR/$name"
    break
  fi
done
# Fallback: first jpg in show dir
if [ -z "$THUMB_SRC" ]; then
  THUMB_SRC=$(find "$SHOW_DIR" -maxdepth 2 -iname "*.jpg" 2>/dev/null | head -1)
fi

# Copy thumb to web/media-thumbs/show-<key>.jpg for use in HTML
THUMB_REL=""
if [ -n "$THUMB_SRC" ] && [ -f "$THUMB_SRC" ]; then
  safe_key=$(printf '%s' "$FOLDER" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/--*/-/g;s/^-*//;s/-*$//')
  DEST_THUMB="$SCRIPT_DIR/web/media-thumbs/show-sched-${safe_key}.jpg"
  mkdir -p "$SCRIPT_DIR/web/media-thumbs"
  cp "$THUMB_SRC" "$DEST_THUMB" 2>/dev/null
  THUMB_REL="media-thumbs/show-sched-${safe_key}.jpg"
fi

# ── Escape for JSON ───────────────────────────────────────────────────────────
json_esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

SHOW_J=$(json_esc "$SHOW_NAME")
FOLDER_J=$(json_esc "$FOLDER")
TORRENT_J=$(json_esc "$TORRENT_TITLE")
THUMB_J=$(json_esc "$THUMB_REL")
NOW_EPOCH=$(date +%s)
EP_LABEL="S$(printf '%02d' "$SEASON")E$(printf '%02d' "$EPISODE")"

ENTRY="{\"show\":\"$SHOW_J\",\"folder\":\"$FOLDER_J\",\"ep\":\"$EP_LABEL\",\"total\":$TOTAL,\"thumb\":\"$THUMB_J\",\"torrent\":\"$TORRENT_J\",\"ts\":$NOW_EPOCH}"

# ── Write / append ────────────────────────────────────────────────────────────
if [ ! -f "$REPORT_JSON" ]; then
  printf '{"date":"%s","episodes":[%s]}\n' "$TODAY" "$ENTRY" > "$REPORT_JSON"
else
  # Append entry into the episodes array using python3 (avoids sed/shell escaping issues)
  python3 - "$REPORT_JSON" "$ENTRY" <<'PYEOF'
import sys, json
path, entry_str = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
data['episodes'].append(json.loads(entry_str))
with open(path, 'w') as f:
    json.dump(data, f)
PYEOF
fi

printf '  show report updated: %s %s\n' "$SHOW_NAME" "$EP_LABEL"

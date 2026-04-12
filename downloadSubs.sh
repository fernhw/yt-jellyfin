#!/bin/sh
# downloadSubs.sh - Check subscribed channels, download new videos
# Uses --flat-playlist for fast ID scanning (~1s/channel)
# DB is source of truth: if video ID not in DB, it's new
#
# Usage:
#   ./downloadSubs.sh              # normal scan + download
#   ./downloadSubs.sh --init       # seed DB with current videos (no download)
#   ./downloadSubs.sh --dry-run    # show what would download, don't do it
#
# INSTALL: brew install yt-dlp ffmpeg

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
YT_ROOT="/Volumes/Darrel4tb/YT"
DB="$SCRIPT_DIR/ytdb.db"
SUBS_FILE="$SCRIPT_DIR/subscribedTo.md"
VARS_FILE="$SCRIPT_DIR/varsYT.md"
GETYT="$SCRIPT_DIR/getyt.sh"
SCAN_DEPTH=20
LOCKFILE="$SCRIPT_DIR/.downloadSubs.lock"
SCRAPER="$SCRIPT_DIR/scrapeYT.py"
CONFIG_FILE="$SCRIPT_DIR/channelConfig.md"
FILTER_FILE="$SCRIPT_DIR/filterYT.md"

MODE="download"
case "$1" in
  --init)        MODE="init" ;;
  --dry-run)     MODE="dry-run" ;;
  --scrape-only) MODE="scrape-only" ;;
esac

init_db() {
  mkdir -p "$YT_ROOT"
  sqlite3 "$DB" "CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY, url TEXT, channel TEXT, title TEXT,
    upload_date TEXT, download_date INTEGER, file_path TEXT,
    status TEXT DEFAULT 'downloaded'
  );"
}

# Mark a video as seen in DB without downloading
db_mark_seen() {
  sqlite3 "$DB" "INSERT OR IGNORE INTO videos (id,url,channel,title,status)
    VALUES (
      '$(printf '%s' "$1" | sed "s/'/''/g")',
      'https://www.youtube.com/watch?v=$1',
      '$(printf '%s' "$2" | sed "s/'/''/g")',
      '',
      'seen'
    );"
}

db_exists() {
  sqlite3 "$DB" "SELECT 1 FROM videos WHERE id='$(printf '%s' "$1" | sed "s/'/''/g")';"
}

update_vars() {
  local now_ms
  now_ms=$(python3 -c "import time; print(int(time.time()*1000))")
  local total
  total=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos;" 2>/dev/null || echo 0)
  cat > "$VARS_FILE" <<EOF
last_scan=$now_ms
total_videos=$total
updated=$(date '+%Y-%m-%d %H:%M:%S')
EOF
}

get_channels() {
  [ ! -f "$SUBS_FILE" ] && { echo "ERROR: $SUBS_FILE not found" >&2; exit 1; }
  grep -Eo 'https?://[^ )]+' "$SUBS_FILE" | grep -i 'youtube\|youtu\.be'
}

# Extract channel handle from URL for labeling
channel_label() {
  printf '%s' "$1" | grep -oE '@[^/]+' || printf '%s' "$1"
}

# Strip @ prefix for config matching
handle_bare() {
  printf '%s' "$1" | sed 's/^@//'
}

# Read priority list from channelConfig.md
get_priority_handles() {
  [ ! -f "$CONFIG_FILE" ] && return
  awk '/^\[priority\]/{found=1; next} /^\[/{found=0} found && /^[^#]/ && NF{print $1}' "$CONFIG_FILE"
}

# Get limit for a channel handle (bare, no @). Empty = unlimited
get_limit() {
  [ ! -f "$CONFIG_FILE" ] && return
  awk -F ' *= *' -v h="$1" '
    /^\[limits\]/{found=1; next} /^\[/{found=0}
    found && tolower($1)==tolower(h) {print $2; exit}
  ' "$CONFIG_FILE"
}

# Enforce rolling limit: delete oldest files if over limit
enforce_limit() {
  local channel_dir="$1" limit="$2"
  [ -z "$limit" ] && return
  [ ! -d "$channel_dir" ] && return

  # Count all mp4 files across year subdirs
  local count
  count=$(find "$channel_dir" -name '*.mp4' -type f 2>/dev/null | wc -l | tr -d ' ')
  [ "$count" -le "$limit" ] && return

  local to_delete=$(( count - limit ))
  echo "  LIMIT: $count videos, keeping $limit, removing $to_delete oldest"

  # Delete oldest by file modification time
  find "$channel_dir" -name '*.mp4' -type f -print0 2>/dev/null \
    | xargs -0 ls -1tr \
    | head -n "$to_delete" \
    | while IFS= read -r f; do
        echo "  DELETE: $(basename "$f")"
        rm -f "$f"
      done
}

# Reorder queue: priority channels first, rest after
reorder_queue() {
  local queue_file="$1"
  [ ! -f "$queue_file" ] || [ ! -s "$queue_file" ] && return

  local priority_queue="$queue_file.priority"
  local normal_queue="$queue_file.normal"
  rm -f "$priority_queue" "$normal_queue"

  # Build priority handle list (lowercase for matching)
  local pri_handles
  pri_handles=$(get_priority_handles | tr '[:upper:]' '[:lower:]')
  [ -z "$pri_handles" ] && return

  # We need video->channel mapping. For now, tag queue entries during scan.
  # Queue lines are: URL [TAB] @handle
  while IFS="$(printf '\t')" read -r url handle; do
    bare=$(handle_bare "$handle" | tr '[:upper:]' '[:lower:]')
    if printf '%s\n' "$pri_handles" | grep -qix "$bare"; then
      echo "$url" >> "$priority_queue"
    else
      echo "$url" >> "$normal_queue"
    fi
  done < "$queue_file"

  # Reassemble: priority first
  cat /dev/null > "$queue_file"
  [ -f "$priority_queue" ] && cat "$priority_queue" >> "$queue_file"
  [ -f "$normal_queue" ] && cat "$normal_queue" >> "$queue_file"
  rm -f "$priority_queue" "$normal_queue"
}

SCRAPED_CACHE="$YT_ROOT/.scraped"

# Build scraped cache: one HDD read, stores folder names + titles + IDs
# All lowercased and stripped of separators for fuzzy handle matching
refresh_scraped_cache() {
  {
    ls -d "$YT_ROOT"/*/tvshow.nfo 2>/dev/null | sed 's|.*/YT/||; s|/tvshow.nfo||'
    grep -h '<title>' "$YT_ROOT"/*/tvshow.nfo 2>/dev/null | sed 's|.*<title>||; s|</title>.*||'
  } | tr '[:upper:]' '[:lower:]' | sed 's/[_ -]//g' | sort -u > "$SCRAPED_CACHE"
}

# Check if a URL is already scraped (from cache file, no per-channel I/O)
is_scraped() {
  local handle
  handle=$(printf '%s' "$1" | grep -oE '@[^/]+' | sed 's/^@//' | tr '[:upper:]' '[:lower:]' | sed 's/[_ -]//g')
  [ -z "$handle" ] && return 1
  grep -qix "$handle" "$SCRAPED_CACHE" 2>/dev/null
}

scrape_all_channels() {
  echo ""
  echo "--- Scraping channel metadata ---"

  # Single HDD read: cache all scraped folder names
  refresh_scraped_cache
  local scraped_count sub_count
  scraped_count=$(wc -l < "$SCRAPED_CACHE" | tr -d ' ')
  sub_count=$(get_channels | wc -l | tr -d ' ')

  if [ "$scraped_count" -ge "$sub_count" ]; then
    echo "  all channels scraped, skipping"
    echo "--- Scraping complete ---"
    return
  fi

  # Collect unscraped URLs
  local scrape_list="$YT_ROOT/.scrape_queue"
  rm -f "$scrape_list"
  get_channels | while IFS= read -r channel_url; do
    [ -z "$channel_url" ] && continue
    is_scraped "$channel_url" && continue
    echo "$channel_url" >> "$scrape_list"
  done

  if [ ! -f "$scrape_list" ] || [ ! -s "$scrape_list" ]; then
    echo "  all channels scraped, skipping"
    echo "--- Scraping complete ---"
    return
  fi

  local to_scrape
  to_scrape=$(wc -l < "$scrape_list" | tr -d ' ')
  echo "  $to_scrape new channel(s) to scrape..."

  # Single Python call with all URLs
  python3 "$SCRAPER" --yt-root "$YT_ROOT" --filter "$FILTER_FILE" $(cat "$scrape_list")
  rm -f "$scrape_list"

  echo "--- Scraping complete ---"
}

# --- Main ---
echo "=== downloadSubs [$MODE] $(date '+%Y-%m-%d %H:%M:%S') ==="

if [ ! -d "/Volumes/Darrel4tb" ]; then
  echo "ERROR: /Volumes/Darrel4tb not mounted"
  exit 1
fi

# Prevent overlapping runs
if [ -f "$LOCKFILE" ]; then
  lock_pid=$(cat "$LOCKFILE" 2>/dev/null)
  if kill -0 "$lock_pid" 2>/dev/null; then
    echo "Already running (pid $lock_pid). Exiting."
    exit 0
  else
    echo "Stale lock found (pid $lock_pid gone). Removing."
    rm -f "$LOCKFILE"
  fi
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT INT TERM

init_db

# Scrape channel artwork/NFO before scanning for videos
if [ "$MODE" != "init" ]; then
  scrape_all_channels
fi

if [ "$MODE" = "scrape-only" ]; then
  update_vars
  echo "=== Scrape-only complete ==="
  exit 0
fi

NEW_COUNT=0
QUEUE="$YT_ROOT/.download_queue"
rm -f "$QUEUE"

get_channels | while IFS= read -r channel_url; do
  [ -z "$channel_url" ] && continue

  # Ensure /videos tab (chronological, no shorts)
  clean_url=$(printf '%s' "$channel_url" | sed 's|/[[:space:]]*$||')
  case "$clean_url" in
    */videos) ;;
    *) clean_url="$clean_url/videos" ;;
  esac

  label=$(channel_label "$channel_url")
  echo ""
  echo "Scanning $label ..."

  # Fast flat-playlist: just IDs, ~1s per channel
  video_ids=$(yt-dlp --flat-playlist --print id --playlist-end "$SCAN_DEPTH" "$clean_url" 2>/dev/null) || true

  if [ -z "$video_ids" ]; then
    echo "  WARN: no videos returned"
    continue
  fi

  # Check if this is a brand new channel (no entries in DB)
  known_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE channel='$(printf '%s' "$label" | sed "s/'/''/g")';" 2>/dev/null)
  is_new_channel=0
  [ "${known_count:-0}" -eq 0 ] && is_new_channel=1

  new_for_channel=0
  first_vid=""
  while IFS= read -r vid; do
    [ -z "$vid" ] && continue

    exists=$(db_exists "$vid")
    if [ -n "$exists" ]; then
      continue
    fi

    new_for_channel=$(( new_for_channel + 1 ))

    if [ "$MODE" = "init" ]; then
      db_mark_seen "$vid" "$label"
      echo "  SEED: $vid"
      [ -z "$first_vid" ] && first_vid="$vid"
    elif [ "$MODE" = "dry-run" ]; then
      if [ "$is_new_channel" -eq 1 ] && [ "$new_for_channel" -gt 1 ]; then
        echo "  WOULD SEED: $vid"
      else
        echo "  WOULD DOWNLOAD: https://www.youtube.com/watch?v=$vid"
      fi
    else
      # New channel: seed all except the first (newest) video
      if [ "$is_new_channel" -eq 1 ] && [ "$new_for_channel" -gt 1 ]; then
        db_mark_seen "$vid" "$label"
        echo "  SEED: $vid"
      else
        [ -z "$first_vid" ] && first_vid="$vid"
        echo "  NEW: $vid"
        printf '%s\t%s\n' "https://www.youtube.com/watch?v=$vid" "$label" >> "$QUEUE"
      fi
    fi
  done <<EOF
$video_ids
EOF

  if [ "$new_for_channel" -eq 0 ]; then
    echo "  up to date"
  elif [ "$is_new_channel" -eq 1 ] && [ "$MODE" != "init" ]; then
    echo "  new channel! seeded $(( new_for_channel - 1 )), 1 queued for download"
  else
    echo "  $new_for_channel new video(s)"
    NEW_COUNT=$(( NEW_COUNT + new_for_channel ))
  fi

  # Init: pop the newest video so next scan will download it
  if [ "$MODE" = "init" ] && [ -n "$first_vid" ]; then
    sqlite3 "$DB" "DELETE FROM videos WHERE id='$(printf '%s' "$first_vid" | sed "s/'/''/g")';" 
    echo "  POPPED latest: $first_vid (will download on next run)"
  fi
done

echo ""
if [ "$MODE" = "init" ]; then
  echo "Init complete. Seeded DB with current videos."
elif [ "$MODE" = "dry-run" ]; then
  echo "Dry run complete. No downloads performed."
elif [ -f "$QUEUE" ] && [ -s "$QUEUE" ]; then
  # Reorder queue: priority channels first
  reorder_queue "$QUEUE"

  # Strip handle tags for getyt (only needs URLs)
  tmp_urls="$QUEUE.urls"
  cut -f1 "$QUEUE" > "$tmp_urls"

  count=$(wc -l < "$tmp_urls" | tr -d ' ')
  echo "Downloading $count new video(s)..."
  "$GETYT" -f "$tmp_urls"
  rm -f "$QUEUE" "$tmp_urls"
else
  echo "No new videos."
  rm -f "$QUEUE"
fi

# Enforce per-channel rolling limits
echo ""
echo "--- Enforcing channel limits ---"
for channel_dir in "$YT_ROOT"/*/; do
  [ ! -d "$channel_dir" ] && continue
  dirname=$(basename "$channel_dir")
  limit=$(get_limit "$dirname")
  if [ -n "$limit" ]; then
    enforce_limit "$channel_dir" "$limit"
  fi
done

update_vars
echo "=== Done ==="

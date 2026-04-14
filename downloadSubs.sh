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
  --init)             MODE="init" ;;
  --dry-run)          MODE="dry-run" ;;
  --scrape-only)      MODE="scrape-only" ;;
  --collage-seasons)  MODE="collage-only" ;;
esac

init_db() {
  mkdir -p "$YT_ROOT"
  sqlite3 "$DB" "CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY, url TEXT, channel TEXT, title TEXT,
    upload_date TEXT, download_date INTEGER, file_path TEXT,
    status TEXT DEFAULT 'downloaded'
  );"
  sqlite3 "$DB" "CREATE TABLE IF NOT EXISTS channel_aliases (
    handle TEXT PRIMARY KEY,
    display_name TEXT
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
  local today
  today=$(date '+%Y-%m-%d')

  # Read existing daily counters if same day, otherwise reset
  local prev_date prev_dl prev_del prev_err prev_skip prev_dl_list prev_del_list prev_err_list prev_skip_list
  prev_date=""
  prev_dl=0; prev_del=0; prev_err=0; prev_skip=0
  prev_dl_list=""; prev_del_list=""; prev_err_list=""; prev_skip_list=""
  if [ -f "$VARS_FILE" ]; then
    prev_date=$(grep '^report_date=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
    if [ "$prev_date" = "$today" ]; then
      prev_dl=$(grep '^downloaded_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_del=$(grep '^deleted_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_err=$(grep '^errors_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_skip=$(grep '^skipped_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_dl_list=$(grep '^downloaded_list=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_del_list=$(grep '^deleted_list=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_err_list=$(grep '^errors_list=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_skip_list=$(grep '^skipped_list=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
    fi
  fi
  prev_dl=${prev_dl:-0}; prev_del=${prev_del:-0}; prev_err=${prev_err:-0}; prev_skip=${prev_skip:-0}

  # Merge new items with existing daily lists
  local final_dl_list final_del_list final_err_list final_skip_list
  if [ -n "$prev_dl_list" ] && [ -n "$DL_ITEMS" ]; then
    final_dl_list="${prev_dl_list}|${DL_ITEMS}"
  else
    final_dl_list="${prev_dl_list}${DL_ITEMS}"
  fi
  if [ -n "$prev_del_list" ] && [ -n "$DEL_ITEMS" ]; then
    final_del_list="${prev_del_list}|${DEL_ITEMS}"
  else
    final_del_list="${prev_del_list}${DEL_ITEMS}"
  fi
  if [ -n "$prev_err_list" ] && [ -n "$ERR_ITEMS" ]; then
    final_err_list="${prev_err_list}|${ERR_ITEMS}"
  else
    final_err_list="${prev_err_list}${ERR_ITEMS}"
  fi
  if [ -n "$prev_skip_list" ] && [ -n "$SKIP_ITEMS" ]; then
    final_skip_list="${prev_skip_list}|${SKIP_ITEMS}"
  else
    final_skip_list="${prev_skip_list}${SKIP_ITEMS}"
  fi

  cat > "$VARS_FILE" <<EOF
last_scan=$now_ms
total_videos=$total
updated=$(date '+%Y-%m-%d %H:%M:%S')
report_date=$today
downloaded_today=$(( prev_dl + DL_COUNT ))
deleted_today=$(( prev_del + DEL_COUNT ))
errors_today=$(( prev_err + ERR_COUNT ))
skipped_today=$(( prev_skip + SKIP_COUNT ))
channels_scanned=$CHAN_SCANNED
channels_total=$CHAN_TOTAL
downloaded_list=$final_dl_list
deleted_list=$final_del_list
errors_list=$final_err_list
skipped_list=$final_skip_list
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
        local bname
        bname=$(basename "$f")
        echo "  DELETE: $bname"
        rm -f "$f"
        # Also remove matching thumbnail
        rm -f "${f%.mp4}-thumb.jpg"
        # Track for report
        echo "$(basename "$channel_dir"):${bname%.mp4}" >> "$YT_ROOT/.deleted_today"
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
      printf '%s\t%s\n' "$url" "$handle" >> "$priority_queue"
    else
      printf '%s\t%s\n' "$url" "$handle" >> "$normal_queue"
    fi
  done < "$queue_file"

  # Reassemble: priority first
  cat /dev/null > "$queue_file"
  [ -f "$priority_queue" ] && cat "$priority_queue" >> "$queue_file"
  [ -f "$normal_queue" ] && cat "$normal_queue" >> "$queue_file"
  rm -f "$priority_queue" "$normal_queue"
}

SCRAPED_HANDLES="$YT_ROOT/.scraped_handles"

# Check if a handle is already scraped (single file lookup)
is_scraped() {
  local handle
  handle=$(printf '%s' "$1" | grep -oE '@[^/]+' | sed 's/^@//' | tr '[:upper:]' '[:lower:]')
  [ -z "$handle" ] && return 1
  [ -f "$SCRAPED_HANDLES" ] && grep -qix "$handle" "$SCRAPED_HANDLES" 2>/dev/null
}

scrape_all_channels() {
  echo ""
  echo "--- Scraping channel metadata ---"

  # Collect unscraped URLs (reads one file, no HDD per-channel)
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

  # Single Python call with all URLs (brew python for modern TLS)
  /opt/homebrew/bin/python3 "$SCRAPER" --yt-root "$YT_ROOT" --filter "$FILTER_FILE" $(cat "$scrape_list")
  rm -f "$scrape_list"

  echo "--- Scraping complete ---"
}

# --- Main ---
echo "=== downloadSubs [$MODE] $(date '+%Y-%m-%d %H:%M:%S') ==="

# Daily counters for report
DL_COUNT=0
DEL_COUNT=0
ERR_COUNT=0
SKIP_COUNT=0
CHAN_SCANNED=0
CHAN_TOTAL=0
DL_ITEMS=""
DEL_ITEMS=""
ERR_ITEMS=""
SKIP_ITEMS=""
REPORT_MAKER="$SCRIPT_DIR/reportMaker.sh"

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

COLLAGE_MAKER="$SCRIPT_DIR/collageSeasons.py"

if [ "$MODE" = "collage-only" ]; then
  [ -f "$COLLAGE_MAKER" ] && python3 "$COLLAGE_MAKER"
  echo "=== Collage-only complete ==="
  exit 0
fi

NEW_COUNT=0
QUEUE="$YT_ROOT/.download_queue"
rm -f "$QUEUE"

# Write channel list to file to avoid stdin conflicts with sqlite3/yt-dlp in the loop
CHANNEL_LIST="$YT_ROOT/.channel_list"
get_channels > "$CHANNEL_LIST"
CHAN_TOTAL=$(wc -l < "$CHANNEL_LIST" | tr -d ' ')

while IFS= read -r channel_url <&3; do
  [ -z "$channel_url" ] && continue
  CHAN_SCANNED=$(( CHAN_SCANNED + 1 ))

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
    ERR_COUNT=$(( ERR_COUNT + 1 ))
    if [ -n "$ERR_ITEMS" ]; then
      ERR_ITEMS="${ERR_ITEMS}|${label}:no_videos_returned"
    else
      ERR_ITEMS="${label}:no_videos_returned"
    fi
    continue
  fi

  # Check if this is a brand new channel (no entries in DB)
  bare_label=$(handle_bare "$label")
  is_new_channel=0

  # Priority channels are never treated as "new" — always download everything
  pri_match=$(get_priority_handles | tr '[:upper:]' '[:lower:]' | grep -ix "$bare_label" || true)
  if [ -z "$pri_match" ]; then
    # Check by @handle, bare handle, and any known display name alias
    alias_name=$(sqlite3 "$DB" "SELECT display_name FROM channel_aliases WHERE handle='$(printf '%s' "$bare_label" | sed "s/'/''/g")';" 2>/dev/null)
    known_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE channel='$(printf '%s' "$label" | sed "s/'/''/g")' OR channel='$(printf '%s' "$bare_label" | sed "s/'/''/g")' OR channel='$(printf '%s' "${alias_name:-___none___}" | sed "s/'/''/g")';" 2>/dev/null)
    [ "${known_count:-0}" -eq 0 ] && is_new_channel=1
  fi

  new_for_channel=0
  first_vid=""
  while IFS= read -r vid; do
    [ -z "$vid" ] && continue

    exists=$(db_exists "$vid")
    if [ -n "$exists" ]; then
      # Pick up force-download entries and queue them
      fd_status=$(sqlite3 "$DB" "SELECT status FROM videos WHERE id='$(printf '%s' "$vid" | sed "s/'/''/g")';")
      if [ "$fd_status" = "force-download" ]; then
        echo "  FORCE: $vid"
        printf '%s\t%s\n' "https://www.youtube.com/watch?v=$vid" "$label" >> "$QUEUE"
        new_for_channel=$(( new_for_channel + 1 ))
      elif [ "$fd_status" = "failed" ] || [ "$fd_status" = "errored" ]; then
        echo "  RETRY: $vid (was $fd_status)"
        sqlite3 "$DB" "UPDATE videos SET status='force-download' WHERE id='$(printf '%s' "$vid" | sed "s/'/''/g")';"
        printf '%s\t%s\n' "https://www.youtube.com/watch?v=$vid" "$label" >> "$QUEUE"
        new_for_channel=$(( new_for_channel + 1 ))
      fi
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
done 3< "$CHANNEL_LIST"
rm -f "$CHANNEL_LIST"

echo ""
if [ "$MODE" = "init" ]; then
  echo "Init complete. Seeded DB with current videos."
elif [ "$MODE" = "dry-run" ]; then
  echo "Dry run complete. No downloads performed."
elif [ -f "$QUEUE" ] && [ -s "$QUEUE" ]; then
  # Reorder queue: priority channels first
  reorder_queue "$QUEUE"

  # Snapshot DB before downloads to detect what got added
  dl_before=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE status='downloaded';" 2>/dev/null || echo 0)
  skip_before=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE status IN ('age-restricted','members-only','unavailable','errored','no-english');" 2>/dev/null || echo 0)

  count=$(wc -l < "$QUEUE" | tr -d ' ')
  echo "Downloading $count new video(s)..."
  "$GETYT" -f "$QUEUE"

  # Compute what was actually downloaded this run
  dl_after=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE status='downloaded';" 2>/dev/null || echo 0)
  DL_COUNT=$(( dl_after - dl_before ))
  [ "$DL_COUNT" -lt 0 ] && DL_COUNT=0

  # Collect titles of newly downloaded videos
  DL_ITEMS=$(sqlite3 "$DB" "SELECT channel || ':' || title FROM videos WHERE status='downloaded' ORDER BY download_date DESC LIMIT $DL_COUNT;" 2>/dev/null | tr '\n' '|' | sed 's/|$//')

  # Count skipped videos (age-restricted, members-only, etc.)
  skip_after=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE status IN ('age-restricted','members-only','unavailable','errored','no-english');" 2>/dev/null || echo 0)
  SKIP_COUNT=$(( skip_after - skip_before ))
  [ "$SKIP_COUNT" -lt 0 ] && SKIP_COUNT=0
  if [ "$SKIP_COUNT" -gt 0 ]; then
    SKIP_ITEMS=$(sqlite3 "$DB" "SELECT status || ':' || url FROM videos WHERE status IN ('age-restricted','members-only','unavailable','errored','no-english') ORDER BY rowid DESC LIMIT $SKIP_COUNT;" 2>/dev/null | tr '\n' '|' | sed 's/|$//')
  fi

  # Count failures from this run
  fail_after=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE status='failed';" 2>/dev/null || echo 0)

  rm -f "$QUEUE"
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

# Generate missing thumbnails for Jellyfin (-thumb.jpg next to each .mp4)
# Extracts 4 candidate frames, scores with ImageMagick, picks the best
echo ""
echo "--- Checking video thumbnails ---"
thumb_queue=""
thumb_needed=0
for mp4 in "$YT_ROOT"/*/*.mp4; do
  [ -f "$mp4" ] || continue
  [ -f "${mp4%.mp4}-thumb.jpg" ] && continue
  thumb_queue="$thumb_queue
$mp4"
  thumb_needed=$((thumb_needed + 1))
done

if [ "$thumb_needed" -eq 0 ]; then
  echo "  all thumbnails present"
else
  echo "  $thumb_needed video(s) missing thumbnails"
  echo "$thumb_queue" | while IFS= read -r mp4; do
    [ -z "$mp4" ] && continue
    thumb="${mp4%.mp4}-thumb.jpg"
    duration=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$mp4" 2>/dev/null | cut -d. -f1)
    if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
      # Fallback: try stream-level duration
      duration=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$mp4" 2>/dev/null | cut -d. -f1)
    fi
    if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
      # Fallback: decode and parse total duration from ffmpeg output
      duration=$(ffmpeg -i "$mp4" -f null - 2>&1 | grep -oE 'Duration: [0-9:.]+' | head -1 \
        | sed 's/Duration: //' | awk -F: '{print int($1*3600+$2*60+$3)}')
    fi
    if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
      echo "  SKIP (unreadable): $(basename "$mp4")"
      continue
    fi

    tmpdir=$(mktemp -d)
    best_score=0
    best_file=""

    # Sample 4 frames at 15%, 35%, 55%, 75% of duration
    for pct in 15 35 55 75; do
      seek=$(( duration * pct / 100 ))
      [ "$seek" -lt 1 ] && seek=1
      candidate="$tmpdir/frame_${pct}.jpg"
      ffmpeg -y -ss "$seek" -i "$mp4" -vframes 1 -update 1 -q:v 2 "$candidate" -loglevel quiet 2>/dev/null || continue
      [ -f "$candidate" ] || continue

      # Score: grayscale std deviation (higher = more visual contrast/interest)
      score=$(magick "$candidate" -colorspace Gray -format "%[fx:standard_deviation*1000]" info: 2>/dev/null)
      score=${score:-0}
      # Integer comparison (strip decimal)
      score_int=$(printf '%.0f' "$score" 2>/dev/null || echo 0)
      if [ "$score_int" -gt "$best_score" ] 2>/dev/null; then
        best_score=$score_int
        best_file=$candidate
      fi
    done

    if [ -n "$best_file" ] && [ -f "$best_file" ]; then
      mv "$best_file" "$thumb"
      echo "  THUMB: $(basename "$thumb") (score: $best_score)"
    else
      # Fallback: single frame at 10% of duration
      local fb_seek=$(( duration / 10 ))
      [ "$fb_seek" -lt 2 ] && fb_seek=2
      ffmpeg -y -ss "$fb_seek" -i "$mp4" -vframes 1 -update 1 -q:v 2 "$thumb" -loglevel quiet 2>/dev/null
      if [ -f "$thumb" ]; then
        echo "  THUMB (fallback): $(basename "$thumb")"
      else
        # Last resort: grab the very first frame
        ffmpeg -y -i "$mp4" -vframes 1 -update 1 -q:v 2 "$thumb" -loglevel quiet 2>/dev/null \
          && echo "  THUMB (first-frame): $(basename "$thumb")" \
          || echo "  SKIP (no frame): $(basename "$mp4")"
      fi
    fi
    rm -rf "$tmpdir"
  done
fi

# Collect delete tracking from temp file (written by enforce_limit subshell)
if [ -f "$YT_ROOT/.deleted_today" ]; then
  DEL_COUNT=$(wc -l < "$YT_ROOT/.deleted_today" | tr -d ' ')
  DEL_ITEMS=$(tr '\n' '|' < "$YT_ROOT/.deleted_today" | sed 's/|$//')
  rm -f "$YT_ROOT/.deleted_today"
fi

update_vars

# Generate season poster collages (only rebuilds changed seasons)
if [ -f "$COLLAGE_MAKER" ]; then
  python3 "$COLLAGE_MAKER"
fi

# Generate daily report
if [ -x "$REPORT_MAKER" ]; then
  "$REPORT_MAKER"
fi

echo "=== Done ==="

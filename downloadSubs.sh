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
. "$SCRIPT_DIR/locations.md"
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
  --thumbs)           MODE="thumbs-only" ;;
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
  local prev_date prev_dl prev_del prev_skip prev_dl_list prev_del_list prev_skip_list
  prev_date=""
  prev_dl=0; prev_del=0; prev_skip=0
  prev_dl_list=""; prev_del_list=""; prev_skip_list=""
  if [ -f "$VARS_FILE" ]; then
    prev_date=$(grep '^report_date=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
    if [ "$prev_date" = "$today" ]; then
      prev_dl=$(grep '^downloaded_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_del=$(grep '^deleted_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_err=$(grep '^errors_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_skip=$(grep '^skipped_today=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_dl_list=$(grep '^downloaded_list=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_del_list=$(grep '^deleted_list=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
      prev_skip_list=$(grep '^skipped_list=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
    fi
  fi
  prev_dl=${prev_dl:-0}; prev_del=${prev_del:-0}; prev_skip=${prev_skip:-0}

  # Merge new items with existing daily lists (downloads, deletions, skips accumulate)
  local final_dl_list final_del_list final_skip_list
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
skipped_today=$(( prev_skip + SKIP_COUNT ))
channels_scanned=$CHAN_SCANNED
channels_total=$CHAN_TOTAL
errors_last_run=$ERR_COUNT
errors_list=$ERR_ITEMS
downloaded_list=$final_dl_list
deleted_list=$final_del_list
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

# Get quality cap for a channel handle (bare, no @). Empty = best available
get_quality() {
  [ ! -f "$CONFIG_FILE" ] && return
  awk -F ' *= *' -v h="$1" '
    /^\[quality\]/{found=1; next} /^\[/{found=0}
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
  # Queue lines are: URL [TAB] @handle [TAB] max_res
  while IFS="$(printf '\t')" read -r url handle max_res; do
    bare=$(handle_bare "$handle" | tr '[:upper:]' '[:lower:]')
    if printf '%s\n' "$pri_handles" | grep -qix "$bare"; then
      printf '%s\t%s\t%s\n' "$url" "$handle" "$max_res" >> "$priority_queue"
    else
      printf '%s\t%s\t%s\n' "$url" "$handle" "$max_res" >> "$normal_queue"
    fi
  done < "$queue_file"

  # Reassemble: priority first
  cat /dev/null > "$queue_file"
  [ -f "$priority_queue" ] && cat "$priority_queue" >> "$queue_file"
  [ -f "$normal_queue" ] && cat "$normal_queue" >> "$queue_file"
  rm -f "$priority_queue" "$normal_queue"
}

# Check if a channel is already scraped (folder + tvshow.nfo exists on disk)
is_scraped() {
  local url="$1"
  local handle folder_name display filtered
  # Extract @handle from URL
  handle=$(printf '%s' "$url" | grep -oE '@[^/]+' | sed 's/^@//')
  if [ -z "$handle" ]; then
    # /channel/UCID URLs
    handle=$(printf '%s' "$url" | grep -oE 'UC[A-Za-z0-9_-]+')
    [ -z "$handle" ] && return 1
  fi

  # 1) Check channel_aliases: handle → display_name → normalize → folder
  display=$(sqlite3 "$DB" "SELECT display_name FROM channel_aliases WHERE handle='$(printf '%s' "$handle" | sed "s/'/''/g")';" 2>/dev/null)
  if [ -n "$display" ]; then
    filtered="$display"
    if [ -f "$FILTER_FILE" ]; then
      local fmatch
      fmatch=$(awk -F ' *-> *' -v ch="$display" 'tolower($1)==tolower(ch) && NF>=2 {print $2; exit}' "$FILTER_FILE")
      [ -n "$fmatch" ] && filtered="$fmatch"
    fi
    folder_name=$(printf '%s' "$filtered" | sed 's/[^a-zA-Z0-9 _-]//g' | tr -d ' _')
    [ -f "$YT_ROOT/$folder_name/tvshow.nfo" ] && return 0
  fi

  # 2) Fallback: handle directly (apply filter, normalize)
  filtered="$handle"
  if [ -f "$FILTER_FILE" ]; then
    local fmatch
    fmatch=$(awk -F ' *-> *' -v ch="$handle" 'tolower($1)==tolower(ch) && NF>=2 {print $2; exit}' "$FILTER_FILE")
    [ -n "$fmatch" ] && filtered="$fmatch"
  fi
  folder_name=$(printf '%s' "$filtered" | sed 's/[^a-zA-Z0-9 _-]//g' | tr -d ' _')
  [ -f "$YT_ROOT/$folder_name/tvshow.nfo" ]
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
  /opt/homebrew/bin/python3 "$SCRAPER" --yt-root "$YT_ROOT" --filter "$FILTER_FILE" --db "$DB" $(cat "$scrape_list")
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

# Prevent overlapping runs (skip lock for thumbs-only — read-only safe)
if [ "$MODE" != "thumbs-only" ]; then
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
fi

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

if [ "$MODE" = "thumbs-only" ]; then
  # Jump straight to video thumbnail generation (skip scan/download/limits)
  :
else

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
  max_res=$(get_quality "$bare_label")
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
        printf '%s\t%s\t%s\n' "https://www.youtube.com/watch?v=$vid" "$label" "$max_res" >> "$QUEUE"
        new_for_channel=$(( new_for_channel + 1 ))
      elif [ "$fd_status" = "failed" ] || [ "$fd_status" = "errored" ] || [ "$fd_status" = "downloading" ]; then
        echo "  RETRY: $vid (was $fd_status)"
        # Remove old placeholder/partial file before retry
        old_path=$(sqlite3 "$DB" "SELECT file_path FROM videos WHERE id='$(printf '%s' "$vid" | sed "s/'/''/g")';" 2>/dev/null)
        [ -n "$old_path" ] && [ -f "$YT_ROOT/$old_path" ] && rm -f "$YT_ROOT/$old_path"
        sqlite3 "$DB" "DELETE FROM videos WHERE id='$(printf '%s' "$vid" | sed "s/'/''/g")';"
        printf '%s\t%s\t%s\n' "https://www.youtube.com/watch?v=$vid" "$label" "$max_res" >> "$QUEUE"
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
        printf '%s\t%s\t%s\n' "https://www.youtube.com/watch?v=$vid" "$label" "$max_res" >> "$QUEUE"
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

  # Flush external drive so files are fully written before thumbnail extraction
  sync 2>/dev/null

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

fi  # end thumbs-only skip

# Generate missing thumbnails for Jellyfin (-thumb.jpg next to each .mp4)
# 4-stage fallback: scored frames → first frame → YouTube thumb → text card
# All thumbs get title + channel text overlay for clarity
THUMB_FONT="/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# Copy a local temp file to external drive with retry
# Usage: _safe_copy <src> <dst>
_safe_copy() {
  _sc_src="$1"; _sc_dst="$2"; _sc_try=0
  while [ "$_sc_try" -lt 3 ]; do
    cp "$_sc_src" "$_sc_dst" 2>/dev/null && { rm -f "$_sc_src"; return 0; }
    sleep 2
    _sc_try=$((_sc_try + 1))
  done
  rm -f "$_sc_src"
  return 1
}

# Helper: overlay video title + channel name centered on a base image → final thumb
# Generates locally in /tmp/ first, then copies to external drive
# Usage: thumb_overlay <base_image> <video_title> <channel_name> <output>
thumb_overlay() {
  _tb_base="$1"; _tb_title="$2"; _tb_chan="$3"; _tb_out="$4"
  _tb_tmp="/tmp/thumb_overlay_$$.jpg"
  magick "$_tb_base" -resize '1920x1080^' -gravity center -extent '1920x1080' \
    -font "$THUMB_FONT" \
    -fill '#00000099' -pointsize 52 -gravity Center \
    -annotate +0-6 "$_tb_title" \
    -annotate +6+0 "$_tb_title" \
    -annotate +0+6 "$_tb_title" \
    -annotate -6+0 "$_tb_title" \
    -annotate +4-4 "$_tb_title" \
    -annotate +4+4 "$_tb_title" \
    -annotate -4-4 "$_tb_title" \
    -annotate -4+4 "$_tb_title" \
    -fill white -pointsize 52 -gravity Center -annotate +0+0 "$_tb_title" \
    -fill '#00000099' -pointsize 28 -gravity South -annotate +0+22 "$_tb_chan" \
    -fill '#ffffffAA' -pointsize 28 -gravity South -annotate +0+24 "$_tb_chan" \
    -quality 92 "$_tb_tmp" 2>/dev/null
  [ -f "$_tb_tmp" ] && _safe_copy "$_tb_tmp" "$_tb_out"
}

# Helper: generate a text-card thumb (plain black gradient + white text, always works)
# Generates locally in /tmp/ first, then copies to external drive
# Usage: thumb_textcard <channel_name> <video_title> <output>
thumb_textcard() {
  _tc_chan="$1"; _tc_title="$2"; _tc_out="$3"
  _tc_tmp="/tmp/thumb_textcard_$$.jpg"
  magick -size 1920x1080 'gradient:#1a1a2e-#0a0a0f' \
    -font "$THUMB_FONT" \
    -fill '#ffffff22' -pointsize 52 -gravity Center -annotate +2-2 "$_tc_title" \
    -fill white      -pointsize 52 -gravity Center -annotate +0+0  "$_tc_title" \
    -fill '#ffffffAA' -pointsize 28 -gravity Center -annotate +0+50 "$_tc_chan" \
    -quality 92 "$_tc_tmp" 2>/dev/null
  [ -f "$_tc_tmp" ] && _safe_copy "$_tc_tmp" "$_tc_out"
}

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
    base_name=$(basename "$mp4" .mp4)
    # Channel name from parent directory (look up display name from DB)
    chan_dir=$(dirname "$mp4")
    chan_folder=$(basename "$chan_dir")
    chan_name=$(sqlite3 "$DB" "SELECT display_name FROM channel_aliases WHERE REPLACE(REPLACE(display_name,' ',''),'_','')='$chan_folder' LIMIT 1;" 2>/dev/null)
    [ -z "$chan_name" ] && chan_name="$chan_folder"

    # Get real title from DB (clean, with proper symbols)
    rel_path="$(basename "$chan_dir")/$(basename "$mp4")"
    vid_title=$(sqlite3 "$DB" "SELECT title FROM videos WHERE file_path='$(printf '%s' "$rel_path" | sed "s/'/''/g")';" 2>/dev/null)
    # Fallback: parse from filename if DB has no title or placeholder text
    if [ -z "$vid_title" ] || [ "$vid_title" = "age-restricted" ] || [ "$vid_title" = "members-only" ] || [ "$vid_title" = "unavailable" ]; then
      vid_title=$(echo "$base_name" | sed 's/_S[0-9]*E[0-9]*$//' | sed 's/_/ /g')
    fi
    # Decode HTML entities for display
    vid_title=$(printf '%s' "$vid_title" | sed "s/&#39;/'/g; s/&amp;/\&/g; s/&lt;/</g; s/&gt;/>/g; s/&quot;/\"/g")

    got_base=""

    # --- Stage 1: 4-frame scored extraction (with I/O retry) ---
    _try=0
    while [ "$_try" -lt 2 ]; do
      duration=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$mp4" 2>/dev/null | cut -d. -f1)
      if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
        duration=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$mp4" 2>/dev/null | cut -d. -f1)
      fi
      if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
        duration=$(ffmpeg -i "$mp4" -f null - 2>&1 | grep -oE 'Duration: [0-9:.]+' | head -1 \
          | sed 's/Duration: //' | awk -F: '{print int($1*3600+$2*60+$3)}')
      fi
      if [ -n "$duration" ] && [ "${duration:-0}" -ge 1 ]; then
        break
      fi
      # I/O might be busy — wait and retry once
      if [ "$_try" -eq 0 ]; then
        sleep 3
      fi
      _try=$((_try + 1))
    done

    if [ -n "$duration" ] && [ "${duration:-0}" -ge 1 ]; then
      tmpdir=$(mktemp -d)
      best_score=0
      best_file=""
      for pct in 15 35 55 75; do
        seek=$(( duration * pct / 100 ))
        [ "$seek" -lt 1 ] && seek=1
        candidate="$tmpdir/frame_${pct}.jpg"
        ffmpeg -y -ss "$seek" -i "$mp4" -vframes 1 -update 1 -q:v 2 "$candidate" -loglevel quiet 2>/dev/null || continue
        [ -f "$candidate" ] || continue
        score=$(magick "$candidate" -colorspace Gray -format "%[fx:standard_deviation*1000]" info: 2>/dev/null)
        score=${score:-0}
        score_int=$(printf '%.0f' "$score" 2>/dev/null || echo 0)
        if [ "$score_int" -gt "$best_score" ] 2>/dev/null; then
          best_score=$score_int
          best_file=$candidate
        fi
      done
      if [ -n "$best_file" ] && [ -f "$best_file" ]; then
        got_base="$best_file"
        thumb_overlay "$got_base" "$vid_title" "$chan_name" "$thumb"
        if [ -f "$thumb" ]; then
          echo "  THUMB: $(basename "$thumb") (score: $best_score)"
        else
          _safe_copy "$got_base" "$thumb"
          if [ -f "$thumb" ]; then
            echo "  THUMB (no overlay): $(basename "$thumb") (score: $best_score)"
          fi
        fi
        rm -rf "$tmpdir"
        [ -f "$thumb" ] && continue
      fi
      rm -rf "$tmpdir"
    fi

    # --- Stage 2: First frame with I/O retry ---
    tmp_first="/tmp/thumb_first_$$.jpg"
    _ftry=0
    while [ "$_ftry" -lt 2 ]; do
      ffmpeg -y -i "$mp4" -vframes 1 -update 1 -q:v 2 "$tmp_first" -loglevel quiet 2>/dev/null
      [ -f "$tmp_first" ] && break
      if [ "$_ftry" -eq 0 ]; then sleep 3; fi
      _ftry=$((_ftry + 1))
    done
    if [ -f "$tmp_first" ]; then
      thumb_overlay "$tmp_first" "$vid_title" "$chan_name" "$thumb"
      rm -f "$tmp_first"
      if [ -f "$thumb" ]; then
        echo "  THUMB (first-frame): $(basename "$thumb")"
        continue
      fi
    fi

    # --- Stage 3: YouTube thumbnail download ---
    # Look up video ID from DB using file_path
    rel_path="$(basename "$chan_dir")/$(basename "$mp4")"
    vid_id=$(sqlite3 "$DB" "SELECT id FROM videos WHERE file_path='$(printf '%s' "$rel_path" | sed "s/'/''/g")';" 2>/dev/null)
    if [ -n "$vid_id" ]; then
      tmp_yt="/tmp/thumb_yt_$$.jpg"
      curl -s -f -o "$tmp_yt" "https://img.youtube.com/vi/${vid_id}/maxresdefault.jpg" 2>/dev/null
      if [ -f "$tmp_yt" ] && [ "$(wc -c < "$tmp_yt" | tr -d ' ')" -gt 1000 ]; then
        thumb_overlay "$tmp_yt" "$vid_title" "$chan_name" "$thumb"
        rm -f "$tmp_yt"
        if [ -f "$thumb" ]; then
          echo "  THUMB (youtube): $(basename "$thumb")"
          continue
        fi
      else
        rm -f "$tmp_yt"
      fi
      # Try lower-res fallback
      curl -s -f -o "$tmp_yt" "https://img.youtube.com/vi/${vid_id}/hqdefault.jpg" 2>/dev/null
      if [ -f "$tmp_yt" ] && [ "$(wc -c < "$tmp_yt" | tr -d ' ')" -gt 1000 ]; then
        thumb_overlay "$tmp_yt" "$vid_title" "$chan_name" "$thumb"
        rm -f "$tmp_yt"
        if [ -f "$thumb" ]; then
          echo "  THUMB (youtube-hq): $(basename "$thumb")"
          continue
        fi
      else
        rm -f "$tmp_yt"
      fi
    fi

    # --- Stage 4: Text card (black gradient + white text) ---
    thumb_textcard "$chan_name" "$vid_title" "$thumb"
    if [ -f "$thumb" ]; then
      echo "  THUMB (textcard): $(basename "$thumb")"
    else
      echo "  FAILED: could not generate any thumbnail for $(basename "$mp4")"
    fi
  done
fi

if [ "$MODE" = "thumbs-only" ]; then
  echo "=== Thumbs-only complete ==="
  exit 0
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

# Extract audio for podcastable channels
PODCAST_TRANSFER="$SCRIPT_DIR/podcastableTransfer.sh"
if [ -x "$PODCAST_TRANSFER" ]; then
  "$PODCAST_TRANSFER"
fi

# Sync repo
cd "$SCRIPT_DIR"
git add .
git commit -m "auto: $(date '+%Y-%m-%d %H:%M')" --allow-empty-message -q 2>/dev/null
git pull --rebase -q 2>/dev/null
git push -q 2>/dev/null

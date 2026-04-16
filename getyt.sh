#!/bin/sh
# getyt.sh - YouTube video downloader for Jellyfin library
# Organizes: <channel>/S##E##_<title>.mp4
# INSTALL: brew install yt-dlp ffmpeg
#
# Usage:
#   ./getyt.sh URL [URL...]
#   ./getyt.sh -f FILE_WITH_URLS

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
. "$SCRIPT_DIR/locations.md"
DB="$SCRIPT_DIR/ytdb.db"
FILTER_FILE="$SCRIPT_DIR/filterYT.md"
MAX_CHANNEL=50
MAX_TITLE=80
DOWNLOAD_DELAY=10  # seconds between downloads to avoid rate limits

init_db() {
  mkdir -p "$YT_ROOT"
  sqlite3 "$DB" "CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    url TEXT,
    channel TEXT,
    title TEXT,
    upload_date TEXT,
    download_date INTEGER,
    file_path TEXT,
    status TEXT DEFAULT 'downloaded'
  );"
}

# Strip non-filesystem-safe chars, spaces to underscores, split camelCase, truncate
normalize() {
  local result
  # Remove unsafe chars, collapse whitespace, convert spaces to underscores
  result=$(printf '%s' "$1" | sed 's/[^a-zA-Z0-9 _-]//g; s/  */ /g; s/^ *//; s/ *$//' | tr ' ' '_')
  # Split camelCase boundaries: insert _ before uppercase preceded by lowercase/digit
  result=$(printf '%s' "$result" | sed 's/\([a-z0-9]\)\([A-Z]\)/\1_\2/g')
  # Collapse repeated underscores, trim leading/trailing underscores, truncate
  result=$(printf '%s' "$result" | sed 's/__*/_/g; s/^_//; s/_$//' | cut -c1-"$2")
  [ -z "$result" ] && result="Untitled"
  printf '%s' "$result"
}

# Look up channel name replacement in filterYT.md
apply_filter() {
  if [ -f "$FILTER_FILE" ]; then
    local match
    match=$(awk -F ' *-> *' -v ch="$1" 'tolower($1)==tolower(ch) && NF>=2 {print $2; exit}' "$FILTER_FILE")
    [ -n "$match" ] && { printf '%s' "$match"; return; }
  fi
  printf '%s' "$1"
}

# Return next episode number for a channel directory
next_episode() {
  local dir="$1" season="$2"
  [ ! -d "$dir" ] && { echo 1; return; }
  local max
  if [ -n "$season" ]; then
    max=$(find "$dir" -maxdepth 1 -name "*_S${season}E*.mp4" -exec basename {} \; 2>/dev/null | grep -oE 'E[0-9]+\.' | sed 's/E//; s/\.//' | sort -n | tail -1)
  else
    max=$(find "$dir" -maxdepth 1 -name '*.mp4' -exec basename {} \; 2>/dev/null | grep -oE 'E[0-9]+\.' | sed 's/E//; s/\.//' | sort -n | tail -1)
  fi
  echo $(( $(echo "${max:-0}" | sed 's/^0*//; s/^$/0/') + 1 ))
}

db_check() {
  sqlite3 "$DB" "SELECT status FROM videos WHERE id='$(printf '%s' "$1" | sed "s/'/''/g")';"
}

db_insert() {
  sqlite3 "$DB" "INSERT OR REPLACE INTO videos (id,url,channel,title,upload_date,download_date,file_path,status)
    VALUES (
      '$(printf '%s' "$1" | sed "s/'/''/g")',
      '$(printf '%s' "$2" | sed "s/'/''/g")',
      '$(printf '%s' "$3" | sed "s/'/''/g")',
      '$(printf '%s' "$4" | sed "s/'/''/g")',
      '$(printf '%s' "$5" | sed "s/'/''/g")',
      $6,
      '$(printf '%s' "$7" | sed "s/'/''/g")',
      '$(printf '%s' "${8:-downloaded}" | sed "s/'/''/g")'
    );"
}

# Fetch video title and upload date from YouTube page (for failed yt-dlp downloads)
# Outputs two lines: title, then upload_date (YYYYMMDD or empty)
fetch_page_info() {
  local page
  page=$(curl -sL --max-time 5 "$1" < /dev/null 2>/dev/null)
  printf '%s' "$page" | sed -n 's/.*<title>\(.*\) - YouTube<\/title>.*/\1/p' | head -1
  printf '%s' "$page" | grep -oE '"uploadDate":"[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | tr -d '-'
}

# Generate a placeholder video via external script, then record in DB
generate_placeholder() {
  local reason="$1" url="$2" vid_id="$3" channel_hint="$4" vid_title="$5" upload_date="$6"

  # Resolve channel folder from hint
  local handle_bare
  handle_bare=$(printf '%s' "$channel_hint" | sed 's/^@//')
  [ -z "$handle_bare" ] && return 1

  # Look up display name from DB, fall back to handle
  local display_name
  display_name=$(sqlite3 "$DB" "SELECT display_name FROM channel_aliases WHERE handle='$(printf '%s' "$handle_bare" | sed "s/'/''/g")';" 2>/dev/null)
  [ -z "$display_name" ] && display_name="$handle_bare"

  local channel_name
  channel_name=$(apply_filter "$display_name")
  local channel_norm
  channel_norm=$(normalize "$channel_name" "$MAX_CHANNEL")

  local dest_dir="$YT_ROOT/$channel_norm"
  mkdir -p "$dest_dir"

  local year season ep filename title_norm
  # Use upload year if available, fall back to current year
  year=$(printf '%s' "$upload_date" | cut -c1-4)
  [ -z "$year" ] && year=$(date +%Y)
  season=$(printf '%s' "$year" | cut -c3-4)
  ep=$(next_episode "$dest_dir" "$season")

  # Use video title for filename if available, fall back to reason
  if [ -n "$vid_title" ]; then
    title_norm=$(normalize "$vid_title" "$MAX_TITLE")
  else
    title_norm=$(normalize "$reason" 30)
  fi
  filename=$(printf '%s_S%02dE%02d' "$title_norm" "$season" "$ep")

  local outfile="$dest_dir/$filename.mp4"

  "$SCRIPT_DIR/generatePlaceholder.sh" "$reason" "$url" "$outfile" "$vid_title"

  if [ -f "$outfile" ]; then
    echo "  PLACEHOLDER: $channel_norm/$filename.mp4"
    db_insert "$vid_id" "$url" "$display_name" "$reason" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "$reason"
    return 0
  else
    echo "  WARN: placeholder generation failed"
    return 1
  fi
}

download_video() {
  local url="$1"
  local channel_hint="$2"  # optional @handle from downloadSubs queue
  local max_res="$3"       # optional resolution cap (e.g. 1080)
  echo ""
  echo "--- $url"

  # Extract video ID from URL early (before any network calls)
  local vid_from_url
  vid_from_url=$(printf '%s' "$url" | grep -oE '[a-zA-Z0-9_-]{11}$')

  # Single yt-dlp call: stdout->temp file (json), stderr->variable (errors)
  local tmpjson="/tmp/ytdl_$$"
  local info errmsg
  errmsg=$(yt-dlp --no-playlist --extractor-args "youtube:lang=en" --dump-json "$url" 2>&1 >"$tmpjson") || true
  info=$(cat "$tmpjson" 2>/dev/null)
  rm -f "$tmpjson"

  if [ -z "$info" ]; then
    local esc_id esc_url skip_title skip_upload_date page_info
    esc_id=$(printf '%s' "$vid_from_url" | sed "s/'/''/g")
    esc_url=$(printf '%s' "$url" | sed "s/'/''/g")
    page_info=$(fetch_page_info "$url")
    skip_title=$(printf '%s' "$page_info" | sed -n '1p')
    skip_upload_date=$(printf '%s' "$page_info" | sed -n '2p')

    # Check for age restriction
    if printf '%s' "$errmsg" | grep -qi "Sign in to confirm your age\|age.gate\|age.restricted"; then
      echo "  SKIP (age-restricted): $url"
      if [ -n "$channel_hint" ] && [ -n "$vid_from_url" ]; then
        generate_placeholder "age-restricted" "$url" "$vid_from_url" "$channel_hint" "$skip_title" "$skip_upload_date"
      else
        [ -n "$vid_from_url" ] && sqlite3 "$DB" "INSERT INTO videos (id,url,status) VALUES ('$esc_id','$esc_url','age-restricted') ON CONFLICT(id) DO UPDATE SET status='age-restricted';"
      fi
      return 0
    fi

    # Check for members-only / premium content
    if printf '%s' "$errmsg" | grep -qi "Join this channel\|members.only\|member.exclusive\|requires payment"; then
      echo "  SKIP (members-only): $url"
      if [ -n "$channel_hint" ] && [ -n "$vid_from_url" ]; then
        generate_placeholder "members-only" "$url" "$vid_from_url" "$channel_hint" "$skip_title" "$skip_upload_date"
      else
        [ -n "$vid_from_url" ] && sqlite3 "$DB" "INSERT INTO videos (id,url,status) VALUES ('$esc_id','$esc_url','members-only') ON CONFLICT(id) DO UPDATE SET status='members-only';"
      fi
      return 0
    fi

    # Check for private/deleted/unavailable
    if printf '%s' "$errmsg" | grep -qi "Private video\|Video unavailable\|has been removed\|does not exist"; then
      echo "  SKIP (unavailable): $url"
      if [ -n "$channel_hint" ] && [ -n "$vid_from_url" ]; then
        generate_placeholder "unavailable" "$url" "$vid_from_url" "$channel_hint" "$skip_title" "$skip_upload_date"
      else
        [ -n "$vid_from_url" ] && sqlite3 "$DB" "INSERT INTO videos (id,url,status) VALUES ('$esc_id','$esc_url','unavailable') ON CONFLICT(id) DO UPDATE SET status='unavailable';"
      fi
      return 0
    fi

    # Transient error (rate-limit, IP ban, connection) — do NOT create placeholder
    # Leave video out of DB so it retries naturally next run
    echo "  ERROR: could not fetch metadata (will retry next run)"
    echo "  errmsg: $(printf '%s' "$errmsg" | head -1)"
    return 1
  fi

  # Parse all fields in one python3 call
  eval "$(printf '%s' "$info" | python3 -c "
import sys, json, shlex
d = json.load(sys.stdin)
print(f'vid={shlex.quote(d.get(\"id\",\"\"))}')
print(f'raw_channel={shlex.quote(d.get(\"channel\",\"Unknown\"))}')
print(f'raw_title={shlex.quote(d.get(\"title\",\"Untitled\"))}')
print(f'upload_date={shlex.quote(d.get(\"upload_date\",\"\"))}')
handle = d.get('uploader_id','') or d.get('channel_id','') or ''
if handle.startswith('@'): handle = handle[1:]
print(f'yt_handle={shlex.quote(handle)}')
")"

  if [ -z "$vid" ]; then
    echo "  ERROR: no video ID"
    return 1
  fi

  # Only check DB when called from downloadSubs (-f mode)
  if [ "$SKIP_DB_CHECK" != "1" ]; then
    local exists
    exists=$(db_check "$vid")
    if [ -n "$exists" ] && [ "$exists" != "force-download" ]; then
      echo "  SKIP ($exists): $raw_title"
      return 0
    fi
  fi

  local channel_name
  channel_name=$(apply_filter "$raw_channel")
  local channel_norm
  channel_norm=$(normalize "$channel_name" "$MAX_CHANNEL")

  # When called from downloadSubs with a handle hint, use the folder the scraper
  # already created (single source of truth) instead of re-deriving from yt-dlp
  if [ -n "$channel_hint" ]; then
    local _hint_bare _hint_display _hint_filtered _hint_norm
    _hint_bare=$(printf '%s' "$channel_hint" | sed 's/^@//')
    _hint_display=$(sqlite3 "$DB" "SELECT display_name FROM channel_aliases WHERE handle='$(printf '%s' "$_hint_bare" | sed "s/'/''/g")';" 2>/dev/null)
    [ -n "$_hint_display" ] && {
      _hint_filtered=$(apply_filter "$_hint_display")
      _hint_norm=$(normalize "$_hint_filtered" "$MAX_CHANNEL")
      [ -d "$YT_ROOT/$_hint_norm" ] && channel_norm="$_hint_norm"
    }
  fi

  # Store handle→display_name alias if we got a handle
  if [ -n "$yt_handle" ] && [ "$raw_channel" != "Unknown" ]; then
    sqlite3 "$DB" "INSERT OR REPLACE INTO channel_aliases (handle, display_name) VALUES (
      '$(printf '%s' "$yt_handle" | sed "s/'/''/g")',
      '$(printf '%s' "$raw_channel" | sed "s/'/''/g")');"
  fi
  local title_norm
  title_norm=$(normalize "$raw_title" "$MAX_TITLE")

  local dest_dir="$YT_ROOT/$channel_norm"
  mkdir -p "$dest_dir"

  # Season from upload year (for S##E## naming)
  local year season
  year=$(printf '%s' "$upload_date" | cut -c1-4)
  [ -z "$year" ] && year=$(date +%Y)
  season=$(printf '%s' "$year" | cut -c3-4)

  # Check if a file with the same normalized title already exists on disk
  local existing_file
  existing_file=$(ls "$dest_dir" 2>/dev/null | grep -F "${title_norm}_S" | grep -F '.mp4' | head -1)
  if [ -n "$existing_file" ]; then
    echo "  EXISTS: $dest_dir/$existing_file"
    db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$existing_file" "downloaded"
    return 0
  fi

  local ep
  ep=$(next_episode "$dest_dir" "$season")
  local filename
  filename=$(printf '%s_S%02dE%02d' "$title_norm" "$season" "$ep")

  echo "  -> $channel_norm/$filename.mp4"

  # Mark in DB before downloading to prevent re-queuing on failure
  db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "downloading"

  # Language enforcement:
  # 1) bv+ba (never bv* or b — forces separate streams so language filter works)
  # 2) -S "lang:en" sorts audio preferring English
  # 3) Post-download ffprobe verification
  local sort_spec="lang:en,res,br,acodec,vcodec"
  if [ -n "$max_res" ]; then
    sort_spec="lang:en,res:${max_res},br,acodec,vcodec"
    echo "  quality cap: ${max_res}p"
  fi
  yt-dlp \
    --no-playlist \
    --extractor-args "youtube:lang=en" \
    -S "$sort_spec" \
    -f "bv+ba[language^=en]/bv+ba[language=en]/bv+ba" \
    --merge-output-format mp4 \
    --throttled-rate 100K \
    --sleep-requests 1 \
    -o "$dest_dir/$filename.%(ext)s" \
    "$url"
  local dl_rc=$?

  # Post-download: verify audio is English (or undefined/single-track)
  if [ $dl_rc -eq 0 ] && [ -f "$dest_dir/$filename.mp4" ]; then
    local audio_lang
    audio_lang=$(ffprobe -v quiet -print_format json -show_streams "$dest_dir/$filename.mp4" 2>/dev/null \
      | python3 -c "
import sys, json
d = json.load(sys.stdin)
for s in d.get('streams', []):
    if s.get('codec_type') == 'audio':
        print(s.get('tags', {}).get('language', 'und'))
        break
" 2>/dev/null)
    audio_lang="${audio_lang:-und}"
    case "$audio_lang" in
      en*|und|"") 
        echo "  audio: $audio_lang (ok)"
        ;;
      *)
        echo "  WRONG AUDIO: $audio_lang — deleting and marking no-english"
        rm -f "$dest_dir/$filename.mp4"
        db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "no-english"
        return 1
        ;;
    esac
  fi

  if [ $dl_rc -eq 0 ] || [ -f "$dest_dir/$filename.mp4" ]; then
    db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "downloaded"
    echo "  DONE"

    # Auto-scrape if channel has no tvshow.nfo (e.g. manual download from unsubscribed channel)
    if [ ! -f "$dest_dir/tvshow.nfo" ] && [ -n "$yt_handle" ]; then
      echo "  no tvshow.nfo — scraping channel metadata..."
      python3 "$SCRIPT_DIR/scrapeYT.py" "https://www.youtube.com/@$yt_handle" \
        --yt-root "$YT_ROOT" --filter "$FILTER_FILE" --db "$DB" 2>/dev/null || true
    fi
  elif [ $dl_rc -eq 130 ]; then
    # SIGINT — keep as 'interrupted' so it won't re-queue
    db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "interrupted"
    echo "  INTERRUPTED"
    return 1
  else
    # Transient failure — remove from DB so video retries as new next run
    sqlite3 "$DB" "DELETE FROM videos WHERE id='$(printf '%s' "$vid" | sed "s/'/''/g")';"
    rm -f "$dest_dir/$filename.mp4"
    echo "  FAILED (will retry next run)"
    return 1
  fi

  # Delay between downloads to avoid IP rate limits
  echo "  waiting ${DOWNLOAD_DELAY}s..."
  sleep "$DOWNLOAD_DELAY"
}

# --- Main ---
if [ $# -eq 0 ]; then
  echo "Usage: $0 URL [URL...]"
  echo "       $0 -f FILE_WITH_URLS"
  exit 1
fi

init_db

SKIP_DB_CHECK=0
if [ "$1" = "-f" ] && [ -n "$2" ]; then
  while IFS= read -r line <&3; do
    line=$(printf '%s' "$line" | sed 's/#.*//' | tr -s ' ' | sed 's/^ *//; s/ *$//')
    [ -z "$line" ] && continue
    # Queue lines may be tab-separated: URL\t@handle\tmax_res
    local_url=$(printf '%s' "$line" | cut -f1)
    local_handle=$(printf '%s' "$line" | cut -sf2)
    local_maxres=$(printf '%s' "$line" | cut -sf3)
    download_video "$local_url" "$local_handle" "$local_maxres"
  done 3< "$2"
else
  SKIP_DB_CHECK=1
  for url in "$@"; do
    download_video "$url"
  done
fi

# Cleanup dangling .part files from interrupted downloads
part_count=$(find "$YT_ROOT" -name "*.webm.part" -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "$part_count" -gt 0 ]; then
  echo ""
  echo "Cleaning up $part_count dangling .webm.part file(s)..."
  find "$YT_ROOT" -name "*.webm.part" -type f -delete
fi

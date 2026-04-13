#!/bin/sh
# getyt.sh - YouTube video downloader for Jellyfin library
# Organizes: <channel>/S##E##_<title>.mp4
# INSTALL: brew install yt-dlp ffmpeg
#
# Usage:
#   ./getyt.sh URL [URL...]
#   ./getyt.sh -f FILE_WITH_URLS

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
YT_ROOT="/Volumes/Darrel4tb/YT"
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

# Strip non-filesystem-safe chars, spaces to underscores, truncate
normalize() {
  local result
  result=$(printf '%s' "$1" | sed 's/[^a-zA-Z0-9 _-]//g; s/  */ /g; s/^ *//; s/ *$//' | tr ' ' '_' | cut -c1-"$2")
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
  local dir="$1"
  [ ! -d "$dir" ] && { echo 1; return; }
  local max
  max=$(find "$dir" -maxdepth 1 -name '*.mp4' -exec basename {} \; 2>/dev/null | grep -oE 'E[0-9]+\.' | sed 's/E//; s/\.//' | sort -n | tail -1)
  echo $(( ${max:-0} + 1 ))
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

download_video() {
  local url="$1"
  echo ""
  echo "--- $url"

  local info errmsg
  errmsg=$(yt-dlp --no-playlist --extractor-args "youtube:lang=en" --dump-json "$url" 2>&1 >/dev/null) || true
  info=$(yt-dlp --no-playlist --extractor-args "youtube:lang=en" --dump-json "$url" 2>/dev/null) || true

  if [ -z "$info" ]; then
    # Check for age restriction
    if printf '%s' "$errmsg" | grep -qi "Sign in to confirm your age"; then
      echo "  SKIP (age-restricted): $url"
      local skip_id
      skip_id=$(printf '%s' "$url" | grep -oE '[a-zA-Z0-9_-]{11}$')
      [ -n "$skip_id" ] && sqlite3 "$DB" "INSERT OR IGNORE INTO videos (id,url,status) VALUES ('$skip_id','$url','age-restricted');"
      return 0
    fi
    echo "  ERROR: could not fetch metadata"
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
  ep=$(next_episode "$dest_dir")
  local filename
  filename=$(printf '%s_S%02dE%02d' "$title_norm" "$season" "$ep")

  echo "  -> $channel_norm/$filename.mp4"

  # Mark in DB before downloading to prevent re-queuing on failure
  db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "downloading"

  # Language enforcement:
  # 1) bv+ba (never bv* or b — forces separate streams so language filter works)
  # 2) -S "lang:en" sorts audio preferring English
  # 3) Post-download ffprobe verification
  yt-dlp \
    --no-playlist \
    --extractor-args "youtube:lang=en" \
    -S "lang:en,res,br,acodec,vcodec" \
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
        echo "  WRONG AUDIO: $audio_lang — deleting and marking failed"
        rm -f "$dest_dir/$filename.mp4"
        db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "failed"
        return 1
        ;;
    esac
  fi

  if [ $dl_rc -eq 0 ] || [ -f "$dest_dir/$filename.mp4" ]; then
    db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "downloaded"
    echo "  DONE"
  elif [ $dl_rc -eq 130 ]; then
    # SIGINT — keep as 'interrupted' so it won't re-queue
    db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "interrupted"
    echo "  INTERRUPTED"
    return 1
  else
    db_insert "$vid" "$url" "$raw_channel" "$raw_title" "$upload_date" "$(date +%s)" "$channel_norm/$filename.mp4" "failed"
    echo "  FAILED"
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
  while IFS= read -r line; do
    line=$(printf '%s' "$line" | sed 's/#.*//' | tr -s ' ' | sed 's/^ *//; s/ *$//')
    [ -z "$line" ] && continue
    download_video "$line"
  done < "$2"
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

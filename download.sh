#!/bin/sh
# download.sh - Universal downloader
# YouTube videos -> /Volumes/Darrel4tb/YT/ (via getyt.sh)
# Torrents/magnets -> /Volumes/Jellyfin/Movies/ or /Volumes/Jellyfin/Shows/
# Audiobooks       -> /Volumes/Jellyfin/Books/
# Books (ebooks)   -> /Volumes/Jellyfin/Books/
#
# Usage:
#   download <youtube-url|video-id>         YouTube video
#   download s                              Show torrent (paste magnet, auto-match folder)
#   download m                              Movie torrent (paste magnet)
#   download a                              Audiobook torrent (paste magnet)
#   download b                              Book/ebook torrent (paste magnet)
#   download episode                        Same as s
#   download movie                          Same as m
#   download                                Paste any URL, auto-detect type
#   download --help                         Show this help
#
# INSTALL: brew install yt-dlp ffmpeg aria2

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
. "$SCRIPT_DIR/locations.md"
GETYT="$SCRIPT_DIR/getyt.sh"
LOG="$SCRIPT_DIR/downloader.log"

log_msg() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M')" "$1" >> "$LOG"
}

# --- Detect input type ---

is_youtube() {
  case "$1" in
    *youtube.com/watch*|*youtu.be/*) return 0 ;;
  esac
  # Bare 11-char video ID
  printf '%s' "$1" | grep -qE '^[a-zA-Z0-9_-]{11}$'
}

is_torrent() {
  case "$1" in
    magnet:*) return 0 ;;
    *.torrent) return 0 ;;
    *) return 1 ;;
  esac
}

# --- YouTube download ---

do_youtube() {
  local input="$1"
  # Normalize bare ID to full URL
  case "$input" in
    *youtube.com/*|*youtu.be/*) ;;
    *) input="https://www.youtube.com/watch?v=$input" ;;
  esac
  echo "YouTube: $input"
  if "$GETYT" "$input"; then
    log_msg "OK  youtube  $input"
  else
    log_msg "FAIL  youtube  $input"
  fi
}

# --- Extract torrent name from magnet dn= parameter ---

torrent_name() {
  # Extract dn= value and URL-decode it
  printf '%s' "$1" | grep -oE 'dn=[^&]+' | sed 's/^dn=//' \
    | python3 -c "import sys, urllib.parse; print(urllib.parse.unquote(sys.stdin.read().strip()))" 2>/dev/null
}

# --- Fuzzy match torrent name against show folders ---

fuzzy_match_show() {
  local tname="$1"
  # Strip common noise: group tags [xxx], resolution, codec, source info
  local cleaned
  cleaned=$(printf '%s' "$tname" | sed -E '
    s/\[[^]]*\]//g
    s/\([^)]*\)//g
    s/[._-]/ /g
    s/ (S[0-9]+|E[0-9]+|Season|1080p|2160p|720p|480p|WEB|WEBRip|BluRay|BDRip|HEVC|x265|x264|AAC|FLAC|DDP|MultiSub|Dual Audio|COMPLETE|Batch|10bit|CR|NF|DSNP|HULU|MAX|HDR|DV|H264|H265|Opus|EAC3|MP4|MKV)//gi
    s/  */ /g
    s/^ *//; s/ *$//
  ')
  [ -z "$cleaned" ] && return 1

  # Extract key words (3+ chars), try matching against folder names
  local best="" best_score=0
  for d in "$SHOWS_DIR"/*/; do
    [ ! -d "$d" ] && continue
    local fname
    fname=$(basename "$d")
    local score=0

    # Check each word from cleaned torrent name against folder name (case-insensitive)
    for word in $cleaned; do
      [ ${#word} -lt 3 ] && continue
      if printf '%s' "$fname" | grep -qi "$word"; then
        score=$((score + 1))
      fi
    done

    if [ "$score" -gt "$best_score" ]; then
      best_score=$score
      best="$fname"
    fi
  done

  # Need at least 2 matching words to be confident
  if [ "$best_score" -ge 2 ] && [ -n "$best" ]; then
    printf '%s' "$best"
    return 0
  fi
  return 1
}

# --- Show folder picker ---

pick_show_folder() {
  echo ""
  echo "=== Pick a show folder ==="
  echo ""

  # Collect folders into a numbered list
  local i=1
  local folders=""
  for d in "$SHOWS_DIR"/*/; do
    [ ! -d "$d" ] && continue
    name=$(basename "$d")
    folders="$folders
$name"
    printf '  %2d) %s\n' "$i" "$name"
    i=$((i + 1))
  done
  local total=$((i - 1))

  echo ""
  printf '   0) ** Create new folder **\n'
  echo ""
  printf 'Pick [0-%d]: ' "$total"
  read -r choice

  # Validate
  case "$choice" in
    ''|*[!0-9]*) echo "Cancelled."; return 1 ;;
  esac
  if [ "$choice" -lt 0 ] || [ "$choice" -gt "$total" ] 2>/dev/null; then
    echo "Invalid choice."; return 1
  fi

  if [ "$choice" -eq 0 ]; then
    printf 'New folder name: '
    read -r new_name
    [ -z "$new_name" ] && { echo "Cancelled."; return 1; }
    DEST="$SHOWS_DIR/$new_name"
    mkdir -p "$DEST"
    echo "Created: $DEST"
  else
    DEST="$SHOWS_DIR/$(printf '%s' "$folders" | sed -n "$((choice + 1))p")"
  fi
  return 0
}

# --- Torrent download ---

do_torrent() {
  local input="$1"
  local mode="$2"  # optional: s, m, or empty (prompt)
  echo "Torrent: $input"

  local choice="$mode"
  if [ -z "$choice" ]; then
    echo ""
    echo "  s) Show (auto-match folder)"
    echo "  sf) Show (full show, dump in Shows/)"
    echo "  m) Movie"
    echo "  a) Audiobook"
    echo "  b) Book / ebook"
    echo "  sc) Show (pick folder)"
    echo "  n) Cancel"
    echo ""
    printf 'Type [s/sf/m/a/b/sc/n]: '
    read -r choice
  fi

  case "$choice" in
    m|M)
      DEST="$MOVIES_DIR"
      echo "-> Movies: $DEST"
      ;;
    a|A)
      DEST="$BOOKS_DIR"
      echo "-> Audiobooks: $DEST"
      ;;
    b|B)
      DEST="$BOOKS_DIR"
      echo "-> Books: $DEST"
      ;;
    sf|SF)
      DEST="$SHOWS_DIR"
      echo "-> Shows (full): $DEST"
      ;;
    s|S)
      # Fast mode: auto-detect, no confirm, just go
      local tname match
      tname=$(torrent_name "$input")
      if [ -n "$tname" ]; then
        match=$(fuzzy_match_show "$tname")
      fi

      if [ -n "$match" ]; then
        DEST="$SHOWS_DIR/$match"
        echo "  -> $match"
      else
        echo "  No match found."
        if ! pick_show_folder; then return 1; fi
      fi
      echo "-> Show folder: $DEST"
      ;;
    sc|SC)
      # Interactive mode: detect + confirm + picker
      local tname match
      tname=$(torrent_name "$input")
      if [ -n "$tname" ]; then
        match=$(fuzzy_match_show "$tname")
      fi

      if [ -n "$match" ]; then
        echo ""
        echo "  Detected: $match"
        printf '  Correct? [Y/n/pick]: '
        read -r confirm
        case "$confirm" in
          n|N|p|pick)
            if ! pick_show_folder; then return 1; fi
            ;;
          *)
            DEST="$SHOWS_DIR/$match"
            ;;
        esac
      else
        echo ""
        echo "  No match found."
        if ! pick_show_folder; then return 1; fi
      fi
      echo "-> Show folder: $DEST"
      ;;
    n|N|"")
      echo "Cancelled."
      return 1
      ;;
    *)
      echo "Invalid choice. Cancelled."
      return 1
      ;;
  esac

  echo ""
  echo "Downloading to: $DEST"
  echo "This may take a while..."
  echo ""

  if aria2c \
    --seed-time=0 \
    --dir="$DEST" \
    --summary-interval=30 \
    --console-log-level=notice \
    --file-allocation=falloc \
    "$input"; then
    log_msg "OK  torrent  $input  →  $DEST"
    echo ""
    echo "Done! Saved to: $DEST"
  else
    log_msg "FAIL  torrent  $input  →  $DEST"
    echo ""
    echo "Download failed."
    return 1
  fi
}

# --- Help ---

show_help() {
  echo "download - Universal downloader (YouTube + torrents)"
  echo ""
  echo "Usage:"
  echo "  download <youtube-url>        Download YouTube video"
  echo "  download <video-id>           Download YouTube video by ID"
  echo "  download s                    Show torrent (paste magnet, auto-match folder)"
  echo "  download m                    Movie torrent (paste magnet)"
  echo "  download a                    Audiobook torrent (paste magnet)"
  echo "  download b                    Book/ebook torrent (paste magnet)"
  echo "  download episode              Same as s"
  echo "  download movie                Same as m"
  echo "  download audiobook            Same as a"
  echo "  download book                 Same as b"
  echo "  download                      Paste any URL, auto-detect type"
  echo ""
  echo "YouTube  -> $YT_ROOT/<Channel>/"
  echo "Movies   -> $MOVIES_DIR/"
  echo "Shows    -> $SHOWS_DIR/<folder>/"
  echo "Books    -> $BOOKS_DIR/"
  echo ""
  echo "Tip: magnets have & so always quote them or use: download s (then paste)"
}

# --- Main ---

case "${1:-}" in
  -h|--help|help) show_help; exit 0 ;;
esac

torrent_mode=""
if [ $# -ge 1 ]; then
  case "$1" in
    s|S|show|episode) torrent_mode="s"; shift ;;
    sc|SC)            torrent_mode="sc"; shift ;;
    sf|SF)            torrent_mode="sf"; shift ;;
    m|M|movie)        torrent_mode="m"; shift ;;
    a|A|audiobook)    torrent_mode="a"; shift ;;
    b|B|book|ebook)    torrent_mode="b"; shift ;;
  esac
fi

# If no URL arg left, prompt (avoids $* expanding & in magnets)
if [ $# -eq 0 ]; then
  printf 'Paste URL: '
  read -r input
  [ -z "$input" ] && { echo "No input."; exit 1; }
else
  input="$1"
fi

if is_youtube "$input"; then
  do_youtube "$input"
elif is_torrent "$input"; then
  do_torrent "$input" "$torrent_mode"
else
  echo "Unrecognized input: $input"
  echo "Expected: YouTube URL/ID, magnet link, or .torrent URL"
  echo "Run 'download --help' for usage."
  exit 1
fi

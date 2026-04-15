#!/bin/sh
# download.sh - Universal downloader
# YouTube videos → /Volumes/Darrel4tb/YT/ (via getyt.sh)
# Torrents/magnets → /Volumes/Jellyfin/Movies/ or /Volumes/Jellyfin/Shows/
#
# Usage:
#   ./download.sh <youtube-url|video-id|magnet|torrent-url>
#
# INSTALL: brew install yt-dlp ffmpeg aria2

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
GETYT="$SCRIPT_DIR/getyt.sh"
LOG="$SCRIPT_DIR/downloader.log"
MOVIES_DIR="/Volumes/Jellyfin/Movies"
SHOWS_DIR="/Volumes/Jellyfin/Shows"

log_msg() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M')" "$1" >> "$LOG"
}

# ── Detect input type ──────────────────────────────────────────────

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

# ── YouTube download ───────────────────────────────────────────────

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

# ── Show folder picker ─────────────────────────────────────────────

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

# ── Torrent download ──────────────────────────────────────────────

do_torrent() {
  local input="$1"
  echo "Torrent: $input"
  echo ""
  echo "  s) Show"
  echo "  m) Movie"
  echo "  sc) Show (select folder)"
  echo "  n) Cancel"
  echo ""
  printf 'Type [s/m/sc/n]: '
  read -r choice

  case "$choice" in
    m|M)
      DEST="$MOVIES_DIR"
      echo "→ Movies: $DEST"
      ;;
    s|S)
      DEST="$SHOWS_DIR"
      echo "→ Shows: $DEST"
      ;;
    sc|SC)
      if ! pick_show_folder; then
        return 1
      fi
      echo "→ Show folder: $DEST"
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

# ── Main ──────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
  echo "Usage: $0 <youtube-url|video-id|magnet-link|torrent-url>"
  exit 1
fi

input="$1"

if is_youtube "$input"; then
  do_youtube "$input"
elif is_torrent "$input"; then
  do_torrent "$input"
else
  echo "Unrecognized input: $input"
  echo "Expected: YouTube URL/ID, magnet link, or .torrent URL"
  exit 1
fi

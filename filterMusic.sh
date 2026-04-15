#!/bin/sh
# filterMusic.sh - Move audio files under 60 seconds to a mirror folder
#
# Original stays intact: /Volumes/Jellyfin/Music/
# Short tracks moved to: /Volumes/Jellyfin/non-music/
#
# Mirror preserves the full folder structure so files can be
# moved back manually if wrongly classified.
#
# Usage:
#   ./filterMusic.sh              # move short files
#   ./filterMusic.sh --dry-run    # preview what would move

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
. "$SCRIPT_DIR/locations.md"
MIRROR_DIR="$MUSIC_MIRROR_DIR"

DRY_RUN=0
[ "$1" = "--dry-run" ] && DRY_RUN=1

if [ ! -d "$MUSIC_DIR" ]; then
  echo "ERROR: $MUSIC_DIR not mounted"
  exit 1
fi

MOVED=0
SKIPPED=0
ERRORS=0

# Find all audio files
find "$MUSIC_DIR" -type f \( \
  -iname '*.mp3' -o -iname '*.flac' -o -iname '*.m4a' -o \
  -iname '*.wav' -o -iname '*.ogg' -o -iname '*.opus' -o \
  -iname '*.wma' -o -iname '*.aac' -o -iname '*.alac' \
\) > /tmp/filter_music_files_$$

while IFS= read -r filepath; do

  # Get duration in seconds via ffprobe
  duration=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$filepath" 2>/dev/null)

  # Skip if ffprobe can't read it
  if [ -z "$duration" ]; then
    ERRORS=$(( ERRORS + 1 ))
    continue
  fi

  # Truncate to integer for comparison
  secs=$(printf '%.0f' "$duration" 2>/dev/null)
  [ -z "$secs" ] && continue

  if [ "$secs" -lt 60 ]; then
    # Build mirror path preserving folder structure
    rel_path="${filepath#$MUSIC_DIR/}"
    mirror_path="$MIRROR_DIR/$rel_path"
    mirror_parent=$(dirname "$mirror_path")

    if [ "$DRY_RUN" -eq 1 ]; then
      echo "  WOULD MOVE (${secs}s): $rel_path"
    else
      mkdir -p "$mirror_parent"
      mv "$filepath" "$mirror_path"
      echo "  MOVED (${secs}s): $rel_path"
    fi
    MOVED=$(( MOVED + 1 ))
  else
    SKIPPED=$(( SKIPPED + 1 ))
  fi
done < /tmp/filter_music_files_$$
rm -f /tmp/filter_music_files_$$

echo ""
if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run complete. $MOVED file(s) would be moved."
else
  echo "Done. $MOVED file(s) moved to $MIRROR_DIR"
fi

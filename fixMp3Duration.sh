#!/usr/bin/env bash
# fixMp3Duration.sh
# Scans PODCASTS_DIR for mp3 files that Audiobookshelf reads as short (e.g. 10 min)
# when the actual playback is much longer (e.g. 2 hours).
#
# Root cause: yt-dlp downloads as M4A/MP4 then converts to MP3 via ffmpeg, but the
# MP4 container tags (major_brand=isom, compatible_brands=isomiso2avc1mp41, etc.)
# bleed into the ID3 header of the resulting mp3. The music-metadata library that
# Audiobookshelf uses gets confused by these MP4 atoms and misreads the duration,
# showing e.g. 10 minutes instead of the real 2-hour episode length.
#
# Root fix: podcastableTransfer.sh now uses -map_metadata -1 -id3v2_version 3 on
# every new conversion, so new files are always clean.
# This script remains as a one-time (and ongoing) repair tool for files already on disk.
#
# Usage:
#   sh fixMp3Duration.sh            # scan + fix all bad files
#   sh fixMp3Duration.sh --dry-run  # report only, no changes
#   sh fixMp3Duration.sh --scan     # report only (alias for --dry-run)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/locations.md" 2>/dev/null || true
PODCASTS_DIR="${PODCASTS_DIR:-/Volumes/Jellyfin/Podcasts}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" || "${1:-}" == "--scan" ]] && DRY_RUN=1

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ERROR: ffprobe not found. Install via: brew install ffmpeg"
  exit 1
fi

fixed=0; bad=0; checked=0

echo "Scanning $PODCASTS_DIR for mp3 files with MP4 tag contamination..."
echo ""

while IFS= read -r -d '' f; do
  ((checked++)) || true

  # Detect MP4 container tags embedded in ID3 (major_brand=isom etc.)
  # These confuse ABS/music-metadata into misreading duration.
  # Use JSON output so the key name is preserved for accurate matching.
  brand=$(ffprobe -v error -show_entries format_tags -of json "$f" 2>/dev/null \
    | grep -i 'major_brand' | grep -o '"[^"]*"$' | tr -d '"')

  [ -z "$brand" ] && continue

  ((bad++)) || true
  actual_dur=$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null | head -1)
  actual_s=${actual_dur%.*}
  actual_fmt=$(printf '%dh%02dm' $((actual_s/3600)) $(((actual_s%3600)/60)))

  echo "BAD: $(basename "$f")"
  echo "     has MP4 tags (major_brand=$brand)  real_dur=$actual_fmt"

  if [ "$DRY_RUN" -eq 0 ]; then
    tmp="${f%.mp3}._fix.mp3"
    if ffmpeg -loglevel error -i "$f" -c copy -map_metadata -1 \
        -id3v2_version 3 "$tmp" && [ -s "$tmp" ]; then
      mv "$tmp" "$f"
      ((fixed++)) || true
      echo "     FIXED ✓"
    else
      rm -f "$tmp"
      echo "     FAILED to fix"
    fi
  fi
  echo ""

done < <(find "$PODCASTS_DIR" -name '*.mp3' -type f -print0 2>/dev/null)

echo "---"
echo "Checked: $checked  Bad: $bad  Fixed: $fixed"
[ "$DRY_RUN" -eq 1 ] && echo "(dry-run — no files changed)"

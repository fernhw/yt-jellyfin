#!/bin/sh
# normalizeBackNames.sh - One-shot repair for broken episode numbering + missing thumbnails
#
# Problems this fixes:
#   1. Multiple E00 files per season (backfill batch all got E00)
#   2. DB file_path references year subdirs that don't exist on disk
#   3. Missing -thumb.jpg files (cron couldn't find ffprobe/magick due to PATH)
#
# Strategy:
#   - For each channel, group mp4s by season (S##)
#   - Sort E00 files by upload_date from DB (oldest first)
#   - Renumber starting from E01, preserving existing non-E00 episode slots
#   - Rename both .mp4 and matching -thumb.jpg
#   - Update DB file_path to match new filename
#   - After renaming, generate any missing thumbnails
#
# Usage:
#   ./normalizeBackNames.sh              # dry run (default - SAFE)
#   ./normalizeBackNames.sh --apply      # actually rename files
#   ./normalizeBackNames.sh --thumbs     # only regenerate missing thumbnails

set -e

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
YT_ROOT="/Volumes/Darrel4tb/YT"
DB="$SCRIPT_DIR/ytdb.db"
MODE="dry-run"

case "${1:-}" in
  --apply)      MODE="apply" ;;
  --thumbs)     MODE="thumbs-only" ;;
  --dry-run|"") MODE="dry-run" ;;
  *)
    echo "Usage: $0 [--apply | --dry-run | --thumbs]"
    exit 1
    ;;
esac

if [ ! -d "$YT_ROOT" ]; then
  echo "ERROR: $YT_ROOT not mounted"
  exit 1
fi

if [ ! -f "$DB" ]; then
  echo "ERROR: database not found at $DB"
  exit 1
fi

RENAME_COUNT=0
THUMB_COUNT=0
ERR_COUNT=0

echo "=== normalizeBackNames [$MODE] $(date '+%Y-%m-%d %H:%M:%S') ==="
echo ""

# -----------------------------------------------------------------------
# PHASE 1: Fix episode numbering
# -----------------------------------------------------------------------
if [ "$MODE" != "thumbs-only" ]; then
  echo "--- Phase 1: Fixing episode numbering ---"
  echo ""

  for channel_dir in "$YT_ROOT"/*/; do
    [ ! -d "$channel_dir" ] && continue
    channel=$(basename "$channel_dir")

    # Collect all seasons in this channel (anchor to _S##E to avoid false matches like PS5)
    seasons=$(find "$channel_dir" -maxdepth 1 -name '*.mp4' -exec basename {} \; 2>/dev/null \
      | grep -oE '_S[0-9]+E' | sed 's/^_//; s/E$//' | sort -u)

    [ -z "$seasons" ] && continue

    for season in $seasons; do
      # Find E00 files for this season
      e00_files=$(find "$channel_dir" -maxdepth 1 -name "*_${season}E00.mp4" 2>/dev/null)
      [ -z "$e00_files" ] && continue

      e00_count=$(printf '%s\n' "$e00_files" | grep -c .)

      # Find already-used episode numbers for this season (non-E00)
      # Strip leading zeros so grep -qx matches plain integers (1 vs 01)
      used_episodes=$(find "$channel_dir" -maxdepth 1 -name "*_${season}E*.mp4" -exec basename {} \; 2>/dev/null \
        | grep -v "${season}E00" \
        | grep -oE 'E[0-9]+\.' | sed 's/E//; s/\.//' | sed 's/^0*//' | sed 's/^$/0/' | sort -n)

      echo "  $channel / $season: $e00_count E00 file(s) to renumber"

      # Build list of E00 files with upload dates, sorted oldest-first
      # We query the DB for upload_date, fall back to file mtime
      sorted_e00=$(printf '%s\n' "$e00_files" | while IFS= read -r f; do
        [ -z "$f" ] && continue
        bn=$(basename "$f")
        db_path="$channel/$bn"
        upload_date=$(sqlite3 "$DB" "SELECT upload_date FROM videos WHERE file_path='$(printf '%s' "$db_path" | sed "s/'/''/g")';" 2>/dev/null)
        if [ -z "$upload_date" ]; then
          # Fallback: use file modification time as YYYYMMDD
          upload_date=$(stat -f '%Sm' -t '%Y%m%d' "$f" 2>/dev/null || echo "99999999")
        fi
        printf '%s\t%s\n' "$upload_date" "$f"
      done | sort -t"$(printf '\t')" -k1,1 | cut -f2)

      # Assign episode numbers: find gaps starting from E01
      next_ep=1
      printf '%s\n' "$sorted_e00" | while IFS= read -r f; do
        [ -z "$f" ] && continue

        # Skip episode numbers already taken
        while printf '%s\n' "$used_episodes" | grep -qx "$next_ep" 2>/dev/null; do
          next_ep=$(( next_ep + 1 ))
        done

        bn=$(basename "$f")
        # Build new filename: replace S##E00 with S##E## (zero-padded)
        new_ep=$(printf 'E%02d' "$next_ep")
        new_bn=$(printf '%s' "$bn" | sed "s/${season}E00/${season}${new_ep}/")
        new_path="$channel_dir$new_bn"

        if [ "$bn" = "$new_bn" ]; then
          next_ep=$(( next_ep + 1 ))
          continue
        fi

        # Check for corresponding thumbnail
        old_thumb="${f%.mp4}-thumb.jpg"
        new_thumb="${new_path%.mp4}-thumb.jpg"

        if [ "$MODE" = "dry-run" ]; then
          echo "    RENAME: $bn -> $new_bn"
          [ -f "$old_thumb" ] && echo "    RENAME: $(basename "$old_thumb") -> $(basename "$new_thumb")"
        elif [ "$MODE" = "apply" ]; then
          # Safety: don't overwrite existing files
          if [ -f "$new_path" ]; then
            echo "    SKIP: $new_bn already exists!"
            ERR_COUNT=$(( ERR_COUNT + 1 ))
          else
            mv "$f" "$new_path"
            echo "    RENAMED: $bn -> $new_bn"
            RENAME_COUNT=$(( RENAME_COUNT + 1 ))

            # Rename thumbnail if it exists
            if [ -f "$old_thumb" ]; then
              mv "$old_thumb" "$new_thumb"
              echo "    RENAMED: $(basename "$old_thumb") -> $(basename "$new_thumb")"
            fi

            # Rename any matching .trickplay directory
            old_trick="${f%.mp4}.trickplay"
            new_trick="${new_path%.mp4}.trickplay"
            if [ -d "$old_trick" ]; then
              mv "$old_trick" "$new_trick"
              echo "    RENAMED: $(basename "$old_trick") -> $(basename "$new_trick")"
            fi

            # Update DB file_path
            old_db_path="$channel/$bn"
            new_db_path="$channel/$new_bn"
            sqlite3 "$DB" "UPDATE videos SET file_path='$(printf '%s' "$new_db_path" | sed "s/'/''/g")' WHERE file_path='$(printf '%s' "$old_db_path" | sed "s/'/''/g")';"

            # Also fix DB entries that reference year subdirs (e.g. Channel/2026/S26E01_Title.mp4)
            # These need to point to the flat file instead
            sqlite3 "$DB" "UPDATE videos SET file_path='$(printf '%s' "$new_db_path" | sed "s/'/''/g")' WHERE file_path LIKE '$(printf '%s' "$channel" | sed "s/'/''/g")/%/$(printf '%s' "$new_bn" | sed "s/'/''/g; s/${season}${new_ep}/${season}E%/")';"
          fi
        fi

        next_ep=$(( next_ep + 1 ))
      done
    done
  done

  # Fix DB entries referencing year subdirs that don't match any E00 rename
  # (e.g. files already properly numbered but DB says Channel/2026/S26E01_...)
  if [ "$MODE" = "apply" ]; then
    echo ""
    echo "--- Fixing stale DB paths (year subdir references) ---"
    sqlite3 "$DB" "SELECT id, file_path FROM videos WHERE file_path LIKE '%/20__/%' AND status='downloaded';" 2>/dev/null \
      | while IFS='|' read -r vid old_path; do
        [ -z "$vid" ] && continue
        # Extract channel and filename, skip the year dir
        channel_part=$(printf '%s' "$old_path" | cut -d/ -f1)
        filename=$(basename "$old_path")
        flat_path="$channel_part/$filename"

        # Check if the flat file exists on disk
        if [ -f "$YT_ROOT/$flat_path" ]; then
          sqlite3 "$DB" "UPDATE videos SET file_path='$(printf '%s' "$flat_path" | sed "s/'/''/g")' WHERE id='$(printf '%s' "$vid" | sed "s/'/''/g")';"
          echo "  DB FIX: $old_path -> $flat_path"
        else
          # Maybe the file on disk uses the old Title_S##E##.mp4 convention
          # Search for a file containing the same S##E## tag
          se_tag=$(printf '%s' "$filename" | grep -oE 'S[0-9]+E[0-9]+')
          if [ -n "$se_tag" ]; then
            disk_match=$(find "$YT_ROOT/$channel_part" -maxdepth 1 -name "*${se_tag}*" -name "*.mp4" 2>/dev/null | head -1)
            if [ -n "$disk_match" ]; then
              real_path="$channel_part/$(basename "$disk_match")"
              sqlite3 "$DB" "UPDATE videos SET file_path='$(printf '%s' "$real_path" | sed "s/'/''/g")' WHERE id='$(printf '%s' "$vid" | sed "s/'/''/g")';"
              echo "  DB FIX: $old_path -> $real_path"
            else
              echo "  DB WARN: $old_path — file not found on disk"
            fi
          fi
        fi
      done
  fi

  echo ""
  if [ "$MODE" = "dry-run" ]; then
    echo "--- Dry run complete. Re-run with --apply to rename files. ---"
  else
    echo "--- Phase 1 complete: $RENAME_COUNT file(s) renamed, $ERR_COUNT error(s) ---"
  fi
  echo ""
fi

# -----------------------------------------------------------------------
# PHASE 2: Generate missing thumbnails
# -----------------------------------------------------------------------
echo "--- Phase 2: Generating missing thumbnails ---"
echo ""

missing_count=0
for mp4 in "$YT_ROOT"/*/*.mp4; do
  [ -f "$mp4" ] || continue
  thumb="${mp4%.mp4}-thumb.jpg"
  [ -f "$thumb" ] && continue
  missing_count=$(( missing_count + 1 ))
done

if [ "$missing_count" -eq 0 ]; then
  echo "  All thumbnails present — nothing to do."
else
  echo "  $missing_count video(s) missing thumbnails"
  echo ""

  for mp4 in "$YT_ROOT"/*/*.mp4; do
    [ -f "$mp4" ] || continue
    thumb="${mp4%.mp4}-thumb.jpg"
    [ -f "$thumb" ] && continue

    bn=$(basename "$mp4")

    # Get duration
    duration=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$mp4" 2>/dev/null | cut -d. -f1)

    if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
      # ffprobe failed on duration — try alternate approach: count frames via stream info
      duration=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$mp4" 2>/dev/null | cut -d. -f1)
    fi

    if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
      # Still nothing — try getting duration by decoding a small portion
      duration=$(ffmpeg -i "$mp4" -f null - 2>&1 | grep -oE 'Duration: [0-9:.]+' | head -1 \
        | sed 's/Duration: //' | awk -F: '{print int($1*3600+$2*60+$3)}')
    fi

    if [ -z "$duration" ] || [ "${duration:-0}" -lt 1 ]; then
      echo "  SKIP (truly unreadable): $bn"
      ERR_COUNT=$(( ERR_COUNT + 1 ))
      continue
    fi

    # Try the smart 4-frame scoring approach first
    tmpdir=$(mktemp -d)
    best_score=0
    best_file=""
    extracted=0

    for pct in 15 35 55 75; do
      seek=$(( duration * pct / 100 ))
      [ "$seek" -lt 1 ] && seek=1
      candidate="$tmpdir/frame_${pct}.jpg"
      ffmpeg -y -ss "$seek" -i "$mp4" -vframes 1 -update 1 -q:v 2 "$candidate" -loglevel quiet 2>/dev/null || continue
      [ -f "$candidate" ] || continue
      extracted=$(( extracted + 1 ))

      # Score with ImageMagick if available
      if command -v magick >/dev/null 2>&1; then
        score=$(magick "$candidate" -colorspace Gray -format "%[fx:standard_deviation*1000]" info: 2>/dev/null || echo "0")
        score_int=$(printf '%.0f' "$score" 2>/dev/null || echo 0)
        if [ "$score_int" -gt "$best_score" ] 2>/dev/null; then
          best_score=$score_int
          best_file=$candidate
        fi
      else
        # No magick — just use the first successful frame
        if [ -z "$best_file" ]; then
          best_file=$candidate
        fi
      fi
    done

    if [ -n "$best_file" ] && [ -f "$best_file" ]; then
      if [ "$MODE" = "dry-run" ]; then
        echo "  WOULD THUMB: $bn (score: $best_score)"
      else
        mv "$best_file" "$thumb"
        echo "  THUMB: $bn (score: $best_score)"
        THUMB_COUNT=$(( THUMB_COUNT + 1 ))
      fi
    elif [ "$extracted" -eq 0 ]; then
      # All 4 frame extractions failed — try single grab at 10% or 5 seconds
      seek=$(( duration / 10 ))
      [ "$seek" -lt 2 ] && seek=2
      if [ "$MODE" = "dry-run" ]; then
        ffmpeg -y -ss "$seek" -i "$mp4" -vframes 1 -update 1 -q:v 2 "$tmpdir/fallback.jpg" -loglevel quiet 2>/dev/null
        if [ -f "$tmpdir/fallback.jpg" ]; then
          echo "  WOULD THUMB (fallback): $bn"
        else
          echo "  FAIL: $bn — could not extract any frame"
          ERR_COUNT=$(( ERR_COUNT + 1 ))
        fi
      else
        ffmpeg -y -ss "$seek" -i "$mp4" -vframes 1 -update 1 -q:v 2 "$thumb" -loglevel quiet 2>/dev/null
        if [ -f "$thumb" ]; then
          echo "  THUMB (fallback): $bn"
          THUMB_COUNT=$(( THUMB_COUNT + 1 ))
        else
          # Last resort: grab the very first frame (no seeking)
          ffmpeg -y -i "$mp4" -vframes 1 -update 1 -q:v 2 "$thumb" -loglevel quiet 2>/dev/null
          if [ -f "$thumb" ]; then
            echo "  THUMB (first-frame): $bn"
            THUMB_COUNT=$(( THUMB_COUNT + 1 ))
          else
            echo "  FAIL: $bn — no frame could be extracted"
            ERR_COUNT=$(( ERR_COUNT + 1 ))
          fi
        fi
      fi
    fi
    rm -rf "$tmpdir"
  done
fi

echo ""
echo "--- Phase 2 complete: $THUMB_COUNT thumbnail(s) generated, $ERR_COUNT error(s) ---"
echo ""
echo "=== normalizeBackNames complete ==="

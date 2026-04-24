#!/bin/sh
# podcastableTransfer.sh - Extract audio from podcastable YT channels
# Reads [podcastable] from channelConfig.md, converts .mp4 -> .mp3
# into PODCASTS_DIR/<channel>/ for Audiobookshelf podcast library
#
# Skips files already extracted. Mirrors YT folder structure.
# Called automatically by downloadSubs.sh before git commit.

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
. "$SCRIPT_DIR/locations.md"
CONFIG_FILE="$SCRIPT_DIR/channelConfig.md"
DB="$SCRIPT_DIR/ytdb.db"
FILTER_FILE="$SCRIPT_DIR/filterYT.md"

# Read podcastable channel list from config
get_podcastable() {
  [ ! -f "$CONFIG_FILE" ] && return
  awk '/^\[podcastable\]/{found=1; next} /^\[/{found=0} found && /^[^#]/ && NF{print $1}' "$CONFIG_FILE" | awk '!seen[tolower($0)]++'
}

handle_bare() {
  printf '%s' "$1" | sed 's/^@//'
}

normalize_channel_name() {
  printf '%s' "$1" \
    | sed 's/[^a-zA-Z0-9 _-]//g; s/  */ /g; s/^ *//; s/ *$//' \
    | tr -d ' _'
}

apply_filter() {
  if [ -f "$FILTER_FILE" ]; then
    local mapped
    mapped=$(awk -F ' *-> *' -v ch="$1" 'tolower($1)==tolower(ch) && NF>=2 {print $2; exit}' "$FILTER_FILE")
    [ -n "$mapped" ] && {
      printf '%s' "$mapped"
      return
    }
  fi
  printf '%s' "$1"
}

find_channel_dir() {
  local handle="$1" bare display filtered folder
  bare=$(handle_bare "$handle")
  [ -z "$bare" ] && return 1

  display=$(sqlite3 "$DB" "SELECT display_name FROM channel_aliases WHERE handle='$(printf '%s' "$bare" | sed "s/'/''/g")';" 2>/dev/null)

  filtered=$(apply_filter "${display:-$bare}")
  folder=$(normalize_channel_name "$filtered")
  if [ -d "$YT_ROOT/$folder" ]; then
    printf '%s' "$YT_ROOT/$folder"
    return 0
  fi

  filtered=$(apply_filter "$bare")
  folder=$(normalize_channel_name "$filtered")
  if [ -d "$YT_ROOT/$folder" ]; then
    printf '%s' "$YT_ROOT/$folder"
    return 0
  fi

  return 1
}

count=0
skipped=0
errors=0
missing_handles=""

# Write handle list to temp file to avoid pipe subshell
HANDLE_LIST=$(mktemp)
get_podcastable > "$HANDLE_LIST"

while IFS= read -r handle; do
  [ -z "$handle" ] && continue

  src_dir=$(find_channel_dir "$handle")
  if [ -z "$src_dir" ]; then
    missing_handles="${missing_handles}${handle}\n"
    continue
  fi

  channel_name=$(basename "$src_dir")
  dest_dir="$PODCASTS_DIR/$channel_name"
  mkdir -p "$dest_dir"

  # Copy channel poster as podcast cover (ABS expects cover.jpg in folder)
  if [ ! -f "$dest_dir/cover.jpg" ]; then
    if [ -f "$src_dir/poster.jpg" ]; then
      cp "$src_dir/poster.jpg" "$dest_dir/cover.jpg"
    elif [ -f "$src_dir/folder.jpg" ]; then
      cp "$src_dir/folder.jpg" "$dest_dir/cover.jpg"
    fi
  fi

  for mp4 in "$src_dir"/*.mp4; do
    [ ! -f "$mp4" ] && continue

    base=$(basename "$mp4" .mp4)

    # Extract S##E## tag and build clean name: "S26E03 Were Getting Dumber Trash Taste 303"
    sxex=$(printf '%s' "$base" | grep -oE 'S[0-9]+E[0-9]+')
    if [ -n "$sxex" ]; then
      # Remove the S##E## and surrounding underscores, then replace _ with spaces
      title=$(printf '%s' "$base" | sed -E "s/_?${sxex}_?//" | tr '_' ' ' | sed 's/^ *//;s/ *$//')
      clean_name="${sxex} ${title}"
    else
      clean_name=$(printf '%s' "$base" | tr '_' ' ')
    fi

    mp3="$dest_dir/$clean_name.mp3"

    # Skip if already extracted (check new name)
    if [ -f "$mp3" ]; then
      skipped=$((skipped + 1))
      continue
    fi

    # Also skip if old-style name exists (already done before rename migration)
    old_mp3="$dest_dir/$base.mp3"
    if [ -f "$old_mp3" ]; then
      skipped=$((skipped + 1))
      continue
    fi

    echo "podcast: $channel_name/$clean_name"
    if ffmpeg -nostdin -i "$mp4" -vn -q:a 2 -y "$mp3" -loglevel warning 2>&1; then
      count=$((count + 1))
    else
      echo "podcast: FAILED $mp4"
      rm -f "$mp3"
      errors=$((errors + 1))
    fi
  done
done < "$HANDLE_LIST"

rm -f "$HANDLE_LIST"
if [ -n "$missing_handles" ]; then
  missing_count=$(printf '%b' "$missing_handles" | sed '/^$/d' | wc -l | tr -d ' ')
  missing_list=$(printf '%b' "$missing_handles" | sed '/^$/d' | paste -sd ', ' -)
  echo "podcast: no YT folder yet for ${missing_count} channel(s): $missing_list -- skipping"
fi
echo "podcast: done -- extracted=$count skipped=$skipped errors=$errors"

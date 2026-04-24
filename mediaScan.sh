#!/bin/bash
# mediaScan.sh — scan Jellyfin/ABS libraries for new media items
# Usage: sh mediaScan.sh [output_file]
# State: .media_state  (persists last scan epoch)
# Output: FIELD_SEP-delimited records → output_file

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

SCAN_OUT="${1:-/tmp/media_scan_items.txt}"
THUMBS_DIR="web/media-thumbs"
STATE_FILE=".media_state"
FIRST_RUN_DAYS=7   # look back this many days on very first run

mkdir -p "$THUMBS_DIR"
> "$SCAN_OUT"

# ── Load path vars from locations.md ─────────────────────────────────────────
MOVIES_DIR=/Volumes/Jellyfin/Movies
SHOWS_DIR=/Volumes/Jellyfin/Shows
MUSIC_DIR=/Volumes/Jellyfin/Music
BOOKS_DIR=/Volumes/Jellyfin/Books
MANGA_DIR=/Volumes/Jellyfin/Manga

if [ -f "$SCRIPT_DIR/locations.md" ]; then
  _loc() { grep -v '^#' "$SCRIPT_DIR/locations.md" | grep "^${1}=" 2>/dev/null | cut -d= -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'; }
  v=$(_loc MOVIES_DIR); [ -n "$v" ] && MOVIES_DIR="$v"
  v=$(_loc SHOWS_DIR);  [ -n "$v" ] && SHOWS_DIR="$v"
  v=$(_loc MUSIC_DIR);  [ -n "$v" ] && MUSIC_DIR="$v"
  v=$(_loc BOOKS_DIR);  [ -n "$v" ] && BOOKS_DIR="$v"
  v=$(_loc MANGA_DIR);  [ -n "$v" ] && MANGA_DIR="$v"
fi

# ── State: rolling 30-hour window ────────────────────────────────────────────
now=$(date +%s)

# ── Scan state: when did we last check for new files ─────────────────────────────────
last_scan=0
if [ -f "$STATE_FILE" ]; then
  last_scan=$(grep '^last_scan=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
  printf '%d' "$last_scan" >/dev/null 2>&1 || last_scan=0
fi
[ "$last_scan" -eq 0 ] && last_scan=$((now - FIRST_RUN_DAYS * 86400))

# ── Display cache: items stay on site for 30h from when first seen ──────────────
CACHE_FILE="$SCRIPT_DIR/.media_cache"
DISPLAY_WINDOW=$((30 * 3600))
_cache_tmp=$(mktemp)
if [ -f "$CACHE_FILE" ]; then
  while IFS= read -r _line; do
    [ -z "$_line" ] && continue
    _at=$(printf '%s' "$_line" | awk -F'\037' '{print $5}')
    [ $(( now - _at )) -le $DISPLAY_WINDOW ] && printf '%s\n' "$_line"
  done < "$CACHE_FILE" > "$_cache_tmp"
fi

_in_cache() {
  awk -F'\037' -v c="$1" -v t="$2" '$1==c && $2==t{found=1;exit} END{exit !found}' "$_cache_tmp" 2>/dev/null
}

# ── Helpers ───────────────────────────────────────────────────────────────────
fmtime() { stat -f '%m' "$1" 2>/dev/null || stat -c '%Y' "$1" 2>/dev/null || echo 0; }
is_new()  { [ "$(fmtime "$1")" -gt "$last_scan" ]; }

safe_html() { printf '%s' "$1" | sed 's/&/\&amp;/g;s/</\&lt;/g;s/>/\&gt;/g;s/"/\&quot;/g'; }

safe_key() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' \
    | sed 's/--*/-/g;s/^-*//;s/-*$//'
}

copy_thumb() {
  local src="$1" key="$2" dest="$THUMBS_DIR/${2}.jpg"
  if [ -f "$src" ] && { [ ! -f "$dest" ] || [ "$src" -nt "$dest" ]; }; then
    cp "$src" "$dest"
  fi
  [ -f "$dest" ] && printf 'media-thumbs/%s.jpg' "$key"
}

find_cover() {
  local dir="$1"
  for name in folder.jpg cover.jpg Cover.jpg poster.jpg; do
    [ -f "$dir/$name" ] && printf '%s/%s' "$dir" "$name" && return
  done
  find "$dir" -maxdepth 2 \( -iname "folder.jpg" -o -iname "cover.jpg" \) 2>/dev/null | head -1
}

emit() {
  # Add to display cache only if this item isn't tracked yet; keep original added_at
  local cat="$1" title="$2" sub="$3" thumb="$4"
  _in_cache "$cat" "$title" && return
  printf '%s\037%s\037%s\037%s\037%s\n' "$cat" "$title" "$sub" "$thumb" "$now" >> "$_cache_tmp"
}

# ── Title cleaners ────────────────────────────────────────────────────────────
clean_movie() {
  printf '%s' "$1" \
    | sed 's/\.[12][0-9]\{3\}[^a-zA-Z].*//; s/ ([12][0-9]\{3\}).*//; s/ [12][0-9]\{3\} .*//' \
    | sed 's/\./ /g; s/  */ /g; s/^ //; s/ $//'
}

clean_show() {
  printf '%s' "$1" \
    | sed 's/[._-][Ss][0-9]\{1,2\}[._].*//; s/[._][12][0-9]\{3\}[._].*//; s/\./ /g' \
    | sed 's/  */ /g; s/^ //; s/ $//'
}

clean_music() {
  printf '%s' "$1" \
    | sed 's/ \[[Mm][Pp]3\]//; s/ \[[Ff][Ll][Aa][Cc]\]//; s/ \[[^\]]*\]//g' \
    | sed "s/ ([Oo]riginal[^)]*[Ss]oundtrack[^)]*)//" \
    | sed 's/ [Oo]riginal [Ss]oundtrack//' \
    | sed 's/  */ /g; s/^ //; s/ $//'
}

clean_book() {
  # "48 Laws of Power [B00WYDJ2YQ]" → "48 Laws of Power"
  printf '%s' "$1" \
    | sed 's/ \[[A-Z0-9]*\].*//; s/\.[^.]*$//; s/: .*//; s/ - .*//' \
    | sed 's/  */ /g; s/^ //; s/ $//'
}

clean_manga_series() {
  # "Berserk v01 (2003) (Digital).cbz" → "Berserk"
  printf '%s' "$1" \
    | sed 's/ [Vv][0-9]\{1,3\}[^a-zA-Z].*//; s/ [Vv]ol\.[0-9]*.*//; s/\.[^.]*$//' \
    | sed 's/ ([12][0-9]\{3\}).*//' \
    | sed 's/ (Digital.*//; s/  */ /g; s/^ //; s/ $//'
}

ep_code() {
  printf '%s' "$1" | grep -oE '[Ss][0-9]{1,2}[Ee][0-9]{2,3}' | head -1 | tr '[:lower:]' '[:upper:]'
}

# ── MOVIES ────────────────────────────────────────────────────────────────────
if [ -d "$MOVIES_DIR" ]; then
  find "$MOVIES_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | while IFS= read -r d; do
    is_new "$d" || continue
    name=$(basename "$d")
    t=$(clean_movie "$name"); [ -z "$t" ] && t="$name"
    cover=$(find_cover "$d")
    thumb=""; [ -n "$cover" ] && thumb=$(copy_thumb "$cover" "movie-$(safe_key "$name")")
    emit "movie" "$(safe_html "$t")" "" "$thumb" "$(fmtime "$d")"
  done
fi

# ── SHOWS ─────────────────────────────────────────────────────────────────────
if [ -d "$SHOWS_DIR" ]; then
  find "$SHOWS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | while IFS= read -r show_dir; do
    eps_file=$(mktemp)
    mt_file=$(mktemp); printf '0' > "$mt_file"

    while IFS= read -r ep; do
      mt=$(fmtime "$ep")
      [ "$mt" -le "$last_scan" ] && continue
      code=$(ep_code "$(basename "$ep")")
      [ -n "$code" ] && printf '%s\n' "$code" >> "$eps_file"
      cur=$(cat "$mt_file")
      [ "$mt" -gt "$cur" ] && printf '%s' "$mt" > "$mt_file"
    done < <(find "$show_dir" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.avi" -o -name "*.m4v" \) 2>/dev/null)

    if [ -s "$eps_file" ]; then
      max_mt=$(cat "$mt_file")
      ec=$(sort -u "$eps_file" | wc -l | tr -d ' ')
      # Is the show folder itself new? → new show
      folder_mt=$(fmtime "$show_dir")
      if [ "$folder_mt" -gt "$last_scan" ]; then
        kind="newshow"
      else
        kind="neweps"
      fi
      if [ "$ec" -gt 3 ]; then
        ep_label="${ec} new episodes"
      else
        ep_label=$(sort -u "$eps_file" | tr '\n' ' ' | sed 's/ $//')
      fi
      sub="${kind}:${ep_label}"
      name=$(basename "$show_dir")
      t=$(clean_show "$name"); [ -z "$t" ] && t="$name"
      cover=$(find_cover "$show_dir")
      thumb=""; [ -n "$cover" ] && thumb=$(copy_thumb "$cover" "show-$(safe_key "$name")")
      emit "show" "$(safe_html "$t")" "$(safe_html "$sub")" "$thumb" "$max_mt"
    fi
    rm -f "$eps_file" "$mt_file"
  done
fi

# ── MUSIC ─────────────────────────────────────────────────────────────────────
if [ -d "$MUSIC_DIR" ]; then
  find "$MUSIC_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | while IFS= read -r d; do
    is_new "$d" || continue
    name=$(basename "$d")
    t=$(clean_music "$name"); [ -z "$t" ] && t="$name"
    cover=$(find_cover "$d")
    [ -z "$cover" ] && cover=$(find "$d" -maxdepth 2 -iname "cover.jpg" 2>/dev/null | head -1)
    thumb=""; [ -n "$cover" ] && thumb=$(copy_thumb "$cover" "music-$(safe_key "$name")")
    emit "music" "$(safe_html "$t")" "" "$thumb" "$(fmtime "$d")"
  done
fi

# ── AUDIOBOOKS ────────────────────────────────────────────────────────────────
if [ -d "$BOOKS_DIR" ]; then
  find "$BOOKS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | while IFS= read -r d; do
    is_new "$d" || continue
    name=$(basename "$d")
    t=$(clean_book "$name"); [ -z "$t" ] && t="$name"
    cover=$(find_cover "$d")
    thumb=""; [ -n "$cover" ] && thumb=$(copy_thumb "$cover" "book-$(safe_key "$name")")
    emit "book" "$(safe_html "$t")" "" "$thumb" "$(fmtime "$d")"
  done
fi

# ── MANGA (group by series) ───────────────────────────────────────────────────
if [ -d "$MANGA_DIR" ]; then
  manga_raw=$(mktemp)
  find "$MANGA_DIR" -maxdepth 2 \( -iname "*.cbz" -o -iname "*.epub" -o -iname "*.pdf" \) 2>/dev/null \
    | sort | while IFS= read -r f; do
    mt=$(fmtime "$f")
    [ "$mt" -le "$last_scan" ] && continue
    series=$(clean_manga_series "$(basename "$f")")
    [ -z "$series" ] && continue
    printf '%s\t%s\n' "$series" "$mt"
  done > "$manga_raw"

  if [ -s "$manga_raw" ]; then
    awk -F'\t' '{
      count[$1]++
      if ($2+0 > mtime[$1]+0) mtime[$1]=$2
    } END {
      for (s in count) printf "%s\t%d\t%s\n", s, count[s], mtime[s]
    }' "$manga_raw" | sort | while IFS=$'\t' read -r series count mt; do
      [ "$count" -gt 1 ] && sub="${count} volumes" || sub="1 volume"
      emit "manga" "$(safe_html "$series")" "$(safe_html "$sub")" "" "$mt"
    done
  fi
  rm -f "$manga_raw"
fi

# ── Save state ─────────────────────────────────────────────────────────────
cp "$_cache_tmp" "$CACHE_FILE"
cat "$_cache_tmp" > "$SCAN_OUT"
rm -f "$_cache_tmp"
printf 'last_scan=%s\n' "$now" > "$STATE_FILE"
total=$(wc -l < "$SCAN_OUT" | tr -d ' ')
printf 'media scan: %s item(s) in 30h window\n' "$total" >&2

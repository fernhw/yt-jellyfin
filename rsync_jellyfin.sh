#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
. "$SCRIPT_DIR/locations.md"

DATA_SRC="/Users/alexander-highground/Library/Application Support/jellyfin/data"
MUSIC_SRC="$MUSIC_DIR"
BOOKS_SRC="/Volumes/Jellyfin/Books"
PODCASTS_SRC="/Volumes/Jellyfin/Podcasts"
MANGA_SRC="/Volumes/Jellyfin/Manga"

DATA_DEST="/Volumes/Darrel4tb/rsync/data"
MUSIC_DEST="/Volumes/Darrel4tb/rsync/music"
BOOKS_DEST="/Volumes/Darrel4tb/rsync/books"
PODCASTS_DEST="/Volumes/Darrel4tb/rsync/podcasts"
MANGA_DEST="/Volumes/Darrel4tb/rsync/manga"

LOG_FILE="/Volumes/Darrel4tb/rsync/sync.log"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOG_FILE"
}

# ---- PER-LIBRARY SYNC (simple rule: src exists + dest exists/creatable = sync) ----

sync_library() {
  local label="$1" src="$2" dest="$3" dest_parent rc

  if [ ! -d "$src" ]; then
    log "SKIP $label — source not found: $src"
    return
  fi

  if [ ! -d "$dest" ]; then
    dest_parent=$(dirname "$dest")
    if [ ! -d "$dest_parent" ]; then
      log "SKIP $label — destination parent missing: $dest_parent"
      return
    fi
    mkdir -p "$dest"
    if [ $? -ne 0 ]; then
      log "SKIP $label — failed to create destination: $dest"
      return
    fi
  fi

  log "Syncing $label ..."
  rsync -avh --delete "$src/" "$dest/" >> "$LOG_FILE" 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    log "OK $label"
  else
    log "ERROR $label — rsync failed (exit $rc)"
  fi
}

log "=== Sync started ==="

sync_library "data"     "$DATA_SRC"     "$DATA_DEST"
sync_library "music"    "$MUSIC_SRC"    "$MUSIC_DEST"
sync_library "books"    "$BOOKS_SRC"    "$BOOKS_DEST"
sync_library "podcasts" "$PODCASTS_SRC" "$PODCASTS_DEST"
sync_library "manga"    "$MANGA_SRC"    "$MANGA_DEST"

log "=== Sync complete ==="
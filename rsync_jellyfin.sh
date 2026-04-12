#!/bin/bash

DATA_SRC="/Users/alexander-highground/Library/Application Support/jellyfin/data"
MUSIC_SRC="/Volumes/Jellyfin/Music"

DATA_DEST="/Volumes/Darrel4tb/rsync/data"
MUSIC_DEST="/Volumes/Darrel4tb/rsync/music"

LOG_FILE="/Volumes/Darrel4tb/rsync/sync.log"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOG_FILE"
}

# ---- CHECKS ----

if [ ! -d "$DATA_SRC" ] || [ ! -d "$MUSIC_SRC" ]; then
  log "Source missing — aborting sync"
  exit 1
fi

if [ -z "$(ls -A "$MUSIC_SRC")" ]; then
  log "Music source is EMPTY — possible mount failure — aborting"
  exit 1
fi

FILE_COUNT=$(find "$MUSIC_SRC" -type f | wc -l)
if [ "$FILE_COUNT" -lt 100 ]; then
  log "File count too low ($FILE_COUNT) — suspicious — aborting"
  exit 1
fi

# ---- SYNC ----

log "Starting safe mirror sync"

rsync -avh --delete "$DATA_SRC/" "$DATA_DEST/" >> "$LOG_FILE" 2>&1
rsync -avh --delete "$MUSIC_SRC/" "$MUSIC_DEST/" >> "$LOG_FILE" 2>&1

log "Sync complete"
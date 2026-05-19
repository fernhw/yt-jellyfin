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

PASSWORDS_SRC="/Users/alexander-highground/vaultwarden-data"
PASSWORDS_DEST_1="/Volumes/Darrel4tb/rsync/passwords"
PASSWORDS_DEST_2="/Volumes/Jellyfin/rsync/passwords"
PASSWORDS_DEST_3="/Users/alexander-highground/Documents/rsync/passwords"

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
      # Try creating the full path if the volume root is mounted
      local volume_root
      volume_root=$(echo "$dest" | cut -d'/' -f1-3)
      if [ ! -d "$volume_root" ]; then
        log "SKIP $label — volume not mounted: $volume_root"
        return
      fi
      mkdir -p "$dest"
    else
      mkdir -p "$dest"
    fi
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
brew services restart cloudflared
# or if running as a launchd service:
launchctl stop com.cloudflare.cloudflared && launchctl start com.cloudflare.cloudflared
# ---- VAULTWARDEN BACKUP (encrypted vault — db + RSA key both required to restore) ----
# Note: passwords are client-side encrypted; server never sees plaintext.
# Both db.sqlite3 and rsa_key.pem are needed together to restore a working instance.
sync_library "passwords→darrel4tb" "$PASSWORDS_SRC" "$PASSWORDS_DEST_1"
sync_library "passwords→jellyfin"  "$PASSWORDS_SRC" "$PASSWORDS_DEST_2"
sync_library "passwords→documents" "$PASSWORDS_SRC" "$PASSWORDS_DEST_3"

log "=== Sync complete ==="
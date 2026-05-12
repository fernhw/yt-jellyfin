#!/bin/sh
# updateLibraries.sh - Daily update of yt-dlp and spotdl
LOG="/Users/alexander-highground/Projects/yt-jellyfin/updateLibraries.log"

echo "=== $(date) ===" >> "$LOG"

# Update yt-dlp
echo "[yt-dlp] Updating..." >> "$LOG"
/opt/homebrew/bin/yt-dlp -U >> "$LOG" 2>&1

# Update spotdl
echo "[spotdl] Updating..." >> "$LOG"
/usr/bin/python3 -m pip install --upgrade spotdl >> "$LOG" 2>&1

echo "[done]" >> "$LOG"

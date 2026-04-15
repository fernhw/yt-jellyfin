#!/bin/sh
# reportMaker.sh - "What to Watch Today" report for Jellyfin
# Focuses on what's new TODAY, ranked by priority and rarity.
# Called at the end of downloadSubs.sh each run.
#
# Ranking logic:
#   1. Priority channels (from channelConfig.md [priority]) always on top
#   2. "Rare drops" — channels that haven't uploaded in 30+ days get highlighted
#   3. Everything else grouped by channel
#
# Files:
#   todayReport.md               - Current day (updated each run)
#   dailyReport.md               - Today + yesterday combined view
#   reportsArchive/YYYYMMDD.md   - Past days (archived on day change)

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
TODAY_REPORT="$SCRIPT_DIR/todayReport.md"
DAILY_REPORT="$SCRIPT_DIR/dailyReport.md"
ARCHIVE_DIR="$SCRIPT_DIR/reportsArchive"
DB="$SCRIPT_DIR/ytdb.db"
CONFIG="$SCRIPT_DIR/channelConfig.md"
VARS_FILE="$SCRIPT_DIR/varsYT.md"

TODAY=$(date '+%Y-%m-%d')
NOW_HUMAN=$(date '+%B %d, %Y at %I:%M %p')
DAY_OF_WEEK=$(date '+%A')
RARE_THRESHOLD=30

# --- Day rollover: archive previous day's report ---
mkdir -p "$ARCHIVE_DIR"
if [ -f "$TODAY_REPORT" ]; then
  old_date=$(head -5 "$TODAY_REPORT" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)
  if [ -n "$old_date" ] && [ "$old_date" != "$TODAY" ]; then
    archive_name=$(printf '%s' "$old_date" | tr -d '-')
    mv "$TODAY_REPORT" "$ARCHIVE_DIR/${archive_name}.md"
  fi
fi

# --- Read priority channels (preserve order) ---
PRIORITY_LIST=""
if [ -f "$CONFIG" ]; then
  in_priority=0
  while IFS= read -r line; do
    case "$line" in
      \[priority\]*) in_priority=1; continue ;;
      \[*) in_priority=0; continue ;;
    esac
    if [ "$in_priority" -eq 1 ]; then
      clean=$(echo "$line" | sed 's/#.*//' | tr -d ' ')
      [ -n "$clean" ] && PRIORITY_LIST="$PRIORITY_LIST|$clean"
    fi
  done < "$CONFIG"
  PRIORITY_LIST="${PRIORITY_LIST#|}"
fi

# --- Greeting ---
hour=$(date '+%H')
if [ "$hour" -lt 12 ]; then
  greeting="Good morning"
elif [ "$hour" -lt 17 ]; then
  greeting="Good afternoon"
else
  greeting="Good evening"
fi

# --- Query: videos downloaded today with gap since channel's previous upload ---
TODAYS_VIDEOS=$(sqlite3 "$DB" "
  WITH today AS (
    SELECT channel, title, upload_date
    FROM videos
    WHERE date(download_date, 'unixepoch', 'localtime') = date('now', 'localtime')
      AND status = 'downloaded'
      AND julianday('now') - julianday(
            substr(upload_date,1,4)||'-'||substr(upload_date,5,2)||'-'||substr(upload_date,7,2)
          ) <= 3
  ),
  with_gap AS (
    SELECT t.channel, t.title, t.upload_date,
      (SELECT MAX(v2.upload_date) FROM videos v2
       WHERE v2.channel = t.channel
         AND v2.upload_date < t.upload_date
         AND v2.status = 'downloaded') as prev_date
    FROM today t
  )
  SELECT g.channel,
         g.title,
         g.upload_date,
         CASE WHEN g.prev_date IS NOT NULL THEN
           CAST(julianday(
             substr(g.upload_date,1,4)||'-'||substr(g.upload_date,5,2)||'-'||substr(g.upload_date,7,2)
           ) - julianday(
             substr(g.prev_date,1,4)||'-'||substr(g.prev_date,5,2)||'-'||substr(g.prev_date,7,2)
           ) AS INTEGER)
         ELSE -1 END as gap_days
  FROM with_gap g
  ORDER BY g.upload_date DESC, g.channel
")

# Count non-empty lines
VIDEO_COUNT=0
if [ -n "$TODAYS_VIDEOS" ]; then
  VIDEO_COUNT=$(printf '%s\n' "$TODAYS_VIDEOS" | grep -c '|')
fi

# --- Categorize into priority / rare / regular ---
TMP_PRI="/tmp/rpt_pri_$$"
TMP_RARE="/tmp/rpt_rare_$$"
TMP_REG="/tmp/rpt_reg_$$"
: > "$TMP_PRI"
: > "$TMP_RARE"
: > "$TMP_REG"

is_priority_channel() {
  # $1 = channel name to check against priority list
  # Normalizes: lowercase, strip @, spaces, underscores, apostrophes, trailing punctuation
  [ -z "$PRIORITY_LIST" ] && return 1
  local chan_norm
  chan_norm=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed "s/[@ _'\"]//g")
  local matched=1
  printf '%s\n' "$PRIORITY_LIST" | tr '|' '\n' | while IFS= read -r pch; do
    pch_norm=$(printf '%s' "$pch" | tr '[:upper:]' '[:lower:]' | sed "s/[@ _'\"]//g")
    # Check exact match or if one contains the other (handles "asmongoldtv" vs "asmongold")
    if [ "$chan_norm" = "$pch_norm" ]; then
      echo "Y"; break
    elif printf '%s' "$chan_norm" | grep -q "$pch_norm"; then
      echo "Y"; break
    elif printf '%s' "$pch_norm" | grep -q "$chan_norm"; then
      echo "Y"; break
    fi
  done | grep -q "Y" && matched=0
  return $matched
}

printf '%s\n' "$TODAYS_VIDEOS" | while IFS='|' read -r chan title upload gap; do
  [ -z "$chan" ] && continue

  # Clean display name (strip leading @, trailing spaces)
  display_chan=$(printf '%s' "$chan" | sed 's/^@//; s/ *$//')

  # Gap annotation
  gap_note=""
  is_rare=0
  if [ "$gap" -ge "$RARE_THRESHOLD" ] 2>/dev/null; then
    gap_note=" — *${gap} days since their last upload*"
    is_rare=1
  fi

  line="- **$title** — *$display_chan*$gap_note"

  if is_priority_channel "$chan"; then
    echo "$line" >> "$TMP_PRI"
  elif [ "$is_rare" -eq 1 ]; then
    echo "$line" >> "$TMP_RARE"
  else
    echo "$line" >> "$TMP_REG"
  fi
done

PRI_N=$(wc -l < "$TMP_PRI" | tr -d ' ')
RARE_N=$(wc -l < "$TMP_RARE" | tr -d ' ')
REG_N=$(wc -l < "$TMP_REG" | tr -d ' ')

# --- Disk usage (precise, no rounding) ---
YT_ROOT="/Volumes/Darrel4tb/YT"
YT_USED=$(du -sk "$YT_ROOT" 2>/dev/null | awk '{printf "%.2f GB", $1/1048576}')
DISK_FREE=$(df /Volumes/Darrel4tb 2>/dev/null | awk 'NR==2{printf "%.2f TB", ($4*512)/1099511627776}')
YT_USED=${YT_USED:-"?"}
DISK_FREE=${DISK_FREE:-"?"}

# --- Write report ---
cat > "$TODAY_REPORT" <<HEADER
# What to Watch — $TODAY

> *$greeting! Here's what landed on $DAY_OF_WEEK, $NOW_HUMAN.*
> **YT Mirror:** ${YT_USED} used · ${DISK_FREE} free

HEADER

if [ "$VIDEO_COUNT" -eq 0 ]; then
  cat >> "$TODAY_REPORT" <<QUIET
Nothing new today. All channels scanned — nobody posted. Check back later.

QUIET
else
  # --- Priority ---
  if [ "$PRI_N" -gt 0 ]; then
    cat >> "$TODAY_REPORT" <<SEC
## Watch First

$(cat "$TMP_PRI")

---

SEC
  else
    cat >> "$TODAY_REPORT" <<SEC
## Watch First

No priority uploads today — your favorites are taking a break.

---

SEC
  fi

  # --- Rare drops ---
  if [ "$RARE_N" -gt 0 ]; then
    cat >> "$TODAY_REPORT" <<SEC
## Rare Drops

These channels have been quiet for a while — worth a look.

$(cat "$TMP_RARE")

---

SEC
  fi

  # --- Regular ---
  if [ "$REG_N" -gt 0 ]; then
    cat >> "$TODAY_REPORT" <<SEC
## Also New

$(cat "$TMP_REG")

---

SEC
  fi
fi

# --- Cleanup ---
rm -f "$TMP_PRI" "$TMP_RARE" "$TMP_REG"

# --- Error summary (one message for connection/rate-limit issues) ---
ERR_LAST=0
SCANNED=0
if [ -f "$VARS_FILE" ]; then
  ERR_LAST=$(grep '^errors_last_run=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
  SCANNED=$(grep '^channels_scanned=' "$VARS_FILE" 2>/dev/null | cut -d= -f2-)
fi
ERR_LAST=${ERR_LAST:-0}
SCANNED=${SCANNED:-0}
# Only show if a significant portion of channels failed (>50% = likely IP ban)
if [ "$ERR_LAST" -gt 0 ] && [ "$SCANNED" -gt 0 ]; then
  half=$(( SCANNED / 2 ))
  if [ "$ERR_LAST" -gt "$half" ]; then
    cat >> "$TODAY_REPORT" <<SEC

## Heads Up

Last scan hit a wall — $ERR_LAST of $SCANNED channels returned nothing. YouTube is likely rate-limiting or IP-blocking. Will retry next run.

SEC
  elif [ "$ERR_LAST" -gt 3 ]; then
    cat >> "$TODAY_REPORT" <<SEC

## Heads Up

$ERR_LAST channel(s) timed out during the last scan. Could be a flaky connection — will retry next run.

SEC
  fi
fi

echo "  report written to todayReport.md"

# --- Build dailyReport.md: today + yesterday combined ---
YESTERDAY=$(date -v-1d '+%Y%m%d' 2>/dev/null || date -d 'yesterday' '+%Y%m%d' 2>/dev/null)
YESTERDAY_FILE="$ARCHIVE_DIR/${YESTERDAY}.md"

cat > "$DAILY_REPORT" <<DAILY_HDR
# Daily Report

DAILY_HDR

# Today section — inline the full todayReport content with bumped headers
if [ -f "$TODAY_REPORT" ]; then
  # Replace leading "# " with "## " so top heading becomes h2, subsections h3
  sed 's/^# /## /; s/^## Watch/### Watch/; s/^## Rare/### Rare/; s/^## Also/### Also/; s/^## Heads/### Heads/' "$TODAY_REPORT" >> "$DAILY_REPORT"
fi

# Yesterday section
if [ -f "$YESTERDAY_FILE" ]; then
  printf '\n---\n\n' >> "$DAILY_REPORT"
  sed 's/^# /## /; s/^## Watch/### Watch/; s/^## Rare/### Rare/; s/^## Also/### Also/; s/^## Heads/### Heads/' "$YESTERDAY_FILE" >> "$DAILY_REPORT"
else
  cat >> "$DAILY_REPORT" <<NO_YEST

---

## Yesterday

No archived report for yesterday.

NO_YEST
fi

echo "  daily report written to dailyReport.md"

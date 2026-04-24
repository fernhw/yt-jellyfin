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
JELLYFIN_APP_URLS="org.jellyfin.expo://|jellyfin://"
ABS_APP_URLS="me.jgrenier.AudioBS://|audiobookshelf://"

TODAY=$(date '+%Y-%m-%d')
NOW_HUMAN=$(date '+%B %d, %Y at %I:%M %p')
DAY_OF_WEEK=$(date '+%A')
RARE_THRESHOLD=30
WEB_RECENT_DAYS=4
FIELD_SEP=$(printf '\037')

# --- Day rollover: archive previous day's report ---
mkdir -p "$ARCHIVE_DIR"
if [ -f "$TODAY_REPORT" ]; then
  old_date=$(head -5 "$TODAY_REPORT" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)
  if [ -n "$old_date" ] && [ "$old_date" != "$TODAY" ]; then
    archive_name=$(printf '%s' "$old_date" | tr -d '-')
    mv "$TODAY_REPORT" "$ARCHIVE_DIR/${archive_name}.md"
  fi
fi

read_config_section() {
  [ -f "$CONFIG" ] || return 0

  local section="$1"
  local in_section=0
  local line clean

  while IFS= read -r line; do
    case "$line" in
      "[$section]") in_section=1; continue ;;
      \[*\]) in_section=0; continue ;;
    esac

    if [ "$in_section" -eq 1 ]; then
      clean=$(printf '%s' "$line" | sed 's/#.*//' | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
      [ -n "$clean" ] && printf '%s\n' "$clean"
    fi
  done < "$CONFIG"
}

normalize_channel() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed "s/[@ _'\".-]//g"
}

channel_matches_list() {
  [ -z "$2" ] && return 1

  local channel_norm entry entry_norm
  channel_norm=$(normalize_channel "$1")

  printf '%s\n' "$2" | tr '|' '\n' | while IFS= read -r entry; do
    entry_norm=$(normalize_channel "$entry")
    [ -z "$entry_norm" ] && continue

    case "$channel_norm" in
      "$entry_norm"|*"$entry_norm"*)
        echo "Y"
        break
        ;;
    esac

    case "$entry_norm" in
      "$channel_norm"|*"$channel_norm"*)
        echo "Y"
        break
        ;;
    esac
  done | grep -q '^Y$'
}

is_priority_channel() {
  channel_matches_list "$1" "$PRIORITY_LIST"
}

is_podcastable_channel() {
  channel_matches_list "$1" "$PODCASTABLE_LIST"
}

html_escape() {
  printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g'
}

format_epoch() {
  local epoch="$1"
  local fmt="$2"
  date -r "$epoch" "$fmt" 2>/dev/null || date -d "@$epoch" "$fmt" 2>/dev/null || printf '%s' "$epoch"
}

format_upload_date() {
  local raw_date="$1"
  date -j -f '%Y%m%d' "$raw_date" '+%b %d, %Y' 2>/dev/null || date -d "$raw_date" '+%b %d, %Y' 2>/dev/null || printf '%s' "$raw_date"
}

format_day_heading() {
  local raw_day="$1"
  local pretty_day
  local yesterday_day

  yesterday_day=$(date -v-1d '+%Y-%m-%d' 2>/dev/null || date -d 'yesterday' '+%Y-%m-%d' 2>/dev/null)
  pretty_day=$(date -j -f '%Y-%m-%d' "$raw_day" '+%A, %B %d' 2>/dev/null || date -d "$raw_day" '+%A, %B %d' 2>/dev/null || printf '%s' "$raw_day")

  if [ "$raw_day" = "$TODAY" ]; then
    printf 'Today <span>%s</span>' "$pretty_day"
  elif [ -n "$yesterday_day" ] && [ "$raw_day" = "$yesterday_day" ]; then
    printf 'Yesterday <span>%s</span>' "$pretty_day"
  else
    printf '%s' "$pretty_day"
  fi
}

PRIORITY_LIST=$(read_config_section priority | paste -sd'|' -)
PODCASTABLE_LIST=$(read_config_section podcastable | paste -sd'|' -)

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
TODAYS_VIDEOS=$(sqlite3 -separator "$FIELD_SEP" "$DB" "
  WITH today AS (
    SELECT channel, title, upload_date, download_date
    FROM videos
    WHERE date(download_date, 'unixepoch', 'localtime') = date('now', 'localtime')
      AND status = 'downloaded'
      AND julianday('now') - julianday(
            substr(upload_date,1,4)||'-'||substr(upload_date,5,2)||'-'||substr(upload_date,7,2)
          ) <= 3
  ),
  with_gap AS (
    SELECT t.channel, t.title, t.upload_date, t.download_date,
      (SELECT MAX(v2.upload_date) FROM videos v2
       WHERE v2.channel = t.channel
         AND v2.upload_date < t.upload_date
         AND v2.status = 'downloaded') as prev_date
    FROM today t
  )
  SELECT g.channel,
         g.title,
         g.upload_date,
         g.download_date,
         CASE WHEN g.prev_date IS NOT NULL THEN
           CAST(julianday(
             substr(g.upload_date,1,4)||'-'||substr(g.upload_date,5,2)||'-'||substr(g.upload_date,7,2)
           ) - julianday(
             substr(g.prev_date,1,4)||'-'||substr(g.prev_date,5,2)||'-'||substr(g.prev_date,7,2)
           ) AS INTEGER)
         ELSE -1 END as gap_days
  FROM with_gap g
  ORDER BY g.download_date DESC, g.channel
")

RECENT_VIDEOS=$(sqlite3 -separator "$FIELD_SEP" "$DB" "
  WITH recent AS (
    SELECT channel, title, upload_date, download_date
    FROM videos
    WHERE date(download_date, 'unixepoch', 'localtime') >= date('now', 'localtime', '-$((WEB_RECENT_DAYS - 1)) day')
      AND status = 'downloaded'
      AND julianday('now') - julianday(
            substr(upload_date,1,4)||'-'||substr(upload_date,5,2)||'-'||substr(upload_date,7,2)
          ) <= 7
  ),
  with_gap AS (
    SELECT r.channel, r.title, r.upload_date, r.download_date,
      (SELECT MAX(v2.upload_date) FROM videos v2
       WHERE v2.channel = r.channel
         AND v2.upload_date < r.upload_date
         AND v2.status = 'downloaded') as prev_date
    FROM recent r
  )
  SELECT g.channel,
         g.title,
         g.upload_date,
         g.download_date,
         strftime('%Y-%m-%d', g.download_date, 'unixepoch', 'localtime') as download_day,
         CASE WHEN g.prev_date IS NOT NULL THEN
           CAST(julianday(
             substr(g.upload_date,1,4)||'-'||substr(g.upload_date,5,2)||'-'||substr(g.upload_date,7,2)
           ) - julianday(
             substr(g.prev_date,1,4)||'-'||substr(g.prev_date,5,2)||'-'||substr(g.prev_date,7,2)
           ) AS INTEGER)
         ELSE -1 END as gap_days
  FROM with_gap g
  ORDER BY g.download_date DESC, g.channel
")

# Count non-empty lines
VIDEO_COUNT=0
if [ -n "$TODAYS_VIDEOS" ]; then
  VIDEO_COUNT=$(printf '%s\n' "$TODAYS_VIDEOS" | grep -c '.')
fi

# --- Categorize into priority / non-priority ---
TMP_PRI="/tmp/rpt_pri_$$"
TMP_NONPRI="/tmp/rpt_nonpri_$$"
: > "$TMP_PRI"
: > "$TMP_NONPRI"

printf '%s\n' "$TODAYS_VIDEOS" | while IFS="$FIELD_SEP" read -r chan title upload download_ts gap; do
  [ -z "$chan" ] && continue

  display_chan=$(printf '%s' "$chan" | sed 's/^@//; s/ *$//')
  download_time=$(format_epoch "$download_ts" '+%I:%M %p')
  podcast_state="not podcastable"
  is_podcast=0
  if is_podcastable_channel "$chan"; then
    podcast_state="podcastable"
    is_podcast=1
  fi

  gap_note=""
  is_rare=0
  if [ "$gap" -ge "$RARE_THRESHOLD" ] 2>/dev/null; then
    gap_note=" · *${gap} days since their last upload*"
    is_rare=1
  fi

  line="- **$title** — *$display_chan* · downloaded $download_time · $podcast_state$gap_note"

  if is_priority_channel "$chan"; then
    echo "$line" >> "$TMP_PRI"
  else
    echo "$line" >> "$TMP_NONPRI"
  fi
done

PRI_N=$(wc -l < "$TMP_PRI" | tr -d ' ')
NONPRI_N=$(wc -l < "$TMP_NONPRI" | tr -d ' ')

# --- Disk usage (precise, no rounding) ---
. "$SCRIPT_DIR/locations.md"
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
## Priority Videos

$(cat "$TMP_PRI")

---

SEC
  else
    cat >> "$TODAY_REPORT" <<SEC
## Priority Videos

No priority uploads today — your favorites are taking a break.

---

SEC
  fi

  # --- Non-priority ---
  if [ "$NONPRI_N" -gt 0 ]; then
    cat >> "$TODAY_REPORT" <<SEC
## Non-Priority Videos

$(cat "$TMP_NONPRI")

---

SEC
  fi
fi

# --- Cleanup ---
rm -f "$TMP_PRI" "$TMP_NONPRI"

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
  sed 's/^# /## /; s/^## Priority/### Priority/; s/^## Non-Priority/### Non-Priority/; s/^## Heads/### Heads/' "$TODAY_REPORT" >> "$DAILY_REPORT"
fi

# Yesterday section
if [ -f "$YESTERDAY_FILE" ]; then
  printf '\n---\n\n' >> "$DAILY_REPORT"
  sed 's/^# /## /; s/^## Priority/### Priority/; s/^## Non-Priority/### Non-Priority/; s/^## Heads/### Heads/' "$YESTERDAY_FILE" >> "$DAILY_REPORT"
else
  cat >> "$DAILY_REPORT" <<NO_YEST

---

## Yesterday

No archived report for yesterday.

NO_YEST
fi

echo "  daily report written to dailyReport.md"

# --- Generate web/index.html ---
WEB_DIR="$SCRIPT_DIR/web"
THUMBS_DIR="$WEB_DIR/thumbs"
HTML_OUT="$WEB_DIR/index.html"
mkdir -p "$THUMBS_DIR"

# Copy channel posters to web/thumbs/<channelname>.jpg (lowercase)
for ch_dir in "$YT_ROOT"/*/; do
  ch=$(basename "$ch_dir")
  src="$ch_dir/poster.jpg"
  if [ -f "$src" ]; then
    dest="$THUMBS_DIR/$(printf '%s' "$ch" | tr '[:upper:]' '[:lower:]').jpg"
    [ ! -f "$dest" ] || [ "$src" -nt "$dest" ] && cp "$src" "$dest"
  fi
done

WEB_TMP_DIR="/tmp/rpt_web_$$"
mkdir -p "$WEB_TMP_DIR"

DAY_ORDER=""
RECENT_COUNT=0

if [ -n "$RECENT_VIDEOS" ]; then
  while IFS="$FIELD_SEP" read -r chan title upload download_ts download_day gap; do
    [ -z "$chan" ] && continue

    RECENT_COUNT=$((RECENT_COUNT + 1))
    if ! printf '%s\n' "$DAY_ORDER" | grep -qx "$download_day"; then
      DAY_ORDER="${DAY_ORDER}${download_day}
"
    fi

    display_chan=$(printf '%s' "$chan" | sed 's/^@//; s/ *$//')
    ch_key=$(printf '%s' "$display_chan" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]//g')
    thumb_path="thumbs/${ch_key}.jpg"
    safe_title=$(html_escape "$title")
    safe_chan=$(html_escape "$display_chan")
    download_time=$(format_epoch "$download_ts" '+%I:%M %p')
    upload_label=$(format_upload_date "$upload")

    card_classes=""
    badge_html=""
    bucket="nonpriority"
    is_rare=0
    is_podcast=0

    if [ "$gap" -ge "$RARE_THRESHOLD" ] 2>/dev/null; then
      is_rare=1
    fi

    if is_priority_channel "$chan"; then
      bucket="priority"
      card_classes=" priority"
      badge_html="${badge_html}<span class=\"badge priority\">Watch First</span>"
    fi

    if is_podcastable_channel "$chan"; then
      is_podcast=1
      if [ "$bucket" = "nonpriority" ]; then
        card_classes=" podcast"
      fi
      badge_html="${badge_html}<span class=\"badge podcast\">Podcastable</span>"
    fi

    if [ "$is_rare" -eq 1 ]; then
      if [ "$bucket" = "nonpriority" ] && [ "$is_podcast" -eq 0 ]; then
        card_classes=" rare"
      fi
      badge_html="${badge_html}<span class=\"badge rare\">Rare Drop</span>"
    fi

    if [ "$is_podcast" -eq 1 ]; then
      podcast_label="Podcastable"
    else
      podcast_label="Not podcastable"
    fi

    if [ "$is_podcast" -eq 1 ]; then
      app_hrefs="${ABS_APP_URLS}"
      launch_label="Open in Audiobookshelf"
    else
      app_hrefs="${JELLYFIN_APP_URLS}"
      launch_label="Open in Jellyfin"
    fi

    filter_text=$(printf '%s %s' "$title" "$chan" | tr '[:upper:]' '[:lower:]')
    safe_filter_text=$(html_escape "$filter_text")

    cat >> "$WEB_TMP_DIR/${download_day}.${bucket}.html" <<CARD
<a class="card-link" href="#" data-app-urls="${app_hrefs}" data-filter-text="${safe_filter_text}" data-bucket="${bucket}" aria-label="${launch_label}: ${safe_title}">
  <article class="card${card_classes}">
    <img src="${thumb_path}" onerror="this.style.display='none'" alt="">
    <div class="info">
      <div class="title">${safe_title}</div>
      <div class="meta-row">${safe_chan} <span>&middot;</span> ${upload_label}</div>
    </div>
  </article>
</a>
CARD
  done <<EOF
$RECENT_VIDEOS
EOF
fi

DAY_SECTIONS_HTML=""
for download_day in $(printf '%s' "$DAY_ORDER"); do
  [ -z "$download_day" ] && continue

  day_heading=$(format_day_heading "$download_day")
  day_blocks=""

  if [ -s "$WEB_TMP_DIR/${download_day}.priority.html" ]; then
    day_blocks="${day_blocks}<section class=\"category\"><h3>Priority Videos</h3><div class=\"cards\">$(cat "$WEB_TMP_DIR/${download_day}.priority.html")</div></section>"
  fi
  if [ -s "$WEB_TMP_DIR/${download_day}.nonpriority.html" ]; then
    day_blocks="${day_blocks}<section class=\"category\"><h3>Non-Priority</h3><div class=\"cards\">$(cat "$WEB_TMP_DIR/${download_day}.nonpriority.html")</div></section>"
  fi

  [ -z "$day_blocks" ] && continue
  DAY_SECTIONS_HTML="${DAY_SECTIONS_HTML}<section class=\"day-section\"><div class=\"day-header\"><h2>${day_heading}</h2></div>${day_blocks}</section>"
done

if [ "$RECENT_COUNT" -eq 0 ]; then
  DAY_SECTIONS_HTML='<section class="day-section"><p class="empty">No recent downloads in the last 4 days.</p></section>'
fi

# 403/IP ban warning banner
WARN_HTML=""
if [ "$ERR_LAST" -gt 0 ] && [ "$SCANNED" -gt 0 ]; then
  half=$(( SCANNED / 2 ))
  if [ "$ERR_LAST" -gt "$half" ]; then
    WARN_HTML="<div class=\"warn-banner\"><strong>Possible IP ban</strong><span>${ERR_LAST} of ${SCANNED} channels returned nothing last scan.</span></div>"
  fi
fi
# Check for 403 flag written by getyt.sh
if [ -f "$YT_ROOT/.ban_detected" ]; then
  ban_detail=$(cat "$YT_ROOT/.ban_detected")
  WARN_HTML="${WARN_HTML}<div class=\"warn-banner\"><strong>HTTP 403 detected</strong><span>${ban_detail}</span></div>"
fi

UPDATED_AT=$(date '+%Y-%m-%d %H:%M')

cat > "$HTML_OUT" <<HTML
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="#0b0e11">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Report">
  <link rel="manifest" href="/manifest.json">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="apple-touch-icon" href="/icon.png">
  <title>What to Watch</title>
  <script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
  <script>
    window.OneSignalDeferred = window.OneSignalDeferred || [];
    OneSignalDeferred.push(async function(OneSignal) {
      await OneSignal.init({ appId: "c88ae5a3-36df-4301-945f-9da65e63d87c" });
    });
  </script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    :root{--bg:#101317;--panel:#171c21;--panel-soft:#1d242b;--line:#2a323b;--text:#ecf2f8;--muted:#97a6b4;--soft:#627384;--accent:#d7a847;--accent-soft:#2e2411;--pod:#72c7a2;--pod-soft:#163126;--rare:#86b9d9;--rare-soft:#172734;--warn:#f07f64;--warn-soft:#2b1916;--scroll-bg-top:#05070a;--scroll-bg-mid:#0b0e11;--scroll-bg-band:#0f1419;--scroll-bg-base:#101317;--chrome-tint:rgba(10,13,16,.86);--chrome-line:rgba(255,255,255,.06);--panel-tint:rgba(23,28,33,.78)}
    html{background:#0b0e11}
    body{background:linear-gradient(180deg,var(--scroll-bg-top) 0,var(--scroll-bg-mid) 120px,var(--scroll-bg-band) 320px,var(--scroll-bg-base) 100%);color:var(--text);font-family:Georgia,'Iowan Old Style','Palatino Linotype',serif;min-height:100vh;min-height:100dvh;padding-top:env(safe-area-inset-top);overflow-x:hidden;transition:background .25s ease,color .25s ease}
    #gate{display:none;position:fixed;inset:0;background:#0c0f13;z-index:999;justify-content:center;align-items:center;flex-direction:column;gap:16px}
    #gate h2{color:#fff;font-size:1.2rem;font-weight:500}
    #gate input{background:#151a1f;border:1px solid #303844;color:#fff;padding:10px 16px;border-radius:8px;font-size:1rem;width:220px;text-align:center;outline:none}
    #gate input:focus{border-color:#59687a}
    #gate button{background:#1a2026;border:1px solid #36404c;color:#d0dae3;padding:8px 24px;border-radius:8px;cursor:pointer;font-size:.9rem}
    #gate button:hover{background:#202730}
    #gate .err{color:#f07f64;font-size:.85rem}
    #app{display:none}
    .shell{max-width:1320px;margin:0 auto;padding:10px 18px 48px}
    header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:12px;padding:10px 12px;background:var(--chrome-tint);border:1px solid var(--chrome-line);border-radius:18px;backdrop-filter:blur(10px);transition:background .25s ease,border-color .25s ease,box-shadow .25s ease}
    .title-wrap{display:flex;align-items:baseline;gap:10px;min-width:0}
    .kicker{font:600 .68rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.16em;text-transform:uppercase;color:#c2a365}
    header h1{font-size:1rem;line-height:1.1;color:#d8e1ea;font-weight:600;white-space:nowrap}
    .lede{display:none}
    .top-meta{display:flex;gap:10px;align-items:center}
    .meta-card{background:rgba(23,28,33,.88);border:1px solid rgba(255,255,255,.07);border-radius:16px;padding:14px 16px;min-width:220px;backdrop-filter:blur(10px)}
    .meta-card strong{display:block;font:600 .72rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.16em;text-transform:uppercase;color:#92a0af;margin-bottom:6px}
    .meta-card span{display:block;color:#fff;font-size:1rem}
    .notify-btn{background:transparent;border:1px solid #3b4550;color:#c6d0da;padding:8px 12px;border-radius:999px;cursor:pointer;font:600 .72rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.08em;text-transform:uppercase;transition:all .2s;flex-shrink:0}
    .notify-btn:hover{border-color:#c2a365;color:#fff}
    .controls{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0 12px;padding:10px 12px;background:rgba(12,15,19,.72);border:1px solid rgba(255,255,255,.06);border-radius:18px;backdrop-filter:blur(10px);position:sticky;top:calc(env(safe-area-inset-top) + 6px);z-index:20;transition:background .25s ease,border-color .25s ease,transform .25s ease}
    .controls.compact{transform:translateY(-2px)}
    .filter-input{flex:1 1 220px;min-width:0;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);color:var(--text);padding:10px 12px;border-radius:999px;font:500 16px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;outline:none}
    .filter-input::placeholder{color:#7f90a1}
    .filter-chip{border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.03);color:#c6d0da;padding:9px 12px;border-radius:999px;cursor:pointer;font:600 .7rem/1.1 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.08em;text-transform:uppercase;transition:background .2s ease,border-color .2s ease,color .2s ease}
    .filter-chip.active{background:#d7a847;color:#120d05;border-color:#f0c671}
    .filter-status{flex:1 1 100%;font:500 .72rem/1.25 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--muted)}
    .summary-bar{margin-top:18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
    .summary-tile{background:rgba(23,28,33,.72);border:1px solid rgba(255,255,255,.06);border-radius:18px;padding:16px 18px}
    .summary-tile strong{display:block;font:600 .68rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#92a0af;margin-bottom:8px}
    .summary-tile span{font-size:1.05rem;color:#fff}
    .warn-stack{display:grid;gap:10px;margin-top:20px}
    .warn-banner{display:flex;gap:12px;align-items:flex-start;background:var(--warn-soft);border:1px solid rgba(240,127,100,.35);border-radius:14px;padding:14px 16px;color:#ffd3c8}
    .warn-banner strong{color:#fff;font:600 .78rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.08em;text-transform:uppercase;min-width:max-content}
    main{margin-top:10px;display:grid;gap:18px}
    .day-section{background:var(--panel-tint);border:1px solid rgba(255,255,255,.06);border-radius:22px;padding:22px 20px 20px;box-shadow:0 18px 60px rgba(0,0,0,.16);transition:background .25s ease,border-color .25s ease,box-shadow .25s ease}
    .day-header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid rgba(255,255,255,.07)}
    .day-header h2{font-size:1.5rem;color:#fff;font-weight:600}
    .day-header h2 span{display:inline-block;margin-left:10px;color:var(--muted);font-size:.95rem;font-weight:400}
    .category+.category{margin-top:18px}
    .category h3{font:600 .74rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.18em;text-transform:uppercase;color:#8fa0b1;margin-bottom:12px}
    .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
    .card-link{text-decoration:none;color:inherit;display:block}
    .card{display:flex;gap:10px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:10px 12px;min-height:82px}
    .card-link:active .card,.card-link:focus-visible .card{border-color:#5c6977;transform:translateY(1px)}
    .card.priority{border-color:rgba(215,168,71,.45);background:linear-gradient(180deg,rgba(46,36,17,.96),rgba(23,28,33,.96))}
    .card.podcast{border-color:rgba(114,199,162,.45);background:linear-gradient(180deg,rgba(22,49,38,.96),rgba(23,28,33,.96))}
    .card.rare{border-color:rgba(134,185,217,.4);background:linear-gradient(180deg,rgba(23,39,52,.96),rgba(23,28,33,.96))}
    .card img{width:42px;height:42px;border-radius:10px;object-fit:cover;flex-shrink:0;background:var(--panel-soft)}
    .info{min-width:0;display:grid;gap:4px;width:100%}
    .title{font-size:.92rem;line-height:1.24;color:#f6f8fb;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .meta-row{font:500 .72rem/1.25 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .meta-row span{padding:0 5px;color:#59697a}
    .empty{color:var(--muted);font-size:1rem;line-height:1.6}
    .day-section.is-filtered-empty,.category.is-filtered-empty{display:none}
    #filterResults{display:none;margin-top:10px}
    #filterResults .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
    #filterResults .filter-empty{color:var(--muted);font-size:.9rem;padding:16px 4px}
    @media(max-width:720px){#filterResults .cards{grid-template-columns:1fr}}
    .bottom-panel{margin-top:18px;padding:18px;border:1px solid rgba(255,255,255,.06);border-radius:22px;background:linear-gradient(180deg,rgba(18,22,27,.96),rgba(13,16,20,.96))}
    .bottom-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:12px}
    .bottom-head h2{font-size:1rem;color:#dbe4ec;font-weight:600}
    .bottom-head p{font:500 .76rem/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--muted)}
    footer{padding-top:16px;color:#7d8d9d;font:500 .76rem/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
    @media(max-width:720px){
      .shell{padding:6px 10px 28px}
      header{padding:10px;gap:10px;align-items:center}
      .controls{top:calc(env(safe-area-inset-top) + 4px);padding:9px 10px;gap:8px}
      .title-wrap{flex-direction:column;align-items:flex-start;gap:4px}
      header h1{font-size:.92rem;white-space:normal}
      .top-meta{margin-left:auto}
      .day-section{padding:18px 14px}
      .cards{grid-template-columns:1fr}
      .card{padding:8px 10px;min-height:74px}
      .card img{width:38px;height:38px}
      .title{font-size:.86rem}
      .meta-row{font-size:.68rem}
      .filter-input{flex-basis:100%;font-size:16px;padding:9px 11px}
      .filter-chip{padding:8px 10px;font-size:.64rem}
      .bottom-panel{padding:14px}
      .bottom-head{flex-direction:column;align-items:flex-start}
    }
  </style>
</head>
<body>
<div id="gate">
  <h2>report.fernhw.com</h2>
  <input type="password" id="pw" placeholder="password" autocomplete="current-password">
  <button onclick="tryAuth()">Enter</button>
  <span class="err" id="err"></span>
</div>

<div id="app">
  <div class="shell">
    <header>
      <div class="title-wrap">
        <div class="kicker">Recent Download Report</div>
        <h1>Latest downloads</h1>
        <p class="lede">${greeting}. This view keeps the latest ${WEB_RECENT_DAYS} days in download order, split into priority, podcastable, rare drops, and everything else.</p>
      </div>
      <div class="top-meta">
        <button class="notify-btn" onclick="subscribeNotify()">Get notified</button>
      </div>
    </header>

    <section class="controls" id="feedControls">
      <input id="feedFilter" class="filter-input" type="search" placeholder="Search title or channel..." autocomplete="off" spellcheck="false">
      <button class="filter-chip active" type="button" data-filter-bucket="all">All</button>
      <button class="filter-chip" type="button" data-filter-bucket="priority">Priority</button>
      <button class="filter-chip" type="button" data-filter-bucket="nonpriority">Non-Priority</button>
      <div class="filter-status" id="filterStatus">Showing all ${RECENT_COUNT} videos.</div>
    </section>

    <div id="filterResults"><div class="cards" id="filterCards"></div></div>

    <main id="feedMain">
      ${DAY_SECTIONS_HTML}
    </main>

    <section class="bottom-panel">
      <div class="bottom-head">
        <h2>Report details</h2>
        <p>${greeting}. ${RECENT_COUNT} items from the last ${WEB_RECENT_DAYS} days.</p>
      </div>

      <div class="warn-stack">${WARN_HTML}</div>

      <section class="summary-bar">
        <div class="summary-tile">
          <strong>Report Date</strong>
          <span>${TODAY}</span>
        </div>
        <div class="summary-tile">
          <strong>Updated</strong>
          <span>${UPDATED_AT}</span>
        </div>
        <div class="summary-tile">
          <strong>Today</strong>
          <span>${VIDEO_COUNT} downloads on ${DAY_OF_WEEK}</span>
        </div>
        <div class="summary-tile">
          <strong>Recent Downloads</strong>
          <span>${RECENT_COUNT} items across ${WEB_RECENT_DAYS} days</span>
        </div>
        <div class="summary-tile">
          <strong>Library Storage</strong>
          <span>${YT_USED} used &middot; ${DISK_FREE} free</span>
        </div>
      </section>
    </section>

    <footer>report.fernhw.com &middot; updated ${UPDATED_AT}</footer>
  </div>
</div>

<script>
const PW_HASH = "5e7601a1ac99c87a65120283ae1b380901ca55bb402dce34e1a4ec51c20d29bb";
const COOKIE = "rauth";

async function sha256hex(str) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("");
}

function getCookie(name) {
  return document.cookie.split(";").map(c => c.trim()).filter(c => c.startsWith(name + "=")).map(c => c.slice(name.length + 1))[0] || "";
}

function setCookie(name, val, days) {
  const exp = new Date(Date.now() + days * 864e5).toUTCString();
  document.cookie = name + "=" + val + ";expires=" + exp + ";path=/;SameSite=Strict";
}

async function tryAuth(auto) {
  const input = auto || document.getElementById("pw").value;
  const h = await sha256hex(input);
  if (h === PW_HASH) {
    setCookie(COOKIE, h.slice(0, 16), 365);
    showApp();
  } else if (!auto) {
    document.getElementById("err").textContent = "wrong password";
  }
}

function showApp() {
  document.getElementById("gate").style.display = "none";
  document.getElementById("app").style.display = "block";
}

document.getElementById("pw").addEventListener("keydown", e => {
  if (e.key === "Enter") tryAuth();
});

(async () => {
  const stored = getCookie(COOKIE);
  if (stored) {
    const expected = PW_HASH.slice(0, 16);
    if (stored === expected) { showApp(); return; }
  }
  const url = new URL(location.href);
  const p = url.searchParams.get("p") || url.searchParams.get("password");
  if (p) {
    url.searchParams.delete("p");
    url.searchParams.delete("password");
    history.replaceState({}, "", url.toString());
    await tryAuth(p);
    if (getCookie(COOKIE)) return;
  }
  document.getElementById("gate").style.display = "flex";
})();

function waitForOneSignal() {
  return new Promise(resolve => {
    if (window.OneSignal && window.OneSignal.Notifications) { resolve(window.OneSignal); return; }
    window.OneSignalDeferred = window.OneSignalDeferred || [];
    window.OneSignalDeferred.push(os => resolve(os));
  });
}

const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
const isStandalone = window.navigator.standalone === true;
const rootStyle = document.documentElement.style;
const themeMeta = document.querySelector('meta[name="theme-color"]');

function tryAppUrls(appUrls) {
  const urls = appUrls.split('|').filter(Boolean);
  let index = 0;

  function attemptNext() {
    if (index >= urls.length) {
      return;
    }

    const startedAt = Date.now();
    const iframe = document.createElement('iframe');
    iframe.style.display = 'none';
    iframe.src = urls[index++];
    document.body.appendChild(iframe);

    setTimeout(() => {
      document.body.removeChild(iframe);
      if (Date.now() - startedAt < 1400) {
        attemptNext();
      }
    }, 700);
  }

  attemptNext();
}

document.addEventListener('click', event => {
  const chip = event.target.closest('.filter-chip');
  if (chip) {
    setActiveBucket(chip.dataset.filterBucket || 'all');
    applyFeedFilter();
    return;
  }

  const link = event.target.closest('.card-link');
  if (!link) return;
  event.preventDefault();
  tryAppUrls(link.dataset.appUrls || '');
});

const filterInput = document.getElementById('feedFilter');
const filterStatus = document.getElementById('filterStatus');
const filterChips = Array.from(document.querySelectorAll('.filter-chip'));
let activeBucket = 'all';

function setActiveBucket(bucket) {
  activeBucket = bucket;
  filterChips.forEach(chip => {
    chip.classList.toggle('active', (chip.dataset.filterBucket || 'all') === bucket);
  });
}

const feedMain = document.getElementById('feedMain');
const filterResults = document.getElementById('filterResults');
const filterCards = document.getElementById('filterCards');

function applyFeedFilter() {
  const query = (filterInput?.value || '').trim().toLowerCase();
  const isFiltering = query || activeBucket !== 'all';

  if (!isFiltering) {
    filterResults.style.display = 'none';
    feedMain.style.display = '';
    filterCards.innerHTML = '';
    if (filterStatus) filterStatus.textContent = 'Showing all ' + document.querySelectorAll('.card-link').length + ' videos.';
    return;
  }

  const allCards = Array.from(document.querySelectorAll('#feedMain .card-link'));
  const matched = allCards.filter(card => {
    const combined = (card.dataset.filterText || '').toLowerCase();
    const bucket = card.dataset.bucket || 'nonpriority';
    const matchesBucket = activeBucket === 'all' || bucket === activeBucket;
    const matchesQuery = !query || combined.includes(query);
    return matchesBucket && matchesQuery;
  });

  filterCards.innerHTML = '';
  matched.forEach(card => filterCards.appendChild(card.cloneNode(true)));

  if (matched.length === 0) {
    filterCards.innerHTML = '<p class="filter-empty">No videos match.</p>';
  }

  feedMain.style.display = 'none';
  filterResults.style.display = 'block';

  if (!filterStatus) return;
  const bucketLabel = activeBucket === 'all' ? '' : activeBucket === 'priority' ? ' in Priority' : ' in Non-Priority';
  const queryLabel = query ? ' matching \"' + query + '\"' : '';
  filterStatus.textContent = matched.length + ' video' + (matched.length === 1 ? '' : 's') + bucketLabel + queryLabel + '.';
}

filterInput?.addEventListener('input', applyFeedFilter);

function mixChannel(from, to, amount) {
  return Math.round(from + (to - from) * amount);
}

function setScrollTheme() {
  const scrollable = Math.max(document.documentElement.scrollHeight - window.innerHeight, 1);
  const progress = Math.min(window.scrollY / scrollable, 1);
  const eased = Math.min(progress * 1.18, 1);

  const top = 'rgb(' + mixChannel(5, 23, eased) + ' ' + mixChannel(7, 43, eased) + ' ' + mixChannel(10, 52, eased) + ')';
  const mid = 'rgb(' + mixChannel(11, 31, eased) + ' ' + mixChannel(14, 59, eased) + ' ' + mixChannel(17, 67, eased) + ')';
  const band = 'rgb(' + mixChannel(15, 43, eased) + ' ' + mixChannel(20, 76, eased) + ' ' + mixChannel(25, 86, eased) + ')';
  const base = 'rgb(' + mixChannel(16, 31, eased) + ' ' + mixChannel(19, 53, eased) + ' ' + mixChannel(23, 60, eased) + ')';
  const chrome = 'rgba(' + mixChannel(10, 18, eased) + ',' + mixChannel(13, 34, eased) + ',' + mixChannel(16, 40, eased) + ',.88)';

  rootStyle.setProperty('--scroll-bg-top', top);
  rootStyle.setProperty('--scroll-bg-mid', mid);
  rootStyle.setProperty('--scroll-bg-band', band);
  rootStyle.setProperty('--scroll-bg-base', base);
  rootStyle.setProperty('--chrome-tint', chrome);
  rootStyle.setProperty('--panel-tint', 'rgba(' + mixChannel(23, 20, eased) + ',' + mixChannel(28, 42, eased) + ',' + mixChannel(33, 48, eased) + ',.8)');
  if (themeMeta) themeMeta.setAttribute('content', mid);
  document.getElementById('feedControls')?.classList.toggle('compact', progress > 0.08);
}

setActiveBucket('all');
applyFeedFilter();
setScrollTheme();
window.addEventListener('scroll', setScrollTheme, { passive: true });

async function subscribeNotify() {
  if (isIOS && !isStandalone) {
    alert('On iPhone: tap the Share button then "Add to Home Screen", then open the app and tap Get Notified.');
    return;
  }
  try {
    const os = await waitForOneSignal();
    const perm = await os.Notifications.requestPermission();
    if (perm) {
      const btn = document.querySelector('.notify-btn');
      btn.textContent = 'Subscribed!';
      btn.style.color = '#9ae0bc';
      btn.style.borderColor = '#3d7c5d';
      btn.disabled = true;
    }
  } catch (e) {
    console.warn('OneSignal:', e);
  }
}

setTimeout(async () => {
  if (isIOS && !isStandalone) return;
  try {
    const os = await waitForOneSignal();
    const subbed = os.User.PushSubscription.optedIn;
    if (!subbed) os.Notifications.requestPermission();
  } catch (e) {}
}, 4000);
</script>
</body>
</html>
HTML

rm -rf "$WEB_TMP_DIR"

echo "  web report written to web/index.html"

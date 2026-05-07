#!/usr/bin/env bash
# showScheduler.sh — Register a show for automatic episode tracking
#
# Usage (interactive walkthrough):
#   sh showScheduler.sh
#
# Usage (param mode):
#   sh showScheduler.sh --name "Re Zero" --folder "re.zero" \
#       --days wed --type anime --season 4 --episode 4 \
#       --total 12 [--immediate] [--no-cron]
#
# Other commands:
#   sh showScheduler.sh --list              list all scheduled shows
#   sh showScheduler.sh --remove "Re Zero"  remove a show from schedule
#   sh showScheduler.sh --install-cron      install/update hourly cron job
#   sh showScheduler.sh --help

SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" || echo "$0")")" && pwd)"
. "$SCRIPT_DIR/locations.md"

SCHEDULE_CSV="$SCRIPT_DIR/showSchedule.csv"
SEARCH_SCRIPT="$SCRIPT_DIR/showSchedulerSearch.py"
CSV_HEADER="show_name,search_name,folder,type,season,next_episode,total_episodes,release_days,status,search_start,search_end,last_check,week_anchor,anchor_episode"

# ── Helpers ────────────────────────────────────────────────────────────────────

die()  { printf '\033[0;31mError:\033[0m %s\n' "$1" >&2; exit 1; }
info() { printf '  \033[0;32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[0;33m⚠\033[0m %s\n' "$1"; }

ensure_csv() {
  if [ ! -f "$SCHEDULE_CSV" ]; then
    printf '%s\n' "$CSV_HEADER" > "$SCHEDULE_CSV"
    info "Created $SCHEDULE_CSV"
  fi
}

# Normalize full day names to 3-letter abbreviations
normalize_days() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/monday/mon/g; s/tuesday/tue/g; s/wednesday/wed/g;
           s/thursday/thu/g; s/friday/fri/g; s/saturday/sat/g; s/sunday/sun/g'
}

validate_days() {
  for d in $(printf '%s' "$1" | tr ',' '\n' | tr -d ' '); do
    case "$d" in
      mon|tue|wed|thu|fri|sat|sun) ;;
      *) die "Invalid day '$d'. Use: mon/tue/wed/thu/fri/sat/sun" ;;
    esac
  done
}

# Fuzzy folder match against SHOWS_DIR — returns best matching folder name
fuzzy_folder() {
  local query="$1"
  local best="" best_score=0
  local qnorm
  qnorm=$(printf '%s' "$query" | tr '[:upper:]' '[:lower:]' | tr '._-' '   ')

  for d in "$SHOWS_DIR"/*/; do
    [ -d "$d" ] || continue
    local fname fnorm score=0
    fname=$(basename "$d")
    fnorm=$(printf '%s' "$fname" | tr '[:upper:]' '[:lower:]' | tr '._-' '   ')
    for word in $qnorm; do
      [ ${#word} -lt 2 ] && continue
      if printf '%s' "$fnorm" | grep -qF "$word"; then
        score=$((score + 1))
      fi
    done
    if [ "$score" -gt "$best_score" ]; then
      best_score=$score
      best="$fname"
    fi
  done

  [ "$best_score" -ge 1 ] && printf '%s' "$best"
}

list_shows() {
  ensure_csv
  echo ""
  echo "  Scheduled shows:"
  echo "  ─────────────────────────────────────────────────────────────────"
  printf '  %-22s %-12s %-6s  %-6s  %s\n' "SHOW" "EPISODE" "DAYS" "TYPE" "STATUS"
  echo "  ─────────────────────────────────────────────────────────────────"
  awk -F',' 'NR>1 && NF>=9 {
    printf "  %-22s %-18s S%02dE%s/%-3s %-5s  %-6s  %s\n",
      substr($1,1,22), substr($2,1,18), $5+0, $6, $7, $8, $4, $9
  }' "$SCHEDULE_CSV"
  echo ""
}

remove_show() {
  local name="$1"
  ensure_csv
  local tmp
  tmp=$(mktemp)
  awk -F',' -v n="$name" 'NR==1 || tolower($1)!=tolower(n)' "$SCHEDULE_CSV" > "$tmp"
  mv "$tmp" "$SCHEDULE_CSV"
  info "Removed: $name"
}

install_cron() {
  local py_path
  py_path=$(command -v python3 2>/dev/null || echo "python3")
  local cron_line="0 * * * * $py_path $SEARCH_SCRIPT >> $SCRIPT_DIR/showScheduler.log 2>&1"
  (crontab -l 2>/dev/null | grep -v "showSchedulerSearch.py"; echo "$cron_line") | crontab -
  info "Cron installed (runs every hour at :00)"
  info "  $cron_line"
}

# ── Parse flags ────────────────────────────────────────────────────────────────

OPT_NAME="" OPT_SEARCH_NAME="" OPT_FOLDER="" OPT_DAYS="" OPT_TYPE="" OPT_SEASON=""
OPT_EPISODE="" OPT_TOTAL="" OPT_IMMEDIATE=0 OPT_NO_CRON=0 OPT_PARAM_MODE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --name)         OPT_NAME="$2";        shift 2; OPT_PARAM_MODE=1 ;;
    --search-name)  OPT_SEARCH_NAME="$2"; shift 2 ;;
    --folder)       OPT_FOLDER="$2";      shift 2 ;;
    --days)         OPT_DAYS="$2";        shift 2 ;;
    --type)         OPT_TYPE="$2";        shift 2 ;;
    --season)       OPT_SEASON="$2";      shift 2 ;;
    --episode)      OPT_EPISODE="$2";     shift 2 ;;
    --total)        OPT_TOTAL="$2";       shift 2 ;;
    --immediate)    OPT_IMMEDIATE=1;      shift ;;
    --no-cron)      OPT_NO_CRON=1;        shift ;;
    --list)         list_shows; exit 0 ;;
    --remove)       remove_show "$2"; exit 0 ;;
    --install-cron) install_cron; exit 0 ;;
    --help|-h)
      sed -n '2,15p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) die "Unknown option: $1  (use --help for usage)" ;;
  esac
done

# ── Header ─────────────────────────────────────────────────────────────────────

if [ "$OPT_PARAM_MODE" -eq 0 ]; then
  echo ""
  echo "  ╔══════════════════════════════════════════╗"
  echo "  ║       Show Episode Scheduler Setup       ║"
  echo "  ╚══════════════════════════════════════════╝"
  echo ""
fi

# ── 1. Show name ───────────────────────────────────────────────────────────────

if [ -z "$OPT_NAME" ]; then
  printf 'Name of show:\n> '
  read -r OPT_NAME
fi
[ -z "$OPT_NAME" ] && die "Show name is required"

# ── 1b. Search name (clean lowercase query term) ──────────────────────────────

if [ -z "$OPT_SEARCH_NAME" ]; then
  # Auto-derive: lowercase, strip punctuation/special chars
  auto_search=$(printf '%s' "$OPT_NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9 ' ' ' | tr -s ' ' | sed 's/^ *//; s/ *$//')
  if [ "$OPT_PARAM_MODE" -eq 1 ]; then
    OPT_SEARCH_NAME="$auto_search"
  else
    printf '\nSearch name (used for Nyaa/TPB queries, default: \033[0;36m%s\033[0m):\n[Enter to accept]:\n> ' "$auto_search"
    read -r _sn
    OPT_SEARCH_NAME="${_sn:-$auto_search}"
  fi
fi

# ── 2. Folder in Shows/ ────────────────────────────────────────────────────────

if [ -z "$OPT_FOLDER" ]; then
  # Auto-detect from show name first
  auto=$(fuzzy_folder "$OPT_NAME")
  if [ "$OPT_PARAM_MODE" -eq 1 ]; then
    # Non-interactive: use auto-match silently, or fail clearly
    if [ -n "$auto" ]; then
      OPT_FOLDER="$auto"
      info "Auto-matched folder: $OPT_FOLDER"
    else
      die "Could not auto-match a folder for '$OPT_NAME'. Pass --folder explicitly."
    fi
  else
    if [ -n "$auto" ]; then
      printf '\nFolder in Shows/ (detected: \033[0;36m%s\033[0m)\n[Enter to accept, or type a different name]:\n> ' "$auto"
      read -r inp
      OPT_FOLDER="${inp:-$auto}"
    else
      printf '\nFolder in Shows/ (will be created if missing):\n> '
      read -r OPT_FOLDER
    fi
    # If the user typed something, also fuzzy-match their input against real folders
    if [ -n "$inp" ]; then
      typed_match=$(fuzzy_folder "$inp")
      if [ -n "$typed_match" ] && [ "$typed_match" != "$OPT_FOLDER" ]; then
        printf '  Did you mean \033[0;36m%s\033[0m? [Y/n]: ' "$typed_match"
        read -r _fmatch
        case "$_fmatch" in n|N) ;; *) OPT_FOLDER="$typed_match" ;; esac
      fi
    fi
  fi
fi
[ -z "$OPT_FOLDER" ] && die "Folder name is required"

# Collision detection — strip noise from both sides, then compare core words
_strip_noise() {
  printf '%s' "$1" \
    | sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g' \
    | tr '[:upper:]' '[:lower:]' \
    | tr '._-' '   ' \
    | sed -E 's/ (s[0-9]+|e[0-9]+|season|1080p|2160p|720p|480p|web|webrip|bluray|hevc|x265|x264|aac|flac|ddp|h264|h265|dual audio|multi sub) */ /g' \
    | tr -s ' ' \
    | sed 's/^ *//; s/ *$//'
}

exact_match="" collision=""
query_norm=$(_strip_noise "$OPT_FOLDER")
for d in "$SHOWS_DIR"/*/; do
  [ -d "$d" ] || continue
  fname=$(basename "$d")
  fname_norm=$(_strip_noise "$fname")
  # Exact after stripping
  if [ "$fname_norm" = "$query_norm" ]; then
    exact_match="$fname"
    break
  fi
  # Word-level intersection: count shared words
  score=0
  for word in $query_norm; do
    [ ${#word} -lt 2 ] && continue
    if printf '%s' "$fname_norm" | grep -qwF "$word" 2>/dev/null; then
      score=$((score + 1))
    fi
  done
  # Accept collision if at least 2 words match OR one word matches and
  # query is short (<=2 meaningful words)
  query_words=$(printf '%s' "$query_norm" | wc -w | tr -d ' ')
  if [ "$score" -ge 2 ] || { [ "$score" -ge 1 ] && [ "$query_words" -le 2 ]; }; then
    collision="$fname"
  fi
done

if [ -n "$exact_match" ]; then
  info "Using existing folder: $exact_match"
  OPT_FOLDER="$exact_match"
elif [ -n "$collision" ]; then
  echo ""
  warn "Found existing folder: $collision"
  printf '  Use this existing folder? [Y/n]: '
  read -r confirm
  case "$confirm" in
    n|N) info "Using new folder: $OPT_FOLDER" ;;
    *)   OPT_FOLDER="$collision"; info "Using existing: $OPT_FOLDER" ;;
  esac
fi

DEST_DIR="$SHOWS_DIR/$OPT_FOLDER"
if [ ! -d "$DEST_DIR" ]; then
  mkdir -p "$DEST_DIR"
  info "Created: $DEST_DIR"
else
  info "Folder exists: $DEST_DIR"
fi

# ── 3. Release days ────────────────────────────────────────────────────────────

if [ -z "$OPT_DAYS" ]; then
  printf '\nRelease day(s) (mon/tue/wed/thu/fri/sat/sun, comma-separate for multiple):\n> '
  read -r OPT_DAYS
fi
OPT_DAYS=$(normalize_days "$OPT_DAYS")
OPT_DAYS=$(printf '%s' "$OPT_DAYS" | tr -d ' ')
validate_days "$OPT_DAYS"

# ── 4. Show type ───────────────────────────────────────────────────────────────

if [ -z "$OPT_TYPE" ]; then
  printf '\nType of show (anime/live):\n> '
  read -r OPT_TYPE
fi
OPT_TYPE=$(printf '%s' "$OPT_TYPE" | tr '[:upper:]' '[:lower:]')
case "$OPT_TYPE" in
  anime|live) ;;
  *) die "Type must be 'anime' or 'live'" ;;
esac

# ── 5. Season number ───────────────────────────────────────────────────────────

if [ -z "$OPT_SEASON" ]; then
  printf '\nSeason number:\n> '
  read -r OPT_SEASON
fi
case "$OPT_SEASON" in
  ''|*[!0-9]*) die "Season must be a positive integer" ;;
esac

# ── 6. Starting episode ────────────────────────────────────────────────────────

if [ -z "$OPT_EPISODE" ]; then
  printf '\nEpisode to start from (default: 1, skip if starting from ep 1):\n> '
  read -r OPT_EPISODE
fi
[ -z "$OPT_EPISODE" ] && OPT_EPISODE=1
case "$OPT_EPISODE" in
  *[!0-9]*) die "Episode must be a positive integer" ;;
esac

# ── 7. Total episodes ──────────────────────────────────────────────────────────

if [ -z "$OPT_TOTAL" ]; then
  printf '\nTotal episodes in season:\n> '
  read -r OPT_TOTAL
fi
case "$OPT_TOTAL" in
  ''|*[!0-9]*) die "Total episodes must be a positive integer" ;;
esac

# ── 8. Immediate search + backfill ────────────────────────────────────────────

OPT_BACKFILL=0
if [ "$OPT_IMMEDIATE" -eq 0 ] && [ "$OPT_PARAM_MODE" -eq 0 ]; then
  printf '\nSearch for current episode now? [Y/n]: '
  read -r _imm
  case "$_imm" in y|Y|"") OPT_IMMEDIATE=1 ;; esac

  # Backfill only relevant when starting mid-season (episode > 1)
  if [ "$OPT_EPISODE" -gt 1 ]; then
    printf '\nBackfill episodes 1 to %s? (will search each one now) [y/N]: ' "$((OPT_EPISODE - 1))"
    read -r _bf
    case "$_bf" in y|Y) OPT_BACKFILL=1 ;; esac
  fi
fi

# ── Summary + confirm ──────────────────────────────────────────────────────────

echo ""
echo "  ┌─────────────────────────────────────────┐"
printf '  │  %-40s│\n' "Show     : $OPT_NAME"
  printf '  │  %-40s│\n' "Search   : $OPT_SEARCH_NAME"
printf '  │  %-40s│\n' "Folder   : $OPT_FOLDER"
printf '  │  %-40s│\n' "Type     : $OPT_TYPE"
printf '  │  %-40s│\n' "Season   : $OPT_SEASON"
printf '  │  %-40s│\n' "Episodes : $OPT_EPISODE → $OPT_TOTAL"
printf '  │  %-40s│\n' "Day(s)   : $OPT_DAYS"
printf '  │  %-40s│\n' "Dest     : $OPT_FOLDER/"
echo "  └─────────────────────────────────────────┘"
echo ""

if [ "$OPT_PARAM_MODE" -eq 0 ]; then
  printf 'Confirm? [Y/n]: '
  read -r _conf
  case "$_conf" in n|N) echo "  Aborted."; exit 0 ;; esac
fi

# ── Write to CSV ───────────────────────────────────────────────────────────────

ensure_csv

# If show already in schedule, offer to update
if awk -F',' -v n="$OPT_NAME" \
    'NR>1 && tolower($1)==tolower(n) {found=1} END {exit !found}' \
    "$SCHEDULE_CSV" 2>/dev/null; then
  if [ "$OPT_PARAM_MODE" -eq 0 ]; then
    warn "Show already in schedule."
    printf '  Update it? [Y/n]: '
    read -r _upd
    case "$_upd" in n|N) echo "  Aborted."; exit 0 ;; esac
  fi
  tmp=$(mktemp)
  awk -F',' -v n="$OPT_NAME" \
    'NR==1 || tolower($1)!=tolower(n)' "$SCHEDULE_CSV" > "$tmp"
  mv "$tmp" "$SCHEDULE_CSV"
fi

# Compute week_anchor = Monday of the current week (ISO date)
_week_anchor=$(python3 -c "
import datetime
t = datetime.date.today()
print((t - datetime.timedelta(days=t.weekday())).isoformat())
")

printf '%s,%s,%s,%s,%s,%s,%s,%s,pending,,,%s,%s,%s\n' \
  "$OPT_NAME" "$OPT_SEARCH_NAME" "$OPT_FOLDER" "$OPT_TYPE" "$OPT_SEASON" \
  "$OPT_EPISODE" "$OPT_TOTAL" "$OPT_DAYS" \
  "$(date +%Y-%m-%d)" "$_week_anchor" "$OPT_EPISODE" >> "$SCHEDULE_CSV"

info "Registered: $OPT_NAME S${OPT_SEASON}E${OPT_EPISODE}→${OPT_TOTAL} ($OPT_DAYS, $OPT_TYPE)"

# ── Cron setup ─────────────────────────────────────────────────────────────────

if [ "$OPT_NO_CRON" -eq 0 ]; then
  if crontab -l 2>/dev/null | grep -q "showSchedulerSearch.py"; then
    info "Hourly cron already active"
  else
    if [ "$OPT_PARAM_MODE" -eq 1 ]; then
      install_cron
    else
      printf '\nInstall hourly cron job? [Y/n]: '
      read -r _cron_ans
      case "$_cron_ans" in
        n|N) warn "Cron not installed. Run manually with: python3 $SEARCH_SCRIPT" ;;
        *)   install_cron ;;
      esac
    fi
  fi
fi

# ── Immediate search ───────────────────────────────────────────────────────────

if [ "$OPT_IMMEDIATE" -eq 1 ]; then
  echo ""
  info "Searching for current episode..."
  python3 "$SEARCH_SCRIPT" --show "$OPT_NAME" --force
fi

# ── Backfill ───────────────────────────────────────────────────────────────────

if [ "$OPT_BACKFILL" -eq 1 ]; then
  echo ""
  info "Backfilling episodes 1 → $((OPT_EPISODE - 1))..."
  _ep=1
  while [ "$_ep" -lt "$OPT_EPISODE" ]; do
    info "  Searching Ep${_ep}..."
    python3 "$SEARCH_SCRIPT" --show "$OPT_NAME" --force --back-ep "$_ep"
    _ep=$((_ep + 1))
  done
  info "Backfill complete."
fi

echo ""
info "Done. Use 'sh showScheduler.sh --list' to see all shows."

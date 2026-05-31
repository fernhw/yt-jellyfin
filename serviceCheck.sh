#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# serviceCheck.sh — health-check every service in serviceCheck.md.
# If a service is DOWN: run its restart command, recheck, push notification.
# Notifications use the same OneSignal app as reportMaker.sh.
#
# Sources its config from the ```bash fenced block in serviceCheck.md so the
# .md is the single source of truth for ports / restart commands.
#
# Cron line (already exported PATH because cron has none):
#   47 * * * * PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
#     /Users/alexander-highground/Projects/yt-jellyfin/serviceCheck.sh >> /tmp/serviceCheck.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

# Belt-and-suspenders PATH (cron has nothing; interactive shell can also lose it).
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

set -u
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CONFIG_MD="${SCRIPT_DIR}/serviceCheck.md"
STATE_FILE="/tmp/serviceCheck.state"
SECRETS_FILE="${SCRIPT_DIR}/secrets.md"
ONESIGNAL_APP_ID="c88ae5a3-36df-4301-945f-9da65e63d87c"

DRY_RUN=0
FORCE_PUSH=0
for a in "$@"; do
  case "$a" in
    --dry-run)    DRY_RUN=1 ;;
    --force-push) FORCE_PUSH=1 ;;
  esac
done

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# ── load SERVICES array from the markdown ────────────────────────────────────
if [ ! -f "$CONFIG_MD" ]; then
  log "FATAL: config file missing: $CONFIG_MD"; exit 2
fi
TMP_CFG="$(mktemp -t serviceCheck.XXXXXX)"
# Extract the first ```bash ... ``` fenced block that contains "SERVICES=(".
awk '
  /^```bash[[:space:]]*$/ { in_block=1; next }
  /^```[[:space:]]*$/      { if (in_block) { in_block=0; if (found) exit } ; next }
  in_block                 { print; if ($0 ~ /SERVICES=\(/) found=1 }
' "$CONFIG_MD" > "$TMP_CFG"

if ! grep -q 'SERVICES=(' "$TMP_CFG"; then
  log "FATAL: could not find SERVICES=(...) bash block in $CONFIG_MD"
  rm -f "$TMP_CFG"; exit 2
fi
# shellcheck disable=SC1090
source "$TMP_CFG"
rm -f "$TMP_CFG"

# ── load OneSignal REST key (same parse as reportMaker.sh) ───────────────────
ONESIGNAL_KEY=""
if [ -f "$SECRETS_FILE" ]; then
  ONESIGNAL_KEY=$(awk -F'=' '/^K[0-9][0-9][0-9]=/{gsub(/"/, "", $2); printf $2}' "$SECRETS_FILE" 2>/dev/null)
fi

push_notify() {
  local heading="$1" body="$2"
  if [ -z "$ONESIGNAL_KEY" ]; then
    log "  push skipped (no ONESIGNAL_KEY in $SECRETS_FILE)"
    return
  fi
  local ip
  ip=$(dig +short onesignal.com @1.1.1.1 2>/dev/null | grep -E '^[0-9]+\.' | head -1)
  [ -z "$ip" ] && ip="104.16.160.145"
  curl -s -o /dev/null --resolve "onesignal.com:443:${ip}" \
    -X POST "https://onesignal.com/api/v1/notifications" \
    -H "Authorization: Basic ${ONESIGNAL_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"app_id\":\"${ONESIGNAL_APP_ID}\",\"included_segments\":[\"All\"],\"headings\":{\"en\":\"${heading}\"},\"contents\":{\"en\":\"${body}\"},\"url\":\"https://report.fernhw.com\"}"
}

probe() {
  # echoes HTTP code, or 000 if curl failed
  curl -ks -o /dev/null -w '%{http_code}' --max-time 6 "$1" 2>/dev/null || echo "000"
}

# Fetches the body (following redirects) and grep -Eq for the pattern.
# Returns 0 if pattern matches, 1 otherwise. Used as a content-level check
# so we catch "wrong container is answering on this port" (e.g. OnlyOffice
# accidentally responding on the Nextcloud port — both return 302 for /).
body_matches() {
  local url="$1" pattern="$2"
  local body
  body=$(curl -ksL --max-time 8 "$url" 2>/dev/null) || return 1
  echo "$body" | grep -Eq "$pattern"
}

# Pure-bash bounded run — macOS has no `timeout`. Kills the child after N seconds.
# Returns 124 if it timed out, otherwise the child's exit code.
with_timeout() {
  local secs="$1"; shift
  local cmd="$*"
  ( bash -c "$cmd" ) &
  local pid=$!
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
  local watcher=$!
  wait "$pid" 2>/dev/null
  local rc=$?
  # If the watcher already killed it, rc will reflect SIGKILL (137 usually).
  kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null
  if ! kill -0 "$pid" 2>/dev/null && [ "$rc" -ge 128 ]; then
    return 124
  fi
  return "$rc"
}

# Quick docker liveness check (5s).
docker_alive() {
  with_timeout 5 'docker info >/dev/null 2>&1'
}

DOCKER_ESCALATED=0
# escalate_docker_desktop intentionally removed — see commit history.
# Killing/restarting Docker Desktop from a cron script took down every
# container at once. Docker daemon hangs are now reported, NOT auto-fixed.

# expect: space-separated list of codes; success if $code is in the list
code_ok() {
  local code="$1" expect="$2"
  for c in $expect; do
    [ "$code" = "$c" ] && return 0
  done
  return 1
}

# State persisted as line-oriented "name=OK|DOWN" — works on macOS bash 3.2.
prev_state_of() {
  [ -f "$STATE_FILE" ] || { echo ""; return; }
  awk -F= -v n="$1" '$1==n{print $2; exit}' "$STATE_FILE"
}
NEW_STATE_FILE="$(mktemp -t serviceCheck.state.XXXXXX)"
overall_failed=""        # restarted and recovered
overall_recovered=""     # was DOWN previously, now OK without restart this run
overall_still_down=""    # DOWN even after restart (or no restart_cmd)

append_csv() { local var="$1"; shift; local cur="${!var}"; if [ -z "$cur" ]; then printf -v "$var" '%s' "$*"; else printf -v "$var" '%s, %s' "$cur" "$*"; fi; }

log "── serviceCheck start (dry_run=$DRY_RUN force_push=$FORCE_PUSH) ──"

for row in "${SERVICES[@]}"; do
  # 8th field expect_body is optional — older 7-field rows leave it empty.
  IFS='^' read -r name enabled type url expect restart_cmd recheck expect_body <<< "$row"
  [ "$enabled" != "1" ] && { log "$name: disabled, skipping"; continue; }

  prev=$(prev_state_of "$name")
  code=$(probe "$url")
  status_ok=0
  code_ok "$code" "$expect" && status_ok=1

  body_ok=1
  body_reason=""
  if [ -n "${expect_body:-}" ] && [ "$status_ok" = "1" ]; then
    if body_matches "$url" "$expect_body"; then
      body_ok=1
    else
      body_ok=0
      body_reason=" (body did not match /${expect_body}/)"
    fi
  fi

  if [ "$status_ok" = "1" ] && [ "$body_ok" = "1" ]; then
    echo "$name=OK" >> "$NEW_STATE_FILE"
    log "$name [$type] $url -> $code OK${expect_body:+ (body ✓)}"
    [ "$prev" = "DOWN" ] && append_csv overall_recovered "$name (now $code)"
    continue
  fi

  # ── service is DOWN ────────────────────────────────────────────────────────
  if [ "$status_ok" = "1" ]; then
    log "$name [$type] $url -> $code OK but DOWN${body_reason}"
    down_code="$code/body-mismatch"
  else
    log "$name [$type] $url -> $code DOWN (expected: $expect)"
    down_code="$code"
  fi
  if [ "$DRY_RUN" = "1" ] || [ -z "$restart_cmd" ]; then
    echo "$name=DOWN" >> "$NEW_STATE_FILE"
    append_csv overall_still_down "$name ($down_code)"
    [ -z "$restart_cmd" ] && log "  no restart_cmd configured; only notifying"
    continue
  fi

  log "  restarting: $restart_cmd"
  # Safety: if it's a docker command and docker itself is unresponsive,
  # DO NOT touch Docker Desktop — just notify and skip. Killing Docker Desktop
  # mid-flight takes down every container at once. A human must intervene.
  if [[ "$restart_cmd" == docker\ * ]] && ! docker_alive; then
    log "  !! Docker daemon unresponsive — refusing to restart Docker Desktop automatically"
    log "  !! Marking $name as still down; will notify so a human can investigate"
    echo "$name=DOWN" >> "$NEW_STATE_FILE"
    append_csv overall_still_down "$name (docker daemon hung)"
    continue
  fi
  with_timeout 60 "$restart_cmd" >> /tmp/serviceCheck.log 2>&1
  rc=$?
  if [ "$rc" = "124" ]; then
    log "  restart TIMED OUT after 60s"
  elif [ "$rc" != "0" ]; then
    log "  restart exited non-zero (rc=$rc, continuing)"
  fi
  sleep "${recheck:-5}"

  code2=$(probe "$url")
  status2_ok=0
  code_ok "$code2" "$expect" && status2_ok=1
  body2_ok=1
  if [ -n "${expect_body:-}" ] && [ "$status2_ok" = "1" ]; then
    body_matches "$url" "$expect_body" || body2_ok=0
  fi
  if [ "$status2_ok" = "1" ] && [ "$body2_ok" = "1" ]; then
    echo "$name=OK" >> "$NEW_STATE_FILE"
    log "  recheck $url -> $code2 RECOVERED${expect_body:+ (body ✓)}"
    append_csv overall_failed "$name (was $down_code, now $code2)"
  else
    echo "$name=DOWN" >> "$NEW_STATE_FILE"
    if [ "$status2_ok" = "1" ]; then
      log "  recheck $url -> $code2 STILL DOWN (body mismatch)"
      append_csv overall_still_down "$name (was $down_code, still $code2/body-mismatch)"
    else
      log "  recheck $url -> $code2 STILL DOWN"
      append_csv overall_still_down "$name (was $down_code, still $code2)"
    fi
  fi
done

# atomically replace state
mv "$NEW_STATE_FILE" "$STATE_FILE"
enabled_count=$(wc -l < "$STATE_FILE" | tr -d ' ')

# ── notify ──────────────────────────────────────────────────────────────────
should_push=0
heading=""
body=""

if [ -n "$overall_still_down" ]; then
  should_push=1
  heading="⚠ Service Down: restart failed"
  body="Still down: $overall_still_down"
elif [ -n "$overall_failed" ]; then
  should_push=1
  heading="Service Down: attempting restart"
  body="Restarted: $overall_failed"
elif [ -n "$overall_recovered" ]; then
  should_push=1
  heading="✓ Service recovered"
  body="$overall_recovered"
fi

if [ "$FORCE_PUSH" = "1" ] && [ "$should_push" = "0" ]; then
  should_push=1
  heading="Service Check: all OK"
  body="All ${enabled_count} enabled services healthy."
fi

if [ "$should_push" = "1" ] && [ "$DRY_RUN" != "1" ]; then
  log "push: $heading | $body"
  push_notify "$heading" "$body"
else
  log "no notification needed"
fi

log "── serviceCheck end ──"

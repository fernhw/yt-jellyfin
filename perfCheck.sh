#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# perfCheck.sh — hourly response-time recorder for status.fernhw.com
#
# Probes each local + public service, measures response time in ms, and
# appends a timestamped entry to web/status/perf.js (ring-buffer of 48h).
#
# Cron (added by setup): */15 * * * *  PATH=... /path/to/perfCheck.sh >> /tmp/perfCheck.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export LC_ALL=C

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PERF_JS="${SCRIPT_DIR}/web/status/perf.js"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# name|scope|url  (scope: local or public)
PERF_URLS=(
  "gyra|local|http://127.0.0.1:5050/"
  "jellyfin|local|http://127.0.0.1:8096/"
  "audiobookshelf|local|http://127.0.0.1:13378/"
  "nextcloud|local|http://127.0.0.1:7990/"
  "onlyoffice|local|http://127.0.0.1:7991/"
  "vaultwarden|local|http://127.0.0.1:7992/"
  "report|local|http://127.0.0.1:8765/"
  "request|local|http://127.0.0.1:8770/"
  "nginx|local|http://127.0.0.1:80/audiobookshelf/login"
  "gyra.fernhw.com|public|https://gyra.fernhw.com/"
  "jellyfin.fernhw.com|public|https://jellyfin.fernhw.com/"
  "drive.fernhw.com|public|https://drive.fernhw.com/"
  "abs.fernhw.com|public|https://abs.fernhw.com/"
  "vault.fernhw.com|public|https://vault.fernhw.com/"
  "status.fernhw.com|public|https://status.fernhw.com/"
)

log "── perfCheck start ──"

json_data=""
for entry in "${PERF_URLS[@]}"; do
  name="${entry%%|*}"
  rest="${entry#*|}"
  url="${rest#*|}"

  result=$(curl -ks -o /dev/null -w '%{http_code}:%{time_total}' --max-time 8 "$url" 2>/dev/null) || result="000:0"
  code="${result%%:*}"
  time_raw="${result##*:}"
  ms=$(awk "BEGIN{printf \"%d\", (\"${time_raw}\"+0)*1000}" 2>/dev/null) || ms=0

  # ok = HTTP code starts with 2 or 3
  ok="false"
  [[ "$code" =~ ^[23] ]] && ok="true"

  log "  ${name}: ${code} ${ms}ms (ok=${ok})"
  json_data="${json_data}\"${name}\":{\"ms\":${ms},\"ok\":${ok}},"
done

json_data="{${json_data%,}}"
iso_ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# ── update perf.js ring-buffer (48 entries = 2 days) via Python ──────────────
PERF_JS="$PERF_JS" TS="$iso_ts" ENTRY="$json_data" \
  /usr/bin/python3 << 'PYEOF'
import json, os, re, tempfile

pj    = os.environ["PERF_JS"]
ts    = os.environ["TS"]
entry = json.loads(os.environ["ENTRY"])

hours = []
if os.path.exists(pj):
    try:
        m = re.search(r'window\.PERF\s*=\s*(\{.+\})\s*;', open(pj).read(), re.DOTALL)
        if m:
            hours = json.loads(m.group(1)).get("hours", [])
    except Exception:
        pass

hours = [{"ts": ts, "data": entry}] + hours
hours = hours[:192]  # 192 × 15-min = 48h ring buffer

js = "window.PERF = " + json.dumps({"hours": hours}, separators=(',',':')) + ";\n"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(pj))
os.write(fd, js.encode())
os.close(fd)
os.rename(tmp, pj)
PYEOF

log "── perfCheck end ──"

#!/usr/bin/env zsh
# startup.sh — full stack startup, managed by com.fernhw.startup launchd plist
# Called automatically at every login/reboot. Do not run manually unless testing.

exec > >(tee -a /tmp/startup.log) 2>&1
echo "=== startup.sh BEGIN $(date) ==="

# ── Kill noisy agents ──────────────────────────────────────────
killall IMDPersistenceAgent 2>/dev/null || true

# ── Wait for Docker Desktop (max 2 min) ───────────────────────
echo "Waiting for Docker..."
i=0
until /usr/local/bin/docker info &>/dev/null; do
  sleep 2
  i=$((i + 1))
  [[ $i -ge 60 ]] && { echo "ERROR: Docker not ready after 2 min — aborting Docker stacks"; break; }
done
/usr/local/bin/docker info &>/dev/null && echo "Docker ready after $((i * 2))s" || true

# ── Docker compose stacks ─────────────────────────────────────
for stack in \
  /Users/alexander-highground/Projects/yt-jellyfin/docker/nginx \
  /Users/alexander-highground/Projects/yt-jellyfin/docker/audiobookshelf \
  /Users/alexander-highground/Projects/nextcloud \
  /Users/alexander-highground/Projects/vaultwarden; do
  if /usr/local/bin/docker info &>/dev/null; then
    (cd "$stack" && /usr/local/bin/docker compose up -d) \
      && echo "UP: $stack" \
      || echo "FAILED: $stack"
  fi
done

# ── GYRA Flask app (port 5050) ────────────────────────────────
lsof -iTCP:5050 -sTCP:LISTEN | awk '/Python/{print $2}' | xargs kill -9 2>/dev/null || true
sleep 1
cd /Users/alexander-highground/Projects/yt-jellyfin/gyra
nohup /usr/bin/python3 app.py > /tmp/gyra.log 2>&1 &
echo "GYRA pid=$!"

# ── metricsd (port 8766) ──────────────────────────────────────
lsof -iTCP:8766 -sTCP:LISTEN | awk '/Python/{print $2}' | xargs kill -9 2>/dev/null || true
sleep 1
cd /Users/alexander-highground/Projects/yt-jellyfin/web/status
nohup /usr/bin/python3 metricsd.py > /tmp/metricsd.log 2>&1 &
echo "metricsd pid=$!"

echo "=== startup.sh DONE $(date) ==="

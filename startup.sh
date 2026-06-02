#!/usr/bin/env zsh
# startup.sh — run on login / after reboot
# Add to Login Items or call from launchd as a wrapper

# ── Kill noisy agents ──────────────────────────────────────────
killall IMDPersistenceAgent 2>/dev/null || true

# ── GYRA Flask app (port 5050) ────────────────────────────────
lsof -iTCP:5050 -sTCP:LISTEN | awk '/Python/{print $2}' | xargs kill -9 2>/dev/null
sleep 1
cd /Users/alexander-highground/Projects/yt-jellyfin/gyra
nohup /usr/bin/python3 app.py > /tmp/gyra.log 2>&1 &
echo "GYRA started (pid $!)"

# ── metricsd (port 8766) ──────────────────────────────────────
lsof -iTCP:8766 -sTCP:LISTEN | awk '/Python/{print $2}' | xargs kill -9 2>/dev/null
sleep 1
cd /Users/alexander-highground/Projects/yt-jellyfin/web/status
nohup /usr/bin/python3 metricsd.py > /tmp/metricsd.log 2>&1 &
echo "metricsd started (pid $!)"

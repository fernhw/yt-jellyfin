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
  /Users/alexander-highground/Projects/nextcloud \
  /Users/alexander-highground/Projects/vaultwarden; do
  if /usr/local/bin/docker info &>/dev/null; then
    (cd "$stack" && /usr/local/bin/docker compose up -d) \
      && echo "UP: $stack" \
      || echo "FAILED: $stack"
  fi
done


echo "=== startup.sh DONE $(date) ==="

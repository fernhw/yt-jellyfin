#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# docker_watchdog.sh — detect Docker Desktop crash and recover automatically
#
# Checks:
#   1. Docker daemon API responsive?  → if not, restart Docker Desktop
#   2. nginx-local container running? → if not, docker compose up -d
#
# Cron (every 5 min):
#   */5 * * * * /Users/alexander-highground/Projects/yt-jellyfin/docker_watchdog.sh >> /tmp/docker_watchdog.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

NGINX_COMPOSE_DIR="/Users/alexander-highground/Projects/yt-jellyfin/docker/nginx"
LOG_TAG="docker_watchdog"
DOCKER_BOOT_WAIT=45   # seconds to wait for Docker Desktop to become ready
NGINX_CONTAINER="nginx-local"

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] [${LOG_TAG}] $*"; }

# ── 1. Check if Docker daemon is alive ───────────────────────────────────────
docker_ok() {
    docker info --format '{{.ServerVersion}}' >/dev/null 2>&1
}

if docker_ok; then
    # ── 2. Docker is up — make sure nginx-local is running ───────────────────
    container_state=$(docker inspect --format '{{.State.Running}}' "$NGINX_CONTAINER" 2>/dev/null)
    if [[ "$container_state" != "true" ]]; then
        log "nginx-local not running (state=${container_state:-missing}) — starting..."
        cd "$NGINX_COMPOSE_DIR" && docker compose up -d
        log "nginx-local start exit=$?"
    fi
    # Silent exit if everything is healthy
    exit 0
fi

# ── Docker daemon unresponsive — restart Docker Desktop ──────────────────────
log "Docker daemon unresponsive — restarting Docker Desktop"

# Kill any zombie Docker processes
killall -9 "Docker Desktop" com.docker.backend com.docker.vmnetd com.docker.hyperkit 2>/dev/null
sleep 3

# Launch Docker Desktop (GUI app; GUI not required — just starts the daemon)
open -a Docker
log "Docker Desktop launched — waiting up to ${DOCKER_BOOT_WAIT}s for daemon..."

# Wait for daemon to become ready
deadline=$(( $(date +%s) + DOCKER_BOOT_WAIT ))
until docker_ok || [[ $(date +%s) -ge $deadline ]]; do
    sleep 3
done

if ! docker_ok; then
    log "ERROR: Docker daemon did not come up within ${DOCKER_BOOT_WAIT}s — giving up this cycle"
    exit 1
fi

log "Docker daemon is back ($(docker info --format '{{.ServerVersion}}' 2>/dev/null))"

# Give containers a moment to auto-restart (restart: unless-stopped)
sleep 5

# Ensure nginx-local is up (may not have auto-started if compose project wasn't loaded)
container_state=$(docker inspect --format '{{.State.Running}}' "$NGINX_CONTAINER" 2>/dev/null)
if [[ "$container_state" != "true" ]]; then
    log "nginx-local not running after Docker restart — starting via compose..."
    cd "$NGINX_COMPOSE_DIR" && docker compose up -d
    log "nginx-local start exit=$?"
else
    log "nginx-local already running after Docker restart"
fi

log "Recovery complete"

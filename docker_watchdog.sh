#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# docker_watchdog.sh — detect Docker Desktop crash and recover automatically
#
# nginx is now a native brew service (not in Docker) so this watchdog
# only needs to ensure the Docker daemon itself is healthy.
# Docker is still needed for: Nextcloud, OnlyOffice, Vaultwarden, Immich, etc.
#
# NOTE: nginx is managed by: sudo brew services restart nginx
#       nginx config: /opt/homebrew/etc/nginx/nginx.conf
#
# Cron (every 5 min):
#   */5 * * * * /Users/alexander-highground/Projects/yt-jellyfin/docker_watchdog.sh >> /tmp/docker_watchdog.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

LOG_TAG="docker_watchdog"
DOCKER_BOOT_WAIT=60   # seconds to wait for Docker Desktop to become ready

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] [${LOG_TAG}] $*"; }

# ── Check if Docker daemon is fully functional (not just partially up) ───────
# docker info can return exit 0 even when the API is returning 500.
# docker ps hits the exact containers/json endpoint that breaks when degraded.
docker_ok() {
    docker ps >/dev/null 2>&1
}

restart_docker() {
    log "Restarting Docker Desktop..."
    killall -9 "Docker Desktop" com.docker.backend com.docker.vmnetd com.docker.hyperkit 2>/dev/null
    sleep 3
    open -a Docker
    log "Docker Desktop launched — waiting up to ${DOCKER_BOOT_WAIT}s..."
    local deadline=$(( $(date +%s) + DOCKER_BOOT_WAIT ))
    until docker_ok || [[ $(date +%s) -ge $deadline ]]; do
        sleep 3
    done
    if ! docker_ok; then
        log "ERROR: Docker did not recover within ${DOCKER_BOOT_WAIT}s"
        return 1
    fi
    log "Docker daemon back ($(docker info --format '{{.ServerVersion}}' 2>/dev/null))"
    return 0
}

if docker_ok; then
    exit 0
fi

# ── Docker daemon unresponsive ────────────────────────────────────────────────
log "Docker daemon unresponsive (docker ps failed)"
restart_docker || exit 1

log "Recovery complete"

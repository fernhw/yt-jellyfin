#!/bin/zsh
# mount-borr-volumes.sh — mount Borr's shared volumes on Agnos
# Managed by com.fernhw.mountborr LaunchDaemon (RunAtLoad + KeepAlive interval)
# Credentials stored in macOS Keychain — no plaintext passwords here.

BORR_IP="192.168.88.212"
BORR_USER="alexander-highground"

mount_if_needed() {
  local share="$1" mountpoint="$2"
  if mount | grep -q "on ${mountpoint} "; then
    echo "[$(date)] already mounted: ${mountpoint}"
    return
  fi
  echo "[$(date)] mounting //${BORR_USER}@${BORR_IP}/${share} → ${mountpoint}"
  mount_smbfs -N "//${BORR_USER}@${BORR_IP}/${share}" "${mountpoint}" 2>&1
  if [ $? -eq 0 ]; then
    echo "[$(date)] OK: ${mountpoint}"
  else
    echo "[$(date)] FAILED: ${mountpoint} — check SMB credentials in Keychain"
  fi
}

mount_if_needed "darrel4tb" "/Volumes/Darrel4tb"
mount_if_needed "jellyfin"  "/Volumes/Jellyfin"

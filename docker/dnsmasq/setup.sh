#!/usr/bin/env bash
# Sets up dnsmasq on agnos for local *.home DNS resolution.
# Run once: bash docker/dnsmasq/setup.sh
#
# After this:
#   - jellyfin.home, nextcloud.home, abs.home resolve to 192.168.0.8 on this machine
#   - Other devices: set their DNS server to 192.168.0.8 (Wi-Fi settings)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BREW_PREFIX="$(brew --prefix)"
BREW_CONF="${BREW_PREFIX}/etc/dnsmasq.conf"

# ── Install ────────────────────────────────────────────────
if ! command -v dnsmasq &>/dev/null; then
    echo "Installing dnsmasq..."
    brew install dnsmasq
fi

# ── Config ─────────────────────────────────────────────────
echo "Copying dnsmasq.conf → ${BREW_CONF}"
cp "${SCRIPT_DIR}/dnsmasq.conf" "${BREW_CONF}"

# ── macOS resolver ─────────────────────────────────────────
# Tells macOS to route *.home queries to local dnsmasq (127.0.0.1)
sudo mkdir -p /etc/resolver
echo "nameserver 127.0.0.1" | sudo tee /etc/resolver/home > /dev/null
echo "Created /etc/resolver/home"

# ── Start ──────────────────────────────────────────────────
# Must run as root so dnsmasq can bind port 53
sudo "${BREW_PREFIX}/bin/brew" services restart dnsmasq

echo ""
echo "Done. dnsmasq is running."
echo ""
echo "This machine (agnos) now resolves *.home automatically."
echo "Other devices on the network: set DNS to 192.168.0.8 in Wi-Fi settings."

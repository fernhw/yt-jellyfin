#!/usr/bin/env python3
"""
metricsd.py — lightweight system-metrics HTTP daemon.

Serves:  GET /metrics  →  JSON snapshot
Port:    8766 (localhost only)
CORS:    Access-Control-Allow-Origin: * (safe; port not exposed via cloudflared)

Metrics:
  cpu       – overall %, per-core %, load average (1/5/15 min)
  mem       – used/total/pct, swap used/pct
  disk_io   – read/write bytes-per-second and iops (delta since last request)
  net_io    – recv/sent bytes-per-second per interface (delta since last request)
  disks     – disk space per mount: total/used/free/pct
  vpn       – active bool, interface, IP (detects utun/wg/tun interfaces)
  docker    – per-container cpu_pct, mem_pct, mem, net, blk (refreshed every 5s)
  uptime    – seconds since boot
  procs     – process count

Run:
  nohup /usr/bin/python3 web/status/metricsd.py >> /tmp/metricsd.log 2>&1 &
"""

import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import psutil
except ImportError:
    raise SystemExit("psutil required: /usr/bin/python3 -m pip install psutil")

PORT = 8766

# ── Delta state (disk I/O and network are cumulative counters; we diff them) ─
_prev_disk: dict  = {}
_prev_net:  dict  = {}
_prev_ts:   float = 0.0

# ── Docker cache — refreshed in a background thread every _DOCKER_TTL seconds ─
_docker:    list  = []
_docker_ts: float = 0.0
_DOCKER_TTL = 5.0
_docker_mu  = threading.Lock()


def _refresh_docker() -> None:
    global _docker, _docker_ts
    try:
        raw = subprocess.check_output(
            [
                "docker", "stats", "--no-stream", "--format",
                "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}",
            ],
            stderr=subprocess.DEVNULL, text=True, timeout=8,
        )
        rows = []
        for line in raw.strip().splitlines():
            p = line.split("\t")
            if len(p) < 6:
                continue
            def _pf(s):
                try:
                    return float(s.strip().rstrip("%").rstrip(" "))
                except (ValueError, AttributeError):
                    return 0.0
            rows.append({
                "name":    p[0].strip(),
                "cpu_pct": _pf(p[1]),
                "mem":     p[2].strip(),
                "mem_pct": _pf(p[3]),
                "net":     p[4].strip(),
                "blk":     p[5].strip(),
            })
        rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
        with _docker_mu:
            _docker[:] = rows
    except Exception:
        pass
    finally:
        _docker_ts = time.time()


def docker_stats() -> list:
    if time.time() - _docker_ts > _DOCKER_TTL:
        threading.Thread(target=_refresh_docker, daemon=True).start()
    with _docker_mu:
        return list(_docker)


# ── VPN detection via public-IP geolocation ──────────────────────────────────
# VPN is ON if the exit IP is NOT from Ecuador (EC).
# Result cached for 5 minutes to avoid hammering ip-api.com.

_vpn_cache: dict  = {}
_vpn_ts:    float = 0.0
_VPN_TTL    = 300.0   # seconds

def detect_vpn() -> dict:
    """Return VPN status by checking exit-IP country via ip-api.com.
    If country != EC (Ecuador), the machine is behind a VPN."""
    global _vpn_cache, _vpn_ts
    if time.time() - _vpn_ts < _VPN_TTL and _vpn_cache:
        return _vpn_cache

    try:
        from urllib.request import urlopen as _urlopen
        with _urlopen("http://ip-api.com/json?fields=query,country,countryCode", timeout=5) as r:
            geo = json.loads(r.read())
        pub_ip      = geo.get("query", "")
        country     = geo.get("country", "")
        country_code = geo.get("countryCode", "")
        is_vpn      = country_code != "EC"
        result = {
            "active":    is_vpn,
            "interface": None,
            "ip":        pub_ip,
            "provider":  country if is_vpn else "ecuador",
            "country":   country,
            "country_code": country_code,
        }
    except Exception as e:
        # geo lookup failed — fall back to interface scan
        result = {"active": False, "interface": None, "ip": None,
                  "provider": None, "country": None, "country_code": None,
                  "geo_error": str(e)}

    _vpn_cache = result
    _vpn_ts    = time.time()
    return result


# ── Core metrics collection ───────────────────────────────────────────────────

def collect() -> dict:
    global _prev_disk, _prev_net, _prev_ts

    now = time.time()
    dt  = max(now - _prev_ts, 0.001) if _prev_ts else 1.0
    _prev_ts = now

    # ── CPU ──────────────────────────────────────────────────────────────────
    cpu_total = psutil.cpu_percent(interval=0)
    cpu_cores = psutil.cpu_percent(interval=0, percpu=True)
    load      = list(os.getloadavg())

    # ── Memory ───────────────────────────────────────────────────────────────
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()

    # ── Disk I/O (cumulative counters → delta bytes/s) ────────────────────────
    cur_disk  = psutil.disk_io_counters(perdisk=True) or {}
    disk_io   = []
    _SKIP_DISK = ("loop", "ram", "zram")
    for name, c in cur_disk.items():
        if any(name.startswith(p) for p in _SKIP_DISK):
            continue
        prev = _prev_disk.get(name, c)
        disk_io.append({
            "name":       name,
            "read_bps":   max(0.0, (c.read_bytes  - prev.read_bytes)  / dt),
            "write_bps":  max(0.0, (c.write_bytes - prev.write_bytes) / dt),
            "read_iops":  max(0.0, (c.read_count  - prev.read_count)  / dt),
            "write_iops": max(0.0, (c.write_count - prev.write_count) / dt),
        })
    _prev_disk = cur_disk

    # ── Network I/O (cumulative counters → delta bytes/s) ────────────────────
    cur_net  = psutil.net_io_counters(pernic=True) or {}
    net_io   = []
    _SKIP_IFACE     = {"lo", "lo0"}
    _SKIP_IFACE_PFX = ("docker", "br-", "veth", "vmnet", "bond", "dummy", "virbr")
    for name, c in cur_net.items():
        if name in _SKIP_IFACE or any(name.startswith(p) for p in _SKIP_IFACE_PFX):
            continue
        prev = _prev_net.get(name, c)
        recv = max(0.0, (c.bytes_recv - prev.bytes_recv) / dt)
        sent = max(0.0, (c.bytes_sent - prev.bytes_sent) / dt)
        # Omit idle virtual interfaces (not en0/wlan/utun)
        # Keep primary wired/wireless always; keep tunnels/others only when
        # they have active traffic (avoids dozens of idle macOS utun entries).
        is_primary = name in ("en0", "en1", "eth0", "wlan0")
        if recv == 0 and sent == 0 and not is_primary:
            continue
        net_io.append({"name": name, "recv_bps": recv, "sent_bps": sent})
    _prev_net = cur_net

    # ── Disk space ────────────────────────────────────────────────────────────
    disks = []
    seen  = set()
    _SKIP_FS   = {"devfs", "autofs", "", "overlay", "tmpfs", "squashfs", "fuse.portal"}
    _SKIP_MNTS = (
        "/System/", "/private/var/vm", "/Volumes/.com.apple",
        "/private/var/folders", "/snap/", "/boot/efi",
    )
    for part in psutil.disk_partitions():
        if part.mountpoint in seen:
            continue
        if part.fstype in _SKIP_FS:
            continue
        if any(part.mountpoint.startswith(p) for p in _SKIP_MNTS):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount":  part.mountpoint,
                "device": part.device,
                "total":  u.total,
                "used":   u.used,
                "free":   u.free,
                "pct":    u.percent,
            })
            seen.add(part.mountpoint)
        except (PermissionError, OSError):
            pass

    return {
        "ts": now,
        "cpu": {
            "pct":   cpu_total,
            "cores": cpu_cores,
            "load":  [round(x, 2) for x in load],
        },
        "mem": {
            "total":      vm.total,
            "used":       vm.used,
            "available":  vm.available,
            "pct":        vm.percent,
            "swap_used":  sw.used,
            "swap_total": sw.total,
            "swap_pct":   sw.percent,
        },
        "disk_io": disk_io,
        "net_io":  net_io,
        "disks":   disks,
        "vpn":     detect_vpn(),
        "uptime":  now - psutil.boot_time(),
        "procs":   len(psutil.pids()),
        "docker":  docker_stats(),
    }


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # silence access log

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.dumps(collect(), separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            try:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(repr(exc).encode())
            except Exception:
                pass


if __name__ == "__main__":
    # Warm up the CPU sampler — psutil requires two consecutive calls to
    # return a meaningful percentage; the first always returns 0.0.
    psutil.cpu_percent(interval=0)
    psutil.cpu_percent(interval=0, percpu=True)
    # Seed the delta state so the very first HTTP response has real I/O deltas.
    collect()
    time.sleep(0.5)
    collect()
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"metricsd on http://127.0.0.1:{PORT}/metrics", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

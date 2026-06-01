#!/usr/bin/env python3
"""metricsCheck.py — collect one metrics snapshot, prepend to metrics.js.

Run every 5 min via cron:
  */5 * * * * /usr/bin/python3 /Users/alexander-highground/Projects/yt-jellyfin/web/status/metricsCheck.py >> /tmp/metricsCheck.log 2>&1
"""

import json, os
from urllib.request import urlopen
from urllib.error import URLError
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_JS = os.path.join(SCRIPT_DIR, "metrics.js")
DAEMON_URL = "http://127.0.0.1:8766/metrics"
MAX_KEEP   = 288   # 24 h at 5-min intervals

try:
    with urlopen(DAEMON_URL, timeout=8) as r:
        data = json.loads(r.read())
except URLError as e:
    raise SystemExit(f"metricsCheck: cannot reach metricsd at {DAEMON_URL}: {e}")

def _sum(lst, key):
    return round(sum(d.get(key, 0) for d in (lst or [])))

snap = {
    "ts":        datetime.fromtimestamp(data["ts"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "cpu":       round(data["cpu"]["pct"], 1),
    "load1":     data["cpu"]["load"][0],
    "mem_pct":   round(data["mem"]["pct"], 1),
    "mem_used":  round(data["mem"]["used"]       / 1073741824, 2),
    "mem_tot":   round(data["mem"]["total"]      / 1073741824, 2),
    "swap_pct":  round(data["mem"]["swap_pct"],  1),
    "swap_used": round(data["mem"]["swap_used"]  / 1073741824, 2),
    "swap_tot":  round(data["mem"]["swap_total"] / 1073741824, 2),
    "disk_r":    _sum(data.get("disk_io", []), "read_bps"),
    "disk_w":    _sum(data.get("disk_io", []), "write_bps"),
    "disk_io":   [{"n": d["name"], "l": d.get("label", d["name"]), "r": round(d["read_bps"]), "w": round(d["write_bps"]), "mr": d.get("max_r", 0), "mw": d.get("max_w", 0)} for d in data.get("disk_io", [])],
    "net_in":    _sum(data.get("net_io",  []), "recv_bps"),
    "net_out":   _sum(data.get("net_io",  []), "sent_bps"),
    "vpn":       data.get("vpn", {}).get("active", False),
    "vpn_if":    data.get("vpn", {}).get("country_code") or data.get("vpn", {}).get("interface"),
    "vpn_ip":    data.get("vpn", {}).get("ip"),
    "uptime":    round(data.get("uptime", 0)),
    "procs":     data.get("procs", 0),
    "disks":     [
        {
            "m":   d["mount"],
            "pct": d["pct"],
            "used": round(d["used"]  / 1e9, 1),
            "tot":  round(d["total"] / 1e9, 1),
        }
        for d in data.get("disks", [])
    ],
}

history = []
if os.path.exists(METRICS_JS):
    try:
        txt = open(METRICS_JS).read().strip()
        if txt.startswith("window.METRICS_HIST"):
            txt = txt[txt.index("=") + 1:].rstrip(";").strip()
        history = json.loads(txt)
    except Exception:
        history = []

history.insert(0, snap)
history = history[:MAX_KEEP]

with open(METRICS_JS, "w") as f:
    f.write("window.METRICS_HIST=")
    json.dump(history, f, separators=(",", ":"))
    f.write(";")

print(f"metricsCheck: {len(history)} entries, cpu={snap['cpu']}% mem={snap['mem_pct']}%")

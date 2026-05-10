#!/usr/bin/env python3
"""
requestServer.py — Media request web UI server.
Runs on port 8770, served at request.fernhw.com via cloudflared.

Start:  python3 requestServer.py
"""

import csv
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory, abort, Response

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.realpath(__file__))
SCHEDULE_CSV  = os.path.join(SCRIPT_DIR, 'showSchedule.csv')
SECRETS_FILE  = os.path.join(SCRIPT_DIR, 'secrets.md')
LOCATIONS_FILE = os.path.join(SCRIPT_DIR, 'locations.md')
SCHEDULER_LOG = os.path.join(SCRIPT_DIR, 'showScheduler.log')
DOWNLOADER_LOG = os.path.join(SCRIPT_DIR, 'downloader.log')
ARIA2_LOG     = os.path.join(SCRIPT_DIR, 'aria2.log')
STATIC_DIR    = os.path.join(SCRIPT_DIR, 'request_web')
PORT          = 8770
ARIA2_RPC_PORT = 6802

CSV_FIELDS = [
    'show_name', 'search_name', 'folder', 'type', 'season', 'next_episode',
    'total_episodes', 'release_days', 'status',
    'search_start', 'search_end', 'last_check', 'week_anchor', 'anchor_episode',
]

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='')

# ── Config ─────────────────────────────────────────────────────────────────────

def _load_kv(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out

def _web_password_hash() -> str:
    """SHA-256 of WEB_PASS from secrets.md (or sha256 of hostname as fallback).
    The client hashes the raw password before sending, so we compare hashes."""
    s = _load_kv(SECRETS_FILE)
    raw = s.get('WEB_PASS') or hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]
    return hashlib.sha256(raw.encode()).hexdigest()

# ── Auth ───────────────────────────────────────────────────────────────────────

def _check_auth() -> bool:
    token = (request.headers.get('X-Auth-Token', '')
             or request.args.get('token', '')
             or request.cookies.get('req_token', ''))
    if not token:
        return False
    return hmac.compare_digest(token, _web_password_hash())

def _require_auth(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*a, **kw):
        if not _check_auth():
            abort(401)
        return f(*a, **kw)
    return wrapper

# ── CSV helpers ────────────────────────────────────────────────────────────────

def read_schedule() -> List[Dict]:
    if not os.path.exists(SCHEDULE_CSV):
        return []
    with open(SCHEDULE_CSV, newline='') as f:
        return list(csv.DictReader(f))

def write_schedule(rows: List[Dict]) -> None:
    with open(SCHEDULE_CSV, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

# ── Log tail helper ───────────────────────────────────────────────────────────

def _tail_log(n: int = 120) -> str:
    lines: list = []
    for path in (SCHEDULER_LOG, DOWNLOADER_LOG, ARIA2_LOG):
        try:
            with open(path) as f:
                lines.extend(f.readlines()[-n:])
        except Exception:
            pass
    return ''.join(lines[-n:])

# ── aria2c RPC helpers ─────────────────────────────────────────────────────────
# aria2c is managed as a persistent RPC daemon (port 6802).
# All downloads go through it — showSchedulerSearch.py and download.sh both
# call aria2.addUri via RPC.  This server starts/monitors the daemon and
# exposes its live download list to the UI.

_aria2_proc: Optional[subprocess.Popen] = None

def _aria2_secret() -> str:
    return _load_kv(SECRETS_FILE).get('ARIA2_SECRET', 'aria2rpc2026')

def _aria2_rpc(method: str, params: Optional[List] = None) -> object:
    """Call aria2 JSON-RPC.  Raises on error or timeout."""
    import urllib.request as _ureq
    body = json.dumps({
        'jsonrpc': '2.0', 'id': 'srv', 'method': method,
        'params': [f'token:{_aria2_secret()}'] + (params or []),
    }).encode()
    req = _ureq.Request(
        f'http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc',
        data=body, headers={'Content-Type': 'application/json'},
    )
    with _ureq.urlopen(req, timeout=4) as r:
        resp = json.load(r)
    if 'error' in resp:
        raise RuntimeError(resp['error'].get('message', 'aria2 error'))
    return resp.get('result')

def _fmt_speed(bps: int) -> str:
    if bps >= 1_048_576: return f'{bps/1_048_576:.1f} MB/s'
    if bps >= 1024:      return f'{bps/1024:.0f} KB/s'
    return f'{bps} B/s'

def _fmt_size(b: int) -> str:
    if b >= 1_073_741_824: return f'{b/1_073_741_824:.2f} GB'
    if b >= 1_048_576:     return f'{b/1_048_576:.0f} MB'
    if b >= 1024:          return f'{b/1024:.0f} KB'
    return f'{b} B'

_ARIA2_KEYS = [
    'gid', 'status', 'totalLength', 'completedLength',
    'downloadSpeed', 'uploadSpeed', 'numSeeders', 'connections',
    'bittorrent', 'files', 'errorMessage',
]

def _aria2_active_downloads() -> List[Dict]:
    """Return live download list from aria2c RPC."""
    try:
        active  = _aria2_rpc('aria2.tellActive',  [_ARIA2_KEYS]) or []
        waiting = _aria2_rpc('aria2.tellWaiting', [0, 20, _ARIA2_KEYS]) or []
    except Exception:
        return []

    out: List[Dict] = []
    for item in active + waiting:
        total     = int(item.get('totalLength')     or 0)
        done      = int(item.get('completedLength') or 0)
        pct       = int(done * 100 / total) if total > 0 else 0
        speed_b   = int(item.get('downloadSpeed')  or 0)
        up_b      = int(item.get('uploadSpeed')    or 0)
        seeds     = int(item.get('numSeeders')     or 0)
        peers     = int(item.get('connections')    or 0)
        status    = item.get('status', 'active')

        bt   = item.get('bittorrent') or {}
        name = (bt.get('info') or {}).get('name', '')
        if not name:
            files = item.get('files') or []
            if files:
                name = (files[0].get('path') or '').rsplit('/', 1)[-1]

        out.append({
            'gid':         item.get('gid', ''),
            'name':        name or '(unknown)',
            'pct':         pct,
            'speed':       _fmt_speed(speed_b),
            'speed_bytes': speed_b,
            'upload':      _fmt_speed(up_b),
            'seeds':       seeds,
            'peers':       peers,
            'status':      status,
            'size':        _fmt_size(total),
            'done':        _fmt_size(done),
            'total_bytes': total,
        })
    return out

def _ensure_aria2_daemon() -> None:
    """Start aria2c RPC daemon if it isn't already responding."""
    global _aria2_proc
    # Already alive and responsive?
    try:
        _aria2_rpc('aria2.getVersion')
        return
    except Exception:
        pass
    # Check if the child we spawned is still running
    if _aria2_proc is not None and _aria2_proc.poll() is None:
        time.sleep(1)  # Give it a moment if just started
        return
    # Find aria2c binary
    aria2c_bin = (
        shutil.which('aria2c')
        or '/opt/homebrew/bin/aria2c'
        or '/usr/local/bin/aria2c'
    )
    if not aria2c_bin or not os.path.exists(aria2c_bin):
        return
    cmd = [
        aria2c_bin,
        '--enable-rpc=true',
        f'--rpc-listen-port={ARIA2_RPC_PORT}',
        f'--rpc-secret={_aria2_secret()}',
        '--rpc-allow-origin-all=true',
        '--enable-color=false',
        '--seed-time=0',
        '--file-allocation=falloc',
        '--max-concurrent-downloads=16',
        '--allow-overwrite=true',
        '--auto-file-renaming=false',
        f'--log={ARIA2_LOG}',
        '--log-level=warn',
    ]
    _aria2_proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1.5)  # Wait for RPC socket to be ready

def _aria2_watchdog() -> None:
    """Background thread: keep aria2c daemon alive."""
    while True:
        try:
            _ensure_aria2_daemon()
        except Exception:
            pass
        time.sleep(30)

# ── API routes ─────────────────────────────────────────────────────────────────

@app.route('/api/ping')
def ping():
    return jsonify({'ok': True})


@app.route('/auth', methods=['POST'])
def do_auth():
    """Accept raw password (hashed server-side) or pre-hashed token. Sets auth cookie."""
    data = request.get_json(force=True) or {}
    raw   = (data.get('password') or '').strip()
    token = (data.get('token')    or '').strip()
    expected = _web_password_hash()  # sha256(WEB_PASS)
    valid = False
    if raw:
        valid = hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), expected)
    elif token:
        valid = hmac.compare_digest(token, expected)
    if not valid:
        abort(401)
    from flask import make_response
    resp = make_response(jsonify({'ok': True}))
    # Cookie is always the hash — httponly=False so JS can read it as X-Auth-Token
    resp.set_cookie('req_token', expected, max_age=2592000, samesite='Strict', httponly=False)
    return resp

@app.route('/api/schedule', methods=['GET'])
@_require_auth
def get_schedule():
    return jsonify(read_schedule())

@app.route('/api/schedule/<int:idx>', methods=['DELETE'])
@_require_auth
def delete_show(idx: int):
    rows = read_schedule()
    if idx < 0 or idx >= len(rows):
        abort(404)
    removed = rows.pop(idx)
    write_schedule(rows)
    return jsonify({'removed': removed['show_name']})

@app.route('/api/schedule', methods=['POST'])
@_require_auth
def add_show():
    data = request.get_json(force=True)
    required = ['show_name', 'search_name', 'folder', 'type', 'season',
                'next_episode', 'total_episodes', 'release_days']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'missing field: {field}'}), 400

    today = datetime.date.today().isoformat()
    row = {
        'show_name':      data['show_name'].strip(),
        'search_name':    data['search_name'].strip().lower(),
        'folder':         data['folder'].strip(),
        'type':           data['type'].strip(),
        'season':         str(data['season']),
        'next_episode':   str(data['next_episode']),
        'total_episodes': str(data['total_episodes']),
        'release_days':   data['release_days'].strip().lower(),
        'status':         'pending',
        'search_start':   '',
        'search_end':     '',
        'last_check':     today,
        'week_anchor':    '',
        'anchor_episode': '',
    }
    rows = read_schedule()
    rows.append(row)
    write_schedule(rows)
    return jsonify({'added': row['show_name']}), 201

@app.route('/api/schedule/<int:idx>/backlog', methods=['POST'])
@_require_auth
def backlog_show(idx: int):
    """Reset a show to episode 1 and kick off a forced sequential download
    of all episodes in a background thread."""
    rows = read_schedule()
    if idx < 0 or idx >= len(rows):
        abort(404)
    row = rows[idx]
    show_name = row['show_name']
    total = int(row.get('total_episodes') or 99)
    # Reset CSV so scheduler starts from ep 1
    rows[idx]['next_episode'] = '1'
    rows[idx]['status'] = 'pending'
    write_schedule(rows)

    search_script = os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py')

    def _run_backlog():
        for _ in range(total):
            subprocess.run(
                [sys.executable, search_script, '--show', show_name, '--force'],
                capture_output=True,
            )
            # Each call blocks until aria2c finishes downloading that episode.
            # Short pause to avoid hammering tracker APIs between episodes.
            time.sleep(5)

    threading.Thread(target=_run_backlog, daemon=True).start()
    return jsonify({'backlog': show_name, 'from_ep': 1, 'total': total}), 202


@app.route('/api/movie', methods=['POST'])
@_require_auth
def request_movie():
    data = request.get_json(force=True)
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    is_anime = bool(data.get('anime'))
    quality = (data.get('quality') or '1080').lower()
    prefer_4k = quality in ('4k', '2160p', '4k2160', '4k/2160p')
    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py'),
        '--movie', title,
    ]
    if is_anime:
        cmd.append('--anime-movie')
    if prefer_4k:
        cmd.append('--prefer-4k')
    threading.Thread(target=subprocess.run, args=(cmd,), daemon=True).start()
    return jsonify({'queued': title, 'anime': is_anime, 'quality': quality}), 202

@app.route('/api/torrent-state')
@_require_auth
def torrent_state():
    downloads = _aria2_active_downloads()
    return jsonify({'downloads': downloads, 'updated': time.time()})

@app.route('/api/torrent/<gid>/remove', methods=['POST'])
@_require_auth
def torrent_remove(gid: str):
    if not re.fullmatch(r'[0-9a-f]{1,16}', gid):
        abort(400)
    try:
        _aria2_rpc('aria2.forceRemove', [gid])
    except Exception:
        try:
            _aria2_rpc('aria2.removeDownloadResult', [gid])
        except Exception:
            pass
    return jsonify({'removed': gid})

@app.route('/api/torrent/<gid>/pause', methods=['POST'])
@_require_auth
def torrent_pause(gid: str):
    if not re.fullmatch(r'[0-9a-f]{1,16}', gid):
        abort(400)
    try:
        _aria2_rpc('aria2.pause', [gid])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'paused': gid})

@app.route('/api/torrent/<gid>/unpause', methods=['POST'])
@_require_auth
def torrent_unpause(gid: str):
    if not re.fullmatch(r'[0-9a-f]{1,16}', gid):
        abort(400)
    try:
        _aria2_rpc('aria2.unpause', [gid])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'unpaused': gid})

@app.route('/api/log')
@_require_auth
def get_log():
    n = min(int(request.args.get('lines', 80)), 300)
    return Response(_tail_log(n), mimetype='text/plain')

# ── Static UI ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # Server-side cookie check: only serve the full app if authenticated.
    # Unauthenticated visitors see login.html (no app structure exposed).
    token = request.cookies.get('req_token', '')
    if token and hmac.compare_digest(token, _web_password_hash()):
        resp = send_from_directory(STATIC_DIR, 'index.html')
    else:
        resp = send_from_directory(STATIC_DIR, 'login.html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(STATIC_DIR, 'favicon.svg', mimetype='image/svg+xml')

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Start aria2c RPC daemon and watchdog
    _ensure_aria2_daemon()
    wd = threading.Thread(target=_aria2_watchdog, daemon=True)
    wd.start()

    kv = _load_kv(SECRETS_FILE)
    pw = kv.get('WEB_PASS', '(using hostname fallback)')
    aria2_sec = kv.get('ARIA2_SECRET', 'aria2rpc2026')
    print(f"  request server  → http://localhost:{PORT}")
    print(f"  aria2c RPC      → http://localhost:{ARIA2_RPC_PORT}")
    print(f"  aria2 secret    : {aria2_sec}")
    print(f"  web password    : {pw}")
    print(f"  token hash      : {_web_password_hash()[:16]}...")
    app.run(host='0.0.0.0', port=PORT, debug=False)

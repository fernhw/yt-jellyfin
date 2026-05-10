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
TORRENT_STATE = os.path.join(SCRIPT_DIR, '.torrent_state.json')
STATIC_DIR    = os.path.join(SCRIPT_DIR, 'request_web')
PORT          = 8770

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
    token = request.headers.get('X-Auth-Token', '') or request.args.get('token', '')
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

# ── Torrent state file (written by aria2c watcher thread) ─────────────────────

def _read_torrent_state() -> Dict:
    try:
        with open(TORRENT_STATE) as f:
            return json.load(f)
    except Exception:
        return {}

def _tail_log(n: int = 120) -> str:
    try:
        with open(SCHEDULER_LOG) as f:
            lines = f.readlines()
        return ''.join(lines[-n:])
    except Exception:
        return ''

# ── Aria2c log watcher ─────────────────────────────────────────────────────────
# Polls showScheduler.log and extracts active torrent progress lines,
# writing them to .torrent_state.json so the UI can poll.

_PROGRESS_RE = re.compile(
    r'\[#([0-9a-f]+)\s+([^\]]+)\]'
)
_FILE_RE = re.compile(r'FILE:\s*(.+)')

def _watch_log():
    last_size = 0
    state: Dict = {}
    while True:
        try:
            size = os.path.getsize(SCHEDULER_LOG)
            if size != last_size:
                last_size = size
                raw = _tail_log(60)
                # Extract current download progress
                progress_lines = []
                current_file = ''
                for line in raw.splitlines():
                    fm = _FILE_RE.search(line)
                    if fm:
                        current_file = fm.group(1).strip()
                    pm = _PROGRESS_RE.search(line)
                    if pm:
                        progress_lines.append({'id': pm.group(1), 'info': pm.group(2), 'file': current_file})
                state = {
                    'updated': time.time(),
                    'progress': progress_lines[-1] if progress_lines else None,
                    'current_file': current_file,
                    'log_tail': raw,
                }
                with open(TORRENT_STATE, 'w') as f:
                    json.dump(state, f)
        except Exception:
            pass
        time.sleep(3)

# ── API routes ─────────────────────────────────────────────────────────────────

@app.route('/api/ping')
def ping():
    return jsonify({'ok': True})

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

@app.route('/api/movie', methods=['POST'])
@_require_auth
def request_movie():
    data = request.get_json(force=True)
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    is_anime = bool(data.get('anime'))
    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py'),
        '--movie', title,
    ]
    if is_anime:
        cmd.append('--anime-movie')
    threading.Thread(target=subprocess.run, args=(cmd,), daemon=True).start()
    return jsonify({'queued': title, 'anime': is_anime}), 202

@app.route('/api/torrent-state')
@_require_auth
def torrent_state():
    return jsonify(_read_torrent_state())

@app.route('/api/log')
@_require_auth
def get_log():
    n = min(int(request.args.get('lines', 80)), 300)
    return Response(_tail_log(n), mimetype='text/plain')

# ── Static UI ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'index.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(STATIC_DIR, 'favicon.svg', mimetype='image/svg+xml')

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Start log watcher background thread
    t = threading.Thread(target=_watch_log, daemon=True)
    t.start()

    kv = _load_kv(SECRETS_FILE)
    pw = kv.get('WEB_PASS', '(using hostname fallback)')
    print(f"  request server → http://localhost:{PORT}")
    print(f"  password (raw, set in secrets.md): {pw}")
    print(f"  token hash (sha256): {_web_password_hash()[:16]}...")
    app.run(host='0.0.0.0', port=PORT, debug=False)

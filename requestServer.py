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

import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s  %(message)s')
log = logging.getLogger(__name__)

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

# ── KHInsider download tracking ───────────────────────────────────────────────
# KHInsider downloads are HTTP-based (no aria2c).  Tracked separately.
_kh_downloads: Dict[str, Dict] = {}
_kh_lock = threading.Lock()
# Short-lived cache: album_url → (album_name, tracks) populated by /api/music-kh-tracks
# consumed by the download thread so we never fetch the same page twice.
_kh_track_cache: Dict[str, tuple] = {}
_kh_track_cache_lock = threading.Lock()

# ── Spotify / spotdl download tracking ────────────────────────────────────────
_spotdl_downloads: Dict[str, Dict] = {}
_spotdl_lock = threading.Lock()

def _spotdl_bin() -> str:
    """Return path to spotdl, preferring Homebrew Python 3.12 install."""
    for candidate in (
        '/opt/homebrew/bin/spotdl',                           # Python 3.12 install (preferred)
        os.path.expanduser('~/Library/Python/3.9/bin/spotdl'),  # old Python 3.9 install
        shutil.which('spotdl'),
        '/usr/local/bin/spotdl',
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    raise RuntimeError('spotdl not found — run: DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib /opt/homebrew/bin/pip3.12 install --break-system-packages spotdl')


def _spotdl_env() -> dict:
    """Build env for spotdl subprocess — ensures Homebrew expat is found on macOS."""
    env = os.environ.copy()
    # Homebrew Python 3.12 on macOS 26 needs this to load pyexpat / libexpat
    expat_lib = '/opt/homebrew/opt/expat/lib'
    if os.path.isdir(expat_lib):
        existing = env.get('DYLD_LIBRARY_PATH', '')
        env['DYLD_LIBRARY_PATH'] = (expat_lib + ':' + existing).rstrip(':')
    # Pass Spotify credentials if configured in secrets.md
    s = _load_kv(SECRETS_FILE)
    if s.get('SPOTIFY_CLIENT_ID'):
        env.setdefault('SPOTIPY_CLIENT_ID',     s['SPOTIFY_CLIENT_ID'])
        env.setdefault('SPOTIPY_CLIENT_SECRET', s.get('SPOTIFY_CLIENT_SECRET', ''))
    return env

# ── Per-instance aria2c management ─────────────────────────────────────────────
# Each download gets its own dedicated aria2c process on a unique port (6810–6910).
# The central monitor thread polls each instance and aggregates state for the UI.
# Architecture: site → requestServer (central) → per-download aria2c instances

_PORT_BASE = 6810   # instance port pool start
_PORT_MAX  = 6910   # instance port pool end (100 concurrent max)
_INST_KEYS = [
    'gid', 'status', 'totalLength', 'completedLength',
    'downloadSpeed', 'uploadSpeed', 'numSeeders', 'connections',
    'bittorrent',
]

# Registry: token → instance state dict
# token is 8 random hex chars used as the opaque gid in the UI
_instances: Dict[str, Dict]  = {}
_instances_lock = threading.Lock()

# Download concurrency cap — jobs beyond this wait in _pending_queue
MAX_ACTIVE_DL = 8
_pending_queue: list = []   # list of (name, magnet, dest) tuples, FIFO

def _aria2_secret() -> str:
    return _load_kv(SECRETS_FILE).get('ARIA2_SECRET', 'aria2rpc2026')

def _fmt_speed(bps: int) -> str:
    if bps >= 1_048_576: return f'{bps/1_048_576:.1f} MB/s'
    if bps >= 1024:      return f'{bps/1024:.0f} KB/s'
    return f'{bps} B/s'

def _fmt_size(b: int) -> str:
    if b >= 1_073_741_824: return f'{b/1_073_741_824:.2f} GB'
    if b >= 1_048_576:     return f'{b/1_048_576:.0f} MB'
    if b >= 1024:          return f'{b/1024:.0f} KB'
    return f'{b} B'

def _inst_rpc(port: int, method: str, params: Optional[List] = None) -> object:
    """JSON-RPC call to a specific aria2c instance. Socket always closed after call."""
    import http.client
    body = json.dumps({
        'jsonrpc': '2.0', 'id': 'c', 'method': method,
        'params': [f'token:{_aria2_secret()}'] + (params or []),
    }).encode()
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.request('POST', '/jsonrpc', body=body,
                     headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        if 'error' in data:
            raise RuntimeError(data['error'].get('message', 'aria2 error'))
        return data.get('result')
    finally:
        conn.close()

def _inst_port_open(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except Exception:
        return False

def _alloc_port() -> int:
    """Allocate next free port from pool. Must be called with _instances_lock held."""
    used = {inst['port'] for inst in _instances.values()}
    for p in range(_PORT_BASE, _PORT_MAX + 1):
        if p not in used:
            return p
    raise RuntimeError('no free aria2c ports available')

def _start_instance(name: str, magnet: str, dest: str, _token: Optional[str] = None) -> Dict:
    """
    Spawn a dedicated aria2c process for a single download.
    Returns the instance dict (token, port, live status fields).
    Raises RuntimeError if aria2c fails to start or the magnet cannot be added.
    Pass _token to reuse an existing queued token (preserves UI tracking).
    """
    aria2c_bin = (
        shutil.which('aria2c')
        or '/opt/homebrew/bin/aria2c'
        or '/usr/local/bin/aria2c'
    )
    if not aria2c_bin or not os.path.exists(aria2c_bin):
        raise RuntimeError('aria2c binary not found')

    with _instances_lock:
        port  = _alloc_port()
        if _token and _token not in _instances:
            token = _token
        else:
            token = os.urandom(4).hex()
            while token in _instances:
                token = os.urandom(4).hex()
        # Reserve slot so concurrent calls don't take the same port
        _instances[token] = {'token': token, 'port': port, 'status': 'starting', 'name': name}

    log_dir      = os.path.join('/tmp', f'aria2-{token}')
    session_path = os.path.join(log_dir, 'aria2.session')
    os.makedirs(log_dir, exist_ok=True)

    cmd = [
        aria2c_bin,
        '--enable-rpc=true', f'--rpc-listen-port={port}',
        f'--rpc-secret={_aria2_secret()}',
        '--rpc-allow-origin-all=false', '--rpc-listen-all=false',
        '--enable-color=false', '--seed-time=0',
        '--max-concurrent-downloads=1',
        '--file-allocation=none', '--allow-overwrite=true',
        '--auto-file-renaming=false',
        f'--log={os.path.join(log_dir, "aria2.log")}', '--log-level=warn',
        f'--save-session={session_path}', '--save-session-interval=30',
        '--continue=true',
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(10):
        time.sleep(0.5)
        if _inst_port_open(port):
            break
    else:
        proc.kill()
        with _instances_lock:
            _instances.pop(token, None)
        raise RuntimeError(f'aria2c port {port} not open after 5s')

    try:
        aria2_gid = _inst_rpc(port, 'aria2.addUri',
                               [[magnet], {'dir': dest, 'seed-time': '0',
                                           'file-allocation': 'falloc'}])
    except Exception as e:
        proc.kill()
        with _instances_lock:
            _instances.pop(token, None)
        raise RuntimeError(f'aria2.addUri failed: {e}')

    inst: Dict = {
        'token':        token,
        'port':         port,
        'name':         name,
        'magnet':       magnet,
        'dest':         dest,
        'proc':         proc,
        'log_dir':      log_dir,
        'aria2_gid':    str(aria2_gid or ''),
        # Live status updated by _instance_monitor
        'pct':          0,
        'speed':        '0 B/s',
        'speed_bytes':  0,
        'upload':       '0 B/s',
        'seeds':        0,
        'peers':        0,
        'status':       'active',
        'size':         '',
        'done':         '',
        'total_bytes':  0,
        'slow':         False,
        'started_at':   time.time(),
        'completed_at': 0.0,
    }
    with _instances_lock:
        _instances[token] = inst
    log.info('aria2c instance started  token=%s  port=%d  name=%r', token, port, name)
    return inst

def _enqueue_or_start(name: str, magnet: str, dest: str) -> Dict:
    """
    Start the download immediately if fewer than MAX_ACTIVE_DL instances are
    active; otherwise push it onto the pending queue and return a placeholder
    dict so callers always get a token back.
    """
    with _instances_lock:
        active_count = sum(
            1 for i in _instances.values()
            if i.get('status') not in ('complete', 'removed', 'error', 'queued')
        )

    if active_count < MAX_ACTIVE_DL:
        return _start_instance(name, magnet, dest)

    # Queue it — mint a token so the caller / UI can track it
    token = os.urandom(4).hex()
    while True:
        with _instances_lock:
            if token not in _instances:
                break
        token = os.urandom(4).hex()

    placeholder: Dict = {
        'token':        token,
        'port':         0,
        'name':         name,
        'magnet':       magnet,
        'dest':         dest,
        'proc':         None,
        'pct':          0,
        'speed':        '0 B/s',
        'speed_bytes':  0,
        'upload':       '0 B/s',
        'seeds':        0,
        'peers':        0,
        'status':       'queued',
        'size':         '',
        'done':         '',
        'total_bytes':  0,
        'slow':         False,
        'started_at':   time.time(),
        'completed_at': 0.0,
    }
    with _instances_lock:
        _instances[token] = placeholder
        _pending_queue.append(token)
    log.info('download queued (active=%d/%d)  token=%s  name=%r', active_count, MAX_ACTIVE_DL, token, name)
    return placeholder


def _stop_instance(token: str) -> None:
    """Remove instance from registry, shutdown its aria2c process, clean up tmp dir."""
    with _instances_lock:
        inst = _instances.pop(token, None)
        # Also remove from pending queue if it was queued
        try:
            _pending_queue.remove(token)
        except ValueError:
            pass
    if inst is None:
        return
    port    = inst.get('port', 0)
    proc    = inst.get('proc')
    log_dir = inst.get('log_dir', '')
    if port:
        try:
            _inst_rpc(port, 'aria2.shutdown')
        except Exception:
            pass
    if proc:
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
    if log_dir and os.path.isdir(log_dir):
        try:
            shutil.rmtree(log_dir, ignore_errors=True)
        except Exception:
            pass
    log.info('aria2c instance stopped  token=%s  port=%d', token, port)


def _drain_queue_bg() -> None:
    """Spawn a background thread to immediately promote the next queued download."""
    def _try() -> None:
        with _instances_lock:
            active_count = sum(
                1 for i in _instances.values()
                if i.get('status') not in ('complete', 'removed', 'error', 'queued')
            )
            if active_count >= MAX_ACTIVE_DL or not _pending_queue:
                return
            next_token = _pending_queue.pop(0)
            queued_inst = _instances.get(next_token)
        if not queued_inst:
            return
        name       = queued_inst['name']
        magnet     = queued_inst['magnet']
        dest       = queued_inst['dest']
        started_at = queued_inst.get('started_at', time.time())
        with _instances_lock:
            _instances.pop(next_token, None)
        try:
            _start_instance(name, magnet, dest, _token=next_token)
            log.info('queued download promoted (on-demand)  token=%s  name=%r', next_token, name)
        except Exception as e:
            log.warning('failed to start queued (on-demand) %r: %s — re-queuing', name, e)
            with _instances_lock:
                _instances[next_token] = {
                    'token': next_token, 'port': 0, 'name': name,
                    'magnet': magnet, 'dest': dest, 'proc': None,
                    'pct': 0, 'speed': '0 B/s', 'speed_bytes': 0,
                    'upload': '0 B/s', 'seeds': 0, 'peers': 0,
                    'status': 'queued', 'size': '', 'done': '',
                    'total_bytes': 0, 'slow': False,
                    'started_at': started_at, 'completed_at': 0.0,
                }
                _pending_queue.insert(0, next_token)  # restore at front
    threading.Thread(target=_try, daemon=True).start()


def _instance_monitor() -> None:
    """
    Background thread: polls every active aria2c instance every 3s,
    updates the state cache, and auto-cleans completed instances after
    a 5-minute display window. No RPC calls block the Flask request path.
    """
    while True:
        time.sleep(3)
        with _instances_lock:
            snapshot = [(t, dict(i)) for t, i in _instances.items()
                        if i.get('status') != 'starting']
        slow_tokens: set = set()
        try:
            with open(os.path.join(SCRIPT_DIR, 'slow_gids.json')) as _sf:
                slow_tokens = set(json.load(_sf))
        except Exception:
            pass

        for token, inst in snapshot:
            port = inst['port']
            proc = inst.get('proc')
            if proc and proc.poll() is not None:
                with _instances_lock:
                    if token in _instances and _instances[token]['status'] not in ('complete', 'removed'):
                        _instances[token]['status'] = 'error'
                continue
            try:
                active = _inst_rpc(port, 'aria2.tellActive',  [_INST_KEYS]) or []
                items  = active or (_inst_rpc(port, 'aria2.tellWaiting', [0, 1, _INST_KEYS]) or [])
                if not items:
                    stopped = _inst_rpc(port, 'aria2.tellStopped', [0, 1, _INST_KEYS]) or []
                    if stopped and stopped[0].get('status') == 'complete':
                        with _instances_lock:
                            if token in _instances:
                                _instances[token]['pct']    = 100
                                _instances[token]['status'] = 'complete'
                                if not _instances[token]['completed_at']:
                                    _instances[token]['completed_at'] = time.time()
                        completed_at = _instances.get(token, {}).get('completed_at', 0.0)
                        if completed_at and time.time() - completed_at > 300:
                            log.info('instance %s complete, cleaning up', token)
                            _stop_instance(token)
                    continue

                item    = items[0]
                total   = int(item.get('totalLength')     or 0)
                done    = int(item.get('completedLength') or 0)
                pct     = int(done * 100 / total) if total > 0 else 0
                speed_b = int(item.get('downloadSpeed')   or 0)
                up_b    = int(item.get('uploadSpeed')     or 0)
                seeds   = int(item.get('numSeeders')      or 0)
                peers   = int(item.get('connections')     or 0)
                status  = item.get('status', 'active')
                bt_name = ((item.get('bittorrent') or {}).get('info') or {}).get('name', '')
                with _instances_lock:
                    if token not in _instances:
                        continue
                    i = _instances[token]
                    i['pct']         = pct
                    i['speed']       = _fmt_speed(speed_b)
                    i['speed_bytes'] = speed_b
                    i['upload']      = _fmt_speed(up_b)
                    i['seeds']       = seeds
                    i['peers']       = peers
                    i['status']      = status
                    i['size']        = _fmt_size(total)
                    i['done']        = _fmt_size(done)
                    i['total_bytes'] = total
                    i['slow']        = token in slow_tokens
                    if bt_name and not bt_name.startswith('[METADATA]'):
                        i['name']    = bt_name
            except Exception:
                pass  # transient RPC error — try again next cycle

        # ── Drain pending queue whenever slots open up ────────────────────
        while True:
            with _instances_lock:
                active_count = sum(
                    1 for i in _instances.values()
                    if i.get('status') not in ('complete', 'removed', 'error', 'queued')
                )
                if active_count >= MAX_ACTIVE_DL or not _pending_queue:
                    break
                next_token = _pending_queue.pop(0)
                queued_inst = _instances.get(next_token)

            if not queued_inst:
                continue

            name       = queued_inst['name']
            magnet     = queued_inst['magnet']
            dest       = queued_inst['dest']
            started_at = queued_inst.get('started_at', time.time())

            # Remove placeholder; _start_instance will re-register under the same token
            with _instances_lock:
                _instances.pop(next_token, None)

            try:
                _start_instance(name, magnet, dest, _token=next_token)
                log.info('queued download started  token=%s  name=%r  active=%d/%d',
                         next_token, name, active_count + 1, MAX_ACTIVE_DL)
            except Exception as e:
                log.warning('failed to start queued download %r: %s — re-queuing', name, e)
                # Restore the item so it can be retried next cycle
                with _instances_lock:
                    _instances[next_token] = {
                        'token': next_token, 'port': 0, 'name': name,
                        'magnet': magnet, 'dest': dest, 'proc': None,
                        'pct': 0, 'speed': '0 B/s', 'speed_bytes': 0,
                        'upload': '0 B/s', 'seeds': 0, 'peers': 0,
                        'status': 'queued', 'size': '', 'done': '',
                        'total_bytes': 0, 'slow': False,
                        'started_at': started_at, 'completed_at': 0.0,
                    }
                    _pending_queue.append(next_token)
                break  # stop draining this cycle; retry next monitor tick

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
        'search_name':    re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', data['search_name'])).strip().lower(),
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

@app.route('/api/show-download', methods=['POST'])
@_require_auth
def show_download_direct():
    """Download all episodes of a finished/older show without permanently adding to schedule.
    Adds a temporary CSV row, runs the full episode backlog, then removes it when done."""
    data = request.get_json(force=True)
    show_name = (data.get('show_name') or '').strip()
    if not show_name:
        return jsonify({'error': 'show_name required'}), 400
    search_name = re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', (data.get('search_name') or show_name))).strip().lower()
    show_type   = (data.get('type') or 'live').strip()
    season      = int(data.get('season') or 1)
    total       = int(data.get('total_episodes') or 99)

    # Add a temporary row so the scheduler script can find it
    rows = read_schedule()
    if not any(r['show_name'] == show_name for r in rows):
        rows.append({
            'show_name':      show_name,
            'search_name':    search_name,
            'folder':         show_name,
            'type':           show_type,
            'season':         str(season),
            'next_episode':   '1',
            'total_episodes': str(total),
            'release_days':   'daily',
            'status':         'pending',
            'search_start':   '', 'search_end': '', 'last_check': '',
            'week_anchor':    '', 'anchor_episode': '',
        })
        write_schedule(rows)

    search_script = os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py')

    def _run_and_cleanup():
        for _ in range(total):
            subprocess.run(
                [sys.executable, search_script, '--show', show_name, '--force'],
                capture_output=True,
            )
            time.sleep(5)
        # Remove the temporary schedule entry when the download loop finishes
        try:
            remaining = [r for r in read_schedule() if r['show_name'] != show_name]
            write_schedule(remaining)
        except Exception:
            pass

    threading.Thread(target=_run_and_cleanup, daemon=True).start()
    return jsonify({'queued': show_name}), 202


@app.route('/api/show-scan', methods=['POST'])
@_require_auth
def show_scan():
    """Scan for season packs + individual episodes without downloading anything.
    Returns structured JSON for the UI preview screen."""
    data = request.get_json(force=True)
    search_name = re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', (data.get('search_name') or ''))).strip().lower()
    show_type = (data.get('type') or 'live').strip()
    seasons = data.get('seasons') or []
    if not search_name or not seasons:
        return jsonify({'error': 'search_name and seasons required'}), 400

    seasons_arg = ','.join(f"{s['n']}:{s.get('episodes', 24)}" for s in seasons)
    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py'),
        '--scan-seasons',
        '--show-search', search_name,
        '--scan-type', show_type,
        '--seasons-arg', seasons_arg,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        for line in reversed(proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                return jsonify(json.loads(line))
        return jsonify({'seasons': [], 'error': 'no results from scanner'})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'scan timed out', 'seasons': []}), 504
    except Exception as e:
        return jsonify({'error': str(e), 'seasons': []}), 500


@app.route('/api/show-download-batch', methods=['POST'])
@_require_auth
def show_download_batch():
    """Start a dedicated aria2c instance per torrent for immediate batch download."""
    data      = request.get_json(force=True)
    show_name = (data.get('show_name') or '').strip()
    items     = data.get('items') or []
    if not show_name or not items:
        return jsonify({'error': 'show_name and items required'}), 400

    locs      = _load_kv(LOCATIONS_FILE)
    shows_dir = locs.get('SHOWS_DIR', '/Volumes/Jellyfin/Shows')
    dest      = os.path.join(shows_dir, show_name)

    started = []
    for item in items:
        magnet = (item.get('magnet') or '').strip()
        label  = (item.get('label')  or show_name).strip()
        if not magnet:
            continue
        try:
            inst = _enqueue_or_start(label, magnet, dest)
            started.append({'gid': inst['token'], 'label': label})
        except Exception as e:
            log.warning('show_download_batch: failed to start %r: %s', label, e)

    if not started:
        return jsonify({'error': 'no torrents started — aria2c may be unavailable'}), 500

    return jsonify({'gids': started, 'count': len(started)}), 200


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


@app.route('/api/library-check')
@_require_auth
def library_check():
    """Fuzzy-match a title against existing Movies + Shows folder names."""
    q = (request.args.get('q') or '').strip().lower()
    if not q or len(q) < 2:
        return jsonify({'matches': []})
    locs = _load_kv(LOCATIONS_FILE)
    dirs = [
        ('movie', locs.get('MOVIES_DIR', '/Volumes/Jellyfin/Movies')),
        ('show',  locs.get('SHOWS_DIR',  '/Volumes/Jellyfin/Shows')),
    ]
    # Separate plain numbers (years, sequels) from word tokens
    q_numbers = re.findall(r'\b\d+\b', q)
    q_words   = {t for t in re.sub(r'\b\d+\b', '', q).split() if len(t) >= 2}
    _VIDEO_EXT = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.ts', '.mpg', '.mpeg', '.webm'}
    matches = []
    for kind, d in dirs:
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith('.'):
                continue
            # Skip non-video files (images, metadata, trickplay, subtitles, etc.)
            if not entry.is_dir():
                if os.path.splitext(name)[1].lower() not in _VIDEO_EXT:
                    continue
            nl = name.lower()
            nl_clean = re.sub(r'\s*\(\d{4}\)', '', nl)
            # Strip extension for file-based matching
            nl_clean = re.sub(r'\.\w{2,4}$', '', nl_clean)
            word_hits = sum(1 for t in q_words if t in nl_clean) if q_words else 0
            word_match = (not q_words) or word_hits >= max(1, len(q_words) - 1)
            num_match = all(re.search(r'\b' + re.escape(n) + r'\b', nl) for n in q_numbers)
            if word_match and num_match:
                # Show clean name without extension
                display = re.sub(r'\.\w{2,4}$', '', name) if not entry.is_dir() else name
                matches.append({'name': display, 'kind': kind})
    matches.sort(key=lambda m: (0 if q in m['name'].lower() else 1, m['name']))
    return jsonify({'matches': matches[:20]})


@app.route('/api/movie-suggest')
@_require_auth
def movie_suggest():
    """Movie title autocomplete using OMDb search API (free key, 1000 req/day)."""
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2:
        return jsonify({'results': []})
    try:
        import urllib.request as _ur
        import urllib.parse as _up
        # OMDb free key — enough for personal use (1000 req/day)
        # User can override via OMDB_KEY env var
        omdb_key = os.environ.get('OMDB_KEY', 'trilogy')
        url = ('https://www.omdbapi.com/?type=movie&s='
               + _up.quote(q) + '&apikey=' + _up.quote(omdb_key))
        req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        results = []
        seen: set = set()
        for item in (data.get('Search') or []):
            title = (item.get('Title') or '').strip()
            year_raw = (item.get('Year') or '')[:4]
            year = int(year_raw) if year_raw.isdigit() else None
            if not title:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            results.append({'title': title, 'year': year})
        return jsonify({'results': results})
    except Exception as e:
        log.warning('movie-suggest error: %s', e)
        return jsonify({'results': []})


@app.route('/api/show-suggest')
@_require_auth
def show_suggest():
    """TV show search via TVmaze (live-action/anime) + AniList GraphQL (anime)."""
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2:
        return jsonify({'results': []})
    import urllib.request as _ur
    import urllib.parse as _up
    results = []

    # ── TVmaze ────────────────────────────────────────────────────────────────
    try:
        url = 'https://api.tvmaze.com/search/shows?q=' + _up.quote(q)
        req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _ur.urlopen(req, timeout=8) as resp:
            items = json.loads(resp.read())
        for entry in (items or [])[:6]:
            show = entry.get('show') or {}
            name = (show.get('name') or '').strip()
            if not name:
                continue
            year_raw = show.get('premiered') or show.get('airdate') or ''
            year = int(year_raw[:4]) if year_raw and len(year_raw) >= 4 else None
            genres = [g.lower() for g in (show.get('genres') or [])]
            show_type = 'anime' if 'anime' in genres else 'live'
            poster = (show.get('image') or {}).get('medium') or ''
            network = ((show.get('network') or {}).get('name') or
                       (show.get('webChannel') or {}).get('name') or '')
            results.append({
                'title': name,
                'year': year,
                'type': show_type,
                'poster': poster,
                'network': network,
                'source': 'tvmaze',
                'source_id': str(show.get('id', '')),
            })
    except Exception as e:
        log.warning('show-suggest tvmaze error: %s', e)

    # ── AniList ───────────────────────────────────────────────────────────────
    try:
        gql = ('query ($q: String) { Page(perPage: 5) { media(search: $q, type: ANIME, '
               'sort: SEARCH_MATCH) { id title { romaji english } seasonYear '
               'coverImage { medium } episodes status } } }')
        body = json.dumps({'query': gql, 'variables': {'q': q}}).encode()
        req = _ur.Request('https://graphql.anilist.co', data=body,
                          headers={'Content-Type': 'application/json',
                                   'User-Agent': 'Mozilla/5.0'})
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        for media in ((data.get('data') or {}).get('Page', {}).get('media') or []):
            title = ((media.get('title') or {}).get('english') or
                     (media.get('title') or {}).get('romaji') or '').strip()
            if not title:
                continue
            poster = (media.get('coverImage') or {}).get('medium') or ''
            results.append({
                'title': title,
                'year': media.get('seasonYear'),
                'type': 'anime',
                'poster': poster,
                'network': 'AniList',
                'source': 'anilist',
                'source_id': str(media.get('id', '')),
            })
    except Exception as e:
        log.warning('show-suggest anilist error: %s', e)

    # Deduplicate by title (case-insensitive); AniList results appended last so
    # TVmaze takes precedence for live-action, AniList for pure anime
    seen_titles: set = set()
    deduped = []
    for r in results:
        key = r['title'].lower()
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append(r)
    return jsonify({'results': deduped[:10]})


@app.route('/api/show-seasons')
@_require_auth
def show_seasons():
    """Return seasons list for a show by source (tvmaze/anilist) and id."""
    source = (request.args.get('source') or '').strip()
    sid    = (request.args.get('id') or '').strip()
    if not source or not sid:
        return jsonify({'seasons': []})
    import urllib.request as _ur

    if source == 'tvmaze':
        if not sid.isdigit():
            return jsonify({'seasons': []}), 400
        try:
            url = f'https://api.tvmaze.com/shows/{sid}/seasons'
            req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with _ur.urlopen(req, timeout=8) as resp:
                items = json.loads(resp.read())
            seasons = []
            for s in (items or []):
                n  = s.get('number')
                ep = s.get('episodeOrder') or s.get('numberOfEpisodes') or 0
                if n and n > 0:
                    upcoming = not s.get('premiereDate')  # announced but not yet airing
                    seasons.append({'n': n, 'episodes': ep or 99, 'upcoming': upcoming})
            return jsonify({'seasons': seasons})
        except Exception as e:
            log.warning('show-seasons tvmaze error: %s', e)
            return jsonify({'seasons': []})

    elif source == 'anilist':
        try:
            gql = 'query ($id: Int) { Media(id: $id, type: ANIME) { episodes } }'
            body = json.dumps({'query': gql, 'variables': {'id': int(sid)}}).encode()
            req = _ur.Request('https://graphql.anilist.co', data=body,
                              headers={'Content-Type': 'application/json',
                                       'User-Agent': 'Mozilla/5.0'})
            with _ur.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            episodes = ((data.get('data') or {}).get('Media') or {}).get('episodes') or 99
            return jsonify({'seasons': [{'n': 1, 'episodes': episodes}]})
        except Exception as e:
            log.warning('show-seasons anilist error: %s', e)
            return jsonify({'seasons': []})

    return jsonify({'seasons': []})


@app.route('/api/movie-candidates')
@_require_auth
def movie_candidates():
    """Search for movie candidates synchronously and return scored list (no download)."""
    title = re.sub(r'[^\w\s]', '', (request.args.get('title') or '').strip())
    title = re.sub(r'\s+', ' ', title).strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    is_anime  = request.args.get('anime', '0') not in ('0', 'false', '')
    prefer_4k = request.args.get('quality', '1080').lower() in ('4k', '2160p', '4k2160')
    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py'),
        '--movie', title,
        '--list-candidates',
    ]
    if is_anime:  cmd.append('--anime-movie')
    if prefer_4k: cmd.append('--prefer-4k')
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=50)
        # last non-empty line that starts with '[' is the JSON
        for line in reversed(proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith('['):
                candidates = json.loads(line)
                return jsonify({'candidates': candidates})
        return jsonify({'candidates': []})
    except Exception as e:
        return jsonify({'error': str(e), 'candidates': []}), 500


@app.route('/api/movie', methods=['POST'])
@_require_auth
def request_movie():
    data = request.get_json(force=True)
    title = re.sub(r'[^\w\s]', '', (data.get('title') or '').strip())
    title = re.sub(r'\s+', ' ', title).strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    is_anime = bool(data.get('anime'))
    quality = (data.get('quality') or '1080').lower()
    prefer_4k = quality in ('4k', '2160p', '4k2160', '4k/2160p')
    magnet = (data.get('magnet') or '').strip()
    if magnet:
        # User picked a specific candidate — download it directly via env var
        env = dict(os.environ, MOVIE_DIRECT_MAGNET=magnet)
        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py'),
            '--movie', title,
        ]
        if is_anime:  cmd.append('--anime-movie')
        if prefer_4k: cmd.append('--prefer-4k')
        threading.Thread(target=subprocess.run, args=(cmd,),
                         kwargs={'env': env}, daemon=True).start()
    else:
        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, 'showSchedulerSearch.py'),
            '--movie', title,
        ]
        if is_anime:  cmd.append('--anime-movie')
        if prefer_4k: cmd.append('--prefer-4k')
        threading.Thread(target=subprocess.run, args=(cmd,), daemon=True).start()
    return jsonify({'queued': title, 'anime': is_anime, 'quality': quality}), 202

@app.route('/api/add-torrent', methods=['POST'])
@_require_auth
def add_torrent():
    """Create a dedicated aria2c instance for a single torrent."""
    data   = request.get_json(force=True) or {}
    name   = (data.get('name')   or '').strip()
    magnet = (data.get('magnet') or '').strip()
    dest   = (data.get('dest')   or '').strip()
    if not magnet or not dest:
        return jsonify({'error': 'magnet and dest required'}), 400
    if not name:
        name = os.path.basename(dest.rstrip('/')) or magnet[:60]
    try:
        inst = _enqueue_or_start(name, magnet, dest)
        return jsonify({'token': inst['token'], 'port': inst['port'], 'status': inst['status']})
    except Exception as e:
        log.exception('add-torrent failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/torrent-state')
@_require_auth
def torrent_state():
    with _instances_lock:
        snapshot = [dict(i) for i in _instances.values()
                    if i.get('status') != 'starting']
    downloads = [{
        'gid':         inst['token'],
        'name':        inst.get('name', '(unknown)'),
        'pct':         inst.get('pct', 0),
        'speed':       inst.get('speed', '0 B/s'),
        'speed_bytes': inst.get('speed_bytes', 0),
        'upload':      inst.get('upload', '0 B/s'),
        'seeds':       inst.get('seeds', 0),
        'peers':       inst.get('peers', 0),
        'status':      inst.get('status', 'active'),
        'size':        inst.get('size', ''),
        'done':        inst.get('done', ''),
        'total_bytes': inst.get('total_bytes', 0),
        'slow':        inst.get('slow', False),
        'dest':        inst.get('dest', ''),
        'magnet':      inst.get('magnet', ''),
        'started_at':  inst.get('started_at', 0),
    } for inst in snapshot]
    with _instances_lock:
        queue_len = len(_pending_queue)
        queue_positions = {tok: idx + 1 for idx, tok in enumerate(_pending_queue)}
    for dl in downloads:
        if dl['status'] == 'queued':
            dl['queue_pos'] = queue_positions.get(dl['gid'], 0)
    return jsonify({'downloads': downloads, 'updated': time.time(), 'queued': queue_len, 'max_active': MAX_ACTIVE_DL})

@app.route('/api/torrent/<token>/remove', methods=['POST'])
@_require_auth
def torrent_remove(token: str):
    if not re.fullmatch(r'[0-9a-f]{4,16}', token):
        abort(400)
    with _instances_lock:
        inst = _instances.get(token)
    if inst is None:
        return jsonify({'error': 'not found'}), 404

    port      = inst['port']
    aria2_gid = inst.get('aria2_gid', '')
    dest      = inst.get('dest', '')

    # Collect file list before removing from aria2
    file_paths: list = []
    base_dir = dest
    if aria2_gid and port:
        try:
            info = _inst_rpc(port, 'aria2.tellStatus',
                             [aria2_gid, ['files', 'dir', 'infoHash']]) or {}
            base_dir = (info.get('dir') or dest).rstrip('/')
            for f in (info.get('files') or []):
                p = (f.get('path') or '').strip()
                if p and p != '[METADATA]':
                    file_paths.append(p)
        except Exception:
            pass

    # Stop instance (removes from registry, kills process, cleans tmp dir)
    _stop_instance(token)
    # Immediately try to promote the next queued download into the freed slot
    _drain_queue_bg()

    # Delete downloaded files
    to_delete: set = set()
    for p in file_paths:
        if base_dir and p.startswith(base_dir + '/'):
            top = p[len(base_dir) + 1:].split('/')[0]
            to_delete.add(os.path.join(base_dir, top))
        else:
            to_delete.add(p)
    if not to_delete and dest:
        to_delete.add(dest)

    for target in to_delete:
        try:
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
                log.info('torrent_remove: deleted dir %s', target)
            elif os.path.isfile(target):
                os.remove(target)
                log.info('torrent_remove: deleted file %s', target)
        except Exception as _de:
            log.warning('torrent_remove: could not delete %s: %s', target, _de)

    return jsonify({'removed': token})

@app.route('/api/torrent/<token>/pause', methods=['POST'])
@_require_auth
def torrent_pause(token: str):
    if not re.fullmatch(r'[0-9a-f]{4,16}', token):
        abort(400)
    with _instances_lock:
        inst = _instances.get(token)
    if inst is None:
        return jsonify({'error': 'not found'}), 404
    try:
        _inst_rpc(inst['port'], 'aria2.pause', [inst['aria2_gid']])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'paused': token})

@app.route('/api/torrent/<token>/unpause', methods=['POST'])
@_require_auth
def torrent_unpause(token: str):
    if not re.fullmatch(r'[0-9a-f]{4,16}', token):
        abort(400)
    with _instances_lock:
        inst = _instances.get(token)
    if inst is None:
        return jsonify({'error': 'not found'}), 404
    try:
        _inst_rpc(inst['port'], 'aria2.unpause', [inst['aria2_gid']])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'unpaused': token})

@app.route('/api/log')
@_require_auth
def get_log():
    n = min(int(request.args.get('lines', 80)), 300)
    return Response(_tail_log(n), mimetype='text/plain')


# ── Music API ──────────────────────────────────────────────────────────────────

@app.route('/api/music-artist-albums')
@_require_auth
def music_artist_albums():
    """Fetch albums for a specific artist from MusicBrainz. Used by the album picker step."""
    artist = (request.args.get('artist') or '').strip()
    if not artist or len(artist) < 2:
        return jsonify({'results': []})
    try:
        from musicSearch import get_artist_albums_mb
        return jsonify({'results': get_artist_albums_mb(artist)})
    except Exception as e:
        log.warning('music-artist-albums error: %s', e)
        return jsonify({'results': []})


@app.route('/api/music-suggest')
@_require_auth
def music_suggest():
    """MusicBrainz artist search for mainstream music autocomplete."""
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2:
        return jsonify({'results': []})
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from musicSearch import suggest_artist_mb
        return jsonify({'results': suggest_artist_mb(q)})
    except Exception as e:
        log.warning('music-suggest error: %s', e)
        return jsonify({'results': []})


@app.route('/api/music-kh-search')
@_require_auth
def music_kh_search():
    """Search KHInsider for soundtrack albums."""
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2:
        return jsonify({'results': []})
    try:
        from musicSearch import search_khinsider
        return jsonify({'results': search_khinsider(q)})
    except Exception as e:
        log.warning('music-kh-search error: %s', e)
        return jsonify({'results': []})


@app.route('/api/show-search-preview')
@_require_auth
def show_search_preview():
    """Quick Nyaa search to preview what a short search name returns. Used by the add-show wizard."""
    q = (request.args.get('q') or '').strip()
    if not q or len(q) > 20:
        return jsonify({'error': 'invalid query'}), 400
    try:
        from showSchedulerSearch import search_nyaa_bare_ep, _parse_nyaa_html, _fetch
        import urllib.parse
        url = (
            f"https://nyaa.si/?f=0&c=1_2"
            f"&q={urllib.parse.quote(q)}&s=seeders&o=desc"
        )
        body = _fetch(url)
        results = _parse_nyaa_html(body) if body else []
        return jsonify({'results': [
            {'title': r['title'], 'seeds': r['seeds']}
            for r in results[:10]
        ]})
    except Exception as e:
        log.exception('show-search-preview error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/music-kh-tracks')
@_require_auth
def music_kh_tracks():
    """Fetch track list from a KHInsider album URL."""
    url = (request.args.get('url') or '').strip()
    if not url or not url.startswith('https://downloads.khinsider.com/'):
        return jsonify({'error': 'invalid url'}), 400
    try:
        from musicSearch import fetch_khinsider_tracks
        album_name, tracks = fetch_khinsider_tracks(url)
        # Cache for the imminent download request
        with _kh_track_cache_lock:
            _kh_track_cache[url] = (album_name, tracks)
        if not tracks:
            return jsonify({'error': 'No tracks found — the album page may have changed.', 'count': 0, 'tracks': []})
        return jsonify({
            'album':  album_name,
            'tracks': [t['title'] for t in tracks],
            'count':  len(tracks),
        })
    except Exception as e:
        log.exception('music-kh-tracks error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/music-tpb-search')
@_require_auth
def music_tpb_search():
    """Search TPB for music torrents. niche=1 fires multiple query variations."""
    q     = (request.args.get('q')     or '').strip()
    niche = request.args.get('niche', '0') == '1'
    if not q or len(q) < 2:
        return jsonify({'results': []})
    try:
        from musicSearch import search_tpb_music
        return jsonify({'results': search_tpb_music(q, niche=niche)})
    except Exception as e:
        log.warning('music-tpb-search error: %s', e)
        return jsonify({'results': []})


@app.route('/api/music', methods=['POST'])
@_require_auth
def request_music():
    """
    Submit a music download.
    Body: {type, query, magnet?, kh_url?}
      kh_url   → KHInsider HTTP download (tracked in _kh_downloads)
      magnet   → aria2c torrent (tracked in _instances, same as movies)
    """
    data     = request.get_json(force=True) or {}
    mtype    = (data.get('type')    or '').strip()
    query    = (data.get('query')   or '').strip()
    magnet   = (data.get('magnet')  or '').strip()
    kh_url   = (data.get('kh_url') or '').strip()

    locs      = _load_kv(LOCATIONS_FILE)
    music_dir = locs.get('MUSIC_DIR', '/Volumes/Jellyfin/Music')

    if kh_url:
        # Validate URL is actually KHInsider to prevent SSRF
        if not re.match(r'^https://downloads\.khinsider\.com/game-soundtracks/album/[\w\-]+/?$', kh_url):
            return jsonify({'error': 'invalid kh_url'}), 400
        folder_name = re.sub(r'[^\w\s\-]', '_', query or kh_url.rstrip('/').split('/')[-1])[:80].strip()
        dest        = os.path.join(music_dir, folder_name)

        stop_ev = threading.Event()
        token   = os.urandom(4).hex()
        state: Dict = {
            'token':        token,
            'name':         query or folder_name,
            'type':         'kh',
            'status':       'starting',
            'total':        0,
            'done':         0,
            'failed':       0,
            'current':      '',
            'dest':         dest,
            'kh_url':       kh_url,
            '_stop':        stop_ev,
            'started_at':   time.time(),
            'completed_at': 0.0,
        }
        with _kh_lock:
            _kh_downloads[token] = state

        def _run_kh():
            try:
                from musicSearch import download_khinsider_album
                # Use cached track list from preview fetch if available (avoids 403 re-fetch)
                with _kh_track_cache_lock:
                    cached = _kh_track_cache.pop(kh_url, None)
                download_khinsider_album(kh_url, dest, state, stop_ev, cached_tracks=cached)
            except Exception as e:
                state['status'] = 'error'
                state['error']  = str(e)
            if state.get('status') in ('complete', 'complete_partial'):
                state['completed_at'] = time.time()
            log.info('KH download %s finished: status=%s done=%s/%s',
                     token, state['status'], state.get('done'), state.get('total'))

        threading.Thread(target=_run_kh, daemon=True).start()
        return jsonify({'token': token, 'type': 'kh'}), 202

    elif magnet:
        try:
            # Download directly into music_dir — torrent's own folder structure
            # organises the content. Avoids makedirs on the network volume.
            inst = _enqueue_or_start(query or 'Music', magnet, music_dir)
            return jsonify({'token': inst['token'], 'type': 'torrent', 'status': inst['status']}), 202
        except Exception as e:
            log.exception('music torrent start failed')
            return jsonify({'error': str(e)}), 500

    else:
        return jsonify({'error': 'magnet or kh_url required'}), 400


@app.route('/api/music-state')
@_require_auth
def music_state():
    """KHInsider download progress for all active/recent downloads."""
    with _kh_lock:
        now      = time.time()
        snapshot = []
        to_clean = []
        for token, st in _kh_downloads.items():
            completed_at = st.get('completed_at', 0.0)
            if completed_at and now - completed_at > 300:
                to_clean.append(token)
                continue
            snapshot.append({k: v for k, v in st.items() if k != '_stop'})
        for t in to_clean:
            _kh_downloads.pop(t, None)
    return jsonify({'downloads': snapshot})


@app.route('/api/music/<token>/remove', methods=['POST'])
@_require_auth
def music_remove(token: str):
    if not re.fullmatch(r'[0-9a-f]{4,16}', token):
        abort(400)
    with _kh_lock:
        st = _kh_downloads.get(token)
    if st is None:
        return jsonify({'error': 'not found'}), 404
    stop_ev = st.get('_stop')
    if stop_ev:
        stop_ev.set()
    with _kh_lock:
        _kh_downloads.pop(token, None)
    return jsonify({'removed': token})


@app.route('/api/spotify', methods=['POST'])
@_require_auth
def spotify_download():
    """
    Start a spotdl download.
    Body: {url: 'https://open.spotify.com/album/...', query: 'optional label'}
    spotdl resolves each track via YouTube Music and downloads MP3s.
    """
    data      = request.get_json(force=True) or {}
    url       = (data.get('url') or '').strip()
    query     = (data.get('query') or '').strip()

    # Validate: must be a Spotify URL for track/album/playlist/artist
    if not re.match(
        r'^https://open\.spotify\.com/(track|album|playlist|artist)/[A-Za-z0-9]+',
        url
    ):
        return jsonify({'error': 'Invalid Spotify URL'}), 400

    locs      = _load_kv(LOCATIONS_FILE)
    music_dir = locs.get('MUSIC_DIR', '/Volumes/Jellyfin/Music')

    try:
        binary = _spotdl_bin()
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500

    token = os.urandom(4).hex()
    state: Dict = {
        'token':        token,
        'name':         query or url.split('/')[-1],
        'url':          url,
        'status':       'starting',
        'lines':        [],     # last N stdout lines for progress display
        'done':         0,
        'total':        0,
        'dest':         music_dir,
        'started_at':   time.time(),
        'completed_at': 0.0,
    }
    with _spotdl_lock:
        _spotdl_downloads[token] = state

    def _run():
        try:
            cmd = [binary, 'download', url, '--output', music_dir, '--audio', 'piped']
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env=_spotdl_env(),
            )
            state['status'] = 'active'
            state['pid']    = proc.pid
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                state['lines'] = (state['lines'] + [line])[-40:]
                # spotdl prints "Downloaded N/M songs" or "Downloaded "Title""
                m = re.search(r'Downloaded\s+(\d+)/(\d+)', line)
                if m:
                    state['done']  = int(m.group(1))
                    state['total'] = int(m.group(2))
                elif re.search(r'Downloaded\s+"', line):
                    state['done'] = state.get('done', 0) + 1
                log.info('[spotdl:%s] %s', token, line)
            proc.wait()
            state['status']       = 'complete' if proc.returncode == 0 else 'error'
            state['completed_at'] = time.time()
            if proc.returncode != 0:
                state['error'] = f'spotdl exited {proc.returncode}'
        except Exception as e:
            state['status']       = 'error'
            state['error']        = str(e)
            state['completed_at'] = time.time()
            log.exception('spotdl %s failed', token)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'token': token}), 202


@app.route('/api/spotify-state')
@_require_auth
def spotify_state():
    """Progress for all active/recent spotdl downloads."""
    with _spotdl_lock:
        now      = time.time()
        snapshot = []
        to_clean = []
        for token, st in _spotdl_downloads.items():
            completed_at = st.get('completed_at', 0.0)
            if completed_at and now - completed_at > 300:
                to_clean.append(token)
                continue
            snapshot.append({k: v for k, v in st.items() if k not in ('pid',)})
        for t in to_clean:
            _spotdl_downloads.pop(t, None)
    return jsonify({'downloads': snapshot})


@app.route('/api/spotify/<token>/remove', methods=['POST'])
@_require_auth
def spotify_remove(token: str):
    if not re.fullmatch(r'[0-9a-f]{4,16}', token):
        abort(400)
    with _spotdl_lock:
        st = _spotdl_downloads.pop(token, None)
    if st is None:
        return jsonify({'error': 'not found'}), 404
    pid = st.get('pid')
    if pid:
        try:
            os.kill(pid, 15)  # SIGTERM
        except Exception:
            pass
    return jsonify({'removed': token})


# ── Error log endpoint ─────────────────────────────────────────────────────────

_error_log: List[Dict] = []   # in-memory ring buffer, max 200 entries
_error_log_lock = threading.Lock()

class _ErrorCapture(logging.Handler):
    """Captures ERROR+ log records into the in-memory ring buffer."""
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        entry = {
            'ts':      record.created,
            'level':   record.levelname,
            'msg':     self.format(record),
        }
        with _error_log_lock:
            _error_log.append(entry)
            if len(_error_log) > 200:
                del _error_log[0]

_capture_handler = _ErrorCapture()
_capture_handler.setFormatter(logging.Formatter('%(message)s'))
logging.getLogger().addHandler(_capture_handler)


@app.route('/api/errors')
@_require_auth
def get_errors():
    """Return recent WARNING/ERROR log entries from this server process."""
    limit = min(int(request.args.get('limit', 50)), 200)
    with _error_log_lock:
        entries = list(_error_log[-limit:])
    return jsonify({'errors': entries[::-1]})  # newest first


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
    # Start central instance monitor
    # Each download gets its own aria2c spawned on demand via _start_instance()
    monitor = threading.Thread(target=_instance_monitor, daemon=True)
    monitor.start()

    kv = _load_kv(SECRETS_FILE)
    pw = kv.get('WEB_PASS', '(using hostname fallback)')
    aria2_sec = kv.get('ARIA2_SECRET', 'aria2rpc2026')
    print(f"  request server  → http://localhost:{PORT}")
    print(f"  aria2c port pool → {_PORT_BASE}–{_PORT_MAX}")
    print(f"  aria2 secret    : {aria2_sec}")
    print(f"  web password    : {pw}")
    print(f"  token hash      : {_web_password_hash()[:16]}...")
    app.run(host='0.0.0.0', port=PORT, debug=False)

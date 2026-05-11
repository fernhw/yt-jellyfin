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
    # Run in a thread so a stuck TCP connect can't block forever
    result = [None]
    error  = [None]
    def _call():
        try:
            with _ureq.urlopen(req, timeout=4) as r:
                resp = json.load(r)
            if 'error' in resp:
                raise RuntimeError(resp['error'].get('message', 'aria2 error'))
            result[0] = resp.get('result')
        except Exception as e:
            error[0] = e
    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=6)
    if t.is_alive():
        raise RuntimeError('aria2 RPC timeout')
    if error[0]:
        raise error[0]
    return result[0]

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
        paused  = _aria2_rpc('aria2.tellWaiting', [0, 20, _ARIA2_KEYS]) or []
    except Exception:
        return []

    # tellWaiting returns both waiting and paused; tellActive returns active only.
    # Combine and deduplicate by gid.
    seen: set = set()
    combined = []
    for item in active + waiting:
        g = item.get('gid')
        if g not in seen:
            seen.add(g)
            combined.append(item)

    out: List[Dict] = []
    for item in combined:
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
    """Start aria2c RPC daemon if it isn't already responding.
    Also kills any existing aria2c RPC processes that are stuck in UN state."""
    global _aria2_proc

    # Kill any aria2c RPC daemon processes stuck in UN (uninterruptible sleep)
    try:
        ps = subprocess.run(
            ['ps', '-axo', 'pid,stat,command'],
            capture_output=True, text=True, timeout=5,
        )
        for line in ps.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid_s, stat, cmd = parts
            # UN = uninterruptible + stopped; U alone also matches UN
            if 'U' in stat and 'aria2c' in cmd and 'rpc-listen-port' in cmd:
                try:
                    os.kill(int(pid_s), 9)
                    log.warning(f'aria2c watchdog: killed stuck UN process {pid_s}')
                    time.sleep(0.5)
                except Exception:
                    pass
    except Exception:
        pass

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
        # ── Session persistence: survive daemon restarts ──────────────────
        # Save all active/waiting downloads every 30s so they reload on restart
        f'--save-session={os.path.join(SCRIPT_DIR, "aria2.session")}',
        '--save-session-interval=30',
        # Reload any saved downloads from last session
        *(
            [f'--input-file={os.path.join(SCRIPT_DIR, "aria2.session")}']
            if os.path.exists(os.path.join(SCRIPT_DIR, 'aria2.session'))
            else []
        ),
        '--continue=true',
    ]
    _aria2_proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1.5)  # Wait for RPC socket to be ready
    # Re-add any dangling .aria2 files left over from before session-save existed
    try:
        _resume_dangling_aria2_files()
    except Exception as e:
        log.warning(f'aria2c resume-dangling: {e}')

def _resume_dangling_aria2_files() -> None:
    """
    Scan MOVIES_DIR and SHOWS_DIR for orphaned .aria2 control files.
    Each one means a partial download was interrupted without being tracked
    in the session file.  We reconstruct the magnet from the embedded infohash
    and re-add it to the daemon — aria2c will find the companion .aria2 file
    on disk and resume from where it left off.
    """
    import struct, binascii
    locs = _load_kv(LOCATIONS_FILE)
    scan_dirs = [
        locs.get('MOVIES_DIR', '/Volumes/Jellyfin/Movies'),
        locs.get('SHOWS_DIR',  '/Volumes/Jellyfin/Shows'),
    ]
    # Ask the daemon which GIDs it already knows about (to avoid duplicates)
    known_names: set = set()
    try:
        active  = _aria2_rpc('aria2.tellActive',  [['files']]) or []
        waiting = _aria2_rpc('aria2.tellWaiting', [0, 100, ['files']]) or []
        for item in active + waiting:
            for f in item.get('files', []):
                p = f.get('path', '')
                if p:
                    known_names.add(os.path.basename(p))
    except Exception:
        pass

    for base_dir in scan_dirs:
        if not os.path.isdir(base_dir):
            continue
        for root, dirs, files in os.walk(base_dir):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fname in files:
                if not fname.endswith('.aria2'):
                    continue
                aria2_path = os.path.join(root, fname)
                partial_name = fname[:-6]  # strip .aria2
                if partial_name in known_names:
                    continue  # already tracked
                try:
                    with open(aria2_path, 'rb') as fh:
                        data = fh.read(64)
                    if len(data) < 30:
                        continue
                    version = struct.unpack('>H', data[0:2])[0]
                    if version != 1:
                        continue  # unknown format
                    # v1 layout: 2B version | 4B ? | 4B ? | 20B infohash @ offset 10
                    infohash = binascii.hexlify(data[10:30]).decode()
                    # Sanity: must be 40 hex chars of non-zero data
                    if len(infohash) != 40 or infohash == '0' * 40:
                        continue
                    magnet = f'magnet:?xt=urn:btih:{infohash}'
                    dest_dir = root
                    options = {
                        'dir': dest_dir,
                        'out': partial_name,
                        'continue': 'true',
                    }
                    _aria2_rpc('aria2.addUri', [[magnet], options])
                    log.info(f'aria2c resume-dangling: re-added {partial_name} ({infohash[:8]}…)')
                except Exception as e:
                    log.warning(f'aria2c resume-dangling: skip {fname}: {e}')

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


@app.route('/api/movie-candidates')
@_require_auth
def movie_candidates():
    """Search for movie candidates synchronously and return scored list (no download)."""
    title = (request.args.get('title') or '').strip()
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
    title = (data.get('title') or '').strip()
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

@app.route('/api/torrent-state')
@_require_auth
def torrent_state():
    downloads = _aria2_active_downloads()
    # Tag slow downloads (flagged by showSchedulerSearch when seeds < threshold)
    slow_gids: list = []
    try:
        with open(os.path.join(SCRIPT_DIR, 'slow_gids.json')) as f:
            slow_gids = json.load(f)
    except Exception:
        pass
    if slow_gids:
        for dl in downloads:
            if dl.get('gid') in slow_gids:
                dl['slow'] = True
    return jsonify({'downloads': downloads, 'updated': time.time()})

@app.route('/api/torrent/<gid>/remove', methods=['POST'])
@_require_auth
def torrent_remove(gid: str):
    import shutil as _shutil
    if not re.fullmatch(r'[0-9a-f]{1,16}', gid):
        abort(400)
    # Collect file paths + base dir before removing
    base_dir = ''
    file_paths: list = []
    try:
        info = _aria2_rpc('aria2.tellStatus', [gid, ['files', 'dir']]) or {}
        base_dir = (info.get('dir') or '').rstrip('/')
        for f in (info.get('files') or []):
            p = (f.get('path') or '').strip()
            if p and p != '[METADATA]':
                file_paths.append(p)
    except Exception:
        pass
    # Force-remove from aria2
    try:
        _aria2_rpc('aria2.forceRemove', [gid])
    except Exception:
        pass
    try:
        _aria2_rpc('aria2.removeDownloadResult', [gid])
    except Exception:
        pass
    # Work out which top-level items to delete.
    # For a single-file torrent: delete the file + its .aria2 companion.
    # For a folder torrent: delete the whole subdirectory inside base_dir.
    to_delete: set = set()
    for p in file_paths:
        if base_dir and p.startswith(base_dir + '/'):
            # First path component inside base_dir
            rel = p[len(base_dir) + 1:]
            top = rel.split('/')[0]
            to_delete.add(os.path.join(base_dir, top))
        else:
            to_delete.add(p)
    for target in to_delete:
        try:
            if os.path.isdir(target):
                _shutil.rmtree(target, ignore_errors=True)
            elif os.path.isfile(target):
                os.remove(target)
            # also remove aria2 control file if present
            ctrl = target + '.aria2'
            if os.path.isfile(ctrl):
                os.remove(ctrl)
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
    # Always scan for dangling .aria2 files at startup, regardless of whether
    # the daemon was already running (covers pre-session-save orphans too)
    try:
        _resume_dangling_aria2_files()
    except Exception as e:
        log.warning(f'startup resume-dangling: {e}')
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

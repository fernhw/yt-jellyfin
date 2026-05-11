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

def _load_gid_names() -> dict:
    """Load gid→friendly name registry written by showSchedulerSearch."""
    try:
        with open(os.path.join(SCRIPT_DIR, 'gid_names.json')) as f:
            return json.load(f)
    except Exception:
        return {}


def _aria2_active_downloads() -> List[Dict]:
    """Return live download list from aria2c RPC."""
    try:
        active  = _aria2_rpc('aria2.tellActive',  [_ARIA2_KEYS]) or []
        # tellWaiting returns both queued and paused items
        waiting = _aria2_rpc('aria2.tellWaiting', [0, 50, _ARIA2_KEYS]) or []
    except Exception:
        return []
    gid_names = _load_gid_names()

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
        # Fall back to registered friendly name (covers [METADATA] phase)
        if not name or name.startswith('[METADATA]'):
            name = gid_names.get(item.get('gid', ''), name)

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
            if 'U' in stat and 'aria2c' in cmd:
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

    # Not responsive — kill any stale process holding the port before relaunching.
    try:
        ps = subprocess.run(
            ['lsof', '-ti', f':{ARIA2_RPC_PORT}'],
            capture_output=True, text=True, timeout=5,
        )
        for pid_s in ps.stdout.split():
            try:
                os.kill(int(pid_s), 9)
                log.warning(f'aria2c watchdog: killed stale process on port {ARIA2_RPC_PORT} pid={pid_s}')
            except Exception:
                pass
        if ps.stdout.strip():
            time.sleep(0.5)
    except Exception:
        pass

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
        '--save-session-interval=10',  # flush every 10s so less is lost on crash
        '--force-save=true',           # also save paused/error downloads, not just active
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
    # Session file (--input-file) already restores all downloads on restart.

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

@app.route('/api/show-download', methods=['POST'])
@_require_auth
def show_download_direct():
    """Download all episodes of a finished/older show without permanently adding to schedule.
    Adds a temporary CSV row, runs the full episode backlog, then removes it when done."""
    data = request.get_json(force=True)
    show_name = (data.get('show_name') or '').strip()
    if not show_name:
        return jsonify({'error': 'show_name required'}), 400
    search_name = (data.get('search_name') or show_name).strip().lower()
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

    # Collect everything we need BEFORE touching aria2 — once forceRemove is
    # called the GID is gone and we can't query it anymore.
    base_dir = ''
    file_paths: list = []
    ih_hex = ''
    try:
        info = _aria2_rpc('aria2.tellStatus', [gid, ['files', 'dir', 'infoHash']]) or {}
        base_dir = (info.get('dir') or '').rstrip('/')
        ih_hex   = (info.get('infoHash') or '').lower()
        for f in (info.get('files') or []):
            p = (f.get('path') or '').strip()
            if p and p != '[METADATA]':
                file_paths.append(p)
    except Exception:
        pass

    # Remove from aria2 daemon
    try:
        _aria2_rpc('aria2.forceRemove', [gid])
    except Exception:
        pass
    try:
        _aria2_rpc('aria2.removeDownloadResult', [gid])
    except Exception:
        pass

    # Remove from friendly-name registry
    try:
        names_path = os.path.join(SCRIPT_DIR, 'gid_names.json')
        with open(names_path) as f:
            names = json.load(f)
        if gid in names:
            del names[gid]
            with open(names_path, 'w') as f:
                json.dump(names, f)
    except Exception:
        pass

    # Build set of files/dirs to delete on disk
    to_delete: set = set()
    for p in file_paths:
        if base_dir and p.startswith(base_dir + '/'):
            rel = p[len(base_dir) + 1:]
            top = rel.split('/')[0]
            to_delete.add(os.path.join(base_dir, top))
        else:
            to_delete.add(p)

    # Always scan for the .aria2 control file by infohash — this catches
    # [METADATA] entries and any orphaned control files not listed in 'files'.
    if ih_hex:
        kv = _load_kv(SECRETS_FILE)
        scan_dirs = [d for k in ('MOVIES_DIR', 'SHOWS_DIR')
                     if (d := kv.get(k, '')) and os.path.isdir(d)]
        for base in scan_dirs:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fname in files:
                    if not fname.endswith('.aria2'):
                        continue
                    aria2_path = os.path.join(root, fname)
                    try:
                        with open(aria2_path, 'rb') as fh:
                            raw = fh.read(30)
                        if len(raw) >= 30:
                            if binascii.hexlify(raw[10:30]).decode().lower() == ih_hex:
                                to_delete.add(aria2_path)
                                partial = aria2_path[:-6]
                                if os.path.exists(partial):
                                    to_delete.add(partial)
                    except Exception:
                        pass
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

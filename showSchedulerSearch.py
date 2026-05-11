#!/usr/bin/env python3
"""
showSchedulerSearch.py — Hourly show episode hunter.

Reads showSchedule.csv and searches Nyaa.si (anime) or ThePirateBay (live
action) for new episodes. Applies quality filters (1080p+, English, 10+
seeds) and an algorithmic score to pick the best release, then downloads
via aria2c and sends a OneSignal push notification.

Run via cron (installed by showScheduler.sh):
  0 * * * * python3 /path/to/showSchedulerSearch.py

Manual usage:
  python3 showSchedulerSearch.py                   # normal run
  python3 showSchedulerSearch.py --dry-run         # search, no download
  python3 showSchedulerSearch.py --show "Re Zero"  # target one show
  python3 showSchedulerSearch.py --force           # ignore search window
  python3 showSchedulerSearch.py --list            # print schedule
"""

import argparse
import csv
import datetime
import html as html_mod
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = os.path.dirname(os.path.realpath(__file__))
SCHEDULE_CSV  = os.path.join(SCRIPT_DIR, "showSchedule.csv")
SECRETS_FILE  = os.path.join(SCRIPT_DIR, "secrets.md")
LOCATIONS_FILE = os.path.join(SCRIPT_DIR, "locations.md")
LOG_FILE      = os.path.join(SCRIPT_DIR, "showScheduler.log")

ARIA2_RPC_PORT = 6802

# ── Constants ──────────────────────────────────────────────────────────────────

ONESIGNAL_APP_ID  = "c88ae5a3-36df-4301-945f-9da65e63d87c"
ONESIGNAL_API_URL = "https://onesignal.com/api/v1/notifications"
REPORT_URL        = "https://report.fernhw.com"

MIN_SEEDS       = 10   # minimum seeds for shows and movies (fast pass)
MOVIE_SLOW_SEEDS = 2   # fallback floor for movies — "slow download" warning
SEARCH_DAYS  = 4   # days to keep searching after release day before marking missed

DAY_MAP   = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

CSV_FIELDS = [
    'show_name', 'search_name', 'folder', 'type', 'season', 'next_episode',
    'total_episodes', 'release_days', 'status',
    'search_start', 'search_end', 'last_check', 'week_anchor', 'anchor_episode',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)-7s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

# Also print INFO+ to stdout so cron logs and manual runs both get output
_con = logging.StreamHandler(sys.stdout)
_con.setLevel(logging.INFO)
_con.setFormatter(logging.Formatter('%(levelname)-7s %(message)s'))
log.addHandler(_con)

# ── Locations ──────────────────────────────────────────────────────────────────

def load_locations() -> Dict[str, str]:
    locs = {}
    try:
        with open(LOCATIONS_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    locs[k.strip()] = v.strip()
    except FileNotFoundError:
        log.warning("locations.md not found — using defaults")
    return locs

# ── OneSignal ──────────────────────────────────────────────────────────────────

def _read_onesignal_key() -> Optional[str]:
    """Reconstruct the obfuscated OneSignal REST key from secrets.md."""
    chars: dict[int, str] = {}
    try:
        with open(SECRETS_FILE) as f:
            for line in f:
                m = re.match(r'^K(\d+)="(.)"', line.rstrip())
                if m:
                    chars[int(m.group(1))] = m.group(2)
    except FileNotFoundError:
        return None
    return ''.join(v for k, v in sorted(chars.items())) if chars else None


def onesignal_push(heading: str, body: str) -> None:
    key = _read_onesignal_key()
    if not key:
        log.warning("OneSignal key missing — push skipped")
        return
    payload = json.dumps({
        "app_id":            ONESIGNAL_APP_ID,
        "included_segments": ["All"],
        "headings":          {"en": heading},
        "contents":          {"en": body},
        "url":               REPORT_URL,
    }).encode()
    req = urllib.request.Request(
        ONESIGNAL_API_URL,
        data=payload,
        headers={
            "Authorization":  f"Basic {key}",
            "Content-Type":   "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            log.info(f"  Push sent: {heading}")
    except Exception as exc:
        log.warning(f"  Push failed: {exc}")

# ── CSV helpers ────────────────────────────────────────────────────────────────

def read_schedule() -> List[Dict]:
    if not os.path.exists(SCHEDULE_CSV):
        return []
    with open(SCHEDULE_CSV, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    # Back-fill search_name for rows added before this field existed
    for r in rows:
        if not r.get('search_name', '').strip():
            r['search_name'] = r['show_name'].lower().strip()
        # Back-fill anchor_episode for rows added before this field existed
        if not r.get('anchor_episode', '').strip():
            r['anchor_episode'] = r.get('next_episode', '1')
    return rows


def write_schedule(rows: List[Dict]) -> None:
    with open(SCHEDULE_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

# ── Day / window helpers ───────────────────────────────────────────────────────

def _parse_days(days_str: str) -> List[int]:
    result = []
    for d in days_str.lower().split(','):
        d = d.strip()
        if d in DAY_MAP:
            result.append(DAY_MAP[d])
    return result


def _days_until_release(release_days_str: str, today: datetime.date) -> Optional[int]:
    """Days until next occurrence of any release day (0 = today)."""
    days = _parse_days(release_days_str)
    if not days:
        return None
    today_wd = today.weekday()
    best: Optional[int] = None
    for wd in days:
        delta = (wd - today_wd) % 7
        if best is None or delta < best:
            best = delta
    return best


def _monday_of_week(d: datetime.date) -> datetime.date:
    """Return the Monday of the week containing d."""
    return d - datetime.timedelta(days=d.weekday())


def _relative_week(today: datetime.date, anchor_str: str) -> Optional[int]:
    """
    Weeks elapsed since anchor Monday (0 = anchor week, 1 = next week, …).
    Returns None if anchor_str is empty or unparseable.
    """
    if not anchor_str:
        return None
    try:
        anchor_monday = _monday_of_week(datetime.date.fromisoformat(anchor_str))
        current_monday = _monday_of_week(today)
        delta = (current_monday - anchor_monday).days
        return delta // 7 if delta >= 0 else None
    except ValueError:
        return None


def _current_expected_episode(row: Dict, today: datetime.date) -> Optional[int]:
    """
    Which episode number is expected to air this calendar week?
    = anchor_episode + weeks_elapsed_since_anchor

    W5 = episode 5, W6 = episode 6, etc.
    This is the single source of truth for week-based dedup.
    """
    anchor_s  = row.get('week_anchor',   '').strip()
    ep_s      = row.get('anchor_episode','').strip()
    if not anchor_s or not ep_s:
        return None
    try:
        rel = _relative_week(today, anchor_s)
        if rel is None:
            return None
        return int(ep_s) + rel
    except ValueError:
        return None


def check_window(row: Dict, today: Optional[datetime.date] = None) -> Tuple[bool, bool]:
    """
    Returns (in_window, open_new_window).
    in_window       — True if we should search right now.
    open_new_window — True if search_start/search_end must be set to today.
    """
    if today is None:
        today = datetime.date.today()

    release_days = _parse_days(row.get('release_days', ''))
    if not release_days:
        return False, False

    # If an active window is stored, honour it
    start_s = row.get('search_start', '').strip()
    end_s   = row.get('search_end',   '').strip()
    if start_s and end_s:
        try:
            w_start = datetime.date.fromisoformat(start_s)
            w_end   = datetime.date.fromisoformat(end_s)
            if w_start <= today <= w_end:
                return True, False
            if today > w_end:
                return False, False   # window expired
        except ValueError:
            pass

    # No active window — start one if today is a release day
    if today.weekday() in release_days:
        return True, True

    return False, False

# ── Quality filters & scoring ──────────────────────────────────────────────────

# Explicit non-English markers — hard reject
_NON_EN = [
    'vostfr', '[fr]', '(fr)', 'french', 'german', 'deutsch',
    'spanish', '[es]', '(es)', 'italian', '[it]', 'portuguese',
    '[pt]', '[de]', '(de)', 'russian', '[ru]', 'arabic', '[ar]',
]
# Explicit English markers — fast accept
_EN_MARKERS = [
    'english', ' en ', '[en]', '(en)', '[en-us]', '[en-gb]',
    'dual audio', 'multi sub', 'multi-sub', 'dubbed', '[dub]',
]


def _has_english(title: str, show_type: str, nyaa_en_cat: bool = False) -> bool:
    """
    For anime  : reject explicit non-English; accept explicit EN or Nyaa cat 1_2.
    For live   : always True (search on TPB already in English shows category).
    """
    if show_type != 'anime':
        return True
    t = title.lower()
    for pat in _NON_EN:
        if pat in t:
            return False
    for pat in _EN_MARKERS:
        if pat in t:
            return True
    # Trust Nyaa English-Translated category if no explicit marker either way
    return nyaa_en_cat


def _has_min_res(title: str) -> bool:
    t = title.lower()
    return '1080p' in t or '2160p' in t or '4k' in t or '1080i' in t


def score_torrent(title: str, seeds: int, show_type: str,
                  nyaa_en_cat: bool = False,
                  min_seeds: int = MIN_SEEDS) -> Optional[float]:
    """
    Score a candidate torrent.  Returns None if below minimum requirements.
    Higher score = better pick.
    """
    if seeds < min_seeds:
        return None
    if not _has_min_res(title):
        return None
    if not _has_english(title, show_type, nyaa_en_cat):
        return None

    t = title.lower()
    score = min(seeds, 500) * 0.3   # seeds → up to 150 pts

    # ── Resolution ──────────────────────────────────────────────────────────
    if '2160p' in t or '4k' in t:
        score += 80
    elif '1080p' in t or '1080i' in t:
        score += 50

    # ── Source ──────────────────────────────────────────────────────────────
    if 'bluray' in t or 'bdrip' in t or 'bd ' in t or 'bd.' in t:
        score += 40
    elif 'web-dl' in t or 'webdl' in t:
        score += 30
    elif 'webrip' in t:
        score += 20
    elif any(s in t for s in ('crunchyroll', 'amzn', ' nf ', 'hidive', 'cr.')):
        score += 15

    # ── Video codec (x265/HEVC = better efficiency, usually better encode) ──
    if any(c in t for c in ('x265', 'hevc', 'h.265', 'h265')):
        score += 15

    # ── Audio ────────────────────────────────────────────────────────────────
    if 'flac' in t:
        score += 10
    elif any(a in t for a in ('ddp', 'eac3', 'dts', 'dd5.1', 'atmos')):
        score += 5

    # ── 10-bit encode quality bonus ──────────────────────────────────────────
    if '10bit' in t or '10-bit' in t or 'hi10p' in t:
        score += 5

    return score

# ── Nyaa.si scraper ────────────────────────────────────────────────────────────

def _fetch(url: str, timeout: int = 20) -> Optional[str]:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        log.warning(f"HTTP {exc.code} fetching {url}")
    except Exception as exc:
        log.warning(f"Fetch error {url}: {exc}")
    return None


def _parse_nyaa_html(body: str) -> List[Dict]:
    """
    Extract torrent entries from a Nyaa.si search results page.

    Table layout (column indices within each <tr>):
      0 — category icon
      1 — title  (colspan=2, still one <td> tag, contains /view/ link)
      2 — download links  (torrent file + magnet link)
      3 — size
      4 — date
      5 — seeders
      6 — leechers
      7 — completed
    """
    results = []
    row_re = re.compile(
        r'<tr\s+class="(?:success|default|warning|danger)"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE,
    )
    td_re = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)

    for row_m in row_re.finditer(body):
        row_html = row_m.group(1)
        tds = td_re.findall(row_html)
        if len(tds) < 7:
            continue

        # Title: the <a href="/view/NNN"> inside td[1]
        title_td = tds[1]
        title_links = re.findall(
            r'href="/view/\d+"[^>]*>([^<]+)</a>', title_td
        )
        title = html_mod.unescape(title_links[-1].strip()) if title_links else ''

        # Magnet: inside td[2]
        dl_td = tds[2]
        mag_m = re.search(r'href="(magnet:\?[^"]+)"', dl_td)
        magnet = html_mod.unescape(mag_m.group(1)) if mag_m else ''

        # Seeders: td[5], may be wrapped in <b>
        seeds_raw = re.sub(r'<[^>]+>', '', tds[5]).strip()
        try:
            seeds = int(seeds_raw)
        except ValueError:
            seeds = 0

        if title and magnet:
            results.append({'title': title, 'magnet': magnet, 'seeds': seeds})

    return results


def search_nyaa(search_name: str, season: int, episode: int) -> List[Dict]:
    """Search Nyaa.si (category 1_2 = Anime English Translated)."""
    query = f"{search_name} S{season:02d}E{episode:02d}"
    url = (
        f"https://nyaa.si/?f=0&c=1_2"
        f"&q={urllib.parse.quote(query)}"
        f"&s=seeders&o=desc"
    )
    log.info(f"  Nyaa query: {query}")
    body = _fetch(url)
    if not body:
        return []
    results = _parse_nyaa_html(body)
    log.info(f"  Nyaa → {len(results)} raw results")
    return results

# ── ThePirateBay (apibay.org JSON API) ────────────────────────────────────────

_TPB_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.tracker.cl:1337/announce',
    'udp://tracker.openbittorrent.com:6969/announce',
    'udp://exodus.desync.com:6969/announce',
]


def search_tpb(search_name: str, season: int, episode: int) -> List[Dict]:
    """Search ThePirateBay via apibay.org (no category filter = broadest results)."""
    query = f"{search_name} S{season:02d}E{episode:02d}"
    url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}"
    log.info(f"  TPB query: {query}")
    body = _fetch(url)
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("  TPB: invalid JSON response")
        return []

    results = []
    tr_str = '&tr='.join(urllib.parse.quote(t, safe='') for t in _TPB_TRACKERS)
    for item in data:
        name = item.get('name', '')
        if not name or name == 'No results returned':
            continue
        info_hash = item.get('info_hash', '').lower()
        if not info_hash:
            continue
        seeds = int(item.get('seeders', 0))
        magnet = (
            f"magnet:?xt=urn:btih:{info_hash}"
            f"&dn={urllib.parse.quote(name)}"
            f"&tr={tr_str}"
        )
        results.append({'title': name, 'magnet': magnet, 'seeds': seeds})

    log.info(f"  TPB → {len(results)} raw results")
    return results

# ── Download via aria2c RPC daemon ─────────────────────────────────────────────
# When requestServer.py is running it keeps an aria2c daemon alive on port
# ARIA2_RPC_PORT.  All downloads go through aria2.addUri so they appear in the
# UI overlay in real-time (speed, peers, seeders, progress).
# Falls back to a direct aria2c subprocess call if the daemon is unreachable.

def _aria2_secret_ss() -> str:
    kv: Dict[str, str] = {}
    try:
        with open(SECRETS_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    kv[k.strip()] = v.strip()
    except Exception:
        pass
    return kv.get('ARIA2_SECRET', 'aria2rpc2026')

def _aria2_rpc_ss(method: str, params: Optional[List] = None) -> object:
    body = json.dumps({
        'jsonrpc': '2.0', 'id': 'ss', 'method': method,
        'params': [f'token:{_aria2_secret_ss()}'] + (params or []),
    }).encode()
    req = urllib.request.Request(
        f'http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc',
        data=body, headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        resp = json.load(r)
    if 'error' in resp:
        raise RuntimeError(resp['error'].get('message', 'aria2 rpc error'))
    return resp.get('result')

def _aria2_add(magnet: str, dest_dir: str) -> Optional[str]:
    """Add a torrent to the aria2c daemon. Returns GID string or None."""
    try:
        gid = _aria2_rpc_ss('aria2.addUri', [
            [magnet],
            {'dir': dest_dir, 'seed-time': '0', 'file-allocation': 'falloc'},
        ])
        if gid:
            log.info(f"  aria2 RPC → GID {gid}")
            return str(gid)
    except urllib.error.URLError:
        log.warning('  aria2 daemon not reachable — falling back to direct aria2c')
    except Exception as exc:
        log.warning(f'  aria2 RPC error: {exc}')
    return None


def _mark_slow_gid(gid: str) -> None:
    """Write GID to slow_gids.json so the server can flag it in the UI."""
    path = os.path.join(SCRIPT_DIR, 'slow_gids.json')
    try:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            data = []
        if gid not in data:
            data.append(gid)
        with open(path, 'w') as f:
            json.dump(data, f)
    except Exception as exc:
        log.warning(f'  could not write slow_gids.json: {exc}')

def _aria2_wait(gid: str, timeout_sec: int = 7200) -> bool:
    """Poll aria2 until GID completes. Returns True on success."""
    import time as _time
    deadline = _time.time() + timeout_sec
    while _time.time() < deadline:
        _time.sleep(8)
        try:
            item = _aria2_rpc_ss('aria2.tellStatus', [gid, ['status', 'errorMessage']])
            status = (item or {}).get('status', '')
            if status == 'complete':
                return True
            if status == 'error':
                log.error(f'  GID {gid} error: {(item or {}).get("errorMessage")}')
                return False
        except Exception:
            pass
    log.error(f'  GID {gid} timed out after {timeout_sec}s')
    return False


def download_torrent(magnet: str, dest_dir: str, dry_run: bool = False,
                     slow_warning: bool = False) -> bool:
    """
    Submit a torrent magnet to aria2c.
    1. Try aria2c RPC daemon (managed by requestServer.py) — live progress in UI.
    2. Fall back to direct aria2c subprocess if daemon is not running.
    Returns True on success.
    """
    if dry_run:
        log.info(f"  [DRY-RUN] → {dest_dir}")
        return True

    os.makedirs(dest_dir, exist_ok=True)

    # ── Try RPC daemon first ──────────────────────────────────────────────────
    gid = _aria2_add(magnet, dest_dir)
    if gid:
        if slow_warning:
            _mark_slow_gid(gid)
        return _aria2_wait(gid)

    # ── Fallback: direct subprocess ───────────────────────────────────────────
    aria2c_bin = (
        shutil.which('aria2c')
        or '/opt/homebrew/bin/aria2c'
        or '/usr/local/bin/aria2c'
    )
    cmd = [
        aria2c_bin,
        '--seed-time=0',
        f'--dir={dest_dir}',
        '--summary-interval=30',
        '--console-log-level=notice',
        '--file-allocation=falloc',
        magnet,
    ]
    log.info(f"  aria2c direct → {dest_dir}")
    try:
        result = subprocess.run(cmd, timeout=7200)
        return result.returncode == 0
    except FileNotFoundError:
        log.error("  aria2c not found — install with: brew install aria2")
        return False
    except subprocess.TimeoutExpired:
        log.error("  aria2c timed out after 2 hours")
        return False

# ── Movie search ─────────────────────────────────────────────────────────────

def search_yts(title: str, year: Optional[int] = None) -> List[Dict]:
    """Search YTS (yts.mx) JSON API. Best for non-anime 1080p/4K movies."""
    query = f"{title} {year}" if year else title
    url = (
        f"https://yts.mx/api/v2/list_movies.json"
        f"?query_term={urllib.parse.quote(query)}&limit=10"
    )
    log.info(f"  YTS query: {query}")
    body = _fetch(url)
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("  YTS: invalid JSON")
        return []
    movies = (data.get('data') or {}).get('movies') or []
    results: List[Dict] = []
    tr_str = '&tr='.join(urllib.parse.quote(t, safe='') for t in _TPB_TRACKERS)
    for movie in movies:
        for torrent in (movie.get('torrents') or []):
            info_hash = torrent.get('hash', '').lower()
            if not info_hash:
                continue
            name = (
                f"{movie.get('title', title)}"
                f" ({movie.get('year', year or '')})"
                f" {torrent.get('quality', '')}"
                f" {torrent.get('video_codec', '')}"
            ).strip()
            seeds = int(torrent.get('seeds', 0))
            magnet = (
                f"magnet:?xt=urn:btih:{info_hash}"
                f"&dn={urllib.parse.quote(name)}"
                f"&tr={tr_str}"
            )
            results.append({'title': name, 'magnet': magnet, 'seeds': seeds})
    log.info(f"  YTS → {len(results)} raw results")
    return results


def search_tpb_movie(title: str) -> List[Dict]:
    """Search ThePirateBay for a movie (no episode filter)."""
    url = f"https://apibay.org/q.php?q={urllib.parse.quote(title)}"
    log.info(f"  TPB movie query: {title}")
    body = _fetch(url)
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("  TPB: invalid JSON")
        return []
    results: List[Dict] = []
    tr_str = '&tr='.join(urllib.parse.quote(t, safe='') for t in _TPB_TRACKERS)
    for item in data:
        name = item.get('name', '')
        if not name or name == 'No results returned':
            continue
        info_hash = item.get('info_hash', '').lower()
        if not info_hash:
            continue
        seeds = int(item.get('seeders', 0))
        magnet = (
            f"magnet:?xt=urn:btih:{info_hash}"
            f"&dn={urllib.parse.quote(name)}"
            f"&tr={tr_str}"
        )
        results.append({'title': name, 'magnet': magnet, 'seeds': seeds})
    log.info(f"  TPB movie → {len(results)} raw results")
    return results


def search_nyaa_movie(title: str) -> List[Dict]:
    """Search Nyaa for an anime movie (no episode filter, anime-eng category)."""
    url = (
        f"https://nyaa.si/?f=0&c=1_2"
        f"&q={urllib.parse.quote(title)}"
        f"&s=seeders&o=desc"
    )
    log.info(f"  Nyaa movie query: {title}")
    body = _fetch(url)
    if not body:
        return []
    results = _parse_nyaa_html(body)
    log.info(f"  Nyaa movie → {len(results)} raw results")
    return results


def _required_numbers(query: str) -> List[str]:
    """Return every standalone number in the user's query (sequel #, year, etc.)."""
    return re.findall(r'\b\d+\b', query)


def _title_satisfies_numbers(torrent_title: str, required: List[str]) -> bool:
    """
    All numbers the user typed must appear as standalone tokens in the
    torrent title.  If the user typed no numbers, always True.
    e.g. query 'jurassic park 2' needs '2' in title → rejects the 1993 film.
    e.g. query 'metropolis 2001' needs '2001' in title → rejects the 1927 film.
    """
    if not required:
        return True
    found = set(re.findall(r'\b\d+\b', torrent_title))
    return all(n in found for n in required)


def _score_movie(title: str, seeds: int, is_anime: bool,
                 prefer_4k: bool = False,
                 min_seeds: int = MIN_SEEDS) -> Optional[float]:
    """Score a movie torrent candidate. Returns None if below requirements."""
    if seeds < min_seeds:
        return None
    if not _has_min_res(title):
        return None
    show_type = 'anime' if is_anime else 'live'
    if not _has_english(title, show_type, False):
        return None
    base = score_torrent(title, seeds, show_type, min_seeds=min_seeds)
    if base is None:
        return None
    # When 4K is preferred, add a large bonus to 4K results so they
    # always sort above 1080p candidates.
    if prefer_4k:
        t = title.lower()
        if '2160p' in t or '4k' in t:
            base += 200
    return base


def list_movie_candidates(title: str, is_anime: bool,
                          prefer_4k: bool = False) -> List[Dict]:
    """
    Search for a movie and return the scored candidates as a list of dicts
    (without downloading anything). Used by requestServer to build the picker UI.
    """
    import json as _json
    q = title.lower()
    req_nums = _required_numbers(q)

    def _score_all(candidates: List[Dict], min_seeds: int = MIN_SEEDS) -> List[Dict]:
        out = []
        for c in candidates:
            if not _title_satisfies_numbers(c['title'], req_nums):
                continue
            s = _score_movie(c['title'], c['seeds'], is_anime, prefer_4k=prefer_4k, min_seeds=min_seeds)
            if s is not None:
                out.append({
                    'title':  c['title'],
                    'seeds':  c['seeds'],
                    'score':  round(s, 1),
                    'magnet': c.get('magnet', ''),
                    'slow':   False,
                })
        out.sort(key=lambda x: x['score'], reverse=True)
        return out

    if is_anime:
        primary = search_nyaa_movie(q) + search_tpb_movie(q)
    else:
        primary = search_tpb_movie(q)

    results = _score_all(primary)
    if not results:
        results = _score_all(search_yts(q))
    if not results:
        all_c = primary + (search_yts(q) if not primary else [])
        results = _score_all(all_c, min_seeds=MOVIE_SLOW_SEEDS)
        for r in results:
            r['slow'] = True

    # deduplicate by magnet
    seen: set = set()
    deduped = []
    for r in results:
        key = r['magnet'][:60] if r['magnet'] else r['title']
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped[:20]


def download_movie(title: str, is_anime: bool,
                   movies_dir: str, dry_run: bool = False,
                   prefer_4k: bool = False) -> None:
    """
    Search → score → download a movie to movies_dir.
    Title is lowercased before querying.
    Primary:  Nyaa + TPB (anime) or TPB alone (live-action).
    Fallback: YTS (both types) if primary yields nothing.
    """
    # If the server passed a pre-chosen magnet (user picked from the picker),
    # skip searching entirely and download it directly.
    direct_magnet = os.environ.get('MOVIE_DIRECT_MAGNET', '').strip()
    if direct_magnet:
        log.info(f"  Direct magnet requested for: {title}")
        ok = download_torrent(direct_magnet, movies_dir, dry_run=dry_run)
        if ok:
            log.info(f"  {title}: direct download started")
        else:
            log.error(f"  {title}: direct download failed")
        return

    q = title.lower()
    req_nums = _required_numbers(q)
    if req_nums:
        log.info(f"  Number filter: torrent title must contain {req_nums}")
    log.info(f"Searching movie: {q}")

    def _score_all(candidates: List[Dict], min_seeds: int = MIN_SEEDS) -> List[Tuple[float, Dict]]:
        scored: List[Tuple[float, Dict]] = []
        for c in candidates:
            if not _title_satisfies_numbers(c['title'], req_nums):
                log.debug(f"  skip (number mismatch): {c['title']}")
                continue
            s = _score_movie(c['title'], c['seeds'], is_anime, prefer_4k=prefer_4k, min_seeds=min_seeds)
            if s is not None:
                scored.append((s, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    # ── Primary pass (10+ seeds) ─────────────────────────────────────────────
    if is_anime:
        primary = search_nyaa_movie(q) + search_tpb_movie(q)
    else:
        primary = search_tpb_movie(q)
    scored = _score_all(primary)

    if not scored:
        log.warning(f"  primary pass: no qualifying results — trying YTS fallback")
        scored = _score_all(search_yts(q))

    slow_download = False
    if not scored:
        # ── Slow fallback (2+ seeds) ─────────────────────────────────────────
        log.warning(f"  no fast results — trying slow fallback (>={MOVIE_SLOW_SEEDS} seeds)")
        all_candidates = primary + (search_yts(q) if not primary else [])
        scored = _score_all(all_candidates, min_seeds=MOVIE_SLOW_SEEDS)
        if scored:
            slow_download = True
            log.warning(f"  slow fallback found {len(scored)} candidate(s)")

    if not scored:
        log.error(f"  {q}: no qualifying results across all sources (need {MIN_SEEDS}+ seeds, 1080p+, English)")
        return

    best_score, best = scored[0]
    if slow_download:
        log.warning(f"  SLOW: {best['title']} | score={best_score:.0f} seeds={best['seeds']} (below normal threshold)")
    else:
        log.info(f"  Best: {best['title']} | score={best_score:.0f} seeds={best['seeds']}")

    ok = download_torrent(best['magnet'], movies_dir, dry_run=dry_run,
                          slow_warning=slow_download)
    if ok:
        log.info(f"  {q}: download {'started (may be slow)' if slow_download else 'complete'}")
    else:
        log.error(f"  {q}: download failed")


# ── Show report (daily web banner) ────────────────────────────────────────────

def _update_show_report(row: Dict, season: int, episode: int,
                        total: int, torrent_title: str) -> None:
    """Call showSchedulerReport.sh to append this episode to today's JSON."""
    report_sh = os.path.join(SCRIPT_DIR, 'showSchedulerReport.sh')
    if not os.path.exists(report_sh):
        log.warning("showSchedulerReport.sh not found — skipping report update")
        return
    try:
        subprocess.run(
            ['sh', report_sh,
             row['show_name'], row['folder'],
             str(season), str(episode), str(total),
             torrent_title],
            timeout=10,
        )
    except Exception as exc:
        log.warning(f"  show report update failed: {exc}")

# ── Core processing ────────────────────────────────────────────────────────────

def process_show(row: dict, locations: dict,
                 dry_run: bool = False,
                 force: bool = False) -> dict:
    """
    Evaluate one schedule row and, if appropriate, search → score → download.
    Returns the (possibly mutated) row dict.
    """
    today       = datetime.date.today()
    show_name   = row['show_name']
    # search_name: clean lowercase query term, auto-derived if missing
    search_name = row.get('search_name', '').strip() or show_name.lower()
    season      = int(row['season'])
    episode     = int(row['next_episode'])
    total       = int(row['total_episodes'])
    show_type   = row.get('type', 'anime').lower()
    status      = row.get('status', 'pending').strip()
    shows_dir   = locations.get('SHOWS_DIR', '/Volumes/Jellyfin/Shows')
    dest        = os.path.join(shows_dir, row['folder'])
    label       = f"{show_name} S{season:02d}E{episode:02d}"

    row['last_check'] = today.isoformat()

    # Terminal states
    if status == 'complete':
        return row
    if status == 'missed' and not force:
        return row

    # Episode-week dedup — not overridable by --force.
    # W5 = episode 5. If we already have this week's episode (next_episode > expected),
    # sit tight until the next week's episode is due.
    current_expected = _current_expected_episode(row, today)
    if current_expected is not None and episode > current_expected:
        log.info(
            f"  {show_name}: already have W{current_expected}/Ep{current_expected}"
            f" — Ep{episode} due at W{episode}"
        )
        return row

    # Window check
    in_window, open_window = check_window(row, today)
    if not in_window and not force:
        log.info(f"  {show_name}: not in search window — skip")
        return row

    # Open a new search window on the release day
    if open_window:
        row['search_start'] = today.isoformat()
        row['search_end']   = (today + datetime.timedelta(days=SEARCH_DAYS)).isoformat()
        row['status']       = 'searching'
        log.info(f"  {show_name}: search window {row['search_start']} → {row['search_end']}")
    elif status == 'missed' and force:
        row['status'] = 'searching'

    # ── Search ──────────────────────────────────────────────────────────────
    log.info(f"Searching: {label}")
    candidates: List[Tuple[float, Dict]] = []

    if show_type == 'anime':
        for r in search_nyaa(search_name, season, episode):
            s = score_torrent(r['title'], r['seeds'], show_type, nyaa_en_cat=True)
            if s is not None:
                candidates.append((s, r))

    # TPB: primary source for live action, fallback for anime when Nyaa dry
    if show_type == 'live' or not candidates:
        for r in search_tpb(search_name, season, episode):
            s = score_torrent(r['title'], r['seeds'], show_type)
            if s is not None:
                candidates.append((s, r))

    if not candidates:
        log.info(f"  {label}: no qualifying results (1080p + English + {MIN_SEEDS}+ seeds)")

        # Check window expiry
        end_s = row.get('search_end', '').strip()
        if end_s:
            try:
                if today > datetime.date.fromisoformat(end_s):
                    if total >= 99:
                        # Open-ended total (don't know) → assume season is over, auto-withdraw
                        row['status']       = 'complete'
                        row['search_start'] = ''
                        row['search_end']   = ''
                        log.warning(
                            f"  {label}: no result after {SEARCH_DAYS} days"
                            f" (open-ended total) → auto-withdrawn as complete"
                        )
                        onesignal_push(
                            f"✅ {show_name} — auto-complete",
                            f"No new episode found after {SEARCH_DAYS} days — assumed season ended at ep {episode - 1}.",
                        )
                    else:
                        row['status'] = 'missed'
                        log.warning(f"  {label}: search window expired → missed")
                        onesignal_push(
                            f"⚠ {show_name} not found",
                            f"{label} — no result after {SEARCH_DAYS} days. Check manually.",
                        )
            except ValueError:
                pass
        return row

    # ── Pick best ───────────────────────────────────────────────────────────
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best = candidates[0]
    log.info(
        f"  Best: {best['title'][:80]}"
        f" | score={best_score:.0f} seeds={best['seeds']}"
    )

    # ── Download ─────────────────────────────────────────────────────────────
    ok = download_torrent(best['magnet'], dest, dry_run=dry_run)

    if ok:
        log.info(f"  {label}: downloaded ✓")
        # Dedup is now purely mathematical (anchor_episode + elapsed weeks)
        # No field to update here — next_episode increment is enough.
        # Update daily show report for the web page (skip in dry-run)
        if not dry_run:
            _update_show_report(row, season, episode, total, best['title'])
        onesignal_push(
            f"📺 {show_name}",
            f"S{season:02d}E{episode:02d} downloaded → {row['folder']}",
        )
        if episode >= total:
            row['status']       = 'complete'
            row['search_start'] = ''
            row['search_end']   = ''
            log.info(f"  {show_name}: all {total} episodes done")
            onesignal_push(
                f"✅ {show_name} — Season {season} complete",
                f"All {total} episodes downloaded.",
            )
        else:
            row['next_episode'] = str(episode + 1)
            row['status']       = 'pending'
            row['search_start'] = ''
            row['search_end']   = ''
            log.info(f"  {show_name}: next → S{season:02d}E{episode + 1:02d}")
    else:
        log.error(f"  {label}: download failed")

    return row

# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Show episode scheduler — hourly torrent hunter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument('--dry-run', action='store_true',
                    help='Search but do not download or modify schedule')
    ap.add_argument('--show',    metavar='NAME',
                    help='Restrict to shows whose name contains NAME')
    ap.add_argument('--force',   action='store_true',
                    help='Search even if outside the release window')
    ap.add_argument('--back-ep', metavar='N', type=int, default=None,
                    help='Override next_episode to N (used by backfill in showScheduler.sh)')
    ap.add_argument('--list',    action='store_true',
                    help='Print the current schedule and exit')
    ap.add_argument('--movie',       metavar='TITLE',
                    help='Download a movie by title (bypasses CSV)')
    ap.add_argument('--anime-movie', action='store_true',
                    help='Treat --movie as anime (searches Nyaa+TPB first, YTS fallback)')
    ap.add_argument('--prefer-4k', action='store_true',
                    help='Prefer 4K/2160p results for movies (large score bonus; falls back to 1080p)')
    ap.add_argument('--list-candidates', action='store_true',
                    help='Print top movie candidates as JSON and exit (requires --movie)')
    args = ap.parse_args()

    # ── Movie mode: search & download, then exit ──────────────────────────────
    if args.movie:
        if args.list_candidates:
            import json as _json
            results = list_movie_candidates(
                title=args.movie,
                is_anime=args.anime_movie,
                prefer_4k=args.prefer_4k,
            )
            print(_json.dumps(results))
            return
        if args.dry_run:
            log.info("=== DRY-RUN mode — no downloads ===")
        locations = load_locations()
        movies_dir = locations.get('MOVIES_DIR', '/Volumes/Jellyfin/Movies')
        download_movie(
            title=args.movie,
            is_anime=args.anime_movie,
            movies_dir=movies_dir,
            dry_run=args.dry_run,
            prefer_4k=args.prefer_4k,
        )
        return

    rows = read_schedule()
    if not rows:
        log.info("Schedule is empty — add shows with showScheduler.sh")
        return

    if args.list:
        today_ld = datetime.date.today()
        print(f"\n{'SHOW':<22} {'SEARCH':<18} {'EP':<10} {'DAYS':<5} {'TYPE':<6} {'THIS WK':<8} STATUS")
        print("─" * 80)
        for r in rows:
            ep_label   = f"S{int(r['season']):02d}E{r['next_episode']}/{r['total_episodes']}"
            cur_ep     = _current_expected_episode(r, today_ld)
            next_ep    = int(r['next_episode'])
            if cur_ep is None:
                week_s = '?'
            elif next_ep > cur_ep:
                week_s = f"W{cur_ep} ✓"   # already have this week's
            else:
                week_s = f"W{cur_ep}"      # due this week
            print(f"{r['show_name']:<22} {r.get('search_name',''):<18} {ep_label:<10} "
                  f"{r['release_days']:<5} {r['type']:<6} {week_s:<8} {r['status']}")
        print()
        locations = load_locations()
        write_status_json(rows, locations)
        return

    if args.dry_run:
        log.info("=== DRY-RUN mode — no downloads or CSV writes ===")

    locations = load_locations()
    # Work on a mutable copy so we can write incrementally
    updated = list(rows)

    for i, row in enumerate(rows):
        if args.show and args.show.lower() not in row['show_name'].lower():
            continue
        # --back-ep: temporarily set next_episode for this pass only
        if args.back_ep is not None:
            row = dict(row)   # shallow copy — don't mutate original for other episodes
            row['next_episode'] = str(args.back_ep)
        new_row = process_show(row, locations,
                               dry_run=args.dry_run,
                               force=args.force)
        updated[i] = new_row
        # Write after every show so a crash / kill mid-run never loses progress.
        # The search window opening and download state are both persisted immediately.
        if not args.dry_run:
            write_schedule(updated)

    if not args.dry_run:
        write_status_json(updated, locations)

    # Summary
    by_status: Dict[str, int] = {}
    for r in updated:
        s = r.get('status', 'unknown')
        by_status[s] = by_status.get(s, 0) + 1
    parts = ', '.join(f"{v} {k}" for k, v in sorted(by_status.items()))
    log.info(f"Run complete — {parts}")


# ── Status JSON ────────────────────────────────────────────────────────────────

def _find_show_thumb(shows_dir: str, folder: str, web_dir: str) -> str:
    """
    Find folder art in the show directory, copy it to web/media-thumbs/,
    and return a web-relative path. Returns '' if nothing found.
    """
    show_dir = os.path.join(shows_dir, folder)
    src = ''
    for name in ('folder.jpg', 'cover.jpg', 'poster.jpg', 'Cover.jpg'):
        candidate = os.path.join(show_dir, name)
        if os.path.isfile(candidate):
            src = candidate
            break
    if not src:
        import glob as _glob
        hits = _glob.glob(os.path.join(show_dir, '**', '*.jpg'), recursive=True)
        if hits:
            src = hits[0]
    if not src:
        return ''
    safe_key = re.sub(r'[^a-z0-9]+', '-', folder.lower()).strip('-')
    thumbs_dir = os.path.join(web_dir, 'media-thumbs')
    os.makedirs(thumbs_dir, exist_ok=True)
    dest = os.path.join(thumbs_dir, f"show-sched-{safe_key}.jpg")
    try:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert('RGB')
            # Resize to 200px wide (2× display size for retina), keep aspect ratio
            w, h = im.size
            new_w = 200
            new_h = int(h * new_w / w)
            im = im.resize((new_w, new_h), Image.LANCZOS)
            im.save(dest, 'JPEG', quality=82, optimize=True)
        return f"media-thumbs/show-sched-{safe_key}.jpg"
    except ImportError:
        import shutil
        shutil.copy2(src, dest)
        return f"media-thumbs/show-sched-{safe_key}.jpg"
    except Exception:
        return ''


def write_status_json(rows: List[Dict], locations: Dict[str, str]) -> None:
    """
    Write showSchedulerStatus.json — full show list with current download state.
    Used by the web report tracker section; written after every non-dry run.
    """
    shows_dir = locations.get('SHOWS_DIR', '/Volumes/Jellyfin/Shows')
    web_dir   = os.path.join(SCRIPT_DIR, 'web')
    today     = datetime.date.today()
    status_path = os.path.join(SCRIPT_DIR, 'showSchedulerStatus.json')

    shows = []
    for r in rows:
        name       = r['show_name']
        season     = int(r['season'])
        next_ep    = int(r['next_episode'])
        total      = int(r['total_episodes'])
        downloaded = next_ep - 1          # last episode we actually have
        cur_ep     = _current_expected_episode(r, today)

        if cur_ep is None:
            week_label = '?'
        elif next_ep > cur_ep:
            week_label = f"W{cur_ep} ✓"
        else:
            week_label = f"W{cur_ep} due"

        thumb = _find_show_thumb(shows_dir, r['folder'], web_dir)

        is_complete = downloaded >= total and total > 0
        days_until  = _days_until_release(r.get('release_days', ''), today)

        shows.append({
            'show':        name,
            'season':      season,
            'downloaded':  downloaded,   # 0 = nothing yet
            'next_ep':     next_ep,
            'total':       total,
            'week':        week_label,
            'status':      r.get('status', 'pending'),
            'release_days': r.get('release_days', ''),
            'days_until':  days_until,
            'is_complete': is_complete,
            'thumb':       thumb,
        })

    data = {
        'updated': today.isoformat(),
        'shows':   shows,
    }
    try:
        with open(status_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning(f"Could not write status JSON: {exc}")


if __name__ == '__main__':
    main()

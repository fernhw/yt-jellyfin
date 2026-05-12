#!/usr/bin/env python3
"""
musicSearch.py — Music download helpers.

Three search paths:
  mainstream  — MusicBrainz autocomplete → aria2c via TPB magnet
  soundtrack  — KHInsider scrape (HTTP download per track) → TPB fallback
  niche       — Deep TPB search with OST/Soundtrack/Score/etc. variations

Called by requestServer.py. Can also be run directly for testing:
  python3 musicSearch.py mainstream "Linkin Park Meteora"
  python3 musicSearch.py kh "Final Fantasy VII"
  python3 musicSearch.py tpb "Arca Kick I" --niche
"""

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── MusicBrainz ───────────────────────────────────────────────────────────────

def suggest_music_mb(query: str, limit: int = 8) -> List[Dict]:
    """
    Search MusicBrainz release-groups for albums/singles matching query.
    Returns list of {artist, album, year, mb_id}.
    Rate limit: ~1 req/s is fine for personal use — no key needed.
    """
    url = (
        'https://musicbrainz.org/ws/2/release-group?fmt=json'
        '&limit=' + str(limit) +
        '&query=' + urllib.parse.quote(query)
    )
    req = urllib.request.Request(url, headers={
        'User-Agent': 'JellyfinReq/1.0 (personal; contact@localhost)',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning('MusicBrainz search error: %s', e)
        return []

    results: List[Dict] = []
    seen: set = set()
    for rg in (data.get('release-groups') or []):
        album = (rg.get('title') or '').strip()
        artist_credits = rg.get('artist-credit') or []
        artist = ''.join(
            (a.get('artist', {}).get('name', '') if isinstance(a, dict) else str(a))
            for a in artist_credits
        ).strip()
        # Ignore join phrases like ' & ', ' feat. ' etc. already embedded by MB
        year_raw = (rg.get('first-release-date') or '')[:4]
        year = int(year_raw) if year_raw.isdigit() else None
        mb_id = rg.get('id', '')
        key = (artist.lower(), album.lower())
        if key in seen or not album:
            continue
        seen.add(key)
        results.append({'artist': artist, 'album': album, 'year': year, 'mb_id': mb_id})
    return results


# ── KHInsider ─────────────────────────────────────────────────────────────────

KH_BASE = 'https://downloads.khinsider.com'

_KH_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
}


def search_khinsider(query: str, limit: int = 10) -> List[Dict]:
    """
    Search KHInsider for soundtrack albums matching query.
    Returns list of {name, url}.
    """
    url = KH_BASE + '/search?search=' + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers=_KH_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        log.warning('KHInsider search error: %s', e)
        return []

    results: List[Dict] = []
    seen: set = set()
    # KHInsider search result links: <a href="/game-soundtracks/album/slug">Name</a>
    for m in re.finditer(
        r'<a\s+href="(/game-soundtracks/album/[^"#?]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE
    ):
        path = m.group(1).strip()
        raw_name = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        name = re.sub(r'\s+', ' ', raw_name)
        if not name or len(name) > 250 or path in seen:
            continue
        seen.add(path)
        results.append({'name': name, 'url': KH_BASE + path})
        if len(results) >= limit:
            break
    return results


def fetch_khinsider_tracks(album_url: str) -> Tuple[str, List[Dict]]:
    """
    Fetch the track list from a KHInsider album page.
    Returns (album_name, [{title, detail_url}]).

    detail_url is the per-track page; the actual MP3 URL is one level deeper.
    """
    req = urllib.request.Request(album_url, headers=_KH_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        log.error('KHInsider album fetch error: %s', e)
        return ('', [])

    # Album title — several possible patterns KHInsider uses
    album_name = ''
    for pat in (
        r'<h2[^>]*class="albumTitle"[^>]*>\s*(.*?)\s*</h2>',
        r'<h2[^>]*>\s*(.*?)\s*</h2>',
        r'<title>\s*(.*?)\s*[|\-]',
    ):
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            album_name = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if album_name:
                break

    tracks: List[Dict] = []
    seen: set = set()

    # Track links in the album song-list table
    # Pattern A: direct .mp3/.flac links to detail pages
    for m in re.finditer(
        r'<a\s+href="(/game-soundtracks/album/[^"]+\.(?:mp3|flac|ogg))"[^>]*>([^<]+)</a>',
        html, re.IGNORECASE
    ):
        path  = m.group(1)
        title = m.group(2).strip()
        url   = KH_BASE + path
        if url not in seen and title:
            seen.add(url)
            tracks.append({'title': title, 'detail_url': url})

    # Pattern B: <td class="clickable-row"><a href="...">title</a></td>
    if not tracks:
        for m in re.finditer(
            r'class="clickable-row"[^>]*>\s*<a\s+href="([^"]+)"[^>]*>([^<]+)</a>',
            html, re.IGNORECASE
        ):
            path  = m.group(1)
            title = m.group(2).strip()
            if not path.startswith('http'):
                path = KH_BASE + path
            if path not in seen and title:
                seen.add(path)
                tracks.append({'title': title, 'detail_url': path})

    return (album_name, tracks)


def _resolve_kh_direct_url(detail_url: str) -> Optional[str]:
    """
    Fetch the per-track KHInsider page and extract the direct CDN download URL.
    The page contains an <audio> tag or a direct link like:
      <a href="https://...cdn.../.../track.mp3">Click here to download</a>
    """
    req = urllib.request.Request(detail_url, headers={**_KH_HEADERS, 'Referer': KH_BASE})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except Exception:
        return None

    # Priority order: audio src, then explicit download anchor
    patterns = [
        r'<audio[^>]+src="(https://[^"]+\.(?:mp3|flac|ogg)[^"]*)"',
        r'<source[^>]+src="(https://[^"]+\.(?:mp3|flac|ogg)[^"]*)"',
        r'href="(https://[^"]+\.(?:mp3|flac)[^"]*)"',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def download_khinsider_album(
    album_url: str,
    dest_dir: str,
    state: Dict,
    stop_event: threading.Event,
) -> bool:
    """
    Download all tracks from a KHInsider album page into dest_dir.
    Updates state dict in-place for progress reporting:
      state['total'], state['done'], state['failed'], state['status'],
      state['current'], state['album'].
    Returns True if fully successful (no failures).
    """
    album_name, tracks = fetch_khinsider_tracks(album_url)

    if not tracks:
        state['status'] = 'error'
        state['error']  = 'No tracks found on album page — the URL may be wrong.'
        return False

    os.makedirs(dest_dir, exist_ok=True)
    state.update({
        'total':   len(tracks),
        'done':    0,
        'failed':  0,
        'status':  'active',
        'album':   album_name or dest_dir,
    })

    for track in tracks:
        if stop_event.is_set():
            state['status'] = 'removed'
            return False

        state['current'] = track['title']

        direct_url = _resolve_kh_direct_url(track['detail_url'])
        if not direct_url:
            log.warning('KH: could not resolve direct URL for %r', track['title'])
            state['failed'] += 1
            time.sleep(0.5)
            continue

        # Build a safe filename from the URL's basename
        raw_fname = urllib.parse.unquote(direct_url.split('/')[-1].split('?')[0])
        safe_fname = re.sub(r'[^\w\s\-.]', '_', raw_fname)[:160].strip()
        if not safe_fname:
            safe_fname = re.sub(r'[^\w\s\-]', '_', track['title'])[:80] + '.mp3'
        out_path = os.path.join(dest_dir, safe_fname)

        if os.path.exists(out_path):
            state['done'] += 1
            continue

        try:
            dl_req = urllib.request.Request(direct_url, headers={
                **_KH_HEADERS,
                'Referer': album_url,
            })
            with urllib.request.urlopen(dl_req, timeout=120) as resp:
                data = resp.read()
            with open(out_path, 'wb') as f:
                f.write(data)
            state['done'] += 1
            log.info('KH downloaded: %s', safe_fname)
        except Exception as e:
            log.warning('KH track download failed for %r: %s', track['title'], e)
            state['failed'] += 1

        # Polite delay — KHInsider rate-limits aggressively
        time.sleep(1.2)

    state['current'] = ''
    if state['failed'] == 0:
        state['status'] = 'complete'
    elif state['done'] > 0:
        state['status'] = 'complete_partial'
    else:
        state['status'] = 'error'
        state['error']  = 'All tracks failed to download'
    return state['failed'] == 0


# ── TPB music search ──────────────────────────────────────────────────────────

# TPB music categories: 101=Music, 102=Audio books, 103=Sound clips, 104=FLAC, 199=Other
_TPB_MUSIC_CATS = '101,104'

_TPB_TRACKERS = (
    '&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce'
    '&tr=udp%3A%2F%2Fopen.tracker.cl%3A1337%2Fannounce'
    '&tr=udp%3A%2F%2Ftracker.openbittorrent.com%3A6969%2Fannounce'
)


def _tpb_single_search(query: str) -> List[Dict]:
    url = (
        'https://apibay.org/q.php?cat=' + _TPB_MUSIC_CATS
        + '&q=' + urllib.parse.quote(query)
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning('TPB music search error for %r: %s', query, e)
        return []

    results = []
    for item in (data or []):
        if not isinstance(item, dict):
            continue
        ih = (item.get('info_hash') or '').lower()
        if not ih or ih == '0' * 40:
            continue
        name   = (item.get('name') or '').strip()
        seeds  = int(item.get('seeders') or 0)
        size_b = int(item.get('size')    or 0)
        magnet = (
            f'magnet:?xt=urn:btih:{ih}'
            f'&dn={urllib.parse.quote(name)}'
            + _TPB_TRACKERS
        )
        results.append({
            'title':      name,
            'seeds':      seeds,
            'magnet':     magnet,
            'size_bytes': size_b,
        })
    return results


def search_tpb_music(query: str, niche: bool = False) -> List[Dict]:
    """
    Search TPB for music.
    niche=True fires multiple queries with OST/Soundtrack/Score etc. variations
    to maximise coverage for game/anime/film scores and rare releases.
    Returns deduplicated list sorted by seeds (top 20).
    """
    queries = [query]
    if niche:
        base = query.strip()
        extras = [
            base + ' OST',
            base + ' Soundtrack',
            base + ' Original Soundtrack',
            base + ' Score',
            base + ' Original Score',
            base + ' Music',
            base + ' FLAC',
        ]
        queries = [base] + extras

    seen: set = set()
    all_results: List[Dict] = []

    for q in queries:
        for item in _tpb_single_search(q):
            # Deduplicate by magnet info_hash
            ih = re.search(r'btih:([0-9a-f]+)', item['magnet'], re.IGNORECASE)
            key = ih.group(1).lower() if ih else item['title'].lower()
            if key in seen:
                continue
            seen.add(key)
            all_results.append(item)

    all_results.sort(key=lambda r: r['seeds'], reverse=True)
    return all_results[:20]


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s  %(message)s')
    if len(sys.argv) < 3:
        print('Usage: python3 musicSearch.py <mainstream|kh|tpb> <query> [--niche]')
        sys.exit(1)

    mode  = sys.argv[1]
    query = sys.argv[2]
    niche = '--niche' in sys.argv

    if mode == 'mainstream':
        results = suggest_music_mb(query)
        print(json.dumps(results, indent=2))
    elif mode == 'kh':
        results = search_khinsider(query)
        print(json.dumps(results, indent=2))
        if results:
            print(f'\n--- Tracks for first result: {results[0]["name"]} ---')
            name, tracks = fetch_khinsider_tracks(results[0]['url'])
            print(f'Album: {name}  |  {len(tracks)} tracks')
            for t in tracks[:5]:
                print(f'  {t["title"]}')
    elif mode == 'tpb':
        results = search_tpb_music(query, niche=niche)
        print(json.dumps(results, indent=2))
    else:
        print('Unknown mode:', mode)
        sys.exit(1)

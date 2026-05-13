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


def get_artist_albums_mb(artist_name: str, limit: int = 30) -> List[Dict]:
    """
    Fetch studio albums for a specific artist from MusicBrainz release-groups.
    Returns [{album, year, mb_id}] sorted by year ascending.
    """
    safe = artist_name.replace('"', '').replace('\\', '')
    query = (
        f'artist:"{safe}" AND primarytype:album'
        ' NOT secondarytype:live'
        ' NOT secondarytype:compilation'
        ' NOT secondarytype:remix'
        ' NOT secondarytype:interview'
        ' NOT secondarytype:demo'
        ' NOT secondarytype:mixtape'
    )
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
        log.warning('MusicBrainz artist albums error: %s', e)
        return []

    results: List[Dict] = []
    seen: set = set()
    _SKIP_SECONDARY = {'Live', 'Compilation', 'Remix', 'DJ-mix', 'Mixtape/Street', 'Demo', 'Interview', 'Spokenword', 'Audiobook'}
    for rg in (data.get('release-groups') or []):
        album = (rg.get('title') or '').strip()
        if not album or album.lower() in seen:
            continue
        sec_types = set(rg.get('secondary-types') or [])
        if sec_types & _SKIP_SECONDARY:
            continue
        seen.add(album.lower())
        year_raw = (rg.get('first-release-date') or '')[:4]
        year = int(year_raw) if year_raw.isdigit() else None
        results.append({'album': album, 'year': year, 'mb_id': rg.get('id', '')})

    results.sort(key=lambda r: (r['year'] or 9999))
    return results


def suggest_artist_mb(query: str, limit: int = 8) -> List[Dict]:
    """
    Search MusicBrainz artists/bands/composers matching query.
    Returns list of {name, type, disambiguation, mb_id}.
    Only includes music-creator types: Person, Group, Orchestra, Choir.
    """
    _CREATOR_TYPES = {'Person', 'Group', 'Orchestra', 'Choir'}
    url = (
        'https://musicbrainz.org/ws/2/artist?fmt=json'
        '&limit=20'
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
        log.warning('MusicBrainz artist search error: %s', e)
        return []

    results: List[Dict] = []
    seen: set = set()
    for artist in (data.get('artists') or []):
        name = (artist.get('name') or '').strip()
        if not name or name.lower() in seen:
            continue
        atype = (artist.get('type') or '').strip()
        if atype not in _CREATOR_TYPES:
            continue
        seen.add(name.lower())
        disambiguation = (artist.get('disambiguation') or '').strip()
        mb_id = artist.get('id', '')
        results.append({
            'name': name,
            'type': atype,
            'disambiguation': disambiguation,
            'mb_id': mb_id,
        })
        if len(results) >= limit:
            break
    return results


# ── KHInsider ─────────────────────────────────────────────────────────────────
# KHInsider is behind Cloudflare; urllib/curl both get 403.
# curl_cffi with Chrome TLS fingerprint impersonation bypasses it.

KH_BASE = 'https://downloads.khinsider.com'


def _kh_get(url: str, referer: str = KH_BASE, timeout: int = 20) -> str:
    """Fetch a KHInsider URL using curl_cffi Chrome impersonation."""
    try:
        from curl_cffi import requests as _cffi_req  # type: ignore
        r = _cffi_req.get(
            url,
            impersonate='chrome124',
            headers={
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': referer,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.text
    except ImportError:
        # Fallback: plain urllib (will 403 on Cloudflare-protected pages)
        import urllib.request as _ur
        req = _ur.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': referer,
        })
        with _ur.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')


def search_khinsider(query: str, limit: int = 10) -> List[Dict]:
    """
    Search KHInsider for soundtrack albums matching query.
    Returns list of {name, url}.
    """
    from bs4 import BeautifulSoup
    url = KH_BASE + '/search?search=' + urllib.parse.quote(query)
    try:
        html = _kh_get(url)
    except Exception as e:
        log.warning('KHInsider search error: %s', e)
        return []

    soup = BeautifulSoup(html, 'html.parser')
    results: List[Dict] = []
    seen: set = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/game-soundtracks/album/' not in href:
            continue
        path = href if href.startswith('http') else KH_BASE + href
        name = a.get_text(strip=True)
        if not name or len(name) > 250 or path in seen:
            continue
        seen.add(path)
        results.append({'name': name, 'url': path})
        if len(results) >= limit:
            break
    return results


def fetch_khinsider_tracks(album_url: str) -> Tuple[str, List[Dict]]:
    """
    Fetch the track list from a KHInsider album page.
    Returns (album_name, [{title, detail_url}]).
    """
    from bs4 import BeautifulSoup
    try:
        html = _kh_get(album_url)
    except Exception as e:
        log.error('KHInsider album fetch error: %s', e)
        return ('', [])

    soup = BeautifulSoup(html, 'html.parser')

    # Album title
    album_name = ''
    title_tag = soup.find('h2', class_='albumTitle') or soup.find('h2')
    if title_tag:
        album_name = title_tag.get_text(strip=True)
    if not album_name and soup.title:
        album_name = soup.title.get_text(strip=True).split('|')[0].split('-')[0].strip()

    song_list = soup.find(id='songlist')
    if not song_list:
        log.error('KHInsider: no #songlist found on %s', album_url)
        return (album_name, [])

    tracks: List[Dict] = []
    seen: set = set()
    for a in song_list.find_all('a', href=True):
        href = a['href']
        if 'mp3' not in href.lower() and 'flac' not in href.lower() and 'ogg' not in href.lower():
            continue
        detail_url = href if href.startswith('http') else KH_BASE + href
        if detail_url in seen:
            continue
        seen.add(detail_url)
        title = a.get_text(strip=True) or 'Unknown'
        tracks.append({'title': title, 'detail_url': detail_url})

    return (album_name, tracks)


def _resolve_kh_direct_url(detail_url: str) -> Optional[str]:
    """
    Fetch the per-track KHInsider page and extract the direct CDN download URL.
    """
    from bs4 import BeautifulSoup
    try:
        html = _kh_get(detail_url, referer=KH_BASE)
    except Exception:
        return None

    soup = BeautifulSoup(html, 'html.parser')

    # Prioritize: FLAC > WAV > MP3 via <audio src=...>
    for fmt in ('flac', 'wav', 'mp3', 'ogg'):
        audio = soup.find('audio', src=lambda x: x and fmt in x.lower() if x else False)
        if audio and audio.get('src'):
            return audio['src']

    # Fallback: any audio tag
    audio = soup.find('audio')
    if audio and audio.get('src'):
        return audio['src']

    return None


def download_khinsider_album(
    album_url: str,
    dest_dir: str,
    state: Dict,
    stop_event: threading.Event,
    cached_tracks: Optional[tuple] = None,
) -> bool:
    """
    Download all tracks from a KHInsider album page into dest_dir.
    Updates state dict in-place for progress reporting:
      state['total'], state['done'], state['failed'], state['status'],
      state['current'], state['album'].
    Returns True if fully successful (no failures).
    """
    import subprocess

    if cached_tracks is not None:
        album_name, tracks = cached_tracks
    else:
        album_name, tracks = fetch_khinsider_tracks(album_url)

    if not tracks:
        state['status'] = 'error'
        state['error']  = 'No tracks found on album page — the URL may be wrong.'
        return False

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
            print(f'KH RESOLVE FAIL: {track["title"]} detail={track["detail_url"]}', file=sys.stderr, flush=True)
            state['failed'] += 1
            time.sleep(0.5)
            continue

        # Build a safe filename from the URL's basename
        raw_fname = urllib.parse.unquote(direct_url.split('/')[-1].split('?')[0])
        safe_fname = re.sub(r'[^\w\s\-.]', '_', raw_fname)[:160].strip()
        if not safe_fname:
            safe_fname = re.sub(r'[^\w\s\-]', '_', track['title'])[:80] + '.mp3'
        out_path = os.path.join(dest_dir, safe_fname)

        # curl handles --create-dirs (mkdir) and download in one shot,
        # bypassing Python.app sandbox restrictions on external volumes
        r = subprocess.run([
            'curl', '-fsSL',
            '--create-dirs',
            '-o', out_path,
            '--referer', album_url,
            '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            direct_url,
        ], capture_output=True, text=True)

        if r.returncode == 0:
            state['done'] += 1
            print(f'KH OK: {safe_fname}', file=sys.stderr, flush=True)
        else:
            print(f'KH CURL FAIL: {track["title"]} rc={r.returncode} err={r.stderr.strip()!r} url={direct_url}', file=sys.stderr, flush=True)
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
        if seeds < 2:
            continue
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

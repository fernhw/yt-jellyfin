#!/opt/homebrew/bin/python3
"""scrapeYT.py - Scrape YouTube channel artwork + metadata for Jellyfin

Generates tvshow.nfo, folder.jpg, poster.jpg, banner.jpg per channel folder.
Matches folder names using the same normalize+filter logic as getyt.sh.

Usage:
  scrapeYT.py <channel_url> <yt_root> [--filter FILTER_FILE]

Exit codes: 0=scraped/success, 1=error, 2=already scraped (skipped)
"""

import sys
import os
import json
import re
import urllib.request
import urllib.error
import argparse
import time
import subprocess

MAX_CHANNEL = 50


def fetch_page(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode('utf-8', errors='replace')


def extract_initial_data(html):
    """Extract ytInitialData JSON from channel page HTML."""
    for pattern in [
        r'var\s+ytInitialData\s*=\s*(\{.*?\});\s*</script>',
        r'ytInitialData\s*=\s*(\{.*?\});\s*</script>',
        r'window\[\"ytInitialData\"\]\s*=\s*(\{.*?\});\s*</script>',
    ]:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    return None


def best_thumbnail(thumbnails):
    """Return URL of highest resolution thumbnail."""
    if not thumbnails:
        return None
    best = max(thumbnails, key=lambda t: (t.get('width', 0) or 0) * (t.get('height', 0) or 0))
    url = best.get('url', '')
    # Upgrade to full resolution
    url = re.sub(r'=s\d+', '=s0', url)
    return url or None


def extract_channel_info(data):
    """Pull channel metadata + image URLs from ytInitialData."""
    info = {}

    # --- channelMetadataRenderer (most reliable for name/desc) ---
    meta = data.get('metadata', {}).get('channelMetadataRenderer', {})
    info['title'] = meta.get('title', '')
    info['description'] = meta.get('description', '')
    info['channel_id'] = meta.get('externalId', '')
    info['channel_url'] = meta.get('channelUrl', '')

    header = data.get('header', {})

    # --- c4TabbedHeaderRenderer (classic layout) ---
    c4 = header.get('c4TabbedHeaderRenderer', {})
    if c4:
        info['avatar_url'] = best_thumbnail(
            c4.get('avatar', {}).get('thumbnails', []))
        info['banner_url'] = best_thumbnail(
            c4.get('banner', {}).get('thumbnails', []))
        if not info.get('banner_url'):
            info['banner_url'] = best_thumbnail(
                c4.get('tvBanner', {}).get('thumbnails', []))

    # --- pageHeaderRenderer (newer layout, 2024+) ---
    ph = header.get('pageHeaderRenderer', {})
    if ph:
        content = (ph.get('content', {})
                     .get('pageHeaderViewModel', {}))
        # avatar
        if not info.get('avatar_url'):
            sources = (content.get('image', {})
                              .get('decoratedAvatarViewModel', {})
                              .get('avatar', {})
                              .get('avatarViewModel', {})
                              .get('image', {})
                              .get('sources', []))
            if sources:
                info['avatar_url'] = best_thumbnail(sources)
        # banner
        if not info.get('banner_url'):
            sources = (content.get('banner', {})
                              .get('imageBannerViewModel', {})
                              .get('image', {})
                              .get('sources', []))
            if sources:
                info['banner_url'] = best_thumbnail(sources)

    return info


def normalize(name, max_len=MAX_CHANNEL):
    """Match getyt.sh normalize(): strip special chars, spaces→underscores."""
    result = re.sub(r'[^a-zA-Z0-9 _-]', '', name)
    result = re.sub(r' +', ' ', result).strip()
    result = result.replace(' ', '_')
    result = result[:max_len]
    return result or 'Untitled'


def apply_filter(name, filter_file):
    """Match getyt.sh apply_filter(): channel name remapping."""
    if not filter_file or not os.path.exists(filter_file):
        return name
    with open(filter_file, encoding='utf-8') as f:
        for line in f:
            if '->' in line and not line.lstrip().startswith('#'):
                parts = line.split('->', 1)
                if len(parts) == 2 and parts[0].strip().lower() == name.lower():
                    return parts[1].strip()
    return name


def download_image(url, filepath):
    """Download image from URL to filepath. Falls back to curl on SSL errors."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                           'AppleWebKit/537.36',
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            if len(data) < 500:
                return False
            with open(filepath, 'wb') as f:
                f.write(data)
        return True
    except Exception:
        # Fallback to curl for SSL compatibility
        try:
            result = subprocess.run(
                ['curl', '-sL', '-o', filepath, '-A',
                 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                 url],
                timeout=30, capture_output=True)
            if result.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) >= 500:
                return True
            if os.path.exists(filepath):
                os.remove(filepath)
            return False
        except Exception as e:
            print(f"  WARN: {os.path.basename(filepath)}: {e}", file=sys.stderr)
            return False


def write_nfo(info, filepath):
    """Write Jellyfin-compatible tvshow.nfo."""
    def esc(s):
        return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    nfo = f"""<?xml version="1.0" encoding="UTF-8"?>
<tvshow>
  <title>{esc(info.get('title', ''))}</title>
  <plot>{esc(info.get('description', ''))}</plot>
  <uniqueid type="youtube">{esc(info.get('channel_id', ''))}</uniqueid>
  <genre>YouTube</genre>
  <studio>YouTube</studio>
</tvshow>
"""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(nfo)


def folder_from_handle(channel_url, filter_file=None):
    """Derive probable folder name from URL handle without any network request."""
    clean = channel_url.rstrip('/')
    for suffix in ('/videos', '/streams', '/shorts', '/playlists', '/featured'):
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break
    # Extract @Handle from URL
    match = re.search(r'@([^/]+)', clean)
    if match:
        handle = match.group(1)
        filtered = apply_filter(handle, filter_file)
        return normalize(filtered)
    return None


def scrape_channel(channel_url, yt_root, filter_file=None, force=False):
    """Scrape a single channel. Returns (folder_name, status_str)."""
    # Clean URL
    clean = channel_url.rstrip('/')
    for suffix in ('/videos', '/streams', '/shorts', '/playlists', '/featured'):
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break

    print(f"  fetching {clean} ...")
    try:
        html = fetch_page(clean)
    except Exception as e:
        return None, f"ERROR: fetch failed: {e}"

    data = extract_initial_data(html)
    if not data:
        return None, "ERROR: no ytInitialData found"

    info = extract_channel_info(data)
    if not info.get('title'):
        return None, "ERROR: no channel title found"

    # Resolve folder name (same logic as getyt.sh)
    filtered = apply_filter(info['title'], filter_file)
    folder_name = normalize(filtered)
    folder_path = os.path.join(yt_root, folder_name)

    print(f"  channel: {info['title']} -> {folder_name}/")

    # Double-check after resolving real name (handle may differ from channel name)
    nfo_path = os.path.join(folder_path, 'tvshow.nfo')
    if os.path.exists(nfo_path) and not force:
        return folder_name, "SKIP (already scraped)"

    # Create folder if needed
    os.makedirs(folder_path, exist_ok=True)

    # Download artwork
    got = []
    if download_image(info.get('avatar_url'), os.path.join(folder_path, 'folder.jpg')):
        got.append('folder.jpg')
        # Copy avatar as poster too
        download_image(info.get('avatar_url'), os.path.join(folder_path, 'poster.jpg'))
        got.append('poster.jpg')

    if download_image(info.get('banner_url'), os.path.join(folder_path, 'banner.jpg')):
        got.append('banner.jpg')

    # Write NFO
    write_nfo(info, nfo_path)
    got.append('tvshow.nfo')

    return folder_name, f"SCRAPED ({', '.join(got)})"


def append_handle(channel_url, yt_root):
    """Record handle in .scraped_handles so shell can skip next time."""
    match = re.search(r'@([^/]+)', channel_url)
    if not match:
        return
    handle = match.group(1).lower()
    handles_file = os.path.join(yt_root, '.scraped_handles')
    existing = set()
    if os.path.exists(handles_file):
        with open(handles_file, encoding='utf-8') as f:
            existing = {line.strip().lower() for line in f}
    if handle not in existing:
        with open(handles_file, 'a', encoding='utf-8') as f:
            f.write(handle + '\n')


def main():
    parser = argparse.ArgumentParser(description='Scrape YouTube channel metadata for Jellyfin')
    parser.add_argument('channel_url', nargs='+', help='YouTube channel URL(s)')
    parser.add_argument('--yt-root', dest='yt_root', required=True,
                        help='Root directory for YT library (e.g. /Volumes/Darrel4tb/YT)')
    parser.add_argument('--filter', dest='filter_file', default=None,
                        help='Path to filterYT.md for channel name remapping')
    parser.add_argument('--force', action='store_true',
                        help='Re-scrape even if already scraped')
    args = parser.parse_args()

    errors = 0
    for url in args.channel_url:
        folder_name, status = scrape_channel(
            url, args.yt_root,
            filter_file=args.filter_file, force=args.force)
        print(f"  {status}")
        if status.startswith('ERROR'):
            errors += 1
        else:
            # Record handle so shell skips this channel next run
            append_handle(url, args.yt_root)
        if not status.startswith('SKIP') and not status.startswith('ERROR'):
            time.sleep(1)

    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()

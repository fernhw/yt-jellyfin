#!/usr/bin/env python3
"""collageSeasons.py - Generate season posters + channel backdrops for Jellyfin

Season posters: season##-poster.jpg (1000x1500, 2:3)
  - Collage of best episode thumbs with channel-color gradient overlay
  - Channel logo in corner, season number in white text
  - Layouts: 1=single, 2=split, 3=hero+2, 4=quad, 5+=hero+4

Backdrops: backdrop.jpg (1920x1080)
  - Blended montage of up to 6 best episode thumbs
  - Channel-color gradient overlay for cohesion

Usage:
  python3 collageSeasons.py              # generate all missing/outdated
  python3 collageSeasons.py --force      # regenerate everything
  python3 collageSeasons.py --dry-run    # preview what would be built
  python3 collageSeasons.py --backdrops  # only generate backdrops
  python3 collageSeasons.py --seasons    # only generate season posters
"""

import os
import sys
import glob
import hashlib
import re
import subprocess
import struct

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Read YT_ROOT from locations.md
YT_ROOT = '/Volumes/Darrel4tb/YT'  # default
_loc = os.path.join(SCRIPT_DIR, 'locations.md')
if os.path.isfile(_loc):
    with open(_loc) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line.startswith('YT_ROOT='):
                YT_ROOT = _line.split('=', 1)[1].strip()
                break
POSTER_W, POSTER_H = 1000, 1500
BACKDROP_W, BACKDROP_H = 1920, 1080
THUMB_W, THUMB_H = 1920, 1080
FONT_BOLD = '/System/Library/Fonts/Supplemental/Arial Bold.ttf'

# --- Color extraction ---

# Vibrant fallback palette keyed by name hash (avoids skin tones)
FALLBACK_PALETTE = [
    '#E53935', '#D81B60', '#8E24AA', '#5E35B1',
    '#3949AB', '#1E88E5', '#00ACC1', '#00897B',
    '#43A047', '#7CB342', '#FFB300', '#FB8C00',
    '#F4511E', '#6D4C41', '#546E7A', '#EC407A',
    '#AB47BC', '#7E57C2', '#5C6BC0', '#42A5F5',
    '#26C6DA', '#26A69A', '#66BB6A', '#FFCA28',
    '#FFA726', '#FF7043', '#78909C', '#29B6F6',
]

def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(r, g, b):
    return f'#{r:02x}{g:02x}{b:02x}'

def color_from_name_hash(name):
    """Deterministic vibrant color from channel name."""
    h = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)
    return FALLBACK_PALETTE[h % len(FALLBACK_PALETTE)]

def is_skin_tone(r, g, b):
    """Detect likely skin tones to avoid using them as brand colors."""
    # HSV-based skin detection
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx == 0:
        return False
    s = (mx - mn) / mx
    # Skin tones: warm hue, moderate saturation, not too dark/bright
    if r > g > b and s < 0.6 and 60 < r < 240:
        ratio = g / max(r, 1)
        if 0.5 < ratio < 0.9:
            return True
    return False

def is_vibrant(r, g, b):
    """Check if a color has enough saturation and brightness to pop."""
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx == 0:
        return False
    saturation = (mx - mn) / mx
    brightness = mx / 255
    # Need decent saturation and not too dark
    return saturation > 0.3 and brightness > 0.25 and brightness < 0.95

def extract_dominant_color(image_path):
    """Extract dominant vibrant color from an image using ImageMagick k-means."""
    if not os.path.exists(image_path):
        return None
    try:
        # Get top 8 colors, pick the most vibrant non-skin one
        result = subprocess.run(
            ['magick', image_path, '-resize', '100x100!', '-colors', '8',
             '-format', '%c', 'histogram:info:'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None

        colors = []
        for line in result.stdout.strip().split('\n'):
            # Parse: "  12345: (R,G,B, ...) #RRGGBB ..."
            m = re.search(r'(\d+):\s*\((\d+),(\d+),(\d+)', line)
            if m:
                count = int(m.group(1))
                r, g, b = int(m.group(2)), int(m.group(3)), int(m.group(4))
                colors.append((count, r, g, b))

        # Sort by pixel count (descending) - most dominant first
        colors.sort(key=lambda x: x[0], reverse=True)

        # Pick the first vibrant non-skin color
        for count, r, g, b in colors:
            if is_vibrant(r, g, b) and not is_skin_tone(r, g, b):
                return rgb_to_hex(r, g, b)

        # If nothing vibrant, pick highest-saturation color
        best = None
        best_sat = 0
        for count, r, g, b in colors:
            mx = max(r, g, b)
            mn = min(r, g, b)
            sat = (mx - mn) / max(mx, 1)
            if sat > best_sat and not is_skin_tone(r, g, b):
                best_sat = sat
                best = rgb_to_hex(r, g, b)
        return best

    except Exception:
        return None

def get_channel_color(channel_dir, channel_name):
    """Get brand color for channel: extract from logo, fallback to name hash."""
    # Try folder.jpg (avatar/logo) first
    logo = os.path.join(channel_dir, 'folder.jpg')
    if os.path.exists(logo):
        color = extract_dominant_color(logo)
        if color:
            return color
    # Try poster.jpg
    poster = os.path.join(channel_dir, 'poster.jpg')
    if os.path.exists(poster):
        color = extract_dominant_color(poster)
        if color:
            return color
    # Fallback: deterministic hash color
    return color_from_name_hash(channel_name)

def darker_shade(hex_color, factor=0.4):
    """Return a darker version of the color for gradient endpoint."""
    r, g, b = hex_to_rgb(hex_color)
    return rgb_to_hex(int(r * factor), int(g * factor), int(b * factor))

# --- Thumbnail scoring ---

def score_thumb(path):
    """Score by visual contrast (std dev of grayscale). Higher = more interesting."""
    try:
        result = subprocess.run(
            ['magick', path, '-colorspace', 'Gray',
             '-format', '%[fx:standard_deviation*1000]', 'info:'],
            capture_output=True, text=True, timeout=10
        )
        return int(float(result.stdout.strip()))
    except Exception:
        return 0

def pick_best_thumbs(thumb_paths, n):
    """Return top N thumbnails by visual score."""
    scored = [(score_thumb(p), p) for p in thumb_paths]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:n]]

# --- ImageMagick helpers ---

def magick(*args):
    """Run magick command, return True on success."""
    try:
        result = subprocess.run(['magick'] + list(args),
                                capture_output=True, timeout=30)
        return result.returncode == 0
    except Exception:
        return False

def crop_fill(src, w, h, dst):
    """Resize+crop to fill exact dimensions."""
    return magick(src, '-resize', f'{w}x{h}^',
                  '-gravity', 'center', '-extent', f'{w}x{h}', dst)

# --- Season poster generation ---

def build_season_poster(thumbs, channel_dir, channel_name, season_num, out_path, color):
    """Build a season poster with collage + gradient + logo + text."""
    W, H = POSTER_W, POSTER_H
    n = len(thumbs)
    dark = darker_shade(color, 0.15)

    tmp_base = f'/tmp/collage_{channel_name}_{season_num}'
    tmp_collage = f'{tmp_base}_raw.jpg'

    # --- Build raw collage ---
    if n == 1:
        crop_fill(thumbs[0], W, H, tmp_collage)
    elif n == 2:
        hw = W // 2
        t1 = f'{tmp_base}_t1.jpg'
        t2 = f'{tmp_base}_t2.jpg'
        crop_fill(thumbs[0], hw, H, t1)
        crop_fill(thumbs[1], hw, H, t2)
        magick(t1, t2, '+append', tmp_collage)
        _cleanup(t1, t2)
    elif n == 3:
        top_h = 900
        bot_h = H - top_h
        bot_w = W // 2
        t1 = f'{tmp_base}_t1.jpg'
        t2 = f'{tmp_base}_t2.jpg'
        t3 = f'{tmp_base}_t3.jpg'
        crop_fill(thumbs[0], W, top_h, t1)
        crop_fill(thumbs[1], bot_w, bot_h, t2)
        crop_fill(thumbs[2], bot_w, bot_h, t3)
        bot_row = f'{tmp_base}_bot.jpg'
        magick(t2, t3, '+append', bot_row)
        magick(t1, bot_row, '-append', tmp_collage)
        _cleanup(t1, t2, t3, bot_row)
    elif n == 4:
        cw, ch = W // 2, H // 2
        tiles = []
        for i, t in enumerate(thumbs):
            out = f'{tmp_base}_t{i}.jpg'
            crop_fill(t, cw, ch, out)
            tiles.append(out)
        row1 = f'{tmp_base}_row1.jpg'
        row2 = f'{tmp_base}_row2.jpg'
        magick(tiles[0], tiles[1], '+append', row1)
        magick(tiles[2], tiles[3], '+append', row2)
        magick(row1, row2, '-append', tmp_collage)
        _cleanup(*tiles, row1, row2)
    else:
        top_h = 900
        bot_h = H - top_h
        bot_w = W // 4
        t0 = f'{tmp_base}_t0.jpg'
        crop_fill(thumbs[0], W, top_h, t0)
        bot_tiles = []
        for i in range(1, 5):
            out = f'{tmp_base}_b{i}.jpg'
            crop_fill(thumbs[i], bot_w, bot_h, out)
            bot_tiles.append(out)
        bot_row = f'{tmp_base}_bot.jpg'
        magick(*bot_tiles, '+append', bot_row)
        magick(t0, bot_row, '-append', tmp_collage)
        _cleanup(t0, *bot_tiles, bot_row)

    if not os.path.exists(tmp_collage):
        return False

    # --- Apply gradient overlay (bottom-up, channel color) ---
    # Subtle top tint + strong bottom band for text readability
    tmp_overlay = f'{tmp_base}_over.jpg'
    magick(
        tmp_collage,
        # Bottom gradient: solid color band rising ~35% from bottom
        '(', '-size', f'{W}x{H}', 'xc:none',
        '(', '-size', f'{W}x{int(H*0.35)}',
        f'gradient:none-{color}DD', ')',
        '-gravity', 'South', '-composite', ')',
        '-compose', 'Over', '-composite',
        # Top tint: very light color wash across top 40%
        '(', '-size', f'{W}x{H}', 'xc:none',
        '(', '-size', f'{W}x{int(H*0.4)}',
        f'gradient:{color}77-none', ')',
        '-gravity', 'North', '-composite', ')',
        '-compose', 'Over', '-composite',
        tmp_overlay
    )

    if not os.path.exists(tmp_overlay):
        tmp_overlay = tmp_collage  # fallback: no gradient

    # --- Composite logo in top-left corner ---
    logo_path = os.path.join(channel_dir, 'folder.jpg')
    tmp_with_logo = f'{tmp_base}_logo.jpg'
    if os.path.exists(logo_path):
        # Circular logo with white border, 120px (YouTube-style)
        tmp_resized = f'{tmp_base}_rsz.png'
        tmp_mask = f'{tmp_base}_mask.png'
        tmp_masked = f'{tmp_base}_masked.png'
        tmp_logo = f'{tmp_base}_circ.png'
        # Resize logo to 110x110 square
        magick(logo_path, '-resize', '110x110^',
               '-gravity', 'center', '-extent', '110x110', tmp_resized)
        # Create circle mask (white circle on black)
        magick('-size', '110x110', 'xc:black',
               '-fill', 'white', '-draw', 'circle 55,55 55,1', tmp_mask)
        # Apply circle mask via composite (preserves colors)
        subprocess.run(['magick', 'composite', '-compose', 'CopyOpacity',
                        tmp_mask, tmp_resized, tmp_masked],
                       capture_output=True, timeout=10)
        # Extend canvas and add white border ring
        magick(tmp_masked,
               '-background', 'none', '-gravity', 'center', '-extent', '120x120',
               '(', '-size', '120x120', 'xc:none',
               '-fill', 'none', '-stroke', 'white', '-strokewidth', '3',
               '-draw', 'circle 60,60 60,3', ')',
               '-compose', 'DstOver', '-composite',
               tmp_logo)
        _cleanup(tmp_resized, tmp_mask, tmp_masked)
        if os.path.exists(tmp_logo):
            magick(tmp_overlay, tmp_logo,
                   '-gravity', 'NorthWest', '-geometry', '+30+30',
                   '-compose', 'Over', '-composite', tmp_with_logo)
            _cleanup(tmp_logo)
        else:
            tmp_with_logo = tmp_overlay
    else:
        tmp_with_logo = tmp_overlay

    if not os.path.exists(tmp_with_logo):
        tmp_with_logo = tmp_overlay

    # --- Season text overlay ---
    season_label = f'Season {season_num}'
    magick(
        tmp_with_logo,
        # Shadow
        '-gravity', 'SouthWest',
        '-font', FONT_BOLD, '-pointsize', '72',
        '-fill', f'{dark}BB', '-annotate', '+42+42', season_label,
        # Main text
        '-fill', 'white', '-annotate', '+40+44', season_label,
        '-quality', '92', out_path
    )

    _cleanup(tmp_collage, tmp_overlay)
    if tmp_with_logo != tmp_overlay:
        _cleanup(tmp_with_logo)

    return os.path.exists(out_path)


# --- Backdrop generation ---

def build_backdrop(channel_dir, channel_name, color):
    """Generate backdrop.jpg from up to 6 best episode thumbs with gradient."""
    W, H = BACKDROP_W, BACKDROP_H
    out_path = os.path.join(channel_dir, 'backdrop.jpg')
    dark = darker_shade(color, 0.2)

    # Collect all episode thumbs
    all_thumbs = sorted(glob.glob(os.path.join(channel_dir, '*-thumb.jpg')))
    if not all_thumbs:
        return False

    best = pick_best_thumbs(all_thumbs, min(6, len(all_thumbs)))
    n = len(best)

    tmp_base = f'/tmp/backdrop_{channel_name}'
    tmp_raw = f'{tmp_base}_raw.jpg'

    if n == 1:
        crop_fill(best[0], W, H, tmp_raw)
    elif n == 2:
        hw = W // 2
        t1 = f'{tmp_base}_t0.jpg'
        t2 = f'{tmp_base}_t1.jpg'
        crop_fill(best[0], hw, H, t1)
        crop_fill(best[1], hw, H, t2)
        magick(t1, t2, '+append', tmp_raw)
        _cleanup(t1, t2)
    elif n == 3:
        tw = W // 3
        tiles = []
        for i, t in enumerate(best):
            out = f'{tmp_base}_t{i}.jpg'
            crop_fill(t, tw, H, out)
            tiles.append(out)
        magick(*tiles, '+append', tmp_raw)
        _cleanup(*tiles)
    elif n <= 4:
        cw, ch = W // 2, H // 2
        tiles = []
        for i, t in enumerate(best):
            out = f'{tmp_base}_t{i}.jpg'
            crop_fill(t, cw, ch, out)
            tiles.append(out)
        while len(tiles) < 4:
            tiles.append(tiles[-1])
        row1 = f'{tmp_base}_row1.jpg'
        row2 = f'{tmp_base}_row2.jpg'
        magick(tiles[0], tiles[1], '+append', row1)
        magick(tiles[2], tiles[3], '+append', row2)
        magick(row1, row2, '-append', tmp_raw)
        _cleanup(*tiles, row1, row2)
    else:
        # 2 rows of 3 for 5-6 thumbs
        tw = W // 3
        rh = H // 2
        tiles = []
        for i, t in enumerate(best[:6]):
            out = f'{tmp_base}_t{i}.jpg'
            crop_fill(t, tw, rh, out)
            tiles.append(out)
        while len(tiles) < 6:
            tiles.append(tiles[-1])
        row1 = f'{tmp_base}_row1.jpg'
        row2 = f'{tmp_base}_row2.jpg'
        magick(tiles[0], tiles[1], tiles[2], '+append', row1)
        magick(tiles[3], tiles[4], tiles[5], '+append', row2)
        magick(row1, row2, '-append', tmp_raw)
        _cleanup(*tiles, row1, row2)

    if not os.path.exists(tmp_raw):
        return False

    # Gaussian blur for dreamy backdrop feel, then gradient overlay
    tmp_blur = f'{tmp_base}_blur.jpg'
    magick(tmp_raw, '-blur', '0x8', '-brightness-contrast', '-10x5', tmp_blur)

    if not os.path.exists(tmp_blur):
        tmp_blur = tmp_raw

    # Channel-color gradient: edges darken with channel color
    # Left-right + bottom vignette
    tmp_graded = f'{tmp_base}_graded.jpg'
    magick(
        tmp_blur,
        # Left edge gradient
        '(', '-size', f'{W}x{H}',
        f'gradient:{color}99-none',
        '-rotate', '-90', ')',
        '-compose', 'Multiply', '-composite',
        # Right edge gradient
        '(', '-size', f'{W}x{H}',
        f'gradient:none-{color}66',
        '-rotate', '-90', ')',
        '-compose', 'Multiply', '-composite',
        # Bottom gradient
        '(', '-size', f'{W}x{H}',
        f'gradient:none-{dark}BB',
        ')',
        '-compose', 'Multiply', '-composite',
        tmp_graded
    )

    if not os.path.exists(tmp_graded):
        tmp_graded = tmp_blur

    # --- Circular logo in bottom-right corner ---
    logo_path = os.path.join(channel_dir, 'folder.jpg')
    if os.path.exists(logo_path):
        tmp_rsz = f'{tmp_base}_rsz.png'
        tmp_msk = f'{tmp_base}_msk.png'
        tmp_mskd = f'{tmp_base}_mskd.png'
        tmp_circ = f'{tmp_base}_circ.png'
        # 90px circular logo with white ring
        magick(logo_path, '-resize', '80x80^',
               '-gravity', 'center', '-extent', '80x80', tmp_rsz)
        magick('-size', '80x80', 'xc:black',
               '-fill', 'white', '-draw', 'circle 40,40 40,1', tmp_msk)
        subprocess.run(['magick', 'composite', '-compose', 'CopyOpacity',
                        tmp_msk, tmp_rsz, tmp_mskd],
                       capture_output=True, timeout=10)
        magick(tmp_mskd,
               '-background', 'none', '-gravity', 'center', '-extent', '90x90',
               '(', '-size', '90x90', 'xc:none',
               '-fill', 'none', '-stroke', 'white', '-strokewidth', '2',
               '-draw', 'circle 45,45 45,2', ')',
               '-compose', 'DstOver', '-composite', tmp_circ)
        _cleanup(tmp_rsz, tmp_msk, tmp_mskd)
        if os.path.exists(tmp_circ):
            magick(tmp_graded, tmp_circ,
                   '-gravity', 'SouthEast', '-geometry', '+40+40',
                   '-compose', 'Over', '-composite',
                   '-quality', '90', out_path)
            _cleanup(tmp_circ)
        else:
            magick(tmp_graded, '-quality', '90', out_path)
    else:
        magick(tmp_graded, '-quality', '90', out_path)

    _cleanup(tmp_raw, tmp_blur, tmp_graded)
    return os.path.exists(out_path)


# --- Thumb generation (series horizontal thumbnail for Jellyfin) ---

def build_thumb(channel_dir, channel_name, color):
    """Generate thumb.jpg (1920x1080) from banner + avatar + channel color.

    Layout: channel-color gradient background, banner across top half,
    large circular avatar centered, channel name below.
    """
    W, H = THUMB_W, THUMB_H
    out_path = os.path.join(channel_dir, 'thumb.jpg')
    dark = darker_shade(color, 0.15)

    banner_path = os.path.join(channel_dir, 'banner.jpg')
    logo_path = os.path.join(channel_dir, 'folder.jpg')

    # Need at least folder.jpg
    if not os.path.exists(logo_path):
        return False

    tmp_base = f'/tmp/thumb_{channel_name}'

    # 1) Solid channel-color gradient canvas (top lighter, bottom darker)
    tmp_canvas = f'{tmp_base}_canvas.jpg'
    magick(
        '-size', f'{W}x{H}',
        f'gradient:{color}-{dark}',
        tmp_canvas
    )
    if not os.path.exists(tmp_canvas):
        return False

    # 2) If banner exists, overlay it across the top ~40% with fade-out
    tmp_with_banner = f'{tmp_base}_banner.jpg'
    if os.path.exists(banner_path):
        banner_h = int(H * 0.4)  # 432px
        tmp_ban_resized = f'{tmp_base}_ban_rsz.jpg'
        # Stretch banner to full width, crop to banner_h
        crop_fill(banner_path, W, banner_h, tmp_ban_resized)

        # Create a fade mask: white at top, transparent at bottom
        tmp_fade = f'{tmp_base}_fade.png'
        magick(
            '-size', f'{W}x{banner_h}',
            'gradient:white-none',
            tmp_fade
        )

        # Apply fade mask to banner
        tmp_ban_faded = f'{tmp_base}_ban_faded.png'
        magick(
            tmp_ban_resized,
            tmp_fade,
            '-compose', 'CopyOpacity', '-composite',
            tmp_ban_faded
        )

        # Composite faded banner onto canvas at top
        magick(
            tmp_canvas,
            tmp_ban_faded,
            '-gravity', 'North',
            '-compose', 'Over', '-composite',
            tmp_with_banner
        )
        _cleanup(tmp_ban_resized, tmp_fade, tmp_ban_faded)
    else:
        tmp_with_banner = tmp_canvas

    if not os.path.exists(tmp_with_banner):
        tmp_with_banner = tmp_canvas

    # 3) Large circular avatar centered (350px diameter)
    avatar_size = 350
    ring_size = avatar_size + 16  # white border
    tmp_rsz = f'{tmp_base}_rsz.png'
    tmp_msk = f'{tmp_base}_msk.png'
    tmp_mskd = f'{tmp_base}_mskd.png'
    tmp_circ = f'{tmp_base}_circ.png'

    r = avatar_size // 2
    magick(logo_path, '-resize', f'{avatar_size}x{avatar_size}^',
           '-gravity', 'center', '-extent', f'{avatar_size}x{avatar_size}', tmp_rsz)
    magick('-size', f'{avatar_size}x{avatar_size}', 'xc:black',
           '-fill', 'white', '-draw', f'circle {r},{r} {r},1', tmp_msk)
    subprocess.run(['magick', 'composite', '-compose', 'CopyOpacity',
                    tmp_msk, tmp_rsz, tmp_mskd],
                   capture_output=True, timeout=10)
    rr = ring_size // 2
    magick(tmp_mskd,
           '-background', 'none', '-gravity', 'center',
           '-extent', f'{ring_size}x{ring_size}',
           '(', '-size', f'{ring_size}x{ring_size}', 'xc:none',
           '-fill', 'none', '-stroke', 'white', '-strokewidth', '5',
           '-draw', f'circle {rr},{rr} {rr},3', ')',
           '-compose', 'DstOver', '-composite',
           tmp_circ)
    _cleanup(tmp_rsz, tmp_msk, tmp_mskd)

    # Place avatar above center (offset up by 80px to leave room for text)
    tmp_with_avatar = f'{tmp_base}_avatar.jpg'
    if os.path.exists(tmp_circ):
        magick(
            tmp_with_banner,
            tmp_circ,
            '-gravity', 'Center', '-geometry', '+0-80',
            '-compose', 'Over', '-composite',
            tmp_with_avatar
        )
        _cleanup(tmp_circ)
    else:
        tmp_with_avatar = tmp_with_banner

    if not os.path.exists(tmp_with_avatar):
        tmp_with_avatar = tmp_with_banner

    # 4) Channel name text below avatar
    display_name = channel_name.replace('_', ' ')
    # Shadow + main text
    magick(
        tmp_with_avatar,
        '-gravity', 'Center',
        '-font', FONT_BOLD, '-pointsize', '56',
        '-fill', f'{dark}CC', '-annotate', '+2+182', display_name,
        '-fill', 'white', '-annotate', '+0+180', display_name,
        '-quality', '92', out_path
    )

    _cleanup(tmp_canvas, tmp_with_banner, tmp_with_avatar)
    return os.path.exists(out_path)


def _cleanup(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


# --- Change detection ---

def read_count_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ''

def write_count_file(path, count):
    with open(path, 'w') as f:
        f.write(str(count))

def any_newer(pattern, reference):
    """Return True if any file matching pattern is newer than reference."""
    if not os.path.exists(reference):
        return True
    ref_mtime = os.path.getmtime(reference)
    for f in glob.glob(pattern):
        if os.path.getmtime(f) > ref_mtime:
            return True
    return False


# --- Main ---

def main():
    mode = 'generate'
    do_seasons = True
    do_backdrops = True
    do_thumbs = True

    for arg in sys.argv[1:]:
        if arg == '--force':
            mode = 'force'
        elif arg == '--dry-run':
            mode = 'dry-run'
        elif arg == '--backdrops':
            do_seasons = False
            do_thumbs = False
        elif arg == '--seasons':
            do_backdrops = False
            do_thumbs = False
        elif arg == '--thumbs':
            do_seasons = False
            do_backdrops = False

    if not os.path.isdir(YT_ROOT):
        print(f'ERROR: {YT_ROOT} not mounted')
        sys.exit(1)

    poster_count = 0
    backdrop_count = 0
    thumb_count = 0
    skip_count = 0

    print('--- Season poster collages ---')

    channels = sorted(glob.glob(os.path.join(YT_ROOT, '*/')))

    for channel_dir in channels:
        if not os.path.isdir(channel_dir):
            continue
        channel = os.path.basename(channel_dir.rstrip('/'))

        # Extract channel color once per channel
        color = get_channel_color(channel_dir, channel)

        # --- Thumb (series horizontal thumbnail) ---
        if do_thumbs:
            thumb_path = os.path.join(channel_dir, 'thumb.jpg')
            if mode != 'force' and os.path.exists(thumb_path):
                # Only rebuild if folder.jpg or banner.jpg is newer
                needs_rebuild = False
                for src in ['folder.jpg', 'banner.jpg']:
                    src_path = os.path.join(channel_dir, src)
                    if os.path.exists(src_path) and os.path.getmtime(src_path) > os.path.getmtime(thumb_path):
                        needs_rebuild = True
                        break
                if not needs_rebuild:
                    skip_count += 1
                else:
                    if mode == 'dry-run':
                        print(f'  WOULD BUILD: {channel}/thumb.jpg (updated source)')
                        thumb_count += 1
                    else:
                        ok = build_thumb(channel_dir, channel, color)
                        if ok:
                            print(f'  THUMB: {channel}/thumb.jpg (color: {color})')
                            thumb_count += 1
                        else:
                            print(f'  FAIL: {channel}/thumb.jpg')
            elif not os.path.exists(os.path.join(channel_dir, 'thumb.jpg')):
                if mode == 'dry-run':
                    print(f'  WOULD BUILD: {channel}/thumb.jpg')
                    thumb_count += 1
                else:
                    ok = build_thumb(channel_dir, channel, color)
                    if ok:
                        print(f'  THUMB: {channel}/thumb.jpg (color: {color})')
                        thumb_count += 1
                    else:
                        print(f'  FAIL: {channel}/thumb.jpg')

        # Get all thumbs in this channel
        all_thumbs = sorted(glob.glob(os.path.join(channel_dir, '*-thumb.jpg')))
        if not all_thumbs:
            continue

        # Channel color already extracted above

        # --- Season posters ---
        if do_seasons:
            # Find seasons (anchor to _S##E to avoid PS5 etc)
            seasons = set()
            for t in all_thumbs:
                m = re.search(r'_S(\d+)E', os.path.basename(t))
                if m:
                    seasons.add(m.group(1))

            for season_num in sorted(seasons):
                season_tag = f'S{season_num}'
                poster_path = os.path.join(channel_dir, f'season{season_num}-poster.jpg')
                count_path = os.path.join(channel_dir, f'season{season_num}-poster.count')

                # Collect thumbs for this season
                season_thumbs = [t for t in all_thumbs
                                 if f'_{season_tag}E' in os.path.basename(t)]
                if not season_thumbs:
                    continue

                thumb_count = len(season_thumbs)

                # Check if rebuild needed
                if mode != 'force' and os.path.exists(poster_path):
                    stored = read_count_file(count_path)
                    if stored == str(thumb_count):
                        if not any_newer(os.path.join(channel_dir, f'*_{season_tag}E*-thumb.jpg'),
                                         poster_path):
                            skip_count += 1
                            continue

                # Pick best thumbs
                n_pick = min(5, thumb_count)
                best = pick_best_thumbs(season_thumbs, n_pick)

                layout_names = {1: 'single', 2: 'split', 3: 'hero+2', 4: 'quad'}
                layout_name = layout_names.get(len(best), f'hero+4 (top {n_pick} of {thumb_count})')

                if mode == 'dry-run':
                    print(f'  WOULD BUILD: {channel}/season{season_num}-poster.jpg '
                          f'({thumb_count} thumbs, {layout_name})')
                    poster_count += 1
                    continue

                ok = build_season_poster(best, channel_dir, channel, season_num,
                                         poster_path, color)
                if ok:
                    write_count_file(count_path, thumb_count)
                    print(f'  POSTER: {channel}/season{season_num}-poster.jpg '
                          f'({thumb_count} eps, {layout_name}, color: {color})')
                    poster_count += 1
                else:
                    print(f'  FAIL: {channel}/season{season_num}-poster.jpg')

        # --- Backdrop ---
        if do_backdrops:
            backdrop_path = os.path.join(channel_dir, 'backdrop.jpg')
            bd_count_path = os.path.join(channel_dir, '.backdrop.count')

            thumb_count = len(all_thumbs)

            if mode != 'force' and os.path.exists(backdrop_path):
                stored = read_count_file(bd_count_path)
                if stored == str(thumb_count):
                    if not any_newer(os.path.join(channel_dir, '*-thumb.jpg'), backdrop_path):
                        skip_count += 1
                        continue

            if mode == 'dry-run':
                print(f'  WOULD BUILD: {channel}/backdrop.jpg '
                      f'({min(6, thumb_count)} of {thumb_count} thumbs)')
                backdrop_count += 1
                continue

            ok = build_backdrop(channel_dir, channel, color)
            if ok:
                write_count_file(bd_count_path, thumb_count)
                print(f'  BACKDROP: {channel}/backdrop.jpg (color: {color})')
                backdrop_count += 1
            else:
                print(f'  FAIL: {channel}/backdrop.jpg')

    print()
    if mode == 'dry-run':
        print(f'  {poster_count} poster(s) + {backdrop_count} backdrop(s) + {thumb_count} thumb(s) would be generated '
              f'({skip_count} up to date)')
    else:
        print(f'  {poster_count} poster(s) + {backdrop_count} backdrop(s) + {thumb_count} thumb(s) generated '
              f'({skip_count} up to date)')
    print('--- Collages complete ---')


if __name__ == '__main__':
    main()

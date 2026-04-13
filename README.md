# yt-jellyfin

YouTube → Jellyfin pipeline. Auto-downloads new videos from subscribed channels, organizes them into a Jellyfin-compatible library with scraped metadata and artwork.

## Structure

```
Channel_Name/
├── tvshow.nfo          # Jellyfin metadata (scraped)
├── folder.jpg          # Channel avatar
├── poster.jpg          # Channel avatar (copy)
├── backdrop.jpg        # Channel banner
├── Older_Video_S25E01.mp4
├── Older_Video_S25E01-thumb.jpg
├── Video_Title_S26E01.mp4
├── Video_Title_S26E01-thumb.jpg
├── Another_Video_S26E02.mp4
└── Another_Video_S26E02-thumb.jpg
```

## Scripts

| Script | Purpose |
|---|---|
| `downloadSubs.sh` | Main runner — scrapes metadata, scans channels, downloads new videos |
| `getyt.sh` | Downloads a single video (or list) with proper naming |
| `scrapeYT.py` | Scrapes channel artwork + NFO for Jellyfin (skips if already done) |
| `rsync_jellyfin.sh` | Backs up Jellyfin data + music library |
| `git-sync.sh` | Auto-commits and pushes config changes |

## Usage

```sh
# Normal run: scrape new channels, scan for new videos, download
./downloadSubs.sh

# Seed DB with existing videos (first run for new channels)
./downloadSubs.sh --init

# Preview what would download
./downloadSubs.sh --dry-run

# Only scrape channel artwork/metadata, no downloads
./downloadSubs.sh --scrape-only

# Download a specific video
./getyt.sh https://www.youtube.com/watch?v=VIDEO_ID

# Scrape a single channel manually
python3 scrapeYT.py "https://www.youtube.com/@Channel" /Volumes/Darrel4tb/YT --filter filterYT.md
```

## Config Files

- **`subscribedTo.md`** — Channel URLs, one per line
- **`channelConfig.md`** — Priority download order + per-channel rolling limits
- **`filterYT.md`** — Channel name → folder name remapping

## Cron

```
# downloadSubs — 14 runs/day at fixed strategic times
# 00:30, 03:30, 07:00, 09:30, 11:30, 15:00, 17:00, 18-22:00 hourly, 23:30
0 * * * *          rsync_jellyfin.sh   # Backup hourly
0 */2 * * *        git-sync.sh         # Auto-commit + push every 2h
```

## Requirements

```sh
brew install yt-dlp ffmpeg sqlite3 python3 imagemagick
```

## Notes

- Library root: `/Volumes/Darrel4tb/YT`
- DB (`ytdb.db`) is source of truth for seen/downloaded videos
- Scraper skips channels that already have `tvshow.nfo` (no network hit)
- Lock file prevents overlapping `downloadSubs.sh` runs
- Videos named `Title_S{YY}E{##}.mp4` — title first for readability, Jellyfin parses season/episode
- Thumbnails: 4 candidate frames extracted (15/35/55/75%), scored by ImageMagick grayscale std deviation, best picked
- All videos stored directly in channel folder (no year subdirectories)

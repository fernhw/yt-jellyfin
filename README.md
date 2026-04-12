# yt-jellyfin

YouTube → Jellyfin pipeline. Auto-downloads new videos from subscribed channels, organizes them into a Jellyfin-compatible library with scraped metadata and artwork.

## Structure

```
Channel_Name/
├── tvshow.nfo          # Jellyfin metadata (scraped)
├── folder.jpg          # Channel avatar
├── poster.jpg          # Channel avatar (copy)
├── backdrop.jpg        # Channel banner
└── 2026/
    ├── S26E01_Video_Title.mp4
    └── S26E02_Another_Video.mp4
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
0 0,6,12,18 * * *  downloadSubs.sh    # Scan + download 4x/day
0 * * * *          rsync_jellyfin.sh   # Backup hourly
0 */2 * * *        git-sync.sh         # Auto-commit + push every 2h
```

## Requirements

```sh
brew install yt-dlp ffmpeg sqlite3 python3
```

## Notes

- Library root: `/Volumes/Darrel4tb/YT`
- DB (`ytdb.db`) is source of truth for seen/downloaded videos
- Scraper skips channels that already have `tvshow.nfo` (no network hit)
- Lock file prevents overlapping `downloadSubs.sh` runs
- Videos named `S{YY}E{##}_{Title}.mp4` for Jellyfin season/episode parsing

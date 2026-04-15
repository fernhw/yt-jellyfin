# yt-jellyfin

> **[What's new today?](dailyReport.md)**

YouTube → Jellyfin pipeline. Auto-downloads new videos from subscribed channels, organizes them into a Jellyfin-compatible library with scraped metadata, artwork, thumbnails, season posters, and daily reports.

## Structure

```
Channel_Name/
├── tvshow.nfo              # Jellyfin metadata (scraped)
├── folder.jpg              # Channel avatar
├── poster.jpg              # Channel avatar (copy)
├── thumb.jpg               # Channel banner collage (avatar + banner + color)
├── backdrop.jpg            # Season backdrop collage
├── season01-poster.jpg     # Season poster collage (hero + grid)
├── Video_Title_S26E01.mp4
├── Video_Title_S26E01-thumb.jpg   # Video thumbnail with title overlay
├── Another_Video_S26E02.mp4
└── Another_Video_S26E02-thumb.jpg
```

## Scripts

| Script | Purpose |
|---|---|
| `downloadSubs.sh` | Main runner — scrapes, scans, downloads, generates thumbnails, collages, reports, auto-commits |
| `getyt.sh` | Downloads a single video (or list) with proper naming |
| `download.sh` | Universal downloader — YouTube, magnets, torrents. `download s` / `download m` / `download sf` |
| `scrapeYT.py` | Scrapes channel artwork + NFO for Jellyfin |
| `collageSeasons.py` | Generates season posters, backdrops, and channel thumb.jpg |
| `reportMaker.sh` | Generates [dailyReport.md](dailyReport.md) — today + yesterday combined, and [todayReport.md](todayReport.md) per-run |
| `normalizeBackNames.sh` | Renames backdrop files to match Jellyfin conventions |
| `rsync_jellyfin.sh` | Backs up Jellyfin data + music library |
| `filterMusic.sh` | Moves audio files under 60s to a mirror folder (non-music cleanup) |
| `generatePlaceholder.sh` | Generates placeholder videos for age-restricted/unavailable content |

## Usage

```sh
# Normal run: scrape → scan → download → thumbs → collages → report → git push
./downloadSubs.sh

# Seed DB with existing videos (first run for new channels)
./downloadSubs.sh --init

# Preview what would download
./downloadSubs.sh --dry-run

# Only scrape channel artwork/metadata, no downloads
./downloadSubs.sh --scrape-only

# Regenerate all video thumbnails (safe to run alongside downloads)
./downloadSubs.sh --thumbs

# Download a specific video (auto-detects channel, auto-scrapes if new)
./getyt.sh https://www.youtube.com/watch?v=VIDEO_ID

# Universal downloader
download VIDEO_ID                           # YouTube by ID
download https://www.youtube.com/watch?v=X  # YouTube by URL
download s                                  # Show torrent (paste magnet, auto-match folder)
download sf                                 # Full show torrent (paste magnet, dump in Shows/)
download m                                  # Movie torrent (paste magnet)
download sc                                 # Show torrent (pick folder interactively)
download --help                             # Show all options
```

## Video Thumbnails

Every video gets a `-thumb.jpg` with a 4-stage fallback:

1. **Scored frames** — 4 frames extracted at 15/35/55/75%, scored by visual complexity, best picked
2. **First frame** — fallback if duration probe fails
3. **YouTube thumbnail** — downloaded from `img.youtube.com`
4. **Text card** — black gradient with white title text, zero dependencies, never fails

All thumbnails get a text overlay: video title (centered, 8-pass shadow halo) + channel name (bottom). Titles are pulled from the DB with HTML entity decoding for proper symbols.

## Season Posters & Collages

`collageSeasons.py` auto-generates artwork per channel per season:
- **Season posters** — hero thumbnail + grid of top episodes (or single/quad for fewer episodes)
- **Backdrops** — color-tinted collage of episode thumbnails
- **Channel thumb.jpg** — banner + avatar + channel color strip

## Config Files

- **`locations.md`** — Storage paths for all scripts. Edit this for your setup:
  ```
  YT_ROOT=/Volumes/Darrel4tb/YT        # YouTube library (HDD)
  MOVIES_DIR=/Volumes/Jellyfin/Movies   # Jellyfin movies (SSD)
  SHOWS_DIR=/Volumes/Jellyfin/Shows     # Jellyfin shows (SSD)
  MUSIC_DIR=/Volumes/Jellyfin/Music     # Music library
  MUSIC_MIRROR_DIR=/Volumes/Jellyfin/non-music
  ```
- **`subscribedTo.md`** — Channel URLs (supports `@handle` and `/channel/UCID` formats)
- **`channelConfig.md`** — Priority download order, per-channel rolling limits, per-channel quality caps
- **`filterYT.md`** — Channel name → folder name remapping

## Daily Report

[dailyReport.md](dailyReport.md) — auto-generated each run. Shows today's uploads and yesterday's side by side.
[todayReport.md](todayReport.md) — current day only, archived to `reportsArchive/YYYYMMDD.md` on day change.

## Database

`ytdb.db` (SQLite) — source of truth:
- **`videos`** — id, url, channel, title, upload_date, download_date, file_path, status
- **`channel_aliases`** — handle → display_name mapping

Statuses: `downloaded`, `age-restricted`, `members-only`, `unavailable`, `no-english`, `failed`, `errored`

## Cron

```
# downloadSubs — 13 runs/day at fixed strategic times
# 00:30, 03:30, 07:00, 09:30, 11:30, 15:00, 17:00, 18-22:00 hourly, 23:30
0 * * * *          rsync_jellyfin.sh   # Backup hourly
```

Each run auto-commits and pushes changes to git.

## Requirements

```sh
brew install yt-dlp ffmpeg sqlite3 python3 imagemagick aria2
pip3 install Pillow
```

## Notes

- Library root: set `YT_ROOT` in `locations.md`
- Lock file prevents overlapping `downloadSubs.sh` runs (`--thumbs` bypasses lock — read-only safe)
- Videos named `Title_S{YY}E{##}.mp4` — title first for readability, Jellyfin parses season/episode
- I/O retry on thumbnail extraction when concurrent downloads cause disk contention
- Placeholder videos generated for age-restricted/members-only content with explanatory text
- Channel scraping supports both `@handle` and raw `/channel/UCID` URLs

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
| `showScheduler.sh` | Interactive setup — register a new series to track and auto-download |
| `showSchedulerSearch.py` | Hourly cron engine — searches Nyaa/TPB, downloads via aria2c, updates CSV |
| `showSchedulerCards.py` | Renders HTML for the web report (today banner + persistent tracker) |
| `showSchedulerReport.sh` | Called by reportMaker after downloads, appends to today's show JSON |
| `showSchedule.csv` | State file — one row per tracked series (episode cursor, window, dedup anchor) |
| `showSchedulerStatus.json` | Generated status snapshot — read by the web tracker |
| `showSchedulerToday.json` | Today's downloaded episodes — cleared on day rollover |

## Show Scheduler

Tracks weekly TV shows and anime. Searches Nyaa (anime) and TPB (live action) on release day, scores by seeders and resolution, downloads the best match via aria2c, and sends a push notification.

### Register a new series

```sh
showScheduler
# Interactive prompts: show name, search name, type (anime/live), season, episode, total, release day
# Example session:
#   Show name:    The Boys
#   Search name:  the boys          ← auto-derived, used for torrent queries
#   Type:         live
#   Season:       5
#   Episode:      6                 ← next episode to download
#   Total:        8
#   Release day:  wed
#   Backfill?     y                 ← downloads eps 1–5 immediately if you're behind

# Non-interactive (all params):
showScheduler --name "Re Zero" --type anime --season 4 --episode 6 --total 13 --days wed
showScheduler --name "Re Zero" --type anime --season 4 --episode 6 --total 13 --days wed --search-name "rezero starting"
```

### Search / download commands

```sh
# What the cron runs every hour — searches all shows with open windows
python3 showSchedulerSearch.py

# Same but never writes to disk or CSV (safe to test)
python3 showSchedulerSearch.py --dry-run

# Show table of all tracked series + current episode/week status
python3 showSchedulerSearch.py --list

# Force-search one specific show right now (bypasses window/day check)
python3 showSchedulerSearch.py --show "The Boys" --force

# Force-search + dry-run (preview what it would pick)
python3 showSchedulerSearch.py --show "The Boys" --force --dry-run

# Download a specific back-episode without advancing the episode counter
python3 showSchedulerSearch.py --show "Re Zero" --force --back-ep 3

# Backfill episodes 1–5 of a show (non-interactive)
for ep in 1 2 3 4 5; do
  python3 showSchedulerSearch.py --show "Re Zero" --force --back-ep $ep
done
```

### How it works

- Cron runs `python3 showSchedulerSearch.py` hourly
- On the show's release day, a 5-day search window opens
- Each hour it searches until a qualifying result is found (1080p+ / 10+ seeds)
- After download: episode counter advances, window closes
- Dedup anchor (`anchor_episode` + `week_anchor`) prevents re-downloading the same week's episode even if you re-run manually

## Usage

```sh
# Normal run: scrape → scan → download → thumbs → collages → report → git push
./downloadSubs.sh

# Seed DB with existing videos (first run for new channels)
./downloadSubs.sh --init

# Preview what would download (no writes)
./downloadSubs.sh --dry-run

# Only scrape channel artwork/metadata, no downloads
./downloadSubs.sh --scrape-only

# Regenerate all video thumbnails (safe to run alongside downloads)
./downloadSubs.sh --thumbs

# Download a specific YouTube video (auto-detects channel, auto-scrapes if new)
./getyt.sh https://www.youtube.com/watch?v=VIDEO_ID
./getyt.sh dQw4w9WgXcQ                          # by video ID only

# Universal downloader (symlinked to /usr/local/bin/download)
download dQw4w9WgXcQ                            # YouTube by ID
download https://www.youtube.com/watch?v=X      # YouTube by URL
download s                                      # Show episode torrent — prompts for magnet, auto-matches folder by name
download sf                                     # Full show torrent — dumps into Shows/ root
download m                                      # Movie torrent — prompts for magnet, drops in Movies/
download sc                                     # Show torrent — interactive folder picker
download --help                                 # All options
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

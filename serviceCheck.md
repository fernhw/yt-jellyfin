# Service Health Checker

Runs hourly via cron. For each enabled service it:

1. Probes the URL.
2. Compares the HTTP code to `expect` (regex).
3. If unhealthy, runs `restart_cmd`, waits `recheck_after`, probes again.
4. Sends a OneSignal push on failure with `Service Down: attempting restart` and the result.

Script: [serviceCheck.sh](serviceCheck.sh) · Log: `/tmp/serviceCheck.log` · State: `/tmp/serviceCheck.state`

State file prevents notification spam — a service must transition `OK -> DOWN` to send a "down" push, or `DOWN -> OK` to send a "recovered" push.

---

## Where each service runs (so future-me does NOT break it)

| Name             | Layer            | Port  | Restart mechanism                                                                 |
| ---------------- | ---------------- | ----- | --------------------------------------------------------------------------------- |
| `gyra`           | local python     | 5050  | kill listener + `nohup python3 app.py` (see repo memory)                          |
| `jellyfin`       | local mac .app   | 8096  | `open -a Jellyfin`                                                                |
| `report`         | launchd agent    | 8765  | `launchctl kickstart -k gui/<uid>/com.alex.report.serve`                          |
| `request`        | launchd agent    | 8770  | `launchctl kickstart -k gui/<uid>/com.fernhw.requestserver`                       |
| `nginx-local`    | docker container | 80    | `docker restart nginx-local` (compose at `docker/nginx/`)                          |
| `audiobookshelf` | docker container | 13378 | `docker restart audiobookshelf` (compose at `docker/audiobookshelf/`)              |
| `nextcloud`      | docker container | 7990  | `docker restart nextcloud-nextcloud-1` (compose at `~/Projects/nextcloud/`)        |
| `onlyoffice`     | docker container | 7991  | `docker restart nextcloud-onlyoffice-1`                                            |
| `vaultwarden`    | docker container | 7992  | `docker restart vaultwarden`                                                       |
| `immich`         | docker container | 2283  | DISABLED — intentionally off                                                       |
| `cloudflared`    | launchd / manual | —     | `launchctl kickstart -k gui/<uid>/com.cloudflare.cloudflared` (tunnel: jellyfin-tunnel) |

---

## Config (this block is SOURCED by serviceCheck.sh — do not break the syntax)

`SERVICES` is a bash array of `^`-delimited rows (caret chosen because it never appears in commands/URLs):

```
name^enabled^type^url^expect^restart_cmd^recheck_after_secs^expect_body
```

* `type`: `local` (lsof/launchd/app) or `docker` (`docker restart …`) — informational, used in alert text.
* `url`: probed with `curl -s -o /dev/null -w '%{http_code}' --max-time 6` (use 127.0.0.1 for local checks; subdomain for cloudflared health).
* `expect`: space-separated list of HTTP codes considered healthy (e.g. `200 302 401`). The script considers anything else (including `000` = unreachable) DOWN.
* `restart_cmd`: full command to bring it back. **Empty string = no restart, only notify.**
* `recheck_after_secs`: how long to wait after running `restart_cmd` before re-probing.
* `expect_body`: OPTIONAL extended-regex matched against the response body (curl follows redirects). Empty = skip. This catches "wrong container is answering on this port" — both Nextcloud and OnlyOffice return `302` for `/`, so HTTP status alone cannot tell them apart.

```bash
# === SERVICE CHECKER CONFIG — sourced by serviceCheck.sh ===
USER_UID="$(id -u)"

SERVICES=(
  # gyra Flask (port 5050) — see /memories/repo/gyra.md "Restart"
  "gyra^1^local^http://127.0.0.1:5050/^200 302^lsof -iTCP:5050 -sTCP:LISTEN | awk '/Python/{print \$2}' | xargs kill -9 2>/dev/null; sleep 1 && cd /Users/alexander-highground/Projects/yt-jellyfin/gyra && nohup /usr/bin/python3 app.py > /tmp/gyra.log 2>&1 &^6^GYRA"

  # Jellyfin desktop app — 302 is normal (redirects to /web)
  "jellyfin^1^local^http://127.0.0.1:8096/^200 302^open -a Jellyfin^10^[Jj]ellyfin"

  # report static site server (launchd)
  "report^1^local^http://127.0.0.1:8765/^200^launchctl kickstart -k gui/${USER_UID}/com.alex.report.serve^4^report\\.fernhw\\.com|What to Watch"

  # request server (launchd)
  "request^1^local^http://127.0.0.1:8770/^200^launchctl kickstart -k gui/${USER_UID}/com.fernhw.requestserver^4^[Rr]equest"

  # metricsd — real-time system metrics daemon for sys.html dashboard
  # Start: nohup /usr/bin/python3 web/status/metricsd.py >> /tmp/metricsd.log 2>&1 &
  "metricsd^1^local^http://127.0.0.1:8766/metrics^200^nohup /usr/bin/python3 /Users/alexander-highground/Projects/yt-jellyfin/web/status/metricsd.py >> /tmp/metricsd.log 2>&1 &^2^disk_io|cpu|mem"

  # nginx — brew service (port 80), routes *.fernhw.com + agnos.local/*.
  # Probe /audiobookshelf/login directly on port 80 to confirm nginx is routing.
  # Body check ensures it's serving ABS (not a default page or connection refused).
  "nginx^1^local^http://127.0.0.1:80/audiobookshelf/login^200 301 302^sudo brew services restart nginx^4^[Aa]udiobookshelf"

  # Audiobookshelf — see /memories/repo/gyra.md \"Audiobookshelf - DO NOT TOUCH\"
  # DO NOT set ROUTER_BASE_PATH. Default /audiobookshelf prefix is mandatory.
  "audiobookshelf^1^docker^http://127.0.0.1:13378/^200^docker restart audiobookshelf^4^[Aa]udiobookshelf"

  # Nextcloud — body check catches port-table swap with OnlyOffice (both return 302 for /).
  "nextcloud^1^docker^http://127.0.0.1:7990/^200 302^docker restart nextcloud-nextcloud-1^6^Server: Apache|Nextcloud|drive\\.fernhw\\.com"

  # OnlyOffice document server — opposite of nextcloud check.
  "onlyoffice^1^docker^http://127.0.0.1:7991/^200 302^docker restart nextcloud-onlyoffice-1^8^ONLYOFFICE|nginx"

  # Vaultwarden
  "vaultwarden^1^docker^http://127.0.0.1:7992/^200^docker restart vaultwarden^4^[Vv]aultwarden"

  # Immich — currently off by design. Set enabled=1 if you bring it back.
  "immich^0^docker^http://127.0.0.1:2283/^200 302^cd /Users/alexander-highground/Projects/yt-jellyfin/docker/immich && docker compose up -d^10^[Ii]mmich"

  # cloudflared tunnel — health-checked by probing the public hostname.
  # Body match confirms it's actually Audiobookshelf at the other end (not a Cloudflare error page).
  "cloudflared^1^local^https://abs.fernhw.com/^200 301 302^launchctl kickstart -k gui/${USER_UID}/com.cloudflare.cloudflared^8^[Aa]udiobookshelf"
)
# === END CONFIG ===
```

---

## Cron schedule

Runs at minute `47` of every hour (no collision with existing crons at `:00`, `:23`, `:30`).
Cron must export `PATH` (cron's default doesn't include docker/launchctl/python):

```cron
47 * * * * PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin /Users/alexander-highground/Projects/yt-jellyfin/serviceCheck.sh >> /tmp/serviceCheck.log 2>&1
```

## Run manually

```bash
/Users/alexander-highground/Projects/yt-jellyfin/serviceCheck.sh           # check & restart
/Users/alexander-highground/Projects/yt-jellyfin/serviceCheck.sh --dry-run # check only, no restart, no push
/Users/alexander-highground/Projects/yt-jellyfin/serviceCheck.sh --force-push  # ignore state, always push current status
```

## Notification source

OneSignal app id: `c88ae5a3-36df-4301-945f-9da65e63d87c` (same as `reportMaker.sh`).
REST key sourced from `secrets.md` line matching `K###="..."` — same parse as `reportMaker.sh:1129`.

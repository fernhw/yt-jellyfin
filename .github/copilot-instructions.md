# Copilot Instructions — yt-jellyfin / GYRA

## USER IS THE SOURCE OF TRUTH
When the user says something is true, broken, working, or exists — it is. Do not doubt them, do not ask for proof. Solve it.

---

## Project: GYRA (Flask app)
- Location: `/Users/alexander-highground/Projects/yt-jellyfin/gyra/`
- Python: `/usr/bin/python3` (3.9.6) — NO virtualenv, packages installed system-wide
- Framework: Flask 3.x, SQLite WAL (`gyra/gyra.db`)
- Port: 5050 (moved from 5000 — macOS AirPlay Receiver squats on 5000)
- Log: `/tmp/gyra.log`
- Public access: via cloudflared tunnel (IP `146.70.183.131`)

## Safe Flask restart command
```bash
lsof -iTCP:5050 -sTCP:LISTEN | awk '/Python/{print $2}' | xargs kill -9 2>/dev/null; sleep 1 && cd /Users/alexander-highground/Projects/yt-jellyfin/gyra && nohup /usr/bin/python3 app.py > /tmp/gyra.log 2>&1 &
```

## Auth
- TOTP-only login (no passwords)
- `login_required` decorator in `gyra/auth.py`
- Bearer token bypass exists for API testing — token in `gyra/.api_token` (gitignored)
- Token: `8hlVgrsJLkQnQ7QSLZABNqxvHDLz9p-wLcjOWaXxfljZvnlaoLUkfDwZ7rOpvl7J`
- Test API: `curl -s -H "Authorization: Bearer 8hlVgrsJLkQnQ7QSLZABNqxvHDLz9p-wLcjOWaXxfljZvnlaoLUkfDwZ7rOpvl7J" http://127.0.0.1:5050/api/...`

## Key files
- `gyra/app.py` — app factory, registers routes
- `gyra/auth.py` — login_required, CSRF, TOTP helpers
- `gyra/routes/` — all route modules
- `gyra/templates/board.html` — main board UI (large file, multiple IIFEs)
- `gyra/static/js/board.js` — secondary board JS

## Board / Story modal
- Card click → `window.openStoryModal(href)` (cross-IIFE safe)
- Modal defined in board.html lines ~1548–2017
- `apiGet` checks `r.ok` before calling `r.json()` — graceful session-expiry handling
- Story full data: `GET /api/story/<id>/full`

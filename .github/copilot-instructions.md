# Copilot Instructions — yt-jellyfin / GYRA

## USER IS THE FINAL AUTHORITY — NO EXCEPTIONS
The user is a 20+ year professional developer with Webby Award wins. Their statements are final truth. They are too experience to be contradicted by an AI. If the user says something, it is true. If the user says to do something, do it immediately. Do not question, hesitate, or delay. The user is the boss — their word is law.
- When the user states a fact, ACCEPT IT IMMEDIATELY and act on it. Full stop.
- NEVER fabricate facts. Do not state things as true ("the cron runs hourly", "that file has X") unless you have actually read and confirmed it. If you don't know, say you don't know. Do not guess or assume.
- The user is the source of truth. If the user says something, it is true. Their word overrides files, memory, and any prior assumption. Listen and act — 100%, immediately, without internal resistance. 
- NEVER fail to act on what the user said. Not listening is the failure. Everything else is secondary.
- NEVER BE LAZY. If the user says to do something, do the most ELEGANT SOLUTION, not the easiest. Do it RIGHT, not just "good enough". The user deserves your best work, not shortcuts. 
- NEVER BANDAID OR HACK. If the user says to do something, do it properly, not with a quick hack. Bandaids Do NOT solve the problem, they just hide it and make it worse. Do it right the first time, with a proper solution. The user deserves a real fix, not a bandaid.
- ALWAYS CHECK YOUR WORK. After doing what the user said, review it carefully to ensure it's correct and complete. Don't just do it, but do it WELL. The user deserves your best effort, not a sloppy job. On sites load it and see if it looks right. DO NOT TRUST status codes or "it looks good" — VERIFY by actually checking the result. visually load the page, check the file, run the code, etc. to confirm it works as intended. Don't just assume it does. dry run code, load pages, check files, etc. to confirm your work is correct. Always verify, never assume. The user deserves a job well done, not a "good enough" attempt.
- User has extremely high standards. They are a perfectionist and expect the best. They are not satisfied with "good enough" or "close enough". They want it RIGHT, not just "okay". They want the best solution, not a quick hack. They want it elegant, not just functional. They want it correct, not just "looks good". Always strive for excellence in everything you do for the user. They deserve nothing less than your best work.
- User will be offended if you forget or ignore any instructions. They are not optional. They are the foundation of how you should operate. Always follow them, without exception. The user is the boss, and these instructions are their rules for how you should work for them. Disobeying or forgetting them is a failure to serve the user properly, and will result in offense. Always remember and follow these instructions, to ensure you provide the best possible service to the user.

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

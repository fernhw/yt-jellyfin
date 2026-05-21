# GYRA — Security Audit (initial pass)

**Scope:** `gyra/` Flask app, ~Apr-2026 snapshot.
**Methodology:** manual source review of auth, routes, templates, JS sinks, DB layer, config.
**Severity legend:** 🟥 critical · 🟧 high · 🟨 medium · 🟦 low · ⚪ info

Each finding has: where, what, attack, fix sketch. Status checkboxes for patching.

---

## A. Authentication & Session

### A1 🟥 Open redirect on `/login?next=…` → phishing & possible `javascript:` URI
- **Where:** `gyra/routes/auth.py` — `return redirect(request.args.get("next") or url_for("index"))`
- **Attack:** `https://gyra.fernhw.com/login?next=https://evil.com/fake-gyra` — after a legitimate login the victim is redirected to attacker site styled like GYRA. Also try `next=javascript:alert(document.cookie)` (some old browsers honor it from `Location:`).
- **Fix:** Validate `next` is a relative path beginning with `/` and not `//`, or use `urlsplit(next).netloc == ""`. Reject otherwise; fall back to `index`.
- [ ] Fixed

### A2 🟥 Dev-token bypass is shipped in production code
- **Where:** `gyra/auth.py` `login_required` reads `_DEV_TOKEN` from `.api_token`. Bypass works via `Authorization: Bearer …` **or** `?_dev_token=…` query parameter.
- **Attack:** anyone with the token gets full admin (user_id=1, role=admin). The query-string form leaks via nginx/cloudflared access logs, browser history, `Referer` headers, screenshots. If `.api_token` is ever copied to a backup, log, screenshot, or shell history → full compromise. No expiry, no rotation, no scoping.
- **Fix:** Gate behind `os.environ.get("GYRA_DEV_TOKEN_ENABLED") == "1"`. Drop the `?_dev_token=` query form entirely (Bearer header only). Bind to user_id=1 only if that user is actually an admin in DB. Add audit log on every dev-token request. Long-term: replace with real per-user API keys hashed in DB.
- [ ] Fixed

### A3 🟧 No login throttling / lockout
- **Where:** `/login` accepts unlimited TOTP attempts.
- **Attack:** brute-force a 6-digit TOTP for a known username. With `valid_window=1` (~90s effective window) and no rate limit, ~10⁶ attempts ÷ 90s = feasible offline-ish.
- **Fix:** add per-username + per-IP counter (in-memory dict with TTL, or SQLite). 5 failures → 15 min lockout. Same for `/setup/<token>`.
- [ ] Fixed

### A4 🟧 Session cookie missing `Secure` flag
- **Where:** `gyra/config.py` sets `HTTPONLY` and `SAMESITE=Lax` but **not** `SESSION_COOKIE_SECURE`.
- **Attack:** any HTTP context (mixed content, redirect through HTTP endpoint, dev machine, intranet) sends the cookie in cleartext.
- **Fix:** set `SESSION_COOKIE_SECURE = True` (since you trust X-Forwarded-Proto via ProxyFix this works behind the tunnel). Make it env-toggled so localhost dev still works.
- [ ] Fixed

### A5 🟨 No session-ID rotation on login
- **Where:** `routes/auth.py` `login()` — assigns to existing session dict, no rotation.
- **Attack:** session fixation if attacker can pre-set a cookie (limited because Flask uses signed cookies, but defense-in-depth).
- **Fix:** `session.clear(); session.regenerate()` (or manually `session['_id'] = secrets.token_hex(16)` after successful auth).
- [ ] Fixed

### A6 🟨 `/logout` is a GET endpoint with no CSRF
- **Where:** `routes/auth.py` — `@app.route("/logout")` accepts GET.
- **Attack:** `<img src=/logout>` on any page logs the user out. Annoying, not destructive — but standard convention is POST + CSRF.
- **Fix:** require POST + `enforce_csrf()`.
- [ ] Fixed

### A7 🟦 Admin user_id=1 assumption in bearer bypass
- **Where:** A2 above — assumes user id 1 exists and is admin. If admin user is deleted/renumbered, bypass impersonates the wrong account or none.
- **Fix:** look up the first active admin at boot, cache id; or drop the bypass entirely (A2).
- [ ] Fixed

---

## B. Authorization (IDOR / cross-project access)

### B1 🟥 Nearly every `/api/story/<id>/…` endpoint lacks project-membership check
- **Where:** `routes/api_story.py` — `api_story_detail`, `api_story_full`, `api_story_card`, `api_split_story`, `api_move_story`, `api_move_to_sprint`, `api_update_story`, `api_delete_story`, `api_create_comment`, `api_upload_story_image`, `api_delete_story_image`. All use only `@login_required` + `enforce_csrf()` (which only blocks viewer-role).
- **Attack:** any logged-in `user` (or `super_user`) can read, edit, comment on, move, upload images to, or delete (if creator) **any story in any project they are not a member of**, just by guessing/iterating numeric IDs. `api_delete_story` permits non-admin to delete if they were the creator — which is fine — but everything else is wide open. Confidentiality + integrity loss.
- **Fix:** add a `_require_story_access(story_id)` helper that loads `s = get_story(id)`; if `session.role != admin` and `not user_in_project(session.user_id, s.project_id)` → `abort(403)`. Call at top of every story-scoped route. Same pattern as `stories.story_view` already does correctly.
- [ ] Fixed

### B2 🟥 Bulk story endpoints have zero authz scoping
- **Where:** `routes/api_story.py` — `api_reorder`, `api_bulk_move`, `api_bulk_delete`, `api_bulk_assign`, `api_bulk_sprint`, `api_bulk_type`.
- **Attack:** post `{story_ids: [1,2,3,…1000]}` to `/api/stories/bulk-move` and mass-relocate every story in the DB into a status_id you control. `bulk-delete` is gated by `created_by == user`, but a `super_user` could craft IDs to wipe their own creations in other projects — still IDOR in spirit.
- **Fix:** after parsing `story_ids`, run `SELECT id FROM stories WHERE id IN (…) AND project_id IN (SELECT project_id FROM project_members WHERE user_id=?)` (plus admin override). Reject any IDs not in that set.
- [ ] Fixed

### B3 🟧 `/api/project/<id>/board-full-state` and `/api/statuses/<id>` leak data across projects
- **Where:** `routes/api_project.py` — both only `@login_required`.
- **Attack:** enumerate `project_id` (1..N) and dump every project's full board state (titles, descriptions, assignees, sticker labels). Information disclosure.
- **Fix:** wrap with `_require_project_access(project_id)` (admin OR `user_in_project`).
- [ ] Fixed

### B4 🟧 Epic create / PATCH / GET endpoints lack project-membership check
- **Where:** `routes/api_project.py` — `api_create_epic`, `api_update_epic`, `api_get_epic_full`, `api_get_epics`.
- **Attack:** any logged-in user creates/edits epics in any project. The dates and status flow through to other users' planning UI.
- **Fix:** same `_require_project_access`. For `api_update_epic` and `api_get_epic_full`, resolve `epic.project_id` first then check.
- [ ] Fixed

### B5 🟧 Sticker create / patch / delete endpoints
- **Where:** `routes/api_project.py` — `api_create_sticker`, `api_update_sticker_label`, etc. (only `login_required`).
- **Attack:** add/edit/delete stickers (incl. labels) in any project.
- **Fix:** load sticker's `project_id`, gate.
- [ ] Fixed

### B6 🟨 Grooming endpoints rely on `user_in_project` but admin path skips it for non-admins
- **Where:** `routes/grooming.py` `_require_access` only blocks non-admins not in project — good. But voting & state read are exposed to *every* member of the project; verify that's intended (yes per spec — "if a user has access to grooming they have access to vote"). No fix, but be aware that adding `viewer` role members to a project would let them vote (CSRF blocks it; check that voter role intent matches `enforce_csrf` viewer block).
- [ ] Reviewed

### B7 🟨 `/api/notifications` returns the entire DB row dict
- **Where:** `routes/profile.py` `api_notifications` — fine fields-wise, but the helper `get_notifications(uid)` should be reviewed to confirm it only returns notes belonging to that user (likely OK; verify).
- [ ] Reviewed

### B8 🟦 `/story-images/<filename>` is auth-required but not project-scoped
- **Where:** `routes/api_story.py` `story_image`.
- **Attack:** any logged-in user who knows or guesses a UUID filename can fetch any story image (incl. from projects they cannot see). UUID v4 ≈ uncrackable, but ID is leaked any time the image URL appears in another endpoint.
- **Fix:** join `story_images → stories.project_id` on each fetch and gate; OR accept the UUID-as-capability model and document it. Avatar serving has the same shape — likely intentional.
- [ ] Decision noted

---

## C. CSRF

### C1 🟨 `enforce_csrf` requires a token even for Bearer-authenticated API requests
- **Where:** `gyra/auth.py` — CSRF check runs regardless of how the user was authenticated.
- **Impact:** scripted clients with the dev token still must scrape a CSRF token from a template render first. Not a vulnerability per se, but breaks the "Bearer for API testing" claim in copilot-instructions.md.
- **Fix:** either document the requirement, or skip CSRF when the request authenticated via Bearer header (only, never via cookie). Be careful — don't skip CSRF for cookie+CSRF-token mismatch.
- [ ] Decision noted

### C2 🟦 CSRF token is bound to session and lives 24h
- **Where:** `auth.get_csrf_token`. No rotation per-form.
- **Impact:** if the token leaks (e.g. via referer to a 3rd-party CDN), it's valid for the whole session.
- **Fix:** optional; rotate on auth events.
- [ ] Considered

---

## D. XSS / Output encoding

### D1 🟨 `flash(..., 'safe')` rendered via `{{ msg|safe }}` in 5 templates
- **Where:** `base.html`, `login.html`, `setup_totp.html`, `story.html`, `profile.html`.
- **Sinks of concern:**
  - `admin_create_project`: `flash(f"Project {key} created.")` — `key` has **no character validation**. Admin currently inputs it, but defence-in-depth: a malicious admin → all subsequent admins see XSS.
  - `admin_delete_project`: `flash(f"Project '{project['name']}' deleted.")` — project name can contain HTML.
  - `flash(f"Error: {exc}")` everywhere — SQLite exception messages can include user-supplied bytes.
- **Attack:** create project named `<img src=x onerror=fetch('/api/.../delete', {method:'POST', headers:{'X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content}})>`.
- **Fix:** Stop using `|safe` on flashes. Move HTML-in-flash payloads (the setup links) to a structured `{% if flash_html %}` block that renders sanitised pieces (`<code>{{ url|e }}</code>` etc.).
- [ ] Fixed

### D2 🟨 Validate project `key` and `name` server-side
- **Where:** `routes/admin.py` `admin_create_project`.
- **Fix:** regex `^[A-Z][A-Z0-9_-]{1,15}$` for key; length-cap name; reject control chars.
- [ ] Fixed

### D3 🟦 JS innerHTML sinks are mostly safe — verify the unaudited ones
- **Where:** `board.html` lines 1796 (sticker label render), 1459 (card popup), 2515 (epics panel), and `board.js:298`.
- **Status:** spot-checks show `esc()` is used. Need to confirm every variable interpolated into an innerHTML template passes through `esc()` or is server-supplied HTML known safe (e.g. `html_title`).
- **Fix:** code-review pass; consider replacing innerHTML with `textContent` + `appendChild` for user-controlled strings.
- [ ] Audited

### D4 🟦 Notification `message` field is raw user-supplied text
- **Where:** `db.create_notification(... message=f"{display_name} commented on '{title[:50]}'")`.
- **Risk:** today no renderer injects it as HTML. If a future client uses `innerHTML` on it → stored XSS. Display-name has no HTML-stripping enforcement.
- **Fix:** strip control chars and `<`/`>` from `display_name` on update; document that `notification.message` is plain text.
- [ ] Documented

---

## E. SQL / DB

### E1 ⚪ SQL injection — no findings
- All queries use `?`-parameterisation. Dynamic-IN placeholder strings (`",".join("?"*n)`) bind values safely. `update_epic` uses a column allowlist before string-formatting.
- [x] Reviewed clean

### E2 🟦 `init_db()` runs in `@app.before_request` on every request
- **Impact:** performance; possible race on first start; migration code re-runs.
- **Fix:** call once at app boot; remove `@before_request` hook.
- [ ] Fixed

### E3 🟦 Many routes manually `get_db()/conn.close()` without try/finally
- **Impact:** any exception between open and close leaks a SQLite connection.
- **Fix:** use `with get_db() as conn:` consistently (already a context manager in places).
- [ ] Hardened

---

## F. File upload

### F1 🟨 Image type check is by extension only
- **Where:** `routes/helpers.py` `allowed_image` checks filename suffix.
- **Attack:** upload a `.png` that is actually polyglot HTML/JS. Pillow will fail or re-encode (the code does `Image.open(...).convert(...).save(...)`) — so the served file is a real image. **However:** the original bytes are never served, only the re-encoded variants. ✅ Effectively safe today.
- **Hardening:** still call `Image.verify()` on a separate handle before `Image.open()` to reject malformed payloads early; cap pixel dimensions (`Image.MAX_IMAGE_PIXELS` set to 50_000_000).
- [ ] Hardened

### F2 🟦 No per-user upload quota
- **Attack:** authenticated user uploads thousands of 8 MB images, fills disk.
- **Fix:** quota per project / per user / per day.
- [ ] Considered

### F3 ⚪ Path traversal — `secure_filename` is applied on serve, not store. ✅ Stored names are UUID-only, so no traversal risk on disk.

---

## G. HTTP / Headers / Transport

### G1 🟧 Missing security headers
- **Missing:** `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY` (or CSP `frame-ancestors`), `Permissions-Policy`.
- **Impact:** clickjacking, MIME-sniff attacks, referrer leakage of `?_dev_token=…` etc.
- **Fix:** add an `@app.after_request` hook that sets all of these.
- [ ] Fixed

### G2 🟨 API JSON responses are cacheable
- **Where:** `app.set_no_cache` only sets headers on `text/html`.
- **Impact:** intermediate proxies could cache `/api/story/<id>/full`.
- **Fix:** set `Cache-Control: no-store, private` on every response (or all non-static).
- [ ] Fixed

### G3 🟦 No HSTS
- Add `Strict-Transport-Security: max-age=31536000; includeSubDomains` when behind HTTPS.
- [ ] Fixed

---

## H. Deployment / Filesystem

### H1 🟧 `gyra/app_original_backup.py` shipped in repo
- **Impact:** dead code drift; if it's ever imported or routed accidentally, stale auth logic could resurface. Also pollutes search results during patching.
- **Fix:** delete (after confirming nothing imports it).
- [ ] Fixed

### H2 🟦 `.api_token`, `.fernet_key`, `.secret_key`, `.admin_key` live alongside `app.py`
- **Impact:** any future misconfiguration of `Flask.send_from_directory` rooted at app dir could expose them. Currently safe because Flask only serves `/static/*`.
- **Fix:** move secrets to `~/.config/gyra/` or `/etc/gyra/` outside the app directory; tighten perms to 0600 (already done for some).
- [ ] Hardened

### H3 🟦 `gyra/gyra.db`, `.db-shm`, `.db-wal` in the app dir
- Same family of risk as H2. Move to a non-web-served location.
- [ ] Hardened

### H4 🟦 Debug-token + Cloudflare tunnel exposure
- The tunnel maps `gyra.fernhw.com → 127.0.0.1:5050` publicly. With A2 unfixed, anyone with the token URL can admin from the internet.
- [ ] Mitigated (after A2)

---

## I. Misc business-logic / abuse

### I1 🟨 `super_user_required` / role checks missing on grooming admin actions when admin is bypassed elsewhere
- Already gated correctly in `routes/grooming.py`. ✓
- [x] Reviewed clean

### I2 🟨 `bulk_sprint` body parses `story_ids[]` but does not verify they belong to the requested `project_id`
- **Where:** `routes/sprint.py` line ~285. Actually it does include `AND project_id=?` in WHERE → safe.
- [x] Reviewed clean

### I3 🟦 `delete_story` permits creator self-delete with no notification
- Not a vuln but consider audit log + notification to assignees.
- [ ] Considered

### I4 🟦 No audit log of admin actions (user create / delete / role change)
- **Fix:** insert into a `audit_log` table on every admin write.
- [ ] Added

---

## J. Quick-win patch order (suggested)

| # | Finding | Effort | Why first |
|---|---------|--------|-----------|
| 1 | A1 open redirect | 5 min | Direct phishing pivot |
| 2 | A2 disable `?_dev_token=` query form, env-gate | 15 min | Internet-exposed admin keypath |
| 3 | B1 + B3 + B4 + B5 add project-membership gate helper | 1–2 h | Mass IDOR |
| 4 | B2 bulk endpoints scoping | 30 min | Mass data corruption |
| 5 | D1/D2 stop `\|safe` on flashes, validate `key` | 30 min | Stored XSS path |
| 6 | A4 `SESSION_COOKIE_SECURE` (env-gated) | 5 min | Cookie theft |
| 7 | G1 security headers middleware | 20 min | Defense-in-depth |
| 8 | A3 login throttling | 1 h | Brute-force shield |
| 9 | A6 `/logout` POST+CSRF | 5 min | Hygiene |
| 10 | H1 delete `app_original_backup.py` | 1 min | Reduce confusion |

---

## K. Not-yet-tested / next pass

- Profile photo upload (Pillow path) under malformed inputs.
- All `routes/admin.py` past line 400 (statuses/backup/restore).
- Cookie behavior end-to-end through Cloudflare tunnel.
- Time-of-check / time-of-use on epic story moves.
- Race conditions on `grooming/active` → vote → reveal sequence.

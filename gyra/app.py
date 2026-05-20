"""
app.py — Gyra Flask application.
Run:  python app.py
"""
import base64
import datetime
import io
import os
import time
import uuid

import qrcode
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from markupsafe import Markup, escape
from PIL import Image
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from auth import (admin_required, decrypt_totp_secret, encrypt_totp_secret,
                  enforce_csrf, generate_setup_token, generate_totp_secret,
                  get_csrf_token, get_totp_uri, login_required, sha256_hex,
                  verify_setup_token, verify_totp)
from config import Config
from db import (create_addon, create_epic, delete_addon, delete_epic,
                ensure_story_types, get_all_active_users,
                get_backlog_stories, get_board_stories,
                get_db, get_epics, get_project, get_projects, get_statuses,
                get_stickers, get_story, get_story_addons, get_story_history,
                get_story_images, get_story_thumbnails, get_story_types,
                get_story_users, get_stories_tasks_batch,
                get_user_by_id, get_user_by_username,
                get_all_active_users, get_all_sprints,
                init_db, log_story_change, toggle_addon,
                update_addon_content,
                get_user_projects, get_project_members, user_in_project,
                create_notification, get_notifications,
                get_unread_count, mark_notifications_read)

app = Flask(__name__)
app.config.from_object(Config)
# Trust X-Forwarded-Prefix from nginx so url_for() works behind /gyra sub-path
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
os.makedirs(Config.STORY_IMAGES_FOLDER, exist_ok=True)

ALLOWED_AVATAR_EXT  = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_IMAGE_EXT   = {"png", "jpg", "jpeg", "webp"}




# ── Template filters ──────────────────────────────────────────────────────────

@app.template_filter("datetimeformat")
def datetimeformat(ts):
    if not ts:
        return "—"
    return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%d %b %Y %H:%M")


# ── Context processors ────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    if "user_id" not in session:
        return dict(projects=[], csrf_token=get_csrf_token, notif_count=0)
    uid  = session["user_id"]
    role = session.get("role")
    projects = get_projects() if role == "admin" else get_user_projects(uid)
    notif_count = get_unread_count(uid)
    return dict(projects=projects, csrf_token=get_csrf_token, notif_count=notif_count)


# ── DB initialisation ─────────────────────────────────────────────────────────

@app.before_request
def bootstrap():
    init_db()


@app.after_request
def set_no_cache(response):
    """Prevent Cloudflare / any proxy from caching HTML pages that contain CSRF tokens."""
    if response.content_type and response.content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# ── Temporary: header debug (admin only) ──────────────────────────────────────
@app.route("/debug/headers")
@login_required
@admin_required
def debug_headers():
    import json as _json
    data = {
        "remote_addr":    request.remote_addr,
        "host":           request.host,
        "url_scheme":     request.scheme,
        "headers":        dict(request.headers),
        "session_keys":   list(session.keys()),
        "has_csrf":       "_csrf" in session,
    }
    return _json.dumps(data, indent=2), 200, {"Content-Type": "application/json"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_AVATAR_EXT


def _allowed_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXT


def _count_words(*parts) -> int:
    return sum(len(str(p).split()) for p in parts if p)


def _build_story_title(actor, verb, z, x, for_conn, y) -> str:
    return " ".join(p for p in [actor, verb, z, x, for_conn, y] if p)


def _bold_verb_in_title(title: str, verb: str) -> Markup:
    """Return HTML-safe Markup with the action word wrapped in <strong>."""
    if not verb or not title:
        return Markup(escape(title or ""))
    idx = title.find(verb)
    if idx < 0:
        return Markup(escape(title))
    return Markup(
        escape(title[:idx]) +
        Markup("<strong>") +
        escape(verb) +
        Markup("</strong>") +
        escape(title[idx + len(verb):])
    )


def _make_qr_png(uri: str) -> str:
    """Return a data-URI PNG of the TOTP QR code."""
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        enforce_csrf()
        username = request.form.get("username", "").strip()
        code     = request.form.get("totp_code", "").strip()

        user = get_user_by_username(username)

        # Constant-time path: always verify to prevent username enumeration.
        if not user or not user["totp_confirmed"] or not user["totp_secret_enc"]:
            verify_totp("", "000000")
            flash("Invalid credentials or account not yet configured.", "error")
            return render_template("login.html")

        if not verify_totp(user["totp_secret_enc"], code):
            flash("Invalid TOTP code.", "error")
            return render_template("login.html")

        session.permanent        = True
        session["user_id"]       = user["id"]
        session["username"]      = user["username"]
        session["display_name"]  = user["display_name"]
        session["role"]          = user["role"]
        session["avatar"]        = user["avatar"]

        return redirect(request.args.get("next") or url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/setup/<token>", methods=["GET", "POST"])
def setup_totp(token):
    token_hash = sha256_hex(token)
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE setup_token_hash = ? AND is_active = 1",
        (token_hash,),
    ).fetchone()
    conn.close()

    if not user:
        abort(404)
    if not verify_setup_token(token, user["setup_token_hash"], user["setup_token_expires"] or 0):
        flash("Setup link has expired. Ask your admin to reset it.", "error")
        return redirect(url_for("login"))
    if user["totp_confirmed"]:
        flash("TOTP already configured. Log in normally.", "info")
        return redirect(url_for("login"))

    # Generate TOTP secret on first visit
    if not user["totp_secret_enc"]:
        secret   = generate_totp_secret()
        enc      = encrypt_totp_secret(secret)
        conn     = get_db()
        conn.execute("UPDATE users SET totp_secret_enc = ? WHERE id = ?", (enc, user["id"]))
        conn.commit()
        conn.close()
        totp_enc = enc
    else:
        totp_enc = user["totp_secret_enc"]
        secret   = decrypt_totp_secret(totp_enc)

    uri     = get_totp_uri(secret, user["username"])
    qr_data = _make_qr_png(uri)

    if request.method == "POST":
        enforce_csrf()
        code = request.form.get("totp_code", "").strip()
        if not verify_totp(totp_enc, code):
            flash("Code incorrect — try again.", "error")
            return render_template("setup_totp.html", qr=qr_data, secret=secret, user=user)
        conn = get_db()
        conn.execute(
            "UPDATE users SET totp_confirmed=1, setup_token_hash=NULL, setup_token_expires=NULL WHERE id=?",
            (user["id"],),
        )
        conn.commit()
        conn.close()
        flash("TOTP configured. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("setup_totp.html", qr=qr_data, secret=secret, user=user)


# ── Core views ────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    projects = get_projects()
    if projects:
        return redirect(url_for("board", project_id=projects[0]["id"]))
    return render_template("no_project.html")


@app.route("/project/<int:project_id>/board")
@login_required
def board(project_id):
    project = get_project(project_id)
    if not project:
        abort(404)

    statuses = get_statuses(project_id)

    # Ensure this project has story types seeded
    with get_db() as _conn:
        ensure_story_types(project_id, _conn)

    raw_stories = get_board_stories(project_id)

    stories = []
    for s in raw_stories:
        d = dict(s)
        d["assignees"]  = get_story_users(s["id"])
        d["html_title"] = _bold_verb_in_title(s["title"], s["story_z"] or "")
        stories.append(d)

    story_ids  = [s["id"] for s in stories]
    thumbnails = get_story_thumbnails(story_ids)
    tasks_map  = get_stories_tasks_batch(story_ids)
    for s in stories:
        s["thumbnail"] = thumbnails.get(s["id"])
        s["tasks"]     = tasks_map.get(s["id"], [])

    board_map: dict = {st["id"]: [] for st in statuses}
    first_status_id = statuses[0]["id"] if statuses else None
    for s in stories:
        col = s["status_id"]
        if col not in board_map and first_status_id is not None:
            col = first_status_id  # safety net: no-status stories go to first column
        if col in board_map:
            board_map[col].append(s)

    all_stickers      = [dict(sk) for sk in get_stickers(project_id)]
    free_stickers     = [s for s in all_stickers if not s.get('card_story_id')]
    card_stickers_map = {}
    for s in all_stickers:
        cid = s.get('card_story_id')
        if cid:
            card_stickers_map.setdefault(cid, []).append(s)

    all_users    = get_all_active_users()
    all_sprints  = get_all_sprints(project_id)
    story_types  = get_story_types(project_id)

    return render_template(
        "board.html",
        project=project,
        statuses=statuses,
        board_map=board_map,
        stickers=free_stickers,
        card_stickers_map=card_stickers_map,
        all_users=all_users,
        all_sprints=all_sprints,
        story_types=story_types,
    )


@app.route("/project/<int:project_id>/backlog")
@login_required
def backlog(project_id):
    project = get_project(project_id)
    if not project:
        abort(404)

    raw_stories = get_backlog_stories(project_id)
    stories     = []
    for s in raw_stories:
        d = dict(s)
        d["assignees"] = get_story_users(s["id"])
        stories.append(d)

    statuses = get_statuses(project_id)

    return render_template(
        "backlog.html",
        project=project,
        stories=stories,
        statuses=statuses,
    )


# ── Stories ───────────────────────────────────────────────────────────────────

@app.route("/story/new", methods=["GET", "POST"])
@login_required
def story_new():
    project_id = request.args.get("project_id", type=int)

    if request.method == "POST":
        enforce_csrf()
        project_id  = request.form.get("project_id", type=int)
        # Structured user-story parts
        actor    = request.form.get("story_actor", "User").strip()
        verb     = request.form.get("story_verb", "needs").strip()
        z        = request.form.get("story_z", "").strip()
        x        = request.form.get("story_x", "").strip()
        for_conn = request.form.get("story_for", "to").strip()
        y        = request.form.get("story_y", "").strip()
        title    = _build_story_title(actor, verb, z, x, for_conn, y)

        description = request.form.get("description", "").strip()
        ac          = request.form.get("acceptance_criteria", "").strip()
        points      = request.form.get("story_points", 0, type=int)
        status_id   = request.form.get("status_id", type=int) or None
        assignees   = request.form.getlist("assignee_ids", type=int)
        sprint      = request.form.get("sprint", type=int) or None
        story_type  = request.form.get("story_type", type=int) or None
        priority    = request.form.get("priority", "").strip() or None
        epic_id     = request.form.get("epic_id", type=int) or None

        if not z or not x or not y or not project_id:
            flash("All story parts are required.", "error")
            return redirect(request.url)

        if " " in z.strip():
            flash("The action word must be a single word (e.g. Walking, Killing, Building).", "error")
            return redirect(request.url)

        word_count = _count_words(actor, verb, z, x, for_conn, y)
        if word_count > 19:
            flash(f"Story title is {word_count} words — max is 19.", "error")
            return redirect(request.url)

        now  = int(time.time())
        conn = get_db()
        row  = conn.execute(
            "SELECT COALESCE(MAX(order_index),0)+1 AS nxt FROM stories WHERE project_id=?",
            (project_id,),
        ).fetchone()
        order = row["nxt"]

        cur = conn.execute(
            """INSERT INTO stories
               (project_id,title,description,acceptance_criteria,story_points,
                status_id,sprint,order_index,created_at,created_by,updated_at,
                story_actor,story_verb,story_z,story_x,story_for,story_y,story_type,
                priority,epic_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, title, description, ac, points,
             status_id, sprint, order, now, session["user_id"], now,
             actor, verb, z, x, for_conn, y, story_type, priority, epic_id),
        )
        story_id = cur.lastrowid
        for uid in assignees:
            conn.execute(
                "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
                (story_id, uid),
            )
        conn.commit()
        conn.close()

        flash("Story created.", "success")
        if sprint:
            return redirect(url_for("board", project_id=project_id))
        else:
            return redirect(url_for("backlog", project_id=project_id))

    project     = get_project(project_id) if project_id else None
    statuses    = get_statuses(project_id) if project_id else []
    all_users   = get_all_active_users()
    default_sprint = request.args.get("sprint", type=int)
    if project_id:
        with get_db() as _c:
            ensure_story_types(project_id, _c)
    story_types = get_story_types(project_id) if project_id else []
    epics       = get_epics(project_id) if project_id else []
    return render_template(
        "story.html",
        mode="new",
        story=None,
        project=project,
        project_id=project_id,
        statuses=statuses,
        all_users=all_users,
        story_types=story_types,
        assignees=[],
        assignee_ids=[],
        comments=[],
        images=[],
        default_sprint=default_sprint,
        epics=epics,
        history=[],
        addons=[],
    )


@app.route("/story/<int:story_id>", methods=["GET", "POST"])
@login_required
def story_view(story_id):
    s = get_story(story_id)
    if not s:
        abort(404)

    if request.method == "POST":
        enforce_csrf()
        action = request.form.get("action")

        if action == "update":
            actor    = request.form.get("story_actor", "User").strip()
            verb     = request.form.get("story_verb", "needs").strip()
            z        = request.form.get("story_z", "").strip()
            x        = request.form.get("story_x", "").strip()
            for_conn = request.form.get("story_for", "to").strip()
            y        = request.form.get("story_y", "").strip()
            title    = _build_story_title(actor, verb, z, x, for_conn, y)

            if z and " " in z.strip():
                flash("The action word must be a single word (e.g. Walking, Killing, Building).", "error")
                return redirect(url_for("story_view", story_id=story_id))

            word_count = _count_words(actor, verb, z, x, for_conn, y)
            if word_count > 19:
                flash(f"Story is {word_count} words — max 19.", "error")
                return redirect(url_for("story_view", story_id=story_id))

            desc       = request.form.get("description", "").strip()
            ac         = request.form.get("acceptance_criteria", "").strip()
            points     = request.form.get("story_points", 0, type=int)
            status_id  = request.form.get("status_id", type=int) or None
            sprint     = request.form.get("sprint", type=int) or None
            assignees  = request.form.getlist("assignee_ids", type=int)
            story_type = request.form.get("story_type", type=int) or None
            priority   = request.form.get("priority", "").strip() or None
            epic_id    = request.form.get("epic_id", type=int) or None

            # Capture before-state for history logging
            old = dict(s)
            old_assignee_ids = {a["id"] for a in get_story_users(story_id)}

            conn = get_db()
            conn.execute(
                """UPDATE stories SET title=?,description=?,acceptance_criteria=?,
                   story_points=?,status_id=?,sprint=?,updated_at=?,
                   story_actor=?,story_verb=?,story_z=?,story_x=?,story_for=?,story_y=?,
                   story_type=?,priority=?,epic_id=?
                   WHERE id=?""",
                (title, desc, ac, points, status_id, sprint, int(time.time()),
                 actor, verb, z, x, for_conn, y, story_type, priority, epic_id, story_id),
            )
            conn.execute("DELETE FROM story_users WHERE story_id=?", (story_id,))
            for uid in assignees:
                conn.execute(
                    "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
                    (story_id, uid),
                )
            conn.commit()
            conn.close()

            # Log field changes to story_history
            uid = session["user_id"]
            log_story_change(story_id, uid, "Status",   old.get("status_id"),   status_id)
            log_story_change(story_id, uid, "Sprint",   old.get("sprint"),       sprint)
            log_story_change(story_id, uid, "Priority", old.get("priority"),     priority)
            log_story_change(story_id, uid, "Points",   old.get("story_points"), points)
            log_story_change(story_id, uid, "Epic",     old.get("epic_id"),      epic_id)
            if old.get("title") != title:
                log_story_change(story_id, uid, "Title", old.get("title"), title)

            # Notify newly assigned users
            new_assignee_ids = set(assignees)
            for newly_assigned_id in (new_assignee_ids - old_assignee_ids):
                if newly_assigned_id != uid:
                    create_notification(
                        user_id=newly_assigned_id,
                        type_="assignment",
                        message=f"{session['display_name']} assigned you to '{s['title'][:50]}'",
                        story_id=story_id,
                        from_user=uid,
                    )

            flash("Story updated.", "success")
            pid = s["project_id"]
            if sprint:
                return redirect(url_for("board", project_id=pid))
            else:
                return redirect(url_for("backlog", project_id=pid))

        if action == "comment":
            content = request.form.get("content", "").strip()
            if content:
                conn = get_db()
                conn.execute(
                    "INSERT INTO comments (story_id,user_id,content,created_at) VALUES (?,?,?,?)",
                    (story_id, session["user_id"], content, int(time.time())),
                )
                conn.commit()
                conn.close()
                # Notify assignees about the new comment
                commenter     = session["user_id"]
                commenter_name = session["display_name"]
                story_title   = s["title"][:50]
                for assignee in get_story_users(story_id):
                    if assignee["id"] != commenter:
                        create_notification(
                            user_id=assignee["id"],
                            type_="comment",
                            message=f"{commenter_name} commented on '{story_title}'",
                            story_id=story_id,
                            from_user=commenter,
                        )
                # Notify @mentioned users
                import re as _re
                for username in _re.findall(r'@(\w+)', content):
                    mentioned = get_user_by_username(username)
                    if mentioned and mentioned["id"] != commenter:
                        create_notification(
                            user_id=mentioned["id"],
                            type_="mention",
                            message=f"{commenter_name} mentioned you in '{story_title}'",
                            story_id=story_id,
                            from_user=commenter,
                        )
            return redirect(url_for("story_view", story_id=story_id))

        if action == "delete":
            if session["role"] != "admin" and s["created_by"] != session["user_id"]:
                abort(403)
            project_id = s["project_id"]
            conn = get_db()
            conn.execute("DELETE FROM stories WHERE id=?", (story_id,))
            conn.commit()
            conn.close()
            flash("Story deleted.", "success")
            return redirect(url_for("backlog", project_id=project_id))

        if action == "upload_image":
            f = request.files.get("image")
            if f and f.filename and _allowed_image(f.filename):
                ext      = secure_filename(f.filename).rsplit(".", 1)[1].lower()
                filename = f"{uuid.uuid4().hex}.{ext}"
                filepath = os.path.join(Config.STORY_IMAGES_FOLDER, filename)
                img = Image.open(f.stream)
                img.thumbnail((900, 900), Image.LANCZOS)
                img.save(filepath, quality=88)
                conn = get_db()
                conn.execute(
                    "INSERT INTO story_images (story_id,filename,created_at) VALUES (?,?,?)",
                    (story_id, filename, int(time.time())),
                )
                conn.commit()
                conn.close()
            return redirect(url_for("story_view", story_id=story_id))

        if action == "delete_image":
            image_id = request.form.get("image_id", type=int)
            if image_id:
                conn = get_db()
                row = conn.execute(
                    "SELECT * FROM story_images WHERE id=? AND story_id=?",
                    (image_id, story_id),
                ).fetchone()
                if row:
                    fp = os.path.join(Config.STORY_IMAGES_FOLDER, row["filename"])
                    if os.path.isfile(fp):
                        os.remove(fp)
                    conn.execute("DELETE FROM story_images WHERE id=?", (image_id,))
                conn.commit()
                conn.close()
            return redirect(url_for("story_view", story_id=story_id))

    conn      = get_db()
    comments  = conn.execute(
        """SELECT c.*, u.display_name, u.avatar FROM comments c
           JOIN users u ON c.user_id=u.id
           WHERE c.story_id=? ORDER BY c.created_at""",
        (story_id,),
    ).fetchall()
    conn.close()

    assignees    = get_story_users(story_id)
    assignee_ids = [a["id"] for a in assignees]
    statuses     = get_statuses(s["project_id"])
    all_users    = get_all_active_users()
    images       = get_story_images(story_id)
    story_types  = get_story_types(s["project_id"])
    epics        = get_epics(s["project_id"])
    history      = get_story_history(story_id)
    addons       = get_story_addons(story_id, session.get("user_id"))

    return render_template(
        "story.html",
        mode="view",
        story=s,
        project=None,
        project_id=s["project_id"],
        statuses=statuses,
        all_users=all_users,
        story_types=story_types,
        assignees=assignees,
        assignee_ids=assignee_ids,
        comments=comments,
        images=images,
        default_sprint=None,
        epics=epics,
        history=history,
        addons=addons,
    )


# ── Admin — users ─────────────────────────────────────────────────────────────

@app.route("/admin/users")
@admin_required
def admin_users():
    conn  = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    enforce_csrf()
    username     = request.form.get("username", "").strip()
    email        = request.form.get("email", "").strip()
    display_name = request.form.get("display_name", "").strip()
    role         = request.form.get("role", "user")

    if not all([username, email, display_name]):
        flash("All fields are required.", "error")
        return redirect(url_for("admin_users"))
    if role not in ("admin", "user"):
        role = "user"

    raw_token, token_hash = generate_setup_token()
    expires = int(time.time()) + 86400 * 7  # 7 days

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO users
               (username,email,display_name,role,setup_token_hash,
                setup_token_expires,created_at,created_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (username, email, display_name, role,
             token_hash, expires, int(time.time()), session["user_id"]),
        )
        conn.commit()
        conn.close()
        setup_url = url_for("setup_totp", token=raw_token, _external=True)
        flash(
            f'User <strong>{username}</strong> created. '
            f'One-time setup link (copy now):<br><code>{setup_url}</code>',
            "success",
        )
    except Exception as exc:
        flash(f"Error: {exc}", "error")

    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    enforce_csrf()
    if user_id == session["user_id"]:
        flash("Cannot deactivate yourself.", "error")
        return redirect(url_for("admin_users"))
    conn = get_db()
    conn.execute(
        "UPDATE users SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
        (user_id,),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-totp", methods=["POST"])
@admin_required
def admin_reset_totp(user_id):
    enforce_csrf()
    raw_token, token_hash = generate_setup_token()
    expires = int(time.time()) + 86400 * 2
    conn = get_db()
    conn.execute(
        """UPDATE users SET totp_secret_enc=NULL, totp_confirmed=0,
           setup_token_hash=?, setup_token_expires=? WHERE id=?""",
        (token_hash, expires, user_id),
    )
    conn.commit()
    conn.close()
    setup_url = url_for("setup_totp", token=raw_token, _external=True)
    flash(
        f"TOTP reset. New setup link (copy now):<br><code>{setup_url}</code>",
        "success",
    )
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/edit", methods=["POST"])
@admin_required
def admin_edit_user(user_id):
    enforce_csrf()
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        flash("User not found.", "error")
        conn.close()
        return redirect(url_for("admin_users"))

    username     = request.form.get("username", "").strip()
    email        = request.form.get("email", "").strip()
    display_name = request.form.get("display_name", "").strip()
    role         = request.form.get("role", "user")
    is_active    = 1 if request.form.get("is_active") else 0

    if not all([username, email, display_name]):
        flash("Username, email and display name are required.", "error")
        conn.close()
        return redirect(url_for("admin_users"))
    if role not in ("admin", "user"):
        role = "user"
    # Prevent removing admin role from self
    if user_id == session["user_id"] and role != "admin":
        flash("You cannot remove your own admin role.", "error")
        conn.close()
        return redirect(url_for("admin_users"))
    # Ensure at least one admin remains
    if role != "admin":
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1 AND id!=?",
            (user_id,)
        ).fetchone()[0]
        if admin_count == 0:
            flash("Cannot demote the only active admin.", "error")
            conn.close()
            return redirect(url_for("admin_users"))

    try:
        conn.execute(
            """UPDATE users SET username=?, email=?, display_name=?, role=?, is_active=?
               WHERE id=?""",
            (username, email, display_name, role, is_active, user_id),
        )
        conn.commit()
        flash(f"User <strong>{username}</strong> updated.", "success")
    except Exception as exc:
        flash(f"Error: {exc}", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    enforce_csrf()
    if user_id == session["user_id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        flash("User not found.", "error")
        conn.close()
        return redirect(url_for("admin_users"))

    # Prevent deleting the last admin
    if target["role"] == "admin":
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND id!=?", (user_id,)
        ).fetchone()[0]
        if admin_count == 0:
            flash("Cannot delete the only admin account.", "error")
            conn.close()
            return redirect(url_for("admin_users"))

    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    flash(f"User <strong>{target['username']}</strong> deleted.", "success")
    return redirect(url_for("admin_users"))


# ── Admin — projects ──────────────────────────────────────────────────────────

@app.route("/admin/projects")
@admin_required
def admin_projects():
    conn         = get_db()
    raw_projects = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
    conn.close()
    all_projects = []
    for p in raw_projects:
        d = dict(p)
        d["statuses"] = get_statuses(p["id"])
        d["members"]  = get_project_members(p["id"])
        all_projects.append(d)
    all_users = get_all_active_users()
    return render_template("admin_project.html", all_projects=all_projects, all_users=all_users)


@app.route("/admin/projects/create", methods=["POST"])
@admin_required
def admin_create_project():
    enforce_csrf()
    name        = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    key         = request.form.get("key", "").strip().upper()

    if not name or not key:
        flash("Name and key are required.", "error")
        return redirect(url_for("admin_projects"))

    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO projects (name,description,key,created_at,created_by) VALUES (?,?,?,?,?)",
            (name, description, key, int(time.time()), session["user_id"]),
        )
        project_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for idx, (sname, color, is_done) in enumerate([
            ("To Do",       "#6B7280", 0),
            ("In Progress", "#3B82F6", 0),
            ("In Review",   "#F59E0B", 0),
            ("Done",        "#10B981", 1),
        ]):
            conn.execute(
                "INSERT INTO statuses (project_id,name,color,order_index,is_done) VALUES (?,?,?,?,?)",
                (project_id, sname, color, idx, is_done),
            )
        conn.commit()
        conn.close()
        flash(f"Project {key} created.", "success")
    except Exception as exc:
        flash(f"Error: {exc}", "error")

    return redirect(url_for("admin_projects"))


@app.route("/admin/projects/<int:project_id>/delete", methods=["POST"])
@admin_required
def admin_delete_project(project_id):
    enforce_csrf()
    conn    = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.", "error")
        return redirect(url_for("admin_projects"))
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    conn.close()
    flash(f"Project '{project['name']}' deleted.", "success")
    return redirect(url_for("admin_projects"))


@app.route("/admin/projects/<int:project_id>/members/add", methods=["POST"])
@admin_required
def admin_add_project_member(project_id):
    enforce_csrf()
    uid = request.form.get("user_id", type=int)
    if uid:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO project_members (project_id, user_id, added_by, added_at) VALUES (?,?,?,?)",
            (project_id, uid, session["user_id"], int(time.time())),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("admin_projects") + f"#{project_id}")


@app.route("/admin/projects/<int:project_id>/members/remove", methods=["POST"])
@admin_required
def admin_remove_project_member(project_id):
    enforce_csrf()
    uid = request.form.get("user_id", type=int)
    if uid:
        conn = get_db()
        conn.execute(
            "DELETE FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, uid),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("admin_projects") + f"#{project_id}")


@app.route("/admin/projects/<int:project_id>/status/add", methods=["POST"])
@admin_required
def admin_add_status(project_id):
    enforce_csrf()
    name    = request.form.get("name", "").strip()
    color   = request.form.get("color", "#6B7280").strip()
    is_done = 1 if request.form.get("is_done") else 0

    if name:
        conn = get_db()
        row  = conn.execute(
            "SELECT COALESCE(MAX(order_index),0)+1 AS nxt FROM statuses WHERE project_id=?",
            (project_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO statuses (project_id,name,color,order_index,is_done) VALUES (?,?,?,?,?)",
            (project_id, name, color, row["nxt"], is_done),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("admin_projects"))


@app.route("/admin/projects/<int:project_id>/status/<int:status_id>/delete", methods=["POST"])
@admin_required
def admin_delete_status(project_id, status_id):
    enforce_csrf()
    conn = get_db()
    conn.execute("DELETE FROM statuses WHERE id=? AND project_id=?", (status_id, project_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_projects"))


@app.route("/api/project/<int:project_id>/statuses/reorder", methods=["POST"])
@admin_required
def api_reorder_statuses(project_id):
    enforce_csrf()
    data       = request.get_json(silent=True) or {}
    ordered_ids = data.get("ids", [])
    if not ordered_ids:
        return jsonify(ok=False, error="missing ids"), 400
    try:
        ordered_ids = [int(i) for i in ordered_ids]
    except (ValueError, TypeError):
        return jsonify(ok=False, error="invalid ids"), 400
    conn = get_db()
    for idx, sid in enumerate(ordered_ids):
        conn.execute(
            "UPDATE statuses SET order_index=? WHERE id=? AND project_id=?",
            (idx, sid, project_id),
        )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ── Profile & avatars ─────────────────────────────────────────────────────────

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = get_user_by_id(session["user_id"])

    if request.method == "POST":
        enforce_csrf()
        display_name = request.form.get("display_name", "").strip() or user["display_name"]
        new_avatar   = user["avatar"]

        if "avatar" in request.files:
            f = request.files["avatar"]
            if f and f.filename and _allowed(f.filename):
                ext      = secure_filename(f.filename).rsplit(".", 1)[1].lower()
                filename = f"{uuid.uuid4().hex}.{ext}"
                filepath = os.path.join(Config.UPLOAD_FOLDER, filename)
                img = Image.open(f.stream).convert("RGB")
                img.thumbnail((128, 128), Image.LANCZOS)
                img.save(filepath, quality=85)

                if user["avatar"]:
                    old = os.path.join(Config.UPLOAD_FOLDER, user["avatar"])
                    if os.path.isfile(old):
                        os.remove(old)
                new_avatar = filename

        conn = get_db()
        conn.execute(
            "UPDATE users SET display_name=?, avatar=? WHERE id=?",
            (display_name, new_avatar, session["user_id"]),
        )
        conn.commit()
        conn.close()
        session["display_name"] = display_name
        session["avatar"]       = new_avatar
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html", user=user)


@app.route("/avatars/<filename>")
@login_required
def avatar(filename):
    return send_from_directory(Config.UPLOAD_FOLDER, secure_filename(filename))


# ── Notifications API ─────────────────────────────────────────────────────────

@app.route("/api/notifications")
@login_required
def api_notifications():
    notes = get_notifications(session["user_id"])
    result = []
    for n in notes:
        result.append({
            "id":         n["id"],
            "type":       n["type"],
            "message":    n["message"],
            "story_id":   n["story_id"],
            "from_name":  n["from_name"],
            "is_read":    bool(n["is_read"]),
            "created_at": n["created_at"],
        })
    return jsonify(notifications=result, unread=get_unread_count(session["user_id"]))


@app.route("/api/notifications/mark-read", methods=["POST"])
@login_required
def api_notifications_mark_read():
    enforce_csrf()
    mark_notifications_read(session["user_id"])
    return jsonify(ok=True)


# ── JSON API (board drag-drop) ────────────────────────────────────────────────

@app.route("/api/story/<int:story_id>/detail")
@login_required
def api_story_detail(story_id):
    conn = get_db()
    row  = conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return jsonify(
        id=row["id"],
        title=row["title"],
        description=row["description"] or "",
        story_points=row["story_points"] or 0,
        priority=row["priority"] or "",
        status_id=row["status_id"],
        sprint=row["sprint"],
        project_id=row["project_id"],
        story_type=row["story_type"],
        story_actor=row["story_actor"] or "",
        story_verb=row["story_verb"] or "",
        story_z=row["story_z"] or "",
        story_x=row["story_x"] or "",
        story_for=row["story_for"] or "",
        story_y=row["story_y"] or "",
        epic_id=row["epic_id"],
    )


@app.route("/api/story/<int:story_id>/split", methods=["POST"])
@login_required
def api_split_story(story_id):
    enforce_csrf()
    data = request.get_json(silent=True) or {}

    conn = get_db()
    orig = conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    if not orig:
        conn.close()
        abort(404)

    # Update Story A (the original) with new points/priority/description
    a_desc     = data.get("a_description", orig["description"] or "")
    a_points   = data.get("a_points", orig["story_points"] or 0)
    a_priority = data.get("a_priority", orig["priority"] or None) or None
    conn.execute(
        "UPDATE stories SET description=?,story_points=?,priority=?,updated_at=? WHERE id=?",
        (a_desc, int(a_points), a_priority, int(time.time()), story_id),
    )

    # Create Story B — inherits everything from A, override with B-specific values
    b_title    = (data.get("b_title") or "").strip() or orig["title"]
    b_desc     = data.get("b_description", orig["description"] or "")
    b_points   = data.get("b_points", orig["story_points"] or 0)
    b_priority = data.get("b_priority", orig["priority"] or None) or None

    order_row = conn.execute(
        "SELECT COALESCE(MAX(order_index),0)+1 AS nxt FROM stories WHERE project_id=?",
        (orig["project_id"],),
    ).fetchone()
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO stories
           (project_id,title,description,acceptance_criteria,story_points,
            status_id,sprint,order_index,created_at,created_by,updated_at,
            story_actor,story_verb,story_z,story_x,story_for,story_y,
            story_type,priority,epic_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (orig["project_id"], b_title, b_desc,
         orig["acceptance_criteria"] or "",
         int(b_points), orig["status_id"], orig["sprint"],
         order_row["nxt"], now, session["user_id"], now,
         orig["story_actor"], orig["story_verb"], orig["story_z"],
         orig["story_x"], orig["story_for"], orig["story_y"],
         orig["story_type"], b_priority, orig["epic_id"]),
    )
    new_id = cur.lastrowid

    # Copy assignees
    assignees = conn.execute(
        "SELECT user_id FROM story_users WHERE story_id=?", (story_id,)
    ).fetchall()
    for a in assignees:
        conn.execute(
            "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
            (new_id, a["user_id"]),
        )

    # Log split in history
    conn.execute(
        "INSERT INTO story_history (story_id,user_id,field_name,old_value,new_value,created_at) VALUES (?,?,?,?,?,?)",
        (story_id, session["user_id"], "Split", "", f"Created #{new_id}", now),
    )

    conn.commit()
    conn.close()
    return jsonify(ok=True, id=new_id)


@app.route("/api/story/<int:story_id>/move", methods=["POST"])
@login_required
def api_move_story(story_id):
    enforce_csrf()
    data      = request.get_json(silent=True) or {}
    status_id = data.get("status_id")
    sprint    = data.get("sprint")
    order     = data.get("order_index", 0)

    conn = get_db()
    old = conn.execute("SELECT status_id, sprint FROM stories WHERE id=?", (story_id,)).fetchone()
    conn.execute(
        "UPDATE stories SET status_id=?,sprint=?,order_index=?,updated_at=? WHERE id=?",
        (status_id, sprint, order, int(time.time()), story_id),
    )
    # Log status change to story_history
    if old and old["status_id"] != status_id:
        old_st = conn.execute("SELECT name FROM statuses WHERE id=?", (old["status_id"],)).fetchone()
        new_st = conn.execute("SELECT name FROM statuses WHERE id=?", (status_id,)).fetchone()
        conn.execute(
            "INSERT INTO story_history (story_id,user_id,field_name,old_value,new_value,created_at) VALUES (?,?,?,?,?,?)",
            (story_id, session["user_id"], "Status",
             old_st["name"] if old_st else str(old["status_id"]),
             new_st["name"] if new_st else str(status_id),
             int(time.time())),
        )
        # Notify all assignees of the status change
        st_name = new_st["name"] if new_st else str(status_id)
        for a in get_story_users(story_id):
            if a["id"] != session["user_id"]:
                conn.execute(
                    "INSERT INTO notifications (user_id,type,message,story_id,from_user,created_at) VALUES (?,?,?,?,?,?)",
                    (a["id"], "status", f"{session['display_name']} moved a story to {st_name}",
                     story_id, session["user_id"], int(time.time())),
                )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/story/<int:story_id>/sprint", methods=["POST"])
@login_required
def api_move_to_sprint(story_id):
    enforce_csrf()
    data   = request.get_json(silent=True) or {}
    sprint = data.get("sprint")

    conn = get_db()
    # If moving to a sprint and this story has no status, assign the first status
    # so it doesn't silently disappear from the board.
    if sprint is not None:
        story = conn.execute("SELECT project_id, status_id FROM stories WHERE id=?", (story_id,)).fetchone()
        if story and not story["status_id"]:
            first_status = conn.execute(
                "SELECT id FROM statuses WHERE project_id=? ORDER BY order_index LIMIT 1",
                (story["project_id"],),
            ).fetchone()
            if first_status:
                conn.execute(
                    "UPDATE stories SET sprint=?,status_id=?,updated_at=? WHERE id=?",
                    (sprint, first_status["id"], int(time.time()), story_id),
                )
                conn.commit()
                conn.close()
                return jsonify(ok=True)
    conn.execute(
        "UPDATE stories SET sprint=?,updated_at=? WHERE id=?",
        (sprint, int(time.time()), story_id),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/statuses/<int:project_id>")
@login_required
def api_statuses(project_id):
    return jsonify([dict(s) for s in get_statuses(project_id)])


@app.route("/api/stories/bulk-move", methods=["POST"])
@login_required
def api_bulk_move():
    enforce_csrf()
    data = request.get_json(silent=True) or {}
    raw_ids   = data.get("story_ids", [])
    status_id = data.get("status_id")
    sprint    = data.get("sprint")
    if not raw_ids or not status_id:
        return jsonify(ok=False, error="missing params"), 400
    try:
        story_ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify(ok=False, error="invalid ids"), 400
    ph   = ','.join('?' * len(story_ids))
    conn = get_db()
    conn.execute(
        f"UPDATE stories SET status_id=?,sprint=?,updated_at=? WHERE id IN ({ph})",
        [int(status_id), sprint, int(time.time())] + story_ids,
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/stories/bulk-delete", methods=["POST"])
@login_required
def api_bulk_delete():
    enforce_csrf()
    data    = request.get_json(silent=True) or {}
    raw_ids = data.get("story_ids", [])
    if not raw_ids:
        return jsonify(ok=False, error="missing ids"), 400
    try:
        story_ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify(ok=False, error="invalid ids"), 400
    current_uid = session["user_id"]
    is_admin    = session.get("role") == "admin"
    ph   = ','.join('?' * len(story_ids))
    conn = get_db()
    rows = conn.execute(
        f"SELECT id, created_by FROM stories WHERE id IN ({ph})", story_ids
    ).fetchall()
    allowed = [r["id"] for r in rows if is_admin or r["created_by"] == current_uid]
    if allowed:
        ph2 = ','.join('?' * len(allowed))
        conn.execute(f"DELETE FROM stories WHERE id IN ({ph2})", allowed)
        conn.commit()
    conn.close()
    return jsonify(ok=True, deleted=allowed)


@app.route("/api/stories/bulk-assign", methods=["POST"])
@login_required
def api_bulk_assign():
    enforce_csrf()
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("story_ids", [])
    user_id = data.get("user_id")
    action  = data.get("action", "add")   # "add" or "remove"
    if not raw_ids or not user_id:
        return jsonify(ok=False, error="missing params"), 400
    try:
        story_ids = [int(i) for i in raw_ids]
        user_id   = int(user_id)
    except (ValueError, TypeError):
        return jsonify(ok=False, error="invalid params"), 400
    conn = get_db()
    if action == "remove":
        ph = ','.join('?' * len(story_ids))
        conn.execute(
            f"DELETE FROM story_users WHERE user_id=? AND story_id IN ({ph})",
            [user_id] + story_ids,
        )
    else:
        for sid in story_ids:
            conn.execute(
                "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
                (sid, user_id),
            )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/stories/bulk-sprint", methods=["POST"])
@login_required
def api_bulk_sprint():
    enforce_csrf()
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("story_ids", [])
    sprint  = data.get("sprint")   # None = remove from sprint
    if not raw_ids:
        return jsonify(ok=False, error="missing ids"), 400
    try:
        story_ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify(ok=False, error="invalid ids"), 400
    ph   = ','.join('?' * len(story_ids))
    conn = get_db()
    # If moving to a sprint, fix any stories that have no status_id
    if sprint is not None:
        for sid in story_ids:
            row = conn.execute("SELECT project_id, status_id FROM stories WHERE id=?", (sid,)).fetchone()
            if row and not row["status_id"]:
                first = conn.execute(
                    "SELECT id FROM statuses WHERE project_id=? ORDER BY order_index LIMIT 1",
                    (row["project_id"],),
                ).fetchone()
                if first:
                    conn.execute("UPDATE stories SET status_id=? WHERE id=?", (first["id"], sid))
    conn.execute(
        f"UPDATE stories SET sprint=?,updated_at=? WHERE id IN ({ph})",
        [sprint, int(time.time())] + story_ids,
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/stories/bulk-type", methods=["POST"])
@login_required
def api_bulk_type():
    enforce_csrf()
    data = request.get_json(silent=True) or {}
    raw_ids  = data.get("story_ids", [])
    type_id  = data.get("story_type")  # None = clear type
    if not raw_ids:
        return jsonify(ok=False, error="missing ids"), 400
    try:
        story_ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify(ok=False, error="invalid ids"), 400
    if type_id is not None:
        try:
            type_id = int(type_id)
        except (ValueError, TypeError):
            return jsonify(ok=False, error="invalid type_id"), 400
    ph   = ','.join('?' * len(story_ids))
    conn = get_db()
    conn.execute(
        f"UPDATE stories SET story_type=?,updated_at=? WHERE id IN ({ph})",
        [type_id, int(time.time())] + story_ids,
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/story-images/<filename>")
@login_required
def story_image(filename):
    return send_from_directory(Config.STORY_IMAGES_FOLDER, secure_filename(filename))


# ── Sticker API ───────────────────────────────────────────────────────────────

@app.route("/api/stickers/<int:project_id>")
@app.route("/api/stickers/<int:project_id>/<sprint>")
@login_required
def api_get_stickers(project_id, sprint=None):
    return jsonify([dict(s) for s in get_stickers(project_id)])


@app.route("/api/stickers", methods=["POST"])
@login_required
def api_create_sticker():
    enforce_csrf()
    data       = request.get_json(silent=True) or {}
    project_id = data.get("project_id")
    sprint     = data.get("sprint")
    stype      = data.get("type", "").strip()
    x          = float(data.get("x", 0))
    y          = float(data.get("y", 0))
    rotation   = float(data.get("rotation", 0))
    label      = data.get("label", "")

    if not project_id or not stype:
        abort(400)

    conn = get_db()
    cur  = conn.execute(
        """INSERT INTO stickers
           (project_id,sprint,type,x,y,rotation,label,created_by,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (project_id, sprint, stype, x, y, rotation,
         label, session["user_id"], int(time.time())),
    )
    sticker_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify(id=sticker_id, ok=True, creator_name=session.get("display_name", ""))


@app.route("/api/stickers/<int:sticker_id>", methods=["PATCH"])
@login_required
def api_update_sticker(sticker_id):
    enforce_csrf()
    data          = request.get_json(silent=True) or {}
    card_story_id = data.get("card_story_id")   # None = free on board

    conn = get_db()
    if card_story_id is not None:
        # Attaching to a card — use card-relative coords
        card_x = float(data.get("card_x", 0))
        card_y = float(data.get("card_y", 0))
        conn.execute(
            "UPDATE stickers SET card_story_id=?,card_x=?,card_y=? WHERE id=?",
            (int(card_story_id), card_x, card_y, sticker_id),
        )
    else:
        # Free on board — clear attachment, save board coords
        x        = float(data.get("x", 0))
        y        = float(data.get("y", 0))
        rotation = float(data.get("rotation", 0))
        conn.execute(
            "UPDATE stickers SET x=?,y=?,rotation=?,card_story_id=NULL,card_x=NULL,card_y=NULL WHERE id=?",
            (x, y, rotation, sticker_id),
        )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/stickers/<int:sticker_id>", methods=["DELETE"])
@login_required
def api_delete_sticker(sticker_id):
    enforce_csrf()
    conn = get_db()
    conn.execute("DELETE FROM stickers WHERE id=?", (sticker_id,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ── Story addons (tasks / mini-waterfall) ─────────────────────────────────────

@app.route("/api/story/<int:story_id>/addons", methods=["GET"])
@login_required
def api_get_addons(story_id):
    addons = get_story_addons(story_id, session["user_id"])
    return jsonify([dict(a) for a in addons])


@app.route("/api/story/<int:story_id>/addons", methods=["POST"])
@login_required
def api_create_addon(story_id):
    enforce_csrf()
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify(error="content required"), 400
    assigned = data.get("assigned_user_id") or None
    addon_id = create_addon(story_id, content, assigned, session["user_id"])
    return jsonify(id=addon_id, ok=True)


@app.route("/api/addon/<int:addon_id>", methods=["PATCH"])
@login_required
def api_update_addon(addon_id):
    enforce_csrf()
    data = request.get_json(silent=True) or {}
    if "is_done" in data:
        toggle_addon(addon_id, session["user_id"], int(bool(data["is_done"])))
        return jsonify(ok=True)
    update_addon_content(
        addon_id,
        content=data.get("content"),
        assigned_user_id=data.get("assigned_user_id"),
        order_index=data.get("order_index"),
    )
    return jsonify(ok=True)


@app.route("/api/addon/<int:addon_id>", methods=["DELETE"])
@login_required
def api_delete_addon(addon_id):
    enforce_csrf()
    delete_addon(addon_id)
    return jsonify(ok=True)


# ── Epics ─────────────────────────────────────────────────────────────────────

@app.route("/api/project/<int:project_id>/epics", methods=["GET"])
@login_required
def api_get_epics(project_id):
    return jsonify([dict(e) for e in get_epics(project_id)])


@app.route("/api/project/<int:project_id>/epics", methods=["POST"])
@login_required
def api_create_epic(project_id):
    enforce_csrf()
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify(error="title required"), 400
    color = data.get("color") or "#6B7280"
    desc  = (data.get("description") or "").strip()
    epic_id = create_epic(project_id, title, color, desc, session["user_id"])
    return jsonify(id=epic_id, ok=True)


@app.route("/api/epic/<int:epic_id>", methods=["DELETE"])
@admin_required
def api_delete_epic(epic_id):
    enforce_csrf()
    delete_epic(epic_id)
    return jsonify(ok=True)


# ── Admin: database backup ────────────────────────────────────────────────────

@app.route("/admin/backup")
@admin_required
def admin_backup():
    import shutil
    db_path = Config.DATABASE
    stamp   = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_name = f"gyra-backup-{stamp}.db"
    bak_path = os.path.join("/tmp", bak_name)
    shutil.copy2(db_path, bak_path)
    return send_from_directory("/tmp", bak_name, as_attachment=True,
                               download_name=bak_name)


# ── Entry-point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="127.0.0.1", port=5000)

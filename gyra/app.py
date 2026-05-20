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
from PIL import Image
from werkzeug.utils import secure_filename

from auth import (admin_required, decrypt_totp_secret, encrypt_totp_secret,
                  enforce_csrf, generate_setup_token, generate_totp_secret,
                  get_csrf_token, get_totp_uri, login_required, sha256_hex,
                  verify_setup_token, verify_totp)
from config import Config
from db import (ensure_story_types, get_all_active_users, get_all_sprints,
                get_backlog_stories, get_board_stories, get_current_sprint,
                get_db, get_project, get_projects, get_statuses, get_stickers,
                get_story, get_story_images, get_story_thumbnails, get_story_types,
                get_story_users, get_user_by_id, get_user_by_username, init_db)

app = Flask(__name__)
app.config.from_object(Config)

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
os.makedirs(Config.STORY_IMAGES_FOLDER, exist_ok=True)

ALLOWED_AVATAR_EXT  = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_IMAGE_EXT   = {"png", "jpg", "jpeg", "webp"}

# Allowed gerund/present-form verbs for Z field (server-side enforcement)
ALLOWED_VERBS = {
    "accessing", "adding", "allowing", "browsing", "building", "checking",
    "choosing", "clicking", "completing", "configuring", "connecting",
    "creating", "deleting", "deploying", "downloading", "editing",
    "exporting", "filtering", "finding", "generating", "implementing",
    "importing", "integrating", "launching", "loading", "logging",
    "managing", "monitoring", "navigating", "ordering", "processing",
    "reading", "running", "saving", "searching", "selecting", "setting",
    "sharing", "submitting", "switching", "testing", "tracking",
    "uploading", "using", "validating", "verifying", "viewing",
    "walking", "writing",
}


# ── Template filters ──────────────────────────────────────────────────────────

@app.template_filter("datetimeformat")
def datetimeformat(ts):
    if not ts:
        return "—"
    return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%d %b %Y %H:%M")


# ── Context processors ────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    return dict(
        projects=get_projects() if "user_id" in session else [],
        csrf_token=get_csrf_token,
    )


# ── DB initialisation ─────────────────────────────────────────────────────────

@app.before_request
def bootstrap():
    init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_AVATAR_EXT


def _allowed_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXT


def _count_words(*parts) -> int:
    return sum(len(str(p).split()) for p in parts if p)


def _build_story_title(actor, verb, z, x, for_conn, y) -> str:
    return " ".join(p for p in [actor, verb, z, x, for_conn, y] if p)


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

    statuses       = get_statuses(project_id)
    max_sprint     = get_current_sprint(project_id)
    all_sprints    = get_all_sprints(project_id)

    # Allow ?sprint=N to view a specific sprint; default to newest
    sprint_param   = request.args.get("sprint", type=int)
    current_sprint = sprint_param if sprint_param in all_sprints else max_sprint

    # Ensure this project has story types seeded
    with get_db() as _conn:
        ensure_story_types(project_id, _conn)

    raw_stories    = get_board_stories(project_id, current_sprint)

    stories = []
    for s in raw_stories:
        d = dict(s)
        d["assignees"] = get_story_users(s["id"])
        stories.append(d)

    story_ids  = [s["id"] for s in stories]
    thumbnails = get_story_thumbnails(story_ids)
    for s in stories:
        s["thumbnail"] = thumbnails.get(s["id"])

    board_map: dict = {st["id"]: [] for st in statuses}
    for s in stories:
        col = s["status_id"]
        if col in board_map:
            board_map[col].append(s)

    stickers = [dict(sk) for sk in get_stickers(project_id, current_sprint)]

    return render_template(
        "board.html",
        project=project,
        statuses=statuses,
        board_map=board_map,
        current_sprint=current_sprint,
        all_sprints=all_sprints,
        max_sprint=max_sprint,
        stickers=stickers,
    )


@app.route("/project/<int:project_id>/backlog")
@login_required
def backlog(project_id):
    project = get_project(project_id)
    if not project:
        abort(404)

    raw_stories    = get_backlog_stories(project_id)
    stories        = []
    for s in raw_stories:
        d = dict(s)
        d["assignees"] = get_story_users(s["id"])
        stories.append(d)

    statuses       = get_statuses(project_id)
    current_sprint = get_current_sprint(project_id)

    return render_template(
        "backlog.html",
        project=project,
        stories=stories,
        statuses=statuses,
        current_sprint=current_sprint,
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

        if not z or not x or not y or not project_id:
            flash("All story parts are required.", "error")
            return redirect(request.url)

        first_word = z.split()[0].lower() if z else ""
        if first_word not in ALLOWED_VERBS:
            flash(f"'{z.split()[0]}' is not a recognised verb. Use a gerund (e.g. Walking, Testing, Building).", "error")
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
                story_actor,story_verb,story_z,story_x,story_for,story_y,story_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, title, description, ac, points,
             status_id, sprint, order, now, session["user_id"], now,
             actor, verb, z, x, for_conn, y, story_type),
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
        return redirect(url_for("story_view", story_id=story_id))

    project     = get_project(project_id) if project_id else None
    statuses    = get_statuses(project_id) if project_id else []
    all_users   = get_all_active_users()
    if project_id:
        with get_db() as _c:
            ensure_story_types(project_id, _c)
    story_types = get_story_types(project_id) if project_id else []
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

            first_word = z.split()[0].lower() if z else ""
            if z and first_word not in ALLOWED_VERBS:
                flash(f"'{z.split()[0]}' is not a recognised verb.", "error")
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

            conn = get_db()
            conn.execute(
                """UPDATE stories SET title=?,description=?,acceptance_criteria=?,
                   story_points=?,status_id=?,sprint=?,updated_at=?,
                   story_actor=?,story_verb=?,story_z=?,story_x=?,story_for=?,story_y=?,
                   story_type=?
                   WHERE id=?""",
                (title, desc, ac, points, status_id, sprint, int(time.time()),
                 actor, verb, z, x, for_conn, y, story_type, story_id),
            )
            conn.execute("DELETE FROM story_users WHERE story_id=?", (story_id,))
            for uid in assignees:
                conn.execute(
                    "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
                    (story_id, uid),
                )
            conn.commit()
            conn.close()
            flash("Story updated.", "success")
            return redirect(url_for("story_view", story_id=story_id))

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
        all_projects.append(d)
    return render_template("admin_project.html", all_projects=all_projects)


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


# ── JSON API (board drag-drop) ────────────────────────────────────────────────

@app.route("/api/story/<int:story_id>/move", methods=["POST"])
@login_required
def api_move_story(story_id):
    enforce_csrf()
    data      = request.get_json(silent=True) or {}
    status_id = data.get("status_id")
    sprint    = data.get("sprint")
    order     = data.get("order_index", 0)

    conn = get_db()
    conn.execute(
        "UPDATE stories SET status_id=?,sprint=?,order_index=?,updated_at=? WHERE id=?",
        (status_id, sprint, order, int(time.time()), story_id),
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


@app.route("/story-images/<filename>")
@login_required
def story_image(filename):
    return send_from_directory(Config.STORY_IMAGES_FOLDER, secure_filename(filename))


# ── Sticker API ───────────────────────────────────────────────────────────────

@app.route("/api/stickers/<int:project_id>/<sprint>")
@login_required
def api_get_stickers(project_id, sprint):
    sp = None if sprint == "backlog" else int(sprint)
    return jsonify([dict(s) for s in get_stickers(project_id, sp)])


@app.route("/api/stickers", methods=["POST"])
@login_required
def api_create_sticker():
    enforce_csrf()
    data       = request.get_json(silent=True) or {}
    project_id = data.get("project_id")
    sprint     = data.get("sprint")
    stype      = data.get("type")
    x          = float(data.get("x", 0))
    y          = float(data.get("y", 0))
    rotation   = float(data.get("rotation", 0))
    label      = data.get("label", "")

    if not project_id or stype not in ("arrow", "exclamation"):
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
    return jsonify(id=sticker_id, ok=True)


@app.route("/api/stickers/<int:sticker_id>", methods=["PATCH"])
@login_required
def api_update_sticker(sticker_id):
    enforce_csrf()
    data     = request.get_json(silent=True) or {}
    x        = float(data.get("x", 0))
    y        = float(data.get("y", 0))
    rotation = float(data.get("rotation", 0))
    label    = data.get("label")

    conn = get_db()
    if label is not None:
        conn.execute(
            "UPDATE stickers SET x=?,y=?,rotation=?,label=? WHERE id=?",
            (x, y, rotation, label, sticker_id),
        )
    else:
        conn.execute(
            "UPDATE stickers SET x=?,y=?,rotation=? WHERE id=?",
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


# ── Entry-point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="127.0.0.1", port=5000)

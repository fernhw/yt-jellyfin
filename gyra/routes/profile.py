"""routes/profile.py — User profile, avatar serving, and notifications API."""
import os
import uuid

from flask import (flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from PIL import Image
from werkzeug.utils import secure_filename

from auth import enforce_csrf, login_required
from config import Config
from db import (get_db, get_notifications, get_unread_count,
                get_user_by_id, mark_notifications_read)
from routes.helpers import allowed_avatar


def register(app) -> None:

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        user = get_user_by_id(session["user_id"])

        if request.method == "POST":
            enforce_csrf()
            display_name = (request.form.get("display_name", "").strip()
                            or user["display_name"])
            new_avatar = user["avatar"]

            if "avatar" in request.files:
                f = request.files["avatar"]
                if f and f.filename and allowed_avatar(f.filename):
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
        return send_from_directory(Config.UPLOAD_FOLDER,
                                   secure_filename(filename))

    # ── Notifications ─────────────────────────────────────────────────────────

    @app.route("/api/notifications")
    @login_required
    def api_notifications():
        notes  = get_notifications(session["user_id"])
        result = [
            {
                "id":         n["id"],
                "type":       n["type"],
                "message":    n["message"],
                "story_id":   n["story_id"],
                "from_name":  n["from_name"],
                "is_read":    bool(n["is_read"]),
                "created_at": n["created_at"],
            }
            for n in notes
        ]
        return jsonify(
            notifications=result,
            unread=get_unread_count(session["user_id"]),
        )

    @app.route("/api/notifications/mark-read", methods=["POST"])
    @login_required
    def api_notifications_mark_read():
        enforce_csrf()
        mark_notifications_read(session["user_id"])
        return jsonify(ok=True)

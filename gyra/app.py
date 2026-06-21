"""
app.py — Gyra Flask application (thin shell — routes live in routes/).
Run:  python app.py
"""
import datetime
import os

from flask import Flask, session
from markupsafe import Markup, escape
from PIL import Image
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import get_csrf_token
from config import Config
from converters import StoryRefConverter
from db import (get_db, get_projects, get_unread_count,
                get_user_projects, init_db)
from routes import register_all

# F1: cap Pillow decode pixel count to mitigate decompression bombs.
# 50 MP comfortably covers a 8000×6000 photo but blocks crafted huge canvases.
Image.MAX_IMAGE_PIXELS = 50_000_000

# ── App factory ───────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config.from_object(Config)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
# Register custom URL converters so `<storyref:story_id>` accepts both bare
# numeric ids (back-compat) and composite KEY-NUM keys (e.g. CTL-124).
app.url_map.converters["storyref"] = StoryRefConverter
# Trust X-Forwarded-Prefix from nginx so url_for() works behind /gyra sub-path
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
os.makedirs(Config.STORY_IMAGES_FOLDER, exist_ok=True)

# ── Template filters ──────────────────────────────────────────────────────────

@app.template_filter("datetimeformat")
def datetimeformat(ts):
    if not ts:
        return "—"
    return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%d %b %Y %H:%M")

# ── Context processors ────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    from routes.helpers import ACTOR_OPTIONS, CONNECTOR_OPTIONS, VERB_OPTIONS
    base = dict(
        story_actor_options=ACTOR_OPTIONS,
        story_verb_options=VERB_OPTIONS,
        story_connector_options=CONNECTOR_OPTIONS,
        csrf_token=get_csrf_token,
    )
    if "user_id" not in session:
        return dict(base, projects=[], notif_count=0)
    uid     = session["user_id"]
    role    = session.get("role")
    projects    = get_projects() if role == "admin" else get_user_projects(uid)
    notif_count = get_unread_count(uid)
    return dict(base, projects=projects, notif_count=notif_count)

# ── DB initialisation ─────────────────────────────────────────────────────────
# init_db() is idempotent and process-guarded inside db.py, so calling it at
# import time covers both `python app.py` and WSGI servers that import this
# module. Running it inside before_request would re-acquire the migration lock
# on every single HTTP hit (cheap but pointless) and historically risked racy
# table swaps on first boot.
init_db()


@app.before_request
def bootstrap():
    # Refresh session role from DB on every request so admin-changed roles
    # take effect immediately without requiring the user to log out.
    uid = session.get("user_id")
    if uid:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT role, is_active FROM users WHERE id = ?", (uid,)
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["is_active"]:
            session.clear()
        elif row["role"] != session.get("role"):
            session["role"] = row["role"]

@app.after_request
def set_no_cache(response):
    """Prevent caching of HTML pages that contain CSRF tokens, and add
    baseline security headers on every response (G1)."""
    ct = response.content_type or ""
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
    # G2: JSON API responses should not be cached by intermediaries either.
    elif ct.startswith("application/json"):
        response.headers.setdefault("Cache-Control", "no-store, private")

    # G1: baseline security headers (safe, non-breaking — no CSP).
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if Config.SESSION_COOKIE_SECURE:
        # Only advertise HSTS when we actually expect HTTPS.
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response

# ── Register all route modules ────────────────────────────────────────────────

register_all(app)
# ── Error handlers ───────────────────────────────────────────────────────────

@app.errorhandler(413)
def request_entity_too_large(e):
    from flask import flash, redirect, request, url_for
    flash(
        "File too large — maximum upload size is 64 MB. "
        "Try splitting the sheet into smaller batches.",
        "error",
    )
    # Best-effort redirect back to wherever the upload came from.
    referrer = request.referrer
    if referrer:
        return redirect(referrer), 413
    return redirect(url_for("dashboard")), 413
# ── Entry-point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="127.0.0.1", port=5050)

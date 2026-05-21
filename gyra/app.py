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
from db import (get_db, get_projects, get_unread_count,
                get_user_projects, init_db)
from routes import register_all

# F1: cap Pillow decode pixel count to mitigate decompression bombs.
# 50 MP comfortably covers a 8000×6000 photo but blocks crafted huge canvases.
Image.MAX_IMAGE_PIXELS = 50_000_000

# ── App factory ───────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config.from_object(Config)
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
    if "user_id" not in session:
        return dict(projects=[], csrf_token=get_csrf_token, notif_count=0)
    uid     = session["user_id"]
    role    = session.get("role")
    projects    = get_projects() if role == "admin" else get_user_projects(uid)
    notif_count = get_unread_count(uid)
    return dict(projects=projects, csrf_token=get_csrf_token,
                notif_count=notif_count)

# ── DB initialisation ─────────────────────────────────────────────────────────

@app.before_request
def bootstrap():
    init_db()

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

# ── Entry-point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="127.0.0.1", port=5050)

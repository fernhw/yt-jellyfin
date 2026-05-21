"""routes/auth.py — Login, logout, TOTP setup."""
import threading
import time
from urllib.parse import urlparse

from flask import (abort, flash, redirect, render_template,
                   request, session, url_for)

from auth import (decrypt_totp_secret, encrypt_totp_secret,
                  enforce_csrf, generate_totp_secret, get_totp_uri,
                  sha256_hex, verify_setup_token, verify_totp)
from db import get_db, get_user_by_username
from routes.helpers import make_qr_png


# ── A1: open-redirect guard ──────────────────────────────────────────────────
def _safe_next(target):
    """Return *target* only if it is a same-origin relative path.
    Blocks absolute URLs, protocol-relative ('//evil.com'), schemes ('javascript:'),
    and anything that does not begin with a single '/'."""
    if not target or not isinstance(target, str):
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    p = urlparse(target)
    if p.scheme or p.netloc:
        return None
    return target


# ── A3: in-memory login throttle ─────────────────────────────────────────────
# Key:  "<username_lower>|<ip>"  →  [fail_count, window_start_epoch]
# 5 failures in 15 min ⇒ HTTP 429 for the remainder of the window.
_LOGIN_ATTEMPTS: dict = {}
_LOGIN_LOCK     = threading.Lock()
_LOGIN_WINDOW   = 15 * 60   # 15 minutes
_LOGIN_MAX_FAIL = 5


def _throttle_key(username: str) -> str:
    return f"{(username or '').lower()}|{request.remote_addr or '?'}"


def _throttle_blocked(key: str) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        entry = _LOGIN_ATTEMPTS.get(key)
        if not entry:
            return False
        count, started = entry
        if now - started > _LOGIN_WINDOW:
            _LOGIN_ATTEMPTS.pop(key, None)
            return False
        return count >= _LOGIN_MAX_FAIL


def _throttle_fail(key: str) -> None:
    now = time.time()
    with _LOGIN_LOCK:
        entry = _LOGIN_ATTEMPTS.get(key)
        if entry and now - entry[1] <= _LOGIN_WINDOW:
            entry[0] += 1
        else:
            _LOGIN_ATTEMPTS[key] = [1, now]


def _throttle_reset(key: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(key, None)


def register(app) -> None:

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "user_id" in session:
            return redirect(url_for("index"))

        if request.method == "POST":
            enforce_csrf()
            username = request.form.get("username", "").strip()
            code     = request.form.get("totp_code", "").strip()
            tkey     = _throttle_key(username)

            if _throttle_blocked(tkey):
                flash("Too many failed attempts. Try again in 15 minutes.", "error")
                return render_template("login.html"), 429

            user = get_user_by_username(username)

            # Constant-time path: always verify to prevent username enumeration.
            if not user or not user["totp_confirmed"] or not user["totp_secret_enc"]:
                verify_totp("", "000000")
                _throttle_fail(tkey)
                flash("Invalid credentials or account not yet configured.", "error")
                return render_template("login.html")

            if not verify_totp(user["totp_secret_enc"], code):
                _throttle_fail(tkey)
                flash("Invalid TOTP code.", "error")
                return render_template("login.html")

            # A5: rotate session on successful auth (drops any pre-login state).
            _throttle_reset(tkey)
            session.clear()
            session.permanent       = True
            session["user_id"]      = user["id"]
            session["username"]     = user["username"]
            session["display_name"] = user["display_name"]
            session["role"]         = user["role"]
            session["avatar"]       = user["avatar"]

            # A1: validate ?next= is a same-origin relative path.
            return redirect(_safe_next(request.args.get("next")) or url_for("index"))

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
        if not verify_setup_token(token, user["setup_token_hash"],
                                   user["setup_token_expires"] or 0):
            flash("Setup link has expired. Ask your admin to reset it.", "error")
            return redirect(url_for("login"))
        if user["totp_confirmed"]:
            flash("TOTP already configured. Log in normally.", "info")
            return redirect(url_for("login"))

        if not user["totp_secret_enc"]:
            secret   = generate_totp_secret()
            enc      = encrypt_totp_secret(secret)
            conn     = get_db()
            conn.execute("UPDATE users SET totp_secret_enc = ? WHERE id = ?",
                         (enc, user["id"]))
            conn.commit()
            conn.close()
            totp_enc = enc
        else:
            totp_enc = user["totp_secret_enc"]
            secret   = decrypt_totp_secret(totp_enc)

        uri     = get_totp_uri(secret, user["username"])
        qr_data = make_qr_png(uri)

        if request.method == "POST":
            enforce_csrf()
            code = request.form.get("totp_code", "").strip()
            if not verify_totp(totp_enc, code):
                flash("Code incorrect — try again.", "error")
                return render_template("setup_totp.html",
                                       qr=qr_data, secret=secret, user=user)
            conn = get_db()
            conn.execute(
                "UPDATE users SET totp_confirmed=1, "
                "setup_token_hash=NULL, setup_token_expires=NULL WHERE id=?",
                (user["id"],),
            )
            conn.commit()
            conn.close()
            flash("TOTP configured. You can now log in.", "success")
            return redirect(url_for("login"))

        return render_template("setup_totp.html",
                               qr=qr_data, secret=secret, user=user)

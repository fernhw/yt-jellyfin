"""routes/auth.py — Login, logout, account setup (password + optional TOTP)."""
import hashlib
import secrets
import threading
import time
from urllib.parse import urlparse

from flask import (abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from auth import (decrypt_totp_secret, encrypt_totp_secret,
                  enforce_csrf, generate_totp_secret, get_totp_uri,
                  hash_password, login_required, password_strength_error,
                  sha256_hex, verify_password, verify_setup_token,
                  verify_totp)
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


# ── Tongue-in-cheek "are you human?" gate ────────────────────────────────────
# The login page generates a one-time nonce in the session and ships it to
# the browser. A short JS script must compute  sha256(nonce + HUMAN_SALT)
# and stuff the hex digest into a hidden field. Form-spamming bots that do
# not execute JavaScript can't produce a valid proof, so the post is dropped
# without ever touching the password hash check.
#
# This is intentionally lightweight (no third-party reCAPTCHA / Turnstile);
# it just filters the lazy-bot floor. Real abuse is also throttled per
# username+IP by `_throttle_*` above.
HUMAN_SALT = "gyra:i-am-a-real-human-being:v1"


def _new_human_nonce() -> str:
    nonce = secrets.token_hex(16)
    session["_human_nonce"] = nonce
    return nonce


def _expected_human_proof(nonce: str) -> str:
    return hashlib.sha256((nonce + HUMAN_SALT).encode()).hexdigest()


def _human_check_ok() -> bool:
    nonce  = session.pop("_human_nonce", None)
    proof  = (request.form.get("human_proof") or "").strip().lower()
    if not nonce or not proof:
        return False
    return secrets.compare_digest(proof, _expected_human_proof(nonce))


def register(app) -> None:

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "user_id" in session:
            return redirect(url_for("index"))

        if request.method == "POST":
            enforce_csrf()
            # Human gate first — cheap, no DB hit. Pop nonce regardless so a
            # failed attempt can't be replayed.
            human_ok = _human_check_ok()

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            code     = request.form.get("totp_code", "").strip()
            tkey     = _throttle_key(username)

            if _throttle_blocked(tkey):
                flash("Too many failed attempts. Try again in 15 minutes.", "error")
                return render_template(
                    "login.html", human_nonce=_new_human_nonce(),
                ), 429

            if not human_ok:
                _throttle_fail(tkey)
                flash(
                    "Hmm — couldn't confirm you're human. Make sure JavaScript "
                    "is enabled and try again.",
                    "error",
                )
                return render_template(
                    "login.html", human_nonce=_new_human_nonce(),
                )

            user = get_user_by_username(username)

            generic_err = "Invalid credentials."

            if not user or not user["is_active"]:
                verify_password(password or "x", "pbkdf2:sha256:260000$x$" + ("0" * 64))
                _throttle_fail(tkey)
                flash(generic_err, "error")
                return render_template(
                    "login.html", human_nonce=_new_human_nonce(),
                )

            has_password = bool(user["password_hash"])
            has_totp     = bool(user["totp_confirmed"] and user["totp_secret_enc"])

            if not has_password and has_totp:
                if not verify_totp(user["totp_secret_enc"], code):
                    _throttle_fail(tkey)
                    flash("Invalid TOTP code.", "error")
                    return render_template(
                        "login.html", legacy_totp_only=True,
                        human_nonce=_new_human_nonce(),
                    )
            else:
                if not has_password:
                    _throttle_fail(tkey)
                    flash("Account not yet configured. Ask your admin for a setup link.", "error")
                    return render_template(
                        "login.html", human_nonce=_new_human_nonce(),
                    )
                if not verify_password(password, user["password_hash"]):
                    _throttle_fail(tkey)
                    flash(generic_err, "error")
                    return render_template(
                        "login.html", human_nonce=_new_human_nonce(),
                    )
                if has_totp and not verify_totp(user["totp_secret_enc"], code):
                    _throttle_fail(tkey)
                    flash("Invalid TOTP code.", "error")
                    return render_template(
                        "login.html",
                        prefill_username=username,
                        require_totp=True,
                        human_nonce=_new_human_nonce(),
                    )

            _throttle_reset(tkey)
            first_login = user["welcomed_at"] is None
            session.clear()
            session.permanent       = True
            session["user_id"]      = user["id"]
            session["username"]     = user["username"]
            session["display_name"] = user["display_name"]
            session["role"]         = user["role"]
            session["avatar"]       = user["avatar"]
            if first_login:
                session["_show_welcome"] = True

            return redirect(_safe_next(request.args.get("next")) or url_for("index"))

        return render_template("login.html", human_nonce=_new_human_nonce())

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ── Invite-link signup flow ──────────────────────────────────────────────
    # Step 1: GET /setup/<token>            — welcome + password form
    #         POST /setup/<token>           — save password, log in, redirect
    # Step 2: GET /account/totp?welcome=1   — optional TOTP enrolment
    #         POST /account/totp            — confirm code or "skip"

    def _load_setup_user(token):
        """Look up the user behind a setup token; flash + redirect on bad token.
        Returns (user_row, None) on success or (None, response) to bail out."""
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
            flash("Setup link has expired. Ask your admin for a new one.", "error")
            return None, redirect(url_for("login"))
        return user, None

    @app.route("/setup/<token>", methods=["GET", "POST"])
    def setup_account(token):
        # If somebody is already logged in (e.g. the admin testing the link),
        # clear their session so the invite page actually renders for the
        # invitee. Without this, base.html's `{% if session.user_id %}` branch
        # wins and the auth_content block (which setup_account.html uses) is
        # skipped, producing a blank page.
        if session.get("user_id"):
            session.clear()
            flash("Signed out so you can finish setting up this account.", "info")

        user, bail = _load_setup_user(token)
        if bail is not None:
            return bail

        # Already onboarded: send to login.
        if user["setup_complete"] and user["password_hash"]:
            flash("Account already set up. Please log in.", "info")
            return redirect(url_for("login"))

        if request.method == "POST":
            enforce_csrf(allow_viewer=True)
            pw1 = request.form.get("password", "")
            pw2 = request.form.get("password_confirm", "")
            err = None
            if pw1 != pw2:
                err = "Passwords do not match."
            else:
                err = password_strength_error(pw1)
            if err:
                flash(err, "error")
                return render_template("setup_account.html", user=user, token=token)

            conn = get_db()
            conn.execute(
                """UPDATE users
                      SET password_hash = ?,
                          setup_complete = 1,
                          setup_token_hash = NULL,
                          setup_token_expires = NULL,
                          setup_token_plain = NULL
                    WHERE id = ?""",
                (hash_password(pw1), user["id"]),
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE id = ?",
                                 (user["id"],)).fetchone()
            conn.close()

            # Auto-login the brand-new user and offer optional TOTP.
            session.clear()
            session.permanent       = True
            session["user_id"]      = user["id"]
            session["username"]     = user["username"]
            session["display_name"] = user["display_name"]
            session["role"]         = user["role"]
            session["avatar"]       = user["avatar"]
            return redirect(url_for("account_totp", welcome=1))

        return render_template("setup_account.html", user=user, token=token)

    # ── Account TOTP settings (any logged-in user) ───────────────────────────
    @app.route("/account/totp", methods=["GET", "POST"])
    @login_required
    def account_totp():
        welcome = request.args.get("welcome") == "1"
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?",
                            (session["user_id"],)).fetchone()

        # User already has TOTP — show "already enabled" view.
        if user["totp_confirmed"] and user["totp_secret_enc"]:
            if request.method == "POST":
                enforce_csrf(allow_viewer=True)
                if request.form.get("action") == "disable":
                    pw = request.form.get("password", "")
                    if not verify_password(pw, user["password_hash"] or ""):
                        conn.close()
                        flash("Wrong password — TOTP not disabled.", "error")
                        return redirect(url_for("account_totp"))
                    conn.execute(
                        "UPDATE users SET totp_secret_enc=NULL, "
                        "totp_confirmed=0 WHERE id=?", (user["id"],),
                    )
                    conn.commit()
                    conn.close()
                    flash("Two-factor authentication disabled.", "success")
                    return redirect(url_for("account_totp"))
            conn.close()
            return render_template("account_totp.html",
                                   user=user, enabled=True, welcome=False)

        # No TOTP yet — generate a pending secret (do not mark confirmed).
        if not user["totp_secret_enc"]:
            secret = generate_totp_secret()
            enc    = encrypt_totp_secret(secret)
            conn.execute("UPDATE users SET totp_secret_enc=? WHERE id=?",
                         (enc, user["id"]))
            conn.commit()
        else:
            enc    = user["totp_secret_enc"]
            secret = decrypt_totp_secret(enc)

        if request.method == "POST":
            enforce_csrf(allow_viewer=True)
            action = request.form.get("action", "confirm")
            if action == "skip":
                conn.close()
                if welcome:
                    flash(
                        "You can enable two-factor auth any time from Account → Security.",
                        "info",
                    )
                return redirect(url_for("index"))

            code = request.form.get("totp_code", "").strip()
            if not verify_totp(enc, code):
                conn.close()
                flash("Code incorrect — try again.", "error")
                return redirect(url_for("account_totp", welcome=1 if welcome else None))
            conn.execute("UPDATE users SET totp_confirmed=1 WHERE id=?",
                         (user["id"],))
            conn.commit()
            conn.close()
            flash("Two-factor authentication enabled.", "success")
            return redirect(url_for("index"))

        uri = get_totp_uri(secret, user["username"])
        qr  = make_qr_png(uri)
        conn.close()
        return render_template("account_totp.html",
                               user=user, enabled=False, welcome=welcome,
                               qr=qr, secret=secret)

    # ── First-login welcome modal: dismiss endpoint ──────────────────────────
    @app.post("/account/welcome/dismiss")
    @login_required
    def account_welcome_dismiss():
        enforce_csrf(allow_viewer=True)
        conn = get_db()
        conn.execute(
            "UPDATE users SET welcomed_at = ? WHERE id = ? AND welcomed_at IS NULL",
            (int(time.time()), session["user_id"]),
        )
        conn.commit()
        conn.close()
        session.pop("_show_welcome", None)
        return jsonify({"ok": True})

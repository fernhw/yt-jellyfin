"""routes/auth.py — Login, logout, TOTP setup."""
import time

from flask import (abort, flash, redirect, render_template,
                   request, session, url_for)

from auth import (decrypt_totp_secret, encrypt_totp_secret,
                  enforce_csrf, generate_totp_secret, get_totp_uri,
                  sha256_hex, verify_setup_token, verify_totp)
from db import get_db, get_user_by_username
from routes.helpers import make_qr_png


def register(app) -> None:

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

            session.permanent       = True
            session["user_id"]      = user["id"]
            session["username"]     = user["username"]
            session["display_name"] = user["display_name"]
            session["role"]         = user["role"]
            session["avatar"]       = user["avatar"]

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

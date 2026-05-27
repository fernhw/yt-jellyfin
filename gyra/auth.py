"""
auth.py — Authentication helpers.

Security model
──────────────
• No passwords stored.  Users authenticate with username + TOTP only.
• TOTP secrets are Fernet-encrypted before DB storage.
• Setup tokens are SHA-256 hashed before DB storage (one-time enrolment).
• Admin master password is SHA-256 (with salt) stored in .admin_key on disk —
  NEVER read by any web route; only by setup_admin.py CLI.
• CSRF tokens guard every mutating web form and Ajax call.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from functools import wraps
from typing import Optional

import pyotp
from cryptography.fernet import Fernet
from flask import abort, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config

# Dev API token — read from .api_token file (gitignored, local only).
# SECURITY: token-based auth is **disabled** unless GYRA_DEBUG=1 is set in the
# environment.  In production deployments leave GYRA_DEBUG unset; any
# Authorization: Bearer / ?_dev_token= request will then be ignored entirely.
_TOKEN_FILE  = os.path.join(os.path.dirname(__file__), ".api_token")
_DEBUG_MODE  = os.environ.get("GYRA_DEBUG") == "1"
_DEV_TOKEN   = None
if _DEBUG_MODE and os.path.exists(_TOKEN_FILE):
    _DEV_TOKEN = open(_TOKEN_FILE).read().strip() or None


# ── Fernet (symmetric encryption for TOTP secrets) ───────────────────────────

def _get_fernet() -> Fernet:
    if os.path.exists(Config.FERNET_KEY_FILE):
        with open(Config.FERNET_KEY_FILE, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        with open(Config.FERNET_KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(Config.FERNET_KEY_FILE, 0o600)
    return Fernet(key)


def encrypt_totp_secret(secret: str) -> str:
    return _get_fernet().encrypt(secret.encode()).decode()


def decrypt_totp_secret(enc: str) -> str:
    return _get_fernet().decrypt(enc.encode()).decode()


# ── TOTP ──────────────────────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(totp_secret_enc: str, code: str) -> bool:
    """Verify a 6-digit TOTP code against the encrypted secret."""
    try:
        secret = decrypt_totp_secret(totp_secret_enc)
        totp   = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)
    except Exception:
        return False


def get_totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name="Gyra"
    )


# ── One-time setup tokens ─────────────────────────────────────────────────────

def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def generate_setup_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash).  Store only the hash in the DB."""
    raw = secrets.token_urlsafe(32)
    return raw, sha256_hex(raw)


def verify_setup_token(raw: str, stored_hash: str, expires: int) -> bool:
    if time.time() > expires:
        return False
    return hmac.compare_digest(sha256_hex(raw), stored_hash)


# ── Passwords (primary auth, May 2026) ────────────────────────────────────────

def hash_password(password: str) -> str:
    """werkzeug pbkdf2-sha256 — salted, slow-by-default, no external deps."""
    return generate_password_hash(password, method="pbkdf2:sha256:260000")


def verify_password(password: str, stored_hash: str) -> bool:
    if not password or not stored_hash:
        return False
    try:
        return check_password_hash(stored_hash, password)
    except Exception:
        return False


def password_strength_error(password: str) -> Optional[str]:
    """Return a human error string, or None if the password is acceptable.

    Rules (kept in sync with the JS live-checker in setup_account.html):
      • at least 8 characters
      • at least one letter
      • at least one digit
      • at least one symbol (anything that isn't a letter or digit)
    """
    if not password or len(password) < 8:
        return "Password must be at least 8 characters long."
    if not any(c.isalpha() for c in password):
        return "Password must contain at least one letter."
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one number."
    if not any((not c.isalnum()) and (not c.isspace()) for c in password):
        return "Password must contain at least one symbol (e.g. ! @ # $ %)."
    return None


# ── Admin key (local only, never touched by web routes) ──────────────────────

def check_admin_key(password: str) -> bool:
    """Used only by setup_admin.py CLI — not by any Flask route."""
    if not os.path.exists(Config.ADMIN_KEY_FILE):
        return False
    try:
        with open(Config.ADMIN_KEY_FILE) as f:
            data = json.load(f)
        computed = hashlib.sha256(
            (password + data["salt"]).encode()
        ).hexdigest()
        return hmac.compare_digest(computed, data["hash"])
    except Exception:
        return False


# ── CSRF ─────────────────────────────────────────────────────────────────────

def get_csrf_token() -> str:
    """Return (and lazily create) the per-session CSRF token."""
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)
    return session["_csrf"]


def _verify_csrf(token) -> bool:
    stored = session.get("_csrf", "")
    if not stored or not token:
        return False
    return hmac.compare_digest(stored, token)


def enforce_csrf(*, allow_viewer: bool = False) -> None:
    """Call at the top of every mutating route (POST/PUT/DELETE).
    Reads the token from the form field *or* the X-CSRF-Token header.
    By default blocks viewer-role users from all write operations; pass
    ``allow_viewer=True`` on self-service account routes (TOTP enrol/skip,
    password change, welcome-dismiss, logout) so viewers can manage their
    own account."""
    if not allow_viewer and session.get("role") == "viewer":
        abort(403)
    token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
    if not _verify_csrf(token):
        abort(403)


# ── Route decorators ──────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        bearer_match = auth.startswith("Bearer ") and _DEV_TOKEN and hmac.compare_digest(auth[7:], _DEV_TOKEN)
        query_match  = _DEV_TOKEN and hmac.compare_digest(request.args.get("_dev_token", ""), _DEV_TOKEN)
        if bearer_match or query_match:
            if "user_id" not in session:
                session["user_id"]      = 1
                session["username"]     = "admin"
                session["display_name"] = "admin"
                session["role"]         = "admin"
            return f(*args, **kwargs)
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def super_user_required(f):
    """Allows super_user and admin roles."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") not in ("admin", "super_user"):
            abort(403)
        return f(*args, **kwargs)
    return wrapper

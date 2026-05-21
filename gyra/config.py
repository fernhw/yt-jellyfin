import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_or_create_key(path: str) -> str:
    """Load a persistent secret key from disk, creating it if absent."""
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


class Config:
    BASE_DIR = BASE_DIR
    DATABASE = os.path.join(BASE_DIR, "gyra.db")
    # ── Local-only secrets (never served via web) ──────────────────────────
    ADMIN_KEY_FILE  = os.path.join(BASE_DIR, ".admin_key")
    FERNET_KEY_FILE = os.path.join(BASE_DIR, ".fernet_key")
    SECRET_KEY      = _load_or_create_key(os.path.join(BASE_DIR, ".secret_key"))
    # ── Avatars ─────────────────────────────────────────────────────────────
    UPLOAD_FOLDER        = os.path.join(BASE_DIR, "static", "avatars")
    STORY_IMAGES_FOLDER  = os.path.join(BASE_DIR, "static", "story-images")
    MAX_CONTENT_LENGTH   = 8 * 1024 * 1024  # 8 MB (covers story images)
    # ── Session hardening ────────────────────────────────────────────────────
    SESSION_COOKIE_HTTPONLY  = True
    SESSION_COOKIE_SAMESITE  = "Lax"
    # A4: opt-in HTTPS-only cookie. Enable by exporting GYRA_SECURE_COOKIE=1
    # when serving behind cloudflared / nginx-TLS. Leave off for localhost.
    SESSION_COOKIE_SECURE    = os.environ.get("GYRA_SECURE_COOKIE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 86400  # 24 h

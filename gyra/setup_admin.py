#!/usr/bin/env python3
"""
setup_admin.py — Gyra local admin CLI.

This script runs ONLY on the server.  It NEVER starts a web listener.
The admin master password it stores (.admin_key) is SHA-256 + salt hashed
and is read exclusively by this script — no Flask route touches the file.

Usage:
    python setup_admin.py                        # initial setup
    python setup_admin.py reset-totp <username>  # regenerate TOTP enrol link
    python setup_admin.py list-users             # print all users
"""
import getpass
import hashlib
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from config import Config
from db import get_db, init_db
from auth import (
    encrypt_totp_secret,
    generate_totp_secret,
    get_totp_uri,
    generate_setup_token,
)


# ── Admin key helpers ─────────────────────────────────────────────────────────

def _write_admin_key(password: str) -> None:
    salt     = secrets.token_hex(16)
    key_hash = hashlib.sha256((password + salt).encode()).hexdigest()
    with open(Config.ADMIN_KEY_FILE, "w") as f:
        json.dump({"hash": key_hash, "salt": salt}, f)
    os.chmod(Config.ADMIN_KEY_FILE, 0o600)
    print(f"  ✓ Admin key saved → {Config.ADMIN_KEY_FILE}")


def _verify_admin_key(password: str) -> bool:
    if not os.path.exists(Config.ADMIN_KEY_FILE):
        return False
    with open(Config.ADMIN_KEY_FILE) as f:
        data = json.load(f)
    return hashlib.sha256((password + data["salt"]).encode()).hexdigest() == data["hash"]


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_setup() -> None:
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("       Gyra — Initial Admin Setup      ")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    init_db()

    conn     = get_db()
    existing = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    conn.close()

    if existing:
        print("An admin account already exists.")
        print("To reset TOTP: python setup_admin.py reset-totp <username>\n")
        return

    username     = input("Admin username  : ").strip()
    email        = input("Admin email     : ").strip()
    display_name = input("Display name    : ").strip()

    if not all([username, email, display_name]):
        sys.exit("All fields are required.")

    print("\nSet the admin MASTER password.")
    print("Stored locally in .admin_key (SHA-256 + salt).")
    print("NEVER read by the web application.\n")

    while True:
        pw  = getpass.getpass("Master password  : ")
        pw2 = getpass.getpass("Confirm password : ")
        if pw == pw2 and len(pw) >= 12:
            break
        print("Passwords must match and be ≥ 12 characters.\n")

    _write_admin_key(pw)

    totp_secret = generate_totp_secret()
    totp_enc    = encrypt_totp_secret(totp_secret)

    conn = get_db()
    conn.execute(
        """INSERT INTO users
           (username,email,display_name,role,totp_secret_enc,totp_confirmed,created_at)
           VALUES (?,?,?,'admin',?,1,?)""",
        (username, email, display_name, totp_enc, int(time.time())),
    )
    conn.commit()
    conn.close()

    uri = get_totp_uri(totp_secret, username)
    print(f"\n  ✓ Admin user '{username}' created.\n")
    print("  ─── TOTP provisioning URI ───────────────────────────────────────")
    print(f"  {uri}")
    print("  ─────────────────────────────────────────────────────────────────")

    try:
        import qrcode as _qr
        qr = _qr.QRCode()
        qr.add_data(uri)
        qr.make(fit=True)
        qr.print_ascii()
    except ImportError:
        print("\n  (Install qrcode[pil] to print the QR in the terminal.)")

    print("\n  Scan the QR / URI with your authenticator app, then run:")
    print("  python app.py\n")


def cmd_reset_totp(username: str) -> None:
    print(f"\nResetting TOTP for '{username}'…")
    pw = getpass.getpass("Admin master password: ")
    if not _verify_admin_key(pw):
        sys.exit("Invalid master password.")

    conn = get_db()
    user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        conn.close()
        sys.exit(f"User '{username}' not found.")

    raw_token, token_hash = generate_setup_token()
    expires = int(time.time()) + 86400 * 7

    conn.execute(
        """UPDATE users SET totp_secret_enc=NULL,totp_confirmed=0,
           setup_token_hash=?,setup_token_expires=? WHERE id=?""",
        (token_hash, expires, user["id"]),
    )
    conn.commit()
    conn.close()

    base_url = input("App base URL (e.g. http://localhost:5000): ").strip().rstrip("/")
    print(f"\n  Setup link for '{username}' (valid 7 days):")
    print(f"  {base_url}/setup/{raw_token}\n")


def cmd_list_users() -> None:
    conn  = get_db()
    users = conn.execute(
        "SELECT id,username,email,role,is_active,totp_confirmed FROM users ORDER BY id"
    ).fetchall()
    conn.close()

    print(f"\n{'ID':>4}  {'Username':<20}  {'Email':<30}  {'Role':<6}  {'Active':>6}  {'TOTP':>7}")
    print("─" * 82)
    for u in users:
        print(
            f"{u['id']:>4}  {u['username']:<20}  {u['email']:<30}  "
            f"{u['role']:<6}  {'yes' if u['is_active'] else 'no':>6}  "
            f"{'ok' if u['totp_confirmed'] else 'pending':>7}"
        )
    print()


# ── Entry-point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        cmd_setup()
    elif args[0] == "reset-totp" and len(args) == 2:
        cmd_reset_totp(args[1])
    elif args[0] == "list-users":
        cmd_list_users()
    else:
        print(__doc__)

#!/usr/bin/env python3
"""
dbcheck.py — Operational tool for the Gyra SQLite database.

Subcommands:
  check     Run PRAGMA integrity_check + foreign_key_check.
  backup    Write a consistent hot-copy under gyra/backups/.
  checkpoint  Force a TRUNCATE checkpoint of the WAL.
  vacuum    Reclaim free pages (locks the DB briefly).
  all       backup + check + checkpoint (recommended weekly/cron entry).

Safe to run while the Flask app is live (uses SQLite's online APIs).

Examples
--------
  python3 dbcheck.py check
  python3 dbcheck.py backup
  python3 dbcheck.py all
"""
import argparse
import os
import sqlite3
import sys
import time

from config import Config
from db import db_backup, db_integrity_check


BACKUP_DIR = os.path.join(Config.BASE_DIR, "backups")


def cmd_check() -> int:
    res = db_integrity_check()
    if res["ok"]:
        print("OK: integrity_check=ok, no FK violations")
        return 0
    print("FAIL")
    if res["errors"]:
        print("  integrity_check:")
        for e in res["errors"]:
            print("   ", e)
    if res["fk_violations"]:
        print("  foreign_key_check (table, rowid, parent, fkid):")
        for v in res["fk_violations"]:
            print("   ", v)
    return 1


def cmd_backup() -> int:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"gyra-{time.strftime('%Y%m%d-%H%M%S')}.db")
    db_backup(dest)
    size = os.path.getsize(dest)
    print(f"backup: {dest} ({size} bytes)")
    # Retention: keep the 14 most recent backups.
    backups = sorted(
        (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
         if f.startswith("gyra-") and f.endswith(".db")),
        reverse=True,
    )
    for old in backups[14:]:
        try:
            os.remove(old)
            print(f"pruned:  {old}")
        except OSError:
            pass
    return 0


def cmd_checkpoint() -> int:
    conn = sqlite3.connect(Config.DATABASE, timeout=30.0)
    try:
        busy, log, ck = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        print(f"checkpoint: busy={busy} log_frames={log} checkpointed={ck}")
        return 1 if busy else 0
    finally:
        conn.close()


def cmd_vacuum() -> int:
    conn = sqlite3.connect(Config.DATABASE, timeout=60.0)
    try:
        before = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.execute("VACUUM")
        after = conn.execute("PRAGMA page_count").fetchone()[0]
        print(f"vacuum: pages {before} -> {after}")
        return 0
    finally:
        conn.close()


def cmd_all() -> int:
    rc = cmd_backup()
    rc |= cmd_check()
    rc |= cmd_checkpoint()
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description="Gyra DB health utility")
    parser.add_argument(
        "cmd",
        choices=("check", "backup", "checkpoint", "vacuum", "all"),
        help="operation to run",
    )
    args = parser.parse_args()
    return {
        "check":      cmd_check,
        "backup":     cmd_backup,
        "checkpoint": cmd_checkpoint,
        "vacuum":     cmd_vacuum,
        "all":        cmd_all,
    }[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())

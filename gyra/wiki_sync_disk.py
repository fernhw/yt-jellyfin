#!/usr/bin/env python3
"""wiki_sync_disk.py — Register on-disk .md files that have no DB row.

Run this whenever you've written or dropped .md files directly into
  gyra/wiki_content/<PROJECT_KEY>/articles/
and want them to appear in the wiki without going through the site UI.

Usage:
    python3 wiki_sync_disk.py                    # sync ALL projects
    python3 wiki_sync_disk.py AMY                # sync one project by key
    python3 wiki_sync_disk.py AMY --dry-run      # show what would be added

Safe to run repeatedly — only inserts rows that are missing, never touches
existing DB rows or .md file content.
"""

import os
import sys
import time
import sqlite3

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "gyra.db")
WIKI_ROOT  = os.path.join(BASE_DIR, "wiki_content")
ADMIN_UID  = 1   # fallback created_by user id


def _read_title(path: str, fallback: str) -> str:
    """Extract title= from [META] section, or fall back to slug."""
    try:
        in_meta = False
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                if line == "[META]":
                    in_meta = True
                    continue
                if in_meta and line.startswith("["):
                    break
                if in_meta and line.startswith("title="):
                    t = line[6:].strip()
                    return t if t else fallback
    except OSError:
        pass
    return fallback


def sync_project(conn: sqlite3.Connection, project_id: int, project_key: str,
                 dry_run: bool) -> tuple:
    adir = os.path.join(WIKI_ROOT, project_key.upper(), "articles")
    if not os.path.isdir(adir):
        return [], []

    now   = int(time.time())
    added = []
    skipped = []

    for fname in sorted(os.listdir(adir)):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]

        exists = conn.execute(
            "SELECT 1 FROM wiki_articles WHERE project_id=? AND slug=?",
            (project_id, slug)
        ).fetchone()

        if exists:
            skipped.append(slug)
            continue

        title = _read_title(os.path.join(adir, fname), slug)

        max_order = conn.execute(
            "SELECT COALESCE(MAX(order_index),0) FROM wiki_articles "
            "WHERE project_id=? AND parent_id IS NULL",
            (project_id,)
        ).fetchone()[0]

        if not dry_run:
            conn.execute(
                """INSERT INTO wiki_articles
                   (project_id, slug, title, parent_id, order_index, params,
                    is_published, created_by, created_at, updated_by, updated_at)
                   VALUES (?,?,?,NULL,?,?,1,?,?,?,?)""",
                (project_id, slug, title, max_order + 1.0,
                 "{}", ADMIN_UID, now, ADMIN_UID, now)
            )

        added.append((slug, title))

    if not dry_run:
        conn.commit()

    return added, skipped


def main():
    args      = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run   = "--dry-run" in sys.argv
    key_filter = args[0].upper() if args else None

    if dry_run:
        print("[DRY RUN] No changes will be written.\n")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    projects = conn.execute(
        "SELECT id, key, name FROM projects" +
        (" WHERE UPPER(key)=?" if key_filter else ""),
        (key_filter,) if key_filter else ()
    ).fetchall()

    if not projects:
        print(f"No project found{f' with key {key_filter}' if key_filter else ''}.")
        conn.close()
        sys.exit(1)

    total_added = 0
    for p in projects:
        added, skipped = sync_project(conn, p["id"], p["key"], dry_run)
        label = f"[{p['key']}] {p['name']}"
        if added:
            print(f"\n{label} — {len(added)} article(s) {'would be added' if dry_run else 'added'}:")
            for slug, title in added:
                print(f"  + {slug}  ({title})")
        if not added:
            print(f"\n{label} — nothing to add ({len(skipped)} already registered)")
        total_added += len(added)

    conn.close()
    print(f"\nDone. {total_added} article(s) {'would be registered' if dry_run else 'registered'}.")


if __name__ == "__main__":
    main()

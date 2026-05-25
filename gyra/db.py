"""
db.py — SQLite schema + thin data-access helpers.
All queries use parameterised statements; no string interpolation in SQL.
"""
import os
import sqlite3
import threading
import time
from config import Config

# ── Init guard ────────────────────────────────────────────────────────────────
# init_db() may be invoked from multiple entry points (app startup, helper
# scripts, ad-hoc imports). The migration block in _migrate_db() performs
# destructive table swaps (DROP/RENAME); running them concurrently from two
# threads would race and could leave half-renamed tables behind. We gate the
# whole flow on a process-wide lock + "done once" flag so it is safe to call
# init_db() repeatedly without risking corruption.
_INIT_LOCK = threading.Lock()
_INITIALIZED = False

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT    UNIQUE NOT NULL COLLATE NOCASE,
    email               TEXT    UNIQUE NOT NULL COLLATE NOCASE,
    display_name        TEXT    NOT NULL,
    avatar              TEXT    DEFAULT NULL,
    totp_secret_enc     TEXT    DEFAULT NULL,
    totp_confirmed      INTEGER DEFAULT 0,
    setup_token_hash    TEXT    DEFAULT NULL,
    setup_token_expires INTEGER DEFAULT NULL,
    role                TEXT    DEFAULT 'user' CHECK(role IN ('admin','user')),
    is_active           INTEGER DEFAULT 1,
    created_at          INTEGER NOT NULL,
    created_by          INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    key         TEXT    UNIQUE NOT NULL COLLATE NOCASE,
    created_at  INTEGER NOT NULL,
    created_by  INTEGER REFERENCES users(id)
);

-- Kanban columns — their IDs, names, colours and order paint the board.
CREATE TABLE IF NOT EXISTS statuses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    color       TEXT    DEFAULT '#6B7280',
    order_index INTEGER DEFAULT 0,
    is_done     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stories (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title                TEXT    NOT NULL,
    description          TEXT    DEFAULT '',
    acceptance_criteria  TEXT    DEFAULT '',
    story_points         INTEGER DEFAULT 0,
    status_id            INTEGER REFERENCES statuses(id),
    sprint               INTEGER DEFAULT NULL,
    order_index          INTEGER DEFAULT 0,
    created_at           INTEGER NOT NULL,
    created_by           INTEGER REFERENCES users(id),
    updated_at           INTEGER,
    -- Structured user-story parts (enforced format)
    story_actor          TEXT    DEFAULT NULL,
    story_verb           TEXT    DEFAULT NULL,
    story_z              TEXT    DEFAULT NULL,
    story_x              TEXT    DEFAULT NULL,
    story_for            TEXT    DEFAULT NULL,
    story_y              TEXT    DEFAULT NULL,
    story_type           INTEGER DEFAULT NULL REFERENCES story_types(id)
);

-- Multiple assignees / reporters per story (many-to-many).
CREATE TABLE IF NOT EXISTS story_users (
    story_id INTEGER NOT NULL REFERENCES stories(id)  ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    role     TEXT    DEFAULT 'assignee',
    PRIMARY KEY (story_id, user_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);

-- Story image attachments
CREATE TABLE IF NOT EXISTS story_images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    filename   TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);

-- Story type labels (Bug, Feature, Task …) — project-level, colour-coded
CREATE TABLE IF NOT EXISTS story_types (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    color       TEXT    NOT NULL DEFAULT '#6B7280',
    order_index INTEGER DEFAULT 0
);

-- Board overlay stickers (arrows, exclamation marks)
CREATE TABLE IF NOT EXISTS stickers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sprint     INTEGER DEFAULT NULL,
    type       TEXT    NOT NULL CHECK(type IN ('arrow','exclamation')),
    x          REAL    DEFAULT 0,
    y          REAL    DEFAULT 0,
    rotation   REAL    DEFAULT 0,
    label      TEXT    DEFAULT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at INTEGER NOT NULL
);

-- Epics: theme groups that span multiple stories
CREATE TABLE IF NOT EXISTS epics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    color       TEXT    NOT NULL DEFAULT '#6B7280',
    description TEXT    DEFAULT '',
    created_at  INTEGER NOT NULL,
    created_by  INTEGER REFERENCES users(id)
);

-- Story sub-tasks / checklist ("mini waterfall")
CREATE TABLE IF NOT EXISTS story_addons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id         INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    content          TEXT    NOT NULL,
    assigned_user_id INTEGER REFERENCES users(id),
    order_index      INTEGER DEFAULT 0,
    created_at       INTEGER NOT NULL,
    created_by       INTEGER REFERENCES users(id)
);

-- Per-user completion state for each addon item
CREATE TABLE IF NOT EXISTS addon_statuses (
    addon_id   INTEGER NOT NULL REFERENCES story_addons(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    is_done    INTEGER DEFAULT 0,
    updated_at INTEGER,
    PRIMARY KEY (addon_id, user_id)
);

-- Story change history log
CREATE TABLE IF NOT EXISTS story_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    field_name TEXT    NOT NULL,
    old_value  TEXT    DEFAULT NULL,
    new_value  TEXT    DEFAULT NULL,
    created_at INTEGER NOT NULL
);

-- User notifications
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    story_id    INTEGER REFERENCES stories(id) ON DELETE CASCADE,
    from_user   INTEGER REFERENCES users(id),
    is_read     INTEGER DEFAULT 0,
    created_at  INTEGER NOT NULL
);

-- Project membership
CREATE TABLE IF NOT EXISTS project_members (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_by    INTEGER REFERENCES users(id),
    added_at    INTEGER NOT NULL,
    PRIMARY KEY (project_id, user_id)
);

-- Grooming: queued stories, current voting state, per-user votes
CREATE TABLE IF NOT EXISTS grooming_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    story_id    INTEGER NOT NULL REFERENCES stories(id)  ON DELETE CASCADE,
    order_index INTEGER DEFAULT 0,
    added_by    INTEGER REFERENCES users(id),
    added_at    INTEGER NOT NULL,
    UNIQUE (project_id, story_id)
);

CREATE TABLE IF NOT EXISTS grooming_state (
    project_id       INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    active_story_id  INTEGER REFERENCES stories(id) ON DELETE SET NULL,
    revealed         INTEGER DEFAULT 0,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS grooming_votes (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    story_id   INTEGER NOT NULL REFERENCES stories(id)  ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    vote       TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (project_id, story_id, user_id)
);

-- Initiatives ("Grand Epics"): roll up multiple epics under one strategic goal
CREATE TABLE IF NOT EXISTS initiatives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    color       TEXT    NOT NULL DEFAULT '#6D28D9',
    rule_type   TEXT    NOT NULL DEFAULT 'priority'
                CHECK (rule_type IN ('priority','pct_stories','pct_points','count_stories','count_points')),
    status      TEXT    NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','active','shipped','archived')),
    created_by  INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS initiative_milestones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    initiative_id INTEGER NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    threshold     TEXT    NOT NULL,
    order_index   INTEGER DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS initiative_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    initiative_id INTEGER NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    field_name    TEXT    NOT NULL,
    old_value     TEXT    DEFAULT NULL,
    new_value     TEXT    DEFAULT NULL,
    created_at    INTEGER NOT NULL
);
"""


# ── Connection ────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    # timeout=30 → if another writer holds the lock, wait up to 30s rather than
    # failing instantly with "database is locked". Combined with PRAGMA
    # busy_timeout (set below) for belt-and-braces.
    conn = sqlite3.connect(Config.DATABASE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL + NORMAL is the SQLite-recommended combo for durability vs throughput
    # on a single host. journal_mode is persistent on the DB file; the rest are
    # per-connection so we set them every time.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")  # 10s
    return conn


def db_integrity_check() -> dict:
    """Run SQLite's integrity + FK checks. Returns a summary dict — empty
    'errors' and 'fk_violations' lists mean the DB is healthy."""
    conn = get_db()
    try:
        integ = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        fkv   = conn.execute("PRAGMA foreign_key_check").fetchall()
        return {
            "ok":             integ == ["ok"] and not fkv,
            "errors":         [] if integ == ["ok"] else integ,
            "fk_violations":  [tuple(r) for r in fkv],
        }
    finally:
        conn.close()


def db_backup(dest_path: str) -> None:
    """Make a consistent online backup of the DB to dest_path, safe to call
    while the app is running (uses SQLite's backup API)."""
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    src = sqlite3.connect(Config.DATABASE)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


def init_db() -> None:
    """Apply schema + migrations. Safe to call repeatedly; the heavy lifting
    only runs once per process."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        conn = get_db()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _migrate_db()
        _INITIALIZED = True


def _migrate_db() -> None:
    """Add columns that were added after the initial schema (idempotent)."""
    new_cols = [
        ("stories", "story_actor", "TEXT DEFAULT NULL"),
        ("stories", "story_verb",  "TEXT DEFAULT NULL"),
        ("stories", "story_z",     "TEXT DEFAULT NULL"),
        ("stories", "story_x",     "TEXT DEFAULT NULL"),
        ("stories", "story_for",   "TEXT DEFAULT NULL"),
        ("stories", "story_y",     "TEXT DEFAULT NULL"),
        ("stories", "story_type",  "INTEGER DEFAULT NULL"),
        ("stories", "priority",    "TEXT DEFAULT NULL"),
        ("stories", "epic_id",     "INTEGER DEFAULT NULL"),
        ("stories", "is_archived", "INTEGER DEFAULT 0"),
        ("stories", "software_version", "TEXT DEFAULT NULL"),
        ("stories", "os",          "TEXT DEFAULT NULL"),
        # ── Container/Attachment model (sovereign-story system) ─────────────
        # box_type:        only set on Container stories. One of
        #                  'whitebox' | 'blackbox' | 'greybox' | 'featurebox'.
        # attached_to:     only set on Attachment stories — points at the
        #                  Container story that hosts this one. Strict 1:1.
        # dependent_action:short string the Container declares onto the
        #                  Attachment ("DO NOT integrate yet", "Wire to slot 3"…).
        ("stories", "box_type",          "TEXT DEFAULT NULL"),
        ("stories", "attached_to",       "INTEGER DEFAULT NULL"),
        ("stories", "dependent_action",  "TEXT DEFAULT NULL"),
        # ── Per-project sequential story number (May 2026) ─────────────────
        # The integer PK `id` is globally unique but meaningless to users.
        # `story_number` is the per-project sequence (1, 2, 3, …) used to
        # build human-readable keys like CTL-1, AMY-42. URLs and display
        # labels prefer `story_number`; `id` remains the internal handle.
        ("stories", "story_number",      "INTEGER DEFAULT NULL"),
        ("epics",   "start_date",  "TEXT DEFAULT NULL"),
        ("epics",   "due_date",    "TEXT DEFAULT NULL"),
        ("epics",   "status",      "TEXT DEFAULT 'planning'"),
        ("epics",   "updated_at",  "INTEGER DEFAULT NULL"),
        ("epics",   "order_index", "INTEGER DEFAULT 0"),
        ("epics",   "initiative_id", "INTEGER DEFAULT NULL"),
        ("epics",   "is_archived", "INTEGER DEFAULT 0"),
    ]
    conn = get_db()
    for table, col, col_def in new_cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except Exception:
            pass  # Column already exists

    # Ensure initiative tables exist on already-migrated DBs.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS initiatives (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name        TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            color       TEXT    NOT NULL DEFAULT '#6D28D9',
            rule_type   TEXT    NOT NULL DEFAULT 'priority',
            status      TEXT    NOT NULL DEFAULT 'draft',
            created_by  INTEGER REFERENCES users(id),
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS initiative_milestones (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            initiative_id INTEGER NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE,
            name          TEXT    NOT NULL,
            threshold     TEXT    NOT NULL,
            order_index   INTEGER DEFAULT 0,
            created_at    INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS initiative_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            initiative_id INTEGER NOT NULL REFERENCES initiatives(id) ON DELETE CASCADE,
            user_id       INTEGER NOT NULL REFERENCES users(id),
            field_name    TEXT    NOT NULL,
            old_value     TEXT    DEFAULT NULL,
            new_value     TEXT    DEFAULT NULL,
            created_at    INTEGER NOT NULL
        );
    """)
    conn.commit()

    # Migrate stickers: drop obsolete CHECK constraint, add card-attachment columns.
    # Idempotent: only runs if card_story_id column is missing.
    sticker_cols = [r[1] for r in conn.execute("PRAGMA table_info(stickers)").fetchall()]
    if 'card_story_id' not in sticker_cols:
        # Wrap the table-swap in a transaction so a crash mid-migration cannot
        # leave behind a half-renamed table.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stickers_v2 (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    sprint        INTEGER DEFAULT NULL,
                    type          TEXT    NOT NULL,
                    x             REAL    DEFAULT 0,
                    y             REAL    DEFAULT 0,
                    rotation      REAL    DEFAULT 0,
                    label         TEXT    DEFAULT NULL,
                    created_by    INTEGER REFERENCES users(id),
                    created_at    INTEGER NOT NULL,
                    card_story_id INTEGER DEFAULT NULL REFERENCES stories(id) ON DELETE SET NULL,
                    card_x        REAL    DEFAULT NULL,
                    card_y        REAL    DEFAULT NULL
                )
            """)
            conn.execute(
                "INSERT INTO stickers_v2 "
                "SELECT id,project_id,sprint,type,x,y,rotation,label,created_by,created_at,"
                "NULL,NULL,NULL FROM stickers"
            )
            conn.execute("DROP TABLE stickers")
            conn.execute("ALTER TABLE stickers_v2 RENAME TO stickers")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys = ON")
            raise
        conn.execute("PRAGMA foreign_keys = ON")

    # Seed project_members: add all active users to all projects (backward compat)
    cnt = conn.execute("SELECT COUNT(*) FROM project_members").fetchone()[0]
    if cnt == 0:
        now = int(time.time())
        conn.execute(
            "INSERT OR IGNORE INTO project_members (project_id, user_id, added_at) "
            "SELECT p.id, u.id, ? FROM projects p, users u WHERE u.is_active = 1",
            (now,),
        )

    # ── Backfill stories.story_number per project (idempotent) ─────────────
    # Assigns 1, 2, 3 … to every existing story within each project, ordered
    # by id ASC. Only touches rows where story_number IS NULL, so re-running
    # is a no-op and new stories created with explicit numbers are preserved.
    try:
        missing = conn.execute(
            "SELECT COUNT(*) FROM stories WHERE story_number IS NULL"
        ).fetchone()[0]
    except Exception:
        missing = 0
    if missing:
        try:
            conn.execute("BEGIN IMMEDIATE")
            project_ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT project_id FROM stories WHERE story_number IS NULL"
            ).fetchall()]
            for pid in project_ids:
                # Continue numbering after the current max (handles partial backfills).
                start_row = conn.execute(
                    "SELECT COALESCE(MAX(story_number),0) AS mx "
                    "FROM stories WHERE project_id = ?",
                    (pid,),
                ).fetchone()
                n = int(start_row["mx"] or 0)
                rows = conn.execute(
                    "SELECT id FROM stories WHERE project_id = ? "
                    "AND story_number IS NULL ORDER BY id ASC",
                    (pid,),
                ).fetchall()
                for r in rows:
                    n += 1
                    conn.execute(
                        "UPDATE stories SET story_number = ? WHERE id = ?",
                        (n, r["id"]),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # Enforce uniqueness of (project_id, story_number) — partial index skips
    # any NULLs that might briefly appear during a failed backfill.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_stories_project_number "
            "ON stories(project_id, story_number) "
            "WHERE story_number IS NOT NULL"
        )
        conn.commit()
    except Exception:
        pass

    # Migrate users: add super_user to role CHECK constraint.
    # Idempotent: only runs if the current CHECK still excludes 'super_user'.
    user_cols = conn.execute("PRAGMA table_info(users)").fetchall()
    role_col  = next((r for r in user_cols if r[1] == 'role'), None)
    # We detect a need to migrate by trying an INSERT with super_user and catching
    # a constraint error — but that's too invasive. Instead we check the sqlite_master.
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if schema_row and "'super_user'" not in schema_row[0]:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users_v2 (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    username            TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                    email               TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                    display_name        TEXT    NOT NULL,
                    avatar              TEXT    DEFAULT NULL,
                    totp_secret_enc     TEXT    DEFAULT NULL,
                    totp_confirmed      INTEGER DEFAULT 0,
                    setup_token_hash    TEXT    DEFAULT NULL,
                    setup_token_expires INTEGER DEFAULT NULL,
                    role                TEXT    DEFAULT 'user' CHECK(role IN ('admin','user','super_user')),
                    is_active           INTEGER DEFAULT 1,
                    created_at          INTEGER NOT NULL,
                    created_by          INTEGER REFERENCES users_v2(id)
                )
            """)
            conn.execute("INSERT INTO users_v2 SELECT * FROM users")
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_v2 RENAME TO users")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys = ON")
            raise
        conn.execute("PRAGMA foreign_keys = ON")

    # Migrate users: add viewer to role CHECK constraint.
    # Idempotent: only runs if the current CHECK still excludes 'viewer'.
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if schema_row and "'viewer'" not in schema_row[0]:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users_v3 (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    username            TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                    email               TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                    display_name        TEXT    NOT NULL,
                    avatar              TEXT    DEFAULT NULL,
                    totp_secret_enc     TEXT    DEFAULT NULL,
                    totp_confirmed      INTEGER DEFAULT 0,
                    setup_token_hash    TEXT    DEFAULT NULL,
                    setup_token_expires INTEGER DEFAULT NULL,
                    role                TEXT    DEFAULT 'user' CHECK(role IN ('admin','user','super_user','viewer')),
                    is_active           INTEGER DEFAULT 1,
                    created_at          INTEGER NOT NULL,
                    created_by          INTEGER REFERENCES users_v3(id)
                )
            """)
            conn.execute("INSERT INTO users_v3 SELECT * FROM users")
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_v3 RENAME TO users")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys = ON")
            raise
        conn.execute("PRAGMA foreign_keys = ON")

    # Repair stale self-FK on users.created_by → users_v3(id) left behind by an
    # earlier in-place rename. SQLite's ALTER TABLE ... RENAME does not always
    # rewrite self-references embedded in the CREATE statement. We detect the
    # condition by inspecting sqlite_master and rebuild the table with the
    # correct REFERENCES users(id) clause. Idempotent.
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if schema_row and "REFERENCES users_v" in schema_row[0]:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE users_fix (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    username            TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                    email               TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                    display_name        TEXT    NOT NULL,
                    avatar              TEXT    DEFAULT NULL,
                    totp_secret_enc     TEXT    DEFAULT NULL,
                    totp_confirmed      INTEGER DEFAULT 0,
                    setup_token_hash    TEXT    DEFAULT NULL,
                    setup_token_expires INTEGER DEFAULT NULL,
                    role                TEXT    DEFAULT 'user' CHECK(role IN ('admin','user','super_user','viewer')),
                    is_active           INTEGER DEFAULT 1,
                    created_at          INTEGER NOT NULL,
                    created_by          INTEGER REFERENCES users(id)
                )
            """)
            conn.execute("INSERT INTO users_fix SELECT * FROM users")
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_fix RENAME TO users")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys = ON")
            raise
        conn.execute("PRAGMA foreign_keys = ON")

    conn.commit()
    # WAL truncate: keep wal file from growing forever during long-lived runs.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    conn.close()


# ── Users ─────────────────────────────────────────────────────────────────────

def get_user_by_username(username: str):
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
    ).fetchone()
    conn.close()
    return row


def get_user_by_id(user_id: int):
    conn = get_db()
    row  = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_all_active_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, display_name, avatar, role FROM users "
        "WHERE is_active = 1 ORDER BY display_name"
    ).fetchall()
    conn.close()
    return rows


# ── Projects ──────────────────────────────────────────────────────────────────

def get_projects():
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def get_user_projects(user_id: int):
    """Return only projects the user is a member of."""
    conn = get_db()
    rows = conn.execute(
        "SELECT p.* FROM projects p "
        "JOIN project_members pm ON pm.project_id = p.id AND pm.user_id = ? "
        "ORDER BY p.created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_project_members(project_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT u.id, u.username, u.display_name, u.avatar, u.role, pm.added_at "
        "FROM project_members pm JOIN users u ON u.id = pm.user_id "
        "WHERE pm.project_id = ? ORDER BY u.display_name",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


def user_in_project(user_id: int, project_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM project_members WHERE project_id=? AND user_id=?",
        (project_id, user_id),
    ).fetchone()
    conn.close()
    return row is not None


def get_project(project_id: int):
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    conn.close()
    return row


# ── Statuses ──────────────────────────────────────────────────────────────────

def get_statuses(project_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM statuses WHERE project_id = ? ORDER BY order_index",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


# ── Story Types ──────────────────────────────────────────────────────────────

_DEFAULT_TYPES = [
    ("Bug",         "#EF4444", 0),
    ("Feature",     "#7C3AED", 1),
    ("Task",        "#3B82F6", 2),
    ("Improvement", "#10B981", 3),
    ("Chore",       "#6B7280", 4),
]


def ensure_story_types(project_id: int, conn) -> None:
    """Seed default story types for a project if none exist."""
    existing = conn.execute(
        "SELECT COUNT(*) AS cnt FROM story_types WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if existing and existing["cnt"] == 0:
        conn.executemany(
            "INSERT INTO story_types (project_id, name, color, order_index) VALUES (?,?,?,?)",
            [(project_id, name, color, order) for name, color, order in _DEFAULT_TYPES],
        )
        conn.commit()


def get_story_types(project_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM story_types WHERE project_id = ? ORDER BY order_index",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


# ── Stories ───────────────────────────────────────────────────────────────────

def get_story(story_id: int):
    conn = get_db()
    row  = conn.execute(
        """SELECT s.*,
                  p.name    AS project_name,
                  p.key     AS project_key,
                  st.name   AS status_name,
                  st.color  AS status_color,
                  u.display_name AS creator_name,
                  sty.name  AS story_type_name,
                  sty.color AS story_type_color
           FROM stories s
           JOIN projects p     ON s.project_id = p.id
           LEFT JOIN statuses st  ON s.status_id  = st.id
           LEFT JOIN users    u   ON s.created_by = u.id
           LEFT JOIN story_types sty ON s.story_type = sty.id
           WHERE s.id = ?""",
        (story_id,),
    ).fetchone()
    conn.close()
    return row


def get_story_users(story_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT u.id, u.username, u.display_name, u.avatar, su.role
           FROM story_users su
           JOIN users u ON su.user_id = u.id
           WHERE su.story_id = ?""",
        (story_id,),
    ).fetchall()
    conn.close()
    return rows


def get_board_stories(project_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, st.name AS status_name, st.color AS status_color,
                  COALESCE(st.is_done,0) AS status_is_done,
                  sty.name AS story_type_name, sty.color AS story_type_color,
                  e.title AS epic_title, e.color AS epic_color,
                  COALESCE(pst.is_done,0) AS parent_is_done,
                  p.box_type AS parent_box_type
           FROM stories s
           LEFT JOIN statuses st   ON s.status_id = st.id
           LEFT JOIN story_types sty ON s.story_type = sty.id
           LEFT JOIN epics e ON s.epic_id = e.id
           LEFT JOIN stories p ON s.attached_to = p.id
           LEFT JOIN statuses pst ON p.status_id = pst.id
           WHERE s.project_id = ? AND s.sprint IS NOT NULL AND s.is_archived = 0
           ORDER BY s.order_index""",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


def get_backlog_stories(project_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, st.name AS status_name, st.color AS status_color
           FROM stories s
           LEFT JOIN statuses st ON s.status_id = st.id
           WHERE s.project_id = ? AND s.sprint IS NULL AND s.is_archived = 0
           ORDER BY s.order_index""",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


def get_current_sprint(project_id: int) -> int:
    conn = get_db()
    row  = conn.execute(
        "SELECT MAX(sprint) AS mx FROM stories WHERE project_id = ? AND sprint IS NOT NULL",
        (project_id,),
    ).fetchone()
    conn.close()
    return row["mx"] if row and row["mx"] is not None else 1


def get_all_sprints(project_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT sprint FROM stories WHERE project_id=? AND sprint IS NOT NULL ORDER BY sprint",
        (project_id,),
    ).fetchall()
    conn.close()
    return [r["sprint"] for r in rows]


# ── Story images ──────────────────────────────────────────────────────────────

def get_story_images(story_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM story_images WHERE story_id = ? ORDER BY id",
        (story_id,),
    ).fetchall()
    conn.close()
    return rows


def get_story_thumbnails(story_ids: list) -> dict:
    """Return {story_id: filename} for the first image of each story."""
    if not story_ids:
        return {}
    placeholders = ",".join("?" * len(story_ids))
    conn = get_db()
    rows = conn.execute(
        f"""SELECT si.story_id, si.filename
            FROM story_images si
            INNER JOIN (
                SELECT story_id, MIN(id) AS min_id
                FROM story_images
                WHERE story_id IN ({placeholders})
                GROUP BY story_id
            ) m ON si.id = m.min_id""",
        story_ids,
    ).fetchall()
    conn.close()
    return {r["story_id"]: r["filename"] for r in rows}


def get_story_previews(story_ids: list, limit: int = 5) -> dict:
    """Return {story_id: [filename, ...]} with up to `limit` images per story."""
    if not story_ids:
        return {}
    placeholders = ",".join("?" * len(story_ids))
    conn = get_db()
    rows = conn.execute(
        f"""SELECT story_id, filename
            FROM story_images
            WHERE story_id IN ({placeholders})
            ORDER BY story_id, id""",
        story_ids,
    ).fetchall()
    conn.close()
    result: dict = {}
    for r in rows:
        sid = r["story_id"]
        if sid not in result:
            result[sid] = []
        if len(result[sid]) < limit:
            result[sid].append(r["filename"])
    return result


# ── Stickers ──────────────────────────────────────────────────────────────────

def get_stickers(project_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, u.display_name AS creator_name
           FROM stickers s
           LEFT JOIN users u ON u.id = s.created_by
           WHERE s.project_id=?""",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


# ── Epics ─────────────────────────────────────────────────────────────────────

def get_epics(project_id: int, include_archived: bool = False):
    conn = get_db()
    if include_archived:
        rows = conn.execute(
            "SELECT * FROM epics WHERE project_id=? ORDER BY order_index, id",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM epics WHERE project_id=? AND COALESCE(is_archived,0)=0 "
            "ORDER BY order_index, id",
            (project_id,),
        ).fetchall()
    conn.close()
    return rows


def create_epic(project_id: int, title: str, color: str, description: str, user_id: int):
    conn = get_db()
    nxt  = conn.execute(
        "SELECT COALESCE(MAX(order_index),0)+1 AS n FROM epics WHERE project_id=?",
        (project_id,),
    ).fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO epics (project_id,title,color,description,created_at,created_by,order_index) "
        "VALUES (?,?,?,?,?,?,?)",
        (project_id, title, color, description, int(time.time()), user_id, nxt),
    )
    conn.commit()
    epic_id = cur.lastrowid
    conn.close()
    return epic_id


def delete_epic(epic_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM epics WHERE id=?", (epic_id,))
    conn.commit()
    conn.close()


def move_epic(epic_id: int, direction: str):
    """Swap an epic's order_index with its neighbor in the same project.
    direction: 'up' (lower order_index) or 'down' (higher order_index).
    Returns (ok, swapped_with_id_or_None)."""
    if direction not in ("up", "down"):
        return False, None
    conn = get_db()
    me = conn.execute(
        "SELECT id, project_id, order_index FROM epics WHERE id=?",
        (epic_id,),
    ).fetchone()
    if not me:
        conn.close()
        return False, None

    if direction == "up":
        neighbor = conn.execute(
            "SELECT id, order_index FROM epics "
            "WHERE project_id=? AND (order_index < ? OR (order_index = ? AND id < ?)) "
            "ORDER BY order_index DESC, id DESC LIMIT 1",
            (me["project_id"], me["order_index"], me["order_index"], me["id"]),
        ).fetchone()
    else:
        neighbor = conn.execute(
            "SELECT id, order_index FROM epics "
            "WHERE project_id=? AND (order_index > ? OR (order_index = ? AND id > ?)) "
            "ORDER BY order_index ASC, id ASC LIMIT 1",
            (me["project_id"], me["order_index"], me["order_index"], me["id"]),
        ).fetchone()

    if not neighbor:
        conn.close()
        return False, None

    # If both rows share the same order_index (legacy data), give them
    # distinct values before swapping so the order actually changes.
    a_oi = me["order_index"]
    b_oi = neighbor["order_index"]
    if a_oi == b_oi:
        if direction == "up":
            a_oi, b_oi = b_oi, b_oi + 1
        else:
            a_oi, b_oi = b_oi + 1, b_oi
        # Pull adjacent value down by 1 so swap creates a gap-free order.
        # Simpler: just assign distinct ints; full re-sequence happens lazily.
    conn.execute("UPDATE epics SET order_index=? WHERE id=?", (b_oi, me["id"]))
    conn.execute("UPDATE epics SET order_index=? WHERE id=?", (a_oi, neighbor["id"]))
    conn.commit()
    conn.close()
    return True, neighbor["id"]


def reorder_epics(project_id: int, ordered_ids):
    """Assign order_index = position for each id, scoped to project_id.
    Silently ignores ids that don't belong to the project."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id FROM epics WHERE project_id=?", (project_id,)
    ).fetchall()
    valid = {r["id"] for r in rows}
    pos = 0
    for eid in ordered_ids:
        try:
            eid_i = int(eid)
        except (TypeError, ValueError):
            continue
        if eid_i not in valid:
            continue
        conn.execute(
            "UPDATE epics SET order_index=? WHERE id=? AND project_id=?",
            (pos, eid_i, project_id),
        )
        pos += 1
    conn.commit()
    conn.close()


def get_epic(epic_id: int):
    conn = get_db()
    row = conn.execute(
        """SELECT e.*, u.display_name AS creator_name
           FROM epics e
           LEFT JOIN users u ON u.id = e.created_by
           WHERE e.id=?""",
        (epic_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_epic(epic_id: int, fields: dict) -> None:
    allowed = {"title", "color", "description", "start_date", "due_date", "status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(int(time.time()))
    vals.append(epic_id)
    conn = get_db()
    conn.execute(f"UPDATE epics SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()


def get_epic_stories_full(epic_id: int):
    """All stories under an epic with status info + story points."""
    conn = get_db()
    rows = conn.execute(
        """SELECT s.id, s.title, s.status_id, s.story_points, s.sprint, s.priority,
                  s.story_z, s.updated_at,
                  st.name  AS status_name,
                  st.color AS status_color,
                  st.is_done AS status_is_done
           FROM stories s
           LEFT JOIN statuses st ON st.id = s.status_id
           WHERE s.epic_id=? AND COALESCE(s.is_archived,0)=0
           ORDER BY st.order_index, s.id""",
        (epic_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_epics_with_stats(project_id: int):
    """Epics for a project with aggregate stats."""
    conn = get_db()
    rows = conn.execute(
        """SELECT e.*,
                  COUNT(s.id)                                   AS total_stories,
                  SUM(CASE WHEN st.is_done=1 THEN 1 ELSE 0 END) AS done_stories,
                  COALESCE(SUM(s.story_points),0)               AS total_points,
                  COALESCE(SUM(CASE WHEN st.is_done=1 THEN s.story_points ELSE 0 END),0) AS done_points
           FROM epics e
           LEFT JOIN stories  s  ON s.epic_id = e.id AND COALESCE(s.is_archived,0)=0
           LEFT JOIN statuses st ON st.id     = s.status_id
           WHERE e.project_id=?
           GROUP BY e.id
           ORDER BY e.title""",
        (project_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Story addons (tasks/checklist) ────────────────────────────────────────────

def get_stories_tasks_batch(story_ids: list) -> dict:
    """Return {story_id: [task_dicts]} for all given story ids."""
    if not story_ids:
        return {}
    conn = get_db()
    ph   = ','.join('?' * len(story_ids))
    rows = conn.execute(
        f"""SELECT sa.id, sa.story_id, sa.content, sa.assigned_user_id,
                   u.display_name AS assigned_name
            FROM story_addons sa
            LEFT JOIN users u ON sa.assigned_user_id = u.id
            WHERE sa.story_id IN ({ph})
            ORDER BY sa.story_id, sa.order_index""",
        story_ids,
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        sid = r['story_id']
        if sid not in result:
            result[sid] = []
        result[sid].append(dict(r))
    return result


def get_story_addons(story_id: int, current_user_id: int = None):
    conn = get_db()
    rows = conn.execute(
        """SELECT sa.*, u.display_name AS assigned_name, u.avatar AS assigned_avatar
           FROM story_addons sa
           LEFT JOIN users u ON sa.assigned_user_id = u.id
           WHERE sa.story_id = ? ORDER BY sa.order_index""",
        (story_id,),
    ).fetchall()
    statuses = {}
    if current_user_id:
        for r in conn.execute(
            "SELECT addon_id, is_done FROM addon_statuses WHERE user_id=?",
            (current_user_id,),
        ).fetchall():
            statuses[r["addon_id"]] = r["is_done"]
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["is_done_by_me"] = statuses.get(r["id"], 0)
        result.append(d)
    return result


def create_addon(story_id: int, content: str, assigned_user_id, user_id: int) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(MAX(order_index),0)+1 AS nxt FROM story_addons WHERE story_id=?",
        (story_id,),
    ).fetchone()
    cur = conn.execute(
        "INSERT INTO story_addons (story_id,content,assigned_user_id,order_index,created_at,created_by) VALUES (?,?,?,?,?,?)",
        (story_id, content, assigned_user_id, row["nxt"], int(time.time()), user_id),
    )
    conn.commit()
    addon_id = cur.lastrowid
    conn.close()
    return addon_id


def update_addon_content(addon_id: int, content: str = None, assigned_user_id=None, order_index=None) -> None:
    conn = get_db()
    if content is not None:
        conn.execute("UPDATE story_addons SET content=? WHERE id=?", (content, addon_id))
    if assigned_user_id is not None:
        conn.execute("UPDATE story_addons SET assigned_user_id=? WHERE id=?", (assigned_user_id, addon_id))
    if order_index is not None:
        conn.execute("UPDATE story_addons SET order_index=? WHERE id=?", (order_index, addon_id))
    conn.commit()
    conn.close()


def delete_addon(addon_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM story_addons WHERE id=?", (addon_id,))
    conn.commit()
    conn.close()


def toggle_addon(addon_id: int, user_id: int, is_done: int) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO addon_statuses (addon_id,user_id,is_done,updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(addon_id,user_id) DO UPDATE
             SET is_done=excluded.is_done, updated_at=excluded.updated_at""",
        (addon_id, user_id, is_done, int(time.time())),
    )
    conn.commit()
    conn.close()


# ── Story history ─────────────────────────────────────────────────────────────

def log_story_change(story_id: int, user_id: int, field_name: str, old_value, new_value) -> None:
    """Log a field change; no-op if old == new."""
    if str(old_value or '') == str(new_value or ''):
        return
    conn = get_db()
    conn.execute(
        "INSERT INTO story_history (story_id,user_id,field_name,old_value,new_value,created_at) VALUES (?,?,?,?,?,?)",
        (story_id, user_id, field_name,
         str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None,
         int(time.time())),
    )
    conn.commit()
    conn.close()


def get_story_history(story_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT sh.*, u.display_name, u.avatar
           FROM story_history sh
           JOIN users u ON sh.user_id = u.id
           WHERE sh.story_id = ? ORDER BY sh.created_at DESC""",
        (story_id,),
    ).fetchall()
    conn.close()
    return rows


# ── Notifications ─────────────────────────────────────────────────────────────

def create_notification(user_id: int, type_: str, message: str,
                        story_id=None, from_user=None) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO notifications (user_id,type,message,story_id,from_user,created_at) VALUES (?,?,?,?,?,?)",
        (user_id, type_, message, story_id, from_user, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_notifications(user_id: int, limit: int = 30):
    conn = get_db()
    rows = conn.execute(
        """SELECT n.*, u.display_name as from_name, u.avatar as from_avatar,
                  s.title as story_title
           FROM notifications n
           LEFT JOIN users u ON u.id = n.from_user
           LEFT JOIN stories s ON s.id = n.story_id
           WHERE n.user_id = ?
           ORDER BY n.created_at DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def get_unread_count(user_id: int) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def mark_notifications_read(user_id: int) -> None:
    conn = get_db()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ── Initiatives ───────────────────────────────────────────────────────────────

RULE_TYPES = ("priority", "pct_stories", "pct_points",
              "count_stories", "count_points")
INIT_STATUSES = ("draft", "active", "shipped", "archived")


def get_initiatives(project_id, status_filter=None):
    conn = get_db()
    q = ("SELECT i.*, "
         "  (SELECT COUNT(*) FROM epics e WHERE e.initiative_id = i.id) "
         "    AS epic_count, "
         "  (SELECT COUNT(*) FROM initiative_milestones m WHERE m.initiative_id = i.id) "
         "    AS milestone_count "
         "FROM initiatives i WHERE i.project_id = ?")
    args = [project_id]
    if status_filter and status_filter in INIT_STATUSES:
        q += " AND i.status = ?"
        args.append(status_filter)
    q += " ORDER BY CASE i.status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 " \
         " WHEN 'shipped' THEN 2 WHEN 'archived' THEN 3 END, i.updated_at DESC"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    return rows


def get_initiative(initiative_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM initiatives WHERE id=?", (initiative_id,)).fetchone()
    conn.close()
    return row


def create_initiative(project_id, name, description, color, rule_type,
                      created_by):
    if rule_type not in RULE_TYPES:
        rule_type = "priority"
    now = int(time.time())
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO initiatives (project_id,name,description,color,rule_type,"
        " status,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, name, description or "", color or "#6D28D9",
         rule_type, "draft", created_by, now, now),
    )
    iid = cur.lastrowid
    conn.commit()
    conn.close()
    return iid


def update_initiative(initiative_id, fields):
    """fields: dict of column→value. Only updates whitelisted columns."""
    allowed = {"name", "description", "color", "rule_type", "status"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    args.append(int(time.time()))
    args.append(initiative_id)
    conn = get_db()
    conn.execute(
        f"UPDATE initiatives SET {','.join(sets)} WHERE id=?", args)
    conn.commit()
    conn.close()


def delete_initiative(initiative_id):
    conn = get_db()
    # Detach epics first (don't delete them — they live on)
    conn.execute(
        "UPDATE epics SET initiative_id=NULL WHERE initiative_id=?",
        (initiative_id,))
    conn.execute("DELETE FROM initiatives WHERE id=?", (initiative_id,))
    conn.commit()
    conn.close()


def list_initiative_milestones(initiative_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM initiative_milestones WHERE initiative_id=? "
        "ORDER BY order_index, id", (initiative_id,)).fetchall()
    conn.close()
    return rows


def add_initiative_milestone(initiative_id, name, threshold, order_index=0):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO initiative_milestones (initiative_id,name,threshold,"
        " order_index,created_at) VALUES (?,?,?,?,?)",
        (initiative_id, name, str(threshold), order_index, int(time.time())))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


def delete_initiative_milestone(milestone_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM initiative_milestones WHERE id=?", (milestone_id,))
    conn.commit()
    conn.close()


def list_initiative_epics(initiative_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM epics WHERE initiative_id=? ORDER BY order_index, id",
        (initiative_id,)).fetchall()
    conn.close()
    return rows


def list_unattached_epics(project_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM epics WHERE project_id=? AND initiative_id IS NULL "
        "ORDER BY title", (project_id,)).fetchall()
    conn.close()
    return rows


def set_epic_initiative(epic_id, initiative_id):
    conn = get_db()
    conn.execute(
        "UPDATE epics SET initiative_id=?, updated_at=? WHERE id=?",
        (initiative_id, int(time.time()), epic_id))
    conn.commit()
    conn.close()


def log_initiative_change(initiative_id, user_id, field_name,
                          old_value, new_value):
    if str(old_value or '') == str(new_value or ''):
        return
    conn = get_db()
    conn.execute(
        "INSERT INTO initiative_history (initiative_id,user_id,field_name,"
        " old_value,new_value,created_at) VALUES (?,?,?,?,?,?)",
        (initiative_id, user_id, field_name,
         str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None,
         int(time.time())))
    conn.commit()
    conn.close()


def get_initiative_history(initiative_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT h.*, u.display_name, u.avatar FROM initiative_history h "
        "JOIN users u ON h.user_id = u.id "
        "WHERE h.initiative_id=? ORDER BY h.created_at DESC",
        (initiative_id,)).fetchall()
    conn.close()
    return rows


def compute_initiative_rollup(initiative_id):
    """Return a dict with aggregate counts across all stories under all
    epics linked to this initiative.

    Keys: total_stories, done_stories, total_points, done_points,
          by_priority -> { 'VH': {'total': n, 'done': n, 'points': n,
                                  'done_points': n}, ... }
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT s.priority, s.story_points, st.is_done
        FROM stories s
        JOIN epics e ON s.epic_id = e.id
        LEFT JOIN statuses st ON s.status_id = st.id
        WHERE e.initiative_id = ? AND s.is_archived = 0
        """, (initiative_id,)).fetchall()
    conn.close()

    out = {
        "total_stories": 0, "done_stories": 0,
        "total_points":  0, "done_points":  0,
        "by_priority": {p: {"total": 0, "done": 0,
                            "points": 0, "done_points": 0}
                        for p in ("VH", "H", "M", "L", "VL", "")},
    }
    for r in rows:
        pri = (r["priority"] or "").upper()
        if pri not in out["by_priority"]:
            pri = ""
        pts = r["story_points"] or 0
        done = 1 if r["is_done"] else 0
        out["total_stories"] += 1
        out["total_points"] += pts
        out["done_stories"] += done
        out["done_points"]  += pts if done else 0
        bp = out["by_priority"][pri]
        bp["total"] += 1
        bp["points"] += pts
        if done:
            bp["done"] += 1
            bp["done_points"] += pts
    return out


def evaluate_initiative_progress(initiative_id):
    """Returns (overall_pct, milestones) where milestones is a list of
    {id, name, threshold, achieved (bool), progress_pct (0-100), label}."""
    init = get_initiative(initiative_id)
    if not init:
        return 0.0, []
    rule = init["rule_type"]
    rollup = compute_initiative_rollup(initiative_id)
    milestones = list_initiative_milestones(initiative_id)

    # Overall pct under the rule.
    overall = _overall_for_rule(rule, rollup)

    out = []
    PRI_ORDER = ["VH", "H", "M", "L", "VL"]
    for m in milestones:
        thr_raw = (m["threshold"] or "").strip()
        progress, achieved, label = 0.0, False, thr_raw
        if rule == "pct_stories":
            try:
                target = float(thr_raw)
            except ValueError:
                target = 0.0
            achieved_pct = (100.0 * rollup["done_stories"]
                            / rollup["total_stories"]) \
                if rollup["total_stories"] else 0.0
            progress = (achieved_pct / target * 100.0) if target else 0.0
            achieved = achieved_pct >= target
            label = f"{target:g}% of stories done"
        elif rule == "pct_points":
            try:
                target = float(thr_raw)
            except ValueError:
                target = 0.0
            achieved_pct = (100.0 * rollup["done_points"]
                            / rollup["total_points"]) \
                if rollup["total_points"] else 0.0
            progress = (achieved_pct / target * 100.0) if target else 0.0
            achieved = achieved_pct >= target
            label = f"{target:g}% of points done"
        elif rule == "count_stories":
            try:
                target = int(float(thr_raw))
            except ValueError:
                target = 0
            progress = (100.0 * rollup["done_stories"] / target) \
                if target else 0.0
            achieved = rollup["done_stories"] >= target
            label = f"{target} stories done"
        elif rule == "count_points":
            try:
                target = int(float(thr_raw))
            except ValueError:
                target = 0
            progress = (100.0 * rollup["done_points"] / target) \
                if target else 0.0
            achieved = rollup["done_points"] >= target
            label = f"{target} points done"
        else:  # priority
            # threshold is a priority letter; achieved when 100% of that
            # tier and all higher tiers are done.
            thr = thr_raw.upper()
            if thr not in PRI_ORDER:
                achieved = False
                progress = 0.0
                label = "Invalid priority"
            else:
                idx = PRI_ORDER.index(thr)
                tiers = PRI_ORDER[:idx + 1]
                tot = sum(rollup["by_priority"][p]["total"] for p in tiers)
                don = sum(rollup["by_priority"][p]["done"]  for p in tiers)
                progress = (100.0 * don / tot) if tot else 0.0
                achieved = tot > 0 and don >= tot
                label = (f"All {'+'.join(tiers)} done "
                         f"({don}/{tot})")
        out.append({
            "id": m["id"],
            "name": m["name"],
            "threshold": thr_raw,
            "label": label,
            "progress_pct": max(0.0, min(100.0, progress)),
            "achieved": achieved,
        })
    return overall, out


def _overall_for_rule(rule, rollup):
    if rule == "pct_stories" or rule == "count_stories":
        return (100.0 * rollup["done_stories"] / rollup["total_stories"]) \
            if rollup["total_stories"] else 0.0
    if rule == "pct_points" or rule == "count_points":
        return (100.0 * rollup["done_points"] / rollup["total_points"]) \
            if rollup["total_points"] else 0.0
    # priority: simple done/total of all linked stories
    return (100.0 * rollup["done_stories"] / rollup["total_stories"]) \
        if rollup["total_stories"] else 0.0


# ── Epic stats / archive (added for Epics management page) ───────────────────

EPIC_SORTS = ("newest", "oldest", "name_asc", "name_desc",
              "pct_done_desc", "pct_done_asc", "stories_desc", "order")


def get_epic_stats(project_id: int, include_archived: bool = False,
                   archived_only: bool = False, sort: str = "newest",
                   offset: int = 0, limit: int = 20, q: str = ""):
    """Return a page of epics with rollup stats.
    Each row has all epic columns plus:
      total_stories, done_stories, total_points, done_points,
      pct_done (float 0..100),
      pri_vh, pri_h, pri_m, pri_l, pri_vl, pri_unset (counts),
      initiative_name (str|None)
    """
    if sort not in EPIC_SORTS:
        sort = "newest"
    conn = get_db()

    where = ["e.project_id = ?"]
    params = [project_id]
    if archived_only:
        where.append("COALESCE(e.is_archived,0) = 1")
    elif not include_archived:
        where.append("COALESCE(e.is_archived,0) = 0")
    q = (q or "").strip()
    if q:
        like = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(LOWER(e.title) LIKE LOWER(?) ESCAPE '\\' "
                     "OR LOWER(COALESCE(e.description,'')) LIKE LOWER(?) ESCAPE '\\' "
                     "OR LOWER(COALESCE(i.name,'')) LIKE LOWER(?) ESCAPE '\\')")
        params.extend([like, like, like])
    where_sql = " AND ".join(where)

    order_sql = {
        "newest":         "e.created_at DESC, e.id DESC",
        "oldest":         "e.created_at ASC, e.id ASC",
        "name_asc":       "LOWER(e.title) ASC",
        "name_desc":      "LOWER(e.title) DESC",
        "order":          "e.order_index ASC, e.id ASC",
        "stories_desc":   "total_stories DESC, e.id DESC",
        "pct_done_desc":  "pct_done DESC, e.id DESC",
        "pct_done_asc":   "pct_done ASC, e.id DESC",
    }[sort]

    sql = f"""
        SELECT e.*,
               i.name AS initiative_name,
               COUNT(s.id) AS total_stories,
               COALESCE(SUM(CASE WHEN st.is_done THEN 1 ELSE 0 END),0) AS done_stories,
               COALESCE(SUM(COALESCE(s.story_points,0)),0) AS total_points,
               COALESCE(SUM(CASE WHEN st.is_done THEN COALESCE(s.story_points,0) ELSE 0 END),0) AS done_points,
               COALESCE(SUM(CASE WHEN s.priority='VH' THEN 1 ELSE 0 END),0) AS pri_vh,
               COALESCE(SUM(CASE WHEN s.priority='H'  THEN 1 ELSE 0 END),0) AS pri_h,
               COALESCE(SUM(CASE WHEN s.priority='M'  THEN 1 ELSE 0 END),0) AS pri_m,
               COALESCE(SUM(CASE WHEN s.priority='L'  THEN 1 ELSE 0 END),0) AS pri_l,
               COALESCE(SUM(CASE WHEN s.priority='VL' THEN 1 ELSE 0 END),0) AS pri_vl,
               COALESCE(SUM(CASE WHEN s.priority IS NULL OR s.priority='' THEN 1 ELSE 0 END),0) AS pri_unset,
               CASE WHEN COUNT(s.id)=0 THEN 0.0
                    ELSE 100.0 * SUM(CASE WHEN st.is_done THEN 1 ELSE 0 END) / COUNT(s.id)
               END AS pct_done
          FROM epics e
          LEFT JOIN initiatives i ON i.id = e.initiative_id
          LEFT JOIN stories s ON s.epic_id = e.id AND COALESCE(s.is_archived,0) = 0
          LEFT JOIN statuses st ON st.id = s.status_id
         WHERE {where_sql}
         GROUP BY e.id
         ORDER BY {order_sql}
         LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    total_sql = (
        f"SELECT COUNT(*) AS c FROM epics e "
        f"LEFT JOIN initiatives i ON i.id = e.initiative_id "
        f"WHERE {where_sql}"
    )
    total = conn.execute(total_sql, params).fetchone()["c"]
    conn.close()
    return rows, total


def set_epic_archived(epic_id: int, archived: bool) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE epics SET is_archived=?, updated_at=? WHERE id=?",
        (1 if archived else 0, int(time.time()), epic_id),
    )
    conn.commit()
    conn.close()


def bulk_set_epic_archived(project_id: int, epic_ids, archived: bool) -> int:
    if not epic_ids:
        return 0
    conn = get_db()
    placeholders = ",".join("?" * len(epic_ids))
    cur = conn.execute(
        f"UPDATE epics SET is_archived=?, updated_at=? "
        f"WHERE project_id=? AND id IN ({placeholders})",
        (1 if archived else 0, int(time.time()), project_id, *epic_ids),
    )
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n

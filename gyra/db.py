"""
db.py — SQLite schema + thin data-access helpers.
All queries use parameterised statements; no string interpolation in SQL.
"""
import sqlite3
import time
from config import Config

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
"""


# ── Connection ────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    _migrate_db()


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
    ]
    conn = get_db()
    for table, col, col_def in new_cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except Exception:
            pass  # Column already exists
    conn.commit()
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
    ("Epic",        "#F59E0B", 4),
    ("Chore",       "#6B7280", 5),
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


def get_board_stories(project_id: int, sprint: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, st.name AS status_name, st.color AS status_color,
                  sty.name AS story_type_name, sty.color AS story_type_color
           FROM stories s
           LEFT JOIN statuses st   ON s.status_id = st.id
           LEFT JOIN story_types sty ON s.story_type = sty.id
           WHERE s.project_id = ? AND s.sprint = ?
           ORDER BY s.order_index""",
        (project_id, sprint),
    ).fetchall()
    conn.close()
    return rows


def get_backlog_stories(project_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, st.name AS status_name, st.color AS status_color
           FROM stories s
           LEFT JOIN statuses st ON s.status_id = st.id
           WHERE s.project_id = ? AND s.sprint IS NULL
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


# ── Stickers ──────────────────────────────────────────────────────────────────

def get_stickers(project_id: int, sprint):
    conn = get_db()
    if sprint is None:
        rows = conn.execute(
            "SELECT * FROM stickers WHERE project_id=? AND sprint IS NULL",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM stickers WHERE project_id=? AND sprint=?",
            (project_id, sprint),
        ).fetchall()
    conn.close()
    return rows

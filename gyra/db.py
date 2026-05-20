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
        ("stories", "priority",    "TEXT DEFAULT NULL"),
        ("stories", "epic_id",     "INTEGER DEFAULT NULL"),
    ]
    conn = get_db()
    for table, col, col_def in new_cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except Exception:
            pass  # Column already exists

    # Migrate stickers: drop obsolete CHECK constraint, add card-attachment columns.
    # Idempotent: only runs if card_story_id column is missing.
    sticker_cols = [r[1] for r in conn.execute("PRAGMA table_info(stickers)").fetchall()]
    if 'card_story_id' not in sticker_cols:
        conn.execute("PRAGMA foreign_keys = OFF")
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
        conn.execute("PRAGMA foreign_keys = ON")

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


def get_board_stories(project_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, st.name AS status_name, st.color AS status_color,
                  sty.name AS story_type_name, sty.color AS story_type_color
           FROM stories s
           LEFT JOIN statuses st   ON s.status_id = st.id
           LEFT JOIN story_types sty ON s.story_type = sty.id
           WHERE s.project_id = ? AND s.sprint IS NOT NULL
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

def get_stickers(project_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM stickers WHERE project_id=?",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


# ── Epics ─────────────────────────────────────────────────────────────────────

def get_epics(project_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM epics WHERE project_id=? ORDER BY title",
        (project_id,),
    ).fetchall()
    conn.close()
    return rows


def create_epic(project_id: int, title: str, color: str, description: str, user_id: int):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO epics (project_id,title,color,description,created_at,created_by) VALUES (?,?,?,?,?,?)",
        (project_id, title, color, description or '', int(time.time()), user_id),
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

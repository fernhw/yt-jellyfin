"""seed_container_lab.py — One-shot fixture script.

Creates a project called "Container/Attachment Test Lab" (CTL) populated with
~40 stories that exercise the Container/Attachment system:

  * 4 Containers (one per box_type: whitebox, greybox, blackbox, featurebox)
  * 1 nested Container (daisy chain: attached to a parent Container)
  * 4-8 Attachments under each Container (mix of done / pending statuses)
  * Several standalone control stories (no container, no box)

Run:
    cd gyra && /usr/bin/python3 seed_container_lab.py
"""
import sys, time, os, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_db, init_db

random.seed(42)

init_db()
NOW = int(time.time())

conn = get_db()
cur  = conn.cursor()

# Pick a creator — first active user, fall back to id=1.
row = cur.execute(
    "SELECT id FROM users WHERE is_active=1 ORDER BY id LIMIT 1"
).fetchone()
creator_id = row["id"] if row else 1

# ── Project ─────────────────────────────────────────────────────────────────
existing = cur.execute(
    "SELECT id FROM projects WHERE key='CTL'"
).fetchone()
if existing:
    project_id = existing["id"]
    print(f"Re-using existing project CTL (id={project_id}); wiping its stories.")
    cur.execute("DELETE FROM stories WHERE project_id=?", (project_id,))
else:
    cur.execute(
        "INSERT INTO projects (name, description, key, created_at, created_by) "
        "VALUES (?,?,?,?,?)",
        ("Container/Attachment Test Lab",
         "Fixture project demonstrating box types & sovereign-story attachments.",
         "CTL", NOW, creator_id),
    )
    project_id = cur.lastrowid
    print(f"Created project CTL (id={project_id}).")

# Statuses (standard Kanban)
cur.execute("DELETE FROM statuses WHERE project_id=?", (project_id,))
status_defs = [
    ("Backlog",      "#94a3b8", 0, 0),
    ("In Progress",  "#3b82f6", 1, 0),
    ("Review",       "#f59e0b", 2, 0),
    ("Done",         "#22c55e", 3, 1),
]
status_ids = {}
for name, color, order, is_done in status_defs:
    cur.execute(
        "INSERT INTO statuses (project_id, name, color, order_index, is_done) "
        "VALUES (?,?,?,?,?)",
        (project_id, name, color, order, is_done),
    )
    status_ids[name] = cur.lastrowid

# Story types (randomized across stories)
cur.execute("DELETE FROM story_types WHERE project_id=?", (project_id,))
type_defs = [
    ("Feature", "#3b82f6"),
    ("Bug",     "#dc2626"),
    ("Chore",   "#a3a3a3"),
    ("Spike",   "#7c3aed"),
    ("Task",    "#22c55e"),
]
type_ids = []
for name, color in type_defs:
    cur.execute(
        "INSERT INTO story_types (project_id, name, color) VALUES (?,?,?)",
        (project_id, name, color),
    )
    type_ids.append(cur.lastrowid)

# Add creator to project_members if table exists & not already there.
try:
    cur.execute(
        "INSERT OR IGNORE INTO project_members (project_id, user_id, added_at) "
        "VALUES (?,?,?)",
        (project_id, creator_id, NOW),
    )
except Exception:
    pass

# ── Story creation helper ──────────────────────────────────────────────────
def mk(title, status="Backlog", box_type=None, attached_to=None,
       dependent_action=None, desc="", points=None, story_type=None):
    st_id = story_type if story_type is not None else random.choice(type_ids)
    pts   = points if points is not None else random.choice([1,2,3,5,8])
    cur.execute(
        """INSERT INTO stories
           (project_id, title, description, acceptance_criteria, story_points,
            status_id, created_at, created_by, box_type, attached_to,
            dependent_action, story_type, sprint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, title, desc, "", pts,
         status_ids[status], NOW, creator_id,
         box_type, attached_to, dependent_action, st_id, 1),
    )
    return cur.lastrowid

# ── Containers ─────────────────────────────────────────────────────────────
WB = mk("Main Menu (Whitebox container)",
        status="In Progress", box_type="whitebox",
        desc="Top-level navigation. Built & inspectable. Attachments may wire in.")
GB = mk("Settings Panel (Greybox container)",
        status="In Progress", box_type="greybox",
        desc="Stub with sketched hooks. Attachments may dry-run only.")
BB = mk("Game Screen (Blackbox container)",
        status="Backlog", box_type="blackbox",
        desc="Does not exist yet. Attachments build standalone, no references.")
FB = mk("Pause Overlay (Featurebox container)",
        status="In Progress", box_type="featurebox",
        desc="I'll absorb integration myself. Attachments just signal complete.")

# Nested container — Settings Panel itself attached to Main Menu.
# (Greybox declaring it's hung off the Whitebox Main Menu.)
cur.execute(
    "UPDATE stories SET attached_to=?, dependent_action=? WHERE id=?",
    (WB, "Surface from gear icon, top-right of Main Menu",  GB),
)

# ── Whitebox attachments (4 stories, mix of done) ──────────────────────────
wb_atts = [
    ("Render Play button",         "Done",        "Mount at slot[0]; matches mock"),
    ("Render Settings gear",       "Done",        "Top-right; opens Settings Panel"),
    ("Render Quit button",         "Done",        "Bottom; confirm dialog wired"),
    ("Animate menu fade-in",       "In Progress", "Use Main Menu's onShow hook"),
    ("Add keyboard nav arrows",    "Review",      "Wire to Main Menu's focus ring"),
    ("Track menu open analytics",  "Backlog",     "Hook into Main Menu's onShow event"),
]
for title, st, act in wb_atts:
    mk(title, status=st, attached_to=WB, dependent_action=act)

# ── Greybox attachments (settings sub-stories) ─────────────────────────────
gb_atts = [
    ("Audio sliders mock",         "Done",        "Dry-run against Settings stub"),
    ("Video resolution dropdown",  "In Progress", "Dry-run only; do NOT bind yet"),
    ("Controls remapper",          "Backlog",     "Stub schema only — no live save"),
    ("Save / Cancel buttons UX",   "Review",      "Use Settings dry-run sink"),
    ("Settings reset confirm",     "Backlog",     "Mock confirm flow; integration deferred"),
]
for title, st, act in gb_atts:
    mk(title, status=st, attached_to=GB, dependent_action=act)

# ── Blackbox attachments — build standalone, no references! ────────────────
bb_atts = [
    ("HUD prototype standalone",     "In Progress",
        "Build in isolation. DO NOT reference Game Screen — it does not exist."),
    ("Player avatar sprite sheet",   "Done",
        "Standalone asset; integration deferred."),
    ("Pause hotkey listener",        "Done",
        "Standalone wiring; do not call Game Screen API yet."),
    ("Mini-map overlay component",   "Backlog",
        "Build & test in storybook. No Game Screen coupling."),
    ("In-game tooltip framework",    "Backlog",
        "Standalone; will wire when Blackbox lifts."),
    ("Damage flash post-FX",         "Review",
        "Build standalone, do not import Game Screen module."),
]
for title, st, act in bb_atts:
    mk(title, status=st, attached_to=BB, dependent_action=act)

# ── Featurebox attachments — just signal complete; container absorbs ───────
fb_atts = [
    ("Pause-blur shader",           "Done",        "Mark Done — Pause Overlay will absorb"),
    ("Resume confirmation toast",   "In Progress", "Just complete; integration handled by Pause Overlay"),
    ("Save-and-quit flow",          "Done",        "Hand off; container owns integration"),
    ("Pause overlay icon set",      "Review",      "Mark Done when assets land; absorbed automatically"),
    ("Pause analytics event",       "Backlog",     "Container will pick up on completion"),
]
for title, st, act in fb_atts:
    mk(title, status=st, attached_to=FB, dependent_action=act)

# ── Standalone control stories (no container, no attachment) ───────────────
standalone = [
    ("Refactor logging utility",       "In Progress"),
    ("Update OpenSSL to 3.x",          "Backlog"),
    ("Fix crash on empty save file",   "Review"),
    ("Add CONTRIBUTING.md",            "Done"),
    ("Localize button labels (es-MX)", "Backlog"),
    ("Profile build size",             "Backlog"),
    ("Sentry release tagging",         "Done"),
    ("Audit npm vulnerabilities",      "In Progress"),
    ("Migrate CI to GitHub Actions",   "Backlog"),
    ("Add changelog automation",       "Backlog"),
    ("Docs: API examples section",     "Review"),
    ("Improve splash screen FPS",      "Done"),
]
for title, st in standalone:
    mk(title, status=st)

conn.commit()

# Print summary
total = cur.execute(
    "SELECT COUNT(*) AS c FROM stories WHERE project_id=?", (project_id,)
).fetchone()["c"]
containers = cur.execute(
    "SELECT COUNT(*) AS c FROM stories WHERE project_id=? AND box_type IS NOT NULL",
    (project_id,),
).fetchone()["c"]
attached = cur.execute(
    "SELECT COUNT(*) AS c FROM stories WHERE project_id=? AND attached_to IS NOT NULL",
    (project_id,),
).fetchone()["c"]
conn.close()

print(f"\nSeeded CTL with {total} stories ({containers} containers, "
      f"{attached} attachments).")
print(f"Open: http://127.0.0.1:5050/board/{project_id}")

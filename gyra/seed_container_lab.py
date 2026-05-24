"""seed_container_lab.py — Fixture project that *feels* like real Gyra usage.

Creates / re-seeds the "Container/Attachment Test Lab" (CTL) project with stories
that have the full Gyra format:
  • structured user-story (actor / verb / x / for / y)
  • description + acceptance criteria
  • 2-4 checklist tasks per story
  • 1-2 images sampled from the existing /static/story-images library
  • box_type + attached_to wiring for the sovereign-story system

Run:
    cd gyra && /usr/bin/python3 seed_container_lab.py
"""
import os, sys, time, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_db, init_db

random.seed(42)
init_db()
NOW = int(time.time())

conn = get_db()
cur  = conn.cursor()

# ── Pick creator ────────────────────────────────────────────────────────────
row = cur.execute(
    "SELECT id FROM users WHERE is_active=1 ORDER BY id LIMIT 1"
).fetchone()
creator_id = row["id"] if row else 1

user_pool = [r["id"] for r in cur.execute(
    "SELECT id FROM users WHERE is_active=1 ORDER BY id LIMIT 5"
).fetchall()] or [creator_id]

# ── Image library (filenames already on disk) ───────────────────────────────
IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "static", "story-images")
IMG_POOL = []
if os.path.isdir(IMG_DIR):
    for fn in sorted(os.listdir(IMG_DIR)):
        if fn.startswith(("thumb_", "med_", ".")):
            continue
        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            IMG_POOL.append(fn)
if not IMG_POOL:
    print("WARN: no images found in static/story-images; stories will have no images.")

# ── Project ─────────────────────────────────────────────────────────────────
existing = cur.execute("SELECT id FROM projects WHERE key='CTL'").fetchone()
if existing:
    project_id = existing["id"]
    print(f"Re-using project CTL (id={project_id}); wiping its stories.")
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

# ── Statuses ────────────────────────────────────────────────────────────────
cur.execute("DELETE FROM statuses WHERE project_id=?", (project_id,))
status_defs = [
    ("Backlog",     "#94a3b8", 0, 0),
    ("In Progress", "#3b82f6", 1, 0),
    ("Review",      "#f59e0b", 2, 0),
    ("Done",        "#22c55e", 3, 1),
]
status_ids = {}
for name, color, order, is_done in status_defs:
    cur.execute(
        "INSERT INTO statuses (project_id, name, color, order_index, is_done) "
        "VALUES (?,?,?,?,?)",
        (project_id, name, color, order, is_done),
    )
    status_ids[name] = cur.lastrowid

# ── Story types ─────────────────────────────────────────────────────────────
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

try:
    cur.execute(
        "INSERT OR IGNORE INTO project_members (project_id, user_id, added_at) "
        "VALUES (?,?,?)",
        (project_id, creator_id, NOW),
    )
except Exception:
    pass

# ── User-story phrase pools ────────────────────────────────────────────────
ACTORS = ["A Player", "A Returning Player", "A New Player", "An Admin",
          "A Designer", "A Streamer", "A QA Tester"]
VERBS  = ["wants to", "needs to", "expects to", "should be able to",
          "would like to"]
FORS   = ["so that", "in order to", "because"]
OUTCOMES = [
    "the flow feels natural", "they can keep playing without friction",
    "they trust the product", "they don't get stuck",
    "the experience is polished", "they save time",
    "they understand what's happening",
]

def pick_story_phrasing(title):
    return {
        "actor": random.choice(ACTORS),
        "verb":  random.choice(VERBS),
        "z":     "",
        "x":     title.lower(),
        "for":   random.choice(FORS),
        "y":     random.choice(OUTCOMES),
    }

# ── Insert helper ──────────────────────────────────────────────────────────
def mk(title, status="Backlog", box_type=None, attached_to=None,
       dependent_action=None, desc="", acceptance="", points=None,
       story_type=None, tasks=None, image_count=None):
    st_id = story_type if story_type is not None else random.choice(type_ids)
    pts   = points if points is not None else random.choice([1,2,3,5,8])
    us    = pick_story_phrasing(title)
    cur.execute(
        """INSERT INTO stories
           (project_id, title, description, acceptance_criteria, story_points,
            status_id, created_at, updated_at, created_by,
            box_type, attached_to, dependent_action, story_type, sprint,
            story_actor, story_verb, story_z, story_x, story_for, story_y)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, title, desc, acceptance, pts,
         status_ids[status], NOW, NOW, creator_id,
         box_type, attached_to, dependent_action, st_id, 1,
         us["actor"], us["verb"], us["z"], us["x"], us["for"], us["y"]),
    )
    sid = cur.lastrowid

    task_list = tasks if tasks is not None else _default_tasks()
    for i, t in enumerate(task_list):
        assignee = random.choice(user_pool)
        cur.execute(
            """INSERT INTO story_addons
               (story_id, content, assigned_user_id, order_index,
                created_at, created_by)
               VALUES (?,?,?,?,?,?)""",
            (sid, t, assignee, i, NOW, creator_id),
        )
        addon_id = cur.lastrowid
        if status in ("Review", "Done") and random.random() < 0.7:
            cur.execute(
                """INSERT INTO addon_statuses (addon_id, user_id, is_done, updated_at)
                   VALUES (?,?,?,?)""",
                (addon_id, assignee, 1, NOW),
            )

    n_imgs = image_count if image_count is not None else random.choice([1, 1, 2, 0])
    if IMG_POOL and n_imgs:
        for fn in random.sample(IMG_POOL, min(n_imgs, len(IMG_POOL))):
            cur.execute(
                "INSERT INTO story_images (story_id, filename, created_at) VALUES (?,?,?)",
                (sid, fn, NOW),
            )

    cur.execute(
        "INSERT OR IGNORE INTO story_users (story_id, user_id, role) VALUES (?,?,?)",
        (sid, random.choice(user_pool), "assignee"),
    )
    return sid

def _default_tasks():
    return random.choice([
        ["Sketch the visual layout", "Implement the markup",
         "Wire up the interaction", "Write a quick smoke test"],
        ["Audit existing implementation", "Draft the change",
         "Self-review against acceptance criteria"],
        ["Design review with team", "Implement happy path",
         "Add error handling", "Capture screenshot for QA"],
        ["Spike: prototype the approach", "Document tradeoffs",
         "Pair with reviewer"],
    ])

# ── Containers ─────────────────────────────────────────────────────────────
WB = mk(
    "Main Menu",
    status="In Progress", box_type="whitebox",
    desc=("The Main Menu is the first screen a player sees. It hosts Play, "
          "Settings, and Quit, plus a small studio logo strip. It is the "
          "container that other UI surfaces (settings, splash, modals) hang "
          "off of."),
    acceptance=("• Play, Settings, Quit buttons visible at first paint\n"
                "• Studio logo visible top-left\n"
                "• Keyboard navigation moves focus between buttons\n"
                "• Screen reads correctly at 1080p and 1440p"),
    image_count=2,
    tasks=["Lay out the three primary buttons",
           "Hook up Play → loads first level",
           "Hook up Quit → confirm dialog",
           "Add focus ring and keyboard nav"],
)
GB = mk(
    "Settings Panel",
    status="In Progress", box_type="greybox",
    desc=("A roll-out panel for audio, video, and controls preferences. "
          "Currently a low-fidelity sketch — children attach as drafts and "
          "are wired up later once the panel hardens."),
    acceptance=("• Panel slides in from the right\n"
                "• Three tabs: Audio, Video, Controls\n"
                "• Save & Cancel buttons in the footer\n"
                "• Esc closes the panel"),
    image_count=2,
    tasks=["Block out the panel container", "Add tab strip",
           "Wire Save/Cancel placeholders", "Esc-to-close handler"],
)
BB = mk(
    "Game Screen",
    status="Backlog", box_type="blackbox",
    desc=("The main in-game viewport. Does not exist yet. Anything that "
          "feeds into it must be built standalone — do not reference the "
          "Game Screen module until this story exits Backlog."),
    acceptance=("• Renders the world at 60fps on the reference machine\n"
                "• Accepts player input via the standard input layer\n"
                "• Exposes a stable mount-point for HUD widgets\n"
                "• Pauses cleanly on focus loss"),
    image_count=2,
    tasks=["Pick rendering approach", "Set up viewport scaffold",
           "Define HUD mount API", "Stub pause behavior"],
)
FB = mk(
    "Pause Overlay",
    status="In Progress", box_type="featurebox",
    desc=("Full-screen translucent overlay shown when the player pauses. "
          "It absorbs integration work for anything attached to it — "
          "attachments just need to finish; this story wires them in."),
    acceptance=("• Dim background, centered menu card\n"
                "• Resume, Restart, Settings, Quit options\n"
                "• Pause/Resume sounds trigger correctly\n"
                "• Game time stops while overlay is up"),
    image_count=2,
    tasks=["Background blur shader", "Center menu card",
           "Resume / restart wiring", "Quit confirm dialog"],
)

# Nested: Settings Panel hangs off Main Menu (greybox inside whitebox).
cur.execute(
    "UPDATE stories SET attached_to=?, dependent_action=? WHERE id=?",
    (WB, "Surface from the gear icon, top-right of Main Menu", GB),
)

# ── Whitebox attachments — Main Menu sub-stories ───────────────────────────
wb_atts = [
    ("Render Play button",        "Done",
     "Most important call-to-action; sits center stage on the menu.",
     "Mount at slot[0]; matches mock"),
    ("Render Settings gear",      "Done",
     "Small gear icon top-right; reveals the Settings Panel.",
     "Top-right; opens Settings Panel"),
    ("Render Quit button",        "Done",
     "Subtle Quit at the bottom of the menu with a confirm dialog.",
     "Bottom; confirm dialog wired"),
    ("Animate menu fade-in",      "In Progress",
     "Soft fade + slight upward drift on first paint of the Main Menu.",
     "Use Main Menu's onShow hook"),
    ("Add keyboard nav arrows",   "Review",
     "Arrow keys move focus between primary buttons; Enter activates.",
     "Wire to Main Menu's focus ring"),
    ("Track menu open analytics", "Backlog",
     "Fire an event each time the Main Menu becomes visible.",
     "Hook into Main Menu's onShow event"),
]
for title, st, d, act in wb_atts:
    mk(title, status=st, attached_to=WB, dependent_action=act,
       box_type="whitebox", desc=d,
       acceptance="• Behavior matches description\n• No regressions in Main Menu paint")

# ── Greybox attachments — rough Settings sub-stories ───────────────────────
gb_atts = [
    ("Audio sliders mock",        "Done",
     "Three sliders (Master, Music, SFX). Visual only — no persistence yet.",
     "Dry-run against Settings stub"),
    ("Video resolution dropdown", "In Progress",
     "Dropdown listing supported resolutions. Selection is sketch-only.",
     "Dry-run only; do NOT bind yet"),
    ("Controls remapper",         "Backlog",
     "Click-to-rebind UI for keyboard actions. Schema-only stub.",
     "Stub schema only — no live save"),
    ("Save / Cancel buttons UX",  "Review",
     "Footer buttons. Save flashes a toast; Cancel closes without saving.",
     "Use Settings dry-run sink"),
    ("Settings reset confirm",    "Backlog",
     "Reset-to-defaults link with a confirm dialog. Currently mock.",
     "Mock confirm flow; integration deferred"),
]
for title, st, d, act in gb_atts:
    mk(title, status=st, attached_to=GB, dependent_action=act,
       box_type="greybox", desc=d,
       acceptance="• Looks right in the Settings Panel\n• No live persistence yet")

# ── Blackbox attachments — build standalone ────────────────────────────────
bb_atts = [
    ("HUD prototype standalone",   "In Progress",
     "Build the HUD as an isolated component. No coupling to Game Screen.",
     "Build in isolation. DO NOT reference Game Screen — it does not exist."),
    ("Player avatar sprite sheet", "Done",
     "Standalone art asset. No engine wiring; just the sheet + metadata.",
     "Standalone asset; integration deferred."),
    ("Pause hotkey listener",      "Done",
     "Listens for the pause key globally; emits an event. No Game Screen calls.",
     "Standalone wiring; do not call Game Screen API yet."),
    ("Mini-map overlay component", "Backlog",
     "Standalone storybook component; renders a grid + tokens from props.",
     "Build & test in storybook. No Game Screen coupling."),
    ("In-game tooltip framework",  "Backlog",
     "Generic tooltip system. Drop into anywhere later — not just gameplay.",
     "Standalone; will wire when Blackbox lifts."),
    ("Damage flash post-FX",       "Review",
     "Full-screen red flash effect; built and tested on a blank canvas.",
     "Build standalone, do not import Game Screen module."),
]
for title, st, d, act in bb_atts:
    mk(title, status=st, attached_to=BB, dependent_action=act,
       box_type="blackbox", desc=d,
       acceptance="• Works in isolation\n• Zero references to Game Screen module")

# ── Featurebox attachments — drop-in, container absorbs integration ────────
fb_atts = [
    ("Pause-blur shader",         "Done",
     "Background blur applied while pause overlay is up. Drop-in shader.",
     "Mark Done — Pause Overlay will absorb"),
    ("Resume confirmation toast", "In Progress",
     "Tiny toast confirming resume action. Self-contained widget.",
     "Just complete; integration handled by Pause Overlay"),
    ("Save-and-quit flow",        "Done",
     "Modal sequence: save current state, then quit to menu.",
     "Hand off; container owns integration"),
    ("Pause overlay icon set",    "Review",
     "Set of small icons for the pause menu (resume, restart, quit, settings).",
     "Mark Done when assets land; absorbed automatically"),
    ("Pause analytics event",     "Backlog",
     "Fire one event per pause with current level and timestamp.",
     "Container will pick up on completion"),
]
for title, st, d, act in fb_atts:
    mk(title, status=st, attached_to=FB, dependent_action=act,
       box_type="featurebox", desc=d,
       acceptance="• Ships polished and self-contained\n• No integration work needed in Pause Overlay")

# ── Standalone control stories ─────────────────────────────────────────────
standalone = [
    ("Refactor logging utility",       "In Progress",
     "Consolidate three competing loggers into a single facade."),
    ("Update OpenSSL to 3.x",          "Backlog",
     "Track CVE-2023-XXXX; bump dependency and re-run integration suite."),
    ("Fix crash on empty save file",   "Review",
     "Reproduce with empty save; ensure graceful fallback to new-game flow."),
    ("Add CONTRIBUTING.md",            "Done",
     "Document branch naming, commit style, and how to run tests."),
    ("Localize button labels (es-MX)", "Backlog",
     "First-pass Spanish (Mexico) localization for top-level UI."),
    ("Profile build size",             "Backlog",
     "Measure asset & code bundle sizes; identify top three offenders."),
    ("Sentry release tagging",         "Done",
     "Tag each release in Sentry so errors map back to a known build."),
    ("Audit npm vulnerabilities",      "In Progress",
     "Run npm audit; triage results; open issues for actionable items."),
    ("Migrate CI to GitHub Actions",   "Backlog",
     "Port from legacy CI; preserve current pipeline steps."),
    ("Add changelog automation",       "Backlog",
     "Generate CHANGELOG.md from conventional commits on release."),
    ("Docs: API examples section",     "Review",
     "Add a curated examples section to the public API docs."),
    ("Improve splash screen FPS",      "Done",
     "Splash dropped below 60fps on low-end; identified shader culprit."),
]
for title, st, d in standalone:
    mk(title, status=st, desc=d,
       acceptance="• Behavior matches description\n• No regressions in covered areas")

conn.commit()

# ── Summary ────────────────────────────────────────────────────────────────
total = cur.execute(
    "SELECT COUNT(*) AS c FROM stories WHERE project_id=?", (project_id,)
).fetchone()["c"]
containers = cur.execute(
    "SELECT COUNT(*) AS c FROM stories WHERE project_id=? AND box_type IS NOT NULL "
    "AND attached_to IS NULL", (project_id,),
).fetchone()["c"]
attached = cur.execute(
    "SELECT COUNT(*) AS c FROM stories WHERE project_id=? AND attached_to IS NOT NULL",
    (project_id,),
).fetchone()["c"]
with_imgs = cur.execute(
    "SELECT COUNT(DISTINCT story_id) AS c FROM story_images si "
    "JOIN stories s ON si.story_id=s.id WHERE s.project_id=?", (project_id,),
).fetchone()["c"]
conn.close()

print(f"\nSeeded CTL with {total} stories "
      f"({containers} root containers, {attached} attachments, "
      f"{with_imgs} with images).")
print(f"Open: http://127.0.0.1:5050/board/{project_id}")

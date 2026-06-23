"""routes/wiki.py — Game Design Wiki (GDW).

File layout on disk:
  wiki_content/<PROJECT_KEY>/articles/<slug>.md   ← article body (raw MD)
  wiki_content/<PROJECT_KEY>/images/              ← uploaded images
  wiki_content/<PROJECT_KEY>/images/thumbs/       ← auto-thumbnails

DB (wiki_articles) stores only metadata + tree structure. The .md file is
the source of truth for content. They are kept in sync: write to DB and disk
together; read from disk when rendering.

Lazy-init: visiting /project/<id>/wiki for the first time auto-creates the
folder structure and a welcome article. Zero setup required.
"""
import json
import os
import re
import time
import unicodedata

from flask import (abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from PIL import Image

from auth import enforce_csrf, login_required
from config import Config
from db import get_db, get_project, user_in_project

# ── Paths ─────────────────────────────────────────────────────────────────────

WIKI_ROOT = os.path.join(Config.BASE_DIR, "wiki_content")


def _wiki_dir(project_key: str) -> str:
    return os.path.join(WIKI_ROOT, project_key.upper())


def _articles_dir(project_key: str) -> str:
    return os.path.join(_wiki_dir(project_key), "articles")


def _images_dir(project_key: str) -> str:
    return os.path.join(_wiki_dir(project_key), "images")


def _article_path(project_key: str, slug: str) -> str:
    return os.path.join(_articles_dir(project_key), f"{slug}.md")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:80]


def _unique_slug(project_id: int, base: str) -> str:
    conn = get_db()
    slug = base
    n = 2
    while conn.execute(
        "SELECT 1 FROM wiki_articles WHERE project_id=? AND slug=?",
        (project_id, slug)
    ).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _read_md(project_key: str, slug: str) -> str:
    path = _article_path(project_key, slug)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def _write_md(project_key: str, slug: str, content: str) -> None:
    os.makedirs(_articles_dir(project_key), exist_ok=True)
    with open(_article_path(project_key, slug), "w", encoding="utf-8") as f:
        f.write(content)


def _gdd_cover_path(project_key: str) -> str:
    return os.path.join(_wiki_dir(project_key), "gdd_cover.json")


def _read_gdd_cover(project_key: str) -> dict:
    path = _gdd_cover_path(project_key)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_gdd_cover(project_key: str, data: dict) -> None:
    path = _gdd_cover_path(project_key)
    os.makedirs(_wiki_dir(project_key), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Section system ────────────────────────────────────────────────────────────
# File format: sections separated by [TAG] on its own line, no closing tags.
# Content runs from after [TAG] until the next [TAG] or end of file.
#
#   [META]
#   title=Combat
#   authors=[[Alex]]
#
#   [HERO]
#   combat.jpg
#   Epic battle scene
#
#   [MAIN_SECTION]
#   # Combat System
#   The player...
#
#   [REFERENCES]
#   - Game Design Patterns, 2004

_SECTION_OPEN_RE = re.compile(r'^\[([A-Z][A-Z_0-9]*)\]\s*$', re.MULTILINE)

# ── Wiki article import templates ─────────────────────────────────────────────
# Each template defines:
#   name        — display label
#   category    — group header in dropdown
#   keywords    — words in title/parent that trigger auto-selection
#   meta_fields — list of "key=?" lines added to [META] section
#   body_hint   — placeholder text in [MAIN_SECTION]
WIKI_IMPORT_TEMPLATES = {
    "character": {
        "name": "Character", "category": "People",
        "keywords": ["character","char","protagonist","hero","villain","ally","companion","playable","player","avatar","npc"],
        "meta_fields": ["role=?","age=?","height=?","weight=?","status=?",
                        "birth_date=?","faction=?","voice_actor=?","first_appearance=?"],
        "body_hint": "Describe this character's background, personality, motivations, and role in the story.",
    },
    "enemy": {
        "name": "Enemy / Mob", "category": "Combat",
        "keywords": ["enemy","mob","creature","monster","minion","grunt","miniboss","undead","drone","beast"],
        "meta_fields": ["type=?","hp=?","damage=?","speed=?","behavior=?",
                        "region=?","drop_table=?","threat_level=?"],
        "body_hint": "Describe this enemy's appearance, AI behaviour, attack patterns and loot.",
    },
    "boss": {
        "name": "Boss", "category": "Combat",
        "keywords": ["boss","final boss","raid boss","elite"],
        "meta_fields": ["phase_count=?","hp=?","weaknesses=?","rewards=?",
                        "location=?","theme=?","unlock_condition=?"],
        "body_hint": "Describe this boss's phases, attacks, cinematic trigger and reward structure.",
    },
    "location": {
        "name": "Location / Area", "category": "World",
        "keywords": ["location","area","zone","region","map","biome","dungeon","world","hub","district","island","city","town","village","forest","cave","ruins","temple","castle"],
        "meta_fields": ["region=?","biome=?","climate=?","population=?",
                        "status=?","difficulty=?","connected_areas=?","music=?"],
        "body_hint": "Describe this area's atmosphere, key landmarks, inhabitants and traversal.",
    },
    "item": {
        "name": "Item / Loot", "category": "Items",
        "keywords": ["item","loot","treasure","pickup","collectible","consumable","key item","artifact","relic"],
        "meta_fields": ["type=?","rarity=?","effect=?","value=?",
                        "craftable=?","drop_source=?","stack_size=?"],
        "body_hint": "Describe where this item drops, what it does, and how it fits the player loop.",
    },
    "weapon": {
        "name": "Weapon", "category": "Items",
        "keywords": ["weapon","sword","gun","bow","staff","axe","blade","hammer","spear","rifle","pistol","wand","shield"],
        "meta_fields": ["weapon_type=?","damage=?","range=?","attack_speed=?",
                        "special_ability=?","unlock_condition=?","rarity=?"],
        "body_hint": "Describe damage values, moveset, unique property and unlock path.",
    },
    "ability": {
        "name": "Ability / Skill", "category": "Gameplay",
        "keywords": ["ability","skill","power","spell","move","technique","talent","passive","active","ultimate"],
        "meta_fields": ["ability_type=?","cooldown=?","damage=?","range=?",
                        "cost=?","unlock_level=?","affects=?"],
        "body_hint": "Describe what this ability does, its visual feedback, and upgrade path.",
    },
    "mechanic": {
        "name": "Game Mechanic", "category": "Gameplay",
        "keywords": ["mechanic","mechanics","gameplay","loop","feature","system","physics","platforming","grapple","stealth"],
        "meta_fields": ["category=?","status=?","complexity=?",
                        "affects=?","dependencies=?","owner=?"],
        "body_hint": "Describe how this mechanic works, player input, feedback, and edge cases.",
    },
    "quest": {
        "name": "Quest / Mission", "category": "Narrative",
        "keywords": ["quest","mission","task","objective","contract","bounty","errand","assignment"],
        "meta_fields": ["quest_type=?","giver=?","reward=?","prerequisites=?",
                        "act=?","duration=?","branch_count=?"],
        "body_hint": "Describe the quest flow, dialogue nodes, success/fail states and reward.",
    },
    "story_beat": {
        "name": "Story Beat / Scene", "category": "Narrative",
        "keywords": ["story","narrative","plot","act","chapter","beat","scene","prologue","epilogue","moment"],
        "meta_fields": ["act=?","chapter=?","pov=?","tone=?","outcome=?","location=?"],
        "body_hint": "Describe what happens in this scene, who is present, and what changes.",
    },
    "dialogue": {
        "name": "Dialogue / Conversation", "category": "Narrative",
        "keywords": ["dialogue","dialog","conversation","monologue","banter","voice line"],
        "meta_fields": ["character=?","scene=?","trigger=?","tone=?","branch_count=?","skippable=?"],
        "body_hint": "Describe the conversation context, speaker, tone and branching options.",
    },
    "cutscene": {
        "name": "Cutscene / Cinematic", "category": "Narrative",
        "keywords": ["cutscene","cinematic","intro","outro","cinematic sequence","video","fmv"],
        "meta_fields": ["duration=?","trigger=?","location=?","characters=?","skippable=?","director=?"],
        "body_hint": "Describe what is shown, camera moves, music, and what it communicates.",
    },
    "level_design": {
        "name": "Level Design", "category": "World",
        "keywords": ["level","stage","floor","chapter","world","sector"],
        "meta_fields": ["biome=?","difficulty=?","objectives=?","enemy_types=?",
                        "boss=?","music=?","time_limit=?"],
        "body_hint": "Describe the layout, set pieces, pacing beats and player flow.",
    },
    "faction": {
        "name": "Faction / Organisation", "category": "World",
        "keywords": ["faction","guild","group","clan","team","organisation","organization","alliance","empire","order"],
        "meta_fields": ["faction_type=?","leader=?","territory=?","alignment=?",
                        "relations=?","strength=?"],
        "body_hint": "Describe this faction's goals, history, hierarchy and role in the world.",
    },
    "ui_screen": {
        "name": "UI / HUD Screen", "category": "Technical",
        "keywords": ["ui","hud","menu","screen","interface","overlay","minimap","inventory","pause","settings"],
        "meta_fields": ["screen_type=?","platform=?","flow=?","status=?","mockup=?"],
        "body_hint": "Describe what this screen shows, how the player navigates it, and any states.",
    },
    "audio": {
        "name": "Audio / Music Track", "category": "Audio",
        "keywords": ["audio","music","sfx","sound","track","ost","ambient","score","voice"],
        "meta_fields": ["audio_type=?","trigger=?","duration=?","loop=?","composer=?","platform=?"],
        "body_hint": "Describe the intended mood, instrumentation, trigger conditions and variations.",
    },
    "progression": {
        "name": "Progression / Upgrade", "category": "Gameplay",
        "keywords": ["progression","upgrade","skill tree","level up","xp","experience","rank","prestige","unlock"],
        "meta_fields": ["progression_type=?","unlock_condition=?","reward=?",
                        "prerequisites=?","cap=?"],
        "body_hint": "Describe what this progression gate does, how it's unlocked and what it rewards.",
    },
    "system": {
        "name": "Technical System", "category": "Technical",
        "keywords": ["system","technical","architecture","database","network","save","load","backend","pipeline","engine"],
        "meta_fields": ["system_category=?","status=?","complexity=?",
                        "owner=?","dependencies=?","platform=?"],
        "body_hint": "Describe the system's purpose, inputs/outputs, dependencies and known risks.",
    },
    "overview": {
        "name": "Overview / Summary", "category": "Structure",
        "keywords": ["overview","summary","introduction","about","vision","mission","concept","pitch"],
        "meta_fields": ["status=?","version=?","owner=?"],
        "body_hint": "Provide a high-level summary of this section's scope and purpose.",
    },
    "generic": {
        "name": "Generic (default)", "category": "Other",
        "keywords": [],
        "meta_fields": ["status=?","owner=?","notes=?"],
        "body_hint": "Add details here.",
    },
}

# Ordered list for dropdown (category grouping happens in JS)
WIKI_TEMPLATE_LIST = list(WIKI_IMPORT_TEMPLATES.keys())


def _detect_wiki_template(title: str, parent_title: str = "") -> str:
    """Return the best template id for a section by scanning title + parent keywords."""
    combined = (title + " " + parent_title).lower()
    words = re.findall(r'[a-z]+', combined)
    word_set = set(words)
    # Check every template; score = number of keyword hits
    best_id, best_score = "generic", 0
    for tid, tpl in WIKI_IMPORT_TEMPLATES.items():
        if tid == "generic":
            continue
        score = 0
        for kw in tpl["keywords"]:
            kw_words = kw.lower().split()
            # Multi-word keyword: check as substring
            if len(kw_words) > 1:
                if kw.lower() in combined:
                    score += 2
            else:
                if kw_words[0] in word_set:
                    score += 1
        if score > best_score:
            best_score = score
            best_id = tid
    return best_id


def _build_wiki_article_content(title: str, body: str, template_id: str) -> str:
    """Build a full [META]/[HERO]/[MAIN_SECTION]/[REFERENCES] article
    using the given template for infobox scaffolding."""
    tpl = WIKI_IMPORT_TEMPLATES.get(template_id, WIKI_IMPORT_TEMPLATES["generic"])
    meta_lines = [f"title={title}", "authors=Unknown", f"template={template_id}"]
    meta_lines.extend(tpl["meta_fields"])
    meta = "\n".join(meta_lines)

    if body.strip():
        main = f"# {title}\n\n{body.strip()}\n"
    else:
        hint = tpl["body_hint"]
        main = f"# {title}\n\n> _{hint}_\n\n## Overview\n\n## Notes\n\n- \n"

    return _serialize_sections([
        {"type": "META",         "content": meta},
        {"type": "HERO",         "content": ""},
        {"type": "MAIN_SECTION", "content": main},
        {"type": "REFERENCES",   "content": ""},
    ])

# Default section template for new articles
_DEFAULT_SECTIONS = [
    {'type': 'META',         'content': 'title=\nauthors=\n'},
    {'type': 'HERO',         'content': ''},
    {'type': 'MAIN_SECTION', 'content': ''},
    {'type': 'REFERENCES',   'content': ''},
]

# Canonical order for healing — these are ALWAYS present in every article file
_REQUIRED_SECTIONS = ['META', 'HERO', 'MAIN_SECTION', 'REFERENCES']

# Default content when a required section is re-inserted during healing
_SECTION_DEFAULTS = {
    'META':         'title=\nauthors=\n',
    'HERO':         '',
    'MAIN_SECTION': '',
    'REFERENCES':   '',
}


def _heal_sections(sections: list) -> list:
    """Ensure all required sections are present.
    Missing ones are silently re-inserted in canonical order.
    Custom sections are preserved in their original position."""
    present = {s['type'] for s in sections}
    missing = [t for t in _REQUIRED_SECTIONS if t not in present]
    if not missing:
        return sections  # nothing to heal

    # Insert each missing section at its canonical position
    healed = list(sections)
    for tag in _REQUIRED_SECTIONS:
        if tag in present:
            continue
        default_content = _SECTION_DEFAULTS.get(tag, '')
        # Find where to insert: after the last required section that comes before it
        canonical_idx   = _REQUIRED_SECTIONS.index(tag)
        insert_at       = 0
        for i, s in enumerate(healed):
            if s['type'] in _REQUIRED_SECTIONS:
                if _REQUIRED_SECTIONS.index(s['type']) < canonical_idx:
                    insert_at = i + 1
        healed.insert(insert_at, {'type': tag, 'content': default_content})
        present.add(tag)

    return healed


def _parse_sections(content: str) -> list:
    """Split on [TAG] markers. Returns [{type, content}].
    Falls back to a single MAIN_SECTION for legacy plain-markdown files."""
    matches = list(_SECTION_OPEN_RE.finditer(content))
    if not matches:
        return [{'type': 'MAIN_SECTION', 'content': content.strip()}]
    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections.append({
            'type':    m.group(1),
            'content': content[start:end].strip(),
        })
    return sections


def _serialize_sections(sections: list) -> str:
    """Serialize sections back to the [TAG]\ncontent\n\n[TAG]\ncontent format."""
    parts = []
    for s in sections:
        tag     = re.sub(r'[^A-Z0-9_]', '_', s.get('type', 'SECTION').strip().upper())
        content = s.get('content', '').strip()
        parts.append(f"[{tag}]\n{content}")
    return '\n\n'.join(parts) + '\n'


def _parse_meta_params(meta_content: str) -> dict:
    """Parse key=value lines from a META section into a dict."""
    params = {}
    for line in meta_content.splitlines():
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            key, _, val = line.partition('=')
            params[key.strip()] = val.strip()
    return params


def _check_access(project_id: int):
    project = get_project(project_id)
    if not project:
        abort(404)
    if session.get("role") != "admin" and not user_in_project(
            session["user_id"], project_id):
        abort(403)
    return project


def _get_tree(project_id: int) -> list:
    """Return articles as a list sorted by (parent_id, order_index) for tree rendering."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, slug, title, parent_id, order_index, is_published,
                  params, updated_at
           FROM wiki_articles WHERE project_id=?
           ORDER BY order_index""",
        (project_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _build_nested(flat: list, parent_id=None) -> list:
    """Recursively build nested tree from flat list."""
    return [
        {**a, "children": _build_nested(flat, a["id"])}
        for a in flat if a["parent_id"] == parent_id
    ]


def _assign_numbers(tree: list, prefix: str = "") -> list:
    """Walk tree and assign GDD numbering (1, 1.1, 1.1.2…)."""
    result = []
    for i, node in enumerate(tree, 1):
        num = f"{prefix}{i}" if prefix else str(i)
        result.append({**node, "gdd_num": num})
        result.extend(_assign_numbers(node.get("children", []), f"{num}."))
    return result


def _get_numbered_flat(project_id: int) -> list:
    """Return pre-order flat list with gdd_num and depth computed. No children key."""
    flat = _get_tree(project_id)
    tree = _build_nested(flat)
    numbered = _assign_numbers(tree)
    return [
        {
            "id":           n["id"],
            "slug":         n["slug"],
            "title":        n["title"],
            "parent_id":    n["parent_id"],
            "order_index":  n["order_index"],
            "is_published": n["is_published"],
            "params":       n.get("params"),
            "updated_at":   n.get("updated_at"),
            "gdd_num":      n["gdd_num"],
            "depth":        n["gdd_num"].count("."),
        }
        for n in numbered
    ]


# ── Lazy init ─────────────────────────────────────────────────────────────────

WELCOME_MD = """\
# Welcome to the {name} Wiki

This is your project's Game Design Wiki. Every article lives as a Markdown
file on disk, fully editable by your team and exportable as a GDD.

## Getting started

- Click **New Article** to create your first page
- Use `[[Article Title]]` to link between articles
- Use the **GDD** view to see the full numbered document

## Structure suggestion

```
1. Overview
   1.1 Vision
   1.2 Core Loop
2. Mechanics
   2.1 Combat
   2.2 Progression
3. Characters
4. World
5. Technical
```

Edit this article to make it your own.
"""


def _ensure_wiki(project_id: int, project_key: str, project_name: str) -> None:
    """Create folder structure + welcome article if this project has no wiki yet.
    Called on every wiki entry point — completely safe to call repeatedly."""
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM wiki_articles WHERE project_id=? LIMIT 1", (project_id,)
    ).fetchone()
    if exists:
        return  # already initialised

    # Create folders
    os.makedirs(_articles_dir(project_key), exist_ok=True)
    os.makedirs(_images_dir(project_key), exist_ok=True)
    os.makedirs(os.path.join(_images_dir(project_key), "thumbs"), exist_ok=True)

    # Create welcome article on disk
    content = WELCOME_MD.format(name=project_name)
    _write_md(project_key, "welcome", content)

    # Insert DB row
    now = int(time.time())
    conn.execute(
        """INSERT INTO wiki_articles
           (project_id, slug, title, parent_id, order_index, params,
            is_published, created_by, created_at, updated_by, updated_at)
           VALUES (?,?,?,NULL,1.0,'{}',1,?,?,?,?)""",
        (project_id, "welcome", f"{project_name} Wiki", session["user_id"],
         now, session["user_id"], now)
    )
    conn.commit()


# ── Route helpers ─────────────────────────────────────────────────────────────

def _resolve_wiki_links(md: str, project_id: int, mode: str = "wiki") -> str:
    """Replace [[Slug]] and [[Slug|Label]] with HTML links (wiki) or §ref (gdd)."""
    conn = get_db()

    def replace(m):
        inner = m.group(1)
        if "|" in inner:
            slug_or_title, label = inner.split("|", 1)
        else:
            slug_or_title = label = inner

        # Try slug first, then title
        row = conn.execute(
            "SELECT slug, title FROM wiki_articles WHERE project_id=? AND (slug=? OR title=?)",
            (project_id, slug_or_title.strip(), slug_or_title.strip())
        ).fetchone()

        if mode == "gdd":
            # In GDD mode we can't resolve §numbers here (need full tree),
            # so emit a placeholder the GDD renderer replaces.
            ref = row["slug"] if row else _slugify(slug_or_title)
            return f"[§{ref}]"
        else:
            if row:
                href = url_for("wiki_article",
                               project_id=project_id, slug=row["slug"])
                return f'<a class="wiki-link" href="{href}">{label.strip()}</a>'
            else:
                return f'<span class="wiki-link-missing" title="Article not found">{label.strip()}</span>'

    return re.sub(r"\[\[([^\]]+)\]\]", replace, md)


# ── Routes ────────────────────────────────────────────────────────────────────

def register(app) -> None:

    # ── Home (article tree) ──────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki")
    @login_required
    def wiki_home(project_id):
        project = _check_access(project_id)
        _ensure_wiki(project_id, project["key"], project["name"])
        flat = _get_numbered_flat(project_id)
        return render_template("wiki_home.html", project=project, flat=flat)

    # ── Read article ─────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/<slug>")
    @login_required
    def wiki_article(project_id, slug):
        project = _check_access(project_id)
        _ensure_wiki(project_id, project["key"], project["name"])
        conn = get_db()
        article = conn.execute(
            """SELECT a.*, u1.display_name AS created_name,
                      u2.display_name AS updated_name
               FROM wiki_articles a
               LEFT JOIN users u1 ON a.created_by = u1.id
               LEFT JOIN users u2 ON a.updated_by = u2.id
               WHERE a.project_id=? AND a.slug=?""",
            (project_id, slug)
        ).fetchone()
        if not article:
            abort(404)
        article = dict(article)

        raw_md = _read_md(project["key"], slug)
        sections = _heal_sections(_parse_sections(raw_md))

        # Build link map for [[wiki link]] resolution in JS
        all_articles = conn.execute(
            "SELECT slug, title FROM wiki_articles WHERE project_id=?",
            (project_id,)
        ).fetchall()
        link_map = {}
        for a in all_articles:
            href = url_for("wiki_article", project_id=project_id, slug=a["slug"])
            link_map[a["slug"]] = {"url": href, "title": a["title"]}
            link_map[a["title"].lower()] = {"url": href, "title": a["title"]}

        # Extract META params from sections (file is source of truth)
        meta_section = next((s for s in sections if s["type"] == "META"), None)
        params = _parse_meta_params(meta_section["content"]) if meta_section else {}

        # Breadcrumb: walk parent chain
        breadcrumb = []
        cur = article
        while cur.get("parent_id"):
            parent = conn.execute(
                "SELECT id, slug, title, parent_id FROM wiki_articles WHERE id=?",
                (cur["parent_id"],)
            ).fetchone()
            if parent:
                breadcrumb.insert(0, dict(parent))
                cur = dict(parent)
            else:
                break

        # Siblings for prev/next navigation
        siblings = conn.execute(
            """SELECT id, slug, title FROM wiki_articles
               WHERE project_id=? AND parent_id IS ?
               ORDER BY order_index""",
            (project_id, article["parent_id"])
        ).fetchall()
        siblings = [dict(s) for s in siblings]
        cur_idx = next((i for i, s in enumerate(siblings) if s["slug"] == slug), None)
        prev_art = siblings[cur_idx - 1] if cur_idx and cur_idx > 0 else None
        next_art = siblings[cur_idx + 1] if cur_idx is not None and cur_idx < len(siblings) - 1 else None

        flat = _get_tree(project_id)

        # Assign GDD numbers so this article knows its own number + children can show theirs
        tree     = _build_nested(flat)
        numbered = _assign_numbers(tree)
        num_map  = {a["slug"]: a["gdd_num"] for a in numbered}
        gdd_num  = num_map.get(slug, "")

        # Direct children of this article
        children = conn.execute(
            """SELECT id, slug, title, order_index FROM wiki_articles
               WHERE project_id=? AND parent_id=?
               ORDER BY order_index""",
            (project_id, article["id"])
        ).fetchall()
        children = [
            {**dict(c), "gdd_num": num_map.get(c["slug"], ""),
             "url": url_for("wiki_article", project_id=project_id, slug=c["slug"])}
            for c in children
        ]

        return render_template("wiki_article.html",
                               project=project,
                               article=article,
                               sections=sections,
                               link_map=link_map,
                               params=params,
                               breadcrumb=breadcrumb,
                               prev_art=prev_art,
                               next_art=next_art,
                               flat=flat,
                               gdd_num=gdd_num,
                               children=children)

    # ── New article ──────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/new", methods=["GET", "POST"])
    @login_required
    def wiki_new(project_id):
        project = _check_access(project_id)
        _ensure_wiki(project_id, project["key"], project["name"])
        conn = get_db()
        flat = _get_numbered_flat(project_id)

        if request.method == "POST":
            enforce_csrf()
            title = request.form.get("title", "").strip()
            parent_id = request.form.get("parent_id") or None
            if parent_id:
                parent_id = int(parent_id)
            if not title:
                flash("Title is required.", "error")
                return render_template("wiki_edit.html", project=project,
                                       article=None, flat=flat, mode="new",
                                       current_gdd_num='',
                                       articles_json=json.dumps([{"slug": a["slug"], "title": a["title"]} for a in flat]))

            base_slug = _slugify(title)
            slug = _unique_slug(project_id, base_slug)

            # Find max order_index among siblings
            max_order = conn.execute(
                "SELECT COALESCE(MAX(order_index),0) FROM wiki_articles WHERE project_id=? AND parent_id IS ?",
                (project_id, parent_id)
            ).fetchone()[0]

            now = int(time.time())
            initial_content = _serialize_sections([
                {'type': 'META',         'content': f'title={title}\nauthors=\n'},
                {'type': 'HERO',         'content': ''},
                {'type': 'MAIN_SECTION', 'content': f'# {title}\n\n'},
                {'type': 'REFERENCES',   'content': ''},
            ])
            _write_md(project["key"], slug, initial_content)

            conn.execute(
                """INSERT INTO wiki_articles
                   (project_id, slug, title, parent_id, order_index, params,
                    is_published, created_by, created_at, updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,1,?,?,?,?)""",
                (project_id, slug, title, parent_id, max_order + 1.0,
                 "{}", session["user_id"], now, session["user_id"], now)
            )
            conn.commit()
            return redirect(url_for("wiki_edit", project_id=project_id, slug=slug))

        parent_id = request.args.get("parent_id")
        articles_list = [{"slug": a["slug"], "title": a["title"]} for a in flat]
        return render_template("wiki_edit.html",
                               project=project, article=None,
                               flat=flat, mode="new",
                               current_gdd_num='',
                               prefill_parent=parent_id,
                               prefill_content=_serialize_sections(_DEFAULT_SECTIONS),
                               sections_json=json.dumps(_DEFAULT_SECTIONS),
                               articles_json=json.dumps(articles_list))

    # ── Edit article ─────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/<slug>/edit", methods=["GET", "POST"])
    @login_required
    def wiki_edit(project_id, slug):
        project = _check_access(project_id)
        conn = get_db()
        article = conn.execute(
            "SELECT * FROM wiki_articles WHERE project_id=? AND slug=?",
            (project_id, slug)
        ).fetchone()
        if not article:
            abort(404)
        article = dict(article)
        flat = _get_numbered_flat(project_id)
        num_map = {a["id"]: a["gdd_num"] for a in flat}
        current_gdd_num = num_map.get(article["id"], "")

        if request.method == "POST":
            enforce_csrf()
            title     = request.form.get("title", "").strip()
            content   = request.form.get("content", "")  # assembled by JS as [SECTION]...[/SECTION]
            parent_id = request.form.get("parent_id") or None
            if parent_id:
                parent_id = int(parent_id)

            if not title:
                flash("Title is required.", "error")
                sections = _heal_sections(_parse_sections(content))
                return render_template("wiki_edit.html", project=project,
                                       article=article, flat=flat, mode="edit",
                                       current_gdd_num=current_gdd_num,
                                       prefill_content=_serialize_sections(sections),
                                       sections_json=json.dumps(sections),
                                       articles_json=json.dumps([{"slug": a["slug"], "title": a["title"]} for a in flat]))

            # Heal: silently restore any deleted required sections before saving
            sections = _heal_sections(_parse_sections(content))
            healed_content = _serialize_sections(sections)

            now = int(time.time())
            _write_md(project["key"], slug, healed_content)
            conn.execute(
                """UPDATE wiki_articles
                   SET title=?, parent_id=?, updated_by=?, updated_at=?
                   WHERE project_id=? AND slug=?""",
                (title, parent_id, session["user_id"], now, project_id, slug)
            )
            conn.commit()
            flash("Article saved.", "success")
            return redirect(url_for("wiki_article", project_id=project_id, slug=slug))

        content  = _read_md(project["key"], slug)
        sections = _heal_sections(_parse_sections(content))
        articles_list = [{"slug": a["slug"], "title": a["title"]} for a in flat]
        return render_template("wiki_edit.html",
                               project=project, article=article,
                               flat=flat, mode="edit",
                               current_gdd_num=current_gdd_num,
                               prefill_content=_serialize_sections(sections),
                               sections_json=json.dumps(sections),
                               articles_json=json.dumps(articles_list))

    # ── Delete article ───────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/<slug>/delete", methods=["POST"])
    @login_required
    def wiki_delete(project_id, slug):
        project = _check_access(project_id)
        enforce_csrf()
        if session.get("role") not in ("admin", "super_user"):
            abort(403)
        conn = get_db()
        conn.execute(
            "DELETE FROM wiki_articles WHERE project_id=? AND slug=?",
            (project_id, slug)
        )
        conn.commit()
        path = _article_path(project["key"], slug)
        if os.path.exists(path):
            os.remove(path)
        flash("Article deleted.", "success")
        return redirect(url_for("wiki_home", project_id=project_id))

    # ── Reorder API (drag-drop) ──────────────────────────────────────────────

    @app.route("/api/project/<int:project_id>/wiki/reorder", methods=["POST"])
    @login_required
    def wiki_reorder(project_id):
        _check_access(project_id)
        enforce_csrf()
        data = request.get_json(force=True)
        # items: [{id, parent_id, order_index}]
        conn = get_db()
        for item in data.get("items", []):
            pid = item.get("parent_id")  # may be None
            conn.execute(
                "UPDATE wiki_articles SET parent_id=?, order_index=? WHERE id=? AND project_id=?",
                (pid, item["order_index"], item["id"], project_id)
            )
        conn.commit()
        return jsonify({"ok": True})

    # ── Image upload ─────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/images/upload", methods=["POST"])
    @login_required
    def wiki_image_upload(project_id):
        project = _check_access(project_id)
        enforce_csrf()
        f = request.files.get("image")
        if not f or not f.filename:
            return jsonify({"ok": False, "error": "No file"}), 400

        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return jsonify({"ok": False, "error": "Invalid file type"}), 400

        img_dir = _images_dir(project["key"])
        thumb_dir = os.path.join(img_dir, "thumbs")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)

        # Safe unique filename
        base = _slugify(os.path.splitext(f.filename)[0]) or "image"
        ts = int(time.time())
        filename = f"{base}-{ts}{ext}"
        thumb_filename = f"thumb-{filename}"

        full_path = os.path.join(img_dir, filename)
        thumb_path = os.path.join(thumb_dir, thumb_filename)

        f.save(full_path)

        # Generate thumbnail (max 300px wide)
        try:
            img = Image.open(full_path)
            img.thumbnail((300, 300))
            img.save(thumb_path)
        except Exception:
            thumb_filename = filename  # fallback: use original

        caption = request.form.get("caption", "")
        conn = get_db()
        conn.execute(
            """INSERT INTO wiki_images (project_id, filename, thumb_filename, caption, uploaded_by, uploaded_at)
               VALUES (?,?,?,?,?,?)""",
            (project_id, filename, thumb_filename, caption,
             session["user_id"], ts)
        )
        conn.commit()

        return jsonify({
            "ok": True,
            "filename": filename,
            "thumb_filename": thumb_filename,
            "md_snippet": f"![{caption or filename}]({filename})",
            "url": url_for("wiki_image_serve", project_id=project_id, filename=filename),
            "thumb_url": url_for("wiki_image_serve", project_id=project_id,
                                 filename=f"thumbs/{thumb_filename}"),
        })

    # ── Image serve ──────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/images/<path:filename>")
    @login_required
    def wiki_image_serve(project_id, filename):
        from flask import send_from_directory
        project = _check_access(project_id)
        img_dir = _images_dir(project["key"])
        return send_from_directory(img_dir, filename)

    # ── Image list API ───────────────────────────────────────────────────────

    @app.route("/api/project/<int:project_id>/wiki/images")
    @login_required
    def wiki_images_list(project_id):
        project = _check_access(project_id)
        conn = get_db()
        imgs = conn.execute(
            "SELECT id, filename, thumb_filename, caption FROM wiki_images WHERE project_id=? ORDER BY uploaded_at DESC",
            (project_id,)
        ).fetchall()
        return jsonify([{
            "id": r["id"],
            "filename": r["filename"],
            "caption": r["caption"],
            "url": url_for("wiki_image_serve", project_id=project_id, filename=r["filename"]),
            "thumb_url": url_for("wiki_image_serve", project_id=project_id,
                                 filename=f"thumbs/{r['thumb_filename']}") if r["thumb_filename"] else None,
            "md_snippet": f"![{r['caption'] or r['filename']}]({r['filename']})",
        } for r in imgs])

    # ── GDD view ─────────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/gdd")
    @login_required
    def wiki_gdd(project_id):
        project = _check_access(project_id)
        _ensure_wiki(project_id, project["key"], project["name"])
        flat = _get_tree(project_id)
        tree = _build_nested(flat)
        numbered = _assign_numbers(tree)
        # Build slug→number map for [[link]] resolution
        num_map = {a["slug"]: a["gdd_num"] for a in numbered}

        # Build slug→url map for clickable GDD links
        slug_url_map = {
            a["slug"]: url_for("wiki_article", project_id=project_id, slug=a["slug"])
            for a in numbered
        }
        # Also map by title (lowercase) → slug for [[Title]] style links
        title_slug_map = {a["title"].lower(): a["slug"] for a in numbered}

        # Load content for each article, resolve [[links]] as clickable §refs
        _GDD_SKIP = {"META", "HERO"}
        sections = []
        for a in numbered:
            raw_full = _read_md(project["key"], a["slug"])
            # Strip META/HERO params — GDD only shows body content
            parsed = _parse_sections(raw_full)
            raw = _serialize_sections([s for s in parsed if s["type"] not in _GDD_SKIP])
            def gdd_replace(m, _num_map=num_map, _slug_url=slug_url_map, _title_slug=title_slug_map):
                inner = m.group(1)
                parts = inner.split("|")
                key   = parts[0].strip()
                label = parts[-1].strip()
                # Resolve slug or title → gdd_num + url
                resolved_slug = key if key in _num_map else _title_slug.get(key.lower(), key)
                ref  = _num_map.get(resolved_slug, key)
                url  = _slug_url.get(resolved_slug, "#")
                display = f"§{ref} — {label}" if "|" in inner else f"§{ref}"
                return f'<a href="{url}" class="gdd-ref-link">{display}</a>'

            resolved = re.sub(r"\[\[([^\]]+)\]\]", gdd_replace, raw)
            sections.append({**a, "content": resolved})

        # Last modified: max updated_at across all articles
        import datetime as _dt
        max_ts = max((a.get("updated_at") or 0 for a in numbered), default=0)
        last_modified = _dt.datetime.fromtimestamp(max_ts).strftime("%-d %B %Y") if max_ts else ""

        cover = _read_gdd_cover(project["key"])

        return render_template("wiki_gdd.html",
                               project=project,
                               sections=sections,
                               flat=flat,
                               last_modified=last_modified,
                               cover=cover)

    # ── GDD PDF (standalone, auto-print) ─────────────────────────────────────

    @app.route("/project/<int:project_id>/gdd/pdf")
    @login_required
    def wiki_gdd_pdf(project_id):
        import datetime as _dt
        import subprocess
        import tempfile
        import markdown as _md
        project = _check_access(project_id)
        flat = _get_tree(project_id)
        tree = _build_nested(flat)
        numbered = _assign_numbers(tree)
        num_map        = {a["slug"]: a["gdd_num"] for a in numbered}
        slug_url_map   = {a["slug"]: "#gdd-" + a["slug"] for a in numbered}
        title_slug_map = {a["title"].lower(): a["slug"] for a in numbered}

        _GDD_SKIP = {"META", "HERO"}
        _md_renderer = _md.Markdown(
            extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
            output_format="html",
        )

        def _render_md(text: str) -> str:
            _md_renderer.reset()
            return _md_renderer.convert(text)

        def _resolve_links(text: str) -> str:
            """Replace [[wiki links]] with plain text for PDF."""
            def _repl(m):
                inner = m.group(1)
                parts = inner.split("|")
                key   = parts[0].strip()
                label = parts[-1].strip()
                resolved_slug = key if key in num_map else title_slug_map.get(key.lower(), key)
                ref = num_map.get(resolved_slug, "")
                return f"§{ref} {label}" if ref else label
            return re.sub(r"\[\[([^\]]+)\]\]", _repl, text)

        sections = []
        for a in numbered:
            raw_full = _read_md(project["key"], a["slug"])
            parsed   = _parse_sections(raw_full)
            # Collect only non-skipped sections, concatenate their content
            body_parts = []
            for s in parsed:
                if s["type"] in _GDD_SKIP:
                    continue
                content = s["content"].strip()
                if not content:
                    continue
                # Strip [SECTION_TAG] lines and leading h1 (the article title)
                content = re.sub(r"^\[[A-Z][A-Z_0-9]*\]\s*", "", content, flags=re.MULTILINE)
                content = re.sub(r"^#{1}\s+[^\n]*\n?", "", content, count=1)
                content = content.strip()
                if content:
                    body_parts.append(content)
            body = "\n\n".join(body_parts)
            body = _resolve_links(body)
            html = _render_md(body) if body else ""
            sections.append({**a, "html": html})

        max_ts        = max((a.get("updated_at") or 0 for a in numbered), default=0)
        last_modified = _dt.datetime.fromtimestamp(max_ts).strftime("%-d %B %Y") if max_ts else ""
        cover         = _read_gdd_cover(project["key"])

        # Build absolute file:// path for cover image so headless Brave can load it
        cover_img_abs = ""
        if cover.get("cover_image"):
            img_path = os.path.join(_images_dir(project["key"]), cover["cover_image"])
            if os.path.exists(img_path):
                cover_img_abs = "file://" + img_path

        from flask import render_template as _rt
        html = _rt("wiki_gdd_pdf.html",
                   project=project,
                   sections=sections,
                   last_modified=last_modified,
                   cover=cover,
                   cover_img_abs=cover_img_abs)

        BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        if not os.path.exists(BRAVE):
            from flask import Response
            return Response(
                html,
                mimetype="text/html",
                headers={"Content-Disposition": f'attachment; filename="{project["key"]}_GDD.html"'}
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = os.path.join(tmpdir, "gdd.html")
            pdf_path  = os.path.join(tmpdir, "gdd.pdf")
            # Rewrite local static asset URLs to absolute file:// paths
            static_dir = os.path.join(Config.BASE_DIR, "static")
            html_out = html.replace(
                'href="/static/', f'href="file://{static_dir}/'
            ).replace(
                'src="/static/', f'src="file://{static_dir}/'
            )
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_out)

            try:
                subprocess.run(
                    [
                        BRAVE,
                        "--headless",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--no-pdf-header-footer",
                        "--print-to-pdf=" + pdf_path,
                        "--print-to-pdf-no-header",
                        "file://" + html_path,
                    ],
                    check=True,
                    timeout=60,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                abort(500)

            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

        from flask import Response
        filename = f"{project['key']}_GDD.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )


        flat = _get_tree(project_id)
        tree = _build_nested(flat)
        numbered = _assign_numbers(tree)
        num_map        = {a["slug"]: a["gdd_num"] for a in numbered}
        slug_url_map   = {a["slug"]: "#gdd-" + a["slug"] for a in numbered}
        title_slug_map = {a["title"].lower(): a["slug"] for a in numbered}

        _GDD_SKIP = {"META", "HERO"}
        sections = []
        for a in numbered:
            raw_full = _read_md(project["key"], a["slug"])
            parsed   = _parse_sections(raw_full)
            raw      = _serialize_sections([s for s in parsed if s["type"] not in _GDD_SKIP])
            def gdd_replace(m, _nm=num_map, _su=slug_url_map, _ts=title_slug_map):
                inner = m.group(1)
                parts = inner.split("|")
                key   = parts[0].strip()
                label = parts[-1].strip()
                resolved_slug = key if key in _nm else _ts.get(key.lower(), key)
                ref     = _nm.get(resolved_slug, key)
                url     = _su.get(resolved_slug, "#")
                display = f"§{ref} — {label}" if "|" in inner else f"§{ref}"
                return f'<a href="{url}">{display}</a>'
            resolved = re.sub(r"\[\[([^\]]+)\]\]", gdd_replace, raw)
            sections.append({**a, "content": resolved})

        max_ts        = max((a.get("updated_at") or 0 for a in numbered), default=0)
        last_modified = _dt.datetime.fromtimestamp(max_ts).strftime("%-d %B %Y") if max_ts else ""
        cover         = _read_gdd_cover(project["key"])

        # Render the full print-ready HTML to a string
        from flask import render_template as _rt
        html = _rt("wiki_gdd_pdf.html",
                   project=project,
                   sections=sections,
                   last_modified=last_modified,
                   cover=cover)

        # Write HTML to a temp file and run headless Brave to produce a PDF
        BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        if not os.path.exists(BRAVE):
            # Fallback: stream the HTML as a downloadable .html file
            from flask import Response
            return Response(
                html,
                mimetype="text/html",
                headers={"Content-Disposition": f'attachment; filename="{project["key"]}_GDD.html"'}
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = os.path.join(tmpdir, "gdd.html")
            pdf_path  = os.path.join(tmpdir, "gdd.pdf")
            # Rewrite static asset URLs to absolute paths so headless browser finds them
            static_dir = os.path.join(Config.BASE_DIR, "static")
            html_absolute = html.replace(
                'href="/static/', f'href="file://{static_dir}/'
            ).replace(
                'src="/static/', f'src="file://{static_dir}/'
            )
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_absolute)

            try:
                subprocess.run(
                    [
                        BRAVE,
                        "--headless",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--no-pdf-header-footer",
                        "--print-to-pdf=" + pdf_path,
                        "--print-to-pdf-no-header",
                        "file://" + html_path,
                    ],
                    check=True,
                    timeout=60,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                abort(500)

            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

        from flask import Response
        filename = f"{project['key']}_GDD.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )


    # ── GDD cover save ────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/gdd/cover", methods=["POST"])
    @login_required
    def wiki_gdd_cover_save(project_id):
        project = _check_access(project_id)
        if session.get("role") not in ("admin", "super_user"):
            abort(403)
        enforce_csrf()
        _COVER_FIELDS = ["studio", "version", "date", "status",
                         "genre", "platform", "tagline", "cover_image", "description"]
        data = {k: request.form.get(k, "").strip() for k in _COVER_FIELDS}
        _write_gdd_cover(project["key"], data)
        flash("Cover page saved.", "success")
        return redirect(url_for("wiki_gdd", project_id=project_id))

    # ── GDD export as .md ────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/gdd/export")
    @login_required
    def wiki_gdd_export(project_id):
        from flask import Response
        project = _check_access(project_id)
        flat = _get_tree(project_id)
        tree = _build_nested(flat)
        numbered = _assign_numbers(tree)
        num_map = {a["slug"]: a["gdd_num"] for a in numbered}

        lines = [f"# {project['name']} — Game Design Document\n\n"]
        for a in numbered:
            depth = a["gdd_num"].count(".") + 1
            heading = "#" * min(depth + 1, 6)
            raw = _read_md(project["key"], a["slug"])

            def gdd_replace(m):
                inner = m.group(1)
                slug_or_title = inner.split("|")[0].strip()
                label = inner.split("|")[-1].strip()
                ref = num_map.get(slug_or_title, slug_or_title)
                return f"§{ref} ({label})" if "|" in inner else f"§{ref}"

            resolved = re.sub(r"\[\[([^\]]+)\]\]", gdd_replace, raw)
            lines.append(f"{heading} {a['gdd_num']}. {a['title']}\n\n")
            # Strip the leading # title from the article body (we add our own)
            body = re.sub(r"^#[^\n]*\n", "", resolved, count=1).strip()
            if body:
                lines.append(body + "\n\n")

        content = "".join(lines)
        filename = f"{project['key'].lower()}-gdd.md"
        return Response(
            content,
            mimetype="text/markdown",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    # ── GDD import ───────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/import", methods=["GET", "POST"])
    @login_required
    def wiki_import(project_id):
        project = _check_access(project_id)
        if session.get("role") not in ("admin", "super_user"):
            abort(403)
        flat = _get_tree(project_id)

        if request.method == "POST":
            enforce_csrf()
            raw_text = request.form.get("gdd_text", "").strip()
            import_mode = request.form.get("import_mode", "append")
            # Template assignments from JS preview: JSON list [{num, template}]
            assignments_raw = request.form.get("template_assignments", "[]")
            try:
                assignments = {a["num"]: a["template"] for a in json.loads(assignments_raw)}
            except Exception:
                assignments = {}

            if not raw_text:
                flash("Nothing to import.", "error")
                return redirect(request.url)

            pattern = re.compile(
                r"^(\d+(?:\.\d+)*)"
                r"\.?"
                r"[ \t]*"
                r"(?:[-\u2013\u2014][ \t]*)?"
                r"(.*)$"
            )
            lines = raw_text.splitlines()
            parsed = []
            current = None
            for line in lines:
                m = pattern.match(line.strip())
                if m:
                    if current:
                        parsed.append(current)
                    num   = m.group(1)
                    title = m.group(2).strip() or f"Section {m.group(1)}"
                    current = {"num": num, "title": title, "body": []}
                elif current is not None:
                    current["body"].append(line)
            if current:
                parsed.append(current)

            if not parsed:
                flash("No numbered headings found. Use format: 1.2 Title", "error")
                return redirect(request.url)

            conn = get_db()
            now = int(time.time())
            _ensure_wiki(project_id, project["key"], project["name"])
            os.makedirs(_articles_dir(project["key"]), exist_ok=True)

            if import_mode == "replace":
                existing = conn.execute(
                    "SELECT slug FROM wiki_articles WHERE project_id=?", (project_id,)
                ).fetchall()
                for row in existing:
                    path = _article_path(project["key"], row["slug"])
                    if os.path.exists(path):
                        os.remove(path)
                conn.execute("DELETE FROM wiki_articles WHERE project_id=?", (project_id,))
                conn.commit()

            # Build parent title map for detection context
            num_title_map = {item["num"]: item["title"] for item in parsed}

            id_map = {}
            for item in parsed:
                num   = item["num"]
                title = item["title"]
                body  = "\n".join(item["body"]).strip()
                parts = num.split(".")

                # Determine template: use JS assignment if available, else auto-detect
                parent_num = ".".join(parts[:-1])
                parent_title = num_title_map.get(parent_num, "")
                template_id = assignments.get(num) or _detect_wiki_template(title, parent_title)

                slug    = _unique_slug(project_id, _slugify(title))
                content = _build_wiki_article_content(title, body, template_id)
                parent_id = id_map.get(parent_num)
                order   = float(parts[-1])

                _write_md(project["key"], slug, content)
                cur = conn.execute(
                    """INSERT INTO wiki_articles
                       (project_id, slug, title, parent_id, order_index, params,
                        is_published, created_by, created_at, updated_by, updated_at)
                       VALUES (?,?,?,?,?,?,1,?,?,?,?)""",
                    (project_id, slug, title, parent_id, order, "{}",
                     session["user_id"], now, session["user_id"], now)
                )
                id_map[num] = cur.lastrowid

            conn.commit()
            action_label = "Replaced all articles with" if import_mode == "replace" else "Appended"
            flash(f"{action_label} {len(parsed)} imported articles.", "success")
            return redirect(url_for("wiki_home", project_id=project_id))

        return render_template("wiki_import.html",
                               project=project, flat=flat,
                               templates_json=json.dumps(WIKI_IMPORT_TEMPLATES),
                               template_list_json=json.dumps(WIKI_TEMPLATE_LIST))

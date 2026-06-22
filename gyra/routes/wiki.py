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
        flat = _get_tree(project_id)
        tree = _build_nested(flat)
        return render_template("wiki_home.html",
                               project=project, tree=tree, flat=flat)

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
        # Do NOT server-resolve [[links]] — pass raw MD + a link map to JS
        # so marked.js renders clean HTML without Jinja escaping corruption.
        all_articles = conn.execute(
            "SELECT slug, title FROM wiki_articles WHERE project_id=?",
            (project_id,)
        ).fetchall()
        link_map = {}
        for a in all_articles:
            href = url_for("wiki_article", project_id=project_id, slug=a["slug"])
            link_map[a["slug"]] = {"url": href, "title": a["title"]}
            # also map by title (lowercased) for [[Title]] style links
            link_map[a["title"].lower()] = {"url": href, "title": a["title"]}

        # Parse params JSON for infobox
        try:
            params = json.loads(article["params"] or "{}")
        except Exception:
            params = {}

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

        return render_template("wiki_article.html",
                               project=project,
                               article=article,
                               raw_md=raw_md,
                               link_map=link_map,
                               params=params,
                               breadcrumb=breadcrumb,
                               prev_art=prev_art,
                               next_art=next_art,
                               flat=flat)

    # ── New article ──────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/wiki/new", methods=["GET", "POST"])
    @login_required
    def wiki_new(project_id):
        project = _check_access(project_id)
        _ensure_wiki(project_id, project["key"], project["name"])
        conn = get_db()
        flat = _get_tree(project_id)

        if request.method == "POST":
            enforce_csrf()
            title = request.form.get("title", "").strip()
            parent_id = request.form.get("parent_id") or None
            if parent_id:
                parent_id = int(parent_id)
            if not title:
                flash("Title is required.", "error")
                return render_template("wiki_edit.html", project=project,
                                       article=None, flat=flat, mode="new")

            base_slug = _slugify(title)
            slug = _unique_slug(project_id, base_slug)

            # Find max order_index among siblings
            max_order = conn.execute(
                "SELECT COALESCE(MAX(order_index),0) FROM wiki_articles WHERE project_id=? AND parent_id IS ?",
                (project_id, parent_id)
            ).fetchone()[0]

            now = int(time.time())
            initial_content = f"# {title}\n\n"
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
        return render_template("wiki_edit.html",
                               project=project, article=None,
                               flat=flat, mode="new",
                               prefill_parent=parent_id)

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
        flat = _get_tree(project_id)

        if request.method == "POST":
            enforce_csrf()
            title   = request.form.get("title", "").strip()
            content = request.form.get("content", "")
            params  = request.form.get("params", "{}").strip()
            parent_id = request.form.get("parent_id") or None
            if parent_id:
                parent_id = int(parent_id)

            # Validate params JSON
            try:
                json.loads(params)
            except Exception:
                params = "{}"

            if not title:
                flash("Title is required.", "error")
                return render_template("wiki_edit.html", project=project,
                                       article=article, flat=flat, mode="edit",
                                       prefill_content=content)

            now = int(time.time())
            _write_md(project["key"], slug, content)
            conn.execute(
                """UPDATE wiki_articles
                   SET title=?, params=?, parent_id=?, updated_by=?, updated_at=?
                   WHERE project_id=? AND slug=?""",
                (title, params, parent_id, session["user_id"], now,
                 project_id, slug)
            )
            conn.commit()
            flash("Article saved.", "success")
            return redirect(url_for("wiki_article", project_id=project_id, slug=slug))

        content = _read_md(project["key"], slug)
        return render_template("wiki_edit.html",
                               project=project, article=article,
                               flat=flat, mode="edit",
                               prefill_content=content)

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

        # Load content for each article, resolve [[links]] as §refs
        sections = []
        for a in numbered:
            raw = _read_md(project["key"], a["slug"])
            # Replace [[slug]] with § references using num_map
            def gdd_replace(m):
                inner = m.group(1)
                slug_or_title = inner.split("|")[0].strip()
                label = inner.split("|")[-1].strip()
                ref = num_map.get(slug_or_title, slug_or_title)
                return f"§{ref} ({label})" if "|" in inner else f"§{ref}"

            resolved = re.sub(r"\[\[([^\]]+)\]\]", gdd_replace, raw)
            sections.append({**a, "content": resolved})

        return render_template("wiki_gdd.html",
                               project=project,
                               sections=sections,
                               flat=flat)

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
            if not raw_text:
                flash("Nothing to import.", "error")
                return redirect(request.url)

            # Parse numbered headings: "1.2.3 Title" or "1.2.3. Title"
            pattern = re.compile(r"^(\d+(?:\.\d+)*\.?)\s+(.+)$")
            lines = raw_text.splitlines()
            parsed = []   # [{num, title, content_lines}]
            current = None
            for line in lines:
                m = pattern.match(line.strip())
                if m:
                    if current:
                        parsed.append(current)
                    num = m.group(1).rstrip(".")
                    current = {"num": num, "title": m.group(2).strip(), "body": []}
                elif current is not None:
                    current["body"].append(line)
            if current:
                parsed.append(current)

            if not parsed:
                flash("No numbered headings found. Use format: 1.2 Title", "error")
                return redirect(request.url)

            # Build id map: num_string → db id (for parent resolution)
            conn = get_db()
            id_map = {}
            now = int(time.time())
            _ensure_wiki(project_id, project["key"], project["name"])
            os.makedirs(_articles_dir(project["key"]), exist_ok=True)

            for item in parsed:
                num = item["num"]
                title = item["title"]
                slug = _unique_slug(project_id, _slugify(title))
                body = "\n".join(item["body"]).strip()
                content = f"# {title}\n\n{body}\n" if body else f"# {title}\n\n"

                # Parent: drop last segment of num
                parts = num.split(".")
                parent_num = ".".join(parts[:-1])
                parent_id = id_map.get(parent_num)

                order = float(parts[-1])
                _write_md(project["key"], slug, content)
                cur = conn.execute(
                    """INSERT INTO wiki_articles
                       (project_id, slug, title, parent_id, order_index, params,
                        is_published, created_by, created_at, updated_by, updated_at)
                       VALUES (?,?,?,?,?,1,1,?,?,?,?)""",
                    (project_id, slug, title, parent_id, order,
                     session["user_id"], now, session["user_id"], now)
                )
                id_map[num] = cur.lastrowid

            conn.commit()
            flash(f"Imported {len(parsed)} articles.", "success")
            return redirect(url_for("wiki_home", project_id=project_id))

        return render_template("wiki_import.html",
                               project=project, flat=flat)

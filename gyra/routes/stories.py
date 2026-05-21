"""routes/stories.py — Story create and story detail views."""
import os
import re
import time
import uuid

from flask import (abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from PIL import Image
from werkzeug.utils import secure_filename

from auth import enforce_csrf, login_required
from config import Config
from db import (create_addon, create_notification, ensure_story_types,
                get_all_active_users, get_db, get_project,
                get_statuses, get_story, get_story_addons, get_story_history,
                get_story_images, get_story_types, get_story_users,
                get_user_by_username, log_story_change, user_in_project)
from routes.helpers import (allowed_image, build_story_title, count_words)


def register(app) -> None:

    @app.route("/story/new", methods=["GET", "POST"])
    @login_required
    def story_new():
        project_id = request.args.get("project_id", type=int)

        if request.method == "POST":
            enforce_csrf()
            project_id  = request.form.get("project_id", type=int)
            actor    = request.form.get("story_actor", "User").strip()
            verb     = request.form.get("story_verb", "needs").strip()
            z        = request.form.get("story_z", "").strip()
            x        = request.form.get("story_x", "").strip()
            for_conn = request.form.get("story_for", "to").strip()
            y        = request.form.get("story_y", "").strip()
            title    = build_story_title(actor, verb, z, x, for_conn, y)

            description = request.form.get("description", "").strip()
            ac          = request.form.get("acceptance_criteria", "").strip()
            points      = request.form.get("story_points", 0, type=int)
            status_id   = request.form.get("status_id", type=int) or None
            assignees   = request.form.getlist("assignee_ids", type=int)
            sprint      = request.form.get("sprint", type=int) or None
            story_type  = request.form.get("story_type", type=int) or None
            priority    = request.form.get("priority", "").strip() or None

            if not z or not x or not y or not project_id:
                flash("All story parts are required.", "error")
                return redirect(request.url)

            if " " in z.strip():
                flash("The action word must be a single word "
                      "(e.g. Walking, Killing, Building).", "error")
                return redirect(request.url)

            word_count = count_words(actor, verb, z, x, for_conn, y)
            if word_count > 19:
                flash(f"Story title is {word_count} words — max is 19.", "error")
                return redirect(request.url)

            now  = int(time.time())
            conn = get_db()
            row  = conn.execute(
                "SELECT COALESCE(MAX(order_index),0)+1 AS nxt "
                "FROM stories WHERE project_id=?",
                (project_id,),
            ).fetchone()
            order = row["nxt"]

            cur = conn.execute(
                """INSERT INTO stories
                   (project_id,title,description,acceptance_criteria,story_points,
                    status_id,sprint,order_index,created_at,created_by,updated_at,
                    story_actor,story_verb,story_z,story_x,story_for,story_y,
                    story_type,priority)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, title, description, ac, points,
                 status_id, sprint, order, now, session["user_id"], now,
                 actor, verb, z, x, for_conn, y, story_type, priority),
            )
            story_id = cur.lastrowid
            for uid in assignees:
                conn.execute(
                    "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
                    (story_id, uid),
                )
            conn.commit()
            conn.close()

            for f in request.files.getlist("images"):
                if f and f.filename and allowed_image(f.filename):
                    ext      = secure_filename(f.filename).rsplit(".", 1)[1].lower()
                    filename = f"{uuid.uuid4().hex}.{ext}"
                    filepath = os.path.join(Config.STORY_IMAGES_FOLDER, filename)
                    img = Image.open(f.stream)
                    img.thumbnail((900, 900), Image.LANCZOS)
                    img.save(filepath, quality=88)
                    conn2 = get_db()
                    conn2.execute(
                        "INSERT INTO story_images (story_id,filename,created_at) "
                        "VALUES (?,?,?)",
                        (story_id, filename, int(time.time())),
                    )
                    conn2.commit()
                    conn2.close()

            for task_text in request.form.getlist("new_task"):
                task_text = task_text.strip()
                if task_text:
                    create_addon(story_id, task_text, None, session["user_id"])

            flash("Story created.", "success")
            if sprint:
                return redirect(url_for("board", project_id=project_id))
            return redirect(url_for("backlog", project_id=project_id))

        project    = get_project(project_id) if project_id else None
        statuses   = get_statuses(project_id) if project_id else []
        all_users  = get_all_active_users()
        default_sprint = request.args.get("sprint", type=int)
        if project_id:
            with get_db() as _c:
                ensure_story_types(project_id, _c)
        story_types = get_story_types(project_id) if project_id else []
        return render_template(
            "story.html",
            mode="new",
            story=None,
            project=project,
            project_id=project_id,
            statuses=statuses,
            all_users=all_users,
            story_types=story_types,
            assignees=[],
            assignee_ids=[],
            comments=[],
            images=[],
            default_sprint=default_sprint,
            history=[],
            addons=[],
        )

    @app.route("/story/<int:story_id>", methods=["GET", "POST"])
    @login_required
    def story_view(story_id):
        s = get_story(story_id)
        if not s:
            abort(404)
        if session.get("role") != "admin" and not user_in_project(
                session["user_id"], s["project_id"]):
            abort(403)

        if request.method == "POST":
            enforce_csrf()
            is_modal = bool(request.args.get("modal"))
            action = request.form.get("action")

            if action == "update":
                actor    = request.form.get("story_actor", "User").strip()
                verb     = request.form.get("story_verb", "needs").strip()
                z        = request.form.get("story_z", "").strip()
                x        = request.form.get("story_x", "").strip()
                for_conn = request.form.get("story_for", "to").strip()
                y        = request.form.get("story_y", "").strip()
                title    = build_story_title(actor, verb, z, x, for_conn, y)

                if z and " " in z.strip():
                    if is_modal:
                        return jsonify(ok=False, error="The action word must be a "
                                       "single word (e.g. Walking, Killing, Building).")
                    flash("The action word must be a single word "
                          "(e.g. Walking, Killing, Building).", "error")
                    return redirect(url_for("story_view", story_id=story_id))

                word_count = count_words(actor, verb, z, x, for_conn, y)
                if word_count > 19:
                    if is_modal:
                        return jsonify(ok=False,
                                       error=f"Story is {word_count} words — max 19.")
                    flash(f"Story is {word_count} words — max 19.", "error")
                    return redirect(url_for("story_view", story_id=story_id))

                desc       = request.form.get("description", "").strip()
                ac         = request.form.get("acceptance_criteria", "").strip()
                points     = request.form.get("story_points", 0, type=int)
                status_id  = request.form.get("status_id", type=int) or None
                sprint     = request.form.get("sprint", type=int) or None
                assignees  = request.form.getlist("assignee_ids", type=int)
                story_type = request.form.get("story_type", type=int) or None
                priority   = request.form.get("priority", "").strip() or None

                old = dict(s)
                old_assignee_ids = {a["id"] for a in get_story_users(story_id)}

                conn = get_db()
                conn.execute(
                    """UPDATE stories SET title=?,description=?,acceptance_criteria=?,
                       story_points=?,status_id=?,sprint=?,updated_at=?,
                       story_actor=?,story_verb=?,story_z=?,story_x=?,
                       story_for=?,story_y=?,story_type=?,priority=?
                       WHERE id=?""",
                    (title, desc, ac, points, status_id, sprint, int(time.time()),
                     actor, verb, z, x, for_conn, y,
                     story_type, priority, story_id),
                )
                conn.execute("DELETE FROM story_users WHERE story_id=?", (story_id,))
                for uid in assignees:
                    conn.execute(
                        "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
                        (story_id, uid),
                    )
                conn.commit()
                conn.close()

                uid = session["user_id"]
                log_story_change(story_id, uid, "Status",
                                 old.get("status_id"), status_id)
                log_story_change(story_id, uid, "Sprint",
                                 old.get("sprint"), sprint)
                log_story_change(story_id, uid, "Priority",
                                 old.get("priority"), priority)
                log_story_change(story_id, uid, "Points",
                                 old.get("story_points"), points)
                if old.get("title") != title:
                    log_story_change(story_id, uid, "Title",
                                     old.get("title"), title)

                new_assignee_ids = set(assignees)
                for newly_assigned_id in (new_assignee_ids - old_assignee_ids):
                    if newly_assigned_id != uid:
                        create_notification(
                            user_id=newly_assigned_id,
                            type_="assignment",
                            message=(f"{session['display_name']} assigned you to "
                                     f"'{s['title'][:50]}'"),
                            story_id=story_id,
                            from_user=uid,
                        )

                flash("Story updated.", "success")
                if is_modal:
                    return jsonify(ok=True, action="update", story_id=story_id)
                pid = s["project_id"]
                if sprint:
                    return redirect(url_for("board", project_id=pid))
                return redirect(url_for("backlog", project_id=pid))

            if action == "comment":
                content = request.form.get("content", "").strip()
                if content:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO comments (story_id,user_id,content,created_at) "
                        "VALUES (?,?,?,?)",
                        (story_id, session["user_id"], content, int(time.time())),
                    )
                    conn.commit()
                    conn.close()

                    commenter      = session["user_id"]
                    commenter_name = session["display_name"]
                    story_title    = s["title"][:50]
                    for assignee in get_story_users(story_id):
                        if assignee["id"] != commenter:
                            create_notification(
                                user_id=assignee["id"],
                                type_="comment",
                                message=(f"{commenter_name} commented on "
                                         f"'{story_title}'"),
                                story_id=story_id,
                                from_user=commenter,
                            )
                    for username in re.findall(r"@(\w+)", content):
                        mentioned = get_user_by_username(username)
                        if mentioned and mentioned["id"] != commenter:
                            create_notification(
                                user_id=mentioned["id"],
                                type_="mention",
                                message=(f"{commenter_name} mentioned you in "
                                         f"'{story_title}'"),
                                story_id=story_id,
                                from_user=commenter,
                            )
                return redirect(url_for("story_view", story_id=story_id))

            if action == "delete":
                if session["role"] not in ("admin", "super_user") and s["created_by"] != session["user_id"]:
                    abort(403)
                project_id = s["project_id"]
                conn = get_db()
                conn.execute("DELETE FROM stories WHERE id=?", (story_id,))
                conn.commit()
                conn.close()
                flash("Story deleted.", "success")
                if is_modal:
                    return jsonify(ok=True, action="delete", story_id=story_id)
                return redirect(url_for("backlog", project_id=project_id))

            if action == "upload_image":
                f = request.files.get("image")
                if f and f.filename and allowed_image(f.filename):
                    ext      = secure_filename(f.filename).rsplit(".", 1)[1].lower()
                    filename = f"{uuid.uuid4().hex}.{ext}"
                    filepath = os.path.join(Config.STORY_IMAGES_FOLDER, filename)
                    img = Image.open(f.stream)
                    img.thumbnail((900, 900), Image.LANCZOS)
                    img.save(filepath, quality=88)
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO story_images (story_id,filename,created_at) "
                        "VALUES (?,?,?)",
                        (story_id, filename, int(time.time())),
                    )
                    conn.commit()
                    conn.close()
                return redirect(url_for("story_view", story_id=story_id))

            if action == "delete_image":
                image_id = request.form.get("image_id", type=int)
                if image_id:
                    conn = get_db()
                    row = conn.execute(
                        "SELECT * FROM story_images WHERE id=? AND story_id=?",
                        (image_id, story_id),
                    ).fetchone()
                    if row:
                        fp = os.path.join(Config.STORY_IMAGES_FOLDER, row["filename"])
                        if os.path.isfile(fp):
                            os.remove(fp)
                        conn.execute("DELETE FROM story_images WHERE id=?",
                                     (image_id,))
                    conn.commit()
                    conn.close()
                return redirect(url_for("story_view", story_id=story_id))

        conn     = get_db()
        comments = conn.execute(
            """SELECT c.*, u.display_name, u.avatar FROM comments c
               JOIN users u ON c.user_id=u.id
               WHERE c.story_id=? ORDER BY c.created_at""",
            (story_id,),
        ).fetchall()
        conn.close()

        assignees    = get_story_users(story_id)
        assignee_ids = [a["id"] for a in assignees]
        statuses     = get_statuses(s["project_id"])
        all_users    = get_all_active_users()
        images       = get_story_images(story_id)
        story_types  = get_story_types(s["project_id"])
        history      = get_story_history(story_id)
        addons       = get_story_addons(story_id, session.get("user_id"))

        return render_template(
            "story.html",
            mode="view",
            story=s,
            project=None,
            project_id=s["project_id"],
            statuses=statuses,
            all_users=all_users,
            story_types=story_types,
            assignees=assignees,
            assignee_ids=assignee_ids,
            comments=comments,
            images=images,
            default_sprint=None,
            history=history,
            addons=addons,
        )

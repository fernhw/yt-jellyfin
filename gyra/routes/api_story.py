"""routes/api_story.py — JSON API for story CRUD, bulk ops, and addon management."""
import os
import time
import uuid

from flask import (abort, jsonify, request,
                   send_from_directory, session, url_for)
from PIL import Image
from werkzeug.utils import secure_filename

from auth import enforce_csrf, login_required
from config import Config
from db import (create_addon, create_notification, delete_addon,
                get_all_active_users, get_db, get_statuses,
                get_story, get_story_addons, get_story_history,
                get_story_images, get_story_thumbnails, get_story_types,
                get_story_users, log_story_change, toggle_addon,
                update_addon_content)
from routes.helpers import (allowed_image, bold_verb_in_title,
                             build_story_title, count_words)


def register(app) -> None:

    # ── Single-story read ──────────────────────────────────────────────────────

    @app.route("/api/story/<int:story_id>/detail")
    @login_required
    def api_story_detail(story_id):
        conn = get_db()
        row  = conn.execute(
            "SELECT * FROM stories WHERE id=?", (story_id,)
        ).fetchone()
        conn.close()
        if not row:
            abort(404)
        return jsonify(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            story_points=row["story_points"] or 0,
            priority=row["priority"] or "",
            status_id=row["status_id"],
            sprint=row["sprint"],
            project_id=row["project_id"],
            story_type=row["story_type"],
            story_actor=row["story_actor"] or "",
            story_verb=row["story_verb"] or "",
            story_z=row["story_z"] or "",
            story_x=row["story_x"] or "",
            story_for=row["story_for"] or "",
            story_y=row["story_y"] or "",
            epic_id=row["epic_id"],
            acceptance_criteria=row["acceptance_criteria"] or "",
        )

    @app.route("/api/story/<int:story_id>/card")
    @login_required
    def api_story_card(story_id):
        """Card-level snapshot — used by the board to refresh a card after an edit."""
        s = get_story(story_id)
        if not s:
            return jsonify(ok=False), 404
        assignees  = get_story_users(story_id)
        html_title = str(bold_verb_in_title(s["title"], s["story_z"] or ""))
        return jsonify(
            ok=True,
            story_id=story_id,
            html_title=html_title,
            story_type_name=s["story_type_name"] or "",
            story_type_color=s["story_type_color"] or "",
            priority=s["priority"] or "",
            story_points=s["story_points"] or 0,
            status_id=s["status_id"],
            assignees=[
                {"id": a["id"], "display_name": a["display_name"],
                 "avatar": a["avatar"]}
                for a in assignees
            ],
        )

    @app.route("/api/story/<int:story_id>/full")
    @login_required
    def api_story_full(story_id):
        s = get_story(story_id)
        if not s:
            return jsonify(ok=False), 404
        assignees   = get_story_users(story_id)
        images      = get_story_images(story_id)
        addons      = get_story_addons(story_id, session["user_id"])
        history     = get_story_history(story_id)
        statuses    = get_statuses(s["project_id"])
        all_users   = get_all_active_users()
        story_types = get_story_types(s["project_id"])
        conn = get_db()
        comments = conn.execute(
            """SELECT c.*, u.display_name, u.avatar FROM comments c
               JOIN users u ON c.user_id = u.id
               WHERE c.story_id = ? ORDER BY c.created_at""",
            (story_id,),
        ).fetchall()
        conn.close()
        return jsonify(
            ok=True,
            story=dict(s),
            html_title=str(bold_verb_in_title(s["title"], s["story_z"] or "")),
            assignees=[dict(a) for a in assignees],
            images=[
                {"id": img["id"],
                 "url": url_for("story_image", filename=img["filename"])}
                for img in images
            ],
            addons=[dict(a) for a in addons],
            comments=[dict(c) for c in comments],
            history=[dict(h) for h in history],
            statuses=[dict(st) for st in statuses],
            all_users=[dict(u) for u in all_users],
            story_types=[dict(t) for t in story_types],
            creator_name=s["creator_name"] or "",
        )

    # ── Story mutation ──────────────────────────────────────────────────────────

    @app.route("/api/story/<int:story_id>/split", methods=["POST"])
    @login_required
    def api_split_story(story_id):
        enforce_csrf()
        data = request.get_json(silent=True) or {}

        conn = get_db()
        orig = conn.execute(
            "SELECT * FROM stories WHERE id=?", (story_id,)
        ).fetchone()
        if not orig:
            conn.close()
            abort(404)

        a_title    = (data.get("a_title") or "").strip() or orig["title"]
        a_desc     = data.get("a_description", orig["description"] or "")
        a_points   = data.get("a_points", orig["story_points"] or 0)
        a_priority = data.get("a_priority", orig["priority"] or None) or None
        a_ac       = data.get("a_acceptance_criteria", orig["acceptance_criteria"] or "")
        conn.execute(
            "UPDATE stories SET title=?,description=?,story_points=?,priority=?,"
            "acceptance_criteria=?,updated_at=? WHERE id=?",
            (a_title, a_desc, int(a_points), a_priority, a_ac, int(time.time()), story_id),
        )

        b_title    = (data.get("b_title") or "").strip() or orig["title"]
        b_desc     = data.get("b_description", orig["description"] or "")
        b_points   = data.get("b_points", orig["story_points"] or 0)
        b_priority = data.get("b_priority", orig["priority"] or None) or None
        b_ac       = data.get("b_acceptance_criteria", orig["acceptance_criteria"] or "")

        order_row = conn.execute(
            "SELECT COALESCE(MAX(order_index),0)+1 AS nxt "
            "FROM stories WHERE project_id=?",
            (orig["project_id"],),
        ).fetchone()
        now = int(time.time())
        cur = conn.execute(
            """INSERT INTO stories
               (project_id,title,description,acceptance_criteria,story_points,
                status_id,sprint,order_index,created_at,created_by,updated_at,
                story_actor,story_verb,story_z,story_x,story_for,story_y,
                story_type,priority,epic_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (orig["project_id"], b_title, b_desc,
             b_ac,
             int(b_points), orig["status_id"], orig["sprint"],
             order_row["nxt"], now, session["user_id"], now,
             orig["story_actor"], orig["story_verb"], orig["story_z"],
             orig["story_x"], orig["story_for"], orig["story_y"],
             orig["story_type"], b_priority, orig["epic_id"]),
        )
        new_id = cur.lastrowid

        for a in conn.execute(
            "SELECT user_id FROM story_users WHERE story_id=?", (story_id,)
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO story_users (story_id,user_id) VALUES (?,?)",
                (new_id, a["user_id"]),
            )

        conn.execute(
            "INSERT INTO story_history "
            "(story_id,user_id,field_name,old_value,new_value,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (story_id, session["user_id"], "Split", "", f"Created #{new_id}", now),
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True, id=new_id)

    @app.route("/api/story/<int:story_id>/move", methods=["POST"])
    @login_required
    def api_move_story(story_id):
        enforce_csrf()
        data      = request.get_json(silent=True) or {}
        status_id = data.get("status_id")
        sprint    = data.get("sprint")
        order     = data.get("order_index", 0)

        conn = get_db()
        old = conn.execute(
            "SELECT status_id, sprint FROM stories WHERE id=?", (story_id,)
        ).fetchone()
        conn.execute(
            "UPDATE stories SET status_id=?,sprint=?,order_index=?,"
            "updated_at=? WHERE id=?",
            (status_id, sprint, order, int(time.time()), story_id),
        )
        if old and old["status_id"] != status_id:
            old_st = conn.execute(
                "SELECT name FROM statuses WHERE id=?", (old["status_id"],)
            ).fetchone()
            new_st = conn.execute(
                "SELECT name FROM statuses WHERE id=?", (status_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO story_history "
                "(story_id,user_id,field_name,old_value,new_value,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (story_id, session["user_id"], "Status",
                 old_st["name"] if old_st else str(old["status_id"]),
                 new_st["name"] if new_st else str(status_id),
                 int(time.time())),
            )
            st_name = new_st["name"] if new_st else str(status_id)
            for a in get_story_users(story_id):
                if a["id"] != session["user_id"]:
                    conn.execute(
                        "INSERT INTO notifications "
                        "(user_id,type,message,story_id,from_user,created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (a["id"], "status",
                         f"{session['display_name']} moved a story to {st_name}",
                         story_id, session["user_id"], int(time.time())),
                    )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/story/<int:story_id>/sprint", methods=["POST"])
    @login_required
    def api_move_to_sprint(story_id):
        enforce_csrf()
        data   = request.get_json(silent=True) or {}
        sprint = data.get("sprint")

        conn = get_db()
        if sprint is not None:
            story = conn.execute(
                "SELECT project_id, status_id FROM stories WHERE id=?",
                (story_id,),
            ).fetchone()
            if story and not story["status_id"]:
                first_status = conn.execute(
                    "SELECT id FROM statuses WHERE project_id=? "
                    "ORDER BY order_index LIMIT 1",
                    (story["project_id"],),
                ).fetchone()
                if first_status:
                    conn.execute(
                        "UPDATE stories SET sprint=?,status_id=?,updated_at=? "
                        "WHERE id=?",
                        (sprint, first_status["id"], int(time.time()), story_id),
                    )
                    conn.commit()
                    conn.close()
                    return jsonify(ok=True)
        conn.execute(
            "UPDATE stories SET sprint=?,updated_at=? WHERE id=?",
            (sprint, int(time.time()), story_id),
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/story/<int:story_id>", methods=["PATCH"])
    @login_required
    def api_update_story(story_id):
        enforce_csrf()
        s = get_story(story_id)
        if not s:
            return jsonify(ok=False), 404
        data     = request.get_json(silent=True) or {}
        actor    = (data.get("story_actor") or "User").strip()
        verb     = (data.get("story_verb") or "needs").strip()
        z        = (data.get("story_z") or "").strip()
        x        = (data.get("story_x") or "").strip()
        for_conn = (data.get("story_for") or "to").strip()
        y        = (data.get("story_y") or "").strip()
        if z and " " in z:
            return jsonify(ok=False,
                           error="The action word must be a single word."), 400
        title      = build_story_title(actor, verb, z, x, for_conn, y)
        word_count = count_words(actor, verb, z, x, for_conn, y)
        if word_count > 19:
            return jsonify(ok=False,
                           error=f"Story is {word_count} words — max 19."), 400
        desc       = (data.get("description") or "").strip()
        ac         = (data.get("acceptance_criteria") or "").strip()
        points     = int(data.get("story_points") or 0)
        status_id  = data.get("status_id") or None
        if status_id:  status_id  = int(status_id)
        sprint     = data.get("sprint") or None
        if sprint:     sprint     = int(sprint)
        assignees  = [int(i) for i in (data.get("assignee_ids") or [])]
        story_type = data.get("story_type") or None
        if story_type: story_type = int(story_type)
        priority   = (data.get("priority") or "").strip() or None

        old = dict(s)
        old_assignee_ids = {a["id"] for a in get_story_users(story_id)}

        conn = get_db()
        conn.execute(
            """UPDATE stories SET title=?,description=?,acceptance_criteria=?,
               story_points=?,status_id=?,sprint=?,updated_at=?,
               story_actor=?,story_verb=?,story_z=?,story_x=?,story_for=?,story_y=?,
               story_type=?,priority=? WHERE id=?""",
            (title, desc, ac, points, status_id, sprint, int(time.time()),
             actor, verb, z, x, for_conn, y, story_type, priority, story_id),
        )
        conn.execute("DELETE FROM story_users WHERE story_id=?", (story_id,))
        for uid in assignees:
            conn.execute(
                "INSERT OR IGNORE INTO story_users (story_id, user_id) VALUES (?,?)",
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
            log_story_change(story_id, uid, "Title", old.get("title"), title)

        new_assignee_ids = set(assignees)
        for newly_assigned_id in (new_assignee_ids - old_assignee_ids):
            if newly_assigned_id != uid:
                create_notification(
                    user_id=newly_assigned_id, type_="assignment",
                    message=(f"{session['display_name']} assigned you to "
                             f"'{s['title'][:50]}'"),
                    story_id=story_id, from_user=uid,
                )

        updated_s         = get_story(story_id)
        updated_assignees = get_story_users(story_id)
        _thumbs           = get_story_thumbnails([story_id])
        return jsonify(
            ok=True,
            story_id=story_id,
            html_title=str(bold_verb_in_title(
                updated_s["title"], updated_s["story_z"] or "")),
            title=updated_s["title"],
            story_type_name=updated_s["story_type_name"] or "",
            story_type_color=updated_s["story_type_color"] or "",
            priority=updated_s["priority"] or "",
            story_points=updated_s["story_points"] or 0,
            status_id=updated_s["status_id"],
            thumbnail=_thumbs.get(story_id),
            assignees=[
                {"id": a["id"], "display_name": a["display_name"],
                 "avatar": a["avatar"]}
                for a in updated_assignees
            ],
        )

    @app.route("/api/story/<int:story_id>", methods=["DELETE"])
    @login_required
    def api_delete_story(story_id):
        enforce_csrf()
        s = get_story(story_id)
        if not s:
            return jsonify(ok=False), 404
        if (session["role"] != "admin"
                and s["created_by"] != session["user_id"]):
            return jsonify(ok=False, error="Not authorized"), 403
        conn = get_db()
        conn.execute("DELETE FROM stories WHERE id=?", (story_id,))
        conn.commit()
        conn.close()
        return jsonify(ok=True, story_id=story_id)

    # ── Images ────────────────────────────────────────────────────────────────

    @app.route("/api/story/<int:story_id>/image", methods=["POST"])
    @login_required
    def api_upload_story_image(story_id):
        enforce_csrf()
        s = get_story(story_id)
        if not s:
            return jsonify(ok=False), 404
        f = request.files.get("image")
        if not f or not f.filename or not allowed_image(f.filename):
            return jsonify(ok=False, error="Invalid file"), 400
        ext      = secure_filename(f.filename).rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(Config.STORY_IMAGES_FOLDER, filename)
        img = Image.open(f.stream)
        img.thumbnail((900, 900), Image.LANCZOS)
        img.save(filepath, quality=88)
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO story_images (story_id, filename, created_at) "
            "VALUES (?,?,?)",
            (story_id, filename, int(time.time())),
        )
        image_id = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify(ok=True, id=image_id,
                       url=url_for("story_image", filename=filename))

    @app.route("/api/story/<int:story_id>/image/<int:image_id>",
               methods=["DELETE"])
    @login_required
    def api_delete_story_image(story_id, image_id):
        enforce_csrf()
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM story_images WHERE id=? AND story_id=?",
            (image_id, story_id),
        ).fetchone()
        if row:
            fp = os.path.join(Config.STORY_IMAGES_FOLDER, row["filename"])
            if os.path.isfile(fp):
                os.remove(fp)
            conn.execute("DELETE FROM story_images WHERE id=?", (image_id,))
            conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/story-images/<filename>")
    @login_required
    def story_image(filename):
        return send_from_directory(Config.STORY_IMAGES_FOLDER,
                                   secure_filename(filename))

    # ── Comments ──────────────────────────────────────────────────────────────

    @app.route("/api/story/<int:story_id>/comment", methods=["POST"])
    @login_required
    def api_create_comment(story_id):
        enforce_csrf()
        s = get_story(story_id)
        if not s:
            return jsonify(ok=False), 404
        data    = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify(ok=False, error="Content required"), 400
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO comments (story_id, user_id, content, created_at) "
            "VALUES (?,?,?,?)",
            (story_id, session["user_id"], content, int(time.time())),
        )
        cid = cur.lastrowid
        conn.commit()
        row = conn.execute(
            """SELECT c.*, u.display_name, u.avatar FROM comments c
               JOIN users u ON c.user_id = u.id WHERE c.id=?""",
            (cid,),
        ).fetchone()
        conn.close()
        commenter = session["user_id"]
        for assignee in get_story_users(story_id):
            if assignee["id"] != commenter:
                create_notification(
                    user_id=assignee["id"], type_="comment",
                    message=(f"{session['display_name']} commented on "
                             f"'{s['title'][:50]}'"),
                    story_id=story_id, from_user=commenter,
                )
        return jsonify(ok=True, comment=dict(row))

    # ── Bulk operations ───────────────────────────────────────────────────────

    @app.route("/api/stories/reorder", methods=["POST"])
    @login_required
    def api_reorder():
        enforce_csrf()
        data  = request.get_json(silent=True) or {}
        items = data.get("items", [])
        if not items:
            return jsonify(ok=True)
        conn = get_db()
        now  = int(time.time())
        for item in items:
            try:
                conn.execute(
                    "UPDATE stories SET order_index=?,subcol_index=?,"
                    "updated_at=? WHERE id=?",
                    (int(item["order_index"]),
                     int(item.get("subcol_index", 0)),
                     now, int(item["id"])),
                )
            except (KeyError, ValueError, TypeError):
                pass
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/stories/bulk-move", methods=["POST"])
    @login_required
    def api_bulk_move():
        enforce_csrf()
        data      = request.get_json(silent=True) or {}
        raw_ids   = data.get("story_ids", [])
        status_id = data.get("status_id")
        sprint    = data.get("sprint")
        if not raw_ids or not status_id:
            return jsonify(ok=False, error="missing params"), 400
        try:
            story_ids = [int(i) for i in raw_ids]
        except (ValueError, TypeError):
            return jsonify(ok=False, error="invalid ids"), 400
        ph   = ",".join("?" * len(story_ids))
        conn = get_db()
        # Only update sprint if explicitly provided — omitting it must NOT null it out
        # (stories with sprint=NULL are hidden from the board entirely)
        if "sprint" in data:
            conn.execute(
                f"UPDATE stories SET status_id=?,sprint=?,updated_at=? "
                f"WHERE id IN ({ph})",
                [int(status_id), sprint, int(time.time())] + story_ids,
            )
        else:
            conn.execute(
                f"UPDATE stories SET status_id=?,updated_at=? "
                f"WHERE id IN ({ph})",
                [int(status_id), int(time.time())] + story_ids,
            )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/stories/bulk-delete", methods=["POST"])
    @login_required
    def api_bulk_delete():
        enforce_csrf()
        data    = request.get_json(silent=True) or {}
        raw_ids = data.get("story_ids", [])
        if not raw_ids:
            return jsonify(ok=False, error="missing ids"), 400
        try:
            story_ids = [int(i) for i in raw_ids]
        except (ValueError, TypeError):
            return jsonify(ok=False, error="invalid ids"), 400
        current_uid = session["user_id"]
        is_admin    = session.get("role") == "admin"
        ph   = ",".join("?" * len(story_ids))
        conn = get_db()
        rows = conn.execute(
            f"SELECT id, created_by FROM stories WHERE id IN ({ph})", story_ids
        ).fetchall()
        allowed = [
            r["id"] for r in rows
            if is_admin or r["created_by"] == current_uid
        ]
        if allowed:
            ph2 = ",".join("?" * len(allowed))
            conn.execute(f"DELETE FROM stories WHERE id IN ({ph2})", allowed)
            conn.commit()
        conn.close()
        return jsonify(ok=True, deleted=allowed)

    @app.route("/api/stories/bulk-assign", methods=["POST"])
    @login_required
    def api_bulk_assign():
        enforce_csrf()
        data    = request.get_json(silent=True) or {}
        raw_ids = data.get("story_ids", [])
        user_id = data.get("user_id")
        action  = data.get("action", "add")
        if not raw_ids or not user_id:
            return jsonify(ok=False, error="missing params"), 400
        try:
            story_ids = [int(i) for i in raw_ids]
            user_id   = int(user_id)
        except (ValueError, TypeError):
            return jsonify(ok=False, error="invalid params"), 400
        conn = get_db()
        if action == "remove":
            ph = ",".join("?" * len(story_ids))
            conn.execute(
                f"DELETE FROM story_users WHERE user_id=? AND story_id IN ({ph})",
                [user_id] + story_ids,
            )
        else:
            for sid in story_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO story_users (story_id,user_id) "
                    "VALUES (?,?)",
                    (sid, user_id),
                )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/stories/bulk-sprint", methods=["POST"])
    @login_required
    def api_bulk_sprint():
        enforce_csrf()
        data    = request.get_json(silent=True) or {}
        raw_ids = data.get("story_ids", [])
        sprint  = data.get("sprint")
        if not raw_ids:
            return jsonify(ok=False, error="missing ids"), 400
        try:
            story_ids = [int(i) for i in raw_ids]
        except (ValueError, TypeError):
            return jsonify(ok=False, error="invalid ids"), 400
        ph   = ",".join("?" * len(story_ids))
        conn = get_db()
        if sprint is not None:
            for sid in story_ids:
                row = conn.execute(
                    "SELECT project_id, status_id FROM stories WHERE id=?",
                    (sid,),
                ).fetchone()
                if row and not row["status_id"]:
                    first = conn.execute(
                        "SELECT id FROM statuses WHERE project_id=? "
                        "ORDER BY order_index LIMIT 1",
                        (row["project_id"],),
                    ).fetchone()
                    if first:
                        conn.execute(
                            "UPDATE stories SET status_id=? WHERE id=?",
                            (first["id"], sid),
                        )
        conn.execute(
            f"UPDATE stories SET sprint=?,updated_at=? WHERE id IN ({ph})",
            [sprint, int(time.time())] + story_ids,
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/stories/bulk-type", methods=["POST"])
    @login_required
    def api_bulk_type():
        enforce_csrf()
        data    = request.get_json(silent=True) or {}
        raw_ids = data.get("story_ids", [])
        type_id = data.get("story_type")
        if not raw_ids:
            return jsonify(ok=False, error="missing ids"), 400
        try:
            story_ids = [int(i) for i in raw_ids]
        except (ValueError, TypeError):
            return jsonify(ok=False, error="invalid ids"), 400
        if type_id is not None:
            try:
                type_id = int(type_id)
            except (ValueError, TypeError):
                return jsonify(ok=False, error="invalid type_id"), 400
        ph   = ",".join("?" * len(story_ids))
        conn = get_db()
        conn.execute(
            f"UPDATE stories SET story_type=?,updated_at=? WHERE id IN ({ph})",
            [type_id, int(time.time())] + story_ids,
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    # ── Addons (mini-waterfall tasks) ─────────────────────────────────────────

    @app.route("/api/story/<int:story_id>/addons", methods=["GET"])
    @login_required
    def api_get_addons(story_id):
        addons = get_story_addons(story_id, session["user_id"])
        return jsonify([dict(a) for a in addons])

    @app.route("/api/story/<int:story_id>/addons", methods=["POST"])
    @login_required
    def api_create_addon(story_id):
        enforce_csrf()
        data     = request.get_json(silent=True) or {}
        content  = (data.get("content") or "").strip()
        if not content:
            return jsonify(error="content required"), 400
        assigned = data.get("assigned_user_id") or None
        addon_id = create_addon(story_id, content, assigned, session["user_id"])
        return jsonify(id=addon_id, ok=True)

    @app.route("/api/addon/<int:addon_id>", methods=["PATCH"])
    @login_required
    def api_update_addon(addon_id):
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        if "is_done" in data:
            toggle_addon(addon_id, session["user_id"], int(bool(data["is_done"])))
            return jsonify(ok=True)
        update_addon_content(
            addon_id,
            content=data.get("content"),
            assigned_user_id=data.get("assigned_user_id"),
            order_index=data.get("order_index"),
        )
        return jsonify(ok=True)

    @app.route("/api/addon/<int:addon_id>", methods=["DELETE"])
    @login_required
    def api_delete_addon(addon_id):
        enforce_csrf()
        delete_addon(addon_id)
        return jsonify(ok=True)

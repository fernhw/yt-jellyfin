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
                get_all_active_users, get_db, get_epics, get_statuses,
                get_story, get_story_addons, get_story_history,
                get_story_images, get_story_thumbnails, get_story_previews, get_story_types,
                get_story_users, log_story_change, toggle_addon,
                update_addon_content, user_in_project)
from routes.helpers import (allowed_image, bold_verb_in_title,
                             build_story_title, count_words)
from routes.api_container import container_payload


def register(app) -> None:

    def _cascade_container_status(conn, story_id, old_status_id, new_status_id, actor_id, actor_name):
        """When a CONTAINER story's done-state flips, notify assignees of all
        attached stories that they (un)became Integrate stories."""
        if old_status_id == new_status_id:
            return
        row = conn.execute(
            "SELECT box_type, title FROM stories WHERE id=?", (story_id,)
        ).fetchone()
        if not row or not row["box_type"]:
            return  # not a container
        def _is_done(sid):
            if not sid: return False
            r = conn.execute("SELECT is_done FROM statuses WHERE id=?", (sid,)).fetchone()
            return bool(r and r["is_done"])
        was_done = _is_done(old_status_id)
        now_done = _is_done(new_status_id)
        if was_done == now_done:
            return
        attached = conn.execute(
            "SELECT id, title FROM stories WHERE attached_to=?", (story_id,)
        ).fetchall()
        if not attached:
            return
        ts = int(time.time())
        for att in attached:
            assignees = conn.execute(
                "SELECT user_id FROM story_users WHERE story_id=?", (att["id"],)
            ).fetchall()
            for a in assignees:
                if a["user_id"] == actor_id:
                    continue
                if now_done:
                    msg = (f"{actor_name} closed container '{row['title'][:40]}' — "
                           f"your story '{att['title'][:40]}' is now an Integrate task")
                    ntype = "integrate_ready"
                else:
                    msg = (f"{actor_name} reopened container '{row['title'][:40]}' — "
                           f"your story '{att['title'][:40]}' is no longer Integrate")
                    ntype = "integrate_undone"
                conn.execute(
                    "INSERT INTO notifications "
                    "(user_id,type,message,story_id,from_user,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (a["user_id"], ntype, msg, att["id"], actor_id, ts),
                )

    # ── B1: project-membership guard for every per-story endpoint ────────────
    def _require_story_access(story_id):
        """Return the story row if the current session user may access it,
        else None.  Admins always pass; everyone else must be a member of the
        story's project.  Callers should return 404 on None to avoid leaking
        story-id existence to unauthorised users."""
        s = get_story(story_id)
        if not s:
            return None
        if session.get("role") in ("admin", "super_user"):
            return s
        uid = session.get("user_id")
        if uid and user_in_project(uid, s["project_id"]):
            return s
        return None

    def _addon_story_id(addon_id):
        """Resolve the story_id that owns an addon (for B1 access checks)."""
        conn = get_db()
        row  = conn.execute(
            "SELECT story_id FROM story_addons WHERE id=?", (addon_id,)
        ).fetchone()
        conn.close()
        return row["story_id"] if row else None

    # ── Story templates (catalogue used by the New-Story screen) ───────────

    @app.route("/api/story-templates")
    @login_required
    def api_story_templates():
        from story_templates import TEMPLATES
        # Return public-safe copy. Strip nothing — these are static, no PII.
        return jsonify(templates=TEMPLATES)

    # ── Single-story read ──────────────────────────────────────────────────────

    @app.route("/api/story/<storyref:story_id>/detail")
    @login_required
    def api_story_detail(story_id):
        if not _require_story_access(story_id):
            abort(404)
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

    @app.route("/api/story/<storyref:story_id>/card")
    @login_required
    def api_story_card(story_id):
        """Card-level snapshot — used by the board to refresh a card after an edit."""
        s = _require_story_access(story_id)
        if not s:
            return jsonify(ok=False), 404
        assignees  = get_story_users(story_id)
        _previews  = get_story_previews([story_id])
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
            images=_previews.get(story_id, []),
            assignees=[
                {"id": a["id"], "display_name": a["display_name"],
                 "avatar": a["avatar"]}
                for a in assignees
            ],
        )

    @app.route("/api/story/<storyref:story_id>/full")
    @login_required
    def api_story_full(story_id):
        s = _require_story_access(story_id)
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
                 "url":       url_for("story_image", filename=img["filename"]),
                 "med_url":   url_for("story_image", filename="med_" + img["filename"]),
                 "thumb_url": url_for("story_image", filename="thumb_" + img["filename"])}
                for img in images
            ],
            addons=[dict(a) for a in addons],
            comments=[dict(c) for c in comments],
            history=[dict(h) for h in history],
            statuses=[dict(st) for st in statuses],
            all_users=[dict(u) for u in all_users],
            story_types=[dict(t) for t in story_types],
            epics=[dict(e) for e in get_epics(s["project_id"])],
            creator_name=s["creator_name"] or "",
            container=container_payload(s),
        )

    # ── Story mutation ──────────────────────────────────────────────────────────

    @app.route("/api/story/<storyref:story_id>/split", methods=["POST"])
    @login_required
    def api_split_story(story_id):
        enforce_csrf()
        if not _require_story_access(story_id):
            abort(404)
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
        snum_row = conn.execute(
            "SELECT COALESCE(MAX(story_number),0)+1 AS nxt "
            "FROM stories WHERE project_id=?",
            (orig["project_id"],),
        ).fetchone()
        now = int(time.time())
        cur = conn.execute(
            """INSERT INTO stories
               (project_id,title,description,acceptance_criteria,story_points,
                status_id,sprint,order_index,created_at,created_by,updated_at,
                story_actor,story_verb,story_z,story_x,story_for,story_y,
                story_type,priority,epic_id,story_number)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (orig["project_id"], b_title, b_desc,
             b_ac,
             int(b_points), orig["status_id"], orig["sprint"],
             order_row["nxt"], now, session["user_id"], now,
             orig["story_actor"], orig["story_verb"], orig["story_z"],
             orig["story_x"], orig["story_for"], orig["story_y"],
             orig["story_type"], b_priority, orig["epic_id"], snum_row["nxt"]),
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

    @app.route("/api/story/<storyref:story_id>/move", methods=["POST"])
    @login_required
    def api_move_story(story_id):
        enforce_csrf()
        if not _require_story_access(story_id):
            return jsonify(ok=False), 404
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
            _cascade_container_status(
                conn, story_id, old["status_id"], status_id,
                session["user_id"], session["display_name"],
            )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/story/<storyref:story_id>/sprint", methods=["POST"])
    @login_required
    def api_move_to_sprint(story_id):
        enforce_csrf()
        if not _require_story_access(story_id):
            return jsonify(ok=False), 404
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

    @app.route("/api/story/<storyref:story_id>", methods=["PATCH"])
    @login_required
    def api_update_story(story_id):
        enforce_csrf()
        s = _require_story_access(story_id)
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
        if "epic_id" in data:
            ep_raw = data.get("epic_id")
            epic_id = int(ep_raw) if ep_raw not in (None, "", 0, "0") else None
            epic_id_provided = True
        else:
            epic_id = s["epic_id"] if "epic_id" in s.keys() else None
            epic_id_provided = False

        old = dict(s)
        old_assignee_ids = {a["id"] for a in get_story_users(story_id)}

        conn = get_db()
        conn.execute(
            """UPDATE stories SET title=?,description=?,acceptance_criteria=?,
               story_points=?,status_id=?,sprint=?,updated_at=?,
               story_actor=?,story_verb=?,story_z=?,story_x=?,story_for=?,story_y=?,
               story_type=?,priority=?,epic_id=? WHERE id=?""",
            (title, desc, ac, points, status_id, sprint, int(time.time()),
             actor, verb, z, x, for_conn, y, story_type, priority, epic_id, story_id),
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
        # Cascade integrate notifications if this is a container that flipped done-state
        if old.get("status_id") != status_id:
            _cascade_conn = get_db()
            _cascade_container_status(
                _cascade_conn, story_id, old.get("status_id"), status_id,
                uid, session["display_name"],
            )
            _cascade_conn.commit()
            _cascade_conn.close()
        log_story_change(story_id, uid, "Sprint",
                         old.get("sprint"), sprint)
        log_story_change(story_id, uid, "Priority",
                         old.get("priority"), priority)
        log_story_change(story_id, uid, "Points",
                         old.get("story_points"), points)
        log_story_change(story_id, uid, "Description",
                         old.get("description"), desc)
        log_story_change(story_id, uid, "Acceptance Criteria",
                         old.get("acceptance_criteria"), ac)
        if old.get("story_type") != story_type:
            log_story_change(story_id, uid, "Type",
                             old.get("story_type"), story_type)
        if epic_id_provided:
            log_story_change(story_id, uid, "Epic",
                             old.get("epic_id"), epic_id)
        if old.get("title") != title:
            log_story_change(story_id, uid, "Title", old.get("title"), title)

        # Assignee deltas — one history row per add/remove, named.
        new_assignee_ids = set(assignees)
        added_ids   = new_assignee_ids - old_assignee_ids
        removed_ids = old_assignee_ids - new_assignee_ids
        if added_ids or removed_ids:
            name_conn = get_db()
            id_to_name = {}
            for r in name_conn.execute(
                "SELECT id, display_name FROM users"
            ).fetchall():
                id_to_name[r["id"]] = r["display_name"]
            name_conn.close()
            for aid in added_ids:
                log_story_change(story_id, uid, "Assigned",
                                 None, id_to_name.get(aid, f"#{aid}"))
            for aid in removed_ids:
                log_story_change(story_id, uid, "Unassigned",
                                 id_to_name.get(aid, f"#{aid}"), None)

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
        _previews         = get_story_previews([story_id])
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
            images=_previews.get(story_id, []),
            assignees=[
                {"id": a["id"], "display_name": a["display_name"],
                 "avatar": a["avatar"]}
                for a in updated_assignees
            ],
        )

    # ── Quick-update: single-field patch for the right-click context menu ──
    @app.route("/api/story/<storyref:story_id>/quick-update", methods=["POST"])
    @login_required
    def api_quick_update_story(story_id):
        """Apply a single targeted mutation to a story.

        Body: {"op": "<operation>", "value": <op-specific>}

        Supported ops:
          set_priority      value=str|null
          set_points        value=int (0 = clear)
          set_type          value=int|null
          set_epic          value=int|null
          set_status        value=int        (also moves card, logs Status)
          set_sprint        value=int|null   (null = remove from sprint)
          add_assignee      value=int (user id)
          remove_assignee   value=int (user id)
          set_addon_done    value={"addon_id": int, "is_done": bool}
        """
        enforce_csrf()
        s = _require_story_access(story_id)
        if not s:
            return jsonify(ok=False), 404
        data  = request.get_json(silent=True) or {}
        op    = (data.get("op") or "").strip()
        value = data.get("value")
        uid   = session["user_id"]
        conn  = get_db()

        def _name(user_id):
            r = conn.execute(
                "SELECT display_name FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return r["display_name"] if r else f"#{user_id}"

        try:
            if op == "set_priority":
                new = (value or "").strip() or None
                old = s["priority"]
                conn.execute(
                    "UPDATE stories SET priority=?, updated_at=? WHERE id=?",
                    (new, int(time.time()), story_id),
                )
                conn.commit()
                log_story_change(story_id, uid, "Priority", old, new)
                return jsonify(ok=True, priority=new or "")

            if op == "set_points":
                new = int(value or 0)
                old = s["story_points"]
                conn.execute(
                    "UPDATE stories SET story_points=?, updated_at=? WHERE id=?",
                    (new, int(time.time()), story_id),
                )
                conn.commit()
                log_story_change(story_id, uid, "Points", old, new)
                return jsonify(ok=True, story_points=new)

            if op == "set_type":
                new = int(value) if value not in (None, "", 0, "0") else None
                old = s["story_type"] if "story_type" in s.keys() else None
                conn.execute(
                    "UPDATE stories SET story_type=?, updated_at=? WHERE id=?",
                    (new, int(time.time()), story_id),
                )
                conn.commit()
                if old != new:
                    log_story_change(story_id, uid, "Type", old, new)
                return jsonify(ok=True, story_type=new)

            if op == "set_epic":
                new = int(value) if value not in (None, "", 0, "0") else None
                old = s["epic_id"] if "epic_id" in s.keys() else None
                conn.execute(
                    "UPDATE stories SET epic_id=?, updated_at=? WHERE id=?",
                    (new, int(time.time()), story_id),
                )
                conn.commit()
                if old != new:
                    log_story_change(story_id, uid, "Epic", old, new)
                return jsonify(ok=True, epic_id=new)

            if op == "set_status":
                new = int(value)
                old = s["status_id"]
                conn.execute(
                    "UPDATE stories SET status_id=?, updated_at=? WHERE id=?",
                    (new, int(time.time()), story_id),
                )
                if old != new:
                    old_st = conn.execute(
                        "SELECT name FROM statuses WHERE id=?", (old,)
                    ).fetchone()
                    new_st = conn.execute(
                        "SELECT name FROM statuses WHERE id=?", (new,)
                    ).fetchone()
                    conn.execute(
                        "INSERT INTO story_history "
                        "(story_id,user_id,field_name,old_value,new_value,created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (story_id, uid, "Status",
                         old_st["name"] if old_st else str(old),
                         new_st["name"] if new_st else str(new),
                         int(time.time())),
                    )
                conn.commit()
                return jsonify(ok=True, status_id=new)

            if op == "set_sprint":
                new = int(value) if value not in (None, "", 0, "0") else None
                old = s["sprint"]
                conn.execute(
                    "UPDATE stories SET sprint=?, updated_at=? WHERE id=?",
                    (new, int(time.time()), story_id),
                )
                conn.commit()
                if old != new:
                    log_story_change(story_id, uid, "Sprint", old, new)
                return jsonify(ok=True, sprint=new)

            if op == "add_assignee":
                aid = int(value)
                conn.execute(
                    "INSERT OR IGNORE INTO story_users (story_id, user_id) "
                    "VALUES (?, ?)", (story_id, aid),
                )
                conn.commit()
                log_story_change(story_id, uid, "Assigned", None, _name(aid))
                if aid != uid:
                    create_notification(
                        user_id=aid, type_="assignment",
                        message=(f"{session['display_name']} assigned you to "
                                 f"'{s['title'][:50]}'"),
                        story_id=story_id, from_user=uid,
                    )
                assignees = get_story_users(story_id)
                return jsonify(ok=True, assignees=[
                    {"id": a["id"], "display_name": a["display_name"],
                     "avatar": a["avatar"]} for a in assignees])

            if op == "remove_assignee":
                aid = int(value)
                conn.execute(
                    "DELETE FROM story_users WHERE story_id=? AND user_id=?",
                    (story_id, aid),
                )
                conn.commit()
                log_story_change(story_id, uid, "Unassigned", _name(aid), None)
                assignees = get_story_users(story_id)
                return jsonify(ok=True, assignees=[
                    {"id": a["id"], "display_name": a["display_name"],
                     "avatar": a["avatar"]} for a in assignees])

            if op == "set_addon_done":
                v = value or {}
                aid = int(v.get("addon_id"))
                want_done = 1 if v.get("is_done") else 0
                # Confirm addon belongs to this story
                row = conn.execute(
                    "SELECT content FROM story_addons WHERE id=? AND story_id=?",
                    (aid, story_id),
                ).fetchone()
                if not row:
                    return jsonify(ok=False, error="addon not found"), 404
                toggle_addon(aid, uid, want_done)
                label = "Subtask done" if want_done else "Subtask reopened"
                if want_done:
                    log_story_change(story_id, uid, label, None, row["content"])
                else:
                    log_story_change(story_id, uid, label, row["content"], None)
                return jsonify(ok=True)

            return jsonify(ok=False, error=f"unknown op: {op}"), 400
        finally:
            conn.close()

    @app.route("/api/story/<storyref:story_id>", methods=["DELETE"])
    @login_required
    def api_delete_story(story_id):
        enforce_csrf()
        s = _require_story_access(story_id)
        if not s:
            return jsonify(ok=False), 404
        if (session["role"] not in ("admin", "super_user")
                and s["created_by"] != session["user_id"]):
            return jsonify(ok=False, error="Not authorized"), 403
        conn = get_db()
        conn.execute("DELETE FROM stories WHERE id=?", (story_id,))
        conn.commit()
        conn.close()
        return jsonify(ok=True, story_id=story_id)

    # ── Images ────────────────────────────────────────────────────────────────

    @app.route("/api/story/<storyref:story_id>/image", methods=["POST"])
    @login_required
    def api_upload_story_image(story_id):
        enforce_csrf()
        s = _require_story_access(story_id)
        if not s:
            return jsonify(ok=False), 404
        f = request.files.get("image")
        if not f or not f.filename or not allowed_image(f.filename):
            return jsonify(ok=False, error="Invalid file"), 400
        ext      = secure_filename(f.filename).rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        folder   = Config.STORY_IMAGES_FOLDER
        img = Image.open(f.stream)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        # Original (lightbox)
        orig = img.copy()
        orig.thumbnail((900, 900), Image.LANCZOS)
        orig.save(os.path.join(folder, filename), quality=88)
        # Medium (story grid)
        med = img.copy()
        med.thumbnail((700, 700), Image.LANCZOS)
        med.save(os.path.join(folder, "med_" + filename), quality=82)
        # Thumbnail (post-it card)
        thumb = img.copy()
        thumb.thumbnail((200, 200), Image.LANCZOS)
        thumb.save(os.path.join(folder, "thumb_" + filename), quality=75)
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
                       url=url_for("story_image", filename=filename),
                       med_url=url_for("story_image", filename="med_" + filename),
                       thumb_url=url_for("story_image", filename="thumb_" + filename))

    @app.route("/api/story/<storyref:story_id>/image/<int:image_id>",
               methods=["DELETE"])
    @login_required
    def api_delete_story_image(story_id, image_id):
        enforce_csrf()
        if not _require_story_access(story_id):
            return jsonify(ok=False), 404
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM story_images WHERE id=? AND story_id=?",
            (image_id, story_id),
        ).fetchone()
        if row:
            folder = Config.STORY_IMAGES_FOLDER
            for prefix in ("", "med_", "thumb_"):
                fp = os.path.join(folder, prefix + row["filename"])
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

    @app.route("/api/story/<storyref:story_id>/comment", methods=["POST"])
    @login_required
    def api_create_comment(story_id):
        enforce_csrf()
        s = _require_story_access(story_id)
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
        # Snapshot old status per story so we can detect container done-state flips
        old_rows = conn.execute(
            f"SELECT id, status_id FROM stories WHERE id IN ({ph})", story_ids
        ).fetchall()
        old_status_map = {r["id"]: r["status_id"] for r in old_rows}
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
        # Cascade integrate notifications for any container that flipped done-state
        for sid in story_ids:
            _cascade_container_status(
                conn, sid, old_status_map.get(sid), int(status_id),
                session["user_id"], session["display_name"],
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

    @app.route("/api/story/<storyref:story_id>/addons", methods=["GET"])
    @login_required
    def api_get_addons(story_id):
        if not _require_story_access(story_id):
            return jsonify([]), 404
        addons = get_story_addons(story_id, session["user_id"])
        return jsonify([dict(a) for a in addons])

    @app.route("/api/story/<storyref:story_id>/addons", methods=["POST"])
    @login_required
    def api_create_addon(story_id):
        enforce_csrf()
        if not _require_story_access(story_id):
            return jsonify(ok=False), 404
        data     = request.get_json(silent=True) or {}
        content  = (data.get("content") or "").strip()
        if not content:
            return jsonify(error="content required"), 400
        assigned = data.get("assigned_user_id") or None
        addon_id = create_addon(story_id, content, assigned, session["user_id"])
        log_story_change(story_id, session["user_id"], "Subtask added",
                         None, content)
        return jsonify(id=addon_id, ok=True)

    @app.route("/api/addon/<int:addon_id>", methods=["PATCH"])
    @login_required
    def api_update_addon(addon_id):
        enforce_csrf()
        sid = _addon_story_id(addon_id)
        if not sid or not _require_story_access(sid):
            return jsonify(ok=False), 404
        data = request.get_json(silent=True) or {}
        # Snapshot current addon (content + assignee) for history diff.
        snap_conn = get_db()
        snap = snap_conn.execute(
            "SELECT content, assigned_user_id FROM story_addons WHERE id=?",
            (addon_id,),
        ).fetchone()
        snap_conn.close()
        uid = session["user_id"]
        if "is_done" in data:
            new_done = int(bool(data["is_done"]))
            toggle_addon(addon_id, uid, new_done)
            label = "Subtask done" if new_done else "Subtask reopened"
            log_story_change(sid, uid, label,
                             None if new_done else (snap["content"] if snap else None),
                             (snap["content"] if snap else None) if new_done else None)
            return jsonify(ok=True)
        update_addon_content(
            addon_id,
            content=data.get("content"),
            assigned_user_id=data.get("assigned_user_id"),
            order_index=data.get("order_index"),
        )
        # Log content change
        if "content" in data and snap is not None:
            new_content = (data.get("content") or "").strip()
            if new_content and new_content != (snap["content"] or ""):
                log_story_change(sid, uid, "Subtask edited",
                                 snap["content"], new_content)
        # Log assignee change
        if "assigned_user_id" in data and snap is not None:
            new_aid = data.get("assigned_user_id") or None
            old_aid = snap["assigned_user_id"]
            if (new_aid or None) != (old_aid or None):
                nm_conn = get_db()
                id_to_name = {}
                for r in nm_conn.execute(
                    "SELECT id, display_name FROM users"
                ).fetchall():
                    id_to_name[r["id"]] = r["display_name"]
                nm_conn.close()
                tail = f" — {snap['content']}" if snap["content"] else ""
                log_story_change(
                    sid, uid, "Subtask assignee",
                    (id_to_name.get(old_aid, f"#{old_aid}") + tail) if old_aid else tail.lstrip(" — ") or None,
                    (id_to_name.get(new_aid, f"#{new_aid}") + tail) if new_aid else tail.lstrip(" — ") or None,
                )
        return jsonify(ok=True)

    @app.route("/api/addon/<int:addon_id>", methods=["DELETE"])
    @login_required
    def api_delete_addon(addon_id):
        enforce_csrf()
        sid = _addon_story_id(addon_id)
        if not sid or not _require_story_access(sid):
            return jsonify(ok=False), 404
        # Capture content for history before delete.
        snap_conn = get_db()
        snap = snap_conn.execute(
            "SELECT content FROM story_addons WHERE id=?", (addon_id,),
        ).fetchone()
        snap_conn.close()
        delete_addon(addon_id)
        if snap:
            log_story_change(sid, session["user_id"], "Subtask removed",
                             snap["content"], None)
        return jsonify(ok=True)

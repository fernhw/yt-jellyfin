"""routes/api_project.py — Project-level JSON API: statuses, board state, stickers, epics."""
import time

from flask import abort, jsonify, request, session

from auth import admin_required, enforce_csrf, login_required
from db import (create_epic, delete_epic, get_db, get_epics, get_statuses,
                get_stickers, get_story_thumbnails)
from routes.helpers import bold_verb_in_title


def register(app) -> None:

    # ── Statuses ──────────────────────────────────────────────────────────────

    @app.route("/api/statuses/<int:project_id>")
    @login_required
    def api_statuses(project_id):
        return jsonify([dict(s) for s in get_statuses(project_id)])

    # ── Board state ───────────────────────────────────────────────────────────

    @app.route("/api/project/<int:project_id>/board-full-state")
    @login_required
    def api_board_full_state(project_id):
        """Return the complete, authoritative board state for real-time sync."""
        conn = get_db()
        stories = conn.execute(
            """SELECT s.id, s.status_id, s.order_index, s.subcol_index,
                      s.title, s.description, s.acceptance_criteria, s.story_z, s.priority, s.story_points, s.updated_at,
                      sty.name  AS story_type_name,
                      sty.color AS story_type_color,
                      (SELECT si.filename FROM story_images si
                       WHERE si.story_id = s.id ORDER BY si.id LIMIT 1) AS thumbnail
               FROM stories s
               LEFT JOIN story_types sty ON s.story_type = sty.id
               WHERE s.project_id=? AND s.sprint IS NOT NULL
               ORDER BY s.order_index""",
            (project_id,),
        ).fetchall()
        stickers = conn.execute(
            """SELECT s.id, s.type, s.x, s.y, s.label,
                      s.card_story_id, s.card_x, s.card_y,
                      u.display_name AS creator_name
               FROM stickers s
               LEFT JOIN users u ON u.id = s.created_by
               WHERE s.project_id=?""",
            (project_id,),
        ).fetchall()
        story_ids = [r["id"] for r in stories]
        assignees_by_story: dict = {}
        addons_by_story: dict = {}
        if story_ids:
            ph = ",".join("?" * len(story_ids))
            for a in conn.execute(
                f"SELECT su.story_id, u.display_name, u.avatar FROM story_users su"
                f" JOIN users u ON su.user_id=u.id WHERE su.story_id IN ({ph})",
                story_ids,
            ).fetchall():
                assignees_by_story.setdefault(a["story_id"], []).append(
                    {"display_name": a["display_name"], "avatar": a["avatar"]}
                )
            tasks_by_story: dict = {}
            for ad in conn.execute(
                f"SELECT sa.story_id, sa.id AS addon_id, sa.content,"
                f" u.display_name AS assigned_name,"
                f" (SELECT COUNT(*) FROM addon_statuses aus"
                f"  WHERE aus.addon_id = sa.id AND aus.is_done = 1) AS done_count"
                f" FROM story_addons sa"
                f" LEFT JOIN users u ON sa.assigned_user_id = u.id"
                f" WHERE sa.story_id IN ({ph}) ORDER BY sa.order_index, sa.id",
                story_ids,
            ).fetchall():
                addons_by_story.setdefault(ad["story_id"], []).append(ad["content"])
                tasks_by_story.setdefault(ad["story_id"], []).append(
                    {"c": ad["content"], "a": ad["assigned_name"] or "", "done": ad["done_count"] > 0}
                )
        conn.close()
        result = []
        for r in stories:
            d = dict(r)
            d["html_title"] = str(bold_verb_in_title(
                r["title"], r["story_z"] or ""))
            d["assignees"]  = assignees_by_story.get(r["id"], [])
            d["addons"]     = addons_by_story.get(r["id"], [])
            d["tasks"]      = tasks_by_story.get(r["id"], [])
            result.append(d)
        return jsonify(
            stories=result,
            stickers=[dict(r) for r in stickers],
        )

    @app.route("/api/project/<int:project_id>/board-snapshot")
    @login_required
    def api_board_snapshot(project_id):
        since = request.args.get("since", 0, type=int)
        conn  = get_db()
        rows  = conn.execute(
            """SELECT id, status_id, sprint, order_index, updated_at
               FROM stories
               WHERE project_id=? AND sprint IS NOT NULL AND updated_at > ?
               ORDER BY order_index""",
            (project_id, since),
        ).fetchall()
        conn.close()
        return jsonify(stories=[dict(r) for r in rows])

    # ── Epics ─────────────────────────────────────────────────────────────────

    @app.route("/api/project/<int:project_id>/epics", methods=["GET"])
    @login_required
    def api_get_epics(project_id):
        return jsonify([dict(e) for e in get_epics(project_id)])

    @app.route("/api/project/<int:project_id>/epics", methods=["POST"])
    @login_required
    def api_create_epic(project_id):
        enforce_csrf()
        data  = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify(error="title required"), 400
        color   = data.get("color") or "#6B7280"
        desc    = (data.get("description") or "").strip()
        epic_id = create_epic(project_id, title, color, desc, session["user_id"])
        return jsonify(id=epic_id, ok=True)

    @app.route("/api/epic/<int:epic_id>", methods=["DELETE"])
    @admin_required
    def api_delete_epic(epic_id):
        enforce_csrf()
        delete_epic(epic_id)
        return jsonify(ok=True)

    # ── Stickers ──────────────────────────────────────────────────────────────

    @app.route("/api/stickers/<int:project_id>")
    @app.route("/api/stickers/<int:project_id>/<sprint>")
    @login_required
    def api_get_stickers(project_id, sprint=None):
        return jsonify([dict(s) for s in get_stickers(project_id)])

    @app.route("/api/stickers", methods=["POST"])
    @login_required
    def api_create_sticker():
        enforce_csrf()
        data       = request.get_json(silent=True) or {}
        project_id = data.get("project_id")
        sprint     = data.get("sprint")
        stype      = data.get("type", "").strip()
        x          = float(data.get("x", 0))
        y          = float(data.get("y", 0))
        rotation   = float(data.get("rotation", 0))
        label      = data.get("label", "")

        if not project_id or not stype:
            abort(400)

        if stype == "star" and session.get("role") not in ("admin", "super_user"):
            abort(403)

        conn = get_db()
        cur  = conn.execute(
            """INSERT INTO stickers
               (project_id,sprint,type,x,y,rotation,label,created_by,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (project_id, sprint, stype, x, y, rotation,
             label, session["user_id"], int(time.time())),
        )
        sticker_id = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify(
            id=sticker_id, ok=True,
            creator_name=session.get("display_name", ""),
        )

    @app.route("/api/stickers/<int:sticker_id>/label", methods=["PATCH"])
    @login_required
    def api_update_sticker_label(sticker_id):
        enforce_csrf()
        data  = request.get_json(silent=True) or {}
        label = data.get("label", "") or ""
        conn  = get_db()
        conn.execute("UPDATE stickers SET label=? WHERE id=?", (label, sticker_id))
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/stickers/<int:sticker_id>", methods=["PATCH"])
    @login_required
    def api_update_sticker(sticker_id):
        enforce_csrf()
        data          = request.get_json(silent=True) or {}
        card_story_id = data.get("card_story_id")

        conn = get_db()
        if card_story_id is not None:
            card_x = float(data.get("card_x", 0))
            card_y = float(data.get("card_y", 0))
            conn.execute(
                "UPDATE stickers SET card_story_id=?,card_x=?,card_y=? WHERE id=?",
                (int(card_story_id), card_x, card_y, sticker_id),
            )
        else:
            x        = float(data.get("x", 0))
            y        = float(data.get("y", 0))
            rotation = float(data.get("rotation", 0))
            conn.execute(
                "UPDATE stickers SET x=?,y=?,rotation=?,"
                "card_story_id=NULL,card_x=NULL,card_y=NULL WHERE id=?",
                (x, y, rotation, sticker_id),
            )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/stickers/<int:sticker_id>", methods=["DELETE"])
    @login_required
    def api_delete_sticker(sticker_id):
        enforce_csrf()
        conn = get_db()
        conn.execute("DELETE FROM stickers WHERE id=?", (sticker_id,))
        conn.commit()
        conn.close()
        return jsonify(ok=True)

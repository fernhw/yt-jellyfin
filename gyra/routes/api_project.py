"""routes/api_project.py — Project-level JSON API: statuses, board state, stickers, epics."""
import time

from flask import abort, jsonify, request, session

from auth import admin_required, enforce_csrf, login_required, super_user_required
from db import (create_epic, delete_epic, get_db, get_epic,
                get_epic_stories_full, get_epics, get_statuses,
                get_stickers, get_story_thumbnails, get_story_previews,
                move_epic, reorder_epics,
                update_epic)
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
                      s.epic_id,
                      s.box_type, s.attached_to,
                      COALESCE(st.is_done,0)  AS status_is_done,
                      COALESCE(pst.is_done,0) AS parent_is_done,
                      p.box_type              AS parent_box_type,
                      sty.name  AS story_type_name,
                      sty.color AS story_type_color
               FROM stories s
               LEFT JOIN story_types sty ON s.story_type = sty.id
               LEFT JOIN statuses    st  ON s.status_id   = st.id
               LEFT JOIN stories     p   ON s.attached_to = p.id
               LEFT JOIN statuses    pst ON p.status_id   = pst.id
               WHERE s.project_id=? AND s.sprint IS NOT NULL
                 AND COALESCE(s.is_archived,0)=0
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
        previews: dict = get_story_previews(story_ids) if story_ids else {}
        assignees_by_story: dict = {}
        addons_by_story: dict = {}
        if story_ids:
            ph = ",".join("?" * len(story_ids))
            for a in conn.execute(
                f"SELECT su.story_id, u.id AS user_id, u.display_name, u.avatar FROM story_users su"
                f" JOIN users u ON su.user_id=u.id WHERE su.story_id IN ({ph})",
                story_ids,
            ).fetchall():
                assignees_by_story.setdefault(a["story_id"], []).append(
                    {"id": a["user_id"], "display_name": a["display_name"], "avatar": a["avatar"]}
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
            d["images"]     = previews.get(r["id"], [])
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
                 AND COALESCE(is_archived,0)=0
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
    @super_user_required
    def api_delete_epic(epic_id):
        enforce_csrf()
        delete_epic(epic_id)
        return jsonify(ok=True)

    @app.route("/api/epic/<int:epic_id>", methods=["PATCH"])
    @login_required
    def api_update_epic(epic_id):
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        fields = {}
        for k in ("title", "color", "description", "start_date", "due_date", "status"):
            if k in data:
                v = data[k]
                if isinstance(v, str):
                    v = v.strip()
                # Empty string for dates means "clear"
                if k in ("start_date", "due_date") and v == "":
                    v = None
                if k == "title" and not v:
                    continue  # don't blank out title
                if k == "status" and v not in ("planning", "active", "completed", "on_hold"):
                    continue
                fields[k] = v
        update_epic(epic_id, fields)
        return jsonify(ok=True)

    @app.route("/api/epic/<int:epic_id>/move", methods=["POST"])
    @login_required
    def api_move_epic(epic_id):
        enforce_csrf()
        data      = request.get_json(silent=True) or {}
        direction = (data.get("direction") or "").strip().lower()
        if direction not in ("up", "down"):
            return jsonify(ok=False, error="direction must be up or down"), 400
        ok, swapped_with = move_epic(epic_id, direction)
        if not ok:
            return jsonify(ok=False, error="cannot move"), 400
        return jsonify(ok=True, swapped_with=swapped_with)

    @app.route("/api/project/<int:project_id>/epics/reorder", methods=["POST"])
    @login_required
    def api_reorder_epics(project_id):
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        ids  = data.get("order")
        if not isinstance(ids, list):
            return jsonify(ok=False, error="order must be a list of epic ids"), 400
        reorder_epics(project_id, ids)
        return jsonify(ok=True)

    @app.route("/api/epic/<int:epic_id>/full", methods=["GET"])
    @login_required
    def api_get_epic_full(epic_id):
        epic = get_epic(epic_id)
        if not epic:
            return jsonify(ok=False, error="not found"), 404
        stories = get_epic_stories_full(epic_id)
        statuses = get_statuses(epic["project_id"])

        # Per-status grouping (counts + points)
        by_status = {}
        for st in statuses:
            by_status[st["id"]] = {
                "id": st["id"], "name": st["name"], "color": st["color"],
                "is_done": st["is_done"], "count": 0, "points": 0,
            }
        total_pts = done_pts = 0
        done_count = 0
        for s in stories:
            sid = s["status_id"]
            pts = s.get("story_points") or 0
            total_pts += pts
            if sid in by_status:
                by_status[sid]["count"] += 1
                by_status[sid]["points"] += pts
            if s.get("status_is_done"):
                done_count += 1
                done_pts  += pts
        total = len(stories)
        pct_count  = round(100 * done_count / total) if total else 0
        pct_points = round(100 * done_pts / total_pts) if total_pts else 0

        # Timeline math
        from datetime import date
        days_total = days_elapsed = days_remaining = None
        sched_pct = None
        sd, dd = epic.get("start_date"), epic.get("due_date")
        try:
            if sd and dd:
                d0 = date.fromisoformat(sd)
                d1 = date.fromisoformat(dd)
                today = date.today()
                days_total = (d1 - d0).days
                days_elapsed = max(0, (today - d0).days)
                days_remaining = (d1 - today).days
                if days_total > 0:
                    sched_pct = round(100 * min(max(days_elapsed,0), days_total) / days_total)
        except Exception:
            pass

        return jsonify(
            ok=True,
            epic=epic,
            stories=stories,
            statuses=[dict(s) for s in statuses],
            stats={
                "total_stories":  total,
                "done_stories":   done_count,
                "total_points":   total_pts,
                "done_points":    done_pts,
                "pct_count":      pct_count,
                "pct_points":     pct_points,
                "by_status":      list(by_status.values()),
                "days_total":     days_total,
                "days_elapsed":   days_elapsed,
                "days_remaining": days_remaining,
                "sched_pct":      sched_pct,
            },
        )

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

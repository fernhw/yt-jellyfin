"""routes/sprint.py — Bulk story add, end-sprint, archive, and unarchive."""
import time

from flask import (abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)

from auth import enforce_csrf, login_required, super_user_required
from db import (get_all_active_users, get_current_sprint, get_db,
                get_epics, get_project, get_story_types, user_in_project)


def _check_project_access(project_id: int):
    """Return project or abort; also 403 if user not in project."""
    project = get_project(project_id)
    if not project:
        abort(404)
    if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
        abort(403)
    return project


def register(app) -> None:

    # ── Bulk story add ────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/bulk-add", methods=["GET", "POST"])
    @super_user_required
    def bulk_add(project_id):
        project = _check_project_access(project_id)

        if request.method == "POST":
            enforce_csrf()
            titles           = request.form.getlist("title[]")
            points_list      = request.form.getlist("points[]")
            priorities       = request.form.getlist("priority[]")
            descs            = request.form.getlist("description[]")
            assignees        = request.form.getlist("assignee[]")
            story_types_list = request.form.getlist("story_type[]")
            epic_ids         = request.form.getlist("epic_id[]")

            conn = get_db()
            first_status = conn.execute(
                "SELECT id FROM statuses WHERE project_id=? ORDER BY order_index LIMIT 1",
                (project_id,),
            ).fetchone()
            status_id = first_status["id"] if first_status else None
            now       = int(time.time())
            created   = 0

            for i, title in enumerate(titles):
                title = title.strip()
                if not title:
                    continue
                pts_raw = points_list[i].strip() if i < len(points_list) else ""
                pts     = int(pts_raw) if pts_raw.isdigit() else 0
                prio    = priorities[i].strip() if i < len(priorities) else None
                desc    = descs[i].strip() if i < len(descs) else ""
                a_raw   = assignees[i].strip() if i < len(assignees) else ""
                a_id    = int(a_raw) if a_raw.isdigit() else None
                st_raw  = story_types_list[i].strip() if i < len(story_types_list) else ""
                st_id   = int(st_raw) if st_raw.isdigit() else None
                ep_raw  = epic_ids[i].strip() if i < len(epic_ids) else ""
                ep_id   = int(ep_raw) if ep_raw.isdigit() else None

                max_idx = conn.execute(
                    "SELECT COALESCE(MAX(order_index), 0) + 1 FROM stories WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                cur = conn.execute(
                    """INSERT INTO stories
                       (project_id, title, description, story_points, priority,
                        story_type, epic_id,
                        status_id, created_at, created_by, order_index, is_archived)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (project_id, title, desc, pts, prio or None,
                     st_id, ep_id,
                     status_id, now, session["user_id"], max_idx),
                )
                if a_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO story_users (story_id, user_id, role) VALUES (?,?,'assignee')",
                        (cur.lastrowid, a_id),
                    )
                created += 1

            conn.commit()
            conn.close()
            flash(f"{created} {'story' if created == 1 else 'stories'} created.", "success")
            return redirect(url_for("backlog", project_id=project_id))

        users       = get_all_active_users()
        story_types = get_story_types(project_id)
        epics       = get_epics(project_id)
        return render_template("bulk_add.html", project=project, users=users,
                               story_types=story_types, epics=epics)

    # ── Bulk send to sprint ───────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/bulk-sprint", methods=["POST"])
    @login_required
    def bulk_sprint(project_id):
        enforce_csrf()
        _check_project_access(project_id)
        raw_ids = request.form.getlist("story_ids[]")
        story_ids = [int(x) for x in raw_ids if x.isdigit()]
        if not story_ids:
            flash("No stories selected.", "warning")
            return redirect(url_for("backlog", project_id=project_id))

        sprint = get_current_sprint(project_id)
        conn   = get_db()
        first_status = conn.execute(
            "SELECT id FROM statuses WHERE project_id=? ORDER BY order_index LIMIT 1",
            (project_id,),
        ).fetchone()
        status_id = first_status["id"] if first_status else None
        now = int(time.time())

        ph = ",".join("?" * len(story_ids))
        conn.execute(
            f"UPDATE stories SET sprint=?, status_id=?, updated_at=?"
            f" WHERE id IN ({ph}) AND project_id=? AND sprint IS NULL AND is_archived=0",
            [sprint, status_id, now] + story_ids + [project_id],
        )
        moved = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        conn.close()
        flash(
            f"{moved} {'story' if moved == 1 else 'stories'} added to Sprint {sprint}.",
            "success",
        )
        return redirect(url_for("backlog", project_id=project_id))

    # ── End sprint ────────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/end-sprint", methods=["POST"])
    @login_required
    def end_sprint(project_id):
        enforce_csrf()
        if session.get("role") not in ("admin", "super_user"):
            abort(403)
        project = _check_project_access(project_id)  # noqa: assigned for clarity
        sprint  = get_current_sprint(project_id)
        now     = int(time.time())
        conn    = get_db()

        # Archive stories in "done" columns
        conn.execute(
            """UPDATE stories SET is_archived=1, updated_at=?
               WHERE project_id=? AND sprint=?
                 AND status_id IN (SELECT id FROM statuses WHERE project_id=? AND is_done=1)""",
            (now, project_id, sprint, project_id),
        )
        archived = conn.execute("SELECT changes()").fetchone()[0]

        # Return unfinished stories to backlog
        conn.execute(
            """UPDATE stories SET sprint=NULL, updated_at=?
               WHERE project_id=? AND sprint=? AND is_archived=0""",
            (now, project_id, sprint),
        )
        returned = conn.execute("SELECT changes()").fetchone()[0]

        conn.commit()
        conn.close()
        flash(
            f"Sprint {sprint} ended — {archived} "
            f"{'story' if archived == 1 else 'stories'} archived, "
            f"{returned} returned to backlog.",
            "success",
        )
        return redirect(url_for("board", project_id=project_id))

    # ── Archive view ──────────────────────────────────────────────────────────

    @app.route("/project/<int:project_id>/archive")
    @login_required
    def archive(project_id):
        project = _check_project_access(project_id)
        return render_template("archive.html", project=project)

    @app.route("/api/project/<int:project_id>/archive")
    @login_required
    def api_archive(project_id):
        _check_project_access(project_id)
        page     = max(0, int(request.args.get("page", 0)))
        per_page = min(int(request.args.get("per_page", 20)), 50)
        offset   = page * per_page

        conn = get_db()
        rows = conn.execute(
            """SELECT s.id, s.title, s.story_points, s.priority, s.sprint,
                      s.updated_at, s.created_at,
                      st.name AS status_name, st.color AS status_color,
                      sty.name AS story_type_name, sty.color AS story_type_color,
                      p.key AS project_key
               FROM stories s
               LEFT JOIN statuses st ON s.status_id = st.id
               LEFT JOIN story_types sty ON s.story_type = sty.id
               LEFT JOIN projects p ON s.project_id = p.id
               WHERE s.project_id=? AND s.is_archived=1
               ORDER BY s.updated_at DESC
               LIMIT ? OFFSET ?""",
            (project_id, per_page + 1, offset),
        ).fetchall()

        has_more = len(rows) > per_page
        items = [dict(r) for r in rows[:per_page]]

        if items:
            story_ids = [it["id"] for it in items]
            ph = ",".join("?" * len(story_ids))

            sk_rows = conn.execute(
                f"SELECT id, type, card_x, card_y, label, card_story_id"
                f" FROM stickers WHERE card_story_id IN ({ph})",
                story_ids,
            ).fetchall()
            stickers_map: dict = {}
            for sk in sk_rows:
                stickers_map.setdefault(sk["card_story_id"], []).append(dict(sk))

            a_rows = conn.execute(
                f"""SELECT su.story_id, u.display_name, u.avatar
                    FROM story_users su JOIN users u ON su.user_id = u.id
                    WHERE su.story_id IN ({ph})""",
                story_ids,
            ).fetchall()
            assignees_map: dict = {}
            for a in a_rows:
                assignees_map.setdefault(a["story_id"], []).append(
                    {"display_name": a["display_name"], "avatar": a["avatar"]}
                )

            th_rows = conn.execute(
                f"""SELECT si.story_id, si.filename FROM story_images si
                    INNER JOIN (
                        SELECT story_id, MIN(id) AS min_id FROM story_images
                        WHERE story_id IN ({ph}) GROUP BY story_id
                    ) m ON si.id = m.min_id""",
                story_ids,
            ).fetchall()
            thumbnails = {r["story_id"]: r["filename"] for r in th_rows}

            for it in items:
                it["stickers"]  = stickers_map.get(it["id"], [])
                it["assignees"] = assignees_map.get(it["id"], [])
                it["thumbnail"] = thumbnails.get(it["id"])

        conn.close()
        return jsonify(items=items, has_more=has_more, page=page)

    # ── Unarchive (re-add to active sprint) ───────────────────────────────────

    @app.route("/api/story/<int:story_id>/unarchive", methods=["POST"])
    @login_required
    def api_unarchive_story(story_id):
        enforce_csrf()
        conn  = get_db()
        story = conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
        if not story:
            conn.close()
            abort(404)
        project_id = story["project_id"]
        if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
            conn.close()
            abort(403)

        sprint = get_current_sprint(project_id)
        first_status = conn.execute(
            "SELECT id FROM statuses WHERE project_id=? ORDER BY order_index LIMIT 1",
            (project_id,),
        ).fetchone()
        status_id = first_status["id"] if first_status else story["status_id"]

        conn.execute(
            "UPDATE stories SET is_archived=0, sprint=?, status_id=?, updated_at=? WHERE id=?",
            (sprint, status_id, int(time.time()), story_id),
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True, sprint=sprint)

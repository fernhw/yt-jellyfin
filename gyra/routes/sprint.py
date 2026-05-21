"""routes/sprint.py — Bulk story add, end-sprint, archive, and unarchive."""
import csv
import io
import time

from flask import (abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from markupsafe import Markup, escape

from auth import enforce_csrf, login_required, super_user_required
from db import (get_all_active_users, get_current_sprint, get_db,
                get_epics, get_project, get_story_types, user_in_project)
from routes.helpers import bold_verb_in_title, build_story_title, validate_story_parts
from db import get_story_previews


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
            actors       = request.form.getlist("story_actor[]")
            verbs        = request.form.getlist("story_verb[]")
            zs           = request.form.getlist("story_z[]")
            xs           = request.form.getlist("story_x[]")
            fors         = request.form.getlist("story_for[]")
            ys           = request.form.getlist("story_y[]")
            points_list  = request.form.getlist("points[]")
            priorities   = request.form.getlist("priority[]")
            descs        = request.form.getlist("description[]")
            assignees    = request.form.getlist("assignee[]")
            story_types_list = request.form.getlist("story_type[]")
            epic_ids     = request.form.getlist("epic_id[]")

            n = max(len(actors), len(verbs), len(zs), len(xs), len(fors), len(ys))

            def at(lst, i):
                return lst[i].strip() if i < len(lst) else ""

            conn = get_db()
            first_status = conn.execute(
                "SELECT id FROM statuses WHERE project_id=? ORDER BY order_index LIMIT 1",
                (project_id,),
            ).fetchone()
            status_id = first_status["id"] if first_status else None
            now       = int(time.time())
            created   = 0
            skipped   = 0
            row_errors = []   # list[(row_num, [messages])]
            row_warnings = [] # list[(row_num, [messages])]

            for i in range(n):
                actor    = at(actors, i)
                verb     = at(verbs, i)
                z        = at(zs, i)
                x        = at(xs, i)
                for_conn = at(fors, i)
                y        = at(ys, i)

                # Skip totally-empty rows silently (no Action Word, no What,
                # no Outcome — clearly a placeholder row the user never filled in).
                if not (z or x or y):
                    continue

                errors, warnings = validate_story_parts(actor, verb, z, x, for_conn, y)
                row_num = i + 1
                if errors:
                    row_errors.append((row_num, errors))
                    skipped += 1
                    continue
                if warnings:
                    row_warnings.append((row_num, warnings))

                title = build_story_title(actor, verb, z, x, for_conn, y)

                pts_raw = at(points_list, i)
                pts     = int(pts_raw) if pts_raw.isdigit() else 0
                prio    = at(priorities, i) or None
                desc    = at(descs, i)
                a_raw   = at(assignees, i)
                a_id    = int(a_raw) if a_raw.isdigit() else None
                st_raw  = at(story_types_list, i)
                st_id   = int(st_raw) if st_raw.isdigit() else None
                ep_raw  = at(epic_ids, i)
                ep_id   = int(ep_raw) if ep_raw.isdigit() else None

                max_idx = conn.execute(
                    "SELECT COALESCE(MAX(order_index), 0) + 1 FROM stories WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                cur = conn.execute(
                    """INSERT INTO stories
                       (project_id, title, description, story_points, priority,
                        story_type, epic_id,
                        status_id, created_at, created_by, order_index, is_archived,
                        story_actor, story_verb, story_z, story_x, story_for, story_y)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                    (project_id, title, desc, pts, prio,
                     st_id, ep_id,
                     status_id, now, session["user_id"], max_idx,
                     actor, verb, z, x, for_conn, y),
                )
                if a_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO story_users (story_id, user_id, role) VALUES (?,?,'assignee')",
                        (cur.lastrowid, a_id),
                    )
                created += 1

            conn.commit()
            conn.close()

            # Build flash messages — keep them readable per-row.
            if row_errors:
                lines = ["<strong>{} row(s) rejected.</strong>".format(skipped)]
                for row_num, errs in row_errors:
                    lines.append("<br><strong>Row {}:</strong>".format(row_num))
                    for e in errs:
                        lines.append("<br>&nbsp;&nbsp;• " + str(escape(e)))
                flash(Markup("".join(lines)), "error")

            if row_warnings:
                wlines = ["<strong>Warnings:</strong>"]
                for row_num, warns in row_warnings:
                    wlines.append("<br><strong>Row {}:</strong>".format(row_num))
                    for w in warns:
                        wlines.append("<br>&nbsp;&nbsp;• " + str(escape(w)))
                flash(Markup("".join(wlines)), "warning")

            if created:
                flash(f"{created} {'story' if created == 1 else 'stories'} created.", "success")
            elif not row_errors:
                flash("Nothing to create — all rows were empty.", "warning")

            # If everything succeeded, send the user to the backlog. Otherwise
            # stay on the bulk-add page so they can fix the rejected rows.
            if created and not row_errors:
                return redirect(url_for("backlog", project_id=project_id))
            return redirect(url_for("bulk_add", project_id=project_id))

        users       = get_all_active_users()
        story_types = get_story_types(project_id)
        epics       = get_epics(project_id)
        return render_template("bulk_add.html", project=project, users=users,
                               story_types=story_types, epics=epics)

    # ── Bulk story add (CSV import) ───────────────────────────────────────────

    @app.route("/project/<int:project_id>/bulk-add/csv", methods=["POST"])
    @super_user_required
    def bulk_add_csv(project_id):
        project = _check_project_access(project_id)
        enforce_csrf()

        f = request.files.get("csv_file")
        if not f or not f.filename:
            flash("No CSV file selected.", "error")
            return redirect(url_for("bulk_add", project_id=project_id))

        try:
            raw = f.read().decode("utf-8-sig", errors="replace")
        except Exception as e:
            flash(f"Could not read CSV: {escape(str(e))}", "error")
            return redirect(url_for("bulk_add", project_id=project_id))

        try:
            reader = csv.DictReader(io.StringIO(raw))
        except Exception as e:
            flash(f"Invalid CSV: {escape(str(e))}", "error")
            return redirect(url_for("bulk_add", project_id=project_id))

        # Normalise header keys → lower-case, stripped.
        def norm(s): return (s or "").strip().lower()
        rows = []
        for r in reader:
            rows.append({norm(k): (v or "").strip() for k, v in r.items()})

        if not rows:
            flash("CSV had no data rows.", "warning")
            return redirect(url_for("bulk_add", project_id=project_id))

        # Lookup maps for human-friendly columns (assignee/type/epic by name).
        users_map = {u["display_name"].lower(): u["id"]
                     for u in get_all_active_users()}
        types_map = {t["name"].lower(): t["id"]
                     for t in get_story_types(project_id)}
        epics_map = {e["title"].lower(): e["id"]
                     for e in get_epics(project_id)}

        conn = get_db()
        first_status = conn.execute(
            "SELECT id FROM statuses WHERE project_id=? ORDER BY order_index LIMIT 1",
            (project_id,),
        ).fetchone()
        status_id = first_status["id"] if first_status else None
        now = int(time.time())

        created = 0
        skipped = 0
        row_errors = []
        row_warnings = []

        for idx, r in enumerate(rows, start=1):
            actor    = r.get("actor", "")
            verb     = r.get("verb", "")
            z        = r.get("z", "") or r.get("action", "") or r.get("action_word", "")
            x        = r.get("x", "") or r.get("what", "")
            for_conn = r.get("for", "") or r.get("connector", "")
            y        = r.get("y", "") or r.get("outcome", "") or r.get("why", "")

            if not (z or x or y):
                continue  # blank row

            errors, warnings = validate_story_parts(actor, verb, z, x, for_conn, y)
            if errors:
                row_errors.append((idx, errors))
                skipped += 1
                continue
            if warnings:
                row_warnings.append((idx, warnings))

            title = build_story_title(actor, verb, z, x, for_conn, y)
            pts_raw = r.get("points", "")
            pts     = int(pts_raw) if pts_raw.isdigit() else 0
            prio    = (r.get("priority", "") or "").upper() or None
            desc    = r.get("description", "")
            os_val  = r.get("os", "") or None
            ver_val = r.get("software_version", "") or r.get("version", "") or None

            a_raw = r.get("assignee", "").lower()
            a_id  = users_map.get(a_raw) if a_raw else None
            t_raw = r.get("type", "").lower() or r.get("story_type", "").lower()
            st_id = types_map.get(t_raw) if t_raw else None
            e_raw = r.get("epic", "").lower()
            ep_id = epics_map.get(e_raw) if e_raw else None

            max_idx = conn.execute(
                "SELECT COALESCE(MAX(order_index), 0) + 1 FROM stories WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            cur = conn.execute(
                """INSERT INTO stories
                   (project_id, title, description, story_points, priority,
                    story_type, epic_id,
                    status_id, created_at, created_by, order_index, is_archived,
                    story_actor, story_verb, story_z, story_x, story_for, story_y,
                    software_version, os)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)""",
                (project_id, title, desc, pts, prio,
                 st_id, ep_id,
                 status_id, now, session["user_id"], max_idx,
                 actor, verb, z, x, for_conn, y,
                 ver_val, os_val),
            )
            if a_id:
                conn.execute(
                    "INSERT OR IGNORE INTO story_users (story_id, user_id, role) VALUES (?,?,'assignee')",
                    (cur.lastrowid, a_id),
                )
            created += 1

        conn.commit()
        conn.close()

        if row_errors:
            lines = ["<strong>{} CSV row(s) rejected.</strong>".format(skipped)]
            for n, errs in row_errors:
                lines.append("<br><strong>Row {}:</strong>".format(n))
                for e in errs:
                    lines.append("<br>&nbsp;&nbsp;• " + str(escape(e)))
            flash(Markup("".join(lines)), "error")
        if row_warnings:
            wlines = ["<strong>CSV warnings:</strong>"]
            for n, ws in row_warnings:
                wlines.append("<br><strong>Row {}:</strong>".format(n))
                for w in ws:
                    wlines.append("<br>&nbsp;&nbsp;• " + str(escape(w)))
            flash(Markup("".join(wlines)), "warning")
        if created:
            flash(f"{created} {'story' if created == 1 else 'stories'} imported from CSV.", "success")
        elif not row_errors:
            flash("CSV had no valid rows.", "warning")

        if created and not row_errors:
            return redirect(url_for("backlog", project_id=project_id))
        return redirect(url_for("bulk_add", project_id=project_id))

    @app.route("/project/<int:project_id>/bulk-add/csv-template")
    @super_user_required
    def bulk_add_csv_template(project_id):
        _check_project_access(project_id)
        sample = (
            "actor,verb,z,x,for,y,points,priority,type,epic,assignee,description,os,software_version\n"
            "Player,needs,Saving,their progress,to,avoid losing game state,3,H,Feature,,,Add a manual save button to the pause menu.,,\n"
            "Designer,needs,Tuning,enemy damage curves,to,balance combat pacing,2,M,Task,,,,,\n"
        )
        from flask import Response
        return Response(
            sample,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=gyra-bulk-template.csv"},
        )

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
            """SELECT s.id, s.title, s.story_z, s.story_points, s.priority, s.sprint,
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

            previews_map = get_story_previews(story_ids)

            for it in items:
                it["stickers"]   = stickers_map.get(it["id"], [])
                it["assignees"]  = assignees_map.get(it["id"], [])
                it["images"]     = previews_map.get(it["id"], [])
                it["thumbnail"]  = it["images"][0] if it["images"] else None
                it["html_title"] = str(bold_verb_in_title(it["title"], it.get("story_z") or ""))

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

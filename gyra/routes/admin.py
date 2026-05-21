"""routes/admin.py — Admin views for users, projects, statuses and backup."""
import datetime
import json
import os
import re
import time

from flask import (abort, flash, jsonify, redirect,
                   render_template, request, send_from_directory,
                   session, url_for)

from auth import admin_required, enforce_csrf, generate_setup_token
from config import Config
from db import (create_notification, get_all_active_users, get_db, get_project,
                get_project_members, get_statuses)


def register(app) -> None:

    # ── Users ────────────────────────────────────────────────────────────────

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        conn  = get_db()
        users = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return render_template("admin_users.html", users=users)

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_create_user():
        enforce_csrf()
        username     = request.form.get("username", "").strip()
        email        = request.form.get("email", "").strip()
        display_name = request.form.get("display_name", "").strip()
        role         = request.form.get("role", "user")

        if not all([username, email, display_name]):
            flash("All fields are required.", "error")
            return redirect(url_for("admin_users"))
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', username):
            flash("Username may only contain letters, numbers, underscores, hyphens and dots.", "error")
            return redirect(url_for("admin_users"))
        if role not in ("admin", "user", "super_user", "viewer"):
            role = "user"

        raw_token, token_hash = generate_setup_token()
        expires = int(time.time()) + 86400 * 7

        try:
            conn = get_db()
            conn.execute(
                """INSERT INTO users
                   (username,email,display_name,role,setup_token_hash,
                    setup_token_expires,created_at,created_by)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (username, email, display_name, role,
                 token_hash, expires, int(time.time()), session["user_id"]),
            )
            conn.commit()
            conn.close()
            setup_url = url_for("setup_totp", token=raw_token, _external=True)
            flash(
                f"User <strong>{username}</strong> created. "
                f"One-time setup link (copy now):<br><code>{setup_url}</code>",
                "success",
            )
        except Exception as exc:
            flash(f"Error: {exc}", "error")

        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
    @admin_required
    def admin_toggle_user(user_id):
        enforce_csrf()
        if user_id == session["user_id"]:
            flash("Cannot deactivate yourself.", "error")
            return redirect(url_for("admin_users"))
        conn = get_db()
        conn.execute(
            "UPDATE users SET is_active = "
            "CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
            (user_id,),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/reset-totp", methods=["POST"])
    @admin_required
    def admin_reset_totp(user_id):
        enforce_csrf()
        raw_token, token_hash = generate_setup_token()
        expires = int(time.time()) + 86400 * 2
        conn = get_db()
        conn.execute(
            """UPDATE users SET totp_secret_enc=NULL, totp_confirmed=0,
               setup_token_hash=?, setup_token_expires=? WHERE id=?""",
            (token_hash, expires, user_id),
        )
        conn.commit()
        conn.close()
        setup_url = url_for("setup_totp", token=raw_token, _external=True)
        flash(
            f"TOTP reset. New setup link (copy now):<br><code>{setup_url}</code>",
            "success",
        )
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/edit", methods=["POST"])
    @admin_required
    def admin_edit_user(user_id):
        enforce_csrf()
        conn = get_db()
        target = conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not target:
            flash("User not found.", "error")
            conn.close()
            return redirect(url_for("admin_users"))

        username     = request.form.get("username", "").strip()
        email        = request.form.get("email", "").strip()
        display_name = request.form.get("display_name", "").strip()
        role         = request.form.get("role", "user")
        is_active    = 1 if request.form.get("is_active") else 0

        if not all([username, email, display_name]):
            flash("Username, email and display name are required.", "error")
            conn.close()
            return redirect(url_for("admin_users"))
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', username):
            flash("Username may only contain letters, numbers, underscores, hyphens and dots.", "error")
            conn.close()
            return redirect(url_for("admin_users"))
        if role not in ("admin", "user", "super_user", "viewer"):
            role = "user"
        if user_id == session["user_id"] and role != "admin":
            flash("You cannot remove your own admin role.", "error")
            conn.close()
            return redirect(url_for("admin_users"))
        if role != "admin":
            admin_count = conn.execute(
                "SELECT COUNT(*) FROM users "
                "WHERE role='admin' AND is_active=1 AND id!=?",
                (user_id,),
            ).fetchone()[0]
            if admin_count == 0:
                flash("Cannot demote the only active admin.", "error")
                conn.close()
                return redirect(url_for("admin_users"))

        try:
            conn.execute(
                """UPDATE users SET username=?, email=?, display_name=?,
                   role=?, is_active=? WHERE id=?""",
                (username, email, display_name, role, is_active, user_id),
            )
            conn.commit()
            flash(f"User <strong>{username}</strong> updated.", "success")
        except Exception as exc:
            flash(f"Error: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id):
        enforce_csrf()
        if user_id == session["user_id"]:
            flash("You cannot delete your own account.", "error")
            return redirect(url_for("admin_users"))

        conn   = get_db()
        target = conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not target:
            flash("User not found.", "error")
            conn.close()
            return redirect(url_for("admin_users"))

        if target["role"] == "admin":
            admin_count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND id!=?",
                (user_id,),
            ).fetchone()[0]
            if admin_count == 0:
                flash("Cannot delete the only admin account.", "error")
                conn.close()
                return redirect(url_for("admin_users"))

        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        conn.close()
        flash(f"User <strong>{target['username']}</strong> deleted.", "success")
        return redirect(url_for("admin_users"))

    # ── Projects ─────────────────────────────────────────────────────────────

    @app.route("/admin/projects")
    @admin_required
    def admin_projects():
        conn         = get_db()
        raw_projects = conn.execute(
            "SELECT * FROM projects ORDER BY created_at"
        ).fetchall()
        conn.close()
        all_projects = []
        for p in raw_projects:
            d = dict(p)
            d["statuses"] = get_statuses(p["id"])
            d["members"]  = get_project_members(p["id"])
            all_projects.append(d)
        all_users = get_all_active_users()
        return render_template("admin_project.html",
                               all_projects=all_projects,
                               all_users=all_users)

    @app.route("/admin/projects/create", methods=["POST"])
    @admin_required
    def admin_create_project():
        enforce_csrf()
        name        = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        key         = request.form.get("key", "").strip().upper()

        if not name or not key:
            flash("Name and key are required.", "error")
            return redirect(url_for("admin_projects"))

        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO projects "
                "(name,description,key,created_at,created_by) VALUES (?,?,?,?,?)",
                (name, description, key, int(time.time()), session["user_id"]),
            )
            project_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            for idx, (sname, color, is_done) in enumerate([
                ("To Do",       "#6B7280", 0),
                ("In Progress", "#3B82F6", 0),
                ("In Review",   "#F59E0B", 0),
                ("Done",        "#10B981", 1),
            ]):
                conn.execute(
                    "INSERT INTO statuses "
                    "(project_id,name,color,order_index,is_done) VALUES (?,?,?,?,?)",
                    (project_id, sname, color, idx, is_done),
                )
            conn.execute(
                "INSERT OR IGNORE INTO project_members "
                "(project_id, user_id, added_by, added_at) VALUES (?,?,?,?)",
                (project_id, session["user_id"],
                 session["user_id"], int(time.time())),
            )
            conn.commit()
            conn.close()
            flash(f"Project {key} created.", "success")
        except Exception as exc:
            flash(f"Error: {exc}", "error")

        return redirect(url_for("admin_projects"))

    @app.route("/admin/projects/<int:project_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_project(project_id):
        enforce_csrf()
        confirm_name = request.form.get("confirm_name", "").strip()
        conn    = get_db()
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not project:
            conn.close()
            flash("Project not found.", "error")
            return redirect(url_for("admin_projects"))

        if confirm_name != project["name"]:
            conn.close()
            flash("Project name did not match — deletion cancelled.", "error")
            return redirect(url_for("admin_projects"))

        # ── Internal backup ──────────────────────────────────────────────────
        try:
            stories = conn.execute(
                "SELECT * FROM stories WHERE project_id=?", (project_id,)
            ).fetchall()
            statuses = conn.execute(
                "SELECT * FROM statuses WHERE project_id=?", (project_id,)
            ).fetchall()
            members = conn.execute(
                "SELECT u.id, u.username, u.display_name FROM users u "
                "JOIN project_members pm ON pm.user_id = u.id "
                "WHERE pm.project_id=?", (project_id,)
            ).fetchall()
            backup_data = {
                "project":  dict(project),
                "statuses": [dict(s) for s in statuses],
                "stories":  [dict(s) for s in stories],
                "members":  [dict(m) for m in members],
                "deleted_at": int(time.time()),
                "deleted_by": session["user_id"],
            }
            backup_dir = os.path.join(os.path.dirname(Config.DATABASE), "project_backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp     = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            bak_path  = os.path.join(backup_dir, f"{project['key']}_{stamp}.json")
            with open(bak_path, "w") as fh:
                json.dump(backup_data, fh, indent=2, default=str)
        except Exception as exc:
            app.logger.error("Project backup failed: %s", exc)

        # ── Notify all project members ────────────────────────────────────────
        member_rows = conn.execute(
            "SELECT user_id FROM project_members WHERE project_id=?", (project_id,)
        ).fetchall()
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()
        conn.close()
        for row in member_rows:
            if row["user_id"] != session["user_id"]:
                create_notification(
                    user_id=row["user_id"],
                    type_="system",
                    message=f"Project '{project['name']}' ({project['key']}) was deleted by an admin.",
                    from_user=session["user_id"],
                )
        flash(f"Project '{project['name']}' deleted.", "success")
        return redirect(url_for("admin_projects"))

    @app.route("/admin/projects/<int:project_id>/members/add", methods=["POST"])
    @admin_required
    def admin_add_project_member(project_id):
        enforce_csrf()
        uid = request.form.get("user_id", type=int)
        if uid:
            conn = get_db()
            conn.execute(
                "INSERT OR IGNORE INTO project_members "
                "(project_id, user_id, added_by, added_at) VALUES (?,?,?,?)",
                (project_id, uid, session["user_id"], int(time.time())),
            )
            conn.commit()
            conn.close()
        return redirect(url_for("admin_projects") + f"#{project_id}")

    @app.route("/admin/projects/<int:project_id>/members/remove", methods=["POST"])
    @admin_required
    def admin_remove_project_member(project_id):
        enforce_csrf()
        uid = request.form.get("user_id", type=int)
        if uid:
            conn = get_db()
            conn.execute(
                "DELETE FROM project_members WHERE project_id=? AND user_id=?",
                (project_id, uid),
            )
            conn.commit()
            conn.close()
        return redirect(url_for("admin_projects") + f"#{project_id}")

    @app.route("/admin/projects/<int:project_id>/status/add", methods=["POST"])
    @admin_required
    def admin_add_status(project_id):
        enforce_csrf()
        name    = request.form.get("name", "").strip()
        color   = request.form.get("color", "#6B7280").strip()
        is_done = 1 if request.form.get("is_done") else 0

        if name:
            conn = get_db()
            row  = conn.execute(
                "SELECT COALESCE(MAX(order_index),0)+1 AS nxt "
                "FROM statuses WHERE project_id=?",
                (project_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO statuses "
                "(project_id,name,color,order_index,is_done) VALUES (?,?,?,?,?)",
                (project_id, name, color, row["nxt"], is_done),
            )
            conn.commit()
            conn.close()
        return redirect(url_for("admin_projects"))

    @app.route(
        "/admin/projects/<int:project_id>/status/<int:status_id>/delete",
        methods=["POST"],
    )
    @admin_required
    def admin_delete_status(project_id, status_id):
        enforce_csrf()
        conn = get_db()
        conn.execute(
            "DELETE FROM statuses WHERE id=? AND project_id=?",
            (status_id, project_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_projects"))

    @app.route("/api/project/<int:project_id>/statuses/reorder",
               methods=["POST"])
    @admin_required
    def api_reorder_statuses(project_id):
        enforce_csrf()
        data        = request.get_json(silent=True) or {}
        ordered_ids = data.get("ids", [])
        if not ordered_ids:
            return jsonify(ok=False, error="missing ids"), 400
        try:
            ordered_ids = [int(i) for i in ordered_ids]
        except (ValueError, TypeError):
            return jsonify(ok=False, error="invalid ids"), 400
        conn = get_db()
        for idx, sid in enumerate(ordered_ids):
            conn.execute(
                "UPDATE statuses SET order_index=? WHERE id=? AND project_id=?",
                (idx, sid, project_id),
            )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    # ── Debug ─────────────────────────────────────────────────────────────────

    @app.route("/debug/headers")
    @admin_required
    def debug_headers():
        from flask import current_app
        lines = [f"{k}: {v}" for k, v in request.headers]
        return "<pre>" + "\n".join(lines) + "</pre>"

    # ── Backup ────────────────────────────────────────────────────────────────

    @app.route("/admin/backup")
    @admin_required
    def admin_backup():
        import shutil
        db_path  = Config.DATABASE
        stamp    = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak_name = f"gyra-backup-{stamp}.db"
        bak_path = os.path.join("/tmp", bak_name)
        shutil.copy2(db_path, bak_path)
        return send_from_directory("/tmp", bak_name, as_attachment=True,
                                   download_name=bak_name)

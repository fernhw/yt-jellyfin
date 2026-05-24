"""routes/epics.py — Epics management page + APIs (stats, archive, reorder)."""
from flask import (abort, jsonify, redirect, render_template, request,
                   session, url_for)

from auth import enforce_csrf, login_required
from db import (bulk_set_epic_archived, get_db, get_epic_stats, get_project,
                reorder_epics, set_epic_archived, user_in_project)


def _check_access(project_id: int):
    project = get_project(project_id)
    if not project:
        abort(404)
    if not user_in_project(session["user_id"], project_id):
        abort(403)
    return project


def _need_writer():
    if session.get("role") not in ("admin", "super_user"):
        abort(403)


def register(app) -> None:

    # ── Page ──────────────────────────────────────────────────────────────
    @app.route("/project/<int:project_id>/epics")
    @login_required
    def epics_manage(project_id):
        project = _check_access(project_id)
        return render_template("epics_manage.html", project=project)

    # ── Stats (paged, used by both this page and board panel) ─────────────
    @app.route("/api/project/<int:project_id>/epics/stats")
    @login_required
    def api_epic_stats(project_id):
        _check_access(project_id)
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit  = max(1, min(100, int(request.args.get("limit", 20))))
        except ValueError:
            offset, limit = 0, 20
        sort = request.args.get("sort", "newest")
        archived = request.args.get("archived", "0")  # "0" | "1" | "all"
        q = (request.args.get("q") or "").strip()
        if archived == "1":
            include, only = True, True
        elif archived == "all":
            include, only = True, False
        else:
            include, only = False, False
        rows, total = get_epic_stats(
            project_id, include_archived=include, archived_only=only,
            sort=sort, offset=offset, limit=limit, q=q,
        )
        out = []
        for r in rows:
            out.append({
                "id":             r["id"],
                "title":          r["title"],
                "color":          r["color"],
                "description":    r["description"] or "",
                "is_archived":    bool(r["is_archived"]),
                "initiative_id":  r["initiative_id"],
                "initiative_name": r["initiative_name"],
                "order_index":    r["order_index"],
                "created_at":     r["created_at"],
                "total_stories":  r["total_stories"],
                "done_stories":   r["done_stories"],
                "total_points":   r["total_points"],
                "done_points":    r["done_points"],
                "pct_done":       round(r["pct_done"] or 0, 1),
                "pri": {
                    "VH":    r["pri_vh"],
                    "H":     r["pri_h"],
                    "M":     r["pri_m"],
                    "L":     r["pri_l"],
                    "VL":    r["pri_vl"],
                    "unset": r["pri_unset"],
                },
            })
        return jsonify({"ok": True, "epics": out, "total": total,
                        "offset": offset, "limit": limit})

    # ── Archive single ────────────────────────────────────────────────────
    @app.route("/api/epic/<int:epic_id>/archive", methods=["POST"])
    @login_required
    def api_epic_archive(epic_id):
        enforce_csrf()
        _need_writer()
        conn = get_db()
        ep = conn.execute(
            "SELECT project_id FROM epics WHERE id=?", (epic_id,)
        ).fetchone()
        conn.close()
        if not ep:
            return jsonify({"ok": False, "error": "not_found"}), 404
        _check_access(ep["project_id"])
        archived = bool((request.get_json(silent=True) or request.form
                         ).get("archived", True))
        set_epic_archived(epic_id, archived)
        return jsonify({"ok": True, "archived": archived})

    # ── Bulk archive ──────────────────────────────────────────────────────
    @app.route("/api/project/<int:project_id>/epics/bulk-archive",
               methods=["POST"])
    @login_required
    def api_epic_bulk_archive(project_id):
        enforce_csrf()
        _need_writer()
        _check_access(project_id)
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        archived = bool(data.get("archived", True))
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_ids"}), 400
        n = bulk_set_epic_archived(project_id, ids, archived)
        return jsonify({"ok": True, "updated": n, "archived": archived})

    # ── Reorder (drag-drop) ───────────────────────────────────────────────
    @app.route("/api/project/<int:project_id>/epics/reorder",
               methods=["POST"])
    @login_required
    def api_epic_reorder(project_id):
        enforce_csrf()
        _need_writer()
        _check_access(project_id)
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_ids"}), 400
        reorder_epics(project_id, ids)
        return jsonify({"ok": True, "count": len(ids)})

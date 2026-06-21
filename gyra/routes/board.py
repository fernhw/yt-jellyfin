"""routes/board.py — Core project views: index, board, backlog, and WIP stubs."""
from flask import (abort, jsonify, redirect, render_template,
                   request, session, url_for)

from auth import login_required
from db import (ensure_story_types, get_all_active_users, get_all_sprints,
                get_backlog_stories, get_board_stories, get_db, get_epics,
                get_project, get_projects, get_statuses, get_stickers,
                get_stories_tasks_batch, get_story_thumbnails,
                get_story_previews,
                get_story_types, get_story_users, get_user_projects,
                user_in_project)
from routes.helpers import bold_verb_in_title


def register(app) -> None:

    @app.route("/")
    @login_required
    def index():
        uid  = session["user_id"]
        role = session.get("role")
        projects = get_projects() if role == "admin" else get_user_projects(uid)
        if projects:
            return redirect(url_for("board", project_id=projects[0]["id"]))
        return render_template("no_project.html")

    @app.route("/project/<int:project_id>/board")
    @login_required
    def board(project_id):
        project = get_project(project_id)
        if not project:
            abort(404)
        if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
            abort(403)

        statuses = get_statuses(project_id)

        # NOTE: sqlite3.Connection's context manager commits/rollbacks but
        # does NOT close — using try/finally here to release the file handle
        # and the WAL reader slot promptly.
        _conn = get_db()
        try:
            ensure_story_types(project_id, _conn)
            _conn.commit()
        finally:
            _conn.close()

        raw_stories = get_board_stories(project_id)

        stories = []
        for s in raw_stories:
            d = dict(s)
            d["assignees"]  = get_story_users(s["id"])
            d["html_title"] = bold_verb_in_title(s["title"], s["story_z"] or "")
            stories.append(d)

        story_ids  = [s["id"] for s in stories]
        previews   = get_story_previews(story_ids)
        tasks_map  = get_stories_tasks_batch(story_ids)
        for s in stories:
            s["images"] = previews.get(s["id"], [])
            s["tasks"]  = tasks_map.get(s["id"], [])

        board_map: dict = {st["id"]: [] for st in statuses}
        first_status_id = statuses[0]["id"] if statuses else None
        for s in stories:
            col = s["status_id"]
            if col not in board_map and first_status_id is not None:
                col = first_status_id
            if col in board_map:
                board_map[col].append(s)

        all_stickers      = [dict(sk) for sk in get_stickers(project_id)]
        free_stickers     = [s for s in all_stickers if not s.get("card_story_id")]
        card_stickers_map: dict = {}
        for s in all_stickers:
            cid = s.get("card_story_id")
            if cid:
                card_stickers_map.setdefault(cid, []).append(s)

        all_users   = get_all_active_users()
        all_sprints = get_all_sprints(project_id)
        story_types = get_story_types(project_id)

        seen_uids = set()
        board_assignees = []
        for s in stories:
            for a in s.get("assignees", []):
                uid = a["id"]
                if uid not in seen_uids:
                    seen_uids.add(uid)
                    board_assignees.append(dict(a))
        board_assignees.sort(key=lambda x: x.get("display_name", ""))

        # Build epics panel data: each epic + its sprint stories
        epics_raw = get_epics(project_id)
        epic_stories_map: dict = {}
        for s in stories:
            eid = s.get("epic_id")
            if eid:
                epic_stories_map.setdefault(eid, []).append({
                    "id":           s["id"],
                    "title":        s["title"],
                    "status_name":  s.get("status_name") or "",
                    "status_color": s.get("status_color") or "#6B7280",
                    "story_points": s.get("story_points") or 0,
                })
        epics_panel = []
        epic_by_id: dict = {}
        for e in epics_raw:
            ed = dict(e)
            ed["stories"] = epic_stories_map.get(e["id"], [])
            epics_panel.append(ed)
            epic_by_id[e["id"]] = ed

        # Per-column epic chips: which epics have stories in each column?
        column_epics: dict = {}
        for st in statuses:
            seen = {}
            for s in board_map.get(st["id"], []):
                eid = s.get("epic_id")
                if eid and eid in epic_by_id and eid not in seen:
                    e = epic_by_id[eid]
                    # count this epic's stories in this column
                    cnt = sum(1 for x in board_map[st["id"]] if x.get("epic_id") == eid)
                    seen[eid] = {
                        "id":    eid,
                        "title": e["title"],
                        "color": e["color"],
                        "count": cnt,
                    }
            column_epics[st["id"]] = list(seen.values())

        return render_template(
            "board.html",
            project=project,
            statuses=statuses,
            board_map=board_map,
            stickers=free_stickers,
            card_stickers_map=card_stickers_map,
            all_users=all_users,
            all_sprints=all_sprints,
            story_types=story_types,
            board_assignees=board_assignees,
            epics=epics_panel,
            column_epics=column_epics,
        )

    @app.route("/project/<int:project_id>/backlog")
    @login_required
    def backlog(project_id):
        project = get_project(project_id)
        if not project:
            abort(404)
        if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
            abort(403)

        raw_stories = get_backlog_stories(project_id)
        stories = []
        for s in raw_stories:
            d = dict(s)
            d["assignees"] = get_story_users(s["id"])
            d["_type"] = "story"
            stories.append(d)

        # Load saved separators and merge into one ordered list
        conn = get_db()
        sep_rows = conn.execute(
            "SELECT id, label, order_index FROM backlog_separators WHERE project_id=? ORDER BY order_index",
            (project_id,),
        ).fetchall()
        conn.close()

        merged = list(stories)
        for sep in sep_rows:
            merged.append({"_type": "sep", "id": sep["id"], "label": sep["label"], "order_index": sep["order_index"]})
        merged.sort(key=lambda x: float(x.get("order_index") or 0))

        statuses = get_statuses(project_id)

        return render_template(
            "backlog.html",
            project=project,
            stories=stories,
            merged=merged,
            statuses=statuses,
        )

    # ── Backlog separator CRUD ─────────────────────────────────────────────────

    @app.route("/api/project/<int:project_id>/backlog/separators/order", methods=["POST"])
    @login_required
    def backlog_separators_order(project_id):
        from auth import enforce_csrf
        from db import get_db
        enforce_csrf()
        if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
            abort(403)
        data = request.get_json(silent=True) or {}
        separators = data.get("separators", [])
        conn = get_db()
        id_map = []
        for sep in separators:
            sid   = sep.get("id", "")
            label = (sep.get("label") or "Section").strip()[:120]
            oidx  = float(sep.get("order_index", 0))
            if str(sid).startswith("new-") or not str(sid).isdigit():
                cur = conn.execute(
                    "INSERT INTO backlog_separators (project_id, label, order_index) VALUES (?,?,?)",
                    (project_id, label, oidx),
                )
                id_map.append({"old": sid, "id": cur.lastrowid})
            else:
                conn.execute(
                    "UPDATE backlog_separators SET label=?, order_index=? WHERE id=? AND project_id=?",
                    (label, oidx, int(sid), project_id),
                )
                id_map.append({"old": sid, "id": int(sid)})
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "ids": id_map})

    @app.route("/api/project/<int:project_id>/backlog/separator/<int:sep_id>", methods=["PATCH", "DELETE"])
    @login_required
    def backlog_separator_detail(project_id, sep_id):
        from auth import enforce_csrf
        from db import get_db
        enforce_csrf()
        if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
            abort(403)
        conn = get_db()
        if request.method == "DELETE":
            conn.execute("DELETE FROM backlog_separators WHERE id=? AND project_id=?", (sep_id, project_id))
        else:
            data  = request.get_json(silent=True) or {}
            label = (data.get("label") or "Section").strip()[:120]
            conn.execute("UPDATE backlog_separators SET label=? WHERE id=? AND project_id=?",
                         (label, sep_id, project_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    # ── WIP placeholder pages ──────────────────────────────────────────────────

    def _wip(project_id: int, feature: str):
        project = get_project(project_id)
        if not project:
            abort(404)
        if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
            abort(403)
        return render_template("wip.html", project=project, feature=feature)

    @app.route("/project/<int:project_id>/standup")
    @login_required
    def standup(project_id):
        return _wip(project_id, "Daily Standup")

    @app.route("/project/<int:project_id>/dailies")
    @login_required
    def dailies(project_id):
        return _wip(project_id, "Dailies")

    @app.route("/project/<int:project_id>/retro")
    @login_required
    def retro(project_id):
        return _wip(project_id, "Retro")

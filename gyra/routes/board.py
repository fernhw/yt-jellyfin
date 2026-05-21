"""routes/board.py — Core project views: index, board, backlog, and WIP stubs."""
from flask import (abort, redirect, render_template,
                   request, session, url_for)

from auth import login_required
from db import (ensure_story_types, get_all_active_users, get_all_sprints,
                get_backlog_stories, get_board_stories, get_db,
                get_project, get_projects, get_statuses, get_stickers,
                get_stories_tasks_batch, get_story_thumbnails,
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

        with get_db() as _conn:
            ensure_story_types(project_id, _conn)

        raw_stories = get_board_stories(project_id)

        stories = []
        for s in raw_stories:
            d = dict(s)
            d["assignees"]  = get_story_users(s["id"])
            d["html_title"] = bold_verb_in_title(s["title"], s["story_z"] or "")
            stories.append(d)

        story_ids  = [s["id"] for s in stories]
        thumbnails = get_story_thumbnails(story_ids)
        tasks_map  = get_stories_tasks_batch(story_ids)
        for s in stories:
            s["thumbnail"] = thumbnails.get(s["id"])
            s["tasks"]     = tasks_map.get(s["id"], [])

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
            stories.append(d)

        statuses = get_statuses(project_id)

        return render_template(
            "backlog.html",
            project=project,
            stories=stories,
            statuses=statuses,
        )

    # ── WIP placeholder pages ──────────────────────────────────────────────────

    def _wip(project_id: int, feature: str):
        project = get_project(project_id)
        if not project:
            abort(404)
        if session.get("role") != "admin" and not user_in_project(session["user_id"], project_id):
            abort(403)
        return render_template("wip.html", project=project, feature=feature)

    @app.route("/project/<int:project_id>/grooming")
    @login_required
    def grooming(project_id):
        return _wip(project_id, "Grooming")

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

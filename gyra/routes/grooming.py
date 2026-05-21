"""routes/grooming.py — Real-time scrum-poker grooming.

Page:
  GET  /project/<id>/grooming                  (regular voters + admin "central")

API:
  GET  /api/project/<id>/grooming/state        (poll: state + queue + votes)
  GET  /api/project/<id>/grooming/eligible     (admin: stories needing grooming)
  POST /api/project/<id>/grooming/queue        (admin: {story_id} add)
  POST /api/project/<id>/grooming/queue/remove (admin: {story_id})
  POST /api/project/<id>/grooming/active       (admin: {story_id|null})
  POST /api/project/<id>/grooming/reveal       (admin: {revealed: 0|1})
  POST /api/project/<id>/grooming/clear        (admin: clear active story's votes)
  POST /api/project/<id>/grooming/vote         (voter: {vote: "..." })
  POST /api/project/<id>/grooming/apply        (admin: {story_id, points})
"""
import time

from flask import abort, jsonify, render_template, request, session

from auth import enforce_csrf, login_required
from db import (get_db, get_epics, get_project, get_project_members,
                get_story_addons, get_story_users, user_in_project)
from routes.helpers import bold_verb_in_title


FIB_SCALE = [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89]


def _is_admin() -> bool:
    return session.get("role") in ("admin", "super_user")


def _require_access(project_id: int):
    if not _is_admin() and not user_in_project(session["user_id"], project_id):
        abort(403)


def _require_admin():
    if not _is_admin():
        abort(403)


def _fib_round(x: float) -> int:
    if x <= 0:
        return 0
    best = FIB_SCALE[0]
    for v in FIB_SCALE:
        if abs(v - x) < abs(best - x):
            best = v
    return best


def _get_state(conn, project_id: int):
    row = conn.execute(
        "SELECT * FROM grooming_state WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO grooming_state (project_id,active_story_id,revealed,updated_at) "
            "VALUES (?,NULL,0,?)",
            (project_id, int(time.time())),
        )
        conn.commit()
        return {"project_id": project_id, "active_story_id": None,
                "revealed": 0, "updated_at": int(time.time())}
    return dict(row)


def _serialize_story(conn, story_id: int, current_user_id: int = None):
    if not story_id:
        return None
    s = conn.execute(
        """SELECT s.*, st.name AS status_name, st.color AS status_color,
                  e.title AS epic_title, e.color AS epic_color
           FROM stories s
           LEFT JOIN statuses st ON st.id = s.status_id
           LEFT JOIN epics    e  ON e.id  = s.epic_id
           WHERE s.id=?""",
        (story_id,),
    ).fetchone()
    if not s:
        return None
    d = dict(s)
    d["html_title"]   = bold_verb_in_title(d.get("title") or "", d.get("story_z") or "")
    d["assignees"]    = [dict(u) for u in get_story_users(story_id)]
    d["addons"]       = [dict(a) for a in get_story_addons(story_id, current_user_id)]
    img_rows = conn.execute(
        "SELECT id, filename FROM story_images WHERE story_id=? ORDER BY id",
        (story_id,),
    ).fetchall()
    d["images"] = [{"id": r["id"], "url": f"/story-images/{r['filename']}",
                    "thumb": f"/story-images/thumb_{r['filename']}",
                    "med":   f"/story-images/med_{r['filename']}"}
                   for r in img_rows]
    return d


def _missing_fields(s) -> list:
    out = []
    if not s.get("story_points"):                       out.append("points")
    if not (s.get("priority") or "").strip():           out.append("priority")
    if not (s.get("description") or "").strip():        out.append("description")
    if not (s.get("acceptance_criteria") or "").strip():out.append("acceptance criteria")
    return out


def register(app) -> None:

    # ── Page ──────────────────────────────────────────────────────────────────
    @app.route("/project/<int:project_id>/grooming")
    @login_required
    def grooming(project_id):
        project = get_project(project_id)
        if not project:
            abort(404)
        _require_access(project_id)
        return render_template("grooming.html",
                               project=project,
                               is_admin=_is_admin())

    # ── State (everyone) ──────────────────────────────────────────────────────
    @app.route("/api/project/<int:project_id>/grooming/state")
    @login_required
    def api_grooming_state(project_id):
        _require_access(project_id)
        conn  = get_db()
        state = _get_state(conn, project_id)
        active_id = state["active_story_id"]
        story = _serialize_story(conn, active_id, session["user_id"]) if active_id else None

        # Queue (with brief story info)
        q_rows = conn.execute(
            """SELECT q.story_id, q.order_index,
                      s.title, s.story_points, s.priority,
                      COALESCE(s.description,'')         AS description,
                      COALESCE(s.acceptance_criteria,'') AS acceptance_criteria
               FROM grooming_queue q
               JOIN stories s ON s.id = q.story_id
               WHERE q.project_id=?
               ORDER BY q.order_index, q.id""",
            (project_id,),
        ).fetchall()
        queue = []
        for r in q_rows:
            d = dict(r)
            d["missing"] = _missing_fields(d)
            queue.append(d)

        # Votes for the active story
        votes_rows = []
        if active_id:
            votes_rows = conn.execute(
                """SELECT v.user_id, v.vote, v.created_at,
                          u.display_name, u.avatar
                   FROM grooming_votes v
                   LEFT JOIN users u ON u.id = v.user_id
                   WHERE v.project_id=? AND v.story_id=?""",
                (project_id, active_id),
            ).fetchall()

        # Members allowed to vote: all project members + admins
        member_rows = conn.execute(
            """SELECT u.id, u.display_name, u.avatar, u.role
               FROM users u
               LEFT JOIN project_members pm ON pm.user_id = u.id AND pm.project_id = ?
               WHERE u.is_active = 1
                 AND (pm.user_id IS NOT NULL OR u.role IN ('admin','super_user'))""",
            (project_id,),
        ).fetchall()
        voters = [dict(m) for m in member_rows]

        revealed = bool(state["revealed"])

        # Build a voter-status list (who voted, what if revealed)
        voted_map = {v["user_id"]: dict(v) for v in votes_rows}
        voter_states = []
        for m in voters:
            v = voted_map.get(m["id"])
            voter_states.append({
                "id":           m["id"],
                "display_name": m["display_name"],
                "avatar":       m["avatar"],
                "has_voted":    v is not None,
                "vote":         (v["vote"] if (v and revealed) else None),
            })

        # Stats
        numeric = []
        for v in votes_rows:
            try: numeric.append(float(v["vote"]))
            except (ValueError, TypeError): pass
        avg     = round(sum(numeric)/len(numeric), 2) if numeric else None
        fib_avg = _fib_round(avg) if avg is not None else None

        # Project members (for assignee +/−) and epics (for epic picker)
        members = [{"id": m["id"], "display_name": m["display_name"], "avatar": m["avatar"]}
                   for m in get_project_members(project_id)]
        epics   = [{"id": e["id"], "title": e["title"], "color": e["color"]}
                   for e in get_epics(project_id)]

        conn.close()
        my_vote = voted_map.get(session["user_id"])
        return jsonify(
            ok=True,
            is_admin=_is_admin(),
            state={"revealed": revealed, "active_story_id": active_id,
                   "updated_at": state["updated_at"]},
            story=story,
            queue=queue,
            voters=voter_states,
            members=members,
            epics=epics,
            my_vote=(my_vote["vote"] if my_vote else None),
            stats={
                "votes_cast":  len(votes_rows),
                "total_voters": len(voters),
                "average":      avg if revealed else None,
                "fib_average":  fib_avg if revealed else None,
            },
            fib_scale=FIB_SCALE,
        )

    # ── Vote (any project member) ─────────────────────────────────────────────
    @app.route("/api/project/<int:project_id>/grooming/vote", methods=["POST"])
    @login_required
    def api_grooming_vote(project_id):
        _require_access(project_id)
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        vote = (data.get("vote") or "").strip()
        if not vote:
            return jsonify(ok=False, error="vote required"), 400
        conn = get_db()
        state = _get_state(conn, project_id)
        sid = state["active_story_id"]
        if not sid:
            conn.close()
            return jsonify(ok=False, error="no active story"), 400
        now = int(time.time())
        conn.execute(
            """INSERT INTO grooming_votes (project_id,story_id,user_id,vote,created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(project_id,story_id,user_id) DO UPDATE SET
                 vote=excluded.vote, created_at=excluded.created_at""",
            (project_id, sid, session["user_id"], vote, now),
        )
        conn.execute(
            "UPDATE grooming_state SET updated_at=? WHERE project_id=?",
            (now, project_id),
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    # ── Admin: eligible stories (need grooming) ───────────────────────────────
    @app.route("/api/project/<int:project_id>/grooming/eligible")
    @login_required
    def api_grooming_eligible(project_id):
        _require_access(project_id)
        _require_admin()
        conn = get_db()
        rows = conn.execute(
            """SELECT s.id, s.title, s.story_points, s.priority,
                      COALESCE(s.description,'')         AS description,
                      COALESCE(s.acceptance_criteria,'') AS acceptance_criteria,
                      st.name AS status_name, st.color AS status_color,
                      s.sprint,
                      EXISTS(SELECT 1 FROM grooming_queue q
                             WHERE q.project_id=s.project_id AND q.story_id=s.id) AS in_queue
               FROM stories s
               LEFT JOIN statuses st ON st.id = s.status_id
               WHERE s.project_id=? AND COALESCE(s.is_archived,0)=0
               ORDER BY s.sprint DESC NULLS LAST, s.id DESC""",
            (project_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            m = _missing_fields(d)
            if not m:
                continue  # already groomed
            d["missing"]  = m
            d["in_queue"] = bool(d.get("in_queue"))
            out.append(d)
        conn.close()
        return jsonify(ok=True, stories=out)

    # ── Admin: queue add / remove ─────────────────────────────────────────────
    @app.route("/api/project/<int:project_id>/grooming/queue", methods=["POST"])
    @login_required
    def api_grooming_queue_add(project_id):
        _require_admin()
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        sid  = int(data.get("story_id") or 0)
        if not sid: return jsonify(ok=False, error="story_id required"), 400
        conn = get_db()
        # Verify story belongs to project
        ok = conn.execute("SELECT 1 FROM stories WHERE id=? AND project_id=?",
                          (sid, project_id)).fetchone()
        if not ok:
            conn.close()
            return jsonify(ok=False, error="not found"), 404
        max_idx = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM grooming_queue WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        try:
            conn.execute(
                "INSERT INTO grooming_queue (project_id,story_id,order_index,added_by,added_at) "
                "VALUES (?,?,?,?,?)",
                (project_id, sid, max_idx + 1, session["user_id"], int(time.time())),
            )
            conn.commit()
        except Exception:
            pass  # already in queue (UNIQUE)
        conn.close()
        return jsonify(ok=True)

    @app.route("/api/project/<int:project_id>/grooming/queue/all", methods=["POST"])
    @login_required
    def api_grooming_queue_all(project_id):
        """Bulk-queue every eligible (needs-grooming) story not already queued."""
        _require_admin()
        enforce_csrf()
        conn = get_db()
        rows = conn.execute(
            """SELECT s.id, s.story_points, s.priority,
                      COALESCE(s.description,'')         AS description,
                      COALESCE(s.acceptance_criteria,'') AS acceptance_criteria
               FROM stories s
               WHERE s.project_id=? AND COALESCE(s.is_archived,0)=0
                 AND NOT EXISTS(SELECT 1 FROM grooming_queue q
                                WHERE q.project_id=s.project_id AND q.story_id=s.id)""",
            (project_id,),
        ).fetchall()
        max_idx = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM grooming_queue WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        now = int(time.time())
        uid = session["user_id"]
        added = 0
        for r in rows:
            if not _missing_fields(dict(r)):
                continue  # already groomed
            max_idx += 1
            try:
                conn.execute(
                    "INSERT INTO grooming_queue (project_id,story_id,order_index,added_by,added_at) "
                    "VALUES (?,?,?,?,?)",
                    (project_id, r["id"], max_idx, uid, now),
                )
                added += 1
            except Exception:
                pass  # UNIQUE
        conn.commit()
        conn.close()
        return jsonify(ok=True, added=added)

    @app.route("/api/project/<int:project_id>/grooming/queue/remove", methods=["POST"])
    @login_required
    def api_grooming_queue_remove(project_id):
        _require_admin()
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        sid  = int(data.get("story_id") or 0)
        conn = get_db()
        conn.execute(
            "DELETE FROM grooming_queue WHERE project_id=? AND story_id=?",
            (project_id, sid),
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    # ── Admin: set active story ───────────────────────────────────────────────
    @app.route("/api/project/<int:project_id>/grooming/active", methods=["POST"])
    @login_required
    def api_grooming_set_active(project_id):
        _require_admin()
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        sid  = data.get("story_id")
        sid  = int(sid) if sid else None
        conn = get_db()
        _get_state(conn, project_id)
        conn.execute(
            "UPDATE grooming_state SET active_story_id=?, revealed=0, updated_at=? WHERE project_id=?",
            (sid, int(time.time()), project_id),
        )
        # Optionally clear stale votes for prior story? We keep votes per (project,story,user)
        # so switching active doesn't lose them. But reset revealed to hidden.
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    # ── Admin: reveal / hide ──────────────────────────────────────────────────
    @app.route("/api/project/<int:project_id>/grooming/reveal", methods=["POST"])
    @login_required
    def api_grooming_reveal(project_id):
        _require_admin()
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        rev  = 1 if data.get("revealed") else 0
        conn = get_db()
        _get_state(conn, project_id)
        conn.execute(
            "UPDATE grooming_state SET revealed=?, updated_at=? WHERE project_id=?",
            (rev, int(time.time()), project_id),
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    # ── Admin: clear votes for active story ───────────────────────────────────
    @app.route("/api/project/<int:project_id>/grooming/clear", methods=["POST"])
    @login_required
    def api_grooming_clear(project_id):
        _require_admin()
        enforce_csrf()
        conn = get_db()
        state = _get_state(conn, project_id)
        sid = state["active_story_id"]
        if sid:
            conn.execute(
                "DELETE FROM grooming_votes WHERE project_id=? AND story_id=?",
                (project_id, sid),
            )
        conn.execute(
            "UPDATE grooming_state SET revealed=0, updated_at=? WHERE project_id=?",
            (int(time.time()), project_id),
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True)

    # ── Admin: apply final score to story (+ optional fields) ─────────────────
    @app.route("/api/project/<int:project_id>/grooming/apply", methods=["POST"])
    @login_required
    def api_grooming_apply(project_id):
        _require_admin()
        enforce_csrf()
        data = request.get_json(silent=True) or {}
        sid  = int(data.get("story_id") or 0)
        if not sid: return jsonify(ok=False, error="story_id required"), 400
        conn = get_db()
        ok = conn.execute("SELECT 1 FROM stories WHERE id=? AND project_id=?",
                          (sid, project_id)).fetchone()
        if not ok:
            conn.close()
            return jsonify(ok=False, error="not found"), 404
        sets, vals = [], []
        if "points" in data and data["points"] is not None:
            try:
                sets.append("story_points=?")
                vals.append(int(data["points"]))
            except (ValueError, TypeError): pass
        if "priority" in data:
            sets.append("priority=?")
            vals.append((data["priority"] or "").strip() or None)
        if "description" in data:
            sets.append("description=?")
            vals.append(data["description"] or "")
        if "acceptance_criteria" in data:
            sets.append("acceptance_criteria=?")
            vals.append(data["acceptance_criteria"] or "")
        if sets:
            sets.append("updated_at=?")
            vals.append(int(time.time()))
            vals.append(sid)
            conn.execute(f"UPDATE stories SET {','.join(sets)} WHERE id=?", vals)
        # Remove from queue (groomed)
        conn.execute("DELETE FROM grooming_queue WHERE project_id=? AND story_id=?",
                     (project_id, sid))
        conn.commit()
        conn.close()
        return jsonify(ok=True)

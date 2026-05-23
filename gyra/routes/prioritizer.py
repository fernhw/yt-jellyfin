"""routes/prioritizer.py — Pareto-balanced priority drag-and-drop.

Summons a sectioned list of all stories (board scope or backlog scope) where
the user vertically drags cards between five priority buckets (VH/H/M/L/VL)
plus an "Unset" bucket. The UI coaches the user toward a 20%-per-bucket split
(Pareto). Changes are staged client-side until Apply, which bulk-updates the
new priorities in a single transaction.
"""
import time

from flask import abort, jsonify, render_template, request, session

from auth import enforce_csrf, login_required
from db import (get_backlog_stories, get_board_stories, get_db,
                get_project, log_story_change, user_in_project)


VALID_PRIORITIES = ("VH", "H", "M", "L", "VL")


def _check_access(project_id):
    project = get_project(project_id)
    if not project:
        abort(404)
    if session.get("role") != "admin" and not user_in_project(
            session["user_id"], project_id):
        abort(403)
    return project


def register(app) -> None:

    @app.route("/project/<int:project_id>/prioritizer")
    @login_required
    def prioritizer(project_id):
        project = _check_access(project_id)
        scope = request.args.get("scope", "backlog")
        if scope not in ("board", "backlog"):
            scope = "backlog"

        rows = (get_board_stories(project_id) if scope == "board"
                else get_backlog_stories(project_id))

        buckets = {p: [] for p in VALID_PRIORITIES}
        buckets["UN"] = []
        for s in rows:
            p = (s["priority"] or "").upper()
            key = p if p in VALID_PRIORITIES else "UN"
            buckets[key].append({
                "id":     s["id"],
                "title":  s["title"],
                "points": s["story_points"] or 0,
                "priority": p or "",
            })

        total = sum(len(v) for v in buckets.values())
        # Pareto ideal: 20% per bucket; tolerance ±20% (min ±1).
        ideal = total / 5.0 if total else 0.0
        tol = max(1, round(ideal * 0.20))
        lo = max(0, int(round(ideal - tol)))
        hi = max(lo, int(round(ideal + tol)))

        return render_template(
            "prioritizer.html",
            project=project,
            scope=scope,
            buckets=buckets,
            total=total,
            ideal=ideal,
            ideal_lo=lo,
            ideal_hi=hi,
        )

    @app.route("/api/project/<int:project_id>/prioritizer/apply",
               methods=["POST"])
    @login_required
    def prioritizer_apply(project_id):
        _check_access(project_id)
        enforce_csrf()

        if session.get("role") not in ("admin", "super_user"):
            return jsonify(ok=False, error="Forbidden"), 403

        data = request.get_json(silent=True) or {}
        updates = data.get("updates") or []
        if not isinstance(updates, list):
            return jsonify(ok=False, error="updates must be a list"), 400

        # Validate first; reject the entire batch on any bad entry.
        clean = []
        for u in updates:
            try:
                sid = int(u.get("id"))
            except (TypeError, ValueError):
                return jsonify(ok=False, error="bad id in updates"), 400
            new_p = (u.get("priority") or "").strip().upper()
            if new_p == "":
                new_p = None  # explicit clear
            elif new_p not in VALID_PRIORITIES:
                return jsonify(
                    ok=False,
                    error=f"invalid priority \"{new_p}\""), 400
            clean.append((sid, new_p))

        if not clean:
            return jsonify(ok=True, updated=0)

        conn = get_db()
        # Scope guard: every updated story must belong to this project.
        ids = [sid for sid, _ in clean]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, priority FROM stories WHERE id IN ({placeholders}) "
            f"AND project_id=?",
            (*ids, project_id),
        ).fetchall()
        existing = {r["id"]: r["priority"] for r in rows}
        if len(existing) != len(ids):
            conn.close()
            return jsonify(
                ok=False,
                error="one or more stories not in this project"), 400

        now = int(time.time())
        uid = session["user_id"]
        diffs = []   # (sid, old, new) — only actually-changing rows
        for sid, new_p in clean:
            old_p = existing[sid]
            if (old_p or None) == (new_p or None):
                continue
            diffs.append((sid, old_p, new_p))

        # Apply UPDATEs in one transaction, then close before logging
        # history — log_story_change() opens its own connection and would
        # otherwise deadlock on the open write transaction.
        for sid, _old, new_p in diffs:
            conn.execute(
                "UPDATE stories SET priority=?, updated_at=? WHERE id=?",
                (new_p, now, sid),
            )
        conn.commit()
        conn.close()

        for sid, old_p, new_p in diffs:
            log_story_change(sid, uid, "Priority", old_p, new_p)

        return jsonify(ok=True, updated=len(diffs))

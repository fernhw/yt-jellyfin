"""routes/api_container.py — Container/Attachment "sovereign story" API.

Conceptual model
----------------
Every story is sovereign — it can be worked in any order, never blocks others.
On top of that, a story may optionally declare itself a **Container** by
choosing a `box_type`:

  * whitebox   — built & inspectable; attachments may wire in now
  * blackbox   — opaque future promise; attachments must NOT reference yet
  * greybox    — stub / placeholder; attachments may dry-run only
  * featurebox — Container will absorb attachments itself; just signal complete

Another story may then point at the Container via `attached_to` (strict 1:1).
The Container may also declare a `dependent_action` string that is broadcast
onto the Attachment as a loud banner.

Authority flows host → guest only. The Attachment never imposes on the
Container. The Attachment never blocks the Container's "Done".

This module provides:
  POST   /api/story/<container_id>/attach           body: {child_id, dependent_action?}
  DELETE /api/story/<container_id>/attach/<child_id>
  POST   /api/story/<id>/box                        body: {box_type}
  GET    /api/story/<id>/container-info             — children + container details
"""
import time
from flask import jsonify, request, session

from auth import enforce_csrf, login_required
from db import get_db, get_story, log_story_change, user_in_project


VALID_BOXES = ("whitebox", "blackbox", "greybox", "featurebox")


def _require_access(story_id):
    s = get_story(story_id)
    if not s:
        return None
    if session.get("role") in ("admin", "super_user"):
        return s
    uid = session.get("user_id")
    if uid and user_in_project(uid, s["project_id"]):
        return s
    return None


def _fetch_attachments(container_id):
    """Return list of attachment stories with status info."""
    conn = get_db()
    rows = conn.execute(
        """SELECT s.id, s.title, s.story_z, s.status_id, s.dependent_action,
                  s.updated_at,
                  COALESCE(st.is_done, 0) AS is_done,
                  COALESCE(st.name, '—') AS status_name
           FROM stories s
           LEFT JOIN statuses st ON st.id = s.status_id
           WHERE s.attached_to = ?
             AND COALESCE(s.is_archived, 0) = 0
           ORDER BY s.id""",
        (container_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _fetch_container(child_id):
    """Return the container story for an attachment, or None."""
    child = get_story(child_id)
    if not child or not child["attached_to"]:
        return None
    container = get_story(child["attached_to"])
    return container


def register(app) -> None:

    # ── Box-type setter ─────────────────────────────────────────────────────
    @app.route("/api/story/<storyref:story_id>/box", methods=["POST"])
    @login_required
    def api_set_box(story_id):
        enforce_csrf()
        s = _require_access(story_id)
        if not s:
            return jsonify(ok=False), 404
        data = request.get_json(silent=True) or {}
        raw  = (data.get("box_type") or "").strip().lower() or None
        if raw is not None and raw not in VALID_BOXES:
            return jsonify(ok=False, error="invalid box_type"), 400

        old = s["box_type"] if "box_type" in s.keys() else None

        conn = get_db()
        # If removing the box (this story is no longer a Container), it
        # must not currently host any attachments. Refuse rather than orphan.
        if raw is None:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM stories WHERE attached_to=?",
                (story_id,),
            ).fetchone()["c"]
            if cnt:
                conn.close()
                return jsonify(
                    ok=False,
                    error=(f"This Container still has {cnt} attachment(s). "
                           "Detach them first."),
                ), 400
        conn.execute(
            "UPDATE stories SET box_type=?, updated_at=? WHERE id=?",
            (raw, int(time.time()), story_id),
        )
        conn.commit()
        conn.close()

        if old != raw:
            log_story_change(story_id, session["user_id"],
                             "Container box", old, raw)
        return jsonify(ok=True, box_type=raw)

    # ── Attach a child story to a container ─────────────────────────────────
    @app.route("/api/story/<storyref:container_id>/attach", methods=["POST"])
    @login_required
    def api_attach(container_id):
        enforce_csrf()
        container = _require_access(container_id)
        if not container:
            return jsonify(ok=False), 404
        data         = request.get_json(silent=True) or {}
        child_id_raw = data.get("child_id")
        action       = (data.get("dependent_action") or "").strip() or None
        force        = bool(data.get("force"))

        if not child_id_raw:
            return jsonify(ok=False, error="child_id required"), 400
        try:
            child_id = int(child_id_raw)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="bad child_id"), 400

        if child_id == container_id:
            return jsonify(ok=False, error="A story cannot attach to itself."), 400

        child = _require_access(child_id)
        if not child:
            return jsonify(ok=False, error="child not found"), 404
        if child["project_id"] != container["project_id"]:
            return jsonify(ok=False,
                           error="Attachments must be in the same project."), 400

        # Cycle guard: walk up the chain from container — if we reach child,
        # the attach would create a loop.
        conn = get_db()
        cur_id = container_id
        depth  = 0
        while cur_id is not None and depth < 32:
            row = conn.execute(
                "SELECT attached_to FROM stories WHERE id=?", (cur_id,)
            ).fetchone()
            if not row:
                break
            parent = row["attached_to"]
            if parent == child_id:
                conn.close()
                return jsonify(
                    ok=False,
                    error="Refused: this attach would create a chain loop."
                ), 400
            cur_id = parent
            depth += 1

        # Strict 1:1 — if child already attached to another container,
        # require force=true (front-end shows a confirm).
        existing = conn.execute(
            "SELECT attached_to FROM stories WHERE id=?", (child_id,)
        ).fetchone()
        prev_container = existing["attached_to"] if existing else None
        if prev_container and prev_container != container_id and not force:
            conn.close()
            return jsonify(
                ok=False,
                error="already_attached",
                current_container_id=prev_container,
            ), 409

        conn.execute(
            "UPDATE stories SET attached_to=?, dependent_action=?, updated_at=? "
            "WHERE id=?",
            (container_id, action, int(time.time()), child_id),
        )
        conn.commit()
        conn.close()

        log_story_change(child_id, session["user_id"],
                         "Attached to container",
                         prev_container, container_id)
        if action:
            log_story_change(child_id, session["user_id"],
                             "Dependent action",
                             None, action)
        return jsonify(ok=True,
                       attachments=_fetch_attachments(container_id))

    # ── Detach a child from a container ─────────────────────────────────────
    @app.route("/api/story/<storyref:container_id>/attach/<storyref:child_id>",
               methods=["DELETE"])
    @login_required
    def api_detach(container_id, child_id):
        enforce_csrf()
        if not _require_access(container_id):
            return jsonify(ok=False), 404
        child = _require_access(child_id)
        if not child or child["attached_to"] != container_id:
            return jsonify(ok=False, error="not attached"), 400

        conn = get_db()
        conn.execute(
            "UPDATE stories SET attached_to=NULL, dependent_action=NULL, "
            "updated_at=? WHERE id=?",
            (int(time.time()), child_id),
        )
        conn.commit()
        conn.close()
        log_story_change(child_id, session["user_id"],
                         "Detached from container",
                         container_id, None)
        return jsonify(ok=True,
                       attachments=_fetch_attachments(container_id))

    # ── Read-only: container info for a story ───────────────────────────────
    @app.route("/api/story/<storyref:story_id>/container-info")
    @login_required
    def api_container_info(story_id):
        s = _require_access(story_id)
        if not s:
            return jsonify(ok=False), 404
        info = _container_payload(s)
        # also include all candidate attachables (same project, not self,
        # not a story already attached elsewhere).
        conn = get_db()
        candidates = conn.execute(
            """SELECT id, title FROM stories
               WHERE project_id=? AND id<>?
                 AND COALESCE(is_archived,0)=0
               ORDER BY id DESC LIMIT 500""",
            (s["project_id"], story_id),
        ).fetchall()
        conn.close()
        info["candidates"] = [dict(r) for r in candidates]
        return jsonify(ok=True, **info)

    # ── Searchable, paged candidates picker ─────────────────────────────────
    @app.route("/api/story/<storyref:story_id>/attach-search")
    @login_required
    def api_attach_search(story_id):
        s = _require_access(story_id)
        if not s:
            return jsonify(ok=False), 404
        q      = (request.args.get("q") or "").strip()
        # Allow users to type project codes like "CTL-168" or "#168"; strip
        # any leading non-digit prefix so plain id-search still works.
        q_id = None
        import re as _re
        m = _re.match(r'^[#\s]*([A-Za-z]+-)?(\d+)\s*$', q)
        if m:
            try: q_id = int(m.group(2))
            except Exception: q_id = None
        limit  = min(max(int(request.args.get("limit",  "30")), 1), 100)
        offset = max(int(request.args.get("offset", "0")), 0)

        params = [s["project_id"], story_id, story_id]
        where  = ("s.project_id=? AND s.id<>? "
                  "AND COALESCE(s.is_archived,0)=0 "
                  "AND (s.attached_to IS NULL OR s.attached_to<>?)")
        if q:
            where += " AND (s.title LIKE ? OR CAST(s.id AS TEXT) LIKE ?"
            like = "%" + q + "%"
            params.extend([like, like])
            if q_id is not None:
                where += " OR s.id=?"
                params.append(q_id)
            where += ")"

        conn = get_db()
        rows = conn.execute(
            "SELECT s.id, s.title, s.attached_to, "
            "       COALESCE(st.name,'—') AS status_name, "
            "       COALESCE(st.color,'#94a3b8') AS status_color "
            "FROM stories s LEFT JOIN statuses st ON st.id=s.status_id "
            "WHERE " + where + " ORDER BY s.id DESC LIMIT ? OFFSET ?",
            params + [limit + 1, offset],
        ).fetchall()
        conn.close()
        items = [dict(r) for r in rows[:limit]]
        return jsonify(ok=True, items=items, has_more=(len(rows) > limit))


def _container_payload(s):
    """Build the container-related payload bolted onto story responses."""
    out = {
        "box_type":         s["box_type"] if "box_type" in s.keys() else None,
        "attached_to":      s["attached_to"] if "attached_to" in s.keys() else None,
        "dependent_action": (s["dependent_action"]
                             if "dependent_action" in s.keys() else None),
        "attachments":      [],
        "container":        None,
    }
    if out["box_type"]:
        out["attachments"] = _fetch_attachments(s["id"])
    if out["attached_to"]:
        c = get_story(out["attached_to"])
        if c:
            # status_is_done lives on the joined statuses row
            done = False
            try:
                conn = get_db()
                r = conn.execute(
                    "SELECT COALESCE(st.is_done,0) AS d FROM stories s "
                    "LEFT JOIN statuses st ON s.status_id=st.id WHERE s.id=?",
                    (c["id"],),
                ).fetchone()
                conn.close()
                done = bool(r["d"]) if r else False
            except Exception:
                pass
            out["container"] = {
                "id":       c["id"],
                "title":    c["title"],
                "box_type": c["box_type"] if "box_type" in c.keys() else None,
                "is_done":  done,
            }
    return out


# Helper for api_story.py to reuse in /api/story/<id>/full
def container_payload(s):
    return _container_payload(s)

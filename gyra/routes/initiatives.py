"""routes/initiatives.py — Initiatives ("Grand Epics") CRUD + detail.

An Initiative groups multiple Epics under one strategic goal and tracks
progress against named Milestones. The rule type (priority / pct_stories /
pct_points / count_stories / count_points) is locked once the initiative
leaves draft status so milestones remain meaningful.
"""
from flask import (abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)

from auth import enforce_csrf, login_required
from db import (add_initiative_milestone, create_initiative,
                delete_initiative, delete_initiative_milestone,
                evaluate_initiative_progress, get_db, get_initiative,
                get_initiative_history, get_initiatives, get_project,
                INIT_STATUSES, list_initiative_epics,
                list_initiative_milestones, list_unattached_epics,
                log_initiative_change, RULE_TYPES, set_epic_initiative,
                update_initiative, user_in_project)


RULE_LABELS = {
    "priority":      "Priority tier (MVP = all VH done, then H, M, L, VL)",
    "pct_stories":   "% of stories done",
    "pct_points":    "% of points done",
    "count_stories": "Absolute # of stories done",
    "count_points":  "Absolute # of points done",
}
STATUS_LABELS = {
    "draft":    "Draft",
    "active":   "Active",
    "shipped":  "Shipped",
    "archived": "Archived",
}


def _check_access(project_id):
    project = get_project(project_id)
    if not project:
        abort(404)
    if session.get("role") != "admin" and not user_in_project(
            session["user_id"], project_id):
        abort(403)
    return project


def _check_initiative(project_id, initiative_id):
    project = _check_access(project_id)
    init = get_initiative(initiative_id)
    if not init or init["project_id"] != project_id:
        abort(404)
    return project, init


def _need_writer():
    if session.get("role") not in ("admin", "super_user"):
        abort(403)


def register(app) -> None:

    # ── Index ──────────────────────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives")
    @login_required
    def initiatives_index(project_id):
        project = _check_access(project_id)
        status_filter = request.args.get("status") or None
        if status_filter not in INIT_STATUSES:
            status_filter = None
        items = get_initiatives(project_id, status_filter)
        # Attach overall progress
        rows = []
        for it in items:
            overall, _ms = evaluate_initiative_progress(it["id"])
            d = dict(it)
            d["overall_pct"] = overall
            rows.append(d)
        return render_template(
            "initiatives.html",
            project=project,
            initiatives=rows,
            status_filter=status_filter,
            STATUS_LABELS=STATUS_LABELS,
            RULE_LABELS=RULE_LABELS,
            INIT_STATUSES=INIT_STATUSES,
        )

    # ── Create form + POST ────────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives/new")
    @login_required
    def initiative_new(project_id):
        _check_access(project_id)
        _need_writer()
        project = get_project(project_id)
        return render_template(
            "initiative_form.html",
            project=project,
            initiative=None,
            RULE_LABELS=RULE_LABELS,
            RULE_TYPES=RULE_TYPES,
        )

    @app.route("/project/<int:project_id>/initiatives", methods=["POST"])
    @login_required
    def initiative_create(project_id):
        _check_access(project_id)
        _need_writer()
        enforce_csrf()
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Initiative name is required.", "error")
            return redirect(url_for("initiative_new", project_id=project_id))
        desc = (request.form.get("description") or "").strip()
        color = (request.form.get("color") or "#6D28D9").strip()
        rule = (request.form.get("rule_type") or "priority").strip()
        if rule not in RULE_TYPES:
            rule = "priority"
        iid = create_initiative(project_id, name, desc, color, rule,
                                session["user_id"])
        log_initiative_change(iid, session["user_id"], "Created", None, name)
        flash(f"Initiative “{name}” created.", "success")
        return redirect(url_for("initiative_detail",
                                project_id=project_id, initiative_id=iid))

    # ── Detail ────────────────────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>")
    @login_required
    def initiative_detail(project_id, initiative_id):
        project, init = _check_initiative(project_id, initiative_id)
        overall, milestones = evaluate_initiative_progress(initiative_id)
        epics = list_initiative_epics(initiative_id)
        # progress per epic (story count + done count)
        conn = get_db()
        epic_rows = []
        for e in epics:
            agg = conn.execute(
                "SELECT COUNT(*) AS total, "
                " SUM(CASE WHEN st.is_done=1 THEN 1 ELSE 0 END) AS done, "
                " COALESCE(SUM(s.story_points),0) AS pts, "
                " COALESCE(SUM(CASE WHEN st.is_done=1 THEN s.story_points "
                "                   ELSE 0 END),0) AS done_pts "
                "FROM stories s LEFT JOIN statuses st ON s.status_id=st.id "
                "WHERE s.epic_id=? AND s.is_archived=0", (e["id"],)
            ).fetchone()
            epic_rows.append({
                "id": e["id"], "title": e["title"], "color": e["color"],
                "total": agg["total"] or 0, "done": agg["done"] or 0,
                "points": agg["pts"] or 0, "done_points": agg["done_pts"] or 0,
            })
        conn.close()
        available_epics = list_unattached_epics(project_id)
        history = get_initiative_history(initiative_id)
        rule_locked = init["status"] != "draft"
        return render_template(
            "initiative_detail.html",
            project=project, initiative=init, overall_pct=overall,
            milestones=milestones, epics=epic_rows,
            available_epics=available_epics, history=history,
            rule_locked=rule_locked,
            RULE_LABELS=RULE_LABELS, RULE_TYPES=RULE_TYPES,
            STATUS_LABELS=STATUS_LABELS, INIT_STATUSES=INIT_STATUSES,
        )

    # ── Update basics ─────────────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>/update",
               methods=["POST"])
    @login_required
    def initiative_update(project_id, initiative_id):
        project, init = _check_initiative(project_id, initiative_id)
        _need_writer()
        enforce_csrf()
        uid = session["user_id"]
        changes = {}
        for f in ("name", "description", "color"):
            new = (request.form.get(f) or "").strip()
            if f == "name" and not new:
                flash("Name is required.", "error")
                return redirect(url_for("initiative_detail",
                                        project_id=project_id,
                                        initiative_id=initiative_id))
            if (init[f] or "") != new:
                changes[f] = new
                log_initiative_change(initiative_id, uid, f.capitalize(),
                                      init[f], new)
        # rule_type only editable while in draft
        if init["status"] == "draft":
            new_rule = (request.form.get("rule_type") or "").strip()
            if new_rule and new_rule in RULE_TYPES and new_rule != init["rule_type"]:
                changes["rule_type"] = new_rule
                log_initiative_change(initiative_id, uid, "Rule type",
                                      init["rule_type"], new_rule)
        if changes:
            update_initiative(initiative_id, changes)
            flash("Initiative updated.", "success")
        return redirect(url_for("initiative_detail",
                                project_id=project_id,
                                initiative_id=initiative_id))

    # ── Status transition ─────────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>/status",
               methods=["POST"])
    @login_required
    def initiative_set_status(project_id, initiative_id):
        project, init = _check_initiative(project_id, initiative_id)
        _need_writer()
        enforce_csrf()
        new = (request.form.get("status") or "").strip()
        if new not in INIT_STATUSES:
            flash("Invalid status.", "error")
        elif new != init["status"]:
            update_initiative(initiative_id, {"status": new})
            log_initiative_change(initiative_id, session["user_id"],
                                  "Status", init["status"], new)
            flash(f"Status → {STATUS_LABELS[new]}.", "success")
        return redirect(url_for("initiative_detail",
                                project_id=project_id,
                                initiative_id=initiative_id))

    # ── Delete ────────────────────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>/delete",
               methods=["POST"])
    @login_required
    def initiative_delete(project_id, initiative_id):
        project, init = _check_initiative(project_id, initiative_id)
        _need_writer()
        enforce_csrf()
        delete_initiative(initiative_id)
        flash(f"Initiative “{init['name']}” deleted.", "success")
        return redirect(url_for("initiatives_index", project_id=project_id))

    # ── Milestones ────────────────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>/milestones",
               methods=["POST"])
    @login_required
    def initiative_milestone_add(project_id, initiative_id):
        project, init = _check_initiative(project_id, initiative_id)
        _need_writer()
        enforce_csrf()
        name = (request.form.get("name") or "").strip()
        thr  = (request.form.get("threshold") or "").strip()
        if not name or not thr:
            flash("Milestone needs a name and a threshold.", "error")
            return redirect(url_for("initiative_detail",
                                    project_id=project_id,
                                    initiative_id=initiative_id))
        # Light validation per rule type.
        rule = init["rule_type"]
        if rule == "priority":
            thr = thr.upper()
            if thr not in ("VH", "H", "M", "L", "VL"):
                flash("Priority threshold must be VH, H, M, L, or VL.", "error")
                return redirect(url_for("initiative_detail",
                                        project_id=project_id,
                                        initiative_id=initiative_id))
        else:
            try:
                float(thr)
            except ValueError:
                flash("Threshold must be a number.", "error")
                return redirect(url_for("initiative_detail",
                                        project_id=project_id,
                                        initiative_id=initiative_id))
        existing = list_initiative_milestones(initiative_id)
        order = len(existing)
        mid = add_initiative_milestone(initiative_id, name, thr, order)
        log_initiative_change(initiative_id, session["user_id"],
                              "Milestone added", None, f"{name} = {thr}")
        flash(f"Milestone “{name}” added.", "success")
        return redirect(url_for("initiative_detail",
                                project_id=project_id,
                                initiative_id=initiative_id))

    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>"
               "/milestones/<int:milestone_id>/delete", methods=["POST"])
    @login_required
    def initiative_milestone_delete(project_id, initiative_id, milestone_id):
        project, init = _check_initiative(project_id, initiative_id)
        _need_writer()
        enforce_csrf()
        ms = [m for m in list_initiative_milestones(initiative_id)
              if m["id"] == milestone_id]
        if ms:
            delete_initiative_milestone(milestone_id)
            log_initiative_change(initiative_id, session["user_id"],
                                  "Milestone removed", ms[0]["name"], None)
        return redirect(url_for("initiative_detail",
                                project_id=project_id,
                                initiative_id=initiative_id))

    # ── Epic attach / detach ──────────────────────────────────────
    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>/epics/attach",
               methods=["POST"])
    @login_required
    def initiative_attach_epic(project_id, initiative_id):
        project, init = _check_initiative(project_id, initiative_id)
        _need_writer()
        enforce_csrf()
        try:
            epic_id = int(request.form.get("epic_id") or 0)
        except ValueError:
            epic_id = 0
        if epic_id <= 0:
            flash("Pick an epic to attach.", "error")
            return redirect(url_for("initiative_detail",
                                    project_id=project_id,
                                    initiative_id=initiative_id))
        conn = get_db()
        ep = conn.execute(
            "SELECT id, title, project_id, initiative_id FROM epics WHERE id=?",
            (epic_id,)).fetchone()
        conn.close()
        if not ep or ep["project_id"] != project_id:
            flash("Epic not found in this project.", "error")
        elif ep["initiative_id"] is not None:
            flash("Epic already belongs to another initiative — detach first.",
                  "error")
        else:
            set_epic_initiative(epic_id, initiative_id)
            log_initiative_change(initiative_id, session["user_id"],
                                  "Epic attached", None, ep["title"])
            flash(f"Epic “{ep['title']}” attached.", "success")
        return redirect(url_for("initiative_detail",
                                project_id=project_id,
                                initiative_id=initiative_id))

    @app.route("/project/<int:project_id>/initiatives/<int:initiative_id>"
               "/epics/<int:epic_id>/detach", methods=["POST"])
    @login_required
    def initiative_detach_epic(project_id, initiative_id, epic_id):
        project, init = _check_initiative(project_id, initiative_id)
        _need_writer()
        enforce_csrf()
        conn = get_db()
        ep = conn.execute(
            "SELECT id, title, initiative_id FROM epics WHERE id=?",
            (epic_id,)).fetchone()
        conn.close()
        if ep and ep["initiative_id"] == initiative_id:
            set_epic_initiative(epic_id, None)
            log_initiative_change(initiative_id, session["user_id"],
                                  "Epic detached", ep["title"], None)
            flash(f"Epic “{ep['title']}” detached.", "success")
        return redirect(url_for("initiative_detail",
                                project_id=project_id,
                                initiative_id=initiative_id))

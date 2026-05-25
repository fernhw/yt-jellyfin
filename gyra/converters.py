"""
converters.py — Custom URL converters for Flask.

`storyref` accepts both forms in a URL:
  • `/story/124`     — bare integer PK (back-compat for old bookmarks)
  • `/story/CTL-12`  — project_key + per-project story_number (canonical)

In both cases the route handler receives the integer PK (`story.id`) it
already expects, so no handler changes are required.

For URL *generation*, `url_for('story_view', story_id=<int>)` automatically
emits the composite `KEY-NUMBER` form by looking up the story's project key
and per-project number. Callers that already have the composite string can
pass it directly to skip the DB lookup.
"""
import re

from werkzeug.exceptions import NotFound
from werkzeug.routing import BaseConverter


_COMPOSITE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$")


class StoryRefConverter(BaseConverter):
    # Match either pure digits or KEY-NUM.
    regex = r"\d+|[A-Za-z][A-Za-z0-9]*-\d+"

    def to_python(self, value):
        if value.isdigit():
            # Back-compat: bare PK.
            return int(value)
        m = _COMPOSITE_RE.match(value)
        if not m:
            raise NotFound()
        project_key, story_number = m.group(1), int(m.group(2))
        sid = _resolve_composite(project_key, story_number)
        if sid is None:
            raise NotFound()
        return sid

    def to_url(self, value):
        # Caller may pass a pre-built composite string ("CTL-12") to skip
        # the DB lookup.
        if isinstance(value, str):
            if not value.isdigit():
                return value
            value = int(value)
        try:
            sid = int(value)
        except (TypeError, ValueError):
            return str(value)
        composite = _composite_for(sid)
        return composite if composite else str(sid)


def _resolve_composite(project_key: str, story_number: int):
    """Resolve `(project_key, story_number)` → integer story id (PK).

    Returns None if no match. Safe to call inside a request context.
    """
    try:
        from db import get_db
        conn = get_db()
        row = conn.execute(
            "SELECT s.id FROM stories s "
            "JOIN projects p ON s.project_id = p.id "
            "WHERE p.key = ? COLLATE NOCASE AND s.story_number = ?",
            (project_key, story_number),
        ).fetchone()
        if row:
            return int(row["id"])
    except Exception:
        return None
    return None


def _composite_for(story_id: int):
    """Look up `<project_key>-<story_number>` for the given story PK.

    Returns None if the story or its project key cannot be found. Falls
    back to `<project_key>-<id>` if `story_number` is missing (shouldn't
    happen post-migration, but kept defensive). Safe to call outside a
    request context — silently degrades to None on error.
    """
    try:
        from db import get_db
        conn = get_db()
        row = conn.execute(
            "SELECT p.key AS k, s.story_number AS n "
            "FROM stories s JOIN projects p ON s.project_id = p.id "
            "WHERE s.id = ?",
            (story_id,),
        ).fetchone()
        if row and row["k"]:
            n = row["n"] if row["n"] is not None else story_id
            return f"{row['k']}-{n}"
    except Exception:
        return None
    return None

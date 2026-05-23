"""routes/__init__.py — Route-module registry.

Each module exposes a ``register(app)`` function that attaches its routes
to the Flask app instance.  No Blueprints are used so that all url_for()
endpoint names remain unchanged throughout templates and JS.
"""
from routes import auth, board, stories, admin, profile, api_story, api_project, sprint, grooming, prioritizer


def register_all(app) -> None:
    auth.register(app)
    board.register(app)
    stories.register(app)
    admin.register(app)
    profile.register(app)
    api_story.register(app)
    api_project.register(app)
    sprint.register(app)
    grooming.register(app)
    prioritizer.register(app)

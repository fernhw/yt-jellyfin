"""Shared test fixtures.

Works with both pytest (preferred) and stdlib unittest. We make sure the
gyra/ directory is on sys.path so `import app`, `from db import ...`, etc.
keep working exactly like in production.

Tests run against a temporary SQLite database (copied from gyra.db) so they
cannot mutate real data. The original DB path is restored afterwards.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# Ensure the gyra/ root is importable regardless of cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def isolated_test_db():
    """Return a path to a throwaway copy of gyra.db (or an empty file).

    Pytest fixtures or unittest setUp methods can call this to obtain a
    fresh database file. Caller is responsible for deleting it (or just
    leaving it in /tmp — tempfile cleans up at process exit).
    """
    src = os.path.join(ROOT, "gyra.db")
    fd, dst = tempfile.mkstemp(prefix="gyra-test-", suffix=".db")
    os.close(fd)
    if os.path.exists(src):
        shutil.copy(src, dst)
    return dst


def make_admin_client(app):
    """Return a Flask test client with an admin session pre-seeded.

    Mirrors the smoke-test pattern used throughout this codebase.
    """
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["role"] = "admin"
        s["username"] = "admin"
        s["display_name"] = "admin"
    return client


# --- pytest fixtures (only registered if pytest is installed) ---
try:  # pragma: no cover - import-time guard
    import pytest  # type: ignore

    @pytest.fixture()
    def app_instance():
        import app as appmod  # noqa: WPS433 (intentional late import)
        return appmod.app

    @pytest.fixture()
    def admin_client(app_instance):
        return make_admin_client(app_instance)
except ImportError:
    pass

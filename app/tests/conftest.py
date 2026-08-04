"""Pytest hooks loaded before test modules — pin integration DB URL early."""
import os
from pathlib import Path

# Must run before any test module imports app.database (engine freezes on first import).
_TEST_DB = Path(__file__).resolve().parent.parent / ".test_db.sqlite"
os.environ.setdefault("PARA_SCOPE_SECRET_KEY", "test-secret-key-for-pytest")
os.environ["PARA_SCOPE_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

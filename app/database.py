import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

SQLALCHEMY_DATABASE_URL = os.environ.get(
    "PARA_SCOPE_DATABASE_URL",
    f"sqlite:///{Path(__file__).resolve().parent.parent / 'para_scope.db'}",
)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# Columns added after initial create_all — SQLite won't alter existing tables.
_SCHEMA_PATCHES = {
    "polling_schedules": {
        "success_count": "INTEGER DEFAULT 0",
        "failure_count": "INTEGER DEFAULT 0",
        "last_error": "TEXT DEFAULT ''",
        "schedule_type": "VARCHAR(8) DEFAULT 'interval'",
    },
    "actions": {
        "source_id": "INTEGER",
        "secret_id_2": "INTEGER",
    },
    "event_types": {
        "enabled": "BOOLEAN DEFAULT 1",
    },
    "metric_points": {
        "field_id": "INTEGER",
    },
    "app_settings": {
        "font": "VARCHAR(50) DEFAULT 'system'",
        "font_size": "VARCHAR(20) DEFAULT 'md'",
        "dashboard_bg_filename": "VARCHAR(100)",
        "dashboard_bg_opacity": "FLOAT DEFAULT 0.35",
    },
}


def ensure_schema():
    """Add missing columns to existing SQLite tables (no Alembic)."""
    import json as _json

    with engine.begin() as conn:
        for table, columns in _SCHEMA_PATCHES.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            if not existing:
                continue  # table created later by create_all
            for col, col_type in columns.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))

        # Drop legacy dashboard_layouts.user_id (shared layout; no prod data to preserve)
        layout_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(dashboard_layouts)"))
        }
        if "user_id" in layout_cols:
            conn.execute(text(
                "CREATE TABLE dashboard_layouts_new ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "layout_config JSON, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            ))
            conn.execute(text(
                "INSERT INTO dashboard_layouts_new (id, layout_config, created_at, updated_at) "
                "SELECT id, layout_config, created_at, updated_at FROM dashboard_layouts "
                "ORDER BY id LIMIT 1"
            ))
            conn.execute(text("DROP TABLE dashboard_layouts"))
            conn.execute(text("ALTER TABLE dashboard_layouts_new RENAME TO dashboard_layouts"))

        # Drop legacy Source.base_url / Source.status / ActionInstance.status
        source_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(sources)"))
        }
        if "base_url" in source_cols:
            conn.execute(text("ALTER TABLE sources DROP COLUMN base_url"))
        if "status" in source_cols:
            conn.execute(text("ALTER TABLE sources DROP COLUMN status"))
        action_drop_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(actions)"))
        }
        if "status" in action_drop_cols:
            conn.execute(text("ALTER TABLE actions DROP COLUMN status"))

        # Migrate legacy schedule_type values (webhook/poll → interval)
        sched_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(polling_schedules)"))
        }
        if "schedule_type" in sched_cols:
            conn.execute(text(
                "UPDATE polling_schedules SET schedule_type = 'interval' "
                "WHERE schedule_type IN ('poll', 'webhook', '') OR schedule_type IS NULL"
            ))

        # Backfill actions.source_id from referencing rules, else first source
        action_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(actions)"))
        }
        if "source_id" not in action_cols:
            return

        orphan_ids = [
            row[0]
            for row in conn.execute(text("SELECT id FROM actions WHERE source_id IS NULL"))
        ]
        if not orphan_ids:
            return

        first_source = conn.execute(
            text("SELECT id FROM sources ORDER BY id LIMIT 1")
        ).fetchone()
        first_source_id = first_source[0] if first_source else None

        # Map action_id -> set of source_ids from rules that reference it
        action_to_sources: dict[int, set[int]] = {aid: set() for aid in orphan_ids}
        for source_id, raw_ids in conn.execute(
            text("SELECT source_id, action_ids FROM rules WHERE source_id IS NOT NULL")
        ):
            try:
                ids = _json.loads(raw_ids) if isinstance(raw_ids, str) else (raw_ids or [])
            except (ValueError, TypeError):
                ids = []
            for aid in ids:
                if aid in action_to_sources:
                    action_to_sources[aid].add(source_id)

        for action_id, sources in action_to_sources.items():
            if len(sources) == 1:
                target = next(iter(sources))
            else:
                target = first_source_id
            if target is not None:
                conn.execute(
                    text("UPDATE actions SET source_id = :sid WHERE id = :aid"),
                    {"sid": target, "aid": action_id},
                )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

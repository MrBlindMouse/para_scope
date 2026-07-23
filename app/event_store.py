"""Event storage helpers — retention so SQLite stays light."""
from __future__ import annotations

from app.models import Event, FieldLogEntry

# ponytail: fixed per-source ceiling; raise or make configurable if long history is needed
MAX_EVENTS_PER_SOURCE = 500
RECENT_LIMIT_DEFAULT = 5
RECENT_LIMIT_MAX = 50


def prune_source_events(db, source_id: int, *, keep: int = MAX_EVENTS_PER_SOURCE) -> int:
    """Keep the newest `keep` events for a source; delete older ones. Returns deleted count.

    Never deletes ``pending`` rows (in-flight webhook BackgroundTasks). Nullifies
    ``FieldLogEntry.event_id`` before delete so FK constraints do not fail.
    """
    if keep < 1:
        keep = 1
    rows = (
        db.query(Event.id, Event.status)
        .filter(Event.source_id == source_id)
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .all()
    )
    if len(rows) <= keep:
        return 0
    drop = [r.id for r in rows[keep:] if r.status != "pending"]
    if not drop:
        return 0
    db.query(FieldLogEntry).filter(FieldLogEntry.event_id.in_(drop)).update(
        {FieldLogEntry.event_id: None},
        synchronize_session=False,
    )
    deleted = (
        db.query(Event)
        .filter(Event.id.in_(drop))
        .delete(synchronize_session=False)
    )
    return deleted

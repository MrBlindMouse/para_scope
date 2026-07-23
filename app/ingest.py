"""Shared event ingress — persist, prune, return Event for pipeline dispatch."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.event_store import prune_source_events
from app.models import Event, Source


def ingest_event(
    db: Session,
    *,
    source: Source,
    event_type_id: int | None,
    correlation_id: str,
    raw_payload: str,
    normalized_data: dict,
    status: str = "pending",
    touch_last_seen: bool = True,
) -> Event:
    """Insert an event, safely prune older ones, optionally update source.last_seen_at."""
    event = Event(
        source_id=source.id,
        event_type_id=event_type_id,
        correlation_id=correlation_id,
        raw_payload=raw_payload,
        normalized_data=normalized_data,
        status=status,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    prune_source_events(db, source.id)
    if touch_last_seen:
        source.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return event

"""Shared event ingress — persist, prune, return Event for pipeline dispatch."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.event_store import prune_source_events
from app.models import Event, EventTypeRecord, Source
from app.pipeline import normalize_event_type


def ingest_event(
    db: Session,
    *,
    source: Source,
    event_type_id: int | None,
    correlation_id: str | None,
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


def ingest_manual_events(
    db: Session,
    source: Source,
    event_type: EventTypeRecord,
    payload: dict,
    *,
    meta: dict,
) -> list[Event]:
    """Ingest a webhook event type (and optional ``always`` sibling). Does not run the pipeline.

    ``meta`` is applied as ``_trigger`` after the payload so templates cannot spoof it.
    """
    if not isinstance(payload, dict):
        raise ValueError("Trigger payload must be a JSON object")

    base = {k: v for k, v in payload.items() if k != "_trigger"}
    base.setdefault("source", source.name)
    primary_data = {**base, "_trigger": dict(meta)}
    events = [
        ingest_event(
            db,
            source=source,
            event_type_id=event_type.id,
            correlation_id=None,
            raw_payload=json.dumps(primary_data),
            normalized_data=primary_data,
        )
    ]

    always_et = None
    want_always = normalize_event_type("always")
    for et in db.query(EventTypeRecord).filter(EventTypeRecord.source_id == source.id).all():
        if normalize_event_type(et.name) == want_always:
            always_et = et
            break
    if always_et and always_et.id != event_type.id and always_et.enabled:
        always_data = {**base, "_trigger": {**meta, "trigger": "always"}}
        events.append(
            ingest_event(
                db,
                source=source,
                event_type_id=always_et.id,
                correlation_id=None,
                raw_payload=json.dumps(always_data),
                normalized_data=always_data,
                touch_last_seen=False,
            )
        )
    return events

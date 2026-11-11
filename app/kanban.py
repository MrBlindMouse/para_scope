"""Kanban board rows and the hand-move event. Pipeline actions call upsert; they do not emit."""
from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.models import EventTypeRecord, KanbanCard, KanbanColumn, Secret, Source

FOCUS_MODES = (
    ("deep", "Deep work"),
    ("people", "People"),
    ("admin", "Admin"),
    ("errands", "Errands"),
)
FOCUS_SLUGS = frozenset(slug for slug, _label in FOCUS_MODES)
FOCUS_LABELS = dict(FOCUS_MODES)

DEFAULT_COLUMNS = (
    ("Next", None),
    ("Doing", 3),
    ("Done", None),
)

_BOARD_RESERVED = "board"


def mode_label(slug: str) -> str:
    return FOCUS_LABELS.get(slug or "", "")


def ensure_columns(db: Session, layout) -> list[KanbanColumn]:
    cols = (
        db.query(KanbanColumn)
        .filter(KanbanColumn.layout_id == layout.id)
        .order_by(KanbanColumn.position, KanbanColumn.id)
        .all()
    )
    if cols:
        return cols
    for index, (name, wip) in enumerate(DEFAULT_COLUMNS):
        db.add(KanbanColumn(
            layout_id=layout.id, name=name, position=index, wip_limit=wip,
        ))
    db.flush()
    return (
        db.query(KanbanColumn)
        .filter(KanbanColumn.layout_id == layout.id)
        .order_by(KanbanColumn.position, KanbanColumn.id)
        .all()
    )


def column_named(cols: list[KanbanColumn], name: str) -> KanbanColumn | None:
    want = (name or "Next").strip().casefold()
    for col in cols:
        if col.name.casefold() == want:
            return col
    return None


def _next_position(db: Session, column_id: int) -> int:
    current = (
        db.query(KanbanCard.position)
        .filter(KanbanCard.column_id == column_id)
        .order_by(KanbanCard.position.desc())
        .first()
    )
    if not current:
        return 0
    return int(current[0]) + 1


def upsert_card(
    db: Session,
    *,
    layout,
    column_name: str,
    title: str,
    mode: str,
    external_key: str,
    remove: bool = False,
) -> str:
    """Create or refresh a card in the entry column. A card you already moved stays put.

    Returns created, updated, kept, removed, missing, or skipped.
    """
    key = (external_key or "").strip()
    if not key:
        return "skipped"
    cols = ensure_columns(db, layout)
    entry = column_named(cols, column_name) or (cols[0] if cols else None)
    if entry is None:
        return "missing"
    card = db.query(KanbanCard).filter(KanbanCard.external_key == key).first()
    if remove:
        if card is None:
            return "missing"
        if card.column_id != entry.id:
            return "kept"
        db.delete(card)
        db.flush()
        return "removed"
    cleaned = (title or "").strip()
    if not cleaned:
        return "skipped"
    focus = mode if mode in FOCUS_SLUGS else ""
    if card is None:
        db.add(KanbanCard(
            column_id=entry.id,
            title=cleaned[:300],
            mode=focus,
            position=_next_position(db, entry.id),
            external_key=key,
        ))
        db.flush()
        return "created"
    card.title = cleaned[:300]
    card.mode = focus
    db.flush()
    if card.column_id == entry.id:
        return "updated"
    return "kept"


def add_card(db: Session, column: KanbanColumn, title: str, mode: str) -> KanbanCard | None:
    cleaned = (title or "").strip()
    if not cleaned:
        return None
    card = KanbanCard(
        column_id=column.id,
        title=cleaned[:300],
        mode=mode if mode in FOCUS_SLUGS else "",
        position=_next_position(db, column.id),
    )
    db.add(card)
    db.flush()
    return card


def move_card(db: Session, card: KanbanCard, column: KanbanColumn) -> tuple[str, str] | None:
    """Move to another column. Returns (from_name, to_name), or None if it stayed."""
    current = card.column
    if current is None or current.id == column.id:
        return None
    origin = current.name
    card.column_id = column.id
    card.position = _next_position(db, column.id)
    db.flush()
    return origin, column.name


def ensure_board_source(db: Session) -> tuple[Source, EventTypeRecord]:
    """Webhook source used only for cards you drag. A random secret blocks outside posts."""
    source = None
    for row in db.query(Source).all():
        if (row.config or {}).get("reserved") == _BOARD_RESERVED:
            source = row
            break
    if source is None:
        slug = "board"
        if db.query(Source).filter(Source.slug == slug).first():
            slug = "board-moves"
        source = Source(
            name="Board",
            slug=slug,
            source_type="webhook",
            description="Kanban cards you move by hand",
            enabled=True,
            config={"reserved": _BOARD_RESERVED, "webhook_provider": "generic_hmac"},
        )
        db.add(source)
        db.flush()
        from app.security import encrypt_secret
        secret = Secret(
            scoped_to_type="source",
            scoped_to_id=source.id,
            encrypted_value=encrypt_secret(secrets.token_hex(32)),
        )
        db.add(secret)
        db.flush()
        source.webhook_secret_id = secret.id
    event_type = (
        db.query(EventTypeRecord)
        .filter(EventTypeRecord.source_id == source.id, EventTypeRecord.name == "card.moved")
        .first()
    )
    if event_type is None:
        event_type = EventTypeRecord(
            source_id=source.id,
            name="card.moved",
            description="A kanban card moved to another column",
            enabled=True,
        )
        db.add(event_type)
        db.flush()
    return source, event_type


def emit_card_moved(db: Session, card: KanbanCard, *, origin: str, dest: str) -> None:
    """Ingest card.moved. kanban_push must not call this."""
    from app.event_stream import publish
    from app.ingest import ingest_manual_events
    from app.pipeline import evaluate_and_dispatch

    source, event_type = ensure_board_source(db)
    payload = {
        "title": card.title,
        "mode": card.mode or "",
        "from": origin,
        "to": dest,
        "external_key": card.external_key or "",
        "card_id": card.id,
    }
    for event in ingest_manual_events(
        db, source, event_type, payload,
        meta={"origin": "kanban", "card_id": card.id},
    ):
        try:
            evaluate_and_dispatch(db, event)
            if event.status != "failed":
                event.status = "processed"
        except Exception as exc:
            event.status = "failed"
            event.processing_error = str(exc)
        db.commit()
        publish(event.id)

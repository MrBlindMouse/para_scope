"""Calendar feed parsing and kanban card identity. No network."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.calendar_sync import apply_feed, match_mode, parse_mode_map
from app.database import Base
from app.ics_feed import parse_ics
from app.kanban import ensure_columns, move_card, upsert_card
from app.models import DashboardLayout, KanbanCard, Source

UTC = timezone.utc
ZONE = ZoneInfo("UTC")


def _window(start: datetime):
    return start - timedelta(days=1), start + timedelta(days=14)


def test_parse_ics_timed_event_and_weekly():
    text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:deep-1
SUMMARY:Deep work
DTSTART:20261016T090000Z
DTEND:20261016T120000Z
CATEGORIES:Deep work
RRULE:FREQ=WEEKLY;BYDAY=FR
END:VEVENT
END:VCALENDAR
"""
    start = datetime(2026, 10, 16, 8, tzinfo=UTC)
    found = parse_ics(text, window_start=start, window_end=start + timedelta(days=14), zone=ZONE)
    assert [item["occurrence"] for item in found] == ["2026-10-16T09:00:00Z", "2026-10-23T09:00:00Z"]
    assert found[0]["title"] == "Deep work"
    assert found[0]["categories"] == ["Deep work"]


def test_match_mode_map_and_label():
    mapping = parse_mode_map("Meetings=people\nnope=not-a-mode\n")
    assert match_mode(["Meetings"], mapping) == "people"
    assert match_mode(["Deep work"], mapping) == "deep"
    assert match_mode(["Lunch"], mapping) == ""


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_apply_feed_emits_edges_once_then_removal():
    db = _session()
    source = Source(name="Cal", slug="cal", source_type="poll", enabled=True)
    db.add(source)
    db.commit()
    now = datetime(2026, 10, 16, 10, tzinfo=UTC)
    item = {
        "uid": "u1",
        "occurrence": "2026-10-16T09:00:00Z",
        "title": "Deep work",
        "starts_at": datetime(2026, 10, 16, 9, tzinfo=UTC),
        "ends_at": datetime(2026, 10, 16, 12, tzinfo=UTC),
        "all_day": False,
        "categories": ["deep"],
    }
    edges, focus = apply_feed(
        db, source.id, [item], now=now, previous_focus="", mode_map={},
    )
    db.commit()
    kinds = [edge["type"] for edge in edges]
    assert kinds == ["item.upsert", "focus.changed"]
    assert focus == "u1|2026-10-16T09:00:00Z"
    assert edges[0]["data"]["mode"] == "deep"

    again, focus_again = apply_feed(
        db, source.id, [item], now=now, previous_focus=focus, mode_map={},
    )
    assert again == []
    assert focus_again == focus

    removed, cleared = apply_feed(
        db, source.id, [], now=now, previous_focus=focus, mode_map={},
    )
    assert [edge["type"] for edge in removed] == ["item.removed", "focus.changed"]
    assert cleared == ""
    assert removed[1]["data"]["mode"] == ""


def test_ics_poller_emits_one_upsert_per_new_occurrence(monkeypatch):
    db = _session()
    source = Source(name="Cal", slug="calfeed", source_type="poll", enabled=True)
    db.add(source)
    db.flush()
    from app.models import Event, EventTypeRecord, PollingSchedule

    schedule = PollingSchedule(
        source_id=source.id,
        name="Cal",
        schedule_type="interval",
        interval_seconds=900,
        handler_type="ics_calendar",
        handler_url="https://example.test/basic.ics",
        handler_params={},
        enabled=True,
    )
    db.add(schedule)
    db.flush()
    for name in ("item.upsert", "item.removed", "focus.changed"):
        db.add(EventTypeRecord(source_id=source.id, name=name, enabled=True))
    db.commit()

    class Resp:
        status_code = 200
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:block-1
SUMMARY:Admin
DTSTART:20200101T090000Z
DTEND:20200101T100000Z
RRULE:FREQ=DAILY
CATEGORIES:admin
END:VEVENT
END:VCALENDAR
"""

    monkeypatch.setattr("app.calendar_sync.httpx.get", lambda *args, **kwargs: Resp())
    from app.pollers import ics_calendar_poll

    ics_calendar_poll(schedule, db)
    first = db.query(Event).filter(Event.source_id == source.id).count()
    assert first > 0
    ics_calendar_poll(schedule, db)
    second = db.query(Event).filter(Event.source_id == source.id).count()
    assert second == first


def test_upsert_card_does_not_move_a_pulled_card():
    db = _session()
    source = Source(name="unused", slug="unused", source_type="poll", enabled=True)
    db.add(source)
    layout = DashboardLayout(name="Work", slug="work", kind="kanban", layout_config={"widgets": []})
    db.add(layout)
    db.flush()
    cols = ensure_columns(db, layout)
    doing = next(col for col in cols if col.name == "Doing")
    status = upsert_card(
        db, layout=layout, column_name="Next", title="Deep work",
        mode="deep", external_key="u1-2026-10-16T09:00:00Z",
    )
    assert status == "created"
    card = db.query(KanbanCard).one()
    moved = move_card(db, card, doing)
    assert moved == ("Next", "Doing")

    again = upsert_card(
        db, layout=layout, column_name="Next", title="Deep work renamed",
        mode="deep", external_key="u1-2026-10-16T09:00:00Z",
    )
    assert again == "kept"
    db.refresh(card)
    assert card.column_id == doing.id
    assert card.title == "Deep work renamed"

    removed = upsert_card(
        db, layout=layout, column_name="Next", title="",
        mode="", external_key="u1-2026-10-16T09:00:00Z", remove=True,
    )
    assert removed == "kept"
    assert db.query(KanbanCard).count() == 1

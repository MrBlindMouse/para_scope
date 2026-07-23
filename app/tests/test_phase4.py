"""Phase 4 DESIGN stretch: action/poller registries, jitter/backoff, http_forward, async webhook."""
import json
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import sessionmaker


def test_action_types_registered():
    from app.actions import get_action_types
    types = get_action_types()
    assert types == ["field_push", "http_forward", "web_push"]


def test_web_push_skips_without_subscriptions(db_session_factory, monkeypatch):
    from app.actions import run_registered_action
    from app.models import ActionInstance, Event, Source

    monkeypatch.setenv("PARA_SCOPE_VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setenv("PARA_SCOPE_VAPID_PRIVATE_KEY", "test-private")

    db = db_session_factory()
    try:
        src = Source(name="PushSrc", slug="push-src", source_type="webhook", enabled=True)
        db.add(src)
        db.commit()
        db.refresh(src)
        event = Event(
            source_id=src.id, correlation_id="c1", raw_payload="{}",
            normalized_data={"msg": "hi"}, status="pending",
        )
        action = ActionInstance(source_id=src.id, action_type="web_push",
            config={"title": "T", "body": "{{msg}}"}, enabled=True,
        )
        db.add_all([event, action])
        db.commit()
        db.refresh(event)
        db.refresh(action)
        # No subscriptions → no-op success
        run_registered_action(db, event, action)
    finally:
        db.close()


def test_web_push_requires_vapid(db_session_factory, monkeypatch):
    from app.actions import run_registered_action
    from app.models import ActionInstance, Event, Source

    monkeypatch.delenv("PARA_SCOPE_VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("PARA_SCOPE_VAPID_PRIVATE_KEY", raising=False)

    db = db_session_factory()
    try:
        src = Source(name="PushSrc2", slug="push-src2", source_type="webhook", enabled=True)
        db.add(src)
        db.commit()
        db.refresh(src)
        event = Event(
            source_id=src.id, correlation_id="c2", raw_payload="{}",
            normalized_data={}, status="pending",
        )
        action = ActionInstance(source_id=src.id, action_type="web_push",
            config={}, enabled=True,
        )
        db.add_all([event, action])
        db.commit()
        with pytest.raises(ValueError, match="VAPID"):
            run_registered_action(db, event, action)
    finally:
        db.close()


def test_http_forward_posts_event_payload(db_session_factory):
    from app.actions import run_registered_action
    from app.models import ActionInstance, Event, Source

    db = db_session_factory()
    try:
        src = Source(name="Fwd", slug="fwd", source_type="generic", enabled=True)
        db.add(src)
        db.commit()
        db.refresh(src)

        event = Event(
            source_id=src.id,
            correlation_id="c1",
            raw_payload="{}",
            normalized_data={"temp": 42},
            status="pending",
        )
        db.add(event)
        db.commit()
        db.refresh(event)

        action = ActionInstance(source_id=src.id, action_type="http_forward",
            config={"url": "https://hooks.example/ingest", "method": "POST"},
            enabled=True,
        )
        db.add(action)
        db.commit()

        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        real_client = httpx.Client

        def make_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(**kwargs)

        with patch("app.actions.httpx.Client", side_effect=make_client):
            run_registered_action(db, event, action)

        assert len(captured) == 1
        req = captured[0]
        assert req.method == "POST"
        assert str(req.url) == "https://hooks.example/ingest"
        body = json.loads(req.content)
        assert body["event_id"] == event.id
        assert body["source_id"] == src.id
        assert body["correlation_id"] == "c1"
        assert body["data"]["temp"] == 42
    finally:
        db.close()


def test_http_forward_requires_url(db_session_factory):
    from app.actions import run_registered_action
    from app.models import ActionInstance, Event, Source

    db = db_session_factory()
    try:
        src = Source(name="NoUrl", slug="nourl", source_type="generic", enabled=True)
        db.add(src)
        db.commit()
        event = Event(
            source_id=src.id, correlation_id="c2", raw_payload="{}",
            normalized_data={}, status="pending",
        )
        action = ActionInstance(source_id=src.id, action_type="http_forward", config={}, enabled=True,
        )
        db.add_all([event, action])
        db.commit()
        with pytest.raises(ValueError, match="url"):
            run_registered_action(db, event, action)
    finally:
        db.close()


def test_unknown_poller_records_last_error(db_session_factory, source_id):
    from app.pollers import run_schedule
    from app.models import PollingSchedule, ScheduleType
    import app.database as database
    import app.pollers as pollers

    db = db_session_factory()
    try:
        sched = PollingSchedule(
            source_id=source_id,
            name="bad-handler",
            schedule_type=ScheduleType.INTERVAL,
            interval_seconds=60,
            handler_type="ftp_poll",
            handler_url="https://example.com",
            handler_params={},
            enabled=True,
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)

        TestSession = sessionmaker(bind=db.get_bind())
        database.SessionLocal = TestSession
        pollers.SessionLocal = TestSession

        ok = run_schedule(sched.id)
        assert ok is False

        s2 = TestSession()
        try:
            updated = s2.query(PollingSchedule).filter_by(id=sched.id).first()
            assert "Unknown poller handler_type" in updated.last_error
            assert updated.failure_count == 1
        finally:
            s2.close()
    finally:
        db.close()


def test_interval_jitter_and_backoff():
    from app.scheduler import (
        _trigger_for,
        backoff_multiplier,
        clear_consecutive_failures,
        interval_jitter_seconds,
        record_poll_outcome,
    )
    from app.models import PollingSchedule, ScheduleType

    clear_consecutive_failures()
    schedule = PollingSchedule(
        id=99901,
        source_id=1,
        name="jitter",
        schedule_type=ScheduleType.INTERVAL,
        interval_seconds=100,
        handler_type="http_get",
        handler_url="https://example.com",
        enabled=True,
    )

    assert interval_jitter_seconds(100) == 10
    assert interval_jitter_seconds(5) == 1
    assert interval_jitter_seconds(400) == 30

    assert backoff_multiplier(0) == 1
    assert backoff_multiplier(3) == 8
    assert backoff_multiplier(5) == 16
    assert backoff_multiplier(10) == 16

    trigger = _trigger_for(schedule, consecutive=0)
    assert trigger.jitter == 10
    assert trigger.interval.total_seconds() == 100

    trigger_bo = _trigger_for(schedule, consecutive=3)
    assert trigger_bo.interval.total_seconds() == 800
    assert trigger_bo.jitter == 10

    assert record_poll_outcome(99901, False) == 1
    assert record_poll_outcome(99901, False) == 2
    assert record_poll_outcome(99901, True) == 0
    clear_consecutive_failures(99901)


def test_sqlite_wal_pragma():
    from sqlalchemy import create_engine, event, text
    from app.database import set_sqlite_pragma

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", set_sqlite_pragma)
    with engine.connect() as conn:
        # busy_timeout is set; WAL may be no-op on :memory: but pragma should succeed
        busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
        assert busy == 5000
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert mode.lower() in ("wal", "memory", "delete")


# ── fixtures shared with isolated DB ────────────────────────────────────────

@pytest.fixture()
def db_session_factory(tmp_path, monkeypatch):
    """Session factory bound to a temp SQLite file with schema."""
    from sqlalchemy import create_engine
    from app.database import Base, ensure_schema
    from app import models  # noqa: F401

    db_path = tmp_path / "phase4.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    # ensure_schema uses the app engine — patch lightly by applying patches here
    Session = sessionmaker(bind=engine)

    def factory():
        return Session()

    yield factory
    engine.dispose()


@pytest.fixture()
def source_id(db_session_factory):
    from app.models import Source
    db = db_session_factory()
    try:
        src = Source(name="P4", slug="p4", source_type="generic", enabled=True)
        db.add(src)
        db.commit()
        db.refresh(src)
        return src.id
    finally:
        db.close()

"""Tests for the polling engine (Phase 3)."""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Use an isolated SQLite DB before importing app modules that bind to para_scope.db
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["PARA_SCOPE_DATABASE_URL"] = f"sqlite:///{_tmp.name}"


@pytest.fixture()
def db():
    """Fresh in-memory-ish SQLite session with all tables."""
    from app.database import Base, ensure_schema
    from app import models  # noqa: F401 — register models

    engine = create_engine(
        f"sqlite:///{_tmp.name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def source(db):
    from app.models import Source
    src = Source(name="Test Source", slug="test-source", source_type="generic", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def _make_schedule(db, source, **kwargs):
    from app.models import PollingSchedule, ScheduleType
    defaults = dict(
        source_id=source.id,
        name="poll-me",
        schedule_type=ScheduleType.INTERVAL,
        interval_seconds=3600,
        handler_type="http_get",
        handler_url="https://example.com/api",
        handler_params={},
        timeout_seconds=10,
        retry_count=0,
        enabled=True,
    )
    defaults.update(kwargs)
    sched = PollingSchedule(**defaults)
    db.add(sched)
    db.commit()
    db.refresh(sched)
    return sched


# ── get_by_path (poll json_path) ────────────────────────────────────────────

def test_extract_path_nested():
    from app.fields import get_by_path
    data = {"data": {"items": [{"id": 1}, {"id": 2}]}}
    assert get_by_path(data, "data.items.0.id") == 1
    assert get_by_path(data, "data.items") == [{"id": 1}, {"id": 2}]
    assert get_by_path(data, "") == data
    assert get_by_path(data, "missing.path") is None


# ── http_poll ───────────────────────────────────────────────────────────────

def test_http_poll_success(db, source):
    from app.pollers import http_poll

    schedule = _make_schedule(db, source, handler_params={"json_path": "payload"})

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"payload": {"orders": 3}, "noise": True}
    mock_response.text = '{"payload": {"orders": 3}}'
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.return_value = mock_response

    with patch("app.pollers.httpx.Client", return_value=mock_client):
        result = http_poll(schedule, db)

    assert result["ok"] is True
    assert result["data"] == {"orders": 3}
    assert result["status_code"] == 200
    assert isinstance(result["response_time_ms"], (int, float))
    assert result["response_time_ms"] >= 0
    mock_client.request.assert_called_once()
    call_kwargs = mock_client.request.call_args.kwargs
    assert call_kwargs["method"] == "GET"
    assert call_kwargs["url"] == "https://example.com/api"


def test_http_poll_retries_then_fails(db, source):
    from app.pollers import http_poll
    import httpx

    schedule = _make_schedule(db, source, retry_count=2)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.side_effect = httpx.ConnectError("down")

    with patch("app.pollers.httpx.Client", return_value=mock_client):
        with pytest.raises(httpx.ConnectError):
            http_poll(schedule, db)

    assert mock_client.request.call_count == 3  # 1 + 2 retries


def test_http_poll_requires_url(db, source):
    from app.pollers import http_poll
    schedule = _make_schedule(db, source, handler_url="")
    with pytest.raises(ValueError, match="Poll URL"):
        http_poll(schedule, db)


# ── run_schedule ────────────────────────────────────────────────────────────

def test_run_schedule_creates_event(db, source):
    from app.pollers import run_schedule
    from app.models import Event
    from app.database import SessionLocal
    import app.database as database
    import app.pollers as pollers

    schedule = _make_schedule(db, source, handler_params={"event_type": ""})

    # Point SessionLocal at our test DB
    TestSession = sessionmaker(bind=db.get_bind())
    database.SessionLocal = TestSession
    pollers.SessionLocal = TestSession

    mock_result = {
        "ok": True,
        "status_code": 200,
        "data": {"temperature": 21.5},
        "raw": '{"temperature": 21.5}',
        "attempts": 1,
    }

    with patch("app.pollers.http_poll", return_value=mock_result):
        run_schedule(schedule.id)

    events = db.query(Event).all()
    # run_schedule uses its own session — re-query via TestSession
    s2 = TestSession()
    try:
        events = s2.query(Event).all()
        assert len(events) == 1
        assert events[0].normalized_data["temperature"] == 21.5
        assert events[0].normalized_data["_poll"]["schedule_id"] == schedule.id
        assert events[0].normalized_data["source"] == source.name
        assert events[0].normalized_data["_poll"]["response_time_ms"] is not None
        assert events[0].normalized_data["_poll"]["timestamp"]
        assert events[0].status == "processed"

        updated = s2.query(type(schedule)).filter_by(id=schedule.id).first()
        assert updated.success_count == 1
        assert updated.failure_count == 0
        assert updated.last_run_at is not None
        assert updated.last_error == ""
    finally:
        s2.close()


def test_run_schedule_records_failure(db, source):
    from app.pollers import run_schedule
    from app.models import Event, EventTypeRecord, PollingSchedule
    import app.database as database
    import app.pollers as pollers

    db.add(EventTypeRecord(
        source_id=source.id, name="on_failure", description="fail",
    ))
    db.commit()

    schedule = _make_schedule(db, source)
    TestSession = sessionmaker(bind=db.get_bind())
    database.SessionLocal = TestSession
    pollers.SessionLocal = TestSession

    with patch("app.pollers.http_poll", side_effect=RuntimeError("boom")):
        run_schedule(schedule.id)

    s2 = TestSession()
    try:
        updated = s2.query(PollingSchedule).filter_by(id=schedule.id).first()
        assert updated.failure_count == 1
        assert updated.success_count == 0
        assert "boom" in updated.last_error
        assert updated.last_run_at is not None

        events = s2.query(Event).all()
        assert len(events) == 1
        assert events[0].normalized_data.get("error") == "boom"
        assert events[0].normalized_data["_poll"]["outcome"] == "on_failure"
        et = s2.query(EventTypeRecord).filter_by(id=events[0].event_type_id).first()
        assert et is not None
        assert et.name == "on_failure"
    finally:
        s2.close()


def test_run_schedule_emits_always_when_present(db, source):
    from app.pollers import run_schedule
    from app.models import Event, EventTypeRecord
    import app.database as database
    import app.pollers as pollers

    db.add(EventTypeRecord(source_id=source.id, name="on_success", description=""))
    db.add(EventTypeRecord(source_id=source.id, name="always", description="every run"))
    db.commit()

    schedule = _make_schedule(db, source)
    TestSession = sessionmaker(bind=db.get_bind())
    database.SessionLocal = TestSession
    pollers.SessionLocal = TestSession

    mock_result = {
        "ok": True,
        "status_code": 200,
        "data": {"v": 1},
        "raw": '{"v": 1}',
        "attempts": 1,
    }
    with patch("app.pollers.http_poll", return_value=mock_result):
        run_schedule(schedule.id)

    s2 = TestSession()
    try:
        events = s2.query(Event).all()
        assert len(events) == 2
        names = {
            s2.query(EventTypeRecord).filter_by(id=e.event_type_id).first().name
            for e in events
        }
        assert names == {"on_success", "always"}
        always = next(
            e for e in events
            if s2.query(EventTypeRecord).filter_by(id=e.event_type_id).first().name == "always"
        )
        assert always.normalized_data["_poll"]["outcome"] == "on_success"
        assert always.normalized_data["_poll"]["trigger"] == "always"
    finally:
        s2.close()


def test_run_schedule_skips_always_when_absent(db, source):
    from app.pollers import run_schedule
    from app.models import Event
    import app.database as database
    import app.pollers as pollers

    schedule = _make_schedule(db, source)
    TestSession = sessionmaker(bind=db.get_bind())
    database.SessionLocal = TestSession
    pollers.SessionLocal = TestSession

    with patch("app.pollers.http_poll", return_value={
        "ok": True, "status_code": 200, "data": {}, "raw": "{}", "attempts": 1,
    }):
        run_schedule(schedule.id)

    s2 = TestSession()
    try:
        # Only the default on_success path (may be untyped if type missing) — not a second always
        assert s2.query(Event).count() == 1
    finally:
        s2.close()


# ── scheduler job registration ──────────────────────────────────────────────

def test_scheduler_add_and_remove_job(db, source):
    from app.scheduler import (
        start_scheduler, stop_scheduler, add_or_update_job, remove_job, job_count, get_scheduler,
    )
    import app.database as database
    import app.scheduler as scheduler_mod

    TestSession = sessionmaker(bind=db.get_bind())
    database.SessionLocal = TestSession
    scheduler_mod.SessionLocal = TestSession

    # Ensure clean slate
    stop_scheduler()
    start_scheduler()
    try:
        assert get_scheduler() is not None
        schedule = _make_schedule(db, source, interval_seconds=60)
        add_or_update_job(schedule)
        assert job_count() >= 1

        remove_job(schedule.id)
        # May still have jobs from start_scheduler if any were in DB; our job should be gone
        sched = get_scheduler()
        assert sched.get_job(f"poll_{schedule.id}") is None
    finally:
        stop_scheduler()


def test_trigger_rejects_bad_interval(db, source):
    from app.scheduler import _trigger_for
    schedule = _make_schedule(db, source, interval_seconds=0)
    with pytest.raises(ValueError, match="interval_seconds"):
        _trigger_for(schedule)

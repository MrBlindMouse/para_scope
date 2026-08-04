"""Tests for secrets encryption and webhook event-type validation."""
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()


@pytest.fixture()
def db():
    from app.database import Base
    from app import models  # noqa: F401

    engine = create_engine(f"sqlite:///{_tmp.name}", connect_args={"check_same_thread": False})
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@contextmanager
def _webhook_client(db):
    """Override get_db for webhook tests; restore SessionLocal afterward.

    Patch every module that holds its own SessionLocal binding — scheduler
    imports it at module load, so database-only patches miss lifespan startup.
    """
    from fastapi.testclient import TestClient
    import app.database as database
    import app.main as main_mod
    import app.pollers as pollers
    import app.scheduler as scheduler_mod

    TestSession = sessionmaker(bind=db.get_bind())
    previous = [
        (database, database.SessionLocal),
        (pollers, pollers.SessionLocal),
        (scheduler_mod, scheduler_mod.SessionLocal),
    ]

    def override_get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    for mod, _ in previous:
        mod.SessionLocal = TestSession
    main_mod.app.dependency_overrides[database.get_db] = override_get_db
    try:
        with TestClient(main_mod.app) as client:
            yield client, TestSession
    finally:
        main_mod.app.dependency_overrides.clear()
        for mod, prev in previous:
            mod.SessionLocal = prev


# ── AES secrets ─────────────────────────────────────────────────────────────

def test_encrypt_without_key_raises():
    with patch.dict(os.environ, {"PARA_SCOPE_SECRET_KEY": ""}, clear=False):
        # Clear the key specifically
        os.environ.pop("PARA_SCOPE_SECRET_KEY", None)
        import importlib
        import app.security as sec
        importlib.reload(sec)
        try:
            with pytest.raises(ValueError, match="PARA_SCOPE_SECRET_KEY"):
                sec.encrypt_secret("super-secret")
        finally:
            os.environ["PARA_SCOPE_SECRET_KEY"] = "test-secret-key-for-pytest"
            importlib.reload(sec)


def test_encrypt_decrypt_aes_roundtrip():
    with patch.dict(os.environ, {"PARA_SCOPE_SECRET_KEY": "test-key-for-aes"}):
        import importlib
        import app.security as sec
        importlib.reload(sec)
        try:
            blob = sec.encrypt_secret("super-secret")
            assert blob.startswith("aes:")
            assert sec.decrypt_secret(blob) == "super-secret"
        finally:
            os.environ["PARA_SCOPE_SECRET_KEY"] = "test-secret-key-for-pytest"
            importlib.reload(sec)


def test_decrypt_rejects_non_aes():
    with patch.dict(os.environ, {"PARA_SCOPE_SECRET_KEY": "test-key-for-aes"}):
        import importlib
        import app.security as sec
        importlib.reload(sec)
        try:
            with pytest.raises(ValueError, match="aes:"):
                sec.decrypt_secret("xor:not-supported")
        finally:
            os.environ["PARA_SCOPE_SECRET_KEY"] = "test-secret-key-for-pytest"
            importlib.reload(sec)


# ── Webhook event type validation ───────────────────────────────────────────

def test_webhook_requires_event_type_when_registered(db):
    from app.models import Source, EventTypeRecord

    src = Source(name="API", slug="api", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="order.paid"))
    db.commit()

    with _webhook_client(db) as (client, _):
        r = client.post("/webhook/api", json={"amount": 10})
        assert r.status_code == 400
        assert "Event type required" in r.json()["error"]

        r = client.post(
            "/webhook/api",
            json={"amount": 10},
            headers={"X-Event-Type": "order.paid"},
        )
        assert r.status_code == 202

        r = client.post(
            "/webhook/api",
            json={"event_type": "order.paid", "amount": 11},
        )
        assert r.status_code == 202

        r = client.post(
            "/webhook/api",
            json={"amount": 10},
            headers={"X-Event-Type": "nope"},
        )
        assert r.status_code == 400


@pytest.mark.parametrize(
    "header_name,header_value",
    [
        ("X-Contentful-Topic", "ContentManagement.Entry.publish"),
        ("Toast-Event-Type", "partner_added"),
        ("Kick-Event-Type", "chat.message.sent"),
        ("X-Webhook-Event", "order.paid"),
        ("X-Webhook-Event-Type", "order.paid"),
        ("X-Gitlab-Event", "Push Hook"),
        ("X-Shopify-Topic", "orders/create"),
        ("X-Gitea-Event", "push"),
    ],
)
def test_webhook_accepts_provider_event_type_headers(db, header_name, header_value):
    from app.models import Source, EventTypeRecord
    from app.pipeline import normalize_event_type

    et = normalize_event_type(header_value)
    src = Source(name="Prov", slug="prov-et", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name=et))
    db.commit()

    with _webhook_client(db) as (client, _):
        r = client.post(
            "/webhook/prov-et",
            json={"ok": True},
            headers={header_name: header_value},
        )
        assert r.status_code == 202, r.text


def test_webhook_event_type_header_priority(db):
    """X-Event-Type wins over a later alias when both are present."""
    from app.models import Source, EventTypeRecord, Event

    src = Source(name="Prio", slug="prio-et", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="from-x-event-type"))
    db.add(EventTypeRecord(source_id=src.id, name="from-alias"))
    db.commit()

    with _webhook_client(db) as (client, TestSession):
        r = client.post(
            "/webhook/prio-et",
            json={"ok": True},
            headers={
                "X-Event-Type": "from-x-event-type",
                "X-Webhook-Event-Type": "from-alias",
            },
        )
        assert r.status_code == 202
        s2 = TestSession()
        try:
            event = s2.query(Event).filter(Event.source_id == src.id).one()
            et = s2.query(EventTypeRecord).filter_by(id=event.event_type_id).one()
            assert et.name == "from-x-event-type"
        finally:
            s2.close()


def test_webhook_allows_untyped_when_no_registry(db):
    from app.models import Source

    src = Source(name="Raw", slug="raw", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()

    with _webhook_client(db) as (client, _):
        r = client.post("/webhook/raw", json={"ping": True})
        assert r.status_code == 202


def test_webhook_emits_always_when_present(db):
    from app.models import Source, EventTypeRecord, Event

    src = Source(name="Hook", slug="hook-always", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="order.paid"))
    db.add(EventTypeRecord(source_id=src.id, name="always", description="every delivery"))
    db.commit()

    with _webhook_client(db) as (client, TestSession):
        r = client.post(
            "/webhook/hook-always",
            json={"amount": 5},
            headers={"X-Event-Type": "order.paid"},
        )
        assert r.status_code == 202
        body = r.json()
        assert "event_id" in body
        assert "always_event_id" in body
        assert body["event_id"] != body["always_event_id"]

        s2 = TestSession()
        try:
            events = s2.query(Event).filter(Event.source_id == src.id).all()
            assert len(events) == 2
            names = {
                s2.query(EventTypeRecord).filter_by(id=e.event_type_id).first().name
                for e in events
            }
            assert names == {"order.paid", "always"}
            always = next(
                e for e in events
                if s2.query(EventTypeRecord).filter_by(id=e.event_type_id).first().name == "always"
            )
            assert always.normalized_data["_webhook"]["trigger"] == "always"
            assert always.normalized_data["_webhook"]["event_type"] == "order.paid"
            assert always.normalized_data["amount"] == 5
        finally:
            s2.close()


def test_webhook_skips_always_when_absent(db):
    from app.models import Source, EventTypeRecord, Event

    src = Source(name="Hook2", slug="hook-no-always", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="ping"))
    db.commit()

    with _webhook_client(db) as (client, TestSession):
        r = client.post(
            "/webhook/hook-no-always",
            json={"ok": True},
            headers={"X-Event-Type": "ping"},
        )
        assert r.status_code == 202
        assert "always_event_id" not in r.json()

        s2 = TestSession()
        try:
            assert s2.query(Event).filter(Event.source_id == src.id).count() == 1
        finally:
            s2.close()


def test_webhook_always_alone_does_not_require_type(db):
    """`always` is a side-emission; alone it must not force producers to declare a type."""
    from app.models import Source, EventTypeRecord, Event

    src = Source(name="Hook3", slug="hook-only-always", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="always"))
    db.commit()

    with _webhook_client(db) as (client, TestSession):
        r = client.post("/webhook/hook-only-always", json={"ping": 1})
        assert r.status_code == 202
        body = r.json()
        assert body.get("always_event_id")

        s2 = TestSession()
        try:
            events = s2.query(Event).filter(Event.source_id == src.id).all()
            assert len(events) == 2  # untyped primary + always
            typed = [e for e in events if e.event_type_id is not None]
            assert len(typed) == 1
            assert typed[0].normalized_data["_webhook"]["trigger"] == "always"
        finally:
            s2.close()


def test_webhook_case_insensitive_match(db):
    from app.models import Source, EventTypeRecord, Event

    src = Source(name="Case", slug="hook-case", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="order.paid"))
    db.commit()

    with _webhook_client(db) as (client, TestSession):
        r = client.post(
            "/webhook/hook-case",
            json={"amount": 1},
            headers={"X-Event-Type": "Order.Paid"},
        )
        assert r.status_code == 202
        s2 = TestSession()
        try:
            ev = s2.query(Event).filter(Event.id == r.json()["event_id"]).first()
            assert ev.normalized_data["_webhook"]["event_type"] == "order.paid"
        finally:
            s2.close()


def test_webhook_only_always_accepts_generic_body_type(db):
    """Unmatched body `type` must not 400 when only `always` is registered."""
    from app.models import Source, EventTypeRecord, Event

    src = Source(name="Sensor", slug="hook-sensor", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="always"))
    db.commit()

    with _webhook_client(db) as (client, TestSession):
        r = client.post(
            "/webhook/hook-sensor",
            json={"type": "temperature", "celsius": 21},
        )
        assert r.status_code == 202
        s2 = TestSession()
        try:
            primary = s2.query(Event).filter(Event.id == r.json()["event_id"]).first()
            assert primary.event_type_id is None
            assert primary.normalized_data["_webhook"]["event_type"] is None
            assert primary.normalized_data["type"] == "temperature"
        finally:
            s2.close()


def test_webhook_rejects_unknown_type_when_producers_exist(db):
    from app.models import Source, EventTypeRecord

    src = Source(name="Producers", slug="hook-producers", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="always"))
    db.add(EventTypeRecord(source_id=src.id, name="order.paid"))
    db.commit()

    with _webhook_client(db) as (client, _):
        r = client.post(
            "/webhook/hook-producers",
            json={"type": "temperature"},
        )
        assert r.status_code == 400
        assert "not found" in r.json()["error"]


def test_webhook_github_event_header(db):
    from app.models import Source, EventTypeRecord, Event

    src = Source(name="GH", slug="hook-gh", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="push"))
    db.commit()

    with _webhook_client(db) as (client, TestSession):
        r = client.post(
            "/webhook/hook-gh",
            json={"ref": "refs/heads/main"},
            headers={"X-GitHub-Event": "Push"},
        )
        assert r.status_code == 202
        s2 = TestSession()
        try:
            ev = s2.query(Event).filter(Event.id == r.json()["event_id"]).first()
            assert ev.normalized_data["_webhook"]["event_type"] == "push"
        finally:
            s2.close()


def test_normalize_event_type_helper():
    from app.pipeline import normalize_event_type, EVENT_TYPE_MAX_LEN
    assert normalize_event_type(" Order.Paid ") == "order.paid"
    assert normalize_event_type("PAYMENT.SALE.COMPLETED") == "payment.sale.completed"
    assert normalize_event_type(None) == ""
    assert normalize_event_type("  ") == ""
    assert EVENT_TYPE_MAX_LEN == 200

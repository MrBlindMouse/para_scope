"""Tests for secrets encryption, webhook event-type validation, and metrics API."""
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()


@pytest.fixture()
def db():
    from app.database import Base, ensure_schema
    from app import models  # noqa: F401

    engine = create_engine(f"sqlite:///{_tmp.name}", connect_args={"check_same_thread": False})
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ── AES / XOR secrets ───────────────────────────────────────────────────────

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


def test_decrypt_legacy_xor():
    with patch.dict(os.environ, {"PARA_SCOPE_SECRET_KEY": "legacy-key"}):
        import importlib
        import app.security as sec
        importlib.reload(sec)
        try:
            # Simulate pre-prefix XOR blob
            legacy = sec._xor_encrypt("old-value")
            assert not legacy.startswith("aes:")
            assert sec.decrypt_secret(legacy) == "old-value"
            # Tagged XOR also works
            tagged = "xor:" + legacy
            assert sec.decrypt_secret(tagged) == "old-value"
        finally:
            os.environ["PARA_SCOPE_SECRET_KEY"] = "test-secret-key-for-pytest"
            importlib.reload(sec)


# ── Webhook event type validation ───────────────────────────────────────────

def test_webhook_requires_event_type_when_registered(db):
    from fastapi.testclient import TestClient
    from app.models import Source, EventTypeRecord
    import app.database as database
    import app.main as main_mod

    src = Source(name="API", slug="api", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(EventTypeRecord(source_id=src.id, name="order.paid"))
    db.commit()

    TestSession = sessionmaker(bind=db.get_bind())
    database.SessionLocal = TestSession
    main_mod.get_db = lambda: (yield TestSession())

    # Patch get_db properly
    def override_get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    try:
        with TestClient(main_mod.app) as client:
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
    finally:
        main_mod.app.dependency_overrides.clear()


def test_webhook_allows_untyped_when_no_registry(db):
    from fastapi.testclient import TestClient
    from app.models import Source
    import app.database as database
    import app.main as main_mod

    src = Source(name="Raw", slug="raw", source_type="webhook", enabled=True)
    db.add(src)
    db.commit()

    TestSession = sessionmaker(bind=db.get_bind())

    def override_get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    try:
        with TestClient(main_mod.app) as client:
            r = client.post("/webhook/raw", json={"ping": True})
            assert r.status_code == 202
    finally:
        main_mod.app.dependency_overrides.clear()


# ── Multi-series metrics API ────────────────────────────────────────────────

def test_metrics_api_multi_series(db):
    from fastapi.testclient import TestClient
    from app.models import Source, MetricPoint, User
    from app.security import hash_password
    import app.main as main_mod

    src = Source(name="S", slug="s", source_type="generic", enabled=True)
    db.add(src)
    db.commit()
    db.refresh(src)
    now = datetime.now(timezone.utc)
    db.add(MetricPoint(source_id=src.id, name="orders", value=1.0, timestamp=now))
    db.add(MetricPoint(source_id=src.id, name="revenue", value=9.5, timestamp=now))
    db.add(User(username="u", hashed_password=hash_password("p")))
    db.commit()

    TestSession = sessionmaker(bind=db.get_bind())

    def override_get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    try:
        with TestClient(main_mod.app) as client:
            client.post("/login", data={"username": "u", "password": "p"})
            r = client.get("/metrics/api?name=orders,revenue&range=24")
            assert r.status_code == 200
            body = r.json()
            assert "series" in body
            assert len(body["series"]) == 2
            names = {s["name"] for s in body["series"]}
            assert names == {"orders", "revenue"}
    finally:
        main_mod.app.dependency_overrides.clear()

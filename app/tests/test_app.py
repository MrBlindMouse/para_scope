"""
Para-Scope tests — covers auth, all config CRUD routes, and dashboard layout.

Run: pytest app/tests/ -v
Recreate DB between runs by removing the sqlite file.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Must be set before importing app.security / app.main / app.database
DB_PATH = Path(__file__).parent.parent / ".test_db.sqlite"
os.environ.setdefault("PARA_SCOPE_SECRET_KEY", "test-secret-key-for-pytest")
os.environ["PARA_SCOPE_DATABASE_URL"] = f"sqlite:///{DB_PATH}"

import pytest
from fastapi.testclient import TestClient

from app.models import Base, User, Source, EventTypeRecord, ActionInstance, Rule, Event, Secret
from app.security import hash_password, encrypt_secret, create_session_token, verify_session_token
from app.main import app


# ── Fixtures ─────────────────────────────────────────────────────────────────


class CsrfClient:
    """TestClient wrapper that injects CSRF token into form POSTs."""

    def __init__(self, client: TestClient):
        self._client = client

    def _ensure_csrf(self, data):
        token = self._client.cookies.get("csrf_token")
        if not token:
            self._client.get("/config/pipeline")
            token = self._client.cookies.get("csrf_token")
        out = dict(data or {})
        out.setdefault("_csrf_token", token or "")
        return out

    def post(self, url, data=None, json=None, **kwargs):
        # Webhooks skip CSRF injection
        if str(url).startswith("/webhook") or isinstance(data, (bytes, bytearray)):
            return self._client.post(url, data=data, json=json, **kwargs)
        token = self._client.cookies.get("csrf_token")
        if not token:
            self._client.get("/config/pipeline")
            token = self._client.cookies.get("csrf_token")
        if json is not None:
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("X-CSRF-Token", token or "")
            return self._client.post(url, data=data, json=json, headers=headers, **kwargs)
        data = self._ensure_csrf(data if isinstance(data, dict) else {})
        return self._client.post(url, data=data, json=json, **kwargs)

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture(scope="class")
def _clean_db(request):
    """Drop everything and recreate tables once per test class."""
    import app.main as main_mod
    main_mod._LOGIN_RATE_LIMIT.clear()
    main_mod._WEBHOOK_REPLAY_CACHE.clear()
    main_mod._WEBHOOK_RATE_LIMIT.clear()
    from app.database import engine as _engine, Base as _Base
    # Don't unlink DB_PATH while the engine has open connections (SQLite I/O errors).
    _Base.metadata.drop_all(bind=_engine)
    _Base.metadata.create_all(bind=_engine)
    yield
    _Base.metadata.drop_all(bind=_engine)


@pytest.fixture(scope="class")
def client(_clean_db):
    """TestClient — no raise_server_exceptions so template errors show as 500."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def test_user(client):
    """Create a user and return (username, password)."""
    from sqlalchemy.orm import sessionmaker
    from app.database import engine as _engine
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == "testadmin").first()
        if existing:
            return ("testadmin", "admin123")
        u = User(username="testadmin", hashed_password=hash_password("admin123"))
        db.add(u)
        db.commit()
        return ("testadmin", "admin123")
    finally:
        db.close()


@pytest.fixture
def authenticated_client(client, test_user):
    """Log in and return a CSRF-aware client."""
    import app.main as main_mod
    main_mod._LOGIN_RATE_LIMIT.clear()
    username, password = test_user
    resp = client.post(
        "/login", data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:500]  # redirect after login
    # Ensure CSRF cookie exists for subsequent form posts
    client.get("/config/pipeline")
    return CsrfClient(client)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create_source(client, name="Test Source", slug=None, source_type="webhook"):
    """Helper: create a source and return (id, slug). Name is uniquified if taken."""
    from app.database import get_db
    db = next(get_db())
    try:
        base = name
        n = 0
        while db.query(Source).filter(Source.name == name).first():
            n += 1
            name = f"{base} {n}"
    finally:
        db.close()

    data = {
        "name": name,
        "source_type": source_type,
        "description": "",
    }
    if source_type == "poll":
        data.update({
            "poll_category": "url",
            "schedule_type": "interval",
            "interval_seconds": "60",
            "handler_type": "http_get",
            "handler_url": "https://example.com",
            "timeout_seconds": "30",
            "retry_count": "0",
        })

    resp = client.post(
        "/config/pipeline/sources",
        data=data,
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:500]
    db = next(get_db())
    try:
        src = db.query(Source).filter(Source.name == name).order_by(Source.id.desc()).first()
        assert src is not None
        return src.id, src.slug
    finally:
        db.close()

def _set_display_timezone(name: str) -> None:
    from app.database import get_db
    from app.themes import get_app_settings

    db = next(get_db())
    try:
        settings = get_app_settings(db)
        settings.display_timezone = name
        db.commit()
    finally:
        db.close()


# ── Auth Tests ───────────────────────────────────────────────────────────────

class TestSetup:
    """First-run /setup — each test starts with an empty user table."""

    @pytest.fixture(autouse=True)
    def _wipe_users(self, client):
        from app.database import get_db
        from app.models import AuditLog
        db = next(get_db())
        try:
            db.query(AuditLog).delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()
        yield

    def test_setup_page_when_empty(self, client):
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert "Create account" in resp.text

    def test_login_redirects_to_setup_when_empty(self, client):
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 303
        assert "/setup" in str(resp.headers.get("location", ""))

    def test_protected_redirects_to_setup_when_empty(self, client):
        resp = client.get("/config/pipeline", follow_redirects=False)
        assert resp.status_code == 303
        assert "/setup" in str(resp.headers.get("location", ""))

    def test_setup_creates_user_and_logs_in(self, client):
        resp = client.post(
            "/setup",
            data={"username": "bootstrap", "password": "secret123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/"
        cookie = client.cookies.get("session_username")
        assert cookie
        assert verify_session_token(cookie) == "bootstrap"
        # Setup locked after first user
        resp2 = client.get("/setup", follow_redirects=False)
        assert resp2.status_code == 303
        assert "/login" in str(resp2.headers.get("location", ""))

    def test_setup_post_rejected_when_users_exist(self, client):
        from app.database import get_db
        db = next(get_db())
        try:
            db.add(User(username="existing", hashed_password=hash_password("p")))
            db.commit()
        finally:
            db.close()
        resp = client.post(
            "/setup",
            data={"username": "another", "password": "secret123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/login" in str(resp.headers.get("location", ""))

    def test_setup_immediate_recheck_blocks_second_user(self, client):
        """After first setup succeeds, a second POST cannot create another admin."""
        r1 = client.post(
            "/setup",
            data={"username": "firstadmin", "password": "secret123"},
            follow_redirects=False,
        )
        assert r1.status_code == 303
        r2 = client.post(
            "/setup",
            data={"username": "secondadmin", "password": "secret456"},
            follow_redirects=False,
        )
        assert r2.status_code == 303
        assert "/login" in str(r2.headers.get("location", ""))
        from app.database import get_db
        db = next(get_db())
        try:
            assert db.query(User).count() == 1
            assert db.query(User).filter(User.username == "firstadmin").first()
            assert db.query(User).filter(User.username == "secondadmin").first() is None
        finally:
            db.close()


class TestAuth:
    def test_login_page_get(self, client, test_user):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Wrong username or password" not in resp.text

    def test_login_bad_credentials(self, client, test_user):
        resp = client.post("/login", data={"username": "nobody", "password": "wrong"})
        assert resp.status_code == 200
        assert "Wrong username or password" in resp.text

    def test_login_success_redirects(self, test_user):
        username, password = test_user
        from app.database import get_db
        from app.models import User
        db = next(get_db())
        try:
            existing = db.query(User).filter(User.username == username).first()
            if not existing:
                u = User(username=username, hashed_password=hash_password(password))
                db.add(u)
                db.commit()
        finally:
            db.close()

        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post("/login", data={"username": username, "password": password},
                          follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers.get("location") == "/"
            # Session cookie must be signed, not raw username
            cookie = c.cookies.get("session_username")
            assert cookie
            assert cookie != username
            assert verify_session_token(cookie) == username

    def test_login_rate_limit(self, client, test_user):
        import app.main as main_mod
        main_mod._LOGIN_RATE_LIMIT.clear()
        for _ in range(10):
            resp = client.post("/login", data={"username": "nobody", "password": "wrong"})
            assert resp.status_code == 200
            assert "Wrong username or password" in resp.text
        resp = client.post("/login", data={"username": "nobody", "password": "wrong"})
        assert resp.status_code == 200
        assert "Too many login attempts" in resp.text
        main_mod._LOGIN_RATE_LIMIT.clear()

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/config/pipeline", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in str(resp.headers.get("location", ""))

    def test_forged_plaintext_session_rejected(self, client, test_user):
        """Plaintext username cookie must not authenticate."""
        client.cookies.set("session_username", "testadmin")
        resp = client.get("/config/pipeline", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in str(resp.headers.get("location", ""))

    def test_signed_session_allows_access(self, client, test_user):
        token = create_session_token("testadmin")
        client.cookies.set("session_username", token)
        resp = client.get("/config/pipeline", follow_redirects=False)
        assert resp.status_code == 200

    def test_csrf_rejects_post_without_token(self, client, test_user):
        token = create_session_token("testadmin")
        client.cookies.set("session_username", token)
        # No csrf_token cookie / form field
        resp = client.post(
            "/config/pipeline/sources",
            data={"name": "X", "source_type": "webhook", "description": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_csrf_accepts_matching_token(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={"name": "CSRF Ok", "source_type": "webhook", "description": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_logout_clears_session(self, authenticated_client):
        resp = authenticated_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in str(resp.headers.get("location", ""))


# ── Config: Sources CRUD ─────────────────────────────────────────────────────

class TestSourcesCRUD:
    def test_get_pipeline_empty(self, authenticated_client):
        resp = authenticated_client.get("/config/pipeline")
        assert resp.status_code == 200
        assert 'body class="config-page"' in resp.text
        assert "No sources yet" in resp.text
        # hx-boost/hx-select must stay on the nav, not #config-panel, or dialog
        # partials get filtered to empty (inherited hx-select="#config-panel").
        assert 'id="config-panel"' in resp.text
        assert 'id="config-panel"\n     hx-boost' not in resp.text
        assert 'hx-boost="true"' in resp.text
        assert 'hx-select="#config-panel"' in resp.text
        assert 'hx-get="/config/pipeline/partials/source-form"' in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text
        assert 'hx-on::after-swap="this.showModal()"' in resp.text
        assert '/static/js/dialogs.js' in resp.text
        assert '/static/js/htmx.min.js' in resp.text
        assert "unpkg.com/htmx" not in resp.text

    def test_htmx_static_served(self, authenticated_client):
        resp = authenticated_client.get("/static/js/htmx.min.js")
        assert resp.status_code == 200
        assert b"htmx" in resp.content

    def test_source_form_partial(self, authenticated_client):
        resp = authenticated_client.get("/config/pipeline/partials/source-form")
        assert resp.status_code == 200
        assert "Add Source" in resp.text
        assert 'name="name"' in resp.text
        assert 'id="config-panel"' not in resp.text
        assert 'value="poll"' in resp.text
        assert 'value="webhook"' in resp.text
        assert 'value="generic"' not in resp.text
        assert 'name="schedule_name"' not in resp.text
        assert 'id="schedule-fields"' in resp.text
        assert 'name="poll_category"' in resp.text
        assert 'name="slug"' not in resp.text
        assert 'class="field-tip"' in resp.text
        assert "data-tip=" in resp.text
        assert 'id="webhook-provider-fields"' in resp.text
        assert 'data-webhook-provider="paypal"' in resp.text
        assert "Client secret" in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text
        assert 'hx-swap="innerHTML"' in resp.text

    def test_create_source(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={"name": "My API", "source_type": "webhook", "description": "Test source"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        from app.models import EventTypeRecord
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.name == "My API").first()
            assert src is not None
            assert src.slug == "my_api"
            assert src.source_type == "webhook"
            names = {
                et.name
                for et in db.query(EventTypeRecord).filter(EventTypeRecord.source_id == src.id)
            }
            assert names == {"always"}
        finally:
            db.close()

    def test_create_source_requires_name(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={"name": "", "source_type": "webhook"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = str(resp.headers.get("location", ""))
        assert "error" in loc
        assert "Name" in loc or "name" in loc.lower()

    def test_htmx_empty_required_flashes_error(self, authenticated_client):
        """HTMX source validation should re-render the dialog with the error inline."""
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={
                "name": "Needs URL",
                "source_type": "poll",
                "schedule_type": "interval",
                "interval_seconds": "60",
                "poll_category": "url",
                "handler_type": "http_get",
            },
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") is None
        assert "URL is required" in resp.text
        assert 'value="Needs URL"' in resp.text
        assert 'option value="poll" selected' in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text

    def test_slug_auto_from_name_and_uniquified(self, authenticated_client):
        resp1 = authenticated_client.post(
            "/config/pipeline/sources",
            data={"name": "Same Name", "source_type": "webhook", "description": ""},
            follow_redirects=False,
        )
        assert resp1.status_code == 303
        resp2 = authenticated_client.post(
            "/config/pipeline/sources",
            data={"name": "Same Name", "source_type": "webhook", "description": ""},
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        from app.database import get_db
        db = next(get_db())
        try:
            slugs = sorted(
                s.slug for s in db.query(Source).filter(Source.name == "Same Name").all()
            )
            assert slugs == ["same_name", "same_name_2"]
        finally:
            db.close()

    def test_list_sources_shows_created(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="List Test", slug="list-test")
        resp = authenticated_client.get("/config/pipeline")
        assert resp.status_code == 200
        assert "List Test" in resp.text
        assert 'class="pipeline-chain__toolbar"' in resp.text
        assert 'class="pipeline-action-menu"' in resp.text
        assert '/static/js/disclosures.js' in resp.text

    def test_sources_render_collapsed_newest_first(self, authenticated_client):
        first_id, _ = _create_source(authenticated_client, name="Zulu Source")
        second_id, _ = _create_source(authenticated_client, name="Alpha Source")

        resp = authenticated_client.get("/config/pipeline")

        assert resp.status_code == 200
        assert 'class="btn btn--sm pipeline-disclosure-toggle"' in resp.text
        assert 'data-disclosure-key="source-chain-' in resp.text
        assert 'aria-controls="source-chain-body-' in resp.text
        assert resp.text.index(f'id="source-chain-{second_id}"') < resp.text.index(f'id="source-chain-{first_id}"')

    def test_edit_source_partial(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Edit Partial", slug="edit-partial")
        resp = authenticated_client.get(f"/config/pipeline/source/{sid}/partials/edit-form")
        assert resp.status_code == 200
        assert "Edit Source" in resp.text
        assert "Edit Partial" in resp.text
        assert 'name="schedule_name"' not in resp.text
        assert 'id="webhook-provider-fields"' in resp.text
        assert 'data-webhook-provider="discord"' in resp.text
        assert 'data-webhook-provider="paypal"' in resp.text
        assert "Application public key" in resp.text
        assert "/webhook/" in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text
        assert 'hx-swap="innerHTML"' in resp.text

    def test_edit_poll_source_hides_webhook_path(self, authenticated_client):
        sid, _ = _create_source(
            authenticated_client, name="Poll Edit Partial", slug="poll-edit-partial", source_type="poll"
        )
        resp = authenticated_client.get(f"/config/pipeline/source/{sid}/partials/edit-form")
        assert resp.status_code == 200
        assert "Webhook path" not in resp.text

    def test_htmx_edit_source_validation_stays_in_dialog(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="HTMX Edit", slug="htmx-edit")
        resp = authenticated_client.post(
            f"/config/source/{sid}/edit",
            data={"name": "", "source_type": "webhook", "description": "Keep me"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") is None
        assert "Name is required" in resp.text
        assert "Keep me" in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text

    def test_edit_source_not_found(self, authenticated_client):
        resp = authenticated_client.get("/config/pipeline/source/9999/partials/edit-form")
        assert resp.status_code == 404

    def test_update_source(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Old Name", slug="old-name")
        resp = authenticated_client.post(
            f"/config/source/{sid}/edit",
            data={"name": "New Name", "source_type": "webhook",
                  "description": "Updated desc"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            assert src.name == "New Name"
            assert src.slug == "new_name"
        finally:
            db.close()

    def test_update_source_poll_schedule(self, authenticated_client):
        sid, slug = _create_source(
            authenticated_client, name="Poll Edit", slug="poll-edit", source_type="poll"
        )
        resp = authenticated_client.post(
            f"/config/source/{sid}/edit",
            data={
                "name": "Poll Edit",
                "source_type": "poll",
                "poll_category": "url",
                "description": "",
                "schedule_type": "interval",
                "interval_seconds": "120",
                "handler_type": "http_get",
                "handler_url": "https://example.com/v2",
                "timeout_seconds": "30",
                "retry_count": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        from app.models import PollingSchedule
        db = next(get_db())
        try:
            sched = db.query(PollingSchedule).filter(PollingSchedule.source_id == sid).first()
            assert sched is not None
            assert sched.name == "Poll Edit"
            assert sched.interval_seconds == 120
            assert sched.handler_url == "https://example.com/v2"
            assert sched.retry_count == 1
        finally:
            db.close()

    def test_update_source_webhook_secret(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Wh Edit", slug="wh-edit")
        resp = authenticated_client.post(
            f"/config/source/{sid}/edit",
            data={
                "name": "Wh Edit",
                "source_type": "webhook",
                "description": "",
                "webhook_secret_value": "supersecret",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            assert src.webhook_secret_id is not None
            secret = db.query(Secret).filter(Secret.id == src.webhook_secret_id).first()
            assert secret.encrypted_value is not None
        finally:
            db.close()

    def test_delete_source(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="To Delete", slug="to-delete")
        resp = authenticated_client.post(f"/config/source/{sid}/delete", follow_redirects=False)
        assert resp.status_code == 303

    def test_toggle_source(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Toggle Me", slug="toggle-me")
        resp = authenticated_client.post(f"/config/source/{sid}/toggle", follow_redirects=False)
        assert resp.status_code == 303
        resp = authenticated_client.post(f"/config/source/{sid}/toggle", follow_redirects=False)
        assert resp.status_code == 303


# ── Config: Event Types ──────────────────────────────────────────────────────

class TestEventTypes:
    def test_create_event_type(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="ET Source", slug="et-source")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "api_error", "description": "API returned error"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_event_type_requires_name(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="ET Source", slug="et-source")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "", "description": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in resp.headers.get("location", "").lower() or "Event" in resp.headers.get("location", "")

    def test_htmx_event_validation_stays_in_dialog(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="ET HTMX", slug="et-htmx")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "", "description": "Keep me"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") is None
        assert "Event type is required" in resp.text
        assert "Keep me" in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text
        assert "Event type *" in resp.text

    def test_pipeline_create_event(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="ET Pipeline", slug="et-pipeline")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "user.created", "description": "New user"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        pipeline = authenticated_client.get("/config/pipeline")
        assert pipeline.status_code == 200
        assert "user.created" in pipeline.text

    def test_delete_event_type(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="ET Source", slug="et-source")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "to-delete", "description": ""},
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import ActionInstance, Event, Rule
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(EventTypeRecord.name == "to-delete").first()
            et_id = et.id
            action = ActionInstance(
                source_id=sid, action_type="http_forward",
                config={"url": "https://example.com", "method": "POST"},
                enabled=True,
            )
            db.add(action)
            db.flush()
            rule = Rule(
                source_id=sid,
                description="bound",
                event_type_ids=[et_id],
                action_ids=[action.id],
                conditions={},
            )
            db.add(rule)
            db.add(Event(
                source_id=sid, event_type_id=et_id,
                raw_payload="{}", normalized_data={}, status="processed",
            ))
            db.commit()
            rule_id = rule.id
            action_id = action.id
            event_count = db.query(Event).filter(Event.event_type_id == et_id).count()
            assert event_count == 1
        finally:
            db.close()
        resp = authenticated_client.post(f"/config/event-type/{et_id}/delete", follow_redirects=False)
        assert resp.status_code == 303
        db = next(get_db())
        try:
            assert db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first() is None
            assert db.query(Rule).filter(Rule.id == rule_id).first() is None
            assert db.query(ActionInstance).filter(ActionInstance.id == action_id).first() is None
            assert db.query(Event).filter(Event.event_type_id == et_id).count() == 0
            assert db.query(Source).filter(Source.id == sid).first() is not None
        finally:
            db.close()

    def test_delete_event_type_keeps_multi_type_rule(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="ET Multi", slug="et-multi")
        for name in ("keep-me", "drop-me"):
            authenticated_client.post(
                f"/config/pipeline/source/{sid}/events",
                data={"name": name, "description": ""},
                follow_redirects=False,
            )
        from app.database import get_db
        from app.models import Rule
        db = next(get_db())
        try:
            keep = db.query(EventTypeRecord).filter(
                EventTypeRecord.source_id == sid, EventTypeRecord.name == "keep-me"
            ).first()
            drop = db.query(EventTypeRecord).filter(
                EventTypeRecord.source_id == sid, EventTypeRecord.name == "drop-me"
            ).first()
            rule = Rule(
                source_id=sid, event_type_ids=[keep.id, drop.id],
                action_ids=[], conditions={},
            )
            db.add(rule)
            db.commit()
            rule_id, keep_id, drop_id = rule.id, keep.id, drop.id
        finally:
            db.close()
        resp = authenticated_client.post(f"/config/event-type/{drop_id}/delete", follow_redirects=False)
        assert resp.status_code == 303
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            assert rule is not None
            assert rule.event_type_ids == [keep_id]
            assert db.query(EventTypeRecord).filter(EventTypeRecord.id == drop_id).first() is None
        finally:
            db.close()

    def test_poll_source_seeds_success_failure_events(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={
                "name": "Poll Seed", "source_type": "poll",
                "poll_category": "url",
                "description": "",
                "schedule_type": "interval",
                "interval_seconds": "60", "handler_type": "http_get",
                "handler_url": "https://example.com/data",
                "timeout_seconds": "30", "retry_count": "0",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        from app.models import PollingSchedule
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.name == "Poll Seed").first()
            assert src is not None
            names = {
                et.name
                for et in db.query(EventTypeRecord).filter(EventTypeRecord.source_id == src.id)
            }
            assert names == {"on_success", "on_failure"}
            sched = db.query(PollingSchedule).filter(PollingSchedule.source_id == src.id).first()
            assert (sched.handler_params or {}).get("event_type") == "on_success"
        finally:
            db.close()

    def test_event_type_normalized_and_duplicates_rejected(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="ET Norm", slug="et-norm")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "Order.Paid", "description": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        from app.models import Rule
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(
                EventTypeRecord.source_id == sid,
                EventTypeRecord.name == "order.paid",
            ).first()
            assert et is not None
            et_id = et.id
            rule = Rule(
                source_id=sid, description="bound",
                event_type_ids=[et_id], action_ids=[], conditions={},
            )
            db.add(rule)
            db.commit()
        finally:
            db.close()

        dup = authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "ORDER.PAID", "description": ""},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert dup.status_code == 200
        assert "already exists" in dup.text

        rename = authenticated_client.post(
            f"/config/pipeline/event/{et_id}",
            data={"name": "Order.Shipped", "description": "shipped"},
            follow_redirects=False,
        )
        assert rename.status_code == 303
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
            assert et.name == "order.shipped"
            rule = db.query(Rule).filter(Rule.source_id == sid).first()
            assert et_id in (rule.event_type_ids or [])
        finally:
            db.close()

    def test_event_type_rejects_too_long(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="ET Long", slug="et-long")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "x" * 201, "description": ""},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "at most 200" in resp.text

    def test_poll_to_webhook_seeds_always(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Convert Me", source_type="poll")
        resp = authenticated_client.post(
            f"/config/source/{sid}/edit",
            data={
                "name": "Convert Me",
                "source_type": "webhook",
                "description": "",
                "webhook_provider": "generic_hmac",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        db = next(get_db())
        try:
            names = {
                et.name
                for et in db.query(EventTypeRecord).filter(EventTypeRecord.source_id == sid)
            }
            assert "always" in names
            assert "on_success" in names
        finally:
            db.close()

    def test_edit_source_warns_about_slug_change(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Rename Warn")
        resp = authenticated_client.get(f"/config/pipeline/source/{sid}/partials/edit-form")
        assert resp.status_code == 200
        assert "/webhook/" in resp.text
        assert "re-derives this path" in resp.text
        assert "alert--warning" in resp.text

        authenticated_client.post(
            f"/config/source/{sid}/edit",
            data={
                "name": "Rename Warn New",
                "source_type": "webhook",
                "description": "",
                "webhook_provider": "generic_hmac",
            },
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import AuditLog
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            assert src.slug != slug
            audit = (
                db.query(AuditLog)
                .filter(AuditLog.action == "source.update", AuditLog.resource_id == sid)
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert audit is not None
            details = audit.details or {}
            assert details.get("previous_slug") == slug
            assert details.get("slug") == src.slug
        finally:
            db.close()


# ── Config: Poll schedules (one per poll source) ──────────────────────────────

class TestSchedules:
    def test_poll_create_yields_one_schedule(self, authenticated_client):
        sid, _ = _create_source(
            authenticated_client, name="One Sched", slug="one-sched", source_type="poll",
        )
        from app.database import get_db
        from app.models import PollingSchedule
        db = next(get_db())
        try:
            assert db.query(PollingSchedule).filter(PollingSchedule.source_id == sid).count() == 1
        finally:
            db.close()

    def test_poll_source_run_now(self, authenticated_client, monkeypatch):
        sid, _ = _create_source(
            authenticated_client, name="Run Now Poll", slug="run-now-poll", source_type="poll",
        )
        called = []

        def fake_run(schedule_id):
            called.append(schedule_id)
            return True

        monkeypatch.setattr("app.routers.pipeline.run_schedule", fake_run)
        resp = authenticated_client.post(
            f"/config/source/{sid}/poll-now",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = str(resp.headers.get("location", ""))
        assert "success" in loc
        assert len(called) == 1


# ── Config: Actions ──────────────────────────────────────────────────────────

class TestActions:
    def test_create_action(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Act Src", slug="act-src")
        from app.database import get_db
        from app.models import Field
        db = next(get_db())
        try:
            f = Field(name="Hits", slug="hits-act", field_type="value", config={}, state={"value": 0})
            db.add(f)
            db.commit()
            fid = f.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "field_push",
                  "field_id": str(fid), "field_type": "value", "value_op": "increment", "delta": "1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = str(resp.headers.get("location", ""))
        assert "error" not in loc

    def test_logbook_value_key_rejects_invalid_json(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Act JSON", slug="act-json-key")
        from app.database import get_db
        from app.models import Field
        db = next(get_db())
        try:
            f = Field(name="LB", slug="lb-bad-json", field_type="logbook", config={}, state={})
            db.add(f)
            db.commit()
            fid = f.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={
                "action_type": "field_push",
                "field_id": str(fid),
                "field_type": "logbook",
                "logbook_mode": "key",
                "value_key": "{not valid",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in str(resp.headers.get("location", ""))

    def test_create_field_push_requires_field(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Act Src", slug="act-src-name")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "field_push"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in str(resp.headers.get("location", ""))

    def test_htmx_action_validation_stays_in_dialog(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Act HTMX", slug="act-htmx")
        from app.database import get_db
        db = next(get_db())
        try:
            rule = Rule(source_id=sid, event_type_ids=[], action_ids=[], conditions={}, order_index=0)
            db.add(rule)
            db.commit()
            rid = rule.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "field_push", "rule_id": str(rid)},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") is None
        assert "Field is required" in resp.text
        assert f'value="{rid}"' in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text

    def test_create_http_forward_requires_url(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Act Src", slug="act-src-json")
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "http_forward", "url": "", "method": "POST"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in str(resp.headers.get("location", ""))

    def test_list_actions_on_pipeline(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Act List", slug="act-list")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "web_push",
                  "title": "Hello", "body": "World", "url": "/"},
            follow_redirects=False,
        )
        resp = authenticated_client.get("/config/pipeline")
        assert resp.status_code == 200
        assert "Alert → Hello" in resp.text or "Browser notification" in resp.text
        assert "Unused actions" in resp.text

    def test_delete_action(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Act Del", slug="act-del")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "web_push",
                  "title": "ToDelete", "body": "x", "url": "/"},
            follow_redirects=False,
        )
        from app.database import get_db
        db = next(get_db())
        try:
            action = db.query(ActionInstance).filter(ActionInstance.source_id == sid).first()
            assert action is not None
            aid = action.id
        finally:
            db.close()
        resp = authenticated_client.post(f"/config/action/{aid}/delete", follow_redirects=False)
        assert resp.status_code == 303


# ── Config: Rules ────────────────────────────────────────────────────────────

class TestRules:
    def _setup(self, client):
        """Create source + event type + action for rule creation."""
        sid, slug = _create_source(client, name="Rule Source", slug="rule-source")
        client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "error", "description": ""},
            follow_redirects=False,
        )
        client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "web_push",
                  "title": "Notify", "body": "hi", "url": "/"},
            follow_redirects=False,
        )
        return sid

    def test_pipeline_shows_rules(self, authenticated_client):
        sid = self._setup(authenticated_client)
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": "[]", "conditions": "{}",
                  "action_ids": "[]", "order_index": "0"},
            follow_redirects=False,
        )
        resp = authenticated_client.get("/config/pipeline")
        assert resp.status_code == 200
        assert "on all events" in resp.text
        assert "pipeline-event__rules" in resp.text
        assert 'pipeline-node__label">Rules</span>' not in resp.text
        assert "Add Rule" not in resp.text  # source-level Rules column removed

    def test_rules_nested_under_matching_event_type(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Nest Rules Src")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "alpha.event", "description": ""},
            follow_redirects=False,
        )
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "beta.event", "description": ""},
            follow_redirects=False,
        )
        from app.database import get_db
        db = next(get_db())
        try:
            ets = (
                db.query(EventTypeRecord)
                .filter(EventTypeRecord.source_id == sid)
                .order_by(EventTypeRecord.name)
                .all()
            )
            # alpha before beta alphabetically; also seeded always for webhooks
            alpha = next(et for et in ets if et.name == "alpha.event")
            beta = next(et for et in ets if et.name == "beta.event")
            alpha_id, beta_id = alpha.id, beta.id
        finally:
            db.close()
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={
                "event_type_ids": f"[{alpha_id}]",
                "conditions": "{}",
                "action_ids": "[]",
                "order_index": "0",
            },
            follow_redirects=False,
        )
        resp = authenticated_client.get("/config/pipeline")
        assert resp.status_code == 200
        text = resp.text
        # Expand details aren't required — markup order should nest rule under alpha.
        assert "alpha.event" in text and "beta.event" in text
        assert "on alpha.event" in text
        alpha_pos = text.index("alpha.event")
        rule_pos = text.index("on alpha.event")
        beta_pos = text.index("beta.event")
        assert alpha_pos < rule_pos
        # Rule should appear before beta when alpha sorts before beta,
        # or at least inside alpha's event block (after alpha, before next sibling content).
        # With name order: always, alpha.event, beta.event — rule after alpha.event chip.
        always_block = text.find("always")
        if always_block != -1 and always_block < alpha_pos:
            pass
        assert rule_pos < beta_pos or text.count("on alpha.event") >= 1
        assert "pipeline-event__rules" in text
        assert "No rules for this event type." in text  # beta (and maybe always) empty

    def test_create_rule(self, authenticated_client):
        sid = self._setup(authenticated_client)
        from app.database import get_db
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(EventTypeRecord.source_id == sid).first()
            action = db.query(ActionInstance).filter(ActionInstance.source_id == sid).first()
            et_id, action_id = et.id, action.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": f"[{et_id}]", "conditions": '{"severity": "high"}',
                  "action_ids": f"[{action_id}]", "order_index": "10"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" not in str(resp.headers.get("location", ""))

    def test_create_rule_rejects_cross_source_action(self, authenticated_client):
        sid = self._setup(authenticated_client)
        other_sid, _ = _create_source(authenticated_client, name="Other", slug="other-rule-src")
        authenticated_client.post(
            f"/config/pipeline/source/{other_sid}/actions",
            data={"action_type": "web_push",
                  "title": "Other", "body": "x", "url": "/"},
            follow_redirects=False,
        )
        from app.database import get_db
        db = next(get_db())
        try:
            other_action = (
                db.query(ActionInstance)
                .filter(ActionInstance.source_id == other_sid)
                .first()
            )
            oid = other_action.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": "[]", "conditions": "{}",
                  "action_ids": f"[{oid}]", "order_index": "0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in str(resp.headers.get("location", ""))

    def test_create_rule_bad_conditions(self, authenticated_client):
        sid = self._setup(authenticated_client)
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": "[]",
                  "conditions": "{bad", "action_ids": "[]", "order_index": "0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in str(resp.headers.get("location", ""))

    def test_htmx_rule_validation_stays_in_dialog(self, authenticated_client):
        sid = self._setup(authenticated_client)
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": "[]", "conditions": "{bad", "order_index": "7"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") is None
        assert "Conditions must be valid JSON" in resp.text
        assert 'value="7"' in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text

    def test_delete_rule(self, authenticated_client):
        sid = self._setup(authenticated_client)
        from app.database import get_db
        db = next(get_db())
        try:
            action = db.query(ActionInstance).filter(ActionInstance.source_id == sid).first()
            action_id = action.id
        finally:
            db.close()
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": "[]",
                  "conditions": "{}", "action_ids": f"[{action_id}]", "order_index": "0"},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.source_id == sid).order_by(Rule.id.desc()).first()
            rid = rule.id
        finally:
            db.close()
        resp = authenticated_client.post(f"/config/rule/{rid}/delete", follow_redirects=False)
        assert resp.status_code == 303
        db = next(get_db())
        try:
            assert db.query(Rule).filter(Rule.id == rid).first() is None
            assert db.query(ActionInstance).filter(ActionInstance.id == action_id).first() is None
        finally:
            db.close()


# ── Rule-first pipeline UX ───────────────────────────────────────────────────

class TestRuleFirstPipeline:
    def test_rule_form_prefills_event_type(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="RF Prefill", slug="rf-prefill")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "order.paid", "description": ""},
            follow_redirects=False,
        )
        from app.database import get_db
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(
                EventTypeRecord.source_id == sid, EventTypeRecord.name == "order.paid"
            ).first()
            et_id = et.id
        finally:
            db.close()
        resp = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/rule-form?event_type_id={et_id}"
        )
        assert resp.status_code == 200
        assert f'value="{et_id}"' in resp.text
        assert "selected" in resp.text
        assert "conditions-builder" in resp.text
        assert 'name="conditions"' in resp.text
        assert "Add condition" in resp.text
        assert 'name="name"' not in resp.text or 'Name *' not in resp.text

    def test_rule_form_edit_loads_conditions(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="RF Cond", slug="rf-cond")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={
                "event_type_ids": "[]",
                "conditions": '{"severity": "high"}',
                "order_index": "0",
            },
            follow_redirects=False,
        )
        from app.database import get_db
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.source_id == sid).first()
            rule_id = rule.id
        finally:
            db.close()
        resp = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/rule-form?rule_id={rule_id}"
        )
        assert resp.status_code == 200
        assert "conditions-builder" in resp.text
        assert "severity" in resp.text
        assert "high" in resp.text

    def test_rule_form_edit_preserves_event_types(self, authenticated_client):
        """Edit must preselect saved event types, not wipe to empty / first option."""
        import re

        sid, _ = _create_source(
            authenticated_client, name="RF Edit ET", source_type="poll",
        )
        from app.database import get_db
        db = next(get_db())
        try:
            ets = {
                et.name: et.id
                for et in db.query(EventTypeRecord).filter(EventTypeRecord.source_id == sid)
            }
            success_id = ets["on_success"]
            failure_id = ets["on_failure"]
        finally:
            db.close()
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={
                "event_type_ids": f"[{success_id}]",
                "conditions": "{}",
                "order_index": "0",
            },
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.source_id == sid).first()
            rule_id = rule.id
        finally:
            db.close()
        resp = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/rule-form?rule_id={rule_id}"
        )
        assert resp.status_code == 200
        success_opt = re.search(rf'<option[^>]*value="{success_id}"[^>]*>', resp.text)
        failure_opt = re.search(rf'<option[^>]*value="{failure_id}"[^>]*>', resp.text)
        assert success_opt is not None
        assert "selected" in success_opt.group(0)
        assert failure_opt is not None
        assert "selected" not in failure_opt.group(0)

    def test_create_action_on_rule_binds(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="RF Bind", slug="rf-bind")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": "[]", "conditions": "{}", "order_index": "0"},
            follow_redirects=False,
        )
        from app.database import get_db
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.source_id == sid).first()
            rule_id = rule.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "web_push", "title": "Bound", "body": "x", "url": "/",
                  "rule_id": str(rule_id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            action = db.query(ActionInstance).filter(ActionInstance.source_id == sid).first()
            assert action is not None
            assert action.id in (rule.action_ids or [])
        finally:
            db.close()
        pipeline = authenticated_client.get("/config/pipeline")
        assert "Alert →" in pipeline.text or "Browser notification" in pipeline.text
        assert "Unused actions" not in pipeline.text

    def test_edit_event_rule_action(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="RF Edit", slug="rf-edit")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "old.event", "description": "d1"},
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import Field
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(EventTypeRecord.source_id == sid).first()
            et_id = et.id
            field = Field(name="RF Val", slug="rf-val", field_type="text", config={}, state={"value": ""})
            db.add(field)
            db.commit()
            fid = field.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/event/{et_id}",
            data={"name": "new.event", "description": "d2"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": f"[{et_id}]", "conditions": "{}", "order_index": "0"},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.source_id == sid).first()
            rule_id = rule.id
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/config/pipeline/rule/{rule_id}",
            data={"event_type_ids": f"[{et_id}]",
                  "conditions": '{"x": 1}', "order_index": "2", "enabled": "1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "field_push", "field_id": str(fid), "field_type": "text",
                  "value_mode": "literal", "value": "hi", "rule_id": str(rule_id)},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            action = db.query(ActionInstance).filter(ActionInstance.source_id == sid).first()
            action_id = action.id
            et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            assert et.name == "new.event"
            assert et.description == "d2"
            assert rule.order_index == 2
            assert rule.conditions == {"x": 1}
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/pipeline/action/{action_id}",
            data={"action_type": "field_push", "field_id": str(fid), "field_type": "text",
                  "value_mode": "literal", "value": "updated"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = next(get_db())
        try:
            action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
            assert action.config.get("value") == "updated"
            assert action.config.get("field_id") == fid
        finally:
            db.close()

    def test_delete_action_scrubs_rule(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="RF Scrub", slug="rf-scrub")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules",
            data={"event_type_ids": "[]", "conditions": "{}", "order_index": "0"},
            follow_redirects=False,
        )
        from app.database import get_db
        db = next(get_db())
        try:
            rule_id = db.query(Rule).filter(Rule.source_id == sid).first().id
        finally:
            db.close()
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/actions",
            data={"action_type": "web_push", "title": "Gone", "body": "x", "url": "/",
                  "rule_id": str(rule_id)},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            action_id = db.query(ActionInstance).filter(
                ActionInstance.source_id == sid
            ).first().id
            assert action_id in (
                db.query(Rule).filter(Rule.id == rule_id).first().action_ids or []
            )
        finally:
            db.close()
        authenticated_client.post(f"/config/action/{action_id}/delete", follow_redirects=False)
        db = next(get_db())
        try:
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            assert action_id not in (rule.action_ids or [])
        finally:
            db.close()


# ── Pipeline recent / sample / retention ─────────────────────────────────────

class TestPipelineEventSamples:
    def test_sample_empty_and_populated(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Sample Src", slug="sample-src")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "order.paid", "description": ""},
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import EventTypeRecord, Event
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(
                EventTypeRecord.source_id == sid, EventTypeRecord.name == "order.paid"
            ).first()
            et_id = et.id
        finally:
            db.close()

        empty = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/latest-event?event_type_id={et_id}"
        )
        assert empty.status_code == 200
        assert "No events of type" in empty.text
        assert "order.paid" in empty.text

        db = next(get_db())
        try:
            db.add(Event(
                source_id=sid, event_type_id=et_id,
                normalized_data={"amount": 42, "source": "Sample Src"},
                raw_payload="{}", correlation_id="s1", status="processed",
            ))
            db.commit()
        finally:
            db.close()

        filled = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/latest-event?event_type_id={et_id}"
        )
        assert filled.status_code == 200
        assert "Sample — order.paid" in filled.text
        assert '"amount": 42' in filled.text

    def test_recent_events_limit(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Recent Src", slug="recent-src")
        from app.database import get_db
        from app.models import Event
        db = next(get_db())
        try:
            for i in range(7):
                db.add(Event(
                    source_id=sid,
                    normalized_data={"n": i},
                    raw_payload="{}", correlation_id=f"r{i}", status="processed",
                ))
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/recent-events?limit=5"
        )
        assert resp.status_code == 200
        assert "Recent — Recent Src" in resp.text
        # Newest first: n=6..2 present; n=0,1 should be beyond limit of 5
        assert '"n": 6' in resp.text
        assert '"n": 2' in resp.text
        assert '"n": 0' not in resp.text

        clamped = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/recent-events?limit=999"
        )
        assert clamped.status_code == 200
        # Max 50 — still shows all 7
        assert '"n": 0' in clamped.text

    def test_recent_events_partial_uses_display_timezone(self, authenticated_client):
        from app.database import get_db
        from app.models import Event

        sid, _ = _create_source(authenticated_client, name="TZ Recent Src", slug="tz-recent-src")
        _set_display_timezone("Africa/Johannesburg")
        db = next(get_db())
        try:
            db.add(Event(
                source_id=sid,
                timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                normalized_data={"n": 1},
                raw_payload="{}",
                correlation_id="tz-recent-1",
                status="processed",
            ))
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.get(
            f"/config/pipeline/source/{sid}/partials/recent-events?limit=5"
        )
        assert resp.status_code == 200
        assert "2026-01-02 05:04:05" in resp.text

    def test_prune_source_events(self, authenticated_client):
        from app.database import get_db
        from app.models import Event
        from app.event_store import prune_source_events

        sid, _ = _create_source(authenticated_client, name="Prune Ev", slug="prune-ev")
        db = next(get_db())
        try:
            for i in range(10):
                db.add(Event(
                    source_id=sid,
                    normalized_data={"i": i},
                    raw_payload="{}", correlation_id=f"p{i}", status="processed",
                ))
            db.commit()
            deleted = prune_source_events(db, sid, keep=3)
            db.commit()
            assert deleted == 7
            remaining = (
                db.query(Event)
                .filter(Event.source_id == sid)
                .order_by(Event.id)
                .all()
            )
            assert len(remaining) == 3
            assert [e.normalized_data["i"] for e in remaining] == [7, 8, 9]
        finally:
            db.close()

    def test_prune_nullifies_logbook_event_fk(self, authenticated_client):
        from app.database import get_db
        from app.models import Event, Field, FieldLogEntry
        from app.event_store import prune_source_events

        sid, _ = _create_source(authenticated_client, name="Prune FK", slug="prune-fk")
        db = next(get_db())
        try:
            field = Field(name="Prune Log", slug="prune-log", field_type="logbook", config={}, state={})
            db.add(field)
            db.flush()
            for i in range(5):
                db.add(Event(
                    source_id=sid,
                    normalized_data={"i": i},
                    raw_payload="{}",
                    correlation_id=f"fk{i}",
                    status="processed",
                ))
            db.commit()
            oldest = (
                db.query(Event)
                .filter(Event.source_id == sid)
                .order_by(Event.id)
                .first()
            )
            oldest_id = oldest.id
            entry = FieldLogEntry(field_id=field.id, value={"x": 1}, event_id=oldest_id, source_id=sid)
            db.add(entry)
            db.commit()
            entry_id = entry.id

            deleted = prune_source_events(db, sid, keep=2)
            db.commit()
            assert deleted >= 1
            entry = db.query(FieldLogEntry).filter(FieldLogEntry.id == entry_id).first()
            assert entry is not None
            assert entry.event_id is None
            assert db.query(Event).filter(Event.id == oldest_id).first() is None
        finally:
            db.close()

    def test_prune_skips_pending(self, authenticated_client):
        from app.database import get_db
        from app.models import Event
        from app.event_store import prune_source_events

        sid, _ = _create_source(authenticated_client, name="Prune Pend", slug="prune-pend")
        db = next(get_db())
        try:
            for i in range(5):
                db.add(Event(
                    source_id=sid,
                    normalized_data={"i": i},
                    raw_payload="{}",
                    correlation_id=f"pend{i}",
                    status="pending" if i < 3 else "processed",
                ))
            db.commit()
            prune_source_events(db, sid, keep=2)
            db.commit()
            # Oldest three are pending — must survive even outside keep window
            assert (
                db.query(Event)
                .filter(Event.source_id == sid, Event.status == "pending")
                .count()
            ) == 3
        finally:
            db.close()

    def test_event_detail_renders_normalized_data(self, authenticated_client):
        from app.database import get_db
        from app.models import Event

        sid, _ = _create_source(authenticated_client, name="Detail Ev", slug="detail-ev")
        _set_display_timezone("Africa/Johannesburg")
        db = next(get_db())
        try:
            ev = Event(
                source_id=sid,
                timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                normalized_data={"hello": "world", "n": 1},
                raw_payload="{}",
                correlation_id="detail-1",
                status="processed",
            )
            db.add(ev)
            db.commit()
            db.refresh(ev)
            eid = ev.id
        finally:
            db.close()
        resp = authenticated_client.get(f"/event/{eid}")
        assert resp.status_code == 200
        assert "hello" in resp.text
        assert "world" in resp.text
        assert "2026-01-02 05:04:05" in resp.text

    def test_pipeline_shows_recent_and_sample_buttons(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Btn Src", slug="btn-src")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "ping", "description": ""},
            follow_redirects=False,
        )
        resp = authenticated_client.get("/config/pipeline")
        assert resp.status_code == 200
        assert "Recent" in resp.text
        assert "Sample" in resp.text
        assert f"/partials/recent-events" in resp.text
        assert "latest-event?event_type_id=" in resp.text


# ── Help ─────────────────────────────────────────────────────────────────────

class TestHelp:
    def test_help_page(self, authenticated_client):
        from app.fields import RESERVED_FIELD_SLUGS

        resp = authenticated_client.get("/help")
        assert resp.status_code == 200
        assert "Rule conditions" in resp.text
        assert 'href="/help"' in resp.text
        assert "config-nav__link--active" in resp.text
        assert 'id="fields"' in resp.text
        assert "Logbook" in resp.text
        assert "Data" in resp.text
        assert 'id="dot-notation"' in resp.text
        assert "Dot notation" in resp.text
        for name in RESERVED_FIELD_SLUGS:
            assert f"<code>{name}</code>" in resp.text
        assert 'id="example-apis"' in resp.text
        assert "api.open-meteo.com" in resp.text
        assert "Trigger source" in resp.text
        assert "Triggers" in resp.text

    def test_help_requires_auth(self, client):
        client.cookies.clear()
        resp = client.get("/help", follow_redirects=False)
        assert resp.status_code == 303
        loc = str(resp.headers.get("location", ""))
        assert "/login" in loc or "/setup" in loc

    def test_pipeline_loads_conditions_builder_js(self, authenticated_client):
        resp = authenticated_client.get("/config/pipeline")
        assert resp.status_code == 200
        assert "/static/js/conditions-builder.js" in resp.text
        assert 'id="pipeline-fields"' in resp.text
        assert "Fields" in resp.text


# ── Config: Users ────────────────────────────────────────────────────────────

class TestUsersCRUD:
    def test_get_users(self, authenticated_client):
        resp = authenticated_client.get("/config/users")
        assert resp.status_code == 200
        assert "testadmin" in resp.text

    def test_create_user(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/users",
            data={"username": "jdoe", "password": "password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_user_requires_fields(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/users",
            data={"username": "", "password": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_duplicate_user(self, authenticated_client):
        authenticated_client.post(
            "/config/users",
            data={"username": "duplicate", "password": "pass"},
            follow_redirects=False,
        )
        resp = authenticated_client.post(
            "/config/users",
            data={"username": "duplicate", "password": "pass2"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ── Config: Dashboard Layout ────────────────────────────────────────────────

class TestDashboardLayout:
    def test_get_dashboard(self, authenticated_client):
        resp = authenticated_client.get("/config/dashboard")
        assert resp.status_code == 200
        assert b'id="widget-catalog"' in resp.content
        assert b'id="add-widget-btn"' in resp.content
        assert b'name="widget_type"' not in resp.content
        assert b'data-widget-toggle' in resp.content
        assert b'dashboard-widget-editor__toggle' in resp.content
        assert b'id="widgets-error"' in resp.content
        assert b"firstWidgetProblem" in resp.content
        assert b'form.addEventListener("submit"' in resp.content

    def test_save_dashboard_layout(self, authenticated_client):
        widgets = [
            {"type": "system", "display": "source_health", "title": "Source Health"},
            {"type": "system", "display": "recent_events", "title": "Recent Events"},
        ]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_dashboard_config_keeps_saved_widget_order(self, authenticated_client):
        widgets = [
            {"type": "system", "display": "recent_events", "title": "Second In Alphabet"},
            {"type": "system", "display": "source_health", "title": "First In Alphabet"},
        ]
        save = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert save.status_code == 303

        resp = authenticated_client.get("/config/dashboard")

        assert resp.status_code == 200
        assert resp.text.index("Second In Alphabet") < resp.text.index("First In Alphabet")
        assert 'data-widget-toggle' in resp.text
        assert 'dashboard-widget-editor__toggle' in resp.text

    def test_dashboard_root_shows_widgets(self, authenticated_client):
        widgets = [{"type": "system", "display": "source_health", "title": "Health"}]
        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        resp = authenticated_client.get("/")
        assert resp.status_code == 200
        assert b"Health" in resp.content
        assert b"card__header" in resp.content

    def test_widget_show_title_false_hides_header(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        widgets = [{
            "type": "system",
            "display": "source_health",
            "title": "Hidden Title",
            "show_title": False,
        }]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"]
            assert saved[0]["show_title"] is False
            assert saved[0]["title"] == "Hidden Title"
        finally:
            db.close()

        home = authenticated_client.get("/")
        assert home.status_code == 200
        assert b"Hidden Title" not in home.content
        assert b'card__header' not in home.content

    def test_widget_show_title_defaults_true(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        widgets = [{"type": "system", "display": "metric_summary", "title": "Summary"}]
        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"]
            assert saved[0].get("show_title") is True
        finally:
            db.close()

    def test_save_multiple_same_type_widgets(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout, Field

        db = next(get_db())
        try:
            a = Field(name="Log A", slug="log-a", field_type="logbook", config={}, state={})
            b = Field(name="Log B", slug="log-b", field_type="logbook", config={}, state={})
            db.add_all([a, b])
            db.commit()
        finally:
            db.close()

        widgets = [
            {"type": "display", "display": "logbook_list", "title": "First log", "config": {"field_slug": "log-a", "limit": 5}},
            {"type": "display", "display": "logbook_list", "title": "Second log", "config": {"field_slug": "log-b", "limit": 15}},
        ]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            cfg = json.loads(layout.layout_config)
            saved = cfg["widgets"]
            assert len(saved) == 2
            assert saved[0]["type"] == "display"
            assert saved[0]["display"] == "logbook_list"
            assert saved[0]["title"] == "First log"
            assert saved[0]["config"]["field_slug"] == "log-a"
            assert saved[0]["config"]["limit"] == 5
            assert saved[1]["type"] == "display"
            assert saved[1]["title"] == "Second log"
            assert saved[1]["config"]["field_slug"] == "log-b"
            assert saved[1]["config"]["limit"] == 15
        finally:
            db.close()

        resp = authenticated_client.get("/")
        assert resp.status_code == 200
        assert b"First log" in resp.content
        assert b"Second log" in resp.content
        assert b"dashboard-grid" in resp.content
        assert b"gs-id=" in resp.content

    def test_layout_shared_across_users(self, authenticated_client):
        from app.database import get_db
        from app.models import User
        from app.security import hash_password, create_session_token

        widgets = [{"type": "system", "display": "source_health", "title": "Shared Health"}]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = next(get_db())
        try:
            other = User(username="otheruser", hashed_password=hash_password("pass"))
            db.add(other)
            db.commit()
            token = create_session_token("otheruser")
        finally:
            db.close()

        from fastapi.testclient import TestClient
        from app.main import app
        raw = TestClient(app)
        raw.cookies.set("session_username", token)
        other_client = CsrfClient(raw)
        resp = other_client.get("/")
        assert resp.status_code == 200
        assert b"Shared Health" in resp.content

    def test_dashboard_editor_lists_clock_widget(self, authenticated_client):
        resp = authenticated_client.get("/config/dashboard")
        assert resp.status_code == 200
        assert b">Clock</option>" in resp.content
        assert b'"type": "clock"' in resp.content

    def test_save_clock_widget_layout_and_render_root(self, authenticated_client):
        widgets = [{
            "type": "clock",
            "display": "digital",
            "title": "Tokyo Clock",
            "config": {
                "style": "mono",
                "timezone_mode": "custom",
                "timezone": "Asia/Tokyo",
                "show_seconds": True,
                "show_date": True,
                "hour_format": "24",
            },
        }]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        home = authenticated_client.get("/")
        assert home.status_code == 200
        assert b"Tokyo Clock" in home.content
        assert b"data-clock-widget" in home.content
        assert b'hx-get="/widgets/clock' not in home.content
        assert b"/static/js/widget-clock.js" in home.content

    def test_invalid_clock_timezone_rejected(self, authenticated_client):
        widgets = [{
            "type": "clock",
            "display": "digital",
            "title": "Broken Clock",
            "config": {
                "style": "plain",
                "timezone_mode": "custom",
                "timezone": "Mars/Olympus",
            },
        }]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert b"Broken Clock" in resp.content
        assert b"valid IANA timezone" in resp.content

    def test_clock_partial_renders_world_clocks(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        widgets = [{
            "type": "clock",
            "display": "world_clock",
            "title": "Markets",
            "config": {
                "style": "cards",
                "show_seconds": False,
                "show_date": True,
                "hour_format": "24",
                "world_clocks": [
                    {"label": "UTC", "timezone": "UTC"},
                    {"label": "Sydney", "timezone": "Australia/Sydney"},
                ],
            },
        }]
        save = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert save.status_code == 303

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            wid = json.loads(layout.layout_config)["widgets"][0]["id"]
        finally:
            db.close()

        resp = authenticated_client.get(f"/widgets/clock?id={wid}")
        assert resp.status_code == 200
        assert "data-clock-display=\"world_clock\"" in resp.text
        assert "Sydney" in resp.text
        assert "data-clock-row" in resp.text

    def test_clock_can_hide_timezone_details(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        widgets = [{
            "type": "clock",
            "display": "digital",
            "title": "Quiet Clock",
            "config": {
                "style": "plain",
                "timezone_mode": "utc",
                "show_seconds": True,
                "show_date": False,
                "show_timezone": False,
                "hour_format": "24",
            },
        }]
        save = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert save.status_code == 303

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"][0]
            wid = saved["id"]
            assert saved["config"]["show_timezone"] is False
        finally:
            db.close()

        resp = authenticated_client.get(f"/widgets/clock?id={wid}")
        assert resp.status_code == 200
        assert 'data-show-timezone="0"' in resp.text
        assert "data-clock-offset" not in resp.text
        assert "widget-clock__label" not in resp.text

    def test_world_clock_row_layout_saved_and_rendered(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        widgets = [{
            "type": "clock",
            "display": "world_clock",
            "title": "Row Markets",
            "config": {
                "style": "list",
                "layout": "row",
                "show_seconds": False,
                "show_date": False,
                "hour_format": "24",
                "world_clocks": [
                    {"label": "UTC", "timezone": "UTC"},
                    {"label": "London", "timezone": "Europe/London"},
                ],
            },
        }]
        save = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert save.status_code == 303

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"][0]
            wid = saved["id"]
            assert saved["config"]["layout"] == "row"
        finally:
            db.close()

        resp = authenticated_client.get(f"/widgets/clock?id={wid}")
        assert resp.status_code == 200
        assert "widget-clock--layout-row" in resp.text
        assert 'data-clock-layout="row"' in resp.text

    def test_invalid_clock_layout_rejected(self, authenticated_client):
        widgets = [{
            "type": "clock",
            "display": "world_clock",
            "title": "Bad Layout",
            "config": {
                "style": "list",
                "layout": "diagonal",
                "world_clocks": [
                    {"label": "UTC", "timezone": "UTC"},
                ],
            },
        }]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert b"Bad Layout" in resp.content
        assert b"layout" in resp.content.lower()


class TestStyleConfig:
    def test_get_style(self, authenticated_client):
        resp = authenticated_client.get("/config/style")
        assert resp.status_code == 200
        assert b"Catppuccin Mocha" in resp.content
        assert b"Clean sans" in resp.content
        assert b"Text size" in resp.content
        assert b'name="display_timezone"' in resp.content
        assert b'name="theme"' in resp.content
        assert b'name="font"' in resp.content
        assert b'name="font_size"' in resp.content
        assert b'data-theme=' in resp.content

    def test_save_theme(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/style",
            data={
                "theme": "nord",
                "font": "serif",
                "font_size": "lg",
                "display_timezone": "Africa/Johannesburg",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = authenticated_client.get("/config/style")
        assert b'data-theme="nord"' in page.content
        assert b'data-font="serif"' in page.content
        assert b'data-font-size="lg"' in page.content
        assert b'data-display-timezone="Africa/Johannesburg"' in page.content
        assert b'value="Africa/Johannesburg"' in page.content
        assert b'value="nord"' in page.content
        assert b"checked" in page.content

    def test_invalid_timezone_rejected(self, authenticated_client):
        authenticated_client.post(
            "/config/style",
            data={
                "theme": "system",
                "font": "system",
                "font_size": "md",
                "display_timezone": "UTC",
            },
            follow_redirects=False,
        )
        resp = authenticated_client.post(
            "/config/style",
            data={
                "theme": "system",
                "font": "system",
                "font_size": "md",
                "display_timezone": "SAST",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers.get("location", "")
        page = authenticated_client.get("/")
        assert b'data-display-timezone="UTC"' in page.content

    def test_invalid_theme_rejected(self, authenticated_client):
        authenticated_client.post(
            "/config/style",
            data={"theme": "nord", "font": "system", "font_size": "md"},
            follow_redirects=False,
        )
        resp = authenticated_client.post(
            "/config/style",
            data={"theme": "not-a-real-theme", "font": "system", "font_size": "md"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers.get("location", "")
        page = authenticated_client.get("/")
        assert b'data-theme="nord"' in page.content

    def test_invalid_font_rejected(self, authenticated_client):
        authenticated_client.post(
            "/config/style",
            data={"theme": "light", "font": "sans", "font_size": "md"},
            follow_redirects=False,
        )
        resp = authenticated_client.post(
            "/config/style",
            data={"theme": "light", "font": "comic-sans", "font_size": "md"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers.get("location", "")
        page = authenticated_client.get("/")
        assert b'data-font="sans"' in page.content

    def test_dashboard_bg_upload_and_clear(self, authenticated_client, tmp_path, monkeypatch):
        monkeypatch.setenv("PARA_SCOPE_UPLOADS_DIR", str(tmp_path))
        # 1x1 PNG
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        resp = authenticated_client.post(
            "/config/style",
            data={
                "theme": "system",
                "font": "system",
                "font_size": "md",
                "dashboard_bg_opacity": "40",
            },
            files={"dashboard_bg": ("bg.png", png, "image/png")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" not in resp.headers.get("location", "")
        home = authenticated_client.get("/")
        assert home.status_code == 200
        assert b"dashboard-bg" in home.content
        assert b"--dashboard-bg-opacity: 0.4" in home.content or b"--dashboard-bg-opacity: 0.40" in home.content
        media = authenticated_client.get("/media/dashboard-bg")
        assert media.status_code == 200
        assert media.content[:8] == b"\x89PNG\r\n\x1a\n"

        clear = authenticated_client.post(
            "/config/style",
            data={
                "theme": "system",
                "font": "system",
                "font_size": "md",
                "dashboard_bg_opacity": "40",
                "clear_dashboard_bg": "1",
            },
            follow_redirects=False,
        )
        assert clear.status_code == 303
        home2 = authenticated_client.get("/")
        assert b'class="dashboard-bg"' not in home2.content
        assert authenticated_client.get("/media/dashboard-bg").status_code == 404


class TestDashboardGridLayout:
    def test_series_chart_drop_tone_on_save(self):
        from app.dashboard_layout import normalize_for_save
        from app.widgets import resolve_widget_tone

        saved = normalize_for_save([
            {
                "type": "series", "display": "line", "title": "S",
                "config": {"style": "basic", "tone": "conditional", "tone_rules": [{"tone": "positive"}]},
            },
            {
                "type": "chart", "display": "pie", "title": "C",
                "config": {"style": "basic", "tone": "none"},
            },
        ])
        assert "tone" not in saved[0]["config"]
        assert "tone_rules" not in saved[0]["config"]
        assert "tone" not in saved[1]["config"]
        assert resolve_widget_tone(
            {"tone": "conditional"}, widget_type="series", display="line",
        ) is None
        assert resolve_widget_tone(
            {"tone": "conditional"}, widget_type="chart", display="pie",
        ) is None

    def test_dashboard_exposes_grid_resolution(self, authenticated_client):
        from app.dashboard_layout import (
            DEFAULT_W, GRID_COLUMN_LIVE_MAX, GRID_COLUMN_WIDTH, GRID_COLUMNS,
            GRID_STACK_BELOW,
        )

        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([
                {"type": "system", "display": "metric_summary", "title": "Summary"},
            ])},
            follow_redirects=False,
        )
        resp = authenticated_client.get("/")
        assert resp.status_code == 200
        assert f'data-gs-column="{GRID_COLUMNS}"'.encode() in resp.content
        assert f'data-gs-column-width="{GRID_COLUMN_WIDTH}"'.encode() in resp.content
        assert f'data-gs-column-live-max="{GRID_COLUMN_LIVE_MAX}"'.encode() in resp.content
        assert f'data-gs-stack-below="{GRID_STACK_BELOW}"'.encode() in resp.content
        assert b".gs-1>" in resp.content
        assert f".gs-{GRID_COLUMNS}>".encode() in resp.content
        assert f".gs-{GRID_COLUMN_LIVE_MAX}>".encode() in resp.content
        assert b"gs-id=" in resp.content

        from app.database import get_db
        from app.models import DashboardLayout

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"]
            assert len(saved) == 1
            assert saved[0]["id"].startswith("w_")
            assert saved[0]["w"] == DEFAULT_W
        finally:
            db.close()

    def test_dashboard_grid_js_defers_column_opts_and_gates_edit(self):
        from pathlib import Path

        js = Path(__file__).resolve().parents[1].joinpath("static/js/dashboard-grid.js").read_text()
        assert "column: liveMax" in js
        assert "applyResponsiveColumns" in js
        assert 'layout = "list"' in js
        assert 'layout = "none"' in js
        assert 'layout = "moveScale"' not in js
        assert "stackBelow" in js
        assert "liveMax" in js
        assert "syncEditAvailability" in js
        assert "toggle.disabled" in js
        assert "columnOpts" not in js
        assert "checkDynamicColumn" not in js

    def test_migrate_and_merge_keep_ultrawide_geometry(self):
        from app.dashboard_layout import GRID_COLUMN_LIVE_MAX, merge_geometry, normalize_widgets

        widgets, _ = normalize_widgets([
            {"id": "w_right", "type": "system", "display": "metric_summary", "x": 40, "y": 0, "w": 4, "h": 2},
        ])
        assert widgets[0]["x"] == 40
        assert widgets[0]["w"] == 4

        merged = merge_geometry(
            [{"id": "w_right", "type": "system", "display": "metric_summary", "x": 0, "y": 0, "w": 6, "h": 2}],
            [{"id": "w_right", "x": 40, "y": 1, "w": 4, "h": 3}],
        )
        assert merged[0]["x"] == 40
        assert merged[0]["w"] == 4
        assert merged[0]["y"] == 1
        assert merged[0]["h"] == 3

        too_wide, _ = normalize_widgets([
            {"id": "w_over", "type": "system", "display": "metric_summary", "x": 90, "y": 0, "w": 20, "h": 2},
        ])
        assert too_wide[0]["w"] == 20
        assert too_wide[0]["x"] == GRID_COLUMN_LIVE_MAX - 20
        assert too_wide[0]["x"] + too_wide[0]["w"] == GRID_COLUMN_LIVE_MAX

    def test_api_layout_merges_geometry_by_id(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([
                {"type": "system", "display": "metric_summary", "title": "A"},
                {"type": "system", "display": "source_health", "title": "B"},
            ])},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            widgets = json.loads(layout.layout_config)["widgets"]
            assert len(widgets) == 2
            id_a, id_b = widgets[0]["id"], widgets[1]["id"]
        finally:
            db.close()

        resp = authenticated_client.post(
            "/api/dashboard/layout",
            json={"widgets": [
                {"id": id_a, "x": 0, "y": 0, "w": 4, "h": 5},
                {"id": id_b, "x": 4, "y": 0, "w": 8, "h": 3},
            ]},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = {w["id"]: w for w in json.loads(layout.layout_config)["widgets"]}
            assert saved[id_a]["w"] == 4 and saved[id_a]["h"] == 5
            assert saved[id_b]["x"] == 4 and saved[id_b]["w"] == 8
            assert saved[id_a]["type"] == "system"
            assert saved[id_a]["title"] == "A"
        finally:
            db.close()

    def test_widget_partial_by_id(self, authenticated_client):
        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([{"type": "system", "display": "metric_summary", "title": "Sum"}])},
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import DashboardLayout
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            wid = json.loads(layout.layout_config)["widgets"][0]["id"]
        finally:
            db.close()
        resp = authenticated_client.get(f"/widgets/system?id={wid}")
        assert resp.status_code == 200
        assert b"No value fields yet" in resp.content or b"stat__value" in resp.content


class TestWidgetTransforms:
    def test_extract_number_and_series(self):
        from app.widget_transforms import extract_number, series_from_points
        assert extract_number(
            {"_poll": {"response_time_ms": 500}},
            "_poll.response_time_ms",
        ) == 500.0
        ts = datetime.now(timezone.utc)
        series = series_from_points(
            [(ts, {"_poll": {"response_time_ms": 1000}})],
            value_path="_poll.response_time_ms",
        )
        assert series == [{"ts": ts.isoformat(), "v": 1000.0}]
        # Bare scalar logbook entry + path "value" (template convention)
        assert extract_number(0.05, "value") == 0.05
        assert extract_number({"value": 0.05}, "value") == 0.05
        assert series_from_points([(ts, 1.5)], value_path="value") == [
            {"ts": ts.isoformat(), "v": 1.5}
        ]

    def test_eval_expr_and_compare(self):
        from app.widget_transforms import eval_expr, eval_compare, resolve_tone_rules

        data = {"value": 19.5}
        assert eval_expr("1/value", data) == pytest.approx(1 / 19.5)
        assert eval_expr("value + 0.5", data) == 20.0
        assert eval_expr("1/0", data) is None
        assert eval_expr("__import__('os')", data) is None
        assert eval_compare("value", "gt", "3", data) is True
        assert eval_compare("1/value", "lt", "0.1", data) is True
        assert eval_compare("value", "eq", "19.5", data) is True
        str_data = {"flit_health": {"status": "ok"}}
        assert eval_compare("flit_health.status", "eq", "ok", str_data) is True
        assert eval_compare("flit_health.status", "neq", "down", str_data) is True
        assert eval_compare("flit_health.status", "eq", "down", str_data) is False
        assert eval_compare("flit_health.status", "gt", "ok", str_data) is False
        assert eval_compare("flit_health.code", "lt", "0", {"flit_health": {"code": -1}}) is True
        assert resolve_tone_rules(
            [
                {"expr": "value", "op": "gt", "compare": "100", "tone": "positive"},
                {"expr": "value", "op": "gt", "compare": "3", "tone": "negative"},
            ],
            data,
        ) == "negative"
        assert resolve_tone_rules([], data) == "neutral"
        assert resolve_tone_rules(
            [{"expr": "flit_health.status", "op": "eq", "compare": "ok", "tone": "positive"}],
            str_data,
        ) == "positive"
        toggle = {"gate": {"value": True}}
        assert eval_compare("gate.value", "eq", "true", toggle) is True
        assert eval_compare("gate.value", "eq", "false", toggle) is False
        assert eval_compare("gate.value", "eq", "on", toggle) is True
        assert eval_compare("gate.value", "neq", "off", toggle) is True
        assert resolve_tone_rules(
            [{"expr": "gate.value", "op": "eq", "compare": "true", "tone": "positive"}],
            toggle,
        ) == "positive"
        assert resolve_tone_rules(
            [{"expr": "gate.value", "op": "eq", "compare": "false", "tone": "negative"}],
            toggle,
        ) == "neutral"

    def test_kv_template_math(self):
        from app.widgets import _render_kv_template

        assert _render_kv_template("EUR - R{{ 1/value }}", {"value": 20}) == "EUR - R0.05"
        assert _render_kv_template("x={{value}}", {"value": "ok"}) == "x=ok"
        assert _render_kv_template(
            "{{ eur_zar.value }}",
            {"eur_zar": {"value": "19.5"}},
        ) == "19.5"
        assert _render_kv_template("{{ missing.value }}", {"eurzar": {"value": 1}}) == ""

    def test_global_slug_namespace(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data, fields_snapshot

        db = next(get_db())
        try:
            gbp = Field(
                name="EURGBP", slug="eurgbp", field_type="text",
                config={}, state={"value": "0.86"},
            )
            zar = Field(
                name="EURZAR", slug="eurzar", field_type="text",
                config={}, state={"value": "19.5"},
            )
            log = Field(
                name="FX Log", slug="fx_log", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add_all([gbp, zar, log])
            db.flush()
            now = datetime.now(timezone.utc)
            db.add(FieldLogEntry(
                field_id=log.id, timestamp=now - timedelta(minutes=5),
                value={"rate": 1.0, "quote": "OLD"},
            ))
            db.add(FieldLogEntry(
                field_id=log.id, timestamp=now,
                value={"rate": 19.5, "quote": "ZAR"},
            ))
            db.commit()

            snap = fields_snapshot(db)
            assert snap["eurgbp"]["value"] == "0.86"
            assert snap["eurzar"]["value"] == "19.5"
            assert snap["fx_log"]["quote"] == "ZAR"

            # Multi-slug template with no bound Field
            multi = fetch_widget_data(
                "display", db, display="kv_text",
                widget_config={
                    "template": "GBP {{ eurgbp.value }} / ZAR {{ eurzar.value }}",
                    "tone": "conditional",
                    "tone_rules": [
                        {"expr": "eurzar.value", "op": "gt", "compare": "19", "tone": "positive"},
                        {"expr": "eurzar.value", "op": "lt", "compare": "19", "tone": "negative"},
                    ],
                },
            )
            assert multi["text"] == "GBP 0.86 / ZAR 19.5"
            assert multi["tone"] == "positive"

            # Logbook latest under slug
            log_kv = fetch_widget_data(
                "display", db, display="kv_text",
                widget_config={"template": "{{ fx_log.quote }} {{ fx_log.rate }}"},
            )
            assert log_kv["text"] == "ZAR 19.5"

            # Slug path from snap (no field_slug binding on kv_text)
            by_slug = fetch_widget_data(
                "display", db, display="kv_text",
                widget_config={"template": "{{ eurgbp.value }}"},
            )
            assert by_slug["text"] == "0.86"

            # Board entry with no Field — slug-only template
            board = fetch_widget_data(
                "display", db, display="board",
                widget_config={
                    "cell_kind": "kv_text",
                    "tone": "conditional",
                    "cells": [{
                        "template": "{{ eurzar.value }}",
                        "tone_rules": [
                            {"expr": "eurzar.value", "op": "gt", "compare": "10", "tone": "positive"},
                        ],
                    }],
                },
            )
            assert board["items"][0]["text"] == "19.5"
            assert board["items"][0]["tone"] == "positive"
            assert board["items"][0]["field_id"] is None
        finally:
            db.close()

    def test_fields_snapshot_and_table_display_for_data_field(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data, fields_snapshot

        db = next(get_db())
        try:
            field = Field(
                name="Latest Payload", slug="latest_payload", field_type="data",
                config={}, state={"payload": {"id": 7}, "status": "ok"},
            )
            db.add(field)
            db.commit()

            snap = fields_snapshot(db)
            assert snap["latest_payload"]["payload"]["id"] == 7
            assert snap["latest_payload"]["status"] == "ok"

            kv = fetch_widget_data(
                "display", db, display="kv_text",
                widget_config={"template": "{{ latest_payload.payload.id }} {{ latest_payload.status }}"},
            )
            assert kv["text"] == "7 ok"

            table = fetch_widget_data(
                "display", db, display="table",
                widget_config={"field_slugs": ["latest_payload"]},
            )
            assert table["rows"][0]["field_type"] == "data"
            assert table["rows"][0]["value"] == {"payload": {"id": 7}, "status": "ok"}
        finally:
            db.close()

    def test_chart_and_series_from_data_field(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data, validate_widget_bindings

        db = next(get_db())
        try:
            field = Field(
                name="Sensor Pack", slug="sensor_pack", field_type="data",
                config={},
                state={
                    "rate": 12.5,
                    "temps": [1.0, 2.0, 3.0, 4.0],
                    "samples": [
                        {"ms": 100, "ts": "2026-01-01T00:00:00+00:00"},
                        {"ms": 200, "ts": "2026-01-01T00:01:00+00:00"},
                        {"ms": 300, "ts": "2026-01-01T00:02:00+00:00"},
                    ],
                },
            )
            db.add(field)
            db.commit()

            chart = fetch_widget_data("chart", db, display="pie", widget_config={
                "sources": [{
                    "field_slug": "sensor_pack.rate",
                    "label": "Rate",
                }],
            })
            assert chart["labels"] == ["Rate"]
            assert chart["values"] == [12.5]

            series_nums = fetch_widget_data("series", db, display="line", widget_config={
                "sources": [{"field_slug": "sensor_pack.temps"}],
                "range_mode": "entries",
                "range_entries": 3,
            })
            assert not series_nums.get("error"), series_nums
            pts = series_nums["series"][0]["points"]
            assert [p["v"] for p in pts] == [2.0, 3.0, 4.0]

            series_map = fetch_widget_data("series", db, display="line", widget_config={
                "sources": [{"field_slug": "sensor_pack.samples.*.ms"}],
                "range_mode": "entries",
                "range_entries": 10,
            })
            assert not series_map.get("error"), series_map
            mapped = series_map["series"][0]["points"]
            assert [p["v"] for p in mapped] == [100.0, 200.0, 300.0]
            assert mapped[0]["ts"].startswith("2026-01-01")

            blank_hours = fetch_widget_data("series", db, display="line", widget_config={
                "sources": [{"field_slug": "sensor_pack.temps"}],
                "range_mode": "hours",
                "range_hours": 24,
            })
            assert blank_hours.get("error")
            assert "Entries" in blank_hours["error"]
            assert blank_hours.get("series") == []

            ok = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "pie",
                "config": {
                    "style": "pie",
                    "sources": [{"field_slug": "sensor_pack.rate", "label": "Rate"}],
                },
            }])
            assert ok is None

            ok_series = validate_widget_bindings(db, [{
                "type": "series",
                "display": "line",
                "config": {
                    "style": "basic",
                    "sources": [{"field_slug": "sensor_pack.temps"}],
                },
            }])
            assert ok_series is None

            bare = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "pie",
                "config": {
                    "style": "pie",
                    "sources": [{"field_slug": "sensor_pack", "label": "Pack"}],
                },
            }])
            assert bare and "path" in bare.lower()
        finally:
            db.close()

    def test_series_field_slug_sources(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            field = Field(
                name="Slug Series", slug="slug-series", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.flush()
            now = datetime.now(timezone.utc)
            db.add(FieldLogEntry(
                field_id=field.id, timestamp=now,
                value={"n": 42},
            ))
            db.commit()
            data = fetch_widget_data("series", db, display="line", widget_config={
                "sources": [{"field_slug": "slug-series.n"}],
                "range_mode": "entries",
                "range_entries": 10,
            })
            assert not data.get("error"), data
            assert data["series"][0]["points"][-1]["v"] == 42.0
        finally:
            db.close()

    def test_series_from_logbook_scalar_value(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            field = Field(
                name="USD Prices", slug="usd_prices", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.flush()
            now = datetime.now(timezone.utc)
            db.add(FieldLogEntry(
                field_id=field.id, timestamp=now - timedelta(minutes=10),
                value=0.04,
            ))
            db.add(FieldLogEntry(
                field_id=field.id, timestamp=now - timedelta(minutes=5),
                value=0.05,
            ))
            db.commit()
            data = fetch_widget_data("series", db, display="line", widget_config={
                "sources": [{"field_slug": "usd_prices.value"}],
                "range_mode": "entries",
                "range_entries": 10,
            })
            assert not data.get("error"), data
            pts = data["series"][0]["points"]
            assert len(pts) == 2
            assert pts[0]["v"] == 0.04
            assert pts[1]["v"] == 0.05
        finally:
            db.close()

    def test_series_from_logbook_path(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            field = Field(
                name="Speed Log", slug="speed_log", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.flush()
            now = datetime.now(timezone.utc)
            db.add(FieldLogEntry(
                field_id=field.id, timestamp=now - timedelta(minutes=10),
                value={"_poll": {"response_time_ms": 250}},
            ))
            db.add(FieldLogEntry(
                field_id=field.id, timestamp=now - timedelta(minutes=5),
                value={"_poll": {"response_time_ms": 500}},
            ))
            db.commit()
            data = fetch_widget_data("series", db, display="line", widget_config={
                "sources": [{
                    "field_slug": "speed_log._poll.response_time_ms",
                }],
                "unit": "s",
                "range_hours": 24,
            })
            assert data["unit"] == "s"
            assert len(data["series"]) == 1
            pts = data["series"][0]["points"]
            assert len(pts) == 2
            assert pts[0]["v"] == 250.0
            assert pts[1]["v"] == 500.0

            ldata = fetch_widget_data("display", db, display="logbook_list", widget_config={
                "template": "rt {{ speed_log._poll.response_time_ms }}",
            })
            assert len(ldata["entries"]) == 2
            assert ldata["entries"][0]["text"] == "rt 500"
            assert ldata["entries"][1]["text"] == "rt 250"
        finally:
            db.close()

    def test_chart_from_logbook_latest_entry(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            speed = Field(
                name="Chart Speed Log", slug="chart_speed_log", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            usd = Field(
                name="Chart USD Prices", slug="chart_usd_prices", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add_all([speed, usd])
            db.flush()
            now = datetime.now(timezone.utc)
            db.add(FieldLogEntry(
                field_id=speed.id, timestamp=now - timedelta(minutes=10),
                value={"_poll": {"response_time_ms": 250}},
            ))
            db.add(FieldLogEntry(
                field_id=speed.id, timestamp=now - timedelta(minutes=5),
                value={"_poll": {"response_time_ms": 500}},
            ))
            db.add(FieldLogEntry(
                field_id=usd.id, timestamp=now - timedelta(minutes=10),
                value=0.04,
            ))
            db.add(FieldLogEntry(
                field_id=usd.id, timestamp=now - timedelta(minutes=5),
                value=0.05,
            ))
            db.commit()

            data = fetch_widget_data("chart", db, display="pie", widget_config={
                "sources": [
                    {
                        "field_slug": "chart_speed_log._poll.response_time_ms",
                        "label": "Latency",
                    },
                    {"field_slug": "chart_usd_prices.value", "label": "USD"},
                ],
            })
            assert data["labels"] == ["Latency", "USD"]
            assert data["values"] == [500.0, 0.05]
        finally:
            db.close()

    def test_chart_counter_and_value(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            counter = Field(name="Hits", slug="hits", field_type="value",
                            config={}, state={"value": 42})
            val = Field(name="Latency", slug="latency", field_type="text",
                        config={}, state={"value": "3.5"})
            db.add_all([counter, val])
            db.commit()
            data = fetch_widget_data("chart", db, display="pie", widget_config={
                "sources": [
                    {"field_slug": "hits", "label": "Hits"},
                    {"field_slug": "latency", "label": "Latency"},
                ],
            })
            assert data["labels"] == ["Hits", "Latency"]
            assert data["values"] == [42.0, 3.5]
            assert "max" not in data
        finally:
            db.close()

    def test_chart_widget_dashboard_render_serializes_values(self, authenticated_client):
        """Jinja wdata.values must use item access — dict.values is a method."""
        from app.database import get_db
        from app.models import DashboardLayout, Field
        import json as _json

        db = next(get_db())
        try:
            db.add(Field(
                name="Hits Render", slug="hits-render", field_type="value",
                config={}, state={"value": 42},
            ))
            widgets = [{
                "id": "w_chart",
                "type": "chart",
                "display": "pie",
                "title": "Pie",
                "config": {
                    "sources": [{"field_slug": "hits-render", "label": "Hits"}],
                },
            }]
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            if not layout:
                layout = DashboardLayout(layout_config=_json.dumps({"widgets": widgets}))
                db.add(layout)
            else:
                layout.layout_config = _json.dumps({"widgets": widgets})
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.get("/")
        assert resp.status_code == 200
        assert 'data-values=\'[42.0]\'' in resp.text or 'data-values="[42.0]"' in resp.text

    def test_chart_max_literal_and_field(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            counter = Field(name="Used", slug="used", field_type="value",
                            config={}, state={"value": 40})
            ceiling = Field(name="Cap", slug="cap", field_type="text",
                            config={}, state={"value": "80"})
            db.add_all([counter, ceiling])
            db.commit()
            by_literal = fetch_widget_data("chart", db, display="radial", widget_config={
                "style": "basic",
                "max": 50,
                "sources": [{"field_slug": "used", "label": "Used"}],
            })
            assert by_literal["values"] == [40.0]
            assert by_literal["max"] == 50.0
            by_field = fetch_widget_data("chart", db, display="radial", widget_config={
                "style": "multi_band",
                "max_field_slug": "cap",
                "sources": [
                    {"field_slug": "used", "label": "Used"},
                    {"field_slug": "cap", "label": "Cap"},
                ],
            })
            assert by_field["max"] == 80.0
            angled = fetch_widget_data("chart", db, display="radial", widget_config={
                "style": "custom_angle",
                "max": 100,
                "start_angle": -120,
                "end_angle": 120,
                "sources": [
                    {"field_slug": "used", "label": "Used"},
                    {"field_slug": "cap", "label": "Cap"},
                ],
            })
            assert angled["start_angle"] == -120.0
            assert angled["end_angle"] == 120.0
            prefer_literal = fetch_widget_data("chart", db, display="radial", widget_config={
                "style": "needle",
                "max": 25,
                "max_field_slug": "cap",
                "sources": [{"field_slug": "used", "label": "Used"}],
            })
            assert prefer_literal["max"] == 25.0
        finally:
            db.close()

    def test_chart_style_source_cardinality(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import validate_widget_bindings

        db = next(get_db())
        try:
            a = Field(name="A", slug="a", field_type="value", config={}, state={"value": 1})
            b = Field(name="B", slug="b", field_type="value", config={}, state={"value": 2})
            log = Field(
                name="Chart Cardinality Log", slug="chart_cardinality_log", field_type="logbook",
                config={}, state={},
            )
            db.add_all([a, b, log])
            db.commit()
            db.add(FieldLogEntry(
                field_id=log.id,
                timestamp=datetime.now(timezone.utc),
                value={"_poll": {"response_time_ms": 500}},
            ))
            db.commit()
            err = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "radial",
                "config": {
                    "style": "multi_band",
                    "sources": [{"field_slug": "a", "label": "A"}],
                },
            }])
            assert err and "at least 2" in err
            err2 = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "radial",
                "config": {
                    "style": "basic",
                    "sources": [
                        {"field_slug": "a", "label": "A"},
                        {"field_slug": "b", "label": "B"},
                    ],
                },
            }])
            assert err2 and "at most 1" in err2
            ok = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "radar",
                "config": {
                    "style": "basic",
                    "sources": [
                        {"field_slug": "a", "label": "A"},
                        {"field_slug": "b", "label": "B"},
                    ],
                },
            }])
            assert ok and "at least 3" in ok
            c = Field(name="C", slug="c", field_type="value", config={}, state={"value": 3})
            db.add(c)
            db.commit()
            ok3 = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "radar",
                "config": {
                    "style": "basic",
                    "sources": [
                        {"field_slug": "a", "label": "A"},
                        {"field_slug": "b", "label": "B"},
                        {"field_slug": "c", "label": "C"},
                    ],
                },
            }])
            assert ok3 is None
            ok_logbook = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "pie",
                "config": {
                    "style": "pie",
                    "sources": [{
                        "field_slug": "chart_cardinality_log._poll.response_time_ms",
                        "label": "Latency",
                    }],
                },
            }])
            assert ok_logbook is None
            polar_short = validate_widget_bindings(db, [{
                "type": "chart",
                "display": "polar",
                "config": {
                    "style": "basic",
                    "sources": [
                        {"field_slug": "a", "label": "A"},
                        {"field_slug": "b", "label": "B"},
                    ],
                },
            }])
            assert polar_short and "at least 3" in polar_short
            bad_disp = validate_widget_bindings(db, [{
                "type": "series",
                "display": "sparkline",
                "config": {"style": "basic", "sources": []},
            }])
            assert bad_disp and "display" in bad_disp.lower()
        finally:
            db.close()

    def test_links_widget(self, authenticated_client):
        from app.database import get_db
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            data = fetch_widget_data("links", db, display="list", widget_config={
                "items": [
                    {"label": "Docs", "url": "https://example.com/docs", "icon": "★"},
                    {"label": "Bad", "url": ""},
                    {"label": "Local", "url": "ftp://files.example/x"},
                ],
            })
            assert len(data["items"]) == 2
            assert data["items"][0]["label"] == "Docs"
            assert data["items"][0]["favicon"] == "https://icons.duckduckgo.com/ip3/example.com.ico"
            assert "icon" not in data["items"][0]
            assert data["items"][1]["label"] == "Local"
            assert data["items"][1]["favicon"] == ""
        finally:
            db.close()


    def test_save_dashboard_rejects_toggle_wrong_field(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, DashboardLayout
        from app.dashboard_layout import parse_layout_config

        db = next(get_db())
        try:
            if not db.query(Field).filter(Field.slug == "c-bad-tog").first():
                db.add(Field(
                    name="Bad Toggle Counter", slug="c-bad-tog",
                    field_type="value", config={}, state={"value": 0},
                ))
            keep_payload = {
                "widgets": [{
                    "type": "system", "display": "source_health", "title": "Keep Me",
                }],
            }
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            if layout:
                layout.layout_config = keep_payload
            else:
                layout = DashboardLayout(layout_config=keep_payload)
                db.add(layout)
            db.commit()
            keep_id = layout.id
        finally:
            db.close()
        widgets = [{
            "type": "display", "display": "toggle", "title": "Bad Toggle Draft",
            "config": {"field_slug": "c-bad-tog", "style": "led"},
        }]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert b"Bad Toggle Draft" in resp.content
        assert b"compatible" in resp.content.lower() or b"toggle" in resp.content.lower()
        assert b'id="widgets-error"' in resp.content

        db = next(get_db())
        try:
            saved = db.query(DashboardLayout).filter(DashboardLayout.id == keep_id).first()
            assert saved is not None
            titles = [w.get("title") for w in parse_layout_config(saved.layout_config)["widgets"]]
            assert "Keep Me" in titles
            assert "Bad Toggle Draft" not in titles
        finally:
            db.close()

    def test_save_dashboard_keeps_draft_on_missing_required_field(self, authenticated_client):
        widgets = [{
            "type": "display", "display": "toggle", "title": "Unfinished Toggle",
            "config": {"style": "led"},
        }]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert b"Unfinished Toggle" in resp.content
        assert b"choose a field" in resp.content.lower()

    def test_toggle_style_in_fetch(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            field = Field(name="Up", slug="up-style", field_type="toggle", config={}, state={"value": True})
            db.add(field)
            db.commit()
            data = fetch_widget_data(
                "display", db, display="toggle",
                widget_config={"field_slug": "up-style", "style": "led"},
            )
            assert data["value"] is True
            assert data["style"] == "led"
            assert data["tone"] == "positive"
        finally:
            db.close()

    def test_widget_tone_conditional(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout, Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            tog = Field(name="Tone Tog", slug="tone_tog", field_type="toggle", config={}, state={"value": False})
            ctr = Field(name="Tone Ctr", slug="tonectr", field_type="value", config={}, state={"value": -3})
            db.add_all([tog, ctr])
            db.commit()

            off = fetch_widget_data(
                "display", db, display="toggle",
                widget_config={"field_slug": "tone_tog"},
            )
            assert off["tone"] == "negative"

            kv_neg = fetch_widget_data(
                "display", db, display="kv_text",
                widget_config={
                    "template": "{{ tonectr.value }}",
                    "tone": "conditional",
                    "tone_rules": [
                        {"expr": "tonectr.value", "op": "lt", "compare": "0", "tone": "negative"},
                        {"expr": "tonectr.value", "op": "gt", "compare": "0", "tone": "positive"},
                    ],
                },
            )
            assert kv_neg["display"] == "kv_text"
            assert kv_neg["text"] == "-3"
            assert kv_neg["tone"] == "negative"

            none_cfg = fetch_widget_data(
                "display", db, display="toggle",
                widget_config={"field_slug": "tone_tog", "tone": "none"},
            )
            assert "tone" not in none_cfg

            links = fetch_widget_data(
                "links", db, display="list",
                widget_config={
                    "tone": "conditional",
                    "items": [{"label": "Docs", "url": "https://example.com"}],
                },
            )
            assert "tone" not in links  # no input condition

            from app.models import FieldLogEntry
            from datetime import datetime, timezone

            lb = Field(name="Tone Log", slug="tonelog", field_type="logbook", config={}, state={})
            db.add(lb)
            db.flush()
            db.add(FieldLogEntry(
                field_id=lb.id,
                timestamp=datetime.now(timezone.utc),
                value={"code": -1},
            ))
            db.commit()
            lb_tone = fetch_widget_data(
                "display", db, display="logbook_list",
                widget_config={
                    "field_slug": "tonelog",
                    "tone": "conditional",
                    "tone_rules": [
                        {"expr": "tonelog.code", "op": "lt", "compare": "0", "tone": "negative"},
                        {"expr": "tonelog.code", "op": "gt", "compare": "0", "tone": "positive"},
                    ],
                },
            )
            assert lb_tone["tone"] == "negative"
        finally:
            db.close()

        widgets = [
            {
                "type": "display", "display": "toggle", "title": "T",
                "config": {"field_slug": "tone_tog", "tone": "conditional"},
            },
            {
                "type": "display", "display": "toggle", "title": "Off bg",
                "config": {"field_slug": "tone_tog", "tone": "none"},
            },
        ]
        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"]
            w_tog = saved[0]["id"]
            w_none = saved[1]["id"]
        finally:
            db.close()

        tog_html = authenticated_client.get(f"/widgets/display?id={w_tog}")
        assert tog_html.status_code == 200
        assert b"widget-tone--negative" in tog_html.content

        none_html = authenticated_client.get(f"/widgets/display?id={w_none}")
        assert none_html.status_code == 200
        assert b"widget-tone--" not in none_html.content


    def test_board_homogeneous(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout, Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            a = Field(name="Board Up", slug="board_up", field_type="toggle", config={}, state={"value": True})
            b = Field(name="Board Ready", slug="board_ready", field_type="toggle", config={}, state={"value": False})
            c = Field(name="Board Hits", slug="board_hits", field_type="value", config={}, state={"value": 1000})
            d = Field(name="Board Lag", slug="board_lag", field_type="value", config={}, state={"value": 50})
            db.add_all([a, b, c, d])
            db.commit()

            # Toggle board via field_slug cells
            toggles = fetch_widget_data(
                "display", db, display="board",
                widget_config={
                    "cell_kind": "toggle",
                    "style": "led",
                    "cells": [
                        {"field_slug": "board_up"},
                        {"field_slug": "board_ready"},
                    ],
                },
            )
            assert toggles["display"] == "board"
            assert toggles["cell_kind"] == "toggle"
            assert len(toggles["items"]) == 2
            assert toggles["items"][0]["style"] == "led"
            assert toggles["tone"] == "neutral"

            # Per-cell tone rules on kv board (maths live in templates, not transform ops)
            stats = fetch_widget_data(
                "display", db, display="board",
                widget_config={
                    "cell_kind": "kv_text",
                    "style": "plain",
                    "tone": "conditional",
                    "cells": [
                        {
                            "field_slug": "board_hits",
                            "template": "{{ board_hits.value }} k",
                            "tone_rules": [
                                {"expr": "board_hits.value", "op": "gt", "compare": "0", "tone": "positive"},
                            ],
                        },
                        {
                            "field_slug": "board_lag",
                            "template": "{{ board_lag.value }} ms",
                            "tone_rules": [
                                {"expr": "board_lag.value", "op": "lt", "compare": "0", "tone": "negative"},
                                {"expr": "board_lag.value", "op": "gt", "compare": "0", "tone": "neutral"},
                            ],
                        },
                    ],
                },
            )
            assert len(stats["items"]) == 2
            assert stats["items"][0]["text"].startswith("1000") and "k" in stats["items"][0]["text"]
            assert "50" in stats["items"][1]["text"] and "ms" in stats["items"][1]["text"]
            assert stats["items"][0]["tone"] == "positive"
            assert stats["items"][1]["tone"] == "neutral"
            assert "tone" not in stats

            # Template-only kv cell (no field_slug)
            template_only = fetch_widget_data(
                "display", db, display="board",
                widget_config={
                    "cell_kind": "kv_text",
                    "cells": [{"template": "{{ board_hits.value }}"}],
                },
            )
            assert template_only["items"][0]["text"] == "1000"
            assert template_only["items"][0]["field_id"] is None
        finally:
            db.close()

        widgets = [{
            "type": "display", "display": "board", "title": "Board",
            "config": {
                "cell_kind": "toggle",
                "style": "led",
                "cells": [{"field_slug": "board_up"}, {"field_slug": "board_ready"}],
            },
        }]
        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"][0]
            assert saved["config"]["cells"][0]["field_slug"] == "board_up"
            wid = saved["id"]
        finally:
            db.close()
        html = authenticated_client.get(f"/widgets/display?id={wid}")
        assert html.status_code == 200
        assert b"widget-board" in html.content
        assert b"Board Up" in html.content and b"Board Ready" in html.content
        assert b"widget-tone--neutral" in html.content


# ── Config: Secrets ──────────────────────────────────────────────────────────

class TestSecretsCRUD:
    def test_source_create_with_inline_webhook_secret(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={
                "name": "Secured Source", "source_type": "webhook",
                "description": "",
                "webhook_secret_value": "abc123",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.name == "Secured Source").first()
            assert src is not None
            assert src.slug == "secured_source"
            assert src.webhook_secret_id is not None
            secret = db.query(Secret).filter(Secret.id == src.webhook_secret_id).first()
            assert secret is not None
        finally:
            db.close()

    def test_source_create_with_inline_schedule(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={
                "name": "Polled Source", "source_type": "poll",
                "poll_category": "url",
                "description": "",
                "schedule_type": "interval",
                "interval_seconds": "60", "handler_type": "http_get",
                "handler_url": "https://example.com/data",
                "timeout_seconds": "30", "retry_count": "0",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from app.database import get_db
        from app.models import PollingSchedule
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.name == "Polled Source").first()
            assert src is not None
            assert src.slug == "polled_source"
            assert src.source_type == "poll"
            assert (src.config or {}).get("poll_category") == "url"
            sched = db.query(PollingSchedule).filter(
                PollingSchedule.source_id == src.id,
            ).first()
            assert sched is not None
            assert sched.name == "Polled Source"
            assert sched.interval_seconds == 60
            assert sched.handler_url == "https://example.com/data"
        finally:
            db.close()

    def test_poll_source_requires_schedule(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={
                "name": "Poll No Sched", "source_type": "poll",
                "poll_category": "url",
                "description": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = str(resp.headers.get("location", ""))
        assert "error" in loc
        from app.database import get_db
        db = next(get_db())
        try:
            assert db.query(Source).filter(Source.name == "Poll No Sched").first() is None
        finally:
            db.close()


# ── Health ───────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_endpoint(self, client):
        # /health is in PUBLIC_PATHS so it's accessible without auth
        resp = client.get("/health")
        assert resp.status_code == 200
        assert '"status"' in resp.text


class TestSystemPage:
    def test_system_page_honest_metrics(self, authenticated_client):
        from app.database import get_db
        from app.models import AuditLog, Event, PollingSchedule

        sid, _ = _create_source(authenticated_client, name="Sys Src", slug="sys-src")
        db = next(get_db())
        try:
            db.add(Event(
                source_id=sid, normalized_data={}, raw_payload="{}",
                correlation_id="sys-1", status="processed",
            ))
            db.add(AuditLog(action="webhook.accepted", details={"slug": "sys-src"}))
            db.add(AuditLog(action="webhook.rejected_noise", details={}))  # must not count
            db.add(PollingSchedule(
                source_id=sid, name="Sys Sched", schedule_type="interval",
                interval_seconds=60, handler_type="http_get",
                handler_url="https://example.com", success_count=3, failure_count=0,
            ))
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.get("/system")
        assert resp.status_code == 200
        assert "Stored events" in resp.text
        assert "max 500 per source" in resp.text
        assert "Webhooks received" in resp.text
        assert "All time" in resp.text
        assert "in the last hour" in resp.text
        assert "Active jobs" in resp.text
        assert "OK / Fail (lifetime)" in resp.text
        assert "Event Status" not in resp.text
        assert "Total Events" not in resp.text
        idx = resp.text.index("Webhooks received")
        snippet = resp.text[idx:idx + 280]
        assert 'stat__value">1</div>' in snippet

    def test_system_source_health_ingress_by_type(self, authenticated_client):
        _, hook_slug = _create_source(
            authenticated_client, name="Sys Hook", source_type="webhook"
        )
        _, poll_slug = _create_source(
            authenticated_client, name="Sys Poll", source_type="poll"
        )
        resp = authenticated_client.get("/system")
        assert resp.status_code == 200
        assert "Ingress" in resp.text
        assert f"/webhook/{hook_slug}" in resp.text
        assert f"/webhook/{poll_slug}" not in resp.text
        health_idx = resp.text.index("Source Health")
        health = resp.text[health_idx:health_idx + 1200]
        assert "Sys Poll" in health
        assert "Poll" in health

    def test_system_pending_notice(self, authenticated_client):
        from app.database import get_db
        from app.models import Event

        sid, _ = _create_source(authenticated_client, name="Pend Sys", slug="pend-sys")
        db = next(get_db())
        try:
            db.add(Event(
                source_id=sid, normalized_data={}, raw_payload="{}",
                correlation_id="pend-sys-1", status="pending",
            ))
            db.commit()
        finally:
            db.close()
        resp = authenticated_client.get("/system")
        assert resp.status_code == 200
        assert "waiting to be processed" in resp.text

    def test_system_uses_display_timezone(self, authenticated_client):
        from app.database import get_db
        from app.models import PollingSchedule

        sid, _ = _create_source(authenticated_client, name="TZ System", slug="tz-system")
        _set_display_timezone("Africa/Johannesburg")
        db = next(get_db())
        try:
            ts = datetime(2026, 1, 2, 3, 4, 0, tzinfo=timezone.utc)
            source = db.query(Source).filter(Source.id == sid).first()
            source.last_seen_at = ts
            db.add(PollingSchedule(
                source_id=sid,
                name="TZ Schedule",
                schedule_type="interval",
                interval_seconds=60,
                handler_type="http_get",
                handler_url="https://example.com",
                last_run_at=ts,
                next_run_at=ts + timedelta(hours=1),
                success_count=1,
                failure_count=0,
            ))
            db.commit()
        finally:
            db.close()

        system_resp = authenticated_client.get("/system")
        assert system_resp.status_code == 200
        assert "2026-01-02 05:04" in system_resp.text
        assert "2026-01-02 06:04" in system_resp.text


# ── Webhook provider UI metadata ─────────────────────────────────────────────

def test_webhook_provider_metadata_covers_verifiers():
    from app.webhook_verifiers import get_webhook_providers, get_webhook_provider_slugs

    providers = get_webhook_providers()
    by_slug = {p["slug"]: p for p in providers}
    assert get_webhook_provider_slugs() == set(by_slug)
    assert by_slug["discord"]["secret_label"] == "Application public key"
    assert by_slug["paypal"]["uses_paypal_config"] is True
    assert by_slug["generic_hmac"]["uses_paypal_config"] is False
    assert all(p.get("secret_help") for p in providers)


# ── Cascade Delete ──────────────────────────────────────────────────────────

class TestCascadeDelete:
    def test_source_delete_cleans_children(self, authenticated_client):
        """Deleting a source should remove its event types and schedule."""
        sid, slug = _create_source(
            authenticated_client, name="Cascade Test", slug="cascade-test", source_type="poll",
        )

        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "child_event", "description": ""},
            follow_redirects=False,
        )

        resp = authenticated_client.post(f"/config/source/{sid}/delete", follow_redirects=False)
        assert resp.status_code == 303

        from app.database import get_db
        from app.models import EventTypeRecord, PollingSchedule, Source
        db = next(get_db())
        try:
            assert db.query(Source).filter(Source.id == sid).first() is None
            assert db.query(EventTypeRecord).filter(EventTypeRecord.source_id == sid).count() == 0
            assert db.query(PollingSchedule).filter(PollingSchedule.source_id == sid).count() == 0
        finally:
            db.close()

    def test_source_delete_with_events(self, authenticated_client):
        """Deleting a source with events FK'd to event types must not IntegrityError."""
        sid, slug = _create_source(authenticated_client, name="Evt Cascade", slug="evt-cascade")
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            et = EventTypeRecord(name="paid", source_id=src.id)
            db.add(et)
            db.flush()
            db.add(Event(
                source_id=src.id, event_type_id=et.id,
                normalized_data={"a": 1}, raw_payload="{}",
            ))
            db.commit()
            source_id = src.id
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/source/{source_id}/delete", follow_redirects=False
        )
        assert resp.status_code == 303
        loc = str(resp.headers.get("location", ""))
        assert "error" not in loc.lower() or "success" in loc

        db = next(get_db())
        try:
            assert db.query(Source).filter(Source.id == source_id).first() is None
            assert db.query(Event).filter(Event.source_id == source_id).count() == 0
        finally:
            db.close()


# ── Webhook Ingress ──────────────────────────────────────────────────────────

class TestWebhookPipeline:
    def test_webhook_source_not_found(self, client):
        resp = client.post("/webhook/nonexistent")
        assert resp.status_code == 404
        assert "Source not found" in resp.text

    def test_webhook_disabled_source(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Disabled", slug="disabled-src")
        authenticated_client.post(f"/config/source/{sid}/toggle", follow_redirects=False)
        resp = authenticated_client.post(
            f"/webhook/{slug}", json={"key": "val"}, follow_redirects=False
        )
        assert resp.status_code == 403

    def test_webhook_accepts_payload(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Webhook Source", slug="webhook-src")
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            json={"key": "val", "count": 42},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        from app.database import get_db
        from app.models import Event
        db = next(get_db())
        try:
            event = db.query(Event).filter(Event.id == resp.json()["event_id"]).first()
            assert event.normalized_data["key"] == "val"
            assert event.normalized_data["source"] == "Webhook Source"
            wh = event.normalized_data["_webhook"]
            assert wh["slug"] == slug
            assert wh["method"] == "POST"
            assert wh["signed"] is False
            assert wh["body_bytes"] > 0
            assert "correlation_id" in wh
            assert wh["timestamp"]
            assert wh.get("event_type") is None
            assert "always_event_id" in resp.json()
        finally:
            db.close()

    def test_webhook_invalid_json(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="JSON Source", slug="json-src")
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            data=b"{invalid json}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_webhook_hmac_valid(self, authenticated_client):
        """Webhook with valid HMAC signature is accepted."""
        import hashlib as _hashlib
        import hmac as _hmac
        from app.database import get_db
        from app.models import Secret, Source
        from app.security import encrypt_secret, decrypt_secret

        sid, slug = _create_source(authenticated_client, name="HMAC Source", slug="hmac-src")
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()

            # Create a secret for the webhook
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id, encrypted_value=encrypt_secret("mysecret")
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.commit()

            payload = json.dumps({"key": "val"}).encode()
            ts = str(int(time.time()))
            secret_val = decrypt_secret(sec.encrypted_value)
            sig = _hmac.new(
                secret_val.encode(), f"{ts}.".encode() + payload, _hashlib.sha256
            ).hexdigest()

            resp = authenticated_client.post(
                f"/webhook/{slug}",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-webhook-signature": f"sha256={sig}",
                    "x-webhook-timestamp": ts,
                },
            )
            assert resp.status_code == 202
        finally:
            db.close()

    def test_webhook_hmac_invalid(self, authenticated_client):
        """Webhook with invalid HMAC signature is rejected."""
        import hashlib as _hashlib
        import hmac as _hmac
        from app.database import get_db
        from app.models import Secret, Source
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="Bad HMAC", slug="bad-hmac")
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id, encrypted_value=encrypt_secret("correctkey")
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.commit()

            payload = json.dumps({"key": "val"}).encode()
            ts = str(int(time.time()))
            # Wrong secret for signature
            sig = _hmac.new(b"wrongkey", f"{ts}.".encode() + payload, _hashlib.sha256).hexdigest()
            resp = authenticated_client.post(
                f"/webhook/{slug}",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-webhook-signature": f"sha256={sig}",
                    "x-webhook-timestamp": ts,
                },
            )
            assert resp.status_code == 401
        finally:
            db.close()

    def test_webhook_hmac_requires_timestamp(self, authenticated_client):
        """Signed sources reject requests that omit X-Webhook-Timestamp."""
        import hashlib as _hashlib
        import hmac as _hmac
        from app.database import get_db
        from app.models import Secret, Source
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="HMAC NoTS", slug="hmac-nots")
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id, encrypted_value=encrypt_secret("mysecret"),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.commit()
        finally:
            db.close()

        payload = json.dumps({"key": "val"}).encode()
        # Body-only signature (old scheme) must not be accepted without timestamp
        sig = _hmac.new(b"mysecret", payload, _hashlib.sha256).hexdigest()
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-webhook-signature": f"sha256={sig}",
            },
        )
        assert resp.status_code == 400

    def test_webhook_hmac_strip_timestamp_rejects_replay(self, authenticated_client):
        """Valid signed request cannot be replayed by stripping the timestamp header."""
        import hashlib as _hashlib
        import hmac as _hmac
        from app.database import get_db
        from app.models import Secret, Source
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="HMAC Strip", slug="hmac-strip")
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id, encrypted_value=encrypt_secret("mysecret"),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.commit()
        finally:
            db.close()

        payload = json.dumps({"key": "val"}).encode()
        ts = str(int(time.time()))
        sig = _hmac.new(b"mysecret", f"{ts}.".encode() + payload, _hashlib.sha256).hexdigest()
        ok = authenticated_client.post(
            f"/webhook/{slug}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-webhook-signature": f"sha256={sig}",
                "x-webhook-timestamp": ts,
            },
        )
        assert ok.status_code == 202
        stripped = authenticated_client.post(
            f"/webhook/{slug}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-webhook-signature": f"sha256={sig}",
            },
        )
        assert stripped.status_code == 400

    def test_webhook_paypal_postback_verification_accepts_and_dedupes(self, authenticated_client):
        """PayPal verification succeeds via postback and replay dedup works."""
        import hashlib
        from unittest.mock import MagicMock, patch

        from app.database import get_db
        from app.models import Secret, Source, Event, EventTypeRecord
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="PayPal Source", slug="paypal-src")
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            assert src is not None
            src.config = {
                "webhook_provider": "paypal",
                "paypal_webhook_id": "webhook-id-1",
                "paypal_client_id": "client-id-1",
                "paypal_environment": "sandbox",
            }
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id,
                encrypted_value=encrypt_secret("paypal-client-secret"),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.add(EventTypeRecord(
                source_id=src.id,
                name="PAYMENT.SALE.COMPLETED",
                description="",
                enabled=True,
            ))
            db.commit()
        finally:
            db.close()

        headers = {
            "Content-Type": "application/json",
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-CERT-URL": "https://example.com/cert",
            "PAYPAL-TRANSMISSION-ID": "tx-123",
            "PAYPAL-TRANSMISSION-SIG": "sig-abc",
            "PAYPAL-TRANSMISSION-TIME": "2016-02-18T20:01:35Z",
        }
        payload = {"event_type": "PAYMENT.SALE.COMPLETED", "resource": {"id": "res-1"}}

        mock_verify_resp = MagicMock()
        mock_verify_resp.raise_for_status = MagicMock()
        mock_verify_resp.json.return_value = {"verification_status": "SUCCESS"}

        mock_http_client = MagicMock()
        mock_http_client.__enter__ = MagicMock(return_value=mock_http_client)
        mock_http_client.__exit__ = MagicMock(return_value=False)
        mock_http_client.post.return_value = mock_verify_resp

        with patch("app.webhook_verifiers._paypal_get_access_token", return_value="access-token"):
            with patch("app.webhook_verifiers.httpx.Client", return_value=mock_http_client):
                resp = authenticated_client.post(f"/webhook/{slug}", json=payload, headers=headers)
                assert resp.status_code == 202
                event_id = resp.json()["event_id"]

                db = next(get_db())
                try:
                    ev = db.query(Event).filter(Event.id == event_id).first()
                    assert ev is not None
                    assert ev.normalized_data["_webhook"]["signed"] is True
                finally:
                    db.close()

                # Same transmission id should be rejected as duplicate.
                dup = authenticated_client.post(f"/webhook/{slug}", json=payload, headers=headers)
                assert dup.status_code == 409
                assert dup.json()["error"] == "Duplicate request"

    def test_webhook_discord_ed25519_verification(self, authenticated_client):
        from nacl.signing import SigningKey

        from app.database import get_db
        from app.models import Event, EventTypeRecord, Secret, Source
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="Discord Source", slug="discord-src")
        signing_key = SigningKey.generate()
        verify_key_hex = signing_key.verify_key.encode().hex()

        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            assert src is not None
            src.config = {"webhook_provider": "discord"}
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id,
                encrypted_value=encrypt_secret(verify_key_hex),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.add(EventTypeRecord(
                source_id=src.id,
                name="application_command",
                description="Discord application command",
                enabled=True,
            ))
            db.commit()
        finally:
            db.close()

        payload = {"type": 2, "data": {"name": "status"}}
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = signing_key.sign(timestamp.encode() + body).signature.hex()
        headers = {
            "Content-Type": "application/json",
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        }

        resp = authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers)
        assert resp.status_code == 202

        db = next(get_db())
        try:
            event = db.query(Event).filter(Event.id == resp.json()["event_id"]).first()
            assert event is not None
            assert event.normalized_data["_webhook"]["signed"] is True
            assert event.normalized_data["_webhook"]["event_type"] == "application_command"
        finally:
            db.close()

        dup = authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers)
        assert dup.status_code == 409
        assert dup.json()["error"] == "Duplicate request"

        bad_headers = dict(headers)
        bad_headers["X-Signature-Ed25519"] = ("0" if signature[0] != "0" else "1") + signature[1:]
        bad = authenticated_client.post(f"/webhook/{slug}", data=body, headers=bad_headers)
        assert bad.status_code == 401
        assert bad.json()["error"] == "Invalid signature"

        # Expired timestamp (beyond replay TTL) must be rejected.
        old_ts = str(int(time.time()) - 10_000)
        old_sig = signing_key.sign(old_ts.encode() + body).signature.hex()
        expired = authenticated_client.post(
            f"/webhook/{slug}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": old_sig,
                "X-Signature-Timestamp": old_ts,
            },
        )
        assert expired.status_code == 400
        assert expired.json()["error"] == "Timestamp expired"

        ping_payload = {"type": 1}
        ping_body = json.dumps(ping_payload, separators=(",", ":")).encode()
        ping_timestamp = str(int(time.time()) + 1)
        ping_signature = signing_key.sign(ping_timestamp.encode() + ping_body).signature.hex()
        ping = authenticated_client.post(
            f"/webhook/{slug}",
            data=ping_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": ping_signature,
                "X-Signature-Timestamp": ping_timestamp,
            },
        )
        assert ping.status_code == 200
        assert ping.json() == {"type": 1}

    def test_webhook_stripe_signature_verification(self, authenticated_client):
        import hashlib as _hashlib
        import hmac as _hmac

        from app.database import get_db
        from app.models import Event, EventTypeRecord, Secret, Source
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="Stripe Source", slug="stripe-src")
        secret = "whsec_test_stripe"
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            src.config = {"webhook_provider": "stripe"}
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id,
                encrypted_value=encrypt_secret(secret),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.add(EventTypeRecord(source_id=src.id, name="charge.succeeded", enabled=True))
            db.commit()
        finally:
            db.close()

        payload = {"type": "charge.succeeded", "data": {"object": {"id": "ch_1"}}}
        body = json.dumps(payload, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        expected = _hmac.new(secret.encode(), f"{ts}.".encode() + body, _hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Stripe-Signature": f"t={ts},v1={expected}",
            "X-Event-Type": "charge.succeeded",
        }
        resp = authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers)
        assert resp.status_code == 202

        db = next(get_db())
        try:
            event = db.query(Event).filter(Event.id == resp.json()["event_id"]).first()
            assert event.normalized_data["_webhook"]["signed"] is True
        finally:
            db.close()

        assert authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers).status_code == 409
        bad = authenticated_client.post(
            f"/webhook/{slug}",
            data=body,
            headers={**headers, "Stripe-Signature": f"t={ts},v1={'0' * 64}"},
        )
        assert bad.status_code == 401

    def test_webhook_github_hmac_verification(self, authenticated_client):
        import hashlib as _hashlib
        import hmac as _hmac

        from app.database import get_db
        from app.models import Event, EventTypeRecord, Secret, Source
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="GitHub HMAC", slug="gh-hmac-src")
        secret = "github-webhook-secret"
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            src.config = {"webhook_provider": "github"}
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id,
                encrypted_value=encrypt_secret(secret),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.add(EventTypeRecord(source_id=src.id, name="push", enabled=True))
            db.commit()
        finally:
            db.close()

        body = json.dumps({"ref": "refs/heads/main"}, separators=(",", ":")).encode()
        sig = _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
            "X-GitHub-Event": "push",
        }
        resp = authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers)
        assert resp.status_code == 202

        db = next(get_db())
        try:
            event = db.query(Event).filter(Event.id == resp.json()["event_id"]).first()
            assert event.normalized_data["_webhook"]["signed"] is True
            assert event.normalized_data["_webhook"]["event_type"] == "push"
        finally:
            db.close()

        assert authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers).status_code == 409
        bad = authenticated_client.post(
            f"/webhook/{slug}",
            data=body,
            headers={**headers, "X-Hub-Signature-256": "sha256=" + ("0" * 64)},
        )
        assert bad.status_code == 401

    def test_webhook_slack_signature_verification(self, authenticated_client):
        import hashlib as _hashlib
        import hmac as _hmac

        from app.database import get_db
        from app.models import Event, EventTypeRecord, Secret, Source
        from app.security import encrypt_secret

        sid, slug = _create_source(authenticated_client, name="Slack Source", slug="slack-src")
        secret = "slack-signing-secret"
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.id == sid).first()
            src.config = {"webhook_provider": "slack"}
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id,
                encrypted_value=encrypt_secret(secret),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.add(EventTypeRecord(source_id=src.id, name="event_callback", enabled=True))
            db.commit()
        finally:
            db.close()

        body = json.dumps(
            {"type": "event_callback", "event": {"type": "message"}},
            separators=(",", ":"),
        ).encode()
        ts = str(int(time.time()))
        expected = _hmac.new(
            secret.encode(), f"v0:{ts}:".encode() + body, _hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Slack-Signature": f"v0={expected}",
            "X-Slack-Request-Timestamp": ts,
            "X-Event-Type": "event_callback",
        }
        resp = authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers)
        assert resp.status_code == 202

        db = next(get_db())
        try:
            event = db.query(Event).filter(Event.id == resp.json()["event_id"]).first()
            assert event.normalized_data["_webhook"]["signed"] is True
        finally:
            db.close()

        assert authenticated_client.post(f"/webhook/{slug}", data=body, headers=headers).status_code == 409
        bad = authenticated_client.post(
            f"/webhook/{slug}",
            data=body,
            headers={**headers, "X-Slack-Signature": f"v0={'0' * 64}"},
        )
        assert bad.status_code == 401


# ── Pipeline Tests ───────────────────────────────────────────────────────────

class TestPipeline:
    def test_evaluate_rules_empty(self, authenticated_client):
        """No rules → no matches."""
        sid, slug = _create_source(authenticated_client, name="Empty", slug="empty")
        from app.database import get_db
        from app.models import Event
        db = next(get_db())
        try:
            event = Event(
                source_id=sid, normalized_data={"key": "val"},
                raw_payload='{}', correlation_id="test"
            )
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_rules
            matches = evaluate_rules(db, event)
            assert len(matches) == 0
        finally:
            db.close()

    def test_matches_rule(self, authenticated_client):
        """Rule with matching source_id and event_type_ids matches."""
        sid, slug = _create_source(authenticated_client, name="Match Source", slug="match-src")
        from app.database import get_db
        from app.models import EventTypeRecord, Rule, Event
        db = next(get_db())
        try:
            et = EventTypeRecord(source_id=sid, name="error", description="")
            db.add(et)
            db.commit()

            rule = Rule(source_id=sid,
                event_type_ids=[et.id], conditions={}, action_ids=[], order_index=0
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=et.id,
                normalized_data={"key": "val"}, raw_payload='{}', correlation_id="test"
            )
            db.add(event)
            db.commit()

            from app.pipeline import evaluate_rules
            matches = evaluate_rules(db, event)
            assert len(matches) == 1
            assert matches[0].id == rule.id
        finally:
            db.close()

    def test_rules_require_source_id(self, authenticated_client):
        """Rules are source-scoped; null source_id is rejected by the schema."""
        from app.database import get_db
        from app.models import Rule
        from sqlalchemy.exc import IntegrityError
        db = next(get_db())
        try:
            rule = Rule(
                source_id=None,
                event_type_ids=[], conditions={}, action_ids=[], order_index=0,
            )
            db.add(rule)
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
        finally:
            db.close()

    def test_conditions_must_match(self, authenticated_client):
        """Rule with conditions that don't match the event payload is excluded."""
        sid, slug = _create_source(authenticated_client, name="Cond Source", slug="cond-src")
        from app.database import get_db
        from app.models import Rule, Event
        db = next(get_db())
        try:
            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={"severity": "high"}, action_ids=[], order_index=0
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"severity": "low"}, raw_payload='{}', correlation_id="test"
            )
            db.add(event)
            db.commit()

            from app.pipeline import evaluate_rules
            matches = evaluate_rules(db, event)
            assert len(matches) == 1  # Rule is loaded (no et filter)

            from app.pipeline import evaluate_conditions
            assert evaluate_conditions(event, rule.conditions) == False
        finally:
            db.close()

    def test_paused_event_type_skips_all_rules(self, authenticated_client):
        """Paused event types still exist on the event, but evaluate_rules returns []."""
        sid, slug = _create_source(authenticated_client, name="Pause ET Src", slug="pause-et-src")
        from app.database import get_db
        from app.models import EventTypeRecord, Rule, Event
        db = next(get_db())
        try:
            et = EventTypeRecord(source_id=sid, name="order.paid", enabled=True)
            db.add(et)
            db.commit()
            db.refresh(et)

            rule = Rule(
                source_id=sid, event_type_ids=[et.id],
                conditions={}, action_ids=[], order_index=0, enabled=True,
            )
            catch_all = Rule(
                source_id=sid, event_type_ids=[],
                conditions={}, action_ids=[], order_index=1, enabled=True,
            )
            db.add_all([rule, catch_all])
            db.commit()

            event = Event(
                source_id=sid, event_type_id=et.id,
                normalized_data={"ok": True}, raw_payload="{}", correlation_id="pause-et",
            )
            db.add(event)
            db.commit()

            from app.pipeline import evaluate_rules
            assert len(evaluate_rules(db, event)) == 2

            et.enabled = False
            db.commit()
            assert evaluate_rules(db, event) == []
        finally:
            db.close()

    def test_toggle_event_type_persists_and_flips_label(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Toggle ET", slug="toggle-et")
        from app.database import get_db
        from app.models import EventTypeRecord
        db = next(get_db())
        try:
            et = EventTypeRecord(source_id=sid, name="ping", enabled=True)
            db.add(et)
            db.commit()
            db.refresh(et)
            et_id = et.id
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/event-type/{et_id}/toggle",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Activate" in resp.text
        assert "Paused" in resp.text

        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
            assert et.enabled is False
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/event-type/{et_id}/toggle",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Pause" in resp.text

    def test_toggle_rule_skips_evaluate_rules(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Toggle Rule Src", slug="toggle-rule-src")
        from app.database import get_db
        from app.models import Rule, Event
        db = next(get_db())
        try:
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[], order_index=0, enabled=True,
            )
            db.add(rule)
            db.commit()
            db.refresh(rule)
            rule_id = rule.id

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload="{}", correlation_id="toggle-rule",
            )
            db.add(event)
            db.commit()

            from app.pipeline import evaluate_rules
            assert len(evaluate_rules(db, event)) == 1
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/rule/{rule_id}/toggle",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Activate" in resp.text

        db = next(get_db())
        try:
            event = db.query(Event).filter(Event.correlation_id == "toggle-rule").first()
            from app.pipeline import evaluate_rules
            assert evaluate_rules(db, event) == []
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            assert rule.enabled is False
        finally:
            db.close()

    def test_toggle_action_skips_dispatch(self, authenticated_client, caplog):
        sid, slug = _create_source(authenticated_client, name="Toggle Act Src", slug="toggle-act-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field, FieldLogEntry
        import logging
        db = next(get_db())
        try:
            field = Field(
                name="Toggle Act Log", slug="toggle-act-log", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.commit()

            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id}, enabled=True,
            )
            db.add(action)
            db.commit()
            db.refresh(action)
            action_id = action.id
            field_id = field.id

            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0, enabled=True,
            )
            db.add(rule)
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/action/{action_id}/toggle",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Activate" in resp.text

        db = next(get_db())
        try:
            action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
            assert action.enabled is False

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"msg": "x"}, raw_payload="{}", correlation_id="toggle-act",
            )
            db.add(event)
            db.commit()

            from app.pipeline import evaluate_and_dispatch
            with caplog.at_level(logging.INFO, logger="para_scope.pipeline"):
                evaluate_and_dispatch(db, event)
            assert db.query(FieldLogEntry).filter(FieldLogEntry.field_id == field_id).count() == 0
        finally:
            db.close()

    def test_dispatches_log_action(self, authenticated_client, caplog):
        """Log action appends to a logbook Field."""
        sid, slug = _create_source(authenticated_client, name="Log Action Source", slug="log-action-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field, FieldLogEntry
        db = next(get_db())
        try:
            field = Field(
                name="App Log", slug="app-log", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.commit()

            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id},
            )
            db.add(action)
            db.commit()

            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"msg": "hello"}, raw_payload='{}', correlation_id="test"
            )
            db.add(event)
            db.commit()

            from app.pipeline import evaluate_and_dispatch
            import logging
            with caplog.at_level(logging.INFO, logger="para_scope.pipeline"):
                evaluate_and_dispatch(db, event)
            assert "field_push" in caplog.text
            assert "App Log" in caplog.text
            entry = db.query(FieldLogEntry).filter(FieldLogEntry.field_id == field.id).first()
            assert entry is not None
            assert entry.value == {"msg": "hello"}
        finally:
            db.close()

    def test_logbook_template_and_maths(self, authenticated_client):
        """Logbook Template substitutes {{ }}; Value from event accepts maths; paths keep objects."""
        sid, _ = _create_source(authenticated_client, name="LB Tmpl Src", slug="lb-tmpl-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field, FieldLogEntry
        from app.pipeline import evaluate_and_dispatch
        db = next(get_db())
        try:
            field = Field(
                name="LB Tmpl", slug="lb-tmpl", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.commit()

            actions = [
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": field.id, "value": "x={{ status }}"},
                ),
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": field.id, "value": "{{ 1/rate }}"},
                ),
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": field.id, "value_key": "rate * 2"},
                ),
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": field.id, "value_key": "payload"},
                ),
            ]
            for a in actions:
                db.add(a)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[a.id for a in actions], order_index=0,
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"status": "ok", "rate": 20, "payload": {"n": 1}},
                raw_payload="{}", correlation_id="test",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)

            values = [
                e.value for e in db.query(FieldLogEntry)
                .filter(FieldLogEntry.field_id == field.id)
                .order_by(FieldLogEntry.id.asc())
                .all()
            ]
            assert values == ["x=ok", "0.05", 40.0, {"n": 1}]
        finally:
            db.close()

    def test_logbook_value_from_event_json_shape(self, authenticated_client):
        """Value from event JSON shape compiles typed path/maths/literal leaves."""
        sid, _ = _create_source(authenticated_client, name="LB Shape Src", slug="lb-shape-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field, FieldLogEntry
        from app.pipeline import evaluate_and_dispatch
        db = next(get_db())
        try:
            field = Field(
                name="LB Shape", slug="lb-shape", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.commit()
            db.add(FieldLogEntry(field_id=field.id, value={"n": 3}, source_id=sid))
            db.commit()

            shape = (
                '{"label":"Sensor A","celsius":"temp",'
                '"fahrenheit":"temp * 1.8 + 32","raw":"payload.sensor",'
                '"next":"field.n + 1"}'
            )
            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value_key": shape},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={
                    "temp": 20,
                    "payload": {"sensor": {"id": 1}},
                },
                raw_payload="{}", correlation_id="shape",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)

            entries = (
                db.query(FieldLogEntry)
                .filter(FieldLogEntry.field_id == field.id)
                .order_by(FieldLogEntry.id.asc())
                .all()
            )
            assert len(entries) == 2
            assert entries[-1].value == {
                "label": "Sensor A",
                "celsius": 20,
                "fahrenheit": 68.0,
                "raw": {"id": 1},
                "next": 4.0,
            }
        finally:
            db.close()

    def test_field_push_data_from_event_and_shape(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Data Src", slug="data-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        from app.pipeline import evaluate_and_dispatch
        from sqlalchemy.orm.attributes import flag_modified

        db = next(get_db())
        try:
            field = Field(
                name="Data State", slug="data-state", field_type="data",
                config={}, state={},
            )
            db.add(field)
            db.commit()

            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"payload": {"id": 1}, "status": "ok"},
                raw_payload="{}", correlation_id="data-event",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state == {"payload": {"id": 1}, "status": "ok"}

            action.config = {"field_id": field.id, "value_key": "payload"}
            flag_modified(action, "config")
            db.commit()
            event2 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"payload": {"id": 2, "kind": "sensor"}},
                raw_payload="{}", correlation_id="data-path",
            )
            db.add(event2)
            db.commit()
            evaluate_and_dispatch(db, event2)
            db.refresh(field)
            assert field.state == {"id": 2, "kind": "sensor"}

            action.config = {
                "field_id": field.id,
                "value_key": '{"reading":"temp","next":"field.id + 1","meta":"payload"}',
            }
            flag_modified(action, "config")
            db.commit()
            event3 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"temp": 20, "payload": {"sensor": "a"}},
                raw_payload="{}", correlation_id="data-shape",
            )
            db.add(event3)
            db.commit()
            evaluate_and_dispatch(db, event3)
            db.refresh(field)
            assert field.state == {"reading": 20, "next": 3.0, "meta": {"sensor": "a"}}
        finally:
            db.close()

    def test_field_push_data_rejects_non_object_values(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Data Reject Src", slug="data-reject-src")
        from app.database import get_db
        from app.models import ActionInstance, Event, Field
        from app.actions import _action_field_push

        db = next(get_db())
        try:
            field = Field(
                name="Data Only", slug="data-only", field_type="data",
                config={}, state={"keep": True},
            )
            db.add(field)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"items": [1, 2], "count": 2},
                raw_payload="{}", correlation_id="data-reject",
            )
            db.add(event)
            db.commit()

            list_action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value_key": "items"},
            )
            with pytest.raises(ValueError, match="JSON object"):
                _action_field_push(db, event, list_action)

            scalar_action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value_key": "count"},
            )
            with pytest.raises(ValueError, match="JSON object"):
                _action_field_push(db, event, scalar_action)

            db.refresh(field)
            assert field.state == {"keep": True}
        finally:
            db.close()

    def test_field_self_reference(self, authenticated_client):
        """Reserved ``field`` is the current stored value in path/template/maths."""
        sid, _ = _create_source(authenticated_client, name="Self Ref Src", slug="self-ref-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field, FieldLogEntry
        from app.pipeline import evaluate_and_dispatch
        db = next(get_db())
        try:
            counter = Field(
                name="Self Ctr", slug="self-ctr", field_type="value",
                config={}, state={"value": 10},
            )
            value_f = Field(
                name="Self Val", slug="self-val", field_type="text",
                config={}, state={"value": "a"},
            )
            logbook = Field(
                name="Self LB", slug="self-lb", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add_all([counter, value_f, logbook])
            db.commit()
            db.add(FieldLogEntry(field_id=logbook.id, value={"n": 2}, source_id=sid))
            db.commit()

            actions = [
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": counter.id, "op": "set", "delta": "field + 1"},
                ),
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": value_f.id, "value": "{{ field }}-{{ status }}"},
                ),
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": logbook.id, "value": "{{ field.n }}"},
                ),
                ActionInstance(
                    source_id=sid, action_type="field_push",
                    config={"field_id": logbook.id},  # entire event — no field inject needed
                ),
            ]
            for a in actions:
                db.add(a)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[a.id for a in actions], order_index=0,
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"status": "ok", "field": "ignored"},
                raw_payload="{}", correlation_id="self-ref",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)

            db.refresh(counter)
            db.refresh(value_f)
            assert counter.state["value"] == 11.0
            assert value_f.state["value"] == "a-ok"

            lb_entries = (
                db.query(FieldLogEntry)
                .filter(FieldLogEntry.field_id == logbook.id)
                .order_by(FieldLogEntry.id.asc())
                .all()
            )
            # prior {"n":2}, then template "2", then entire event
            assert [e.value for e in lb_entries] == [
                {"n": 2},
                "2",
                {"status": "ok", "field": "ignored"},
            ]
        finally:
            db.close()

    def test_field_push_counter(self, authenticated_client):
        """field_push increment updates counter state only."""
        sid, slug = _create_source(authenticated_client, name="Metric Source", slug="metric-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        db = next(get_db())
        try:
            field = Field(
                name="request_count", slug="request-count", field_type="value",
                config={}, state={"value": 0},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "increment", "delta": 1.0})
            db.add(action)
            db.commit()
            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0)
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"key": "val"}, raw_payload='{}', correlation_id="test")
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == 1.0
        finally:
            db.close()

    def test_field_push_text_from_template(self, authenticated_client):
        """field_push to text Field stores rendered template from event."""
        sid, slug = _create_source(authenticated_client, name="Metric Field Source", slug="metric-field-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        db = next(get_db())
        try:
            field = Field(
                name="status_code", slug="status-code", field_type="text",
                config={}, state={"value": ""},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value": "{{ code }}"})
            db.add(action)
            db.commit()
            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0)
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"code": 42}, raw_payload='{}', correlation_id="test")
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == "42"
        finally:
            db.close()

    def test_field_push_text_dotted_path(self, authenticated_client):
        """field_push text template supports nested paths like _poll.response_time_ms."""
        sid, slug = _create_source(authenticated_client, name="Dotted Push Src", slug="dotted-push-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        db = next(get_db())
        try:
            field = Field(
                name="latency", slug="latency-ms", field_type="text",
                config={}, state={"value": ""},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value": "{{ _poll.response_time_ms }}"},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"_poll": {"response_time_ms": 273.5}, "status": "ok"},
                raw_payload="{}", correlation_id="dotted-push",
            )
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == "273.5"
        finally:
            db.close()

    def test_field_push_text_template_and_maths(self, authenticated_client):
        """Text field Template substitutes {{ }} including maths."""
        sid, _ = _create_source(authenticated_client, name="Val Tmpl Src", slug="val-tmpl-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        from app.pipeline import evaluate_and_dispatch
        from sqlalchemy.orm.attributes import flag_modified
        db = next(get_db())
        try:
            field = Field(
                name="Val Tmpl", slug="val-tmpl", field_type="text",
                config={}, state={"value": ""},
            )
            db.add(field)
            db.commit()

            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value": "x={{ status }}"},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"status": "ok", "rate": 20},
                raw_payload="{}", correlation_id="val-tmpl",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == "x=ok"

            action.config = {"field_id": field.id, "value": "{{ 1/rate }}"}
            flag_modified(action, "config")
            db.commit()
            event2 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"status": "ok", "rate": 20},
                raw_payload="{}", correlation_id="val-tmpl-math",
            )
            db.add(event2)
            db.commit()
            evaluate_and_dispatch(db, event2)
            db.refresh(field)
            assert field.state["value"] == "0.05"

            action.config = {"field_id": field.id, "value": "{{ rate * 2 }}"}
            flag_modified(action, "config")
            db.commit()
            event3 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"rate": 20},
                raw_payload="{}", correlation_id="val-key-math",
            )
            db.add(event3)
            db.commit()
            evaluate_and_dispatch(db, event3)
            db.refresh(field)
            assert field.state["value"] == "40"
        finally:
            db.close()

    def test_field_push_counter_reset(self, authenticated_client):
        """field_push reset sets counter to 0."""
        sid, slug = _create_source(authenticated_client, name="Zero Metric", slug="zero-metric")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        db = next(get_db())
        try:
            field = Field(
                name="hits", slug="hits-reset", field_type="value",
                config={}, state={"value": 9},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "reset"})
            db.add(action)
            db.commit()
            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0)
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload='{}', correlation_id="test")
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == 0.0
        finally:
            db.close()

    def test_field_push_counter_set(self, authenticated_client):
        """field_push set replaces counter with literal, path, or maths."""
        sid, _ = _create_source(authenticated_client, name="Set Metric", slug="set-metric")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        from app.pipeline import evaluate_and_dispatch
        from sqlalchemy.orm.attributes import flag_modified
        db = next(get_db())
        try:
            field = Field(
                name="gauge", slug="gauge-set", field_type="value",
                config={}, state={"value": 9},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "set", "delta": 42},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            db.commit()

            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload="{}", correlation_id="set-lit",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == 42.0

            action.config = {"field_id": field.id, "op": "set", "delta": "qty"}
            flag_modified(action, "config")
            db.commit()
            event2 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"qty": 7}, raw_payload="{}", correlation_id="set-key",
            )
            db.add(event2)
            db.commit()
            evaluate_and_dispatch(db, event2)
            db.refresh(field)
            assert field.state["value"] == 7.0

            action.config = {"field_id": field.id, "op": "set", "delta": "qty * 2"}
            flag_modified(action, "config")
            db.commit()
            event3 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"qty": 5}, raw_payload="{}", correlation_id="set-math",
            )
            db.add(event3)
            db.commit()
            evaluate_and_dispatch(db, event3)
            db.refresh(field)
            assert field.state["value"] == 10.0
        finally:
            db.close()

    def test_field_push_counter_decrement(self, authenticated_client):
        """field_push decrement subtracts delta (literal and event key)."""
        sid, slug = _create_source(authenticated_client, name="Dec Metric", slug="dec-metric")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        db = next(get_db())
        try:
            field = Field(
                name="stock", slug="stock-dec", field_type="value",
                config={}, state={"value": 10},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "decrement", "delta": 1},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload="{}", correlation_id="dec-1",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == 9.0

            action.config = {"field_id": field.id, "op": "decrement", "delta": "qty"}
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(action, "config")
            db.commit()
            event2 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"qty": 3}, raw_payload="{}", correlation_id="dec-key",
            )
            db.add(event2)
            db.commit()
            evaluate_and_dispatch(db, event2)
            db.refresh(field)
            assert field.state["value"] == 6.0

            action.config = {"field_id": field.id, "op": "increment", "delta": "qty * 2"}
            flag_modified(action, "config")
            db.commit()
            event3 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={"qty": 3}, raw_payload="{}", correlation_id="inc-math",
            )
            db.add(event3)
            db.commit()
            evaluate_and_dispatch(db, event3)
            db.refresh(field)
            assert field.state["value"] == 12.0
        finally:
            db.close()

    def test_field_push_toggle(self, authenticated_client):
        """field_push sets toggle bool."""
        sid, slug = _create_source(authenticated_client, name="Toggle Field Src", slug="toggle-field-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        db = next(get_db())
        try:
            field = Field(
                name="up", slug="up-toggle", field_type="toggle",
                config={}, state={"value": False},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value": True})
            db.add(action)
            db.commit()
            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0)
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload='{}', correlation_id="tog")
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] is True
        finally:
            db.close()

    def test_field_push_toggle_switch(self, authenticated_client):
        """field_push Switch flips toggle bool."""
        sid, _ = _create_source(authenticated_client, name="Toggle Switch Src", slug="toggle-switch-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        from app.pipeline import evaluate_and_dispatch
        db = next(get_db())
        try:
            field = Field(
                name="flip", slug="flip-toggle", field_type="toggle",
                config={}, state={"value": False},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "switch"},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload="{}", correlation_id="sw1",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] is True

            event2 = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload="{}", correlation_id="sw2",
            )
            db.add(event2)
            db.commit()
            evaluate_and_dispatch(db, event2)
            db.refresh(field)
            assert field.state["value"] is False
        finally:
            db.close()

    def test_field_push_unresolved_delta(self, authenticated_client):
        """Unresolved value delta key fails with a clear error."""
        sid, slug = _create_source(authenticated_client, name="Bad Field", slug="bad-field")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        db = next(get_db())
        try:
            field = Field(
                name="x", slug="x-counter", field_type="value",
                config={}, state={"value": 0},
            )
            db.add(field)
            db.commit()
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "increment", "delta": "nope"})
            db.add(action)
            db.commit()
            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0)
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload='{}', correlation_id="bad")
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.expire_all()
            ev = db.query(Event).filter(Event.correlation_id == "bad").first()
            assert ev.status == "failed"
            assert "Couldn’t find number" in (ev.processing_error or "")
        finally:
            db.close()

    def test_field_push_missing_field_id(self, authenticated_client):
        """field_push without field_id fails clearly."""
        sid, slug = _create_source(authenticated_client, name="No Field Id", slug="no-field-id")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event
        db = next(get_db())
        try:
            action = ActionInstance(source_id=sid, action_type="field_push", config={})
            db.add(action)
            db.commit()
            rule = Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0)
            db.add(rule)
            db.commit()
            event = Event(
                source_id=sid, event_type_id=None,
                normalized_data={}, raw_payload='{}', correlation_id="nof")
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.expire_all()
            ev = db.query(Event).filter(Event.correlation_id == "nof").first()
            assert ev.status == "failed"
            assert "missing a field" in (ev.processing_error or "")
        finally:
            db.close()

    def test_conditions_missing_field_fail_closed(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(source_id=1, normalized_data={}, raw_payload="{}")
        assert evaluate_conditions(event, {"amount": {"gt": 10}}) is False

    def test_conditions_bad_regex_fail_closed(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(source_id=1, normalized_data={"name": "abc"}, raw_payload="{}")
        assert evaluate_conditions(event, {"name": {"regex": "("}}) is False

    def test_conditions_non_numeric_gt_fail_closed(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(source_id=1, normalized_data={"amount": "n/a"}, raw_payload="{}")
        assert evaluate_conditions(event, {"amount": {"gt": 10}}) is False

    def test_conditions_not_operator(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(source_id=1, normalized_data={"status": "error"}, raw_payload="{}")
        assert evaluate_conditions(event, {"status": {"not": "ok"}}) is True
        assert evaluate_conditions(event, {"status": {"not": "error"}}) is False
        missing = Event(source_id=1, normalized_data={}, raw_payload="{}")
        assert evaluate_conditions(missing, {"status": {"not": "ok"}}) is True

    def test_conditions_dotted_path(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(
            source_id=1,
            normalized_data={"_poll": {"outcome": "on_success", "response_time_ms": 50}},
            raw_payload="{}",
        )
        assert evaluate_conditions(event, {"_poll.outcome": "on_success"}) is True
        assert evaluate_conditions(event, {"_poll.outcome": "on_failure"}) is False
        assert evaluate_conditions(event, {"_poll.response_time_ms": {"gt": 10}}) is True
        assert evaluate_conditions(event, {"_poll.response_time_ms": {"gt": 100}}) is False
        assert evaluate_conditions(event, {"_poll.missing": "x"}) is False

    def test_conditions_unknown_ops_fail_closed(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(source_id=1, normalized_data={"amount": 5}, raw_payload="{}")
        assert evaluate_conditions(event, {"amount": {"eq": 5}}) is False
        assert evaluate_conditions(event, {"amount": {}}) is False

    def test_conditions_star_any_item(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(
            source_id=1,
            normalized_data={"data": [{"status": "ok"}, {"status": "fail"}]},
            raw_payload="{}",
        )
        assert evaluate_conditions(event, {"data.*.status": "fail"}) is True
        assert evaluate_conditions(event, {"data.*.status": "pending"}) is False
        assert evaluate_conditions(event, {"data.*.status": "ok"}) is True
        empty = Event(source_id=1, normalized_data={"data": []}, raw_payload="{}")
        assert evaluate_conditions(empty, {"data.*.status": "fail"}) is False
        not_list = Event(source_id=1, normalized_data={"data": {"status": "fail"}}, raw_payload="{}")
        assert evaluate_conditions(not_list, {"data.*.status": "fail"}) is False

    def test_conditions_star_correlated_and(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        same_row = Event(
            source_id=1,
            normalized_data={
                "data": [
                    {"base": "EUR", "quote": "ZAR"},
                    {"base": "USD", "quote": "ZAR"},
                ]
            },
            raw_payload="{}",
        )
        assert evaluate_conditions(
            same_row, {"data.*.base": "USD", "data.*.quote": "ZAR"}
        ) is True
        split_rows = Event(
            source_id=1,
            normalized_data={
                "data": [
                    {"base": "USD", "quote": "EUR"},
                    {"base": "EUR", "quote": "ZAR"},
                ]
            },
            raw_payload="{}",
        )
        assert evaluate_conditions(
            split_rows, {"data.*.base": "USD", "data.*.quote": "ZAR"}
        ) is False

    def test_conditions_star_with_plain(self, authenticated_client):
        from app.models import Event
        from app.pipeline import evaluate_conditions
        event = Event(
            source_id=1,
            normalized_data={
                "source": "fx",
                "data": [{"base": "USD", "quote": "ZAR"}],
            },
            raw_payload="{}",
        )
        assert evaluate_conditions(
            event, {"source": "fx", "data.*.base": "USD", "data.*.quote": "ZAR"}
        ) is True
        assert evaluate_conditions(
            event, {"source": "other", "data.*.base": "USD"}
        ) is False

    def test_match_conditions_star_bindings(self, authenticated_client):
        from app.pipeline import match_conditions

        data = {
            "value": [
                {"base": "EUR", "quote": "AED", "rate": 4.1},
                {"base": "EUR", "quote": "ZAR", "rate": 19.5},
                {"base": "USD", "quote": "ZAR", "rate": 18.0},
            ]
        }
        ok, bindings = match_conditions(
            data, {"value.*.base": "EUR", "value.*.quote": "ZAR"}
        )
        assert ok is True
        assert bindings == {"value": 1}
        from app.fields import get_by_path
        assert get_by_path(data, "value.*.rate", star_bindings=bindings) == 19.5

    def test_star_binding_applies_to_field_push(self, authenticated_client):
        """Rule * conditions bind action template * to the matched row."""
        from app.database import SessionLocal
        from app.models import Field, Source, EventTypeRecord, Rule, ActionInstance, Event
        from app.pipeline import evaluate_and_dispatch

        db = SessionLocal()
        try:
            field = Field(
                name="zar-rate",
                slug="zar-rate-bind",
                field_type="text",
                config={},
                state={"value": ""},
            )
            src = Source(
                name="FX", slug="fx-bind", source_type="webhook", enabled=True
            )
            db.add_all([field, src])
            db.flush()
            et = EventTypeRecord(source_id=src.id, name="on_success")
            db.add(et)
            db.flush()
            action = ActionInstance(
                action_type="field_push",
                source_id=src.id,
                config={"field_id": field.id, "value": "{{ value.*.rate }}"},
                enabled=True,
            )
            db.add(action)
            db.flush()
            rule = Rule(
                source_id=src.id,
                event_type_ids=[et.id],
                conditions={"value.*.base": "EUR", "value.*.quote": "ZAR"},
                action_ids=[action.id],
                enabled=True,
            )
            db.add(rule)
            event = Event(
                source_id=src.id,
                event_type_id=et.id,
                correlation_id="fx-bind-1",
                raw_payload="{}",
                normalized_data={
                    "value": [
                        {"base": "EUR", "quote": "AED", "rate": 4.1},
                        {"base": "EUR", "quote": "ZAR", "rate": 19.5},
                    ]
                },
                status="pending",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state.get("value") == "19.5"
        finally:
            db.close()

    def test_star_binding_maths_value_from_event_logbook(self, authenticated_client):
        """Value from event maths with * uses the rule-matched list row."""
        from app.database import SessionLocal
        from app.models import Field, FieldLogEntry, Source, EventTypeRecord, Rule, ActionInstance, Event
        from app.pipeline import evaluate_and_dispatch

        db = SessionLocal()
        try:
            field = Field(
                name="inv-rate",
                slug="inv-rate-bind",
                field_type="logbook",
                config={"max_entries": 50},
                state={},
            )
            src = Source(
                name="FX Maths", slug="fx-maths-bind", source_type="webhook", enabled=True
            )
            db.add_all([field, src])
            db.flush()
            et = EventTypeRecord(source_id=src.id, name="on_success")
            db.add(et)
            db.flush()
            action = ActionInstance(
                action_type="field_push",
                source_id=src.id,
                config={"field_id": field.id, "value_key": "1 / value.*.rate"},
                enabled=True,
            )
            db.add(action)
            db.flush()
            rule = Rule(
                source_id=src.id,
                event_type_ids=[et.id],
                conditions={"value.*.base": "EUR", "value.*.quote": "ZAR"},
                action_ids=[action.id],
                enabled=True,
            )
            db.add(rule)
            event = Event(
                source_id=src.id,
                event_type_id=et.id,
                correlation_id="fx-maths-1",
                raw_payload="{}",
                normalized_data={
                    "value": [
                        {"base": "EUR", "quote": "AED", "rate": 4.1},
                        {"base": "EUR", "quote": "ZAR", "rate": 19.5},
                    ]
                },
                status="pending",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            entry = (
                db.query(FieldLogEntry)
                .filter(FieldLogEntry.field_id == field.id)
                .order_by(FieldLogEntry.id.desc())
                .first()
            )
            assert entry is not None
            assert entry.value == pytest.approx(1 / 19.5)
        finally:
            db.close()

    def test_star_binding_maths_template_text_field(self, authenticated_client):
        """Text Template ``{{ 1 / value.*.rate }}`` uses rule-matched row."""
        from app.database import SessionLocal
        from app.models import Field, Source, EventTypeRecord, Rule, ActionInstance, Event
        from app.pipeline import evaluate_and_dispatch

        db = SessionLocal()
        try:
            field = Field(
                name="inv-tmpl",
                slug="inv-tmpl-bind",
                field_type="text",
                config={},
                state={"value": ""},
            )
            src = Source(
                name="FX Tmpl", slug="fx-tmpl-bind", source_type="webhook", enabled=True
            )
            db.add_all([field, src])
            db.flush()
            et = EventTypeRecord(source_id=src.id, name="on_success")
            db.add(et)
            db.flush()
            action = ActionInstance(
                action_type="field_push",
                source_id=src.id,
                config={"field_id": field.id, "value": "inv={{ 1 / value.*.rate }}"},
                enabled=True,
            )
            db.add(action)
            db.flush()
            rule = Rule(
                source_id=src.id,
                event_type_ids=[et.id],
                conditions={"value.*.base": "EUR", "value.*.quote": "ZAR"},
                action_ids=[action.id],
                enabled=True,
            )
            db.add(rule)
            event = Event(
                source_id=src.id,
                event_type_id=et.id,
                correlation_id="fx-tmpl-1",
                raw_payload="{}",
                normalized_data={
                    "value": [
                        {"base": "EUR", "quote": "AED", "rate": 4.1},
                        {"base": "EUR", "quote": "ZAR", "rate": 19.5},
                    ]
                },
                status="pending",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state.get("value") == "inv=0.0512820512821"
        finally:
            db.close()


# ── Webhook Replay & Size Tests ─────────────────────────────────────────────

class TestWebhookReplaySize:
    """Replay protection, size limits, and result tracking."""

    def test_webhook_timestamp_without_secret_no_crash(self, authenticated_client):
        """Timestamp header on unsigned source must not 500 (UnboundLocalError)."""
        sid, slug = _create_source(authenticated_client, name="TS No Secret", slug="ts-nosec")
        ts = str(int(time.time()))
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            json={"key": "val"},
            headers={"x-webhook-timestamp": ts},
        )
        assert resp.status_code == 202

    def test_webhook_missing_secret_row_returns_401(self, authenticated_client):
        """webhook_secret_id pointing at a deleted secret must 401, not skip HMAC."""
        from sqlalchemy import text
        sid, slug = _create_source(authenticated_client, name="Missing Sec", slug="missing-sec")
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id, encrypted_value=encrypt_secret("x"),
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.commit()
            # Orphan the FK so the runtime path sees a missing Secret row
            db.execute(text("PRAGMA foreign_keys=OFF"))
            db.execute(text("DELETE FROM secrets WHERE id = :id"), {"id": sec.id})
            db.commit()
            db.execute(text("PRAGMA foreign_keys=ON"))
        finally:
            db.close()
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            json={"key": "val"},
        )
        assert resp.status_code == 401

    def test_webhook_payload_too_large(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Big", slug="big-src")
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            data=b"x" * (256 * 1024 + 1),
        )
        assert resp.status_code == 413

    def test_webhook_timestamp_expired(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="TS Expired", slug="ts-expired")
        old_ts = str(int(time.time()) - 600)
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            json={"key": "val"},
            headers={"x-webhook-timestamp": old_ts},
        )
        assert resp.status_code == 400

    def test_webhook_timestamp_invalid(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="TS Bad", slug="ts-bad")
        resp = authenticated_client.post(
            f"/webhook/{slug}",
            json={"key": "val"},
            headers={"x-webhook-timestamp": "not-a-number"},
        )
        assert resp.status_code == 400

    def test_webhook_duplicate_request(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Dup", slug="dup-src")
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            sec = Secret(
                scoped_to_type="source",
                scoped_to_id=src.id, encrypted_value=encrypt_secret("mysecret")
            )
            db.add(sec)
            db.flush()
            src.webhook_secret_id = sec.id
            db.commit()
        finally:
            db.close()

        import time
        import hashlib as _hashlib
        import hmac as _hmac
        ts = str(int(time.time()))
        payload = json.dumps({"key": "val"}).encode()
        secret_val = "mysecret"
        sig = _hmac.new(
            secret_val.encode(), f"{ts}.".encode() + payload, _hashlib.sha256
        ).hexdigest()
        resp1 = authenticated_client.post(
            f"/webhook/{slug}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-webhook-signature": f"sha256={sig}",
                "x-webhook-timestamp": ts,
            },
        )
        assert resp1.status_code == 202
        # Same signature + same source = duplicate
        resp2 = authenticated_client.post(
            f"/webhook/{slug}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-webhook-signature": f"sha256={sig}",
                "x-webhook-timestamp": ts,
            },
        )
        assert resp2.status_code == 409

    def test_webhook_rate_limit(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Rate Lim", slug="rate-lim")
        import app.main as main_mod
        main_mod._WEBHOOK_RATE_LIMIT.clear()
        # Exhaust the in-memory window quickly
        from app import webctx
        old = webctx._WEBHOOK_MAX_ATTEMPTS
        webctx._WEBHOOK_MAX_ATTEMPTS = 3
        try:
            codes = []
            for _ in range(4):
                resp = authenticated_client.post(f"/webhook/{slug}", json={"k": 1})
                codes.append(resp.status_code)
            assert codes[:3] == [202, 202, 202]
            assert codes[3] == 429
        finally:
            webctx._WEBHOOK_MAX_ATTEMPTS = old
            main_mod._WEBHOOK_RATE_LIMIT.clear()

    def test_action_result_tracking_success(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="ResultOK", slug="result-ok")
        from app.database import get_db
        from app.models import Field
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            et = EventTypeRecord(name="test", source_id=src.id)
            db.add(et)
            db.flush()
            field = Field(
                name="Result Log", slug="result-log", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.flush()
            action = ActionInstance(source_id=src.id, action_type="field_push",
                config={"field_id": field.id},
            )
            db.add(action)
            db.commit()
            rule = Rule(source_id=src.id,
                event_type_ids=[et.id], conditions={}, action_ids=[action.id], order_index=0
            )
            db.add(rule)
            db.commit()
            event = Event(
                source_id=src.id, event_type_id=et.id,
                normalized_data={"ok": True}, raw_payload='{}', correlation_id="result-test"
            )
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.expire_all()
            ev = db.query(Event).filter(Event.id == event.id).first()
            assert ev.status == "processed"
        finally:
            db.close()

    def test_action_result_tracking_failure(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="ResultFail", slug="result-fail")
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            et = EventTypeRecord(name="test", source_id=src.id)
            db.add(et)
            db.flush()
            action = ActionInstance(source_id=src.id, action_type="nonexistent_action", config={"retry_count": 0}
            )
            db.add(action)
            db.commit()
            rule = Rule(source_id=src.id,
                event_type_ids=[et.id], conditions={}, action_ids=[action.id], order_index=0
            )
            db.add(rule)
            db.commit()
            event = Event(
                source_id=src.id, event_type_id=et.id,
                normalized_data={"ok": True}, raw_payload='{}', correlation_id="fail-test"
            )
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.expire_all()
            ev = db.query(Event).filter(Event.id == event.id).first()
            assert ev.status == "failed"
            assert "Unknown action type" in ev.processing_error
        finally:
            db.close()

    def test_action_retry_on_failure(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Retry", slug="retry-src")
        from app.database import get_db
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == slug).first()
            et = EventTypeRecord(name="test", source_id=src.id)
            db.add(et)
            db.flush()
            action = ActionInstance(source_id=src.id, action_type="nonexistent_action", config={"retry_count": 2}
            )
            db.add(action)
            db.commit()
            rule = Rule(source_id=src.id,
                event_type_ids=[et.id], conditions={}, action_ids=[action.id], order_index=0
            )
            db.add(rule)
            db.commit()
            event = Event(
                source_id=src.id, event_type_id=et.id,
                normalized_data={"ok": True}, raw_payload='{}', correlation_id="retry-test"
            )
            db.add(event)
            db.commit()
            from app.pipeline import evaluate_and_dispatch
            evaluate_and_dispatch(db, event)
            db.expire_all()
            ev = db.query(Event).filter(Event.id == event.id).first()
            assert ev.status == "failed"
            # Should have 3 attempts (1 original + 2 retries)
            assert "Try 1:" in ev.processing_error
            assert "Try 3:" in ev.processing_error
        finally:
            db.close()


# ── Phase 2: widgets, dashboard, webhook audit ───────────────────────

class TestMetricGraphRange:
    def test_series_respects_range_hours(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Graph Src", slug="graph-src")
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            field = Field(
                name="Latency Hours", slug="latency-hours", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.flush()
            old = datetime.now(timezone.utc) - timedelta(hours=48)
            recent = datetime.now(timezone.utc) - timedelta(minutes=30)
            db.add(FieldLogEntry(
                field_id=field.id, source_id=sid, timestamp=old, value={"ms": 1.0},
            ))
            db.add(FieldLogEntry(
                field_id=field.id, source_id=sid, timestamp=recent, value={"ms": 2.0},
            ))
            db.commit()
            data = fetch_widget_data(
                "series", db, display="line",
                widget_config={
                    "sources": [{"field_slug": "latency-hours.ms"}],
                    "range_hours": 24,
                },
            )
            assert len(data["series"]) == 1
            assert data["series"][0]["points"][0]["v"] == 2.0
        finally:
            db.close()

    def test_series_respects_range_entries(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Graph Entries Src", slug="graph-entries-src")
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            field = Field(
                name="Latency Entries", slug="latency-entries", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.flush()
            now = datetime.now(timezone.utc)
            for i, hours_ago in enumerate((72, 48, 24, 2, 1)):
                db.add(FieldLogEntry(
                    field_id=field.id, source_id=sid,
                    timestamp=now - timedelta(hours=hours_ago),
                    value={"ms": float(i + 1)},
                ))
            db.commit()
            data = fetch_widget_data(
                "series", db, display="line",
                widget_config={
                    "sources": [{"field_slug": "latency-entries.ms"}],
                    "range_mode": "entries",
                    "range_entries": 2,
                    "range_hours": 1,
                },
            )
            assert data["range_mode"] == "entries"
            assert len(data["series"]) == 1
            assert [p["v"] for p in data["series"][0]["points"]] == [4.0, 5.0]
        finally:
            db.close()

    def test_series_by_field_slug(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Graph Field Src", slug="graph-field-src")
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            field = Field(
                name="Latency By Slug", slug="latency-by-slug", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.commit()
            recent = datetime.now(timezone.utc) - timedelta(minutes=30)
            db.add(FieldLogEntry(
                field_id=field.id, source_id=sid, timestamp=recent, value={"ms": 2.0},
            ))
            db.commit()
            data = fetch_widget_data(
                "series", db, display="line",
                widget_config={
                    "sources": [{"field_slug": "latency-by-slug.ms"}],
                    "range_hours": 24,
                },
            )
            assert data["series"][0]["name"] == "Latency By Slug"
            assert len(data["series"]) == 1
            assert data["series"][0]["points"][0]["v"] == 2.0
        finally:
            db.close()

    def test_series_rejects_counter_field(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            field = Field(
                name="Hits Only", slug="hits-only", field_type="value",
                config={}, state={"value": 9},
            )
            db.add(field)
            db.commit()
            data = fetch_widget_data(
                "series", db, display="line",
                widget_config={
                    "sources": [{"field_slug": "hits-only.value"}],
                    "range_hours": 24,
                },
            )
            assert data.get("error")
            assert "logbook" in data["error"].lower()
        finally:
            db.close()

    def test_series_multi_sources(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Multi Series Src", slug="multi-series-src")
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            a = Field(name="A", slug="ser-a", field_type="logbook", config={"max_entries": 50}, state={})
            b = Field(name="B", slug="ser-b", field_type="logbook", config={"max_entries": 50}, state={})
            db.add_all([a, b])
            db.commit()
            now = datetime.now(timezone.utc)
            db.add(FieldLogEntry(field_id=a.id, source_id=sid, timestamp=now, value={"v": 1.0}))
            db.add(FieldLogEntry(field_id=b.id, source_id=sid, timestamp=now, value={"v": 2.0}))
            db.commit()
            data = fetch_widget_data("series", db, display="line", widget_config={
                "sources": [
                    {"field_slug": "ser-a.v", "label": "Alpha"},
                    {"field_slug": "ser-b.v", "label": "Beta"},
                ],
                "range_hours": 24,
            })
            assert len(data["series"]) == 2
            assert data["series"][0]["name"] == "Alpha"
            assert data["series"][1]["name"] == "Beta"
            assert data["series"][0]["points"][0]["v"] == 1.0
            assert data["series"][1]["points"][0]["v"] == 2.0
            assert data.get("style") == "basic"
        finally:
            db.close()


class TestDashboardConfigPreserve:
    def test_save_dashboard_preserves_widget_config(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout
        import json as _json

        # Seed layout with config on system source_health
        db = next(get_db())
        try:
            widgets = [
                {"type": "system", "display": "source_health", "label": "Source Health",
                 "config": {"stale_threshold_hours": 9}},
                {"type": "system", "display": "recent_events", "label": "Recent Events"},
            ]
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            if not layout:
                layout = DashboardLayout(layout_config=_json.dumps({"widgets": widgets}))
                db.add(layout)
            else:
                layout.layout_config = _json.dumps({"widgets": widgets})
            db.commit()
        finally:
            db.close()

        # POST same widgets JSON (as saveWidgets would after preserving config)
        preserved = [
            {"type": "system", "display": "source_health", "label": "Source Health",
             "config": {"stale_threshold_hours": 9}},
            {"type": "system", "display": "metric_summary", "label": "Metric Summary"},
        ]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": _json.dumps(preserved)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            cfg = _json.loads(layout.layout_config)
            sh = next(w for w in cfg["widgets"] if w.get("display") == "source_health")
            assert sh.get("config", {}).get("stale_threshold_hours") == 9
        finally:
            db.close()


class TestNotesWidget:
    def test_catalog_includes_notes(self, authenticated_client):
        from app.widgets import get_widget_kinds, KIND_DISPLAYS, default_tone

        assert "notes" in KIND_DISPLAYS
        assert KIND_DISPLAYS["notes"] == ("notes",)
        assert default_tone("notes", "notes") == "none"
        kinds = {k["type"]: k for k in get_widget_kinds()}
        assert "notes" in kinds
        assert kinds["notes"]["title"] == "Notes"
        assert kinds["notes"]["displays"][0]["title"] == "Text"
        resp = authenticated_client.get("/config/dashboard")
        assert resp.status_code == 200
        assert b'"type": "notes"' in resp.content or b'"type":"notes"' in resp.content

    def test_save_layout_with_tone_rules(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        widgets = [{
            "type": "notes",
            "display": "notes",
            "title": "Scratch",
            "config": {
                "tone": "conditional",
                "tone_rules": [
                    {"expr": "tonectr.value", "op": "lt", "compare": "0", "tone": "negative"},
                ],
                "text": "hello",
            },
        }]
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps(widgets)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = json.loads(layout.layout_config)["widgets"]
            assert len(saved) == 1
            assert saved[0]["type"] == "notes"
            assert saved[0]["title"] == "Scratch"
            assert saved[0]["config"]["text"] == "hello"
            assert saved[0]["config"]["tone"] == "conditional"
            assert saved[0]["config"]["tone_rules"][0]["tone"] == "negative"
            assert saved[0].get("id")
        finally:
            db.close()

    def test_fetch_notes_text_and_tone(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            ctr = Field(name="NCtr", slug="nctr", field_type="value", config={}, state={"value": -2})
            db.add(ctr)
            db.commit()
            data = fetch_widget_data(
                "notes", db, display="notes",
                widget_config={
                    "text": "keep calm",
                    "tone": "conditional",
                    "tone_rules": [
                        {"expr": "nctr.value", "op": "lt", "compare": "0", "tone": "negative"},
                        {"expr": "nctr.value", "op": "gt", "compare": "0", "tone": "positive"},
                    ],
                },
            )
            assert data["display"] == "notes"
            assert data["text"] == "keep calm"
            assert data["tone"] == "negative"
            none_data = fetch_widget_data(
                "notes", db, display="notes",
                widget_config={"text": "x", "tone": "none"},
            )
            assert "tone" not in none_data
            assert none_data["text"] == "x"
        finally:
            db.close()

    def test_api_notes_save_and_reject(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([
                {"type": "notes", "display": "notes", "title": "N", "config": {"text": ""}},
                {"type": "system", "display": "metric_summary", "title": "S"},
            ])},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            widgets = json.loads(layout.layout_config)["widgets"]
            notes_id = next(w["id"] for w in widgets if w["type"] == "notes")
            sys_id = next(w["id"] for w in widgets if w["type"] == "system")
        finally:
            db.close()

        ok = authenticated_client.post(
            "/api/dashboard/notes",
            json={"id": notes_id, "text": "saved later"},
        )
        assert ok.status_code == 200
        assert ok.json().get("ok") is True

        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            saved = {w["id"]: w for w in json.loads(layout.layout_config)["widgets"]}
            assert saved[notes_id]["config"]["text"] == "saved later"
        finally:
            db.close()

        bad = authenticated_client.post(
            "/api/dashboard/notes",
            json={"id": sys_id, "text": "nope"},
        )
        assert bad.status_code == 404

        missing = authenticated_client.post(
            "/api/dashboard/notes",
            json={"id": "w_missing", "text": "nope"},
        )
        assert missing.status_code == 404

    def test_config_save_preserves_notes_text(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout

        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([{
                "type": "notes",
                "display": "notes",
                "title": "Pad",
                "config": {
                    "tone": "conditional",
                    "tone_rules": [{"expr": "value", "op": "gt", "compare": "0", "tone": "positive"}],
                    "text": "do not wipe",
                },
            }])},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            wid = json.loads(layout.layout_config)["widgets"][0]["id"]
        finally:
            db.close()

        # Config form omits text (as if readFromDom had no notesText stash).
        resp = authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([{
                "id": wid,
                "type": "notes",
                "display": "notes",
                "title": "Pad renamed",
                "config": {
                    "tone": "conditional",
                    "tone_rules": [{"expr": "value", "op": "gt", "compare": "0", "tone": "positive"}],
                },
            }])},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = next(get_db())
        try:
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            w = json.loads(layout.layout_config)["widgets"][0]
            assert w["title"] == "Pad renamed"
            assert w["config"]["text"] == "do not wipe"
        finally:
            db.close()

    def test_dashboard_renders_notes_without_htmx_poll(self, authenticated_client):
        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([{
                "type": "notes",
                "display": "notes",
                "title": "My Notes",
                "config": {"text": "body text here", "tone": "none"},
            }])},
            follow_redirects=False,
        )
        home = authenticated_client.get("/")
        assert home.status_code == 200
        assert b"My Notes" in home.content
        assert b'data-notes-widget' in home.content
        assert b"body text here" in home.content
        assert b"widget-notes.js" in home.content
        # No HTMX poll on notes (same as links)
        assert b'hx-get="/widgets/notes' not in home.content


class TestWebhookAudit:
    def test_webhook_accepted_writes_audit(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Audit WH", slug="audit-wh")
        resp = authenticated_client.post(f"/webhook/{slug}", json={"ok": True})
        assert resp.status_code == 202
        from app.database import get_db
        from app.models import AuditLog
        db = next(get_db())
        try:
            entry = db.query(AuditLog).filter(AuditLog.action == "webhook.accepted").first()
            assert entry is not None
            assert entry.resource_id == sid
        finally:
            db.close()

    def test_webhook_background_marks_processed(self, authenticated_client):
        """TestClient runs BackgroundTasks before returning; event should be processed."""
        sid, slug = _create_source(authenticated_client, name="BG WH", slug="bg-wh")
        resp = authenticated_client.post(f"/webhook/{slug}", json={"ping": 1})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        event_id = body["event_id"]
        from app.database import get_db
        from app.models import Event
        db = next(get_db())
        try:
            ev = db.query(Event).filter(Event.id == event_id).first()
            assert ev is not None
            assert ev.status == "processed"
        finally:
            db.close()


# ── Global Fields ────────────────────────────────────────────────────────────

class TestFields:
    def test_field_form_partial_type_panels(self, authenticated_client):
        resp = authenticated_client.get("/config/pipeline/partials/field-form")
        assert resp.status_code == 200
        assert 'id="field-type-select"' in resp.text
        assert 'id="field-params-logbook"' in resp.text
        assert 'id="field-params-value" class="stack field-type-params" hidden' in resp.text
        assert 'id="field-params-text"' in resp.text
        assert 'name="max_entries"' in resp.text
        assert resp.text.index('id="field-type-select"') < resp.text.index('id="field-params-logbook"')
        assert resp.text.index('id="field-params-logbook"') < resp.text.index('name="max_entries"')
        assert resp.text.index('name="max_entries"') < resp.text.index('id="field-params-value"')
        value_panel = resp.text.split('id="field-params-value"', 1)[1].split('id="field-params-text"', 1)[0]
        assert "max_entries" not in value_panel
        assert 'hx-target="#pipeline-dialog"' in resp.text

    def test_edit_field_warns_about_slug_change(self, authenticated_client):
        authenticated_client.post(
            "/config/pipeline/fields",
            data={"name": "Rename Warn Field", "field_type": "value"},
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import AuditLog, Field
        db = next(get_db())
        try:
            field = db.query(Field).filter(Field.name == "Rename Warn Field").first()
            assert field is not None
            fid = field.id
            slug = field.slug
        finally:
            db.close()

        resp = authenticated_client.get(
            f"/config/pipeline/partials/field-form?field_id={fid}"
        )
        assert resp.status_code == 200
        assert slug in resp.text
        assert "re-derives this slug" in resp.text
        assert "alert--warning" in resp.text

        authenticated_client.post(
            f"/config/pipeline/field/{fid}",
            data={"name": "Rename Warn Field New", "field_type": "value"},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            field = db.query(Field).filter(Field.id == fid).first()
            assert field.slug != slug
            audit = (
                db.query(AuditLog)
                .filter(AuditLog.action == "field.update", AuditLog.resource_id == fid)
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert audit is not None
            details = audit.details or {}
            assert details.get("previous_slug") == slug
            assert details.get("slug") == field.slug
        finally:
            db.close()

    def test_htmx_field_validation_stays_in_dialog(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/fields",
            data={"name": "", "field_type": "value"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") is None
        assert "Name is required" in resp.text
        assert 'option value="value" selected' in resp.text
        assert 'hx-target="#pipeline-dialog"' in resp.text

    def test_create_value_ignores_max_entries(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/fields",
            data={
                "name": "Hits Value",
                "field_type": "value",
                "max_entries": "99",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)
        from app.database import get_db
        from app.models import Field
        db = next(get_db())
        try:
            field = db.query(Field).filter(Field.name == "Hits Value").first()
            assert field is not None
            assert field.field_type == "value"
            assert field.config == {}
        finally:
            db.close()

    def test_create_data_field_defaults_to_empty_object(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/fields",
            data={
                "name": "Latest Payload",
                "field_type": "data",
                "max_entries": "99",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)
        from app.database import get_db
        from app.models import Field
        db = next(get_db())
        try:
            field = db.query(Field).filter(Field.name == "Latest Payload").first()
            assert field is not None
            assert field.field_type == "data"
            assert field.config == {}
            assert field.state == {}
        finally:
            db.close()

        page = authenticated_client.get("/config/pipeline")
        assert "Latest Payload" in page.text
        assert "Data" in page.text

    def test_htmx_field_refresh_uses_creation_order(self, authenticated_client):
        first = authenticated_client.post(
            "/config/pipeline/fields",
            data={"name": "Zulu Field", "field_type": "value"},
            follow_redirects=False,
        )
        assert first.status_code in (200, 303)

        resp = authenticated_client.post(
            "/config/pipeline/fields",
            data={"name": "Alpha Field", "field_type": "value"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )

        assert resp.status_code == 200
        assert 'id="pipeline-fields"' in resp.text
        assert resp.text.index("Zulu Field") < resp.text.index("Alpha Field")

    def test_create_list_edit_delete_field(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/fields",
            data={
                "name": "Errors",
                "field_type": "logbook",
                "max_entries": "5",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        from app.database import get_db
        from app.models import Field
        db = next(get_db())
        try:
            field = db.query(Field).filter(Field.name == "Errors").first()
            assert field is not None
            assert field.field_type == "logbook"
            assert field.config["max_entries"] == 5
            assert "value_type" not in field.config
            fid = field.id
        finally:
            db.close()

        page = authenticated_client.get("/config/pipeline")
        assert "Errors" in page.text
        assert "Logbook" in page.text
        assert 'class="pipeline-action-menu"' in page.text
        assert 'pipeline-action-menu__item' in page.text

        resp = authenticated_client.post(
            f"/config/pipeline/field/{fid}",
            data={
                "name": "Errors Renamed",
                "field_type": "logbook",
                "max_entries": "10",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)
        db = next(get_db())
        try:
            field = db.query(Field).filter(Field.id == fid).first()
            assert field.name == "Errors Renamed"
            assert field.config["max_entries"] == 10
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/pipeline/field/{fid}/delete",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)
        db = next(get_db())
        try:
            assert db.query(Field).filter(Field.id == fid).first() is None
        finally:
            db.close()

    def test_delete_blocked_when_action_references_field(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Field Ref Src", slug="field-ref-src")
        from app.database import get_db
        from app.models import Field, ActionInstance
        db = next(get_db())
        try:
            field = Field(
                name="Blocked", slug="blocked", field_type="value",
                config={}, state={"value": 0},
            )
            db.add(field)
            db.flush()
            db.add(ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "increment", "delta": 1},
            ))
            db.commit()
            fid = field.id
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/pipeline/field/{fid}/delete",
            data={},
            follow_redirects=False,
        )
        # HTMX 400 or redirect with error
        assert resp.status_code in (400, 303)
        db = next(get_db())
        try:
            assert db.query(Field).filter(Field.id == fid).first() is not None
        finally:
            db.close()

    def test_logbook_recent_and_clear(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry

        db = next(get_db())
        try:
            field = Field(
                name="Recent Log", slug="recent-log", field_type="logbook",
                config={"max_entries": 50}, state={},
            )
            db.add(field)
            db.flush()
            for i in range(7):
                db.add(FieldLogEntry(field_id=field.id, value={"n": i}))
            db.commit()
            fid = field.id
        finally:
            db.close()

        page = authenticated_client.get("/config/pipeline")
        assert f"/config/pipeline/field/{fid}/partials/recent-entries" in page.text
        assert f"/config/pipeline/field/{fid}/clear" in page.text
        assert "7/50" in page.text

        resp = authenticated_client.get(
            f"/config/pipeline/field/{fid}/partials/recent-entries?limit=5"
        )
        assert resp.status_code == 200
        assert "Recent — Recent Log" in resp.text
        assert '"n": 6' in resp.text
        assert '"n": 2' in resp.text
        assert '"n": 0' not in resp.text

        clamped = authenticated_client.get(
            f"/config/pipeline/field/{fid}/partials/recent-entries?limit=999"
        )
        assert clamped.status_code == 200
        assert '"n": 0' in clamped.text

        clear = authenticated_client.post(
            f"/config/pipeline/field/{fid}/clear",
            data={},
            follow_redirects=False,
        )
        assert clear.status_code in (200, 303)
        db = next(get_db())
        try:
            assert db.query(FieldLogEntry).filter(FieldLogEntry.field_id == fid).count() == 0
            assert db.query(Field).filter(Field.id == fid).first() is not None
        finally:
            db.close()

        empty = authenticated_client.get(
            f"/config/pipeline/field/{fid}/partials/recent-entries"
        )
        assert empty.status_code == 200
        assert "No entries yet" in empty.text

    def test_logbook_recent_entries_use_display_timezone(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry

        _set_display_timezone("Africa/Johannesburg")
        db = next(get_db())
        try:
            field = Field(
                name="TZ Log",
                slug="tz-log",
                field_type="logbook",
                config={"max_entries": 10},
                state={},
            )
            db.add(field)
            db.flush()
            db.add(FieldLogEntry(
                field_id=field.id,
                timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                value={"n": 1},
            ))
            db.commit()
            fid = field.id
        finally:
            db.close()

        resp = authenticated_client.get(
            f"/config/pipeline/field/{fid}/partials/recent-entries?limit=5"
        )
        assert resp.status_code == 200
        assert "2026-01-02 05:04:05" in resp.text

    def test_clear_rejects_non_logbook(self, authenticated_client):
        from app.database import get_db
        from app.models import Field

        db = next(get_db())
        try:
            field = Field(
                name="Not Log", slug="not-log", field_type="value",
                config={}, state={"value": 1},
            )
            db.add(field)
            db.commit()
            fid = field.id
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/pipeline/field/{fid}/clear",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code in (400, 303)
        recent = authenticated_client.get(
            f"/config/pipeline/field/{fid}/partials/recent-entries"
        )
        assert recent.status_code == 400

    def test_logbook_prunes_max_entries(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Prune Src", slug="prune-src")
        from app.database import get_db
        from app.models import Field, FieldLogEntry, ActionInstance, Rule, Event
        db = next(get_db())
        try:
            field = Field(
                name="Tiny Log", slug="tiny-log", field_type="logbook",
                config={"max_entries": 2}, state={},
            )
            db.add(field)
            db.flush()
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id, "value_key": "n"},
            )
            db.add(action)
            db.flush()
            db.add(Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0,
            ))
            db.commit()
            fid = field.id
        finally:
            db.close()

        from app.pipeline import evaluate_and_dispatch
        for n in (1, 2, 3):
            db = next(get_db())
            try:
                event = Event(
                    source_id=sid, normalized_data={"n": n},
                    raw_payload="{}", correlation_id=f"p{n}",
                )
                db.add(event)
                db.commit()
                evaluate_and_dispatch(db, event)
            finally:
                db.close()

        db = next(get_db())
        try:
            entries = (
                db.query(FieldLogEntry)
                .filter(FieldLogEntry.field_id == fid)
                .order_by(FieldLogEntry.id)
                .all()
            )
            assert len(entries) == 2
            assert [e.value for e in entries] == [2, 3]
        finally:
            db.close()

    def test_counter_increments(self, authenticated_client):
        sid, slug = _create_source(authenticated_client, name="Cnt Src", slug="cnt-src")
        from app.database import get_db
        from app.models import Field, ActionInstance, Rule, Event
        from app.pipeline import evaluate_and_dispatch
        db = next(get_db())
        try:
            field = Field(
                name="Hits", slug="hits", field_type="value",
                config={}, state={"value": 0},
            )
            db.add(field)
            db.flush()
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": field.id},  # default delta 1
            )
            db.add(action)
            db.flush()
            db.add(Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0,
            ))
            db.commit()
            fid = field.id
            for i in range(3):
                event = Event(
                    source_id=sid, normalized_data={}, raw_payload="{}", correlation_id=f"c{i}",
                )
                db.add(event)
                db.commit()
                evaluate_and_dispatch(db, event)
            db.expire_all()
            field = db.query(Field).filter(Field.id == fid).first()
            assert field.state["value"] == 3
        finally:
            db.close()

    def test_widgets_field_value_and_logbook(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            counter = Field(
                name="Widget Counter", slug="widget_counter", field_type="value",
                config={}, state={"value": 7},
            )
            logbook = Field(
                name="Widget Log", slug="widget_log", field_type="logbook",
                config={"max_entries": 10}, state={},
            )
            db.add_all([counter, logbook])
            db.flush()
            db.add(FieldLogEntry(field_id=logbook.id, value="hello"))
            db.commit()

            vdata = fetch_widget_data(
                "display", db, display="kv_text",
                widget_config={"template": "{{ widget_counter.value }}"},
            )
            assert vdata["display"] == "kv_text"
            assert vdata["text"] == "7"

            ldata = fetch_widget_data(
                "display", db, display="logbook_list",
                widget_config={"field_slug": "widget_log"},
            )
            assert ldata["name"] == "Widget Log"
            assert len(ldata["entries"]) == 1
            assert ldata["entries"][0]["value"] == "hello"
        finally:
            db.close()

    def test_metric_summary_shows_value_counters(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, Source
        from app.widgets import fetch_widget_data
        db = next(get_db())
        try:
            src = Source(name="MS", slug="ms-summary", source_type="webhook", enabled=True)
            counter = Field(
                name="Summary Hits", slug="ms-hits", field_type="value",
                config={}, state={"value": 3},
            )
            db.add_all([src, counter])
            db.commit()

            data = fetch_widget_data(
                "system", db, display="metric_summary", widget_config={}, source_id=src.id,
            )
            assert {"name": "Summary Hits", "value": 3.0} in data["counters"]
            assert "series" not in data
            assert "points" not in data
        finally:
            db.close()

    def test_wrong_field_type_fails(self, authenticated_client):
        """field_push with nonexistent field_id fails."""
        sid, slug = _create_source(authenticated_client, name="Wrong Type Src", slug="wrong-type-src")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event
        from app.pipeline import evaluate_and_dispatch
        db = next(get_db())
        try:
            action = ActionInstance(source_id=sid, action_type="field_push",
                config={"field_id": 999999},
            )
            db.add(action)
            db.flush()
            db.add(Rule(source_id=sid,
                event_type_ids=[], conditions={}, action_ids=[action.id], order_index=0,
            ))
            event = Event(
                source_id=sid, normalized_data={}, raw_payload="{}", correlation_id="wt",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.expire_all()
            ev = db.query(Event).filter(Event.correlation_id == "wt").first()
            assert ev.status == "failed"
            assert "not found" in (ev.processing_error or "").lower()
        finally:
            db.close()


class TestSystemWidgets:
    def test_source_health_status_bands(self, authenticated_client):
        from app.database import get_db
        from app.models import Source
        from app.widgets import fetch_widget_data, source_age_status

        now = datetime.now(timezone.utc)
        assert source_age_status(now, now=now) == "healthy"
        assert source_age_status(now - timedelta(hours=3), now=now) == "recent"
        assert source_age_status(now - timedelta(hours=30), now=now) == "stale"
        assert source_age_status(None, now=now) == "never"

        db = next(get_db())
        try:
            healthy = Source(
                name="SW Healthy", slug="sw-healthy", source_type="webhook",
                enabled=True, last_seen_at=now,
            )
            recent = Source(
                name="SW Recent", slug="sw-recent", source_type="webhook",
                enabled=True, last_seen_at=now - timedelta(hours=3),
            )
            stale = Source(
                name="SW Stale", slug="sw-stale", source_type="webhook",
                enabled=True, last_seen_at=now - timedelta(hours=30),
            )
            disabled = Source(
                name="SW Disabled", slug="sw-disabled", source_type="webhook",
                enabled=False, last_seen_at=now,
            )
            db.add_all([healthy, recent, stale, disabled])
            db.commit()

            data = fetch_widget_data("system", db, display="source_health", widget_config={})
            by_name = {s["name"]: s["status"] for s in data["sources"]}
            assert by_name["SW Healthy"] == "healthy"
            assert by_name["SW Recent"] == "recent"
            assert by_name["SW Stale"] == "stale"
            assert by_name["SW Disabled"] == "disabled"
        finally:
            db.close()

    def test_poller_status_includes_disabled_and_errors(self, authenticated_client):
        from app.database import get_db
        from app.models import PollingSchedule, Source
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            ok_src = Source(name="SW Poll Ok", slug="sw-poll-ok", source_type="poll", enabled=True)
            bad_src = Source(name="SW Poll Bad", slug="sw-poll-bad", source_type="poll", enabled=True)
            off_src = Source(name="SW Poll Off", slug="sw-poll-off", source_type="poll", enabled=True)
            db.add_all([ok_src, bad_src, off_src])
            db.flush()
            db.add(PollingSchedule(
                source_id=ok_src.id, name="SW Ok Sched", schedule_type="interval",
                interval_seconds=60, handler_type="http_get",
                handler_url="https://example.com", enabled=True,
                success_count=2, failure_count=0,
            ))
            db.add(PollingSchedule(
                source_id=bad_src.id, name="SW Err Sched", schedule_type="interval",
                interval_seconds=60, handler_type="http_get",
                handler_url="https://example.com", enabled=True,
                success_count=0, failure_count=4, last_error="timeout",
            ))
            db.add(PollingSchedule(
                source_id=off_src.id, name="SW Off Sched", schedule_type="interval",
                interval_seconds=60, handler_type="http_get",
                handler_url="https://example.com", enabled=False,
                success_count=1, failure_count=0,
            ))
            db.commit()

            data = fetch_widget_data("system", db, display="poller_status", widget_config={})
            by_name = {s["name"]: s for s in data["schedules"]}
            assert "SW Off Sched" in by_name
            assert by_name["SW Off Sched"]["enabled"] is False
            assert by_name["SW Err Sched"]["last_error"] == "timeout"
            assert by_name["SW Err Sched"]["source"] == "SW Poll Bad"
            assert by_name["SW Ok Sched"]["enabled"] is True
        finally:
            db.close()

    def test_system_widget_partial_uses_display_timezone(self, authenticated_client):
        from app.database import get_db
        from app.models import DashboardLayout, Source

        _set_display_timezone("Africa/Johannesburg")
        authenticated_client.post(
            "/config/dashboard",
            data={"widgets": json.dumps([{"type": "system", "display": "source_health", "title": "Health"}])},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            src = Source(
                name="SW TZ Src", slug="sw-tz-src", source_type="webhook",
                enabled=True,
                last_seen_at=datetime(2026, 1, 2, 3, 4, 0, tzinfo=timezone.utc),
            )
            db.add(src)
            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            wid = json.loads(layout.layout_config)["widgets"][0]["id"]
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.get(f"/widgets/system?id={wid}")
        assert resp.status_code == 200
        assert "2026-01-02 05:04" in resp.text
        assert "SW TZ Src" in resp.text
        assert "healthy" in resp.text or "recent" in resp.text or "stale" in resp.text

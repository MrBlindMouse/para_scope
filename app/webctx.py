import logging
import os

import hashlib
import hmac as hmac_mod
import json
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import quote, parse_qs
from fastapi import BackgroundTasks, FastAPI, Request, Depends, Form, status
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from pathlib import Path

from app.database import engine, Base, get_db, ensure_schema
from app.models import (
    User, Source, EventTypeRecord, PollingSchedule, ScheduleType,
    ActionInstance, Rule, Secret, DashboardLayout, Event, AuditLog, MetricPoint,
    PushSubscription, Field, FieldLogEntry,
)
from fastapi.templating import Jinja2Templates
from app.security import (
    verify_password, hash_password, encrypt_secret, decrypt_secret,
    create_session_token, verify_session_token, generate_csrf_token,
    SESSION_MAX_AGE_SECONDS,
)
from app.pipeline import evaluate_and_dispatch
from app.dashboard_layout import parse_layout_config
from app.scheduler import start_scheduler, stop_scheduler, add_or_update_job, remove_job, job_count

# ponytail: simple in-memory rate limiter for login (IP-based, 10 req/minute)
_LOGIN_RATE_LIMIT = {}
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 60
# ponytail: in-memory webhook rate limit (per IP+slug, 60 req/minute); use reverse proxy for multi-worker
_WEBHOOK_RATE_LIMIT: dict[str, list[float]] = {}
_WEBHOOK_MAX_ATTEMPTS = 60
_WEBHOOK_RATE_WINDOW_SECONDS = 60
_SECURE_COOKIES = os.environ.get("PARA_SCOPE_SECURE_COOKIES", "").lower() in ("1", "true", "yes")

_LOG_LEVEL = getattr(
    logging, os.environ.get("PARA_SCOPE_LOG_LEVEL", "INFO").upper(), logging.INFO
)
logging.getLogger("para_scope").setLevel(_LOG_LEVEL)
logger = logging.getLogger("para_scope")
http_logger = logging.getLogger("para_scope.http")


def _check_login_rate_limit(ip: str) -> bool:
    """Return True if login is allowed, False if rate-limited."""
    now = time.time()
    window_start = now - _LOGIN_WINDOW_SECONDS
    # Clean old entries
    _LOGIN_RATE_LIMIT[ip] = [t for t in _LOGIN_RATE_LIMIT.get(ip, []) if t > window_start]
    attempts = _LOGIN_RATE_LIMIT.get(ip, [])
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        return False
    attempts.append(now)
    _LOGIN_RATE_LIMIT[ip] = attempts
    return True


def _check_webhook_rate_limit(key: str) -> bool:
    """Return True if webhook is allowed, False if rate-limited."""
    now = time.time()
    window_start = now - _WEBHOOK_RATE_WINDOW_SECONDS
    _WEBHOOK_RATE_LIMIT[key] = [t for t in _WEBHOOK_RATE_LIMIT.get(key, []) if t > window_start]
    attempts = _WEBHOOK_RATE_LIMIT.get(key, [])
    if len(attempts) >= _WEBHOOK_MAX_ATTEMPTS:
        return False
    attempts.append(now)
    _WEBHOOK_RATE_LIMIT[key] = attempts
    return True


def _audit_log(db, request, action, user_id=None, resource_type="", resource_id=None, details=None):
    """Write an audit log entry."""
    from app.models import AuditLog
    ip = ""
    if request and request.client:
        ip = request.client.host
    entry = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
        ip_address=ip,
    )
    db.add(entry)
    try:
        db.commit()
    except Exception:
        logger.exception("Failed to write audit log action=%s", action)


# ponytail: replay protection uses in-memory LRU cache — fine for single-process deployment.
_WEBHOOK_REPLAY_CACHE: OrderedDict[str, float] = OrderedDict()
_WEBHOOK_REPLAY_MAX = 5000  # max entries before oldest are evicted
_WEBHOOK_REPLAY_TTL_SECONDS = 300  # 5 minutes
_WEBHOOK_MAX_BODY = 1024 * 256  # 256KB


class AuthMiddleware(BaseHTTPMiddleware):
    PUBLIC_PATHS = {"/login", "/logout", "/setup", "/health", "/static", "/webhook", "/sw.js"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.PUBLIC_PATHS or path.startswith(("/static", "/webhook")):
            return await call_next(request)

        db = next(get_db())
        try:
            if _needs_setup(db):
                return RedirectResponse(url="/setup", status_code=status.HTTP_303_SEE_OTHER)
            token = request.cookies.get("session_username")
            username = verify_session_token(token) if token else None
            user = db.query(User).filter(User.username == username).first() if username else None
            if not user:
                is_htmx = request.headers.get("HX-Request", "").lower() == "true"
                wants_json = "application/json" in (request.headers.get("accept") or "")
                if is_htmx:
                    return JSONResponse(
                        {"error": "Please sign in"},
                        status_code=401,
                        headers={"HX-Redirect": "/login"},
                    )
                if wants_json or path.startswith("/api/") or path.startswith("/metrics/api"):
                    return JSONResponse({"error": "Please sign in"}, status_code=401)
                return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        finally:
            db.close()

        return await call_next(request)


def _needs_setup(db: Session) -> bool:
    """True when no users exist yet (first-run setup required)."""
    return db.query(User).count() == 0


def _get_user(request: Request, db: Session) -> User | None:
    """Get current user from signed session cookie."""
    token = request.cookies.get("session_username")
    username = verify_session_token(token) if token else None
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


_SOURCE_TYPES = frozenset({"webhook", "poll"})


def _slugify_name(name: str) -> str:
    """Derive an identifier-safe slug (underscores, no hyphens)."""
    slug = name.lower().strip().replace(" ", "_").replace("-", "_")
    slug = "".join(c for c in slug if c.isalnum() or c == "_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "source"


def _unique_slug_from_name(db: Session, name: str, exclude_id: int | None = None) -> str:
    """Slugify name and append _2, _3, ... if the slug is already taken."""
    base = _slugify_name(name)
    slug = base
    n = 2
    while True:
        q = db.query(Source).filter(Source.slug == slug)
        if exclude_id is not None:
            q = q.filter(Source.id != exclude_id)
        if not q.first():
            return slug
        slug = f"{base}_{n}"
        n += 1


def _unique_field_slug(db: Session, name: str, exclude_id: int | None = None) -> str:
    base = _slugify_name(name) or "field"
    slug = base
    n = 2
    while True:
        q = db.query(Field).filter(Field.slug == slug)
        if exclude_id is not None:
            q = q.filter(Field.id != exclude_id)
        if not q.first():
            return slug
        slug = f"{base}_{n}"
        n += 1


def _fields_section_template(request: Request, db: Session):
    fields = db.query(Field).order_by(Field.name).all()
    return templates.TemplateResponse(
        request, "config/pipeline/_fields_section.html", {"fields": fields}
    )


def _field_in_use(db: Session, field_id: int) -> str | None:
    """Return a reason string if field is referenced by actions or widgets."""
    from app.widgets import widget_referenced_field_ids

    for action in db.query(ActionInstance).all():
        cfg = action.config or {}
        if cfg.get("field_id") == field_id:
            return "used by an action"
    for layout in db.query(DashboardLayout).all():
        for w in parse_layout_config(layout.layout_config)["widgets"]:
            if field_id in widget_referenced_field_ids(db, w.get("config") or {}):
                return "used by a dashboard widget"
    return None


def _parse_action_config(form, action_type: str) -> tuple[dict | None, str | None]:
    """Build typed action config from form fields. Returns (config, error)."""
    if action_type == "field_push":
        field_id_raw = (form.get("field_id") or "").strip()
        if not field_id_raw:
            return None, "Field is required"
        try:
            field_id = int(field_id_raw)
        except ValueError:
            return None, "Choose a valid field"
        field_type = (form.get("field_type") or "").strip()
        out: dict = {"field_id": field_id}
        if field_type == "logbook":
            mode = (form.get("logbook_mode") or "event").strip()
            if mode == "key":
                key = (form.get("value_key") or "").strip()
                if not key:
                    return None, "Value from event is required"
                out["value_key"] = key
            elif mode == "literal":
                out["value"] = form.get("value") or ""
            # mode == event → whole normalized_data (no value/value_key)
        elif field_type == "counter":
            op = (form.get("counter_op") or "increment").strip()
            if op not in ("increment", "decrement", "reset"):
                return None, "Choose Increment, Decrement, or Reset"
            out["op"] = op
            if op in ("increment", "decrement"):
                delta = (form.get("delta") or "").strip()
                if delta == "":
                    out["delta"] = 1
                else:
                    try:
                        num = float(delta)
                        out["delta"] = int(num) if num.is_integer() else num
                    except ValueError:
                        out["delta"] = delta  # event key
        elif field_type == "value":
            mode = (form.get("value_mode") or "literal").strip()
            if mode == "key":
                key = (form.get("value_key") or "").strip()
                if not key:
                    return None, "Value from event is required"
                out["value_key"] = key
            else:
                out["value"] = form.get("value") if form.get("value") is not None else ""
        elif field_type == "toggle":
            mode = (form.get("toggle_mode") or "literal").strip()
            if mode == "key":
                key = (form.get("value_key") or "").strip()
                if not key:
                    return None, "Value from event is required"
                out["value_key"] = key
            else:
                raw = (form.get("toggle_value") or "false").strip().lower()
                out["value"] = raw in ("1", "true", "yes", "on")
        else:
            # field_type unknown at parse time — still store field_id; handler checks type
            pass
        return out, None

    if action_type == "web_push":
        return {
            "title": (form.get("title") or "Para-Scope").strip() or "Para-Scope",
            "body": form.get("body") or "",
            "url": (form.get("url") or "/").strip() or "/",
        }, None

    if action_type == "http_forward":
        url = (form.get("url") or "").strip()
        if not url:
            return None, "URL is required"
        method = (form.get("method") or "POST").strip().upper()
        if method not in ("GET", "POST", "PUT", "PATCH"):
            return None, "Invalid HTTP method"
        auth_mode = (form.get("auth_mode") or "none").strip()
        if auth_mode not in ("bearer", "key_secret", "none"):
            auth_mode = "none"
        out = {
            "url": url,
            "method": method,
            "auth_mode": auth_mode,
        }
        timeout_raw = (form.get("timeout_seconds") or "").strip()
        if timeout_raw:
            try:
                out["timeout_seconds"] = float(timeout_raw)
            except ValueError:
                return None, "Timeout must be a number"
        if auth_mode == "key_secret":
            key_h = (form.get("api_key_header") or "").strip()
            sec_h = (form.get("api_secret_header") or "").strip()
            if key_h:
                out["api_key_header"] = key_h
            if sec_h:
                out["api_secret_header"] = sec_h
        body_mode = (form.get("body_mode") or "auto").strip()
        if body_mode == "custom":
            raw = (form.get("custom_body") or "").strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    out["body_mode"] = "custom"
                    out["custom_body"] = parsed
                except (json.JSONDecodeError, TypeError):
                    return None, "Custom body is not valid JSON"
        return out, None

    return None, "That action type isn’t supported"


def _upsert_action_secret(db, action, *, value: str, which: str = "primary"):
    """Create or update action secret. which is 'primary' or 'secondary'."""
    encrypted_value = encrypt_secret(value)
    attr = "secret_id" if which == "primary" else "secret_id_2"
    sid = getattr(action, attr)
    if sid:
        secret = db.query(Secret).filter(Secret.id == sid).first()
        if secret:
            secret.encrypted_value = encrypted_value
            return
    secret = Secret(
        scoped_to_type="action",
        scoped_to_id=action.id, encrypted_value=encrypted_value,
    )
    db.add(secret)
    db.flush()
    setattr(action, attr, secret.id)


def _parse_field_form(form, *, existing: Field | None = None):
    """Parse field create/update form. Returns (kwargs, error)."""
    from app.fields import FIELD_TYPES, default_field_config

    name = (form.get("name") or "").strip()
    if not name:
        return None, "Name is required"
    field_type = (form.get("field_type") or (existing.field_type if existing else "logbook")).strip()
    if existing:
        field_type = existing.field_type
    if field_type not in FIELD_TYPES:
        return None, "Choose a field type"

    config = dict(existing.config or {}) if existing else default_field_config(field_type)
    if field_type == "logbook":
        try:
            max_entries = int(form.get("max_entries") or config.get("max_entries") or 100)
        except (TypeError, ValueError):
            return None, "Max entries must be a number"
        max_entries = max(1, min(max_entries, 100_000))
        config = {"max_entries": max_entries}
    else:
        config = {}

    return {
        "name": name,
        "field_type": field_type,
        "config": config,
    }, None


def _parse_schedule_form(form, *, required: bool = True):
    """Parse polling-schedule fields from a form (no user-facing name).

    Returns (kwargs_dict | None, error_message | None).
    When required is False and no schedule fields are filled, returns (None, None).
    Caller must set kwargs['name'] from the source name.
    """
    schedule_type_raw = form.get("schedule_type", "interval")
    cron_expression = (form.get("cron_expression") or "").strip()
    interval_seconds = form.get("interval_seconds")
    handler_type = form.get("handler_type", "http_get")
    handler_url = (form.get("handler_url") or "").strip()
    handler_params_str = (form.get("handler_params") or "{}").strip()
    timeout_seconds = int(form.get("timeout_seconds") or 30)
    retry_count = int(form.get("retry_count") or 0)

    if not required:
        has_any = bool(
            handler_url
            or (interval_seconds or "").strip()
            or cron_expression
        )
        if not has_any:
            return None, None

    try:
        schedule_type = ScheduleType(schedule_type_raw)
    except ValueError:
        return None, "Invalid schedule type"

    if schedule_type == ScheduleType.INTERVAL and not interval_seconds:
        return None, "Enter an interval in seconds"
    if schedule_type == ScheduleType.CRON and not cron_expression:
        return None, "Enter a cron schedule"

    try:
        handler_params = json.loads(handler_params_str) if handler_params_str != "{}" else {}
    except json.JSONDecodeError:
        return None, "Extra settings must be valid JSON"

    return {
        "schedule_type": schedule_type,
        "cron_expression": cron_expression,
        "interval_seconds": int(interval_seconds) if interval_seconds else None,
        "handler_type": handler_type,
        "handler_url": handler_url,
        "handler_params": handler_params,
        "timeout_seconds": timeout_seconds,
        "retry_count": retry_count,
        "enabled": True,
    }, None


def _parse_int_list(form, key: str) -> list[int]:
    """Parse multi-select getlist or legacy JSON string into a list of ints."""
    values = form.getlist(key)
    if len(values) == 1 and values[0].strip().startswith("["):
        try:
            parsed = json.loads(values[0])
            return [int(x) for x in parsed]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    result = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        try:
            result.append(int(v))
        except ValueError:
            continue
    return result


def _action_map(actions: list[ActionInstance]) -> dict[int, ActionInstance]:
    return {a.id: a for a in actions}


def _event_type_map(event_types: list[EventTypeRecord]) -> dict[int, EventTypeRecord]:
    return {et.id: et for et in event_types}


def _bound_action_ids(rules: list[Rule]) -> set[int]:
    bound: set[int] = set()
    for rule in rules:
        for aid in rule.action_ids or []:
            bound.add(aid)
    return bound


def _scrub_action_from_rules(db: Session, source_id: int, action_id: int) -> None:
    """Remove action_id from any rule.action_ids on this source."""
    rules = db.query(Rule).filter(Rule.source_id == source_id).all()
    for rule in rules:
        ids = list(rule.action_ids or [])
        if action_id in ids:
            rule.action_ids = [i for i in ids if i != action_id]


def _scrub_event_type_from_rules(db: Session, et_id: int) -> None:
    """Remove et_id from any rule.event_type_ids (source-scoped or global)."""
    rules = db.query(Rule).all()
    for rule in rules:
        ids = list(rule.event_type_ids or [])
        if et_id in ids:
            rule.event_type_ids = [i for i in ids if i != et_id]


def _source_chain_template(request: Request, db: Session, source: Source, **extra):
    rules = (
        db.query(Rule)
        .filter(Rule.source_id == source.id)
        .order_by(Rule.order_index, Rule.id)
        .all()
    )
    actions = (
        db.query(ActionInstance)
        .filter(ActionInstance.source_id == source.id)
        .order_by(ActionInstance.id)
        .all()
    )
    event_types = (
        db.query(EventTypeRecord)
        .filter(EventTypeRecord.source_id == source.id)
        .order_by(EventTypeRecord.name)
        .all()
    )
    actions_by_id = _action_map(actions)
    fields = db.query(Field).order_by(Field.name).all()
    fields_by_id = {f.id: f for f in fields}
    unbound = [a for a in actions if a.id not in _bound_action_ids(rules)]
    ctx = {
        "source": source,
        "rules": rules,
        "actions": actions,
        "actions_by_id": actions_by_id,
        "unbound_actions": unbound,
        "event_types": event_types,
        "event_types_by_id": _event_type_map(event_types),
        "fields_by_id": fields_by_id,
        "active": "pipeline",
    }
    ctx.update(extra)
    return templates.TemplateResponse(
        request, "config/pipeline/_source_chain.html", ctx
    )


def _pipeline_redirect(success: str | None = None, error: str | None = None):
    return RedirectResponse(
        url=flash_url("/config/pipeline", success=success, error=error),
        status_code=303,
    )


class CsrfProtectMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection for authenticated POST requests.

    Validates `_csrf_token` form field or `X-CSRF-Token` header against the
    `csrf_token` cookie. Replays the request body so multipart upload works.
    """
    _EXEMPT_PATHS = {"/login", "/setup", "/health", "/static", "/webhook", "/sw.js"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        exempt = path in self._EXEMPT_PATHS or path.startswith(("/static", "/webhook"))

        # Mint CSRF before render so templates and Set-Cookie use the same token
        csrf_cookie = request.cookies.get("csrf_token", "")
        minted_csrf = None
        if not csrf_cookie:
            minted_csrf = generate_csrf_token()
            request.state.csrf_token = minted_csrf
        else:
            request.state.csrf_token = csrf_cookie

        if not exempt and request.method in ("POST", "PUT", "DELETE"):
            form_token = (request.headers.get("X-CSRF-Token") or "").strip()
            body = await request.body()

            if not form_token and body:
                async def _receive():
                    return {"type": "http.request", "body": body, "more_body": False}

                form_req = StarletteRequest(request.scope, _receive)
                form = await form_req.form()
                form_token = str(form.get("_csrf_token") or "")

            if not _verify_csrf(request.state.csrf_token, form_token):
                http_logger.warning(
                    "CSRF token mismatch method=%s path=%s",
                    request.method, path,
                )
                return JSONResponse({"error": "Session expired — refresh and try again"}, status_code=403)

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request = StarletteRequest(request.scope, receive)
            # Preserve minted token across request object replacement
            request.state.csrf_token = csrf_cookie or minted_csrf

        response = await call_next(request)
        if minted_csrf:
            response.set_cookie(
                key="csrf_token", value=minted_csrf, httponly=True,
                samesite="lax", secure=_SECURE_COOKIES,
                max_age=SESSION_MAX_AGE_SECONDS, path="/",
            )
        return response


def _verify_csrf(cookie_token: str, form_token: str) -> bool:
    if not cookie_token or not form_token:
        return False
    return hmac_mod.compare_digest(cookie_token.encode(), form_token.encode())


# Last added = outermost: CSRF runs before Auth.
templates = Jinja2Templates(directory="app/templates")
from app.labels import (  # noqa: E402
    action_label,
    action_type_label,
    field_type_label,
    http_method_label,
    operator_label,
    rule_label,
)

templates.env.filters["action_label"] = action_label
templates.env.filters["action_type_label"] = action_type_label
templates.env.filters["field_type_label"] = field_type_label
templates.env.filters["http_method_label"] = http_method_label
templates.env.filters["operator_label"] = operator_label
templates.env.filters["rule_label"] = rule_label



# ── Helpers ──────────────────────────────────────────────────────────────────

def flash_url(path: str, success: str | None = None, error: str | None = None) -> str:
    """Build a redirect URL with a flash message as the query value."""
    if success is not None:
        return f"{path}?success={quote(success)}"
    if error is not None:
        return f"{path}?error={quote(error)}"
    return path


def get_message_params(request: Request):
    """Extract flash message params from query string."""
    qs = parse_qs(request.url.query)
    return qs.get("success", [None])[0], qs.get("error", [None])[0]


def _get_csrf_token(req: Request) -> str:
    return getattr(req.state, "csrf_token", None) or req.cookies.get("csrf_token", "")


# Monkey-patch Jinja2Templates to inject csrf_token into every render
_original_template_response = templates.TemplateResponse


def _template_response_with_csrf(request_obj: Any, template_name: str, context: dict | None = None, status_code: int = 200, headers: dict | None = None, media_type: str | None = None, response: Any | None = None):
    from starlette.requests import Request as _Request
    if isinstance(request_obj, _Request):
        csrf = _get_csrf_token(request_obj)
        context = context or {}
        context.setdefault("_csrf_token", csrf)
        if "theme" not in context or "font" not in context or "font_size" not in context:
            from app.database import SessionLocal
            from app.themes import appearance_context
            db = SessionLocal()
            try:
                context.update({k: v for k, v in appearance_context(db).items() if k not in context})
            finally:
                db.close()
    return _original_template_response(request_obj, template_name, context, status_code, headers, media_type, response)


templates.TemplateResponse = _template_response_with_csrf


# ── Auth ─────────────────────────────────────────────────────────────────────

def _set_session_cookies(response, request: Request, username: str):
    """Attach signed session cookie (and CSRF cookie if missing)."""
    token = create_session_token(username)
    response.set_cookie(
        key="session_username", value=token, httponly=True,
        samesite="lax", secure=_SECURE_COOKIES,
        max_age=SESSION_MAX_AGE_SECONDS, path="/",
    )
    if not request.cookies.get("csrf_token"):
        response.set_cookie(
            key="csrf_token", value=generate_csrf_token(), httponly=True,
            samesite="lax", secure=_SECURE_COOKIES,
            max_age=SESSION_MAX_AGE_SECONDS, path="/",
        )
    return response




def _parse_rule_form(form, *, for_update: bool = False):
    """Parse rule fields. Returns (kwargs, error) or (None, error)."""
    conditions_str = (form.get("conditions") or "{}").strip()
    order_index = int(form.get("order_index") or 0)
    event_type_ids = _parse_int_list(form, "event_type_ids")
    # Legacy clients may still send action_ids; create form does not.
    action_ids = _parse_int_list(form, "action_ids") if "action_ids" in form else None

    try:
        conditions = json.loads(conditions_str) if conditions_str else {}
    except json.JSONDecodeError:
        return None, "Conditions must be valid JSON"

    data = {
        "conditions": conditions,
        "order_index": order_index,
        "event_type_ids": event_type_ids,
    }
    if action_ids is not None:
        data["action_ids"] = action_ids
    if for_update:
        data["enabled"] = form.get("enabled") in ("1", "on", "true", "True")
    return data, None




def _cleanup_replay_cache(cutoff: float):
    """Remove entries older than cutoff from the LRU replay cache."""
    while _WEBHOOK_REPLAY_CACHE and next(iter(_WEBHOOK_REPLAY_CACHE.values())) < cutoff:
        _WEBHOOK_REPLAY_CACHE.popitem(last=False)
    # ponytail: bounded size — evict oldest when over limit
    while len(_WEBHOOK_REPLAY_CACHE) > _WEBHOOK_REPLAY_MAX:
        _WEBHOOK_REPLAY_CACHE.popitem(last=False)




def _process_webhook_event(event_id: int) -> None:
    """Background: run the pipeline for an accepted webhook event.

    In-flight work is lost if the process crashes (no durable queue).
    """
    from app.database import SessionLocal

    webhook_logger = logging.getLogger("para_scope.webhook")
    db = SessionLocal()
    try:
        event = db.query(Event).filter(Event.id == event_id).first()
        if not event:
            webhook_logger.warning("Webhook background: event %s not found", event_id)
            return
        try:
            evaluate_and_dispatch(db, event)
            if event.status != "failed":
                event.status = "processed"
        except Exception as e:
            event.status = "failed"
            event.processing_error = str(e)
            webhook_logger.exception("Webhook pipeline failed for event %s", event_id)
        db.commit()
    finally:
        db.close()



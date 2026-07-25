"""Action type registry — built-in handlers for the event pipeline."""
from __future__ import annotations

import json
import logging
from typing import Callable

import httpx

from app.fields import (
    coerce_logbook_value,
    get_by_path,
    resolve_bool,
    resolve_numeric,
    resolve_string,
)
from app.models import (
    ActionInstance,
    Event,
    Field,
    FieldLogEntry,
    Secret,
    PushSubscription,
)
from app.security import decrypt_secret
from app.webpush_util import render_template, vapid_config

logger = logging.getLogger("para_scope.pipeline")

_ACTIONS: dict[str, Callable] = {}


def register_action(action_type: str, handler: Callable):
    """Register an action handler: handler(db, event, action) -> None."""
    _ACTIONS[action_type] = handler


def get_action_types() -> list[str]:
    """Return registered action type names (sorted)."""
    return sorted(_ACTIONS.keys())


def run_registered_action(db, event: Event, action: ActionInstance) -> None:
    """Dispatch to a registered handler or raise for unknown types."""
    handler = _ACTIONS.get(action.action_type)
    if handler is None:
        raise ValueError(f"Unknown action type: {action.action_type}")
    handler(db, event, action)


def _require_field(db, field_id, expected_types: tuple[str, ...] | None = None) -> Field:
    if field_id is None:
        raise ValueError("Action is missing a field")
    try:
        fid = int(field_id)
    except (TypeError, ValueError) as e:
        raise ValueError("Invalid field") from e
    field = db.query(Field).filter(Field.id == fid).first()
    if not field:
        raise ValueError("Field not found")
    if expected_types is not None and field.field_type not in expected_types:
        raise ValueError(f"Field “{field.name}” is the wrong type")
    return field


def _action_field_push(db, event: Event, action: ActionInstance) -> None:
    config = action.config or {}
    field = _require_field(db, config.get("field_id"))
    nd = event.normalized_data or {}

    if field.field_type == "logbook":
        cfg = field.config or {}
        max_entries = int(cfg.get("max_entries") or 100)
        max_entries = max(1, min(max_entries, 100_000))

        if "value" in config:
            raw = config["value"]
        elif config.get("value_key"):
            raw = get_by_path(nd, config["value_key"])
        else:
            raw = nd

        value = coerce_logbook_value(raw)
        entry = FieldLogEntry(
            field_id=field.id,
            value=value,
            source_id=event.source_id,
            event_id=event.id,
        )
        db.add(entry)
        db.flush()

        ids = [
            r[0]
            for r in db.query(FieldLogEntry.id)
            .filter(FieldLogEntry.field_id == field.id)
            .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
            .all()
        ]
        if len(ids) > max_entries:
            drop = ids[max_entries:]
            db.query(FieldLogEntry).filter(FieldLogEntry.id.in_(drop)).delete(
                synchronize_session=False
            )
        db.commit()
        logger.info(
            "field_push logbook field=%s event_id=%s", field.name, event.id,
        )
        return

    if field.field_type == "counter":
        op = (config.get("op") or "increment").strip()
        if op not in ("increment", "decrement", "reset"):
            raise ValueError("Unknown counter operation")
        # Re-lock row so concurrent webhook/poll increments do not lose updates
        field = db.query(Field).filter(Field.id == field.id).with_for_update().one()
        state = dict(field.state or {})
        if op == "reset":
            new_value = 0.0
        else:
            delta = config.get("delta", 1)
            resolved = resolve_numeric(delta, nd)
            current = float(state.get("value") or 0)
            new_value = current - resolved if op == "decrement" else current + resolved
        state["value"] = new_value
        field.state = state
        db.commit()
        return

    if field.field_type == "value":
        state = dict(field.state or {})
        state["value"] = resolve_string(config, nd)
        field.state = state
        db.commit()
        return

    if field.field_type == "toggle":
        state = dict(field.state or {})
        state["value"] = resolve_bool(config, nd)
        field.state = state
        db.commit()
        return

    raise ValueError("This field type can’t be updated by that action")


def _secret_value(db, secret_id) -> str:
    """Resolve and decrypt a configured secret. Raises if missing/unreadable."""
    if not secret_id:
        raise ValueError("Secret is missing")
    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        raise ValueError("Secret not found")
    return decrypt_secret(secret.encrypted_value)


def _forward_headers(db, action: ActionInstance, config: dict) -> dict:
    headers = dict(config.get("headers") or {})
    auth_mode = (config.get("auth_mode") or "none").strip()
    if auth_mode == "key_secret":
        if not action.secret_id or not action.secret_id_2:
            raise ValueError("This forward needs both API key and secret")
        key = _secret_value(db, action.secret_id)
        secret = _secret_value(db, action.secret_id_2)
        headers[config.get("api_key_header") or "X-Api-Key"] = key
        headers[config.get("api_secret_header") or "X-Api-Secret"] = secret
    elif action.secret_id:
        token = _secret_value(db, action.secret_id)
        header_name = config.get("auth_header", "Authorization")
        prefix = config.get("auth_prefix", "Bearer ")
        headers[header_name] = f"{prefix}{token}"
    return headers


def _action_http_forward(db, event: Event, action: ActionInstance) -> None:
    config = action.config or {}
    url = (config.get("url") or "").strip()
    if not url:
        raise ValueError("Forward URL is missing")
    method = (config.get("method") or "POST").upper()
    timeout = float(config.get("timeout_seconds") or 30)
    headers = _forward_headers(db, action, config)

    body_mode = config.get("body_mode", "auto")
    nd = event.normalized_data or {}
    if body_mode == "custom":
        raw_body = config.get("custom_body")
        if isinstance(raw_body, dict):
            # Resolve {{field}} placeholders in string values
            resolved = _resolve_body_templates(raw_body, nd)
            default = {
                "event_id": event.id,
                "source_id": event.source_id,
                "correlation_id": event.correlation_id,
                "data": nd,
            }
            body = {**default, **resolved}
        else:
            body = raw_body if raw_body is not None else {}
    else:
        # Default: send the full event envelope
        body = {
            "event_id": event.id,
            "source_id": event.source_id,
            "correlation_id": event.correlation_id,
            "data": nd,
        }

    with httpx.Client(timeout=timeout) as client:
        kwargs = {"method": method, "url": url, "headers": headers}
        if method in ("POST", "PUT", "PATCH"):
            kwargs["json"] = body
        elif method == "GET":
            kwargs["params"] = config.get("query") or None
        response = client.request(**kwargs)
        response.raise_for_status()


def _resolve_body_templates(obj, data: dict):
    """Recursively resolve {{field}} placeholders in string values of a parsed JSON body."""
    if isinstance(obj, str):
        return render_template(obj, data)
    if isinstance(obj, dict):
        return {k: _resolve_body_templates(v, data) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_body_templates(item, data) for item in obj]
    return obj


def _action_web_push(db, event: Event, action: ActionInstance) -> None:
    """Send a Web Push notification to all subscribed users."""
    vapid = vapid_config()
    if not vapid:
        raise ValueError("Browser notifications aren’t configured")

    config = action.config or {}
    data = event.normalized_data or {}
    title = render_template(config.get("title") or "Para-Scope", data)
    body = render_template(config.get("body") or "", data)
    url = render_template(config.get("url") or "/", data)
    payload = json.dumps({"title": title, "body": body, "url": url})

    subs = db.query(PushSubscription).all()
    if not subs:
        logger.info("web_push: no subscriptions, skipping")
        return

    try:
        from pywebpush import webpush, WebPushException
    except ImportError as e:
        raise ValueError("Push notifications aren’t available on this server") from e

    claims = {"sub": vapid["subject"]}
    errors = []
    live_attempts = 0
    for sub in list(subs):
        live_attempts += 1
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=vapid["private_key"],
                vapid_claims=claims,
            )
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                db.delete(sub)
                live_attempts -= 1
            else:
                errors.append(str(e))
                logger.warning("web_push failed endpoint=%s: %s", sub.endpoint[:80], e)
    db.commit()
    if live_attempts > 0 and errors and len(errors) == live_attempts:
        raise ValueError("Couldn’t send any push notifications")


register_action("field_push", _action_field_push)
register_action("http_forward", _action_http_forward)
register_action("web_push", _action_web_push)

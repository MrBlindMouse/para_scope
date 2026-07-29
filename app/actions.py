"""Action type registry — built-in handlers for the event pipeline."""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import logging
import os
import subprocess
import time
from typing import Any, Callable
from urllib.parse import urljoin

import httpx

from app.fields import (
    coerce_data_value,
    coerce_logbook_value,
    resolve_numeric,
    with_current_field,
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
from app.webpush_util import vapid_config
from app.widget_transforms import render_data_template, resolve_value_from_event

logger = logging.getLogger("para_scope.pipeline")

_ACTIONS: dict[str, Callable] = {}

_NOTIFY_SERVICES = ("ntfy", "gotify", "discord")
_LOCAL_TIMEOUT_DEFAULT = 30.0
_LOCAL_TIMEOUT_MAX = 120.0


def register_action(action_type: str, handler: Callable):
    """Register an action handler: handler(db, event, action) -> None."""
    _ACTIONS[action_type] = handler


def get_action_types() -> list[str]:
    """Return registered action types in canonical UI order (not A–Z)."""
    from app.labels import ACTION_TYPE_LABELS

    ordered = [slug for slug in ACTION_TYPE_LABELS if slug in _ACTIONS]
    extras = [slug for slug in _ACTIONS if slug not in ACTION_TYPE_LABELS]
    return ordered + extras


def local_actions_enabled() -> bool:
    """True when PARA_SCOPE_ALLOW_LOCAL_ACTIONS is set to a truthy value."""
    return (os.environ.get("PARA_SCOPE_ALLOW_LOCAL_ACTIONS") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


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

        latest = (
            db.query(FieldLogEntry)
            .filter(FieldLogEntry.field_id == field.id)
            .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
            .first()
        )
        if "value" in config or config.get("value_key"):
            ctx = with_current_field(nd, latest.value if latest else None)
            if "value" in config:
                raw = render_data_template(str(config["value"] or ""), ctx)
            else:
                raw = resolve_value_from_event(str(config["value_key"]), ctx)
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

    if field.field_type == "value":
        op = (config.get("op") or "increment").strip()
        if op not in ("increment", "decrement", "reset", "set"):
            raise ValueError("Unknown value operation")
        # Re-lock row so concurrent webhook/poll increments do not lose updates
        field = db.query(Field).filter(Field.id == field.id).with_for_update().one()
        state = dict(field.state or {})
        current = float(state.get("value") or 0)
        ctx = with_current_field(nd, current)
        if op == "reset":
            new_value = 0.0
        elif op == "set":
            new_value = resolve_numeric(config.get("delta"), ctx)
        else:
            delta = config.get("delta", 1)
            resolved = resolve_numeric(delta, ctx)
            new_value = current - resolved if op == "decrement" else current + resolved
        if op in ("set", "reset") and float(new_value) == float(current):
            logger.debug(
                "field_push value skip unchanged field=%s event_id=%s",
                field.name, event.id,
            )
            db.commit()  # release FOR UPDATE
            return
        state["value"] = new_value
        field.state = state
        db.commit()
        return

    if field.field_type == "text":
        if "value" not in config:
            raise ValueError("Text action needs a template")
        state = dict(field.state or {})
        current = state.get("value", "")
        ctx = with_current_field(nd, current)
        new_value = render_data_template(str(config["value"] or ""), ctx)
        if str(new_value) == str(current if current is not None else ""):
            logger.debug(
                "field_push text skip unchanged field=%s event_id=%s",
                field.name, event.id,
            )
            return
        state["value"] = new_value
        field.state = state
        db.commit()
        return

    if field.field_type == "toggle":
        field = db.query(Field).filter(Field.id == field.id).with_for_update().one()
        state = dict(field.state or {})
        current = bool(state.get("value", False))
        if (config.get("op") or "").strip() == "switch":
            state["value"] = not current
        elif "value" in config:
            raw = config["value"]
            if isinstance(raw, bool):
                if raw == current:
                    logger.debug(
                        "field_push toggle skip unchanged field=%s event_id=%s",
                        field.name, event.id,
                    )
                    db.commit()  # release FOR UPDATE
                    return
                state["value"] = raw
            else:
                raise ValueError("Toggle Fixed needs on or off")
        else:
            raise ValueError("Toggle action needs Fixed or Switch")
        field.state = state
        db.commit()
        return

    if field.field_type == "data":
        field = db.query(Field).filter(Field.id == field.id).with_for_update().one()
        state = dict(field.state or {})
        ctx = with_current_field(nd, state)
        raw = resolve_value_from_event(str(config["value_key"]), ctx) if config.get("value_key") else nd
        new_value = coerce_data_value(raw)
        if new_value == state:
            logger.debug(
                "field_push data skip unchanged field=%s event_id=%s",
                field.name, event.id,
            )
            db.commit()  # release FOR UPDATE
            return
        field.state = new_value
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


def _template_mapping(obj: Any, data: dict) -> Any:
    """Recursively resolve {{…}} in string values (headers, query, JSON body)."""
    if isinstance(obj, str):
        return render_data_template(obj, data)
    if isinstance(obj, dict):
        return {k: _template_mapping(v, data) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_template_mapping(item, data) for item in obj]
    return obj


def _forward_headers(db, action: ActionInstance, config: dict, data: dict) -> dict:
    raw_headers = config.get("headers") or {}
    headers = {
        str(k): str(v)
        for k, v in _template_mapping(dict(raw_headers), data).items()
    }
    raw_auth_mode = config.get("auth_mode")
    auth_mode = (raw_auth_mode or "none").strip()
    auth_mode_explicit = raw_auth_mode is not None
    if auth_mode == "key_secret":
        if not action.secret_id or not action.secret_id_2:
            raise ValueError("This forward needs both API key and secret")
        key = _secret_value(db, action.secret_id)
        secret = _secret_value(db, action.secret_id_2)
        headers[config.get("api_key_header") or "X-Api-Key"] = key
        headers[config.get("api_secret_header") or "X-Api-Secret"] = secret
    elif action.secret_id and (not auth_mode_explicit or auth_mode == "bearer"):
        token = _secret_value(db, action.secret_id)
        header_name = config.get("auth_header", "Authorization")
        prefix = config.get("auth_prefix", "Bearer ")
        headers[header_name] = f"{prefix}{token}"
    return headers


def _send_http(
    db,
    action: ActionInstance,
    config: dict,
    *,
    url: str,
    method: str,
    headers: dict,
    body: Any = None,
    body_text: str | None = None,
    query: dict | None = None,
) -> None:
    """POST/PUT/PATCH/GET with optional HMAC signing. Raises on HTTP errors."""
    method = (method or "POST").upper()
    timeout = float(config.get("timeout_seconds") or 30)
    url = (url or "").strip()
    if not url:
        raise ValueError("Forward URL is missing")

    signing_mode = (config.get("signing_mode") or "none").strip()
    if signing_mode == "hmac_sha256":
        if action.secret_id_2:
            signing_key = _secret_value(db, action.secret_id_2)
        elif action.secret_id:
            signing_key = _secret_value(db, action.secret_id)
        else:
            raise ValueError("HMAC signing requires a configured secret")

        ts = str(int(time.time()))
        signature_header = (config.get("signing_signature_header") or "X-Call-Signature").strip()
        timestamp_header = (config.get("signing_timestamp_header") or "X-Call-Timestamp").strip()

        body_str = ""
        if method in ("POST", "PUT", "PATCH"):
            if body_text is not None:
                body_str = body_text
            elif body is not None:
                body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
        message = f"{method}\n{url}\n{ts}\n{body_str}".encode()
        signature = hmac_mod.new(signing_key.encode(), message, hashlib.sha256).hexdigest()
        headers = dict(headers)
        headers[signature_header] = signature
        headers[timestamp_header] = ts

    with httpx.Client(timeout=timeout) as client:
        kwargs: dict[str, Any] = {"method": method, "url": url, "headers": headers}
        if method in ("POST", "PUT", "PATCH"):
            if body_text is not None:
                kwargs["content"] = body_text.encode("utf-8")
                headers.setdefault("Content-Type", "text/plain; charset=utf-8")
                kwargs["headers"] = headers
            elif body is not None:
                kwargs["json"] = body
        elif method == "GET":
            kwargs["params"] = query or None
        response = client.request(**kwargs)
        response.raise_for_status()


def build_notify_request(
    service: str,
    *,
    server_url: str,
    topic: str = "",
    title: str = "",
    body: str = "",
    priority: int | None = None,
) -> dict:
    """Map notify/preset fields to http_forward-shaped request parts.

    Returns dict with keys: url, method, headers, body (JSON) or body_text (str).
    """
    service = (service or "").strip().lower()
    base = (server_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Server URL is required")
    title = title or ""
    body = body or ""

    if service == "ntfy":
        topic = (topic or "").strip().strip("/")
        if not topic:
            raise ValueError("Topic is required for ntfy")
        url = f"{base}/{topic}"
        headers: dict[str, str] = {}
        if title:
            headers["Title"] = title
        if priority is not None:
            headers["Priority"] = str(priority)
        return {
            "url": url,
            "method": "POST",
            "headers": headers,
            "body_text": body,
        }

    if service == "gotify":
        url = urljoin(base + "/", "message")
        payload: dict[str, Any] = {"message": body or title or "Para-Scope"}
        if title:
            payload["title"] = title
        if priority is not None:
            payload["priority"] = int(priority)
        return {
            "url": url,
            "method": "POST",
            "headers": {},
            "body": payload,
        }

    if service == "discord":
        # Discord webhook URL is the full server_url
        content = body or title or "Para-Scope"
        if title and body and title != body:
            content = f"**{title}**\n{body}"
        elif title and not body:
            content = title
        return {
            "url": base if base.startswith("http") else server_url.strip(),
            "method": "POST",
            "headers": {},
            "body": {"content": content[:2000]},
        }

    raise ValueError(f"Unknown notify service: {service}")


def _action_http_forward(db, event: Event, action: ActionInstance) -> None:
    config = action.config or {}
    nd = event.normalized_data or {}
    url = render_data_template((config.get("url") or "").strip(), nd)
    if not url:
        raise ValueError("Forward URL is missing")
    method = (config.get("method") or "POST").upper()
    headers = _forward_headers(db, action, config, nd)

    body_mode = config.get("body_mode", "auto")
    body = None
    body_text = None
    query = None

    if body_mode == "text":
        body_text = render_data_template(str(config.get("body_text") or ""), nd)
    elif body_mode == "custom":
        raw_body = config.get("custom_body")
        if isinstance(raw_body, (dict, list)):
            body = _template_mapping(raw_body, nd)
        else:
            body = raw_body if raw_body is not None else {}
    else:
        body = {
            "event_id": event.id,
            "source_id": event.source_id,
            "correlation_id": event.correlation_id,
            "data": nd,
        }

    if method == "GET":
        raw_q = config.get("query") or {}
        query = {
            str(k): str(v)
            for k, v in _template_mapping(dict(raw_q), nd).items()
        } or None

    _send_http(
        db, action, config,
        url=url, method=method, headers=headers,
        body=body, body_text=body_text, query=query,
    )


def _action_notify(db, event: Event, action: ActionInstance) -> None:
    config = action.config or {}
    service = (config.get("service") or "").strip().lower()
    if service not in _NOTIFY_SERVICES:
        raise ValueError("Choose ntfy, Gotify, or Discord")
    nd = event.normalized_data or {}
    title = render_data_template(str(config.get("title") or ""), nd)
    body = render_data_template(str(config.get("body") or ""), nd)
    server_url = render_data_template(str(config.get("server_url") or ""), nd)
    topic = render_data_template(str(config.get("topic") or ""), nd)
    priority = config.get("priority")
    if priority is not None and priority != "":
        try:
            priority = int(priority)
        except (TypeError, ValueError) as e:
            raise ValueError("Priority must be a number") from e
    else:
        priority = None

    req = build_notify_request(
        service,
        server_url=server_url,
        topic=topic,
        title=title,
        body=body,
        priority=priority,
    )
    # Auth: optional bearer for ntfy/gotify via secret_id
    fwd_config = {
        "timeout_seconds": config.get("timeout_seconds"),
        "auth_mode": config.get("auth_mode") or ("bearer" if action.secret_id else "none"),
        "auth_header": config.get("auth_header"),
        "auth_prefix": config.get("auth_prefix"),
        "signing_mode": "none",
        "headers": req.get("headers") or {},
    }
    headers = _forward_headers(db, action, fwd_config, nd)
    # Merge service headers after auth so Title etc. aren't wiped — auth wins on clash
    for k, v in (req.get("headers") or {}).items():
        headers.setdefault(k, v)

    _send_http(
        db, action, fwd_config,
        url=req["url"],
        method=req["method"],
        headers=headers,
        body=req.get("body"),
        body_text=req.get("body_text"),
    )


def _local_allowlist() -> list[str]:
    raw = (os.environ.get("PARA_SCOPE_LOCAL_ACTION_ALLOWLIST") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(":") if p.strip()]


def _check_local_allowlist(executable: str) -> None:
    allow = _local_allowlist()
    if not allow:
        return
    resolved = os.path.realpath(executable)
    for entry in allow:
        entry_real = os.path.realpath(entry)
        if resolved == entry_real or resolved.startswith(entry_real.rstrip("/") + "/"):
            return
    raise ValueError(f"Script path is not on the local-action allowlist: {resolved}")


def _action_local_script(db, event: Event, action: ActionInstance) -> None:
    if not local_actions_enabled():
        raise ValueError(
            "Local scripts are disabled. Set PARA_SCOPE_ALLOW_LOCAL_ACTIONS=1 to enable."
        )
    config = action.config or {}
    nd = event.normalized_data or {}
    timeout = float(config.get("timeout_seconds") or _LOCAL_TIMEOUT_DEFAULT)
    timeout = max(1.0, min(timeout, _LOCAL_TIMEOUT_MAX))

    argv = config.get("argv")
    use_shell = bool(config.get("shell"))
    if isinstance(argv, list) and argv:
        cmd = [render_data_template(str(a), nd) for a in argv]
        if not cmd[0]:
            raise ValueError("Script path is empty")
        _check_local_allowlist(cmd[0])
        use_shell = False
        run_arg: str | list[str] = cmd
    else:
        command = render_data_template(str(config.get("command") or ""), nd).strip()
        if not command:
            raise ValueError("Command is required")
        use_shell = True
        # Allowlist: first token of the command when it looks like a path
        first = command.split(None, 1)[0]
        if first.startswith("/") or first.startswith("./"):
            _check_local_allowlist(first)
        run_arg = command

    try:
        result = subprocess.run(
            run_arg,
            shell=use_shell,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError(f"Local script timed out after {timeout}s") from e

    if result.stdout:
        logger.info("local_script stdout: %s", result.stdout[:2000])
    if result.stderr:
        logger.warning("local_script stderr: %s", result.stderr[:2000])
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:500]
        raise ValueError(
            f"Local script exited {result.returncode}"
            + (f": {err}" if err else "")
        )


def _action_web_push(db, event: Event, action: ActionInstance) -> None:
    """Send a Web Push notification to all subscribed users."""
    vapid = vapid_config()
    if not vapid:
        raise ValueError("Browser notifications aren’t configured")

    config = action.config or {}
    data = event.normalized_data or {}
    title = render_data_template(config.get("title") or "Para-Scope", data)
    body = render_data_template(config.get("body") or "", data)
    url = render_data_template(config.get("url") or "/", data)
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
register_action("notify", _action_notify)
register_action("web_push", _action_web_push)
register_action("local_script", _action_local_script)

"""HTTP poller handlers — fetch remote data and feed events into the pipeline."""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

import httpx

from app.database import SessionLocal
from app.fields import get_by_path
from app.models import Event, EventTypeRecord, PollingSchedule, Source
from app.pipeline import evaluate_and_dispatch
from app.security import decrypt_secret

logger = logging.getLogger("para_scope.poller")

_POLLERS: dict[str, Callable] = {}


def register_poller(handler_type: str, fn: Callable):
    """Register a poller handler: fn(schedule, db) -> result dict."""
    _POLLERS[handler_type] = fn


def get_poller_types() -> list[str]:
    return sorted(_POLLERS.keys())


def _build_headers(db, params: dict) -> dict:
    headers = dict(params.get("headers") or {})
    secret_id = params.get("auth_secret_id")
    if secret_id:
        from app.models import Secret
        secret = db.query(Secret).filter(Secret.id == secret_id).first()
        if not secret:
            raise ValueError(f"auth_secret_id {secret_id} not found")
        header_name = params.get("auth_header", "Authorization")
        prefix = params.get("auth_prefix", "Bearer ")
        headers[header_name] = f"{prefix}{decrypt_secret(secret.encrypted_value)}"
    return headers


_HTTP_METHODS = {
    "http_get": "GET",
    "http_post": "POST",
    "http_put": "PUT",
    "http_delete": "DELETE",
}


def http_poll(schedule: PollingSchedule, db) -> dict:
    """Execute one HTTP poll for a schedule. Returns result metadata.

    handler_params keys:
      - headers: dict of extra headers
      - query: dict of query string params
      - body: JSON body (for POST)
      - json_path: dotted path into response JSON to use as event data
      - event_type: name of EventTypeRecord to attach (optional)
      - auth_secret_id / auth_header / auth_prefix: optional bearer auth
    """
    params = schedule.handler_params or {}
    method = _HTTP_METHODS.get(schedule.handler_type, "GET")

    if not schedule.handler_url:
        raise ValueError("handler_url is required for HTTP polling")

    headers = _build_headers(db, params)
    timeout = schedule.timeout_seconds or 30
    retries = schedule.retry_count or 0
    last_error = None

    for attempt in range(1 + retries):
        try:
            t0 = time.perf_counter()
            with httpx.Client(timeout=timeout) as client:
                kwargs = {
                    "method": method,
                    "url": schedule.handler_url,
                    "headers": headers,
                    "params": params.get("query") or None,
                }
                if method in ("POST", "PUT") and "body" in params:
                    kwargs["json"] = params["body"]
                response = client.request(**kwargs)
                response.raise_for_status()
            response_time_ms = round((time.perf_counter() - t0) * 1000, 2)

            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                payload = {"raw": response.text[:65536]}

            extracted = get_by_path(payload, params.get("json_path", ""))
            if extracted is None:
                extracted = payload

            return {
                "ok": True,
                "status_code": response.status_code,
                "data": extracted,
                "raw": response.text[:65536],
                "attempts": attempt + 1,
                "response_time_ms": response_time_ms,
            }
        except Exception as e:
            last_error = e
            if attempt < retries:
                continue

    raise last_error or RuntimeError("poll failed")


def _http_poll_registered(schedule: PollingSchedule, db) -> dict:
    """Thin wrapper so tests can patch `http_poll` and the registry still sees it."""
    return http_poll(schedule, db)


for _ht in _HTTP_METHODS:
    register_poller(_ht, _http_poll_registered)


def run_schedule(schedule_id: int) -> bool:
    """Entry point called by the scheduler for a single schedule id.

    Returns True on success, False on failure (or no-op skip).
    """
    db = SessionLocal()
    success = False
    try:
        schedule = db.query(PollingSchedule).filter(PollingSchedule.id == schedule_id).first()
        if not schedule or not schedule.enabled:
            return False

        source = db.query(Source).filter(Source.id == schedule.source_id).first()
        if not source or not source.enabled:
            return False

        now = datetime.now(timezone.utc)
        handler = _POLLERS.get(schedule.handler_type)
        t0 = time.perf_counter()
        try:
            if handler is None:
                raise ValueError(f"Unknown poller handler_type: {schedule.handler_type}")
            result = handler(schedule, db)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            if isinstance(result, dict) and result.get("response_time_ms") is None:
                result["response_time_ms"] = elapsed_ms
            _create_poll_event(db, schedule, source, result, outcome="on_success")
            _create_poll_event(
                db, schedule, source, result,
                outcome="on_success", type_name="always",
            )
            schedule.success_count = (schedule.success_count or 0) + 1
            schedule.last_error = ""
            success = True
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            schedule.failure_count = (schedule.failure_count or 0) + 1
            schedule.last_error = str(e)[:2000]
            logger.exception(
                "Poll failed schedule_id=%s name=%s",
                schedule.id, schedule.name,
            )
            fail_result = {
                "ok": False,
                "data": {"error": str(e)[:2000]},
                "raw": str(e)[:65536],
                "response_time_ms": elapsed_ms,
            }
            _create_poll_event(
                db, schedule, source, fail_result, outcome="on_failure",
            )
            _create_poll_event(
                db, schedule, source, fail_result,
                outcome="on_failure", type_name="always",
            )
            success = False

        schedule.last_run_at = now
        db.commit()
        return success
    finally:
        db.close()


def _resolve_poll_event_type(db, schedule, source, outcome: str, *, type_name: str | None = None):
    """Resolve EventTypeRecord for a poll outcome.

    Success uses handler_params.event_type when set, else falls back to on_success.
    Failure uses on_failure.
    When type_name is set (e.g. 'always'), look up that name only — return None if missing.
    """
    if type_name:
        return db.query(EventTypeRecord).filter(
            EventTypeRecord.source_id == source.id,
            EventTypeRecord.name == type_name,
        ).first()

    params = schedule.handler_params or {}
    if outcome == "on_failure":
        et_name = "on_failure"
    else:
        et_name = (params.get("event_type") or "").strip() or "on_success"
    return db.query(EventTypeRecord).filter(
        EventTypeRecord.source_id == source.id,
        EventTypeRecord.name == et_name,
    ).first()


def _create_poll_event(
    db, schedule, source, result: dict, *, outcome: str = "on_success",
    type_name: str | None = None,
):
    """Turn a poll result into an Event and run the pipeline.

    If type_name is set (e.g. 'always'), only emit when that event type exists
    on the source; otherwise no-op. Not pre-seeded on poll create.
    """
    event_type = _resolve_poll_event_type(
        db, schedule, source, outcome, type_name=type_name,
    )
    if type_name and event_type is None:
        return

    data = result.get("data")
    if not isinstance(data, dict):
        data = {"value": data}

    poll_meta = {
        "schedule_id": schedule.id,
        "status_code": result.get("status_code"),
        "attempts": result.get("attempts"),
        "outcome": outcome,
        "response_time_ms": result.get("response_time_ms"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if type_name:
        poll_meta["trigger"] = type_name

    normalized = {
        **data,
        "_poll": poll_meta,
        "source": source.name,
    }

    from app.ingest import ingest_event
    event = ingest_event(
        db,
        source=source,
        event_type_id=event_type.id if event_type else None,
        correlation_id=str(uuid.uuid4()),
        raw_payload=result.get("raw", "")[:65536],
        normalized_data=normalized,
        touch_last_seen=False,
    )

    try:
        evaluate_and_dispatch(db, event)
        if event.status != "failed":
            event.status = "processed"
    except Exception as e:
        event.status = "failed"
        event.processing_error = str(e)
        logger.exception(
            "Poll event pipeline failed event_id=%s schedule_id=%s",
            event.id, schedule.id,
        )

    source.last_seen_at = datetime.now(timezone.utc)
    db.commit()

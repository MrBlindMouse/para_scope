"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from pathlib import Path
from datetime import datetime, timezone
import json
import hashlib
import hmac as hmac_mod
import time
import uuid
import logging

from app.database import get_db
from app.models import (
    User, Source, SourceStatus, EventTypeRecord, PollingSchedule, ScheduleType,
    ActionInstance, Rule, Secret, DashboardLayout, Event, AuditLog, MetricPoint,
    PushSubscription, Field, FieldLogEntry,
)
from app.security import (
    verify_password, hash_password, encrypt_secret, decrypt_secret,
    create_session_token, verify_session_token, generate_csrf_token,
    SESSION_MAX_AGE_SECONDS,
)
from app.pipeline import evaluate_and_dispatch
from app.widgets import fetch_widget_data, get_widget_types
from app.dashboard_layout import (
    find_widget, layout_json, merge_geometry, migrate_widgets,
    normalize_for_save, parse_layout_config,
)
from app.scheduler import add_or_update_job, remove_job, job_count
from app.ingest import ingest_event

from app import webctx as ctx

router = APIRouter()

# route: /sw.js
@router.get("/sw.js")
async def service_worker():
    """Service worker at site root so scope can be /."""
    path = Path(__file__).resolve().parent.parent / "static" / "sw.js"
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )



# route: /webhook/{source_slug}
@router.post("/webhook/{source_slug}")
async def handle_webhook(
    source_slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Accept incoming webhook events from sources.

    Returns 202 after the event row is committed; pipeline runs via BackgroundTasks.
    """
    source = db.query(Source).filter(Source.slug == source_slug).first()
    if not source:
        return JSONResponse({"error": "Source not found"}, status_code=404)
    if not source.enabled:
        return JSONResponse({"error": "Source disabled"}, status_code=403)

    client_ip = request.client.host if request.client else "unknown"
    if not ctx._check_webhook_rate_limit(f"{client_ip}:{source_slug}"):
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)

    raw_body = await request.body()

    # Input size limit
    if len(raw_body) > ctx._WEBHOOK_MAX_BODY:
        return JSONResponse({"error": "Payload too large (max 256KB)"}, status_code=413)

    signature = (request.headers.get("x-webhook-signature") or "").strip()
    actual = signature.replace("sha256=", "") if signature else ""
    timestamp_str = (request.headers.get("x-webhook-timestamp") or "").strip()

    if source.webhook_secret_id:
        secret = db.query(Secret).filter(Secret.id == source.webhook_secret_id).first()
        if not secret:
            return JSONResponse({"error": "Webhook secret not configured"}, status_code=401)
        # Replay protection is mandatory when signing: require timestamp and bind it into HMAC
        if not timestamp_str:
            return JSONResponse(
                {"error": "Timestamp required", "hint": "Send X-Webhook-Timestamp (unix seconds)"},
                status_code=400,
            )
        try:
            ts = float(timestamp_str)
        except ValueError:
            return JSONResponse({"error": "Invalid timestamp"}, status_code=400)
        now = time.time()
        if abs(now - ts) > ctx._WEBHOOK_REPLAY_TTL_SECONDS:
            return JSONResponse({"error": "Timestamp expired"}, status_code=400)

        signed_payload = f"{timestamp_str}.".encode() + raw_body
        expected = hmac_mod.new(
            decrypt_secret(secret.encrypted_value).encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if not hmac_mod.compare_digest(expected, actual):
            return JSONResponse({"error": "Invalid signature"}, status_code=401)

        replay_key = f"{source.id}:{actual}"
        ctx._cleanup_replay_cache(now - ctx._WEBHOOK_REPLAY_TTL_SECONDS)
        if replay_key in ctx._WEBHOOK_REPLAY_CACHE:
            return JSONResponse({"error": "Duplicate request"}, status_code=409)
        ctx._WEBHOOK_REPLAY_CACHE[replay_key] = now
    elif timestamp_str:
        # Optional soft replay check for unsigned sources that send a timestamp
        try:
            ts = float(timestamp_str)
        except ValueError:
            return JSONResponse({"error": "Invalid timestamp"}, status_code=400)
        now = time.time()
        if abs(now - ts) > ctx._WEBHOOK_REPLAY_TTL_SECONDS:
            return JSONResponse({"error": "Timestamp expired"}, status_code=400)
        replay_key = f"{source.id}:{ts}"
        ctx._cleanup_replay_cache(now - ctx._WEBHOOK_REPLAY_TTL_SECONDS)
        if replay_key in ctx._WEBHOOK_REPLAY_CACHE:
            return JSONResponse({"error": "Duplicate request"}, status_code=409)
        ctx._WEBHOOK_REPLAY_CACHE[replay_key] = now

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Resolve event type: header preferred, then payload fields
    et_name = (request.headers.get("x-event-type") or "").strip()
    if not et_name and isinstance(payload, dict):
        et_name = str(payload.get("event_type") or payload.get("type") or "").strip()

    registered_types = db.query(EventTypeRecord).filter(
        EventTypeRecord.source_id == source.id
    ).all()
    event_type = None

    if registered_types:
        # Source has registered types — require a matching event type
        if not et_name:
            names = sorted({et.name for et in registered_types})
            return JSONResponse(
                {
                    "error": "Event type required",
                    "hint": "Send X-Event-Type header or event_type/type in JSON body",
                    "registered": names,
                },
                status_code=400,
            )
        event_type = next((et for et in registered_types if et.name == et_name), None)
        if not event_type:
            return JSONResponse(
                {"error": f"Event type '{et_name}' not found for source"},
                status_code=400,
            )
    elif et_name:
        # No registry yet — still attach if a matching type exists (none will)
        event_type = db.query(EventTypeRecord).filter(
            EventTypeRecord.source_id == source.id,
            EventTypeRecord.name == et_name,
        ).first()
        if not event_type:
            return JSONResponse(
                {"error": f"Event type '{et_name}' not found for source"},
                status_code=400,
            )

    correlation_id = (request.headers.get("x-correlation-id") or "").strip() or str(uuid.uuid4())
    raw_truncated = raw_body.decode("utf-8", errors="replace")[:65536]

    webhook_meta = {
        "slug": source.slug,
        "method": request.method,
        "path": request.url.path,
        "content_type": (request.headers.get("content-type") or "").split(";")[0].strip() or None,
        "body_bytes": len(raw_body),
        "client": request.client.host if request.client else None,
        "user_agent": ((request.headers.get("user-agent") or "").strip() or None),
        "event_type": et_name or None,
        "correlation_id": correlation_id,
        "signed": bool(source.webhook_secret_id and actual),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(webhook_meta["user_agent"], str) and len(webhook_meta["user_agent"]) > 200:
        webhook_meta["user_agent"] = webhook_meta["user_agent"][:200]
    if timestamp_str:
        try:
            webhook_meta["delivery_lag_ms"] = round((time.time() - float(timestamp_str)) * 1000, 2)
        except ValueError:
            pass

    if isinstance(payload, dict):
        normalized = {**payload, "source": source.name, "_webhook": webhook_meta}
    else:
        normalized = {"value": payload, "source": source.name, "_webhook": webhook_meta}

    from app.ingest import ingest_event
    event = ingest_event(
        db,
        source=source,
        event_type_id=event_type.id if event_type else None,
        correlation_id=correlation_id,
        raw_payload=raw_truncated,
        normalized_data=normalized,
    )

    ctx._audit_log(
        db, request, "webhook.accepted",
        resource_type="source", resource_id=source.id,
        details={"slug": source.slug, "event_id": event.id, "correlation_id": correlation_id},
    )

    background_tasks.add_task(ctx._process_webhook_event, event.id)
    return JSONResponse({"status": "accepted", "event_id": event.id}, status_code=202)


# ── Health ──────────────────────────────────────────────────────────────────


# route: /health
@router.get("/health")
async def health():
    return {"status": "ok"}

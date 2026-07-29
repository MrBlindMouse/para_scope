"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy.orm import Session
from pathlib import Path
from datetime import datetime, timezone
import json
import time
import uuid

from app.database import get_db
from app.models import (
    Source,
    EventTypeRecord,
)
from app.ingest import ingest_event

from app import webctx as ctx
from app.webhook_verifiers import verify_webhook_request, WebhookAuthError

router = APIRouter()

# Discord InteractionType → event type names for pipeline matching.
_DISCORD_INTERACTION_TYPES = {
    2: "application_command",
    3: "message_component",
    4: "application_command_autocomplete",
    5: "modal_submit",
}

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

    timestamp_str = (request.headers.get("x-webhook-timestamp") or "").strip()
    try:
        verified = verify_webhook_request(db=db, source=source, request=request, raw_body=raw_body)
    except WebhookAuthError as e:
        return JSONResponse(e.payload, status_code=e.status_code)

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    provider = ((source.config or {}).get("webhook_provider") or "generic_hmac").strip() or "generic_hmac"
    if provider == "discord" and isinstance(payload, dict) and payload.get("type") == 1:
        return JSONResponse({"type": 1}, status_code=200)

    # Resolve event type: header preferred, then payload fields.
    # `always` is an optional side-emission (like pollers), not a producer type.
    et_name = (request.headers.get("x-event-type") or "").strip()
    if not et_name and isinstance(payload, dict):
        if provider == "discord":
            raw_type = payload.get("type")
            mapped = _DISCORD_INTERACTION_TYPES.get(raw_type)
            if mapped:
                et_name = mapped
            elif raw_type is not None:
                et_name = str(raw_type).strip()
        if not et_name:
            et_name = str(payload.get("event_type") or payload.get("type") or "").strip()

    registered_types = db.query(EventTypeRecord).filter(
        EventTypeRecord.source_id == source.id
    ).all()
    always_et = next((et for et in registered_types if et.name == "always"), None)
    match_types = [et for et in registered_types if et.name != "always"]
    event_type = None

    if match_types:
        # Source has producer-facing types — require a matching event type
        if not et_name:
            names = sorted({et.name for et in match_types})
            return JSONResponse(
                {
                    "error": "Event type required",
                    "hint": "Send X-Event-Type header or event_type/type in JSON body",
                    "registered": names,
                },
                status_code=400,
            )
        event_type = next((et for et in match_types if et.name == et_name), None)
        if not event_type:
            return JSONResponse(
                {"error": f"Event type '{et_name}' not found for source"},
                status_code=400,
            )
    elif et_name:
        # Only `always` (or empty) registered — attach if the named type exists
        event_type = next((et for et in registered_types if et.name == et_name), None)
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
        "signed": bool(verified.signed),
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

    event = ingest_event(
        db,
        source=source,
        event_type_id=event_type.id if event_type else None,
        correlation_id=correlation_id,
        raw_payload=raw_truncated,
        normalized_data=normalized,
    )

    always_event = None
    if always_et and (event_type is None or event_type.id != always_et.id):
        always_meta = {**webhook_meta, "trigger": "always"}
        if isinstance(payload, dict):
            always_normalized = {**payload, "source": source.name, "_webhook": always_meta}
        else:
            always_normalized = {"value": payload, "source": source.name, "_webhook": always_meta}
        always_event = ingest_event(
            db,
            source=source,
            event_type_id=always_et.id,
            correlation_id=correlation_id,
            raw_payload=raw_truncated,
            normalized_data=always_normalized,
            touch_last_seen=False,
        )

    details = {"slug": source.slug, "event_id": event.id, "correlation_id": correlation_id}
    if always_event:
        details["always_event_id"] = always_event.id
    ctx._audit_log(
        db, request, "webhook.accepted",
        resource_type="source", resource_id=source.id,
        details=details,
    )

    background_tasks.add_task(ctx._process_webhook_event, event.id)
    if always_event:
        background_tasks.add_task(ctx._process_webhook_event, always_event.id)
    body = {"status": "accepted", "event_id": event.id}
    if always_event:
        body["always_event_id"] = always_event.id
    return JSONResponse(body, status_code=202)


# ── Health ──────────────────────────────────────────────────────────────────


# route: /health
@router.get("/health")
async def health():
    return {"status": "ok"}

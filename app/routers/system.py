"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import hashlib
import hmac as hmac_mod
import time
import uuid
import logging

from app.database import get_db
from app.models import (
    User, Source, EventTypeRecord, PollingSchedule, ScheduleType,
    ActionInstance, Rule, Secret, DashboardLayout, Event, AuditLog, MetricPoint,
    PushSubscription, Field, FieldLogEntry,
)
from app.security import (
    verify_password, hash_password, encrypt_secret, decrypt_secret,
    create_session_token, verify_session_token, generate_csrf_token,
    SESSION_MAX_AGE_SECONDS,
)
from app.pipeline import evaluate_and_dispatch
from app.scheduler import add_or_update_job, remove_job, job_count
from app.ingest import ingest_event
from app.themes import (
    THEME_OPTIONS, FONT_OPTIONS, FONT_SIZE_OPTIONS,
    get_theme, get_font, get_font_size, get_display_timezone, update_style,
    get_dashboard_bg_filename, get_dashboard_bg_opacity, dashboard_bg_path,
    clamp_opacity,
)

from app import webctx as ctx

router = APIRouter()


def _iso_utc(dt: datetime) -> str:
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)).isoformat()

# route: /config/style
@router.get("/config/style")
async def config_style(request: Request, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    return ctx.templates.TemplateResponse(
        request, "config/style.html", {
            "active": "style",
            "themes": THEME_OPTIONS,
            "fonts": FONT_OPTIONS,
            "font_sizes": FONT_SIZE_OPTIONS,
            "current_theme": get_theme(db),
            "current_font": get_font(db),
            "current_font_size": get_font_size(db),
            "current_display_timezone": get_display_timezone(db),
            "current_dashboard_bg": bool(get_dashboard_bg_filename(db)),
            "current_dashboard_bg_opacity": get_dashboard_bg_opacity(db),
            "success": success,
            "error": error,
        }
    )


# route: /config/style
@router.post("/config/style")
async def save_style(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    theme = (form.get("theme") or "").strip()
    font = (form.get("font") or "").strip()
    font_size = (form.get("font_size") or "").strip()
    display_timezone = (form.get("display_timezone") or "").strip()
    opacity_raw = form.get("dashboard_bg_opacity")
    # Slider posts 0–100; also accept 0–1
    if opacity_raw is not None and str(opacity_raw).strip() != "":
        try:
            ov = float(opacity_raw)
            if ov > 1:
                ov = ov / 100.0
            opacity = clamp_opacity(ov)
        except (TypeError, ValueError):
            opacity = get_dashboard_bg_opacity(db)
    else:
        opacity = get_dashboard_bg_opacity(db)
    clear_bg = str(form.get("clear_dashboard_bg") or "").lower() in ("1", "true", "on", "yes")
    upload = form.get("dashboard_bg")
    bg_bytes = None
    bg_ct = None
    if upload is not None and hasattr(upload, "read"):
        raw = await upload.read()
        if raw:
            bg_bytes = raw
            bg_ct = getattr(upload, "content_type", None)
    saved, err = update_style(
        db,
        theme=theme,
        font=font,
        font_size=font_size,
        display_timezone=display_timezone,
        dashboard_bg_opacity=opacity,
        clear_dashboard_bg=clear_bg,
        dashboard_bg_bytes=bg_bytes,
        dashboard_bg_content_type=bg_ct,
    )
    if err:
        return RedirectResponse(
            url=ctx.flash_url("/config/style", error=err),
            status_code=303,
        )
    user = ctx._get_user(request, db)
    ctx._audit_log(
        db, request, "style.update",
        user_id=user.id if user else None,
        resource_type="app_settings",
        resource_id=1,
        details=saved,
    )
    return RedirectResponse(
        url=ctx.flash_url("/config/style", success="Style saved"),
        status_code=303,
    )


# route: /media/dashboard-bg
@router.get("/media/dashboard-bg")
async def media_dashboard_bg(db: Session = Depends(get_db)):
    name = get_dashboard_bg_filename(db)
    path = dashboard_bg_path(name)
    if not path or not path.is_file():
        return HTMLResponse(status_code=404, content="Not found")
    media = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


# route: /config/users
@router.get("/config/users")
async def config_users(request: Request, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    users = db.query(User).all()
    return ctx.templates.TemplateResponse(
        request, "config/users.html", {"active": "users", "items": users,
         "success": success, "error": error}
    )



# route: /config/users
@router.post("/config/users")
async def create_user(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""

    if not username or not password:
        return RedirectResponse(
            url=ctx.flash_url("/config/users", error="Username and password are required"),
            status_code=303,
        )

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return RedirectResponse(
            url=ctx.flash_url("/config/users", error=f"User '{username}' already exists"),
            status_code=303,
        )

    user = User(username=username, hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    ctx._audit_log(db, request, "user.create", resource_type="user", resource_id=user.id, details={"username": username})
    return RedirectResponse(
        url=ctx.flash_url("/config/users", success=f"User '{username}' created"),
        status_code=303,
    )


# ── Config: Dashboard Layout ────────────────────────────────────────────────


# route: /config/secrets
@router.post("/config/secrets")
async def create_secret(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    scoped_to_type = (form.get("scoped_to_type") or "source").strip()
    scoped_to_id_str = form.get("scoped_to_id", "").strip()
    value = (form.get("value") or "").strip()

    if not scoped_to_id_str or not value:
        return ctx._pipeline_redirect(error="Target and value are required")

    try:
        scoped_to_id = int(scoped_to_id_str)
    except ValueError:
        return ctx._pipeline_redirect(error="Target must be a number")

    try:
        encrypted_value = encrypt_secret(value)
    except ValueError:
        return ctx._pipeline_redirect(error="Secrets aren’t available until the server is configured")
    secret = Secret(
        scoped_to_type=scoped_to_type,
        scoped_to_id=scoped_to_id, encrypted_value=encrypted_value,
    )
    db.add(secret)
    db.commit()
    ctx._audit_log(db, request, "secret.create", resource_type="secret", resource_id=secret.id)
    return ctx._pipeline_redirect(success="Secret created")



# route: /config/secret/{secret_id}/delete
@router.post("/config/secret/{secret_id}/delete")
async def delete_secret(request: Request, secret_id: int, db: Session = Depends(get_db)):
    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        return ctx._pipeline_redirect(error="Secret not found")
    sid = secret.id
    db.delete(secret)
    db.commit()
    ctx._audit_log(db, request, "secret.delete", resource_type="secret", resource_id=sid)
    return ctx._pipeline_redirect(success="Secret deleted")


# ── Config: Audit Log ───────────────────────────────────────────────────────


# route: /config/audit-log
@router.get("/config/audit-log")
async def config_audit_log(request: Request, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    user_id_filter = request.query_params.get("user_id", "").strip()
    action_filter = request.query_params.get("action", "").strip()
    page = int(request.query_params.get("page", 1))
    per_page = 50
    offset = (page - 1) * per_page

    query = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    if user_id_filter:
        query = query.filter(AuditLog.user_id == int(user_id_filter))
    if action_filter:
        query = query.filter(AuditLog.action.like(f"%{action_filter}%"))

    total = query.count()
    entries = query.offset(offset).limit(per_page).all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    return ctx.templates.TemplateResponse(
        request, "config/audit_log.html", {
            "active": "audit-log", "items": entries,
            "success": success, "error": error,
            "user_id_filter": user_id_filter, "action_filter": action_filter,
            "page": page, "total_pages": total_pages, "total": total,
        }
    )


# ── Event Log ───────────────────────────────────────────────────────────────


# route: /events
@router.get("/events")
async def list_events(request: Request, db: Session = Depends(get_db)):
    source_id_filter = request.query_params.get("source_id", "").strip()
    event_type_filter = request.query_params.get("event_type_id", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    page = int(request.query_params.get("page", 1))
    per_page = 50
    offset = (page - 1) * per_page

    query = db.query(Event).order_by(Event.timestamp.desc())
    sources = db.query(Source).order_by(Source.name).all()
    event_types = db.query(EventTypeRecord).all()

    if source_id_filter:
        query = query.filter(Event.source_id == int(source_id_filter))
    if event_type_filter:
        query = query.filter(Event.event_type_id == int(event_type_filter))
    if status_filter:
        query = query.filter(Event.status == status_filter)

    total = query.count()
    entries = query.offset(offset).limit(per_page).all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    return ctx.templates.TemplateResponse(
        request, "events.html", {
            "items": entries, "sources": sources, "event_types": event_types,
            "source_id_filter": source_id_filter, "event_type_filter": event_type_filter,
            "status_filter": status_filter,
            "page": page, "total_pages": total_pages, "total": total,
        }
    )



# route: /events/rows
@router.get("/events/rows")
async def events_live_rows(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: return new event rows newer than `after` id."""
    after = int(request.query_params.get("after") or 0)
    source_id_filter = request.query_params.get("source_id", "").strip()
    event_type_filter = request.query_params.get("event_type_id", "").strip()
    status_filter = request.query_params.get("status", "").strip()

    query = db.query(Event).filter(Event.id > after).order_by(Event.id.asc()).limit(50)
    if source_id_filter:
        query = query.filter(Event.source_id == int(source_id_filter))
    if event_type_filter:
        query = query.filter(Event.event_type_id == int(event_type_filter))
    if status_filter:
        query = query.filter(Event.status == status_filter)

    entries = list(reversed(query.all()))  # newest first for afterbegin swap
    if not entries:
        return HTMLResponse("")

    rows = []
    display_timezone = get_display_timezone(db)
    for item in entries:
        rows.append(
            ctx.templates.env.get_template("components/event_row.html").render(
                item=item,
                display_timezone=display_timezone,
            )
        )
    return HTMLResponse("".join(rows))



# route: /event/{event_id}
@router.get("/event/{event_id}")
async def view_event(request: Request, event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return RedirectResponse(url=ctx.flash_url("/events", error="Event not found"), status_code=303)
    source = db.query(Source).filter(Source.id == event.source_id).first() if event.source_id else None
    event_type = db.query(EventTypeRecord).filter(EventTypeRecord.id == event.event_type_id).first() if event.event_type_id else None
    return ctx.templates.TemplateResponse(
        request, "event_detail.html", {
            "event": event, "source": source, "event_type": event_type,
        }
    )


# ── Metrics Graphing ────────────────────────────────────────────────────────


# route: /metrics
@router.get("/metrics")
async def metrics_page(request: Request, db: Session = Depends(get_db)):
    """Time-series chart for MetricPoint data. Supports multiple series via name=a,b,c."""
    metric_names_selected = []
    for n in request.query_params.getlist("name"):
        for part in n.split(","):
            part = part.strip()
            if part and part not in metric_names_selected:
                metric_names_selected.append(part)

    source_id_str = request.query_params.get("source_id", "").strip()
    range_hours = int(request.query_params.get("range", "24") or 24)
    sources = db.query(Source).order_by(Source.name).all()

    all_metrics = db.query(MetricPoint.name).distinct().order_by(MetricPoint.name).all()
    metric_names = [m[0] for m in all_metrics]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=range_hours)
    series_list = []
    for metric_name in metric_names_selected:
        query = db.query(MetricPoint).filter(
            MetricPoint.name == metric_name,
            MetricPoint.timestamp >= cutoff,
        )
        if source_id_str:
            query = query.filter(MetricPoint.source_id == int(source_id_str))
        points = query.order_by(MetricPoint.timestamp).all()
        series_list.append({
            "name": metric_name,
            "points": [{"ts": _iso_utc(p.timestamp), "v": p.value} for p in points],
        })

    return ctx.templates.TemplateResponse(
        request, "metrics.html", {
            "metric_names_selected": metric_names_selected,
            "metric_name": ",".join(metric_names_selected),
            "source_id": source_id_str,
            "range_hours": range_hours, "sources": sources,
            "metric_names": metric_names, "series_list": series_list,
        }
    )



# route: /metrics/api
@router.get("/metrics/api")
async def metrics_api(request: Request, db: Session = Depends(get_db)):
    """JSON API for metric data — supports multiple series via name=a,b."""
    raw_names = request.query_params.get("name", "").strip()
    names = [n.strip() for n in raw_names.split(",") if n.strip()] if raw_names else []
    source_id_str = request.query_params.get("source_id", "").strip()
    range_hours = int(request.query_params.get("range", "24") or 24)
    if not names:
        return JSONResponse({"error": "name required"}, status_code=400)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=range_hours)
    series = []
    for metric_name in names:
        query = db.query(MetricPoint).filter(
            MetricPoint.name == metric_name,
            MetricPoint.timestamp >= cutoff,
        )
        if source_id_str:
            query = query.filter(MetricPoint.source_id == int(source_id_str))
        points = query.order_by(MetricPoint.timestamp).all()
        series.append({
            "name": metric_name,
            "points": [{"ts": _iso_utc(p.timestamp), "v": p.value} for p in points],
        })
    return {"series": series}


# ── Help ─────────────────────────────────────────────────────────────────────


# route: /help
@router.get("/help")
async def help_page(request: Request):
    return ctx.templates.TemplateResponse(
        request, "config/help.html", {"active": "help"}
    )


# ── System Observability ────────────────────────────────────────────────────


# route: /system
@router.get("/system")
async def system_page(request: Request, db: Session = Depends(get_db)):
    """Show system health: sources, scheduler, webhook audit, retained event log."""
    from app.event_store import MAX_EVENTS_PER_SOURCE

    total_sources = db.query(Source).count()
    enabled_sources = db.query(Source).filter(Source.enabled == True).count()  # noqa: E712

    retained_events = db.query(Event).count()
    events_pending = db.query(Event).filter(Event.status == "pending").count()

    webhook_accepted = db.query(AuditLog).filter(
        AuditLog.action == "webhook.accepted"
    ).count()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    webhook_accepted_last_hour = db.query(AuditLog).filter(
        AuditLog.action == "webhook.accepted",
        AuditLog.timestamp >= cutoff,
    ).count()

    schedules = db.query(PollingSchedule).all()
    schedule_info = []
    for s in schedules:
        schedule_info.append({
            "name": s.name,
            "source_id": s.source_id,
            "last_run": s.last_run_at,
            "next_run": s.next_run_at,
            "enabled": s.enabled,
            "success_count": s.success_count or 0,
            "failure_count": s.failure_count or 0,
            "last_error": s.last_error or "",
        })

    from app.widgets import source_age_status

    source_health = []
    all_sources = db.query(Source).all()
    now = datetime.now(timezone.utc)
    for src in all_sources:
        last_seen = src.last_seen_at
        source_health.append({
            "id": src.id, "name": src.name, "slug": src.slug,
            "enabled": src.enabled, "last_seen": last_seen,
            "status": source_age_status(last_seen, now=now),
        })

    return ctx.templates.TemplateResponse(
        request, "system.html", {
            "active": "system",
            "total_sources": total_sources,
            "enabled_sources": enabled_sources,
            "job_count": job_count(),
            "webhook_accepted": webhook_accepted,
            "webhook_accepted_last_hour": webhook_accepted_last_hour,
            "retained_events": retained_events,
            "events_pending": events_pending,
            "max_events_per_source": MAX_EVENTS_PER_SOURCE,
            "schedules": schedule_info,
            "source_health": source_health,
        }
    )


# ── Webhook Ingress ─────────────────────────────────────────────────────────


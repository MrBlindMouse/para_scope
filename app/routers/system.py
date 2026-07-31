"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

from app.database import get_db
from app.models import (
    User,
    Source,
    EventTypeRecord,
    PollingSchedule,
    Event,
    AuditLog,
)
from app.security import (
    hash_password,
)
from app.scheduler import job_count
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


# ── Config: Audit Log ───────────────────────────────────────────────────────


# route: /config/audit-log
@router.get("/config/audit-log")
async def config_audit_log(request: Request, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    user_id_filter = request.query_params.get("user_id", "").strip()
    action_filter = request.query_params.get("action", "").strip()
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    query = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    if user_id_filter:
        try:
            query = query.filter(AuditLog.user_id == int(user_id_filter))
        except ValueError:
            pass
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
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    query = db.query(Event).order_by(Event.timestamp.desc())
    sources = db.query(Source).order_by(Source.name).all()
    event_types = db.query(EventTypeRecord).all()

    if source_id_filter:
        try:
            query = query.filter(Event.source_id == int(source_id_filter))
        except ValueError:
            pass
    if event_type_filter:
        try:
            query = query.filter(Event.event_type_id == int(event_type_filter))
        except ValueError:
            pass
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


def _render_event_row(item, display_timezone) -> str:
    return ctx.templates.env.get_template("components/event_row.html").render(
        item=item,
        display_timezone=display_timezone,
    )


# route: /events/stream
@router.get("/events/stream")
async def events_stream(request: Request):
    """SSE live event tail. Auth via session cookie; short-lived DB sessions only."""
    import asyncio
    import json as _json
    from starlette.responses import StreamingResponse
    from app.database import SessionLocal
    from app import event_stream as es
    from app.themes import get_display_timezone

    source_id_filter = request.query_params.get("source_id", "").strip()
    event_type_filter = request.query_params.get("event_type_id", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    after_raw = request.query_params.get("after") or request.headers.get("Last-Event-ID") or "0"
    try:
        after = int(after_raw)
    except ValueError:
        after = 0

    def _matches(event: Event) -> bool:
        if source_id_filter:
            try:
                if event.source_id != int(source_id_filter):
                    return False
            except ValueError:
                return False
        if event_type_filter:
            try:
                if event.event_type_id != int(event_type_filter):
                    return False
            except ValueError:
                return False
        if status_filter and event.status != status_filter:
            return False
        return True

    def _fetch_rows(ids: list[int]) -> list[tuple[int, str]]:
        if not ids:
            return []
        db = SessionLocal()
        try:
            display_timezone = get_display_timezone(db)
            rows = (
                db.query(Event)
                .filter(Event.id.in_(ids))
                .order_by(Event.id.asc())
                .all()
            )
            out = []
            for item in rows:
                if not _matches(item):
                    continue
                out.append((item.id, _render_event_row(item, display_timezone)))
            return out
        finally:
            db.close()

    queue = await es.subscribe()
    # Catch-up: register first, then query so nothing between is lost; dedupe by id.
    seen: set[int] = set()

    async def gen():
        try:
            db = SessionLocal()
            try:
                q = db.query(Event).filter(Event.id > after).order_by(Event.id.asc())
                if source_id_filter:
                    q = q.filter(Event.source_id == int(source_id_filter))
                if event_type_filter:
                    q = q.filter(Event.event_type_id == int(event_type_filter))
                if status_filter:
                    q = q.filter(Event.status == status_filter)
                catchup = q.limit(50).all()
                display_timezone = get_display_timezone(db)
                for item in catchup:
                    seen.add(item.id)
                    html = _render_event_row(item, display_timezone)
                    yield f"id: {item.id}\nevent: event\ndata: {_json.dumps(html)}\n\n"
            finally:
                db.close()

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event_id = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event_id in seen:
                    continue
                seen.add(event_id)
                for eid, html in _fetch_rows([event_id]):
                    yield f"id: {eid}\nevent: event\ndata: {_json.dumps(html)}\n\n"
        finally:
            await es.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Event detail ────────────────────────────────────────────────────────────

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
    from pathlib import Path
    from app.event_store import MAX_EVENTS_PER_SOURCE
    from app.database import SQLALCHEMY_DATABASE_URL

    total_sources = db.query(Source).count()
    enabled_sources = db.query(Source).filter(Source.enabled == True).count()  # noqa: E712

    retained_events = db.query(Event).count()
    events_pending = db.query(Event).filter(Event.status == "pending").count()
    events_failed = db.query(Event).filter(Event.status == "failed").count()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    events_failed_last_hour = db.query(Event).filter(
        Event.status == "failed",
        Event.timestamp >= cutoff,
    ).count()

    webhook_accepted = db.query(AuditLog).filter(
        AuditLog.action == "webhook.accepted"
    ).count()
    webhook_accepted_last_hour = db.query(AuditLog).filter(
        AuditLog.action == "webhook.accepted",
        AuditLog.timestamp >= cutoff,
    ).count()

    # SQLite on-disk size (main + wal + shm)
    db_size_bytes = 0
    if SQLALCHEMY_DATABASE_URL.startswith("sqlite:///"):
        db_path = Path(SQLALCHEMY_DATABASE_URL.removeprefix("sqlite:///"))
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix) if suffix else db_path
            try:
                if p.is_file():
                    db_size_bytes += p.stat().st_size
            except OSError:
                pass

    now = datetime.now(timezone.utc)
    schedules = db.query(PollingSchedule).all()
    schedule_info = []
    for s in schedules:
        overdue_seconds = None
        if s.enabled and s.next_run_at:
            nxt = s.next_run_at
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            overdue_seconds = max(0, int((now - nxt).total_seconds()))
        schedule_info.append({
            "name": s.name,
            "source_id": s.source_id,
            "last_run": s.last_run_at,
            "next_run": s.next_run_at,
            "enabled": s.enabled,
            "success_count": s.success_count or 0,
            "failure_count": s.failure_count or 0,
            "last_error": s.last_error or "",
            "overdue_seconds": overdue_seconds,
        })

    from app.widgets import source_age_status

    source_health = []
    all_sources = db.query(Source).all()
    for src in all_sources:
        last_seen = src.last_seen_at
        source_health.append({
            "id": src.id, "name": src.name, "slug": src.slug,
            "source_type": src.source_type or "webhook",
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
            "events_failed": events_failed,
            "events_failed_last_hour": events_failed_last_hour,
            "db_size_bytes": db_size_bytes,
            "max_events_per_source": MAX_EVENTS_PER_SOURCE,
            "schedules": schedule_info,
            "source_health": source_health,
        }
    )


# ── Webhook Ingress ─────────────────────────────────────────────────────────


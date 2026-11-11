"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from urllib.parse import urlencode
import json

from app.database import get_db
from app.models import (
    DashboardLayout,
    PushSubscription,
    Field,
    Source,
    EventTypeRecord,
    PollingSchedule,
)
from app.widgets import (
    fetch_widget_data,
    fields_snapshot,
    get_widget_types,
    validate_widget_bindings,
    _render_widget_text,
)
from app.dashboard_layout import (
    GRID_CELL_HEIGHT, GRID_COLUMN_LIVE_MAX, GRID_COLUMN_WIDTH, GRID_COLUMNS,
    GRID_MARGIN, GRID_STACK_BELOW, GRID_STACK_FIXED_H,
    MAIN_NAME, MAIN_SLUG,
    find_widget, get_layout, grid_stack_column_css, layout_json,
    list_named_pages, merge_geometry, normalize_for_save, normalize_page_slug,
    normalize_widgets, parse_layout_config, slugify_page_name, unique_page_slug,
    validate_page_name,
)

from app import webctx as ctx

router = APIRouter()

# Widget HTML must never be reused from browser/proxy/HTMX history caches.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# route: /api/push/vapid-public-key
@router.get("/api/push/vapid-public-key")
async def push_vapid_public_key():
    from app.webpush_util import vapid_config
    cfg = vapid_config()
    if not cfg:
        return JSONResponse(
            {"error": "Browser notifications aren’t set up on this server"},
            status_code=503,
        )
    return {"public_key": cfg["public_key"]}


# route: /api/push/subscribe
@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, db: Session = Depends(get_db)):
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return JSONResponse({"error": "Couldn’t save notification subscription"}, status_code=400)

    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if existing:
        existing.user_id = user.id
        existing.p256dh = p256dh
        existing.auth = auth
    else:
        db.add(PushSubscription(
            user_id=user.id, endpoint=endpoint, p256dh=p256dh, auth=auth,
        ))
    db.commit()
    return {"ok": True}


# route: /api/push/subscribe
@router.delete("/api/push/subscribe")
async def push_unsubscribe(request: Request, db: Session = Depends(get_db)):
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        return JSONResponse({"error": "Missing subscription details"}, status_code=400)
    sub = (
        db.query(PushSubscription)
        .filter(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == user.id,
        )
        .first()
    )
    if sub:
        db.delete(sub)
        db.commit()
    return {"ok": True}


# ── Dashboard pages ─────────────────────────────────────────────────────────

def _page_slug_from_request(request: Request) -> str:
    return normalize_page_slug(request.query_params.get("page"))


def _page_slug_from_body(body: dict | None) -> str:
    if not isinstance(body, dict):
        return MAIN_SLUG
    return normalize_page_slug(body.get("page"))


def _config_dashboard_url(slug: str = MAIN_SLUG, *, success: str | None = None, error: str | None = None) -> str:
    params: dict[str, str] = {}
    if slug:
        params["page"] = slug
    if success is not None:
        params["success"] = success
    if error is not None:
        params["error"] = error
    qs = urlencode(params)
    return f"/config/dashboard?{qs}" if qs else "/config/dashboard"


def _has_calendar_feed(db: Session) -> bool:
    from app.source_templates import has_calendar_feed
    return has_calendar_feed(db)


def _dashboard_pages_ctx(db: Session) -> list[dict]:
    return [
        {"name": p.name, "slug": p.slug, "kind": p.kind or "dashboard"}
        for p in list_named_pages(db)
    ]


def _load_widgets(db: Session, slug: str = MAIN_SLUG) -> list[dict] | None:
    """Load widgets for a page. None if named page missing."""
    layout = get_layout(db, slug)
    if not layout:
        return None
    widgets = parse_layout_config(layout.layout_config)["widgets"]
    widgets, changed = normalize_widgets(widgets)
    if changed:
        layout.layout_config = layout_json(widgets)
        db.commit()
    return widgets


def _render_calendar(request: Request, db: Session, user, layout):
    from datetime import datetime, timedelta
    from app.calendar_sync import current_focus, display_zone, load_week, week_bounds
    from app.kanban import mode_label

    zone = display_zone(db)
    raw = (request.query_params.get("week") or "").strip()
    try:
        anchor = datetime.strptime(raw, "%Y-%m-%d").date() if raw else datetime.now(zone).date()
    except ValueError:
        anchor = datetime.now(zone).date()
    start, _end = week_bounds(anchor, zone)
    items = load_week(db, anchor, zone)
    days = []
    for offset in range(7):
        moment = start + timedelta(days=offset)
        day = moment.date()
        day_items = []
        for item in items:
            from datetime import timezone
            starts = item.starts_at
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=timezone.utc)
            if starts.astimezone(zone).date() != day:
                continue
            day_items.append(item)
        days.append({
            "date": day,
            "weekday": day.strftime("%a"),
            "number": day.day,
            "items": day_items,
            "today": day == datetime.now(zone).date(),
        })
    focus = current_focus(db)
    prev_week = (start.date() - timedelta(days=7)).isoformat()
    next_week = (start.date() + timedelta(days=7)).isoformat()
    return ctx.templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "user": user,
            "page_slug": layout.slug,
            "page_name": layout.name,
            "page_kind": "calendar",
            "dashboard_pages": _dashboard_pages_ctx(db),
            "days": days,
            "focus": focus,
            "focus_label": mode_label(focus.mode) if focus and focus.mode else "",
            "prev_week": prev_week,
            "next_week": next_week,
            "week_label": f"{start.strftime('%b')} {start.day}",
            "has_events": any(day["items"] for day in days),
        },
        headers=_NO_STORE,
    )


def _render_kanban(request: Request, db: Session, user, layout):
    from app.calendar_sync import current_focus
    from app.kanban import ensure_columns, mode_label

    from app.models import KanbanColumn
    had_columns = (
        db.query(KanbanColumn).filter(KanbanColumn.layout_id == layout.id).count() > 0
    )
    columns = ensure_columns(db, layout)
    if not had_columns:
        db.commit()
    focus = current_focus(db)
    return ctx.templates.TemplateResponse(
        request,
        "kanban.html",
        _kanban_ctx(db, user, layout, columns, focus, mode_label),
        headers=_NO_STORE,
    )


def _kanban_ctx(db, user, layout, columns, focus, mode_label):
    from app.kanban import FOCUS_MODES
    return {
        "user": user,
        "page_slug": layout.slug,
        "page_name": layout.name,
        "page_kind": "kanban",
        "dashboard_pages": _dashboard_pages_ctx(db),
        "columns": columns,
        "focus": focus,
        "focus_label": mode_label(focus.mode) if focus and focus.mode else "",
        "focus_modes": FOCUS_MODES,
    }


# route: /
@router.get("/")
async def root(request: Request, db: Session = Depends(get_db)):
    user = ctx._get_user(request, db)
    if not user:
        return ctx.templates.TemplateResponse(request, "base.html", {})
    slug = _page_slug_from_request(request)
    layout = get_layout(db, slug)
    if layout and (layout.kind or "dashboard") != "dashboard":
        if layout.kind == "calendar":
            return _render_calendar(request, db, user, layout)
        if layout.kind == "kanban":
            return _render_kanban(request, db, user, layout)
        return HTMLResponse("Unknown page", status_code=404, headers=_NO_STORE)
    widgets = _load_widgets(db, slug)
    if widgets is None:
        return HTMLResponse("Dashboard page not found", status_code=404, headers=_NO_STORE)
    layout = get_layout(db, slug)
    page_name = (layout.name if layout else MAIN_NAME) or MAIN_NAME
    # ponytail: titles only re-render on full page load (HTMX refreshes card body only).
    snap = fields_snapshot(db)
    for w in widgets:
        w["title"] = _render_widget_text(w.get("title") or "", snap)
    widget_data = {}
    for w in widgets:
        wtype = w.get("type", "")
        wid = w.get("id") or ""
        disp = w.get("display") or ""
        widget_data[wid] = fetch_widget_data(
            wtype, db, widget_config=w.get("config") or {}, display=disp,
        )
    return ctx.templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "widgets": widgets,
            "widget_data": widget_data,
            "page_slug": slug,
            "page_name": page_name,
            "dashboard_pages": _dashboard_pages_ctx(db),
            "grid_columns": GRID_COLUMNS,
            "grid_column_width": GRID_COLUMN_WIDTH,
            "grid_column_live_max": GRID_COLUMN_LIVE_MAX,
            "grid_cell_height": GRID_CELL_HEIGHT,
            "grid_margin": GRID_MARGIN,
            "grid_stack_below": GRID_STACK_BELOW,
            "grid_stack_fixed_h": GRID_STACK_FIXED_H,
            "grid_column_css": grid_stack_column_css(GRID_COLUMN_LIVE_MAX),
        },
        headers=_NO_STORE,
    )


# route: /widgets/{widget_type}
@router.get("/widgets/{widget_type}")
async def widget_partial(request: Request, widget_type: str, db: Session = Depends(get_db)):
    """HTMX partial: re-render a single widget's content body."""
    user = ctx._get_user(request, db)
    if not user:
        return HTMLResponse("", status_code=401, headers=_NO_STORE)

    widget_id = (request.query_params.get("id") or "").strip() or None
    try:
        index = int(request.query_params.get("index") or 0) or None
    except (TypeError, ValueError):
        index = None

    slug = _page_slug_from_request(request)
    widgets = _load_widgets(db, slug)
    if widgets is None:
        return HTMLResponse("", status_code=404, headers=_NO_STORE)
    widget = find_widget(widgets, widget_id=widget_id, index=index)
    config = (widget.get("config") or {}) if widget else {}
    display = (widget.get("display") if widget else None) or ""
    canvas_id = widget_id or f"{widget_type}-{index or 0}"

    wdata = fetch_widget_data(
        widget_type, db, widget_config=config, display=display,
    ) or {}
    try:
        from app.themes import appearance_context
        html = ctx.templates.env.get_template(f"widgets/{widget_type}_content.html").render(
            wdata=wdata, request=request, widget_id=canvas_id,
            widget_config=config, display=display or wdata.get("display"),
            **appearance_context(db),
        )
    except Exception:
        return HTMLResponse(
            '<p class="text-muted">Unknown widget</p>',
            headers=_NO_STORE,
        )
    return HTMLResponse(html, headers=_NO_STORE)


# route: /api/dashboard/layout
@router.post("/api/dashboard/layout")
async def api_dashboard_layout(request: Request, db: Session = Depends(get_db)):
    """Merge widget geometry (x/y/w/h) by id into the page layout."""
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    updates = body.get("widgets") if isinstance(body, dict) else None
    if not isinstance(updates, list):
        return JSONResponse({"error": "Widgets array required"}, status_code=400)

    slug = _page_slug_from_body(body if isinstance(body, dict) else None)
    layout = get_layout(db, slug)
    if not layout:
        return JSONResponse({"error": "No layout"}, status_code=404)
    widgets = parse_layout_config(layout.layout_config)["widgets"]
    widgets, _ = normalize_widgets(widgets)
    widgets = merge_geometry(widgets, updates)
    layout.layout_config = layout_json(widgets)
    db.commit()
    return {"ok": True}


# route: /api/dashboard/trigger
@router.post("/api/dashboard/trigger")
async def api_dashboard_trigger(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Fire a webhook event type or run a poll source once (authenticated)."""
    from app.actions import _template_mapping
    from app.ingest import ingest_manual_events
    from app.pollers import run_schedule
    from app.widgets import fields_snapshot

    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    kind = (body.get("kind") or "").strip()
    try:
        source_id = int(body.get("source_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Source id required"}, status_code=400)

    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return JSONResponse({"error": "Source not found"}, status_code=404)
    if not source.enabled:
        return JSONResponse({"error": "Source is disabled"}, status_code=400)

    if kind == "poll":
        if source.source_type != "poll":
            return JSONResponse({"error": "Not a poll source"}, status_code=400)
        schedule = (
            db.query(PollingSchedule)
            .filter(PollingSchedule.source_id == source.id)
            .first()
        )
        if not schedule or not schedule.enabled:
            return JSONResponse({"error": "No enabled schedule"}, status_code=400)
        ok = run_schedule(schedule.id)
        ctx._audit_log(
            db, request, "trigger.fire",
            resource_type="source", resource_id=source.id,
            details={"kind": "poll", "ok": ok},
        )
        if not ok:
            return JSONResponse({"error": "Poll failed", "ok": False}, status_code=502)
        return {"ok": True, "kind": "poll"}

    if kind == "webhook":
        if source.source_type != "webhook":
            return JSONResponse({"error": "Not a webhook source"}, status_code=400)
        try:
            et_id = int(body.get("event_type_id"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Event type id required"}, status_code=400)
        event_type = (
            db.query(EventTypeRecord)
            .filter(EventTypeRecord.id == et_id, EventTypeRecord.source_id == source.id)
            .first()
        )
        if not event_type:
            return JSONResponse({"error": "Event type not found"}, status_code=404)
        if not event_type.enabled:
            return JSONResponse({"error": "Event type is disabled"}, status_code=400)

        raw_payload = body.get("payload")
        if raw_payload is None:
            raw_payload = {}
        if not isinstance(raw_payload, dict):
            return JSONResponse({"error": "Payload must be a JSON object"}, status_code=400)
        try:
            encoded = json.dumps(raw_payload)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Payload must be a JSON object"}, status_code=400)
        if len(encoded.encode("utf-8")) > ctx._WEBHOOK_MAX_BODY:
            return JSONResponse({"error": "Payload too large"}, status_code=400)

        data = _template_mapping(raw_payload, fields_snapshot(db))
        if not isinstance(data, dict):
            return JSONResponse({"error": "Payload must be a JSON object"}, status_code=400)

        trigger_meta = {
            "origin": "dashboard",
            "user": user.username,
            "event_type": event_type.name,
        }
        events = ingest_manual_events(
            db, source, event_type, data, meta=trigger_meta,
        )
        event = events[0]
        always_event = events[1] if len(events) > 1 else None

        details = {
            "kind": "webhook",
            "event_id": event.id,
            "event_type": event_type.name,
            "payload_keys": sorted(data.keys()),
        }
        if always_event:
            details["always_event_id"] = always_event.id
        ctx._audit_log(
            db, request, "trigger.fire",
            resource_type="source", resource_id=source.id,
            details=details,
        )

        for ev in events:
            background_tasks.add_task(ctx._process_webhook_event, ev.id)
        body_out = {"ok": True, "kind": "webhook", "event_id": event.id}
        if always_event:
            body_out["always_event_id"] = always_event.id
        return body_out

    return JSONResponse({"error": "Unknown trigger kind"}, status_code=400)


NOTES_TEXT_MAX = 50_000


# route: /api/dashboard/notes
@router.post("/api/dashboard/notes")
async def api_dashboard_notes(request: Request, db: Session = Depends(get_db)):
    """Persist notes widget body text into page layout config."""
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    wid = (body.get("id") or "").strip()
    if not wid:
        return JSONResponse({"error": "Widget id required"}, status_code=400)
    text = body.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > NOTES_TEXT_MAX:
        return JSONResponse({"error": "Notes text too long"}, status_code=400)

    slug = _page_slug_from_body(body)
    layout = get_layout(db, slug)
    if not layout:
        return JSONResponse({"error": "No layout"}, status_code=404)
    widgets = parse_layout_config(layout.layout_config)["widgets"]
    widgets, _ = normalize_widgets(widgets)
    widget = find_widget(widgets, widget_id=wid)
    if not widget or widget.get("type") != "notes":
        return JSONResponse({"error": "Notes widget not found"}, status_code=404)
    cfg = dict(widget.get("config") or {})
    cfg["text"] = text
    widget["config"] = cfg
    layout.layout_config = layout_json(widgets)
    db.commit()
    return {"ok": True}


# ── Config: Dashboard ───────────────────────────────────────────────────────


# route: /config/dashboard
@router.get("/config/dashboard")
async def config_dashboard(request: Request, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    slug = _page_slug_from_request(request)
    widgets = _load_widgets(db, slug)
    if widgets is None:
        return RedirectResponse(
            url=_config_dashboard_url(error="Dashboard page not found"),
            status_code=303,
        )
    layout = get_layout(db, slug)
    return ctx.templates.TemplateResponse(
        request, "config/dashboard.html",
        _dashboard_editor_context(
            db,
            current_widgets=widgets,
            page_slug=slug,
            page_name=(layout.name if layout else MAIN_NAME) or MAIN_NAME,
            page_kind=(layout.kind if layout else None) or "dashboard",
            success=success,
            error=error,
        ),
    )


def _dashboard_editor_context(
    db: Session,
    *,
    current_widgets: list,
    page_slug: str = MAIN_SLUG,
    page_name: str = MAIN_NAME,
    page_kind: str = "dashboard",
    success: str | None = None,
    error: str | None = None,
) -> dict:
    available_widgets = [
        {
            "type": w["type"],
            "title": w["title"],
            "displays": w["displays"],
            "default_display": w["default_display"],
            "binding": w.get("binding"),
        }
        for w in get_widget_types()
    ]
    from app.widgets import trigger_targets as _trigger_targets
    return {
        "active": "dashboard",
        "current_widgets": current_widgets,
        "available_widgets": available_widgets,
        "fields": db.query(Field).order_by(Field.name).all(),
        "trigger_targets": _trigger_targets(db),
        "page_slug": page_slug,
        "page_name": page_name,
        "page_kind": page_kind if page_kind in ("dashboard", "calendar", "kanban") else "dashboard",
        "dashboard_pages": _dashboard_pages_ctx(db),
        "has_calendar_feed": _has_calendar_feed(db),
        "success": success,
        "error": error,
    }


# route: /config/dashboard
@router.post("/config/dashboard")
async def save_dashboard(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    slug = normalize_page_slug(form.get("page"))
    widgets_raw = (form.get("widgets") or "[]").strip()

    try:
        widgets = json.loads(widgets_raw) if widgets_raw else []
    except json.JSONDecodeError:
        return RedirectResponse(
            url=_config_dashboard_url(slug, error="Couldn’t read the dashboard settings"),
            status_code=303,
        )

    layout = get_layout(db, slug)
    if not layout:
        return RedirectResponse(
            url=_config_dashboard_url(error="Dashboard page not found"),
            status_code=303,
        )

    if (layout.kind or "dashboard") != "dashboard":
        cleaned, name_err = validate_page_name(form.get("page_name") or layout.name)
        if name_err:
            return RedirectResponse(
                url=_config_dashboard_url(slug, error=name_err),
                status_code=303,
            )
        layout.name = cleaned
        db.commit()
        return RedirectResponse(
            url=_config_dashboard_url(slug, success="Page saved"),
            status_code=303,
        )

    # Preserve geometry from existing layout when the form omits x/y/w/h
    existing = {}
    for w in parse_layout_config(layout.layout_config)["widgets"]:
        if w.get("id"):
            existing[w["id"]] = w
    for w in widgets:
        if isinstance(w, dict) and w.get("id") and w["id"] in existing:
            prev = existing[w["id"]]
            for key in ("x", "y", "w", "h"):
                if w.get(key) is None and prev.get(key) is not None:
                    w[key] = prev[key]
            # Notes body is edited on the dashboard; config form omits it.
            if w.get("type") == "notes":
                prev_cfg = prev.get("config") if isinstance(prev.get("config"), dict) else {}
                cfg = w.get("config") if isinstance(w.get("config"), dict) else {}
                if "text" not in cfg and "text" in prev_cfg:
                    cfg = dict(cfg)
                    cfg["text"] = prev_cfg["text"]
                    w["config"] = cfg

    widgets = normalize_for_save(widgets)
    err = validate_widget_bindings(db, widgets)
    if err:
        return ctx.templates.TemplateResponse(
            request, "config/dashboard.html",
            _dashboard_editor_context(
                db,
                current_widgets=widgets,
                page_slug=slug,
                page_name=layout.name or MAIN_NAME,
                error=err,
            ),
            status_code=400,
        )

    if slug:
        cleaned, name_err = validate_page_name(form.get("page_name") or layout.name)
        if name_err:
            return ctx.templates.TemplateResponse(
                request, "config/dashboard.html",
                _dashboard_editor_context(
                    db,
                    current_widgets=widgets,
                    page_slug=slug,
                    page_name=form.get("page_name") or layout.name,
                    error=name_err,
                ),
                status_code=400,
            )
        new_slug = unique_page_slug(
            db, slugify_page_name(cleaned), exclude_id=layout.id,
        )
        layout.name = cleaned
        layout.slug = new_slug
        slug = new_slug

    layout.layout_config = layout_json(widgets)
    db.commit()
    return RedirectResponse(
        url=_config_dashboard_url(slug, success="Dashboard layout saved"),
        status_code=303,
    )


# route: /config/dashboard/add-page
@router.post("/config/dashboard/add-page")
async def add_dashboard_page(request: Request, db: Session = Depends(get_db)):
    from app.dashboard_layout import PAGE_KINDS
    from app.kanban import ensure_columns

    form = await request.form()
    kind = (form.get("kind") or "dashboard").strip()
    if kind not in PAGE_KINDS:
        kind = "dashboard"
    fallback = {"dashboard": "New Page", "calendar": "Calendar", "kanban": "Kanban"}[kind]
    cleaned, err = validate_page_name((form.get("page_name") or "").strip() or fallback)
    if err:
        return RedirectResponse(
            url=_config_dashboard_url(error=err),
            status_code=303,
        )
    base = slugify_page_name(cleaned)
    slug = unique_page_slug(db, base)
    layout = DashboardLayout(
        name=cleaned,
        slug=slug,
        kind=kind,
        layout_config=layout_json([]),
    )
    db.add(layout)
    db.flush()
    schedule = None
    if kind == "kanban":
        ensure_columns(db, layout)
    if kind in ("calendar", "kanban"):
        from app.source_templates import ensure_calendar_pipeline

        kanban_slug = slug if kind == "kanban" else None
        if kanban_slug is None:
            boards = [p for p in list_named_pages(db) if (p.kind or "") == "kanban"]
            if len(boards) == 1:
                kanban_slug = boards[0].slug
        try:
            schedule = ensure_calendar_pipeline(
                db,
                ics_url=(form.get("ics_url") or "").strip(),
                kanban_slug=kanban_slug,
            )
        except ValueError as exc:
            db.rollback()
            return RedirectResponse(
                url=_config_dashboard_url(error=str(exc)),
                status_code=303,
            )
    db.commit()
    if schedule is not None:
        from app.scheduler import add_or_update_job
        add_or_update_job(schedule)
    message = "Page created"
    if kind in ("calendar", "kanban"):
        message = "Page created. Calendar feed and rules are ready."
    return RedirectResponse(
        url=_config_dashboard_url(slug, success=message),
        status_code=303,
    )


# route: /config/dashboard/delete-page
@router.post("/config/dashboard/delete-page")
async def delete_dashboard_page(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    slug = normalize_page_slug(form.get("page"))
    if not slug:
        return RedirectResponse(
            url=_config_dashboard_url(error="Main cannot be deleted"),
            status_code=303,
        )
    layout = get_layout(db, slug)
    if not layout:
        return RedirectResponse(
            url=_config_dashboard_url(error="Dashboard page not found"),
            status_code=303,
        )
    db.delete(layout)
    db.commit()
    return RedirectResponse(
        url=_config_dashboard_url(success="Page deleted"),
        status_code=303,
    )


def _kanban_layout(db: Session, slug: str):
    layout = get_layout(db, slug)
    if not layout or (layout.kind or "") != "kanban":
        return None
    return layout


def _kanban_reply(request: Request, db: Session, user, layout):
    from app.calendar_sync import current_focus
    from app.kanban import ensure_columns, mode_label

    columns = ensure_columns(db, layout)
    if ctx._is_htmx(request):
        return ctx.templates.TemplateResponse(
            request,
            "kanban_board.html",
            _kanban_ctx(db, user, layout, columns, current_focus(db), mode_label),
            headers=_NO_STORE,
        )
    return RedirectResponse(url=f"/?page={layout.slug}", status_code=303)


# route: /kanban/card
@router.post("/kanban/card")
async def kanban_add_card(request: Request, db: Session = Depends(get_db)):
    from app.kanban import add_card, column_named, ensure_columns

    user = ctx._get_user(request, db)
    if not user:
        return HTMLResponse("", status_code=401)
    form = await request.form()
    layout = _kanban_layout(db, normalize_page_slug(form.get("page")))
    if layout is None:
        return HTMLResponse("Kanban page not found", status_code=404)
    columns = ensure_columns(db, layout)
    column = column_named(columns, "Next") or columns[0]
    add_card(db, column, form.get("title") or "", (form.get("mode") or "").strip())
    db.commit()
    return _kanban_reply(request, db, user, layout)


# route: /kanban/card/move
@router.post("/kanban/card/move")
async def kanban_move_card(request: Request, db: Session = Depends(get_db)):
    from app.kanban import emit_card_moved, move_card
    from app.models import KanbanCard, KanbanColumn

    user = ctx._get_user(request, db)
    if not user:
        return HTMLResponse("", status_code=401)
    form = await request.form()
    layout = _kanban_layout(db, normalize_page_slug(form.get("page")))
    if layout is None:
        return HTMLResponse("Kanban page not found", status_code=404)
    try:
        card_id = int(form.get("card_id") or 0)
        column_id = int(form.get("column_id") or 0)
    except (TypeError, ValueError):
        return HTMLResponse("Invalid card", status_code=400)
    card = (
        db.query(KanbanCard)
        .join(KanbanColumn)
        .filter(KanbanCard.id == card_id, KanbanColumn.layout_id == layout.id)
        .first()
    )
    column = (
        db.query(KanbanColumn)
        .filter(KanbanColumn.id == column_id, KanbanColumn.layout_id == layout.id)
        .first()
    )
    if card is None or column is None:
        return HTMLResponse("Card not found", status_code=404)
    moved = move_card(db, card, column)
    if moved:
        origin, dest = moved
        emit_card_moved(db, card, origin=origin, dest=dest)
    else:
        db.commit()
    return _kanban_reply(request, db, user, layout)


# route: /kanban/card/delete
@router.post("/kanban/card/delete")
async def kanban_delete_card(request: Request, db: Session = Depends(get_db)):
    from app.models import KanbanCard, KanbanColumn

    user = ctx._get_user(request, db)
    if not user:
        return HTMLResponse("", status_code=401)
    form = await request.form()
    layout = _kanban_layout(db, normalize_page_slug(form.get("page")))
    if layout is None:
        return HTMLResponse("Kanban page not found", status_code=404)
    try:
        card_id = int(form.get("card_id") or 0)
    except (TypeError, ValueError):
        return HTMLResponse("Invalid card", status_code=400)
    card = (
        db.query(KanbanCard)
        .join(KanbanColumn)
        .filter(KanbanCard.id == card_id, KanbanColumn.layout_id == layout.id)
        .first()
    )
    if card is not None:
        db.delete(card)
        db.commit()
    return _kanban_reply(request, db, user, layout)

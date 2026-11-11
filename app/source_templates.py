"""Full-stack pipeline templates for the “From template” picker.

Each entry is a recipe: poll source + Fields + rules/actions + dashboard
widgets. ``apply_source_template`` creates the whole stack in one step.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from sqlalchemy.orm import Session

from app.dashboard_layout import (
    DEFAULT_W,
    layout_json,
    new_widget_id,
    normalize_widgets,
    parse_layout_config,
    slugify_page_name,
    unique_page_slug,
)
from app.fields import default_field_state
from app.models import (
    ActionInstance,
    EventTypeRecord,
    Field,
    PollingSchedule,
    Rule,
    ScheduleType,
    Source,
)
from app.scheduler import add_or_update_job
from app.webctx import _slugify_name, _unique_field_slug

_FX_QUOTES = ("EUR", "GBP", "AUD", "CAD", "JPY")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

SOURCE_TEMPLATES: list[dict] = [
    {
        "slug": "fx_usd",
        "title": "USD forex rates",
        "summary": "Poll Frankfurter daily at 08:00 UTC; per-quote rate logbooks, multi-series chart, and current USD prices.",
        "creates": "Poll source, 5 rate logbooks, star-matched rules, multi-series + price board widgets",
        "source": {
            "name": "USD forex rates",
            "preferred_slug": "fx_usd",
            "description": "Frankfurter FX rates with base USD",
            "poll_category": "url",
            "handler_type": "http_get",
            "handler_url": (
                "https://api.frankfurter.dev/v2/rates"
                "?base=USD&quotes=EUR,GBP,AUD,CAD,JPY"
            ),
            "schedule_type": "cron",
            "cron_expression": "0 8 * * *",
        },
        "fields": [
            {
                "key": f"r_{q}",
                "name": f"USD/{q} rate",
                "preferred_slug": f"fx_usd_{q.lower()}",
                "field_type": "logbook",
                "config": {"max_entries": 100},
            }
            for q in _FX_QUOTES
        ],
        "rules": [
            {
                "description": f"Log {q} rate",
                "event_type": "on_success",
                "conditions": {"value.*.quote": q},
                "actions": [
                    {
                        "action_type": "field_push",
                        "field_key": f"r_{q}",
                        "config": {"value_key": "value.*.rate"},
                    },
                ],
            }
            for q in _FX_QUOTES
        ],
        "widgets": [
            {
                "type": "series",
                "display": "line",
                "title": "FX rates (per USD)",
                "w": 36,
                "h": 4,
                "config": {
                    "style": "multi",
                    "range_mode": "entries",
                    "range_entries": 48,
                    "sources": [
                        {"field_slug": f"{{r_{q}}}.value", "label": q}
                        for q in _FX_QUOTES
                    ],
                },
            },
            {
                "type": "display",
                "display": "board",
                "title": "Current USD prices",
                "w": 36,
                "h": 3,
                "config": {
                    "cell_kind": "kv_text",
                    "style": "callout",
                    "cells": [
                        {
                            "template": (
                                f"USD/{q} {{{{ round(1/{{r_{q}}}.value, 4) }}}}"
                            ),
                        }
                        for q in _FX_QUOTES
                    ],
                },
            },
        ],
    },
    {
        "slug": "github_status",
        "title": "GitHub Status",
        "summary": "Poll GitHub Statuspage; LED toggle when the platform is healthy.",
        "creates": "Poll source, toggle Field, OK/degraded rules, LED widget",
        "source": {
            "name": "GitHub Status",
            "preferred_slug": "github_status",
            "description": "GitHub Statuspage summary",
            "poll_category": "url",
            "handler_type": "http_get",
            "handler_url": "https://www.githubstatus.com/api/v2/summary.json",
            "interval_seconds": 300,
        },
        "fields": [
            {
                "key": "ok",
                "name": "GitHub OK",
                "preferred_slug": "github_ok",
                "field_type": "toggle",
                "config": {},
            },
        ],
        "rules": [
            {
                "description": "GitHub healthy",
                "event_type": "on_success",
                "conditions": {"status.indicator": "none"},
                "actions": [
                    {
                        "action_type": "field_push",
                        "field_key": "ok",
                        "config": {"value": True},
                    },
                ],
            },
            {
                "description": "GitHub degraded",
                "event_type": "on_success",
                "conditions": {"status.indicator": {"not": "none"}},
                "actions": [
                    {
                        "action_type": "field_push",
                        "field_key": "ok",
                        "config": {"value": False},
                    },
                ],
            },
        ],
        "widgets": [
            {
                "type": "display",
                "display": "toggle",
                "title": "GitHub Status",
                "w": 12,
                "h": 2,
                "config": {
                    "field_slug": "{ok}",
                    "style": "led",
                },
            },
        ],
    },
    {
        "slug": "iss_now",
        "title": "ISS position",
        "summary": "Poll Open Notify for the ISS location; show lat/lon on the dashboard.",
        "creates": "Poll source, data Field, update rule, display widget",
        "source": {
            "name": "ISS position",
            "preferred_slug": "iss_now",
            "description": "Open Notify ISS now",
            "poll_category": "url",
            "handler_type": "http_get",
            "handler_url": "http://api.open-notify.org/iss-now.json",
            "interval_seconds": 300,
        },
        "fields": [
            {
                "key": "iss",
                "name": "ISS position",
                "preferred_slug": "iss_now",
                "field_type": "data",
                "config": {},
            },
        ],
        "rules": [
            {
                "description": "Store ISS position",
                "event_type": "on_success",
                "conditions": {},
                "actions": [
                    {"action_type": "field_push", "field_key": "iss", "config": {}},
                ],
            },
        ],
        "widgets": [
            {
                "type": "display",
                "display": "kv_text",
                "title": "ISS now",
                "w": 18,
                "h": 2,
                "config": {
                    "style": "callout",
                    "template": (
                        "ISS lat {{ {iss}.iss_position.latitude }}"
                        " · lon {{ {iss}.iss_position.longitude }}"
                    ),
                },
            },
        ],
    },
    {
        "slug": "calendar_kanban",
        "title": "Calendar → kanban",
        "summary": "Read a secret ICS URL. Events with a focus mode become cards. The current block is stored on two text Fields.",
        "creates": "Calendar page, kanban page, ICS poll, focus Fields, card rules, focus widget",
        "inputs": [
            {
                "name": "ics_url",
                "label": "Secret ICS URL",
                "placeholder": "https://…/basic.ics",
            },
        ],
        "source": {
            "name": "Calendar",
            "preferred_slug": "calendar_feed",
            "description": "Read-only ICS feed. Edit events in the calendar that publishes this URL.",
            "poll_category": "external",
            "handler_type": "ics_calendar",
            "handler_url": "",
            "url_field": "ics_url",
            "interval_seconds": 900,
        },
        "pages": [
            {"key": "calendar", "name": "Calendar", "kind": "calendar"},
            {"key": "work", "name": "Work", "kind": "kanban"},
        ],
        "fields": [
            {
                "key": "focus_mode",
                "name": "Focus mode",
                "preferred_slug": "focus_mode",
                "field_type": "text",
                "config": {},
            },
            {
                "key": "focus_title",
                "name": "Focus title",
                "preferred_slug": "focus_title",
                "field_type": "text",
                "config": {},
            },
        ],
        "rules": [
            {
                "description": "Card for a focus block",
                "event_type": "item.upsert",
                "conditions": {"mode": {"not": ""}},
                "actions": [
                    {
                        "action_type": "kanban_push",
                        "page_key": "work",
                        "config": {
                            "column": "Next",
                            "title": "{{ title }}",
                            "mode": "{{ mode }}",
                            "key": "{{ uid }}-{{ occurrence }}",
                        },
                    },
                ],
            },
            {
                "description": "Drop a Next card that left the feed",
                "event_type": "item.removed",
                "conditions": {},
                "actions": [
                    {
                        "action_type": "kanban_push",
                        "page_key": "work",
                        "config": {
                            "column": "Next",
                            "remove": True,
                            "title": "",
                            "mode": "",
                            "key": "{{ uid }}-{{ occurrence }}",
                        },
                    },
                ],
            },
            {
                "description": "Remember the current focus",
                "event_type": "focus.changed",
                "conditions": {},
                "actions": [
                    {
                        "action_type": "field_push",
                        "field_key": "focus_mode",
                        "config": {"value": "{{ mode }}"},
                    },
                    {
                        "action_type": "field_push",
                        "field_key": "focus_title",
                        "config": {"value": "{{ title }}"},
                    },
                ],
            },
        ],
        "widgets": [
            {
                "type": "display",
                "display": "kv_text",
                "title": "Focus",
                "w": 18,
                "h": 2,
                "config": {
                    "style": "callout",
                    "template": "{{ {focus_mode}.value }} — {{ {focus_title}.value }}",
                },
            },
        ],
    },
]


def get_source_template(slug: str) -> dict | None:
    key = (slug or "").strip()
    for t in SOURCE_TEMPLATES:
        if t["slug"] == key:
            return t
    return None


def list_source_templates() -> list[dict]:
    return list(SOURCE_TEMPLATES)


def _unique_preferred_slug(db: Session, model, preferred: str) -> str:
    """Keep preferred slug when free; otherwise append _2, _3, …"""
    base = _slugify_name(preferred) or "item"
    if model is Field:
        return _unique_field_slug(db, base.replace("_", " "))
    # Source: prefer exact base then uniquify like webctx
    slug = base
    n = 2
    while db.query(Source).filter(Source.slug == slug).first():
        slug = f"{base}_{n}"
        n += 1
    return slug


def _unique_field_name(db: Session, name: str) -> str:
    base = (name or "").strip() or "Field"
    candidate = base
    n = 2
    while db.query(Field).filter(Field.name == candidate).first():
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def _ensure_event_type(db: Session, source_id: int, name: str, description: str) -> EventTypeRecord:
    from app.pipeline import normalize_event_type

    want = normalize_event_type(name)
    existing = (
        db.query(EventTypeRecord)
        .filter(EventTypeRecord.source_id == source_id)
        .all()
    )
    for et in existing:
        if normalize_event_type(et.name) == want:
            return et
    et = EventTypeRecord(source_id=source_id, name=want, description=description)
    db.add(et)
    db.flush()
    return et


def _resolve_placeholders(obj: Any, slugs: dict[str, str]) -> Any:
    """Replace ``{field_key}`` with resolved Field slugs in strings / nested structures."""
    if isinstance(obj, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            return slugs.get(key, m.group(0))

        return _PLACEHOLDER_RE.sub(repl, obj)
    if isinstance(obj, list):
        return [_resolve_placeholders(x, slugs) for x in obj]
    if isinstance(obj, dict):
        return {k: _resolve_placeholders(v, slugs) for k, v in obj.items()}
    return obj


def _append_widgets(db: Session, widget_defs: list[dict]) -> list[str]:
    """Append widgets below existing Main layout; return new widget ids."""
    from app.dashboard_layout import ensure_main_layout

    layout = ensure_main_layout(db)
    existing = parse_layout_config(layout.layout_config)["widgets"]
    y = 0
    for w in existing:
        try:
            y = max(y, int(w.get("y") or 0) + int(w.get("h") or 1))
        except (TypeError, ValueError):
            continue

    added: list[str] = []
    new_widgets = list(existing)
    for raw in widget_defs:
        item = copy.deepcopy(raw)
        wid = new_widget_id()
        item["id"] = wid
        item.setdefault("x", 0)
        item["y"] = y
        item.setdefault("w", DEFAULT_W)
        item.setdefault("h", 3)
        item.setdefault("show_title", True)
        if not isinstance(item.get("config"), dict):
            item["config"] = {}
        y += int(item["h"])
        new_widgets.append(item)
        added.append(wid)

    normalized, _ = normalize_widgets(new_widgets)
    payload = layout_json(normalized)
    layout.layout_config = payload
    db.flush()
    return added


def apply_source_template(db: Session, slug: str, *, values: dict | None = None) -> dict:
    """Create source + fields + rules/actions + widgets for a template slug.

    Returns a summary dict. Raises ``ValueError`` if the slug is unknown.
    """
    tmpl = get_source_template(slug)
    if not tmpl:
        raise ValueError(f"Unknown template '{slug}'")
    values = values or {}

    src_spec = tmpl["source"]
    name = src_spec["name"]
    preferred = src_spec.get("preferred_slug") or name
    source_slug = _unique_preferred_slug(db, Source, preferred)
    handler_url = src_spec.get("handler_url") or ""
    url_field = src_spec.get("url_field")
    if url_field:
        handler_url = (values.get(url_field) or "").strip()
        if not handler_url.startswith(("http://", "https://")):
            raise ValueError("Calendar URL must start with http:// or https://")

    source = Source(
        name=name,
        slug=source_slug,
        source_type="poll",
        description=src_spec.get("description") or "",
        config={"poll_category": src_spec.get("poll_category") or "url"},
        enabled=True,
    )
    db.add(source)
    db.flush()

    on_success = _ensure_event_type(
        db, source.id, "on_success", "Poll completed successfully",
    )
    _ensure_event_type(
        db, source.id, "on_failure", "Poll failed (HTTP error or timeout)",
    )

    schedule_type_raw = (src_spec.get("schedule_type") or "interval").strip()
    try:
        schedule_type = ScheduleType(schedule_type_raw)
    except ValueError:
        schedule_type = ScheduleType.INTERVAL
    cron_expression = (src_spec.get("cron_expression") or "").strip()
    interval_seconds = src_spec.get("interval_seconds")
    if schedule_type == ScheduleType.CRON:
        interval_seconds = None
    else:
        interval_seconds = int(interval_seconds or 300)
        cron_expression = ""

    schedule = PollingSchedule(
        source_id=source.id,
        name=name,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        interval_seconds=interval_seconds,
        handler_type=src_spec.get("handler_type") or "http_get",
        handler_url=handler_url,
        handler_params={"event_type": "on_success"},
        timeout_seconds=30,
        retry_count=0,
        enabled=True,
    )
    db.add(schedule)
    db.flush()

    field_ids: dict[str, int] = {}
    field_slugs: dict[str, str] = {}
    for fspec in tmpl.get("fields") or []:
        fname = _unique_field_name(db, fspec["name"])
        preferred_f = fspec.get("preferred_slug") or fname
        fslug = _unique_preferred_slug(db, Field, preferred_f)
        field = Field(
            name=fname,
            slug=fslug,
            field_type=fspec["field_type"],
            config=dict(fspec.get("config") or {}),
            state=default_field_state(fspec["field_type"]),
        )
        db.add(field)
        db.flush()
        field_ids[fspec["key"]] = field.id
        field_slugs[fspec["key"]] = field.slug

    from app.kanban import ensure_columns
    from app.models import DashboardLayout

    page_slugs: dict[str, str] = {}
    for pspec in tmpl.get("pages") or []:
        page_name = pspec["name"]
        page_slug = unique_page_slug(db, slugify_page_name(page_name))
        page = DashboardLayout(
            name=page_name,
            slug=page_slug,
            kind=pspec.get("kind") or "dashboard",
            layout_config=layout_json([]),
        )
        db.add(page)
        db.flush()
        if page.kind == "kanban":
            ensure_columns(db, page)
        page_slugs[pspec["key"]] = page.slug

    event_types = {
        "on_success": on_success,
    }
    rule_ids: list[int] = []
    action_ids: list[int] = []
    for order, rspec in enumerate(tmpl.get("rules") or []):
        acts: list[int] = []
        for aspec in rspec.get("actions") or []:
            cfg = dict(aspec.get("config") or {})
            fkey = aspec.get("field_key")
            if fkey:
                cfg["field_id"] = field_ids[fkey]
            page_key = aspec.get("page_key")
            if page_key:
                cfg["page_slug"] = page_slugs[page_key]
            action = ActionInstance(
                source_id=source.id,
                action_type=aspec["action_type"],
                config=cfg,
                enabled=True,
            )
            db.add(action)
            db.flush()
            acts.append(action.id)
            action_ids.append(action.id)

        et_name = rspec.get("event_type") or "on_success"
        et = event_types.get(et_name) or _ensure_event_type(
            db, source.id, et_name, et_name,
        )
        rule = Rule(
            source_id=source.id,
            description=rspec.get("description") or "",
            event_type_ids=[et.id],
            conditions=dict(rspec.get("conditions") or {}),
            action_ids=acts,
            order_index=order,
            enabled=True,
        )
        db.add(rule)
        db.flush()
        rule_ids.append(rule.id)

    widget_defs = _resolve_placeholders(
        copy.deepcopy(tmpl.get("widgets") or []),
        field_slugs,
    )
    widget_ids = _append_widgets(db, widget_defs) if widget_defs else []

    db.commit()
    db.refresh(schedule)
    add_or_update_job(schedule)

    return {
        "title": tmpl["title"],
        "source_id": source.id,
        "source_name": source.name,
        "source_slug": source.slug,
        "field_slugs": field_slugs,
        "rule_ids": rule_ids,
        "action_ids": action_ids,
        "widget_ids": widget_ids,
    }


def _ics_schedule(db: Session) -> PollingSchedule | None:
    return (
        db.query(PollingSchedule)
        .filter(PollingSchedule.handler_type == "ics_calendar")
        .order_by(PollingSchedule.id)
        .first()
    )


def has_calendar_feed(db: Session) -> bool:
    return _ics_schedule(db) is not None


def ensure_calendar_pipeline(
    db: Session,
    *,
    ics_url: str,
    kanban_slug: str | None,
) -> PollingSchedule:
    """Create or reuse the ICS poll, focus Fields, and card rules.

    Card rules are created only when ``kanban_slug`` is set. An existing rule
    keeps its board unless that page is gone.
    """
    tmpl = get_source_template("calendar_kanban")
    if not tmpl:
        raise ValueError("Calendar pipeline is not available")
    url = (ics_url or "").strip()
    schedule = _ics_schedule(db)
    if schedule is None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Calendar URL must start with http:// or https://")
        src_spec = tmpl["source"]
        name = src_spec["name"]
        source = Source(
            name=name,
            slug=_unique_preferred_slug(db, Source, src_spec.get("preferred_slug") or name),
            source_type="poll",
            description=src_spec.get("description") or "",
            config={"poll_category": src_spec.get("poll_category") or "external"},
            enabled=True,
        )
        db.add(source)
        db.flush()
        _ensure_event_type(db, source.id, "on_success", "Poll completed successfully")
        _ensure_event_type(db, source.id, "on_failure", "Poll failed (HTTP error or timeout)")
        schedule = PollingSchedule(
            source_id=source.id,
            name=name,
            schedule_type=ScheduleType.INTERVAL,
            cron_expression="",
            interval_seconds=int(src_spec.get("interval_seconds") or 900),
            handler_type="ics_calendar",
            handler_url=url,
            handler_params={"event_type": "on_success"},
            timeout_seconds=30,
            retry_count=0,
            enabled=True,
        )
        db.add(schedule)
        db.flush()
    elif url:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Calendar URL must start with http:// or https://")
        schedule.handler_url = url
        source = db.query(Source).filter(Source.id == schedule.source_id).one()
    else:
        source = db.query(Source).filter(Source.id == schedule.source_id).one()

    field_ids: dict[str, int] = {}
    field_slugs: dict[str, str] = {}
    for fspec in tmpl.get("fields") or []:
        preferred = fspec.get("preferred_slug") or fspec["name"]
        slug = _slugify_name(preferred) or "field"
        field = db.query(Field).filter(Field.slug == slug).first()
        if field is None:
            field = Field(
                name=_unique_field_name(db, fspec["name"]),
                slug=_unique_preferred_slug(db, Field, preferred),
                field_type=fspec["field_type"],
                config=dict(fspec.get("config") or {}),
                state=default_field_state(fspec["field_type"]),
            )
            db.add(field)
            db.flush()
        field_ids[fspec["key"]] = field.id
        field_slugs[fspec["key"]] = field.slug

    rules_by_desc = {
        (rule.description or ""): rule
        for rule in db.query(Rule).filter(Rule.source_id == source.id).all()
    }
    order = len(rules_by_desc)
    for rspec in tmpl.get("rules") or []:
        needs_board = any(
            aspec.get("action_type") == "kanban_push"
            for aspec in (rspec.get("actions") or [])
        )
        existing = rules_by_desc.get(rspec.get("description") or "")
        if existing is not None:
            if needs_board and kanban_slug:
                _retarget_kanban_rule(db, existing, kanban_slug)
            continue
        if needs_board and not kanban_slug:
            continue
        et = _ensure_event_type(
            db, source.id, rspec.get("event_type") or "on_success", rspec.get("event_type") or "",
        )
        acts: list[int] = []
        for aspec in rspec.get("actions") or []:
            cfg = dict(aspec.get("config") or {})
            fkey = aspec.get("field_key")
            if fkey:
                cfg["field_id"] = field_ids[fkey]
            if aspec.get("page_key"):
                cfg["page_slug"] = kanban_slug
            action = ActionInstance(
                source_id=source.id,
                action_type=aspec["action_type"],
                config=cfg,
                enabled=True,
            )
            db.add(action)
            db.flush()
            acts.append(action.id)
        rule = Rule(
            source_id=source.id,
            description=rspec.get("description") or "",
            event_type_ids=[et.id],
            conditions=dict(rspec.get("conditions") or {}),
            action_ids=acts,
            order_index=order,
            enabled=True,
        )
        db.add(rule)
        db.flush()
        order += 1

    widget_defs = _resolve_placeholders(copy.deepcopy(tmpl.get("widgets") or []), field_slugs)
    if widget_defs and not _main_has_widget_title(db, "Focus"):
        _append_widgets(db, widget_defs)
    return schedule


def _main_has_widget_title(db: Session, title: str) -> bool:
    from app.dashboard_layout import ensure_main_layout

    layout = ensure_main_layout(db)
    widgets = parse_layout_config(layout.layout_config)["widgets"]
    return any((w.get("title") or "") == title for w in widgets)


def _retarget_kanban_rule(db: Session, rule: Rule, kanban_slug: str) -> None:
    from app.dashboard_layout import get_layout

    for action_id in rule.action_ids or []:
        action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
        if action is None or action.action_type != "kanban_push":
            continue
        cfg = dict(action.config or {})
        current = str(cfg.get("page_slug") or "")
        layout = get_layout(db, current) if current else None
        if layout is not None and (layout.kind or "") == "kanban":
            continue
        cfg["page_slug"] = kanban_slug
        action.config = cfg

"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, text
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

from app import webctx as ctx

router = APIRouter()

# route: /config/pipeline
@router.get("/config/pipeline")
async def config_pipeline(request: Request, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    sources = db.query(Source).order_by(Source.name).all()
    fields = db.query(Field).order_by(Field.name).all()
    fields_by_id = {f.id: f for f in fields}
    chains = []
    for source in sources:
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
        actions_by_id = ctx._action_map(actions)
        unbound = [a for a in actions if a.id not in ctx._bound_action_ids(rules)]
        chains.append({
            "source": source,
            "rules": rules,
            "actions": actions,
            "actions_by_id": actions_by_id,
            "unbound_actions": unbound,
            "event_types": event_types,
            "event_types_by_id": ctx._event_type_map(event_types),
            "fields_by_id": fields_by_id,
        })
    return ctx.templates.TemplateResponse(
        request, "config/pipeline.html", {
            "active": "pipeline",
            "chains": chains,
            "fields": fields,
            "success": success,
            "error": error,
        }
    )



# route: /config/pipeline/partials/field-form
@router.get("/config/pipeline/partials/field-form")
async def pipeline_field_form(request: Request, db: Session = Depends(get_db)):
    from app.fields import FIELD_TYPES
    field = None
    field_id = request.query_params.get("field_id")
    if field_id:
        try:
            fid = int(field_id)
        except ValueError:
            return HTMLResponse("Invalid field", status_code=400)
        field = db.query(Field).filter(Field.id == fid).first()
        if not field:
            return HTMLResponse("Field not found", status_code=404)
    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_field_form.html", {
            "field": field,
            "field_types": FIELD_TYPES,
        }
    )



# route: /config/pipeline/fields
@router.post("/config/pipeline/fields")
async def pipeline_create_field(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    kwargs, err = ctx._parse_field_form(form)
    if err:
        if ctx._is_htmx(request):
            return HTMLResponse(err, status_code=400)
        return ctx._pipeline_redirect(error=err)
    if db.query(Field).filter(Field.name == kwargs["name"]).first():
        msg = f"Field '{kwargs['name']}' already exists"
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return ctx._pipeline_redirect(error=msg)

    from app.fields import default_field_state

    field = Field(
        name=kwargs["name"],
        slug=ctx._unique_field_slug(db, kwargs["name"]),
        field_type=kwargs["field_type"],
        config=kwargs["config"],
        state=default_field_state(kwargs["field_type"]),
    )
    db.add(field)
    db.commit()
    ctx._audit_log(db, request, "field.create", resource_type="field", resource_id=field.id,
               details={"name": field.name, "field_type": field.field_type})

    if ctx._is_htmx(request):
        resp = ctx._fields_section_template(request, db)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success=f"Field '{field.name}' created")



# route: /config/pipeline/field/{field_id}
@router.post("/config/pipeline/field/{field_id}")
async def pipeline_update_field(request: Request, field_id: int, db: Session = Depends(get_db)):
    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        if ctx._is_htmx(request):
            return HTMLResponse("Field not found", status_code=404)
        return ctx._pipeline_redirect(error="Field not found")

    form = await request.form()
    kwargs, err = ctx._parse_field_form(form, existing=field)
    if err:
        if ctx._is_htmx(request):
            return HTMLResponse(err, status_code=400)
        return ctx._pipeline_redirect(error=err)

    clash = (
        db.query(Field)
        .filter(Field.name == kwargs["name"], Field.id != field_id)
        .first()
    )
    if clash:
        msg = f"Field '{kwargs['name']}' already exists"
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return ctx._pipeline_redirect(error=msg)

    field.name = kwargs["name"]
    field.slug = ctx._unique_field_slug(db, kwargs["name"], exclude_id=field_id)
    field.config = kwargs["config"]
    db.commit()
    ctx._audit_log(db, request, "field.update", resource_type="field", resource_id=field.id,
               details={"name": field.name})

    if ctx._is_htmx(request):
        resp = ctx._fields_section_template(request, db)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success=f"Field '{field.name}' updated")



# route: /config/pipeline/field/{field_id}/delete
@router.post("/config/pipeline/field/{field_id}/delete")
async def pipeline_delete_field(request: Request, field_id: int, db: Session = Depends(get_db)):
    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        if ctx._is_htmx(request):
            return HTMLResponse("Field not found", status_code=404)
        return ctx._pipeline_redirect(error="Field not found")

    reason = ctx._field_in_use(db, field_id)
    if reason:
        msg = f"Can’t delete “{field.name}” — it’s still in use"
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return ctx._pipeline_redirect(error=msg)

    name = field.name
    db.query(FieldLogEntry).filter(FieldLogEntry.field_id == field_id).delete(
        synchronize_session=False
    )
    db.query(MetricPoint).filter(MetricPoint.field_id == field_id).update(
        {MetricPoint.field_id: None}, synchronize_session=False
    )
    db.delete(field)
    db.commit()
    ctx._audit_log(db, request, "field.delete", resource_type="field", resource_id=field_id,
               details={"name": name})

    if ctx._is_htmx(request):
        return ctx._fields_section_template(request, db)
    return ctx._pipeline_redirect(success=f"Field '{name}' deleted")



# route: /config/pipeline/partials/source-form
@router.get("/config/pipeline/partials/source-form")
async def pipeline_source_form(request: Request):
    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_source_form.html", {"active": "pipeline"}
    )



# route: /config/pipeline/sources
@router.post("/config/pipeline/sources")
async def pipeline_create_source(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    source_type = (form.get("source_type") or "webhook").strip()
    description = (form.get("description") or "").strip()
    secret_value = (form.get("webhook_secret_value") or "").strip()

    def _err(msg: str):
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return ctx._pipeline_redirect(error=msg)

    if not name:
        return _err("Name is required")
    if source_type not in ctx._SOURCE_TYPES:
        return _err("Choose Webhook or Poll")

    slug = ctx._unique_slug_from_name(db, name)

    schedule_required = source_type == "poll"
    schedule_kwargs, schedule_error = ctx._parse_schedule_form(
        form, required=schedule_required,
    )
    if schedule_error:
        return _err(schedule_error)

    source = Source(
        name=name, slug=slug, source_type=source_type,
        description=description,
    )
    db.add(source)
    db.flush()

    if schedule_kwargs:
        schedule_kwargs = {**schedule_kwargs, "name": name}

    if source_type == "webhook" and secret_value:
        try:
            encrypted_value = encrypt_secret(secret_value)
        except ValueError as e:
            db.rollback()
            return _err(str(e))
        secret = Secret(
            scoped_to_type="source",
            scoped_to_id=source.id, encrypted_value=encrypted_value,
        )
        db.add(secret)
        db.flush()
        source.webhook_secret_id = secret.id

    if source_type == "poll":
        for et_name, et_desc in (
            ("on_success", "Poll completed successfully"),
            ("on_failure", "Poll failed (HTTP error or timeout)"),
        ):
            db.add(EventTypeRecord(
                source_id=source.id, name=et_name, description=et_desc,
            ))

    schedule = None
    if schedule_kwargs:
        params = dict(schedule_kwargs.get("handler_params") or {})
        if source_type == "poll" and "event_type" not in params:
            params["event_type"] = "on_success"
        schedule_kwargs = {**schedule_kwargs, "handler_params": params}
        schedule = PollingSchedule(source_id=source.id, **schedule_kwargs)
        db.add(schedule)

    db.commit()
    ctx._audit_log(db, request, "source.create", resource_type="source", resource_id=source.id, details={"name": name})

    if schedule:
        db.refresh(schedule)
        add_or_update_job(schedule)
        ctx._audit_log(
            db, request, "schedule.create",
            resource_type="schedule", resource_id=schedule.id,
            details={"name": schedule.name},
        )

    if ctx._is_htmx(request):
        # 200 + HX-Redirect: a 303 would be followed transparently by the
        # browser's XHR, so htmx would never see the redirect header.
        return HTMLResponse("", headers={"HX-Redirect": "/config/pipeline"})
    return ctx._pipeline_redirect(success=f"Source '{name}' created")



# route: /config/pipeline/source/{source_id}/partials/event-form
@router.get("/config/pipeline/source/{source_id}/partials/event-form")
async def pipeline_event_form(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return HTMLResponse("Source not found", status_code=404)
    event = None
    event_id = request.query_params.get("event_id")
    if event_id:
        try:
            eid = int(event_id)
        except ValueError:
            return HTMLResponse("Invalid event", status_code=400)
        event = (
            db.query(EventTypeRecord)
            .filter(EventTypeRecord.id == eid, EventTypeRecord.source_id == source_id)
            .first()
        )
        if not event:
            return HTMLResponse("Event not found", status_code=404)
    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_event_form.html", {
            "active": "pipeline",
            "source": source,
            "event": event,
        }
    )



# route: /config/pipeline/source/{source_id}/partials/recent-events
@router.get("/config/pipeline/source/{source_id}/partials/recent-events")
async def pipeline_recent_events(request: Request, source_id: int, db: Session = Depends(get_db)):
    from app.event_store import RECENT_LIMIT_DEFAULT, RECENT_LIMIT_MAX

    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return HTMLResponse("Source not found", status_code=404)
    try:
        limit = int(request.query_params.get("limit") or RECENT_LIMIT_DEFAULT)
    except ValueError:
        limit = RECENT_LIMIT_DEFAULT
    limit = max(1, min(limit, RECENT_LIMIT_MAX))

    events = (
        db.query(Event)
        .filter(Event.source_id == source_id)
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(limit)
        .all()
    )
    et_ids = {e.event_type_id for e in events if e.event_type_id}
    types_by_id = {}
    if et_ids:
        for et in db.query(EventTypeRecord).filter(EventTypeRecord.id.in_(et_ids)).all():
            types_by_id[et.id] = et

    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_recent_events.html", {
            "source": source,
            "events": events,
            "types_by_id": types_by_id,
            "limit": limit,
            "limit_choices": (5, 10, 25, 50),
        }
    )



# route: /config/pipeline/source/{source_id}/partials/latest-event
@router.get("/config/pipeline/source/{source_id}/partials/latest-event")
async def pipeline_latest_event(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return HTMLResponse("Source not found", status_code=404)
    et_raw = (request.query_params.get("event_type_id") or "").strip()
    if not et_raw:
        return HTMLResponse("Choose an event type", status_code=400)
    try:
        et_id = int(et_raw)
    except ValueError:
        return HTMLResponse("Invalid event type", status_code=400)
    event_type = (
        db.query(EventTypeRecord)
        .filter(EventTypeRecord.id == et_id, EventTypeRecord.source_id == source_id)
        .first()
    )
    if not event_type:
        return HTMLResponse("Event type not found", status_code=404)

    event = (
        db.query(Event)
        .filter(Event.source_id == source_id, Event.event_type_id == et_id)
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .first()
    )
    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_latest_event.html", {
            "source": source,
            "event_type": event_type,
            "event": event,
        }
    )



# route: /config/pipeline/source/{source_id}/events
@router.post("/config/pipeline/source/{source_id}/events")
async def pipeline_create_event(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        if ctx._is_htmx(request):
            return HTMLResponse("Source not found", status_code=404)
        return ctx._pipeline_redirect(error="Source not found")

    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    if not name:
        if ctx._is_htmx(request):
            return HTMLResponse("Name is required", status_code=400)
        return ctx._pipeline_redirect(error="Name is required")

    et = EventTypeRecord(source_id=source_id, name=name, description=description)
    db.add(et)
    db.commit()
    ctx._audit_log(db, request, "event_type.create", resource_type="event_type", resource_id=et.id, details={"name": name})

    if ctx._is_htmx(request):
        resp = ctx._source_chain_template(request, db, source)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success=f"Event '{name}' created")



# route: /config/pipeline/event/{et_id}
@router.post("/config/pipeline/event/{et_id}")
async def pipeline_update_event(request: Request, et_id: int, db: Session = Depends(get_db)):
    et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
    if not et:
        if ctx._is_htmx(request):
            return HTMLResponse("Event not found", status_code=404)
        return ctx._pipeline_redirect(error="Event not found")
    source = db.query(Source).filter(Source.id == et.source_id).first()
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    if not name:
        if ctx._is_htmx(request):
            return HTMLResponse("Name is required", status_code=400)
        return ctx._pipeline_redirect(error="Name is required")
    et.name = name
    et.description = description
    db.commit()
    ctx._audit_log(db, request, "event_type.update", resource_type="event_type", resource_id=et.id, details={"name": name})
    if ctx._is_htmx(request) and source:
        resp = ctx._source_chain_template(request, db, source)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success=f"Event '{name}' updated")



# route: /config/pipeline/source/{source_id}/partials/rule-form
@router.get("/config/pipeline/source/{source_id}/partials/rule-form")
async def pipeline_rule_form(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return HTMLResponse("Source not found", status_code=404)
    event_types = (
        db.query(EventTypeRecord)
        .filter(EventTypeRecord.source_id == source_id)
        .order_by(EventTypeRecord.name)
        .all()
    )
    rule = None
    selected_event_type_ids: list[int] = []
    rule_id = request.query_params.get("rule_id")
    event_type_id = request.query_params.get("event_type_id")
    if rule_id:
        try:
            rid = int(rule_id)
        except ValueError:
            return HTMLResponse("Invalid rule", status_code=400)
        rule = (
            db.query(Rule)
            .filter(Rule.id == rid, Rule.source_id == source_id)
            .first()
        )
        if not rule:
            return HTMLResponse("Rule not found", status_code=404)
    elif event_type_id:
        try:
            selected_event_type_ids = [int(event_type_id)]
        except ValueError:
            return HTMLResponse("Invalid event type", status_code=400)
    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_rule_form.html", {
            "active": "pipeline",
            "source": source,
            "event_types": event_types,
            "rule": rule,
            "selected_event_type_ids": selected_event_type_ids,
        }
    )


def _validate_rule_refs(db, source_id: int, event_type_ids: list, action_ids: list | None):
    if action_ids:
        owned = {
            a.id
            for a in db.query(ActionInstance)
            .filter(
                ActionInstance.source_id == source_id,
                ActionInstance.id.in_(action_ids),
            )
            .all()
        }
        if set(action_ids) - owned:
            return "Actions must belong to this source"
    if event_type_ids:
        owned_ets = {
            et.id
            for et in db.query(EventTypeRecord)
            .filter(
                EventTypeRecord.source_id == source_id,
                EventTypeRecord.id.in_(event_type_ids),
            )
            .all()
        }
        if set(event_type_ids) - owned_ets:
            return "Event types must belong to this source"
    return None



# route: /config/pipeline/source/{source_id}/rules
@router.post("/config/pipeline/source/{source_id}/rules")
async def pipeline_create_rule(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        if ctx._is_htmx(request):
            return HTMLResponse("Source not found", status_code=404)
        return ctx._pipeline_redirect(error="Source not found")

    form = await request.form()
    data, err = ctx._parse_rule_form(form)
    if err:
        if ctx._is_htmx(request):
            return HTMLResponse(err, status_code=400)
        return ctx._pipeline_redirect(error=err)

    ref_err = _validate_rule_refs(
        db, source_id, data["event_type_ids"], data.get("action_ids"),
    )
    if ref_err:
        if ctx._is_htmx(request):
            return HTMLResponse(ref_err, status_code=400)
        return ctx._pipeline_redirect(error=ref_err)

    rule = Rule(
        source_id=source_id,
        event_type_ids=data["event_type_ids"],
        conditions=data["conditions"],
        action_ids=data.get("action_ids") or [],
        order_index=data["order_index"],
    )
    db.add(rule)
    db.commit()
    ctx._audit_log(db, request, "rule.create", resource_type="rule", resource_id=rule.id,
               details={"order_index": rule.order_index})

    if ctx._is_htmx(request):
        resp = ctx._source_chain_template(request, db, source)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success="Rule created")



# route: /config/pipeline/rule/{rule_id}
@router.post("/config/pipeline/rule/{rule_id}")
async def pipeline_update_rule(request: Request, rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule or not rule.source_id:
        if ctx._is_htmx(request):
            return HTMLResponse("Rule not found", status_code=404)
        return ctx._pipeline_redirect(error="Rule not found")
    source = db.query(Source).filter(Source.id == rule.source_id).first()

    form = await request.form()
    data, err = ctx._parse_rule_form(form, for_update=True)
    if err:
        if ctx._is_htmx(request):
            return HTMLResponse(err, status_code=400)
        return ctx._pipeline_redirect(error=err)

    # Edit form does not send action_ids — keep existing bindings
    ref_err = _validate_rule_refs(db, rule.source_id, data["event_type_ids"], None)
    if ref_err:
        if ctx._is_htmx(request):
            return HTMLResponse(ref_err, status_code=400)
        return ctx._pipeline_redirect(error=ref_err)

    rule.conditions = data["conditions"]
    rule.order_index = data["order_index"]
    rule.event_type_ids = data["event_type_ids"]
    rule.enabled = data["enabled"]
    db.commit()
    ctx._audit_log(db, request, "rule.update", resource_type="rule", resource_id=rule.id,
               details={"order_index": rule.order_index})

    if ctx._is_htmx(request) and source:
        resp = ctx._source_chain_template(request, db, source)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success="Rule updated")



# route: /config/pipeline/source/{source_id}/partials/action-form
@router.get("/config/pipeline/source/{source_id}/partials/action-form")
async def pipeline_action_form(request: Request, source_id: int, db: Session = Depends(get_db)):
    from app.actions import get_action_types
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return HTMLResponse("Source not found", status_code=404)

    action = None
    rule = None
    action_id = request.query_params.get("action_id")
    rule_id = request.query_params.get("rule_id")

    if action_id:
        try:
            aid = int(action_id)
        except ValueError:
            return HTMLResponse("Invalid action", status_code=400)
        action = (
            db.query(ActionInstance)
            .filter(ActionInstance.id == aid, ActionInstance.source_id == source_id)
            .first()
        )
        if not action:
            return HTMLResponse("Action not found", status_code=404)
    elif rule_id:
        try:
            rid = int(rule_id)
        except ValueError:
            return HTMLResponse("Invalid rule", status_code=400)
        rule = (
            db.query(Rule)
            .filter(Rule.id == rid, Rule.source_id == source_id)
            .first()
        )
        if not rule:
            return HTMLResponse("Rule not found", status_code=404)
    else:
        return HTMLResponse("Pick a rule first", status_code=400)

    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_action_form.html", {
            "active": "pipeline",
            "source": source,
            "action_types": get_action_types(),
            "action": action,
            "rule": rule,
            "fields": db.query(Field).order_by(Field.name).all(),
        }
    )



# route: /config/pipeline/source/{source_id}/actions
@router.post("/config/pipeline/source/{source_id}/actions")
async def pipeline_create_action(request: Request, source_id: int, db: Session = Depends(get_db)):
    from app.actions import get_action_types
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        if ctx._is_htmx(request):
            return HTMLResponse("Source not found", status_code=404)
        return ctx._pipeline_redirect(error="Source not found")

    form = await request.form()
    action_type = form.get("action_type", "field_push")
    secret_value = (form.get("secret_value") or "").strip()
    secret2_value = (form.get("secret2_value") or "").strip()
    rule_id_raw = (form.get("rule_id") or "").strip()

    # Creates unused action when no rule references it yet.
    require_rule = ctx._is_htmx(request) or bool(rule_id_raw)
    rule = None
    if rule_id_raw:
        try:
            rid = int(rule_id_raw)
        except ValueError:
            msg = "Invalid rule"
            if ctx._is_htmx(request):
                return HTMLResponse(msg, status_code=400)
            return ctx._pipeline_redirect(error=msg)
        rule = (
            db.query(Rule)
            .filter(Rule.id == rid, Rule.source_id == source_id)
            .first()
        )
        if not rule:
            msg = "Rule not found on this source"
            if ctx._is_htmx(request):
                return HTMLResponse(msg, status_code=400)
            return ctx._pipeline_redirect(error=msg)
    elif require_rule:
        msg = "A rule is required"
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return ctx._pipeline_redirect(error=msg)

    if action_type not in get_action_types():
        msg = "That action type isn’t supported"
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return ctx._pipeline_redirect(error=msg)

    # Resolve field_type for field_push when not posted (look up Field)
    if action_type == "field_push" and not (form.get("field_type") or "").strip():
        fid_raw = (form.get("field_id") or "").strip()
        if fid_raw:
            try:
                f = db.query(Field).filter(Field.id == int(fid_raw)).first()
                if f:
                    # MutableMapping from Starlette form is immutable — rebuild via dict
                    form = dict(form)
                    form["field_type"] = f.field_type
            except ValueError:
                pass

    config, err = ctx._parse_action_config(form, action_type)
    if err:
        if ctx._is_htmx(request):
            return HTMLResponse(err, status_code=400)
        return ctx._pipeline_redirect(error=err)

    if action_type == "field_push":
        field = db.query(Field).filter(Field.id == config["field_id"]).first()
        if not field:
            msg = "Field not found"
            if ctx._is_htmx(request):
                return HTMLResponse(msg, status_code=400)
            return ctx._pipeline_redirect(error=msg)

    action = ActionInstance(
        source_id=source_id, action_type=action_type, config=config,
    )
    db.add(action)
    db.flush()

    if action_type == "http_forward":
        try:
            if secret_value:
                ctx._upsert_action_secret(db, action, value=secret_value, which="primary")
            if config.get("auth_mode") == "key_secret" and secret2_value:
                ctx._upsert_action_secret(db, action, value=secret2_value, which="secondary")
        except ValueError as e:
            db.rollback()
            if ctx._is_htmx(request):
                return HTMLResponse(str(e), status_code=400)
            return ctx._pipeline_redirect(error=str(e))

    if rule is not None:
        ids = list(rule.action_ids or [])
        ids.append(action.id)
        rule.action_ids = ids

    db.commit()
    ctx._audit_log(db, request, "action.create", resource_type="action", resource_id=action.id,
               details={"action_type": action_type})

    if ctx._is_htmx(request):
        resp = ctx._source_chain_template(request, db, source)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success="Action created")



# route: /config/pipeline/action/{action_id}
@router.post("/config/pipeline/action/{action_id}")
async def pipeline_update_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    from app.actions import get_action_types
    action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
    if not action:
        if ctx._is_htmx(request):
            return HTMLResponse("Action not found", status_code=404)
        return ctx._pipeline_redirect(error="Action not found")
    source = db.query(Source).filter(Source.id == action.source_id).first()

    form = await request.form()
    action_type = form.get("action_type", action.action_type)
    secret_value = (form.get("secret_value") or "").strip()
    secret2_value = (form.get("secret2_value") or "").strip()

    if action_type not in get_action_types():
        msg = "That action type isn’t supported"
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return ctx._pipeline_redirect(error=msg)

    if action_type == "field_push" and not (form.get("field_type") or "").strip():
        fid_raw = (form.get("field_id") or "").strip()
        if fid_raw:
            try:
                f = db.query(Field).filter(Field.id == int(fid_raw)).first()
                if f:
                    form = dict(form)
                    form["field_type"] = f.field_type
            except ValueError:
                pass

    config, err = ctx._parse_action_config(form, action_type)
    if err:
        if ctx._is_htmx(request):
            return HTMLResponse(err, status_code=400)
        return ctx._pipeline_redirect(error=err)

    if action_type == "field_push":
        field = db.query(Field).filter(Field.id == config["field_id"]).first()
        if not field:
            msg = "Field not found"
            if ctx._is_htmx(request):
                return HTMLResponse(msg, status_code=400)
            return ctx._pipeline_redirect(error=msg)

    action.action_type = action_type
    action.config = config

    if action_type == "http_forward":
        try:
            if secret_value:
                ctx._upsert_action_secret(db, action, value=secret_value, which="primary")
            if config.get("auth_mode") == "key_secret" and secret2_value:
                ctx._upsert_action_secret(db, action, value=secret2_value, which="secondary")
        except ValueError as e:
            if ctx._is_htmx(request):
                return HTMLResponse(str(e), status_code=400)
            return ctx._pipeline_redirect(error=str(e))
    else:
        # Non-http actions should not keep secrets
        action.secret_id = None
        action.secret_id_2 = None

    db.commit()
    ctx._audit_log(db, request, "action.update", resource_type="action", resource_id=action.id,
               details={"action_type": action_type})

    if ctx._is_htmx(request) and source:
        resp = ctx._source_chain_template(request, db, source)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success="Action updated")


# route: /config/pipeline/source/{source_id}/partials/edit-form
@router.get("/config/pipeline/source/{source_id}/partials/edit-form")
async def pipeline_source_edit_form(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return HTMLResponse("Source not found", status_code=404)
    schedule = (
        db.query(PollingSchedule)
        .filter(PollingSchedule.source_id == source_id)
        .order_by(PollingSchedule.id)
        .first()
    )
    webhook_secret = None
    if source.webhook_secret_id:
        webhook_secret = db.query(Secret).filter(Secret.id == source.webhook_secret_id).first()
    params = (schedule.handler_params if schedule else None) or {}
    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_source_edit_form.html", {
            "active": "pipeline",
            "source": source,
            "schedule": schedule,
            "webhook_secret": webhook_secret,
            "handler_params_json": json.dumps(params, indent=2) if params else "{}",
        }
    )



# route: /config/source/{source_id}/edit
@router.post("/config/source/{source_id}/edit")
async def update_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found")

    def _err(msg: str):
        if ctx._is_htmx(request):
            return HTMLResponse(msg, status_code=400)
        return RedirectResponse(
            url=ctx.flash_url("/config/pipeline", error=msg),
            status_code=303,
        )

    name = (form.get("name") or "").strip()
    source_type = (form.get("source_type") or source.source_type or "webhook").strip()
    description = (form.get("description") or "").strip()
    secret_value = (form.get("webhook_secret_value") or "").strip()
    clear_secret = form.get("clear_webhook_secret") in ("1", "on", "true")

    if not name:
        return _err("Name is required")
    if source_type not in ctx._SOURCE_TYPES:
        return _err("Choose Webhook or Poll")

    schedule_required = source_type == "poll"
    schedule_kwargs, schedule_error = ctx._parse_schedule_form(
        form, required=schedule_required,
    )
    if schedule_error:
        return _err(schedule_error)

    source.name = name
    source.slug = ctx._unique_slug_from_name(db, name, exclude_id=source_id)
    source.source_type = source_type
    source.description = description

    if schedule_kwargs:
        schedule_kwargs = {**schedule_kwargs, "name": name}

    if source_type == "webhook":
        if clear_secret:
            old_id = source.webhook_secret_id
            source.webhook_secret_id = None
            if old_id:
                orphan = db.query(Secret).filter(Secret.id == old_id).first()
                if orphan:
                    db.delete(orphan)
        elif secret_value:
            try:
                encrypted_value = encrypt_secret(secret_value)
            except ValueError as e:
                return _err(str(e))
            secret = Secret(
                scoped_to_type="source",
                scoped_to_id=source.id, encrypted_value=encrypted_value,
            )
            db.add(secret)
            db.flush()
            source.webhook_secret_id = secret.id

    schedule = None
    if schedule_kwargs:
        schedule_id_raw = form.get("schedule_id")
        existing = None
        if schedule_id_raw:
            try:
                sid = int(schedule_id_raw)
            except (TypeError, ValueError):
                sid = None
            if sid:
                existing = (
                    db.query(PollingSchedule)
                    .filter(PollingSchedule.id == sid, PollingSchedule.source_id == source_id)
                    .first()
                )
        if existing is None:
            existing = (
                db.query(PollingSchedule)
                .filter(PollingSchedule.source_id == source_id)
                .order_by(PollingSchedule.id)
                .first()
            )
        if existing:
            for key, value in schedule_kwargs.items():
                setattr(existing, key, value)
            schedule = existing
        else:
            schedule = PollingSchedule(source_id=source.id, **schedule_kwargs)
            db.add(schedule)

    db.commit()
    ctx._audit_log(db, request, "source.update", resource_type="source", resource_id=source.id, details={"name": name})

    if schedule:
        db.refresh(schedule)
        add_or_update_job(schedule)
        ctx._audit_log(
            db, request, "schedule.update",
            resource_type="schedule", resource_id=schedule.id,
            details={"name": schedule.name},
        )

    if ctx._is_htmx(request):
        resp = ctx._source_chain_template(request, db, source)
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success=f"Source '{name}' updated")



# route: /config/source/{source_id}/delete
@router.post("/config/source/{source_id}/delete")
async def delete_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found")
    name = source.name
    schedules = db.query(PollingSchedule).filter(PollingSchedule.source_id == source_id).all()
    for sched in schedules:
        remove_job(sched.id)

    actions = db.query(ActionInstance).filter(ActionInstance.source_id == source_id).all()
    action_ids = [a.id for a in actions]
    for action in actions:
        action.secret_id = None
    source.webhook_secret_id = None
    db.flush()

    if action_ids:
        db.query(Secret).filter(
            Secret.scoped_to_type == "action",
            Secret.scoped_to_id.in_(action_ids),
        ).delete(synchronize_session=False)
    db.query(Secret).filter(
        Secret.scoped_to_type == "source",
        Secret.scoped_to_id == source_id,
    ).delete(synchronize_session=False)
    db.query(ActionInstance).filter(ActionInstance.source_id == source_id).delete()
    db.query(FieldLogEntry).filter(FieldLogEntry.source_id == source_id).update(
        {FieldLogEntry.source_id: None, FieldLogEntry.event_id: None},
        synchronize_session=False,
    )
    db.query(MetricPoint).filter(MetricPoint.source_id == source_id).delete()
    db.query(Event).filter(Event.source_id == source_id).delete()
    db.query(EventTypeRecord).filter(EventTypeRecord.source_id == source_id).delete()
    db.query(PollingSchedule).filter(PollingSchedule.source_id == source_id).delete()
    db.query(Rule).filter(Rule.source_id == source_id).delete()
    db.delete(source)
    db.commit()
    ctx._audit_log(db, request, "source.delete", resource_type="source", resource_id=source_id, details={"name": name})

    if ctx._is_htmx(request):
        return HTMLResponse("")
    return ctx._pipeline_redirect(success=f"Source '{name}' deleted")



# route: /config/source/{source_id}/toggle
@router.post("/config/source/{source_id}/toggle")
async def toggle_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found")
    source.enabled = not source.enabled
    db.commit()
    status_text = "enabled" if source.enabled else "disabled"
    ctx._audit_log(db, request, "source.toggle", resource_type="source", resource_id=source.id, details={"status": status_text})

    if ctx._is_htmx(request):
        resp = ctx._source_chain_template(request, db, source)
        return resp
    return ctx._pipeline_redirect(success=f"Source '{source.name}' {status_text}")


# route: /config/event-type/{et_id}/toggle
@router.post("/config/event-type/{et_id}/toggle")
async def toggle_event_type(request: Request, et_id: int, db: Session = Depends(get_db)):
    et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
    if not et:
        return ctx._pipeline_redirect(error="Event type not found")
    et.enabled = not et.enabled
    db.commit()
    status_text = "active" if et.enabled else "paused"
    ctx._audit_log(
        db, request, "event_type.toggle",
        resource_type="event_type", resource_id=et.id,
        details={"status": status_text, "name": et.name},
    )
    source = db.query(Source).filter(Source.id == et.source_id).first()
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success=f"Event '{et.name}' {status_text}")


# route: /config/rule/{rule_id}/toggle
@router.post("/config/rule/{rule_id}/toggle")
async def toggle_rule(request: Request, rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        return ctx._pipeline_redirect(error="Rule not found")
    rule.enabled = not rule.enabled
    db.commit()
    status_text = "active" if rule.enabled else "paused"
    ctx._audit_log(
        db, request, "rule.toggle",
        resource_type="rule", resource_id=rule.id,
        details={"status": status_text},
    )
    source = db.query(Source).filter(Source.id == rule.source_id).first() if rule.source_id else None
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success=f"Rule {status_text}")


# route: /config/action/{action_id}/toggle
@router.post("/config/action/{action_id}/toggle")
async def toggle_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
    if not action:
        return ctx._pipeline_redirect(error="Action not found")
    action.enabled = not action.enabled
    db.commit()
    status_text = "active" if action.enabled else "paused"
    ctx._audit_log(
        db, request, "action.toggle",
        resource_type="action", resource_id=action.id,
        details={"status": status_text},
    )
    source = db.query(Source).filter(Source.id == action.source_id).first()
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success=f"Action {status_text}")


# route: /config/event-type/{et_id}/delete
@router.post("/config/event-type/{et_id}/delete")
async def delete_event_type(request: Request, et_id: int, db: Session = Depends(get_db)):
    et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
    if not et:
        return ctx._pipeline_redirect(error="Event type not found")
    name = et.name
    source_id = et.source_id
    ctx._scrub_event_type_from_rules(db, et_id)
    db.delete(et)
    db.commit()
    ctx._audit_log(db, request, "event_type.delete", resource_type="event_type", resource_id=et_id, details={"name": name})

    source = db.query(Source).filter(Source.id == source_id).first()
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success=f"Event type '{name}' deleted")


# ── Config: Schedules ───────────────────────────────────────────────────────


# route: /config/source/{source_id}/schedules
@router.get("/config/source/{source_id}/schedules")
async def config_schedules(request: Request, source_id: int, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found")
    schedules = db.query(PollingSchedule).filter(PollingSchedule.source_id == source_id).all()
    return ctx.templates.TemplateResponse(
        request, "config/schedules.html", {"active": "pipeline", "source": source,
         "items": schedules, "success": success, "error": error}
    )



# route: /config/source/{source_id}/schedules
@router.post("/config/source/{source_id}/schedules")
async def create_schedule(request: Request, source_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found")

    kwargs, error = ctx._parse_schedule_form(form, required=True)
    if error:
        return RedirectResponse(
            url=ctx.flash_url(f"/config/source/{source_id}/schedules", error=error),
            status_code=303,
        )

    schedule = PollingSchedule(source_id=source_id, name=source.name, **kwargs)
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    add_or_update_job(schedule)
    ctx._audit_log(db, request, "schedule.create", resource_type="schedule", resource_id=schedule.id, details={"name": schedule.name})
    return RedirectResponse(
        url=ctx.flash_url(f"/config/source/{source_id}/schedules", success=f"Schedule for '{source.name}' created"),
        status_code=303,
    )



# route: /config/schedule/{schedule_id}/delete
@router.post("/config/schedule/{schedule_id}/delete")
async def delete_schedule(request: Request, schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.query(PollingSchedule).filter(PollingSchedule.id == schedule_id).first()
    if not schedule:
        return ctx._pipeline_redirect(error="Schedule not found")
    name = schedule.name
    source_id = schedule.source_id
    sid = schedule.id
    remove_job(sid)
    db.delete(schedule)
    db.commit()
    ctx._audit_log(db, request, "schedule.delete", resource_type="schedule", resource_id=sid, details={"name": name})
    return RedirectResponse(
        url=ctx.flash_url(f"/config/source/{source_id}/schedules", success=f"Schedule '{name}' deleted"),
        status_code=303,
    )



# route: /config/action/{action_id}/delete
@router.post("/config/action/{action_id}/delete")
async def delete_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
    if not action:
        return ctx._pipeline_redirect(error="Action not found")
    action_type = action.action_type
    source_id = action.source_id
    ctx._scrub_action_from_rules(db, source_id, action_id)
    for sid in (action.secret_id, action.secret_id_2):
        if sid:
            secret = db.query(Secret).filter(Secret.id == sid).first()
            if secret:
                db.delete(secret)
    db.query(Secret).filter(
        Secret.scoped_to_type == "action",
        Secret.scoped_to_id == action_id,
    ).delete(synchronize_session=False)
    db.delete(action)
    db.commit()
    ctx._audit_log(db, request, "action.delete", resource_type="action", resource_id=action_id,
               details={"action_type": action_type})

    source = db.query(Source).filter(Source.id == source_id).first()
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success="Action deleted")



# route: /config/rule/{rule_id}/delete
@router.post("/config/rule/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        return ctx._pipeline_redirect(error="Rule not found")
    source_id = rule.source_id
    db.delete(rule)
    db.commit()
    ctx._audit_log(db, request, "rule.delete", resource_type="rule", resource_id=rule_id, details={})

    source = db.query(Source).filter(Source.id == source_id).first() if source_id else None
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success="Rule deleted")


# ── Config: Users ───────────────────────────────────────────────────────────


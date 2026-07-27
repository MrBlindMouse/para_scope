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
from app.pollers import (
    get_poller_categories, get_poller_category, get_poller_specs, run_schedule,
)

from app import webctx as ctx

router = APIRouter()


def _poller_template_context(source: Source | None = None, schedule: PollingSchedule | None = None) -> dict:
    from app.webhook_verifiers import get_webhook_providers

    params = dict((schedule.handler_params if schedule else None) or {})
    source_cfg = dict((source.config if source else None) or {})
    selected_category = (
        source_cfg.get("poll_category")
        or (get_poller_category(schedule.handler_type) if schedule else "url")
    )
    return {
        "poller_categories": get_poller_categories(),
        "poller_specs": get_poller_specs(),
        "selected_poll_category": selected_category,
        "handler_params": params,
        "schedule_secret_keys": {
            key: bool(value)
            for key, value in params.items()
            if key.endswith("_secret_id")
        },
        "webhook_providers": get_webhook_providers(),
    }


def _sync_schedule_secrets(
    db: Session,
    schedule: PollingSchedule,
    *,
    previous_params: dict | None,
    secret_updates: dict[str, str],
) -> None:
    params = dict(schedule.handler_params or {})
    previous_params = dict(previous_params or {})
    secret_keys = {
        key
        for key in set(previous_params) | set(params) | set(secret_updates)
        if key.endswith("_secret_id")
    }
    for param_key in secret_keys:
        existing_id = previous_params.get(param_key)
        if param_key in secret_updates:
            encrypted = encrypt_secret(secret_updates[param_key])
            if existing_id:
                secret = db.query(Secret).filter(Secret.id == existing_id).first()
                if secret:
                    secret.encrypted_value = encrypted
                    params[param_key] = secret.id
                    continue
            secret = Secret(
                scoped_to_type="schedule",
                scoped_to_id=schedule.id,
                encrypted_value=encrypted,
            )
            db.add(secret)
            db.flush()
            params[param_key] = secret.id
            continue

        if params.get(param_key) is None:
            if existing_id:
                secret = db.query(Secret).filter(Secret.id == existing_id).first()
                if secret:
                    db.delete(secret)
            params.pop(param_key, None)
        elif existing_id and param_key not in params:
            params[param_key] = existing_id

    schedule.handler_params = params


def _delete_schedule_secrets(db: Session, schedule_id: int) -> None:
    db.query(Secret).filter(
        Secret.scoped_to_type == "schedule",
        Secret.scoped_to_id == schedule_id,
    ).delete(synchronize_session=False)

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
        return ctx._pipeline_redirect(error=err, request=request)
    if db.query(Field).filter(Field.name == kwargs["name"]).first():
        msg = f"Field '{kwargs['name']}' already exists"
        return ctx._pipeline_redirect(error=msg, request=request)

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
        return ctx._pipeline_redirect(error=err, request=request)

    clash = (
        db.query(Field)
        .filter(Field.name == kwargs["name"], Field.id != field_id)
        .first()
    )
    if clash:
        msg = f"Field '{kwargs['name']}' already exists"
        return ctx._pipeline_redirect(error=msg, request=request)

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
        return ctx._pipeline_redirect(error=msg, request=request)

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
    context = {"active": "pipeline"}
    context.update(_poller_template_context())
    return ctx.templates.TemplateResponse(request, "config/pipeline/_source_form.html", context)



# route: /config/pipeline/sources
@router.post("/config/pipeline/sources")
async def pipeline_create_source(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    source_type = (form.get("source_type") or "webhook").strip()
    description = (form.get("description") or "").strip()
    secret_value = (form.get("webhook_secret_value") or "").strip()
    webhook_provider = (form.get("webhook_provider") or "generic_hmac").strip() or "generic_hmac"
    paypal_webhook_id = (form.get("paypal_webhook_id") or "").strip()
    paypal_client_id = (form.get("paypal_client_id") or "").strip()
    paypal_environment = (form.get("paypal_environment") or "sandbox").strip() or "sandbox"
    poll_category = (form.get("poll_category") or "url").strip()

    def _err(msg: str):
        return ctx._pipeline_redirect(error=msg, request=request)

    if not name:
        return _err("Name is required")
    if source_type not in ctx._SOURCE_TYPES:
        return _err("Choose Webhook or Poll")

    from app.webhook_verifiers import get_webhook_provider_slugs
    if source_type == "webhook" and webhook_provider not in get_webhook_provider_slugs():
        return _err("Choose a supported webhook verification method")

    slug = ctx._unique_slug_from_name(db, name)

    schedule_required = source_type == "poll"
    schedule_kwargs, schedule_error = ctx._parse_schedule_form(
        form, required=schedule_required,
    )
    if schedule_error:
        return _err(schedule_error)

    source_config = {}
    if source_type == "poll":
        source_config["poll_category"] = poll_category
    if source_type == "webhook":
        source_config["webhook_provider"] = webhook_provider
        if webhook_provider == "paypal":
            if not paypal_webhook_id:
                return _err("Webhook ID is required")
            if not paypal_client_id:
                return _err("Client ID is required")
            if not secret_value:
                return _err("Client secret is required")
            source_config["paypal_webhook_id"] = paypal_webhook_id
            source_config["paypal_client_id"] = paypal_client_id
            source_config["paypal_environment"] = paypal_environment
    source = Source(
        name=name, slug=slug, source_type=source_type,
        description=description,
        config=source_config,
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
        secret_updates = dict(schedule_kwargs.pop("_secret_updates", {}))
        params = dict(schedule_kwargs.get("handler_params") or {})
        if source_type == "poll" and "event_type" not in params:
            params["event_type"] = "on_success"
        schedule_kwargs = {**schedule_kwargs, "handler_params": params}
        schedule = PollingSchedule(source_id=source.id, **schedule_kwargs)
        db.add(schedule)
        db.flush()
        _sync_schedule_secrets(
            db,
            schedule,
            previous_params={},
            secret_updates=secret_updates,
        )

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
        return ctx._pipeline_redirect(error="Name is required", request=request)

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
        return ctx._pipeline_redirect(error="Name is required", request=request)
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
        return ctx._pipeline_redirect(error=err, request=request)

    ref_err = _validate_rule_refs(
        db, source_id, data["event_type_ids"], data.get("action_ids"),
    )
    if ref_err:
        return ctx._pipeline_redirect(error=ref_err, request=request)

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
        return ctx._pipeline_redirect(error=err, request=request)

    # Edit form does not send action_ids — keep existing bindings
    ref_err = _validate_rule_refs(db, rule.source_id, data["event_type_ids"], None)
    if ref_err:
        return ctx._pipeline_redirect(error=ref_err, request=request)

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
    from app.actions import get_action_types, local_actions_enabled
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

    cfg = (action.config if action else {}) or {}
    headers = cfg.get("headers") or {}
    headers_text = "\n".join(f"{k}: {v}" for k, v in headers.items()) if isinstance(headers, dict) else ""
    custom_body = cfg.get("custom_body")
    if isinstance(custom_body, (dict, list)):
        custom_body_text = json.dumps(custom_body, indent=2)
    else:
        custom_body_text = custom_body if isinstance(custom_body, str) else ""

    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_action_form.html", {
            "active": "pipeline",
            "source": source,
            "action_types": get_action_types(),
            "action": action,
            "rule": rule,
            "fields": db.query(Field).order_by(Field.name).all(),
            "local_actions_enabled": local_actions_enabled(),
            "headers_text": headers_text,
            "custom_body_text": custom_body_text,
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
            return ctx._pipeline_redirect(error=msg, request=request)
        rule = (
            db.query(Rule)
            .filter(Rule.id == rid, Rule.source_id == source_id)
            .first()
        )
        if not rule:
            msg = "Rule not found on this source"
            return ctx._pipeline_redirect(error=msg, request=request)
    elif require_rule:
        msg = "A rule is required"
        return ctx._pipeline_redirect(error=msg, request=request)

    if action_type not in get_action_types():
        msg = "That action type isn’t supported"
        return ctx._pipeline_redirect(error=msg, request=request)

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
        return ctx._pipeline_redirect(error=err, request=request)

    if action_type == "field_push":
        field = db.query(Field).filter(Field.id == config["field_id"]).first()
        if not field:
            msg = "Field not found"
            return ctx._pipeline_redirect(error=msg, request=request)

    action = ActionInstance(
        source_id=source_id, action_type=action_type, config=config,
    )
    db.add(action)
    db.flush()

    if action_type in ("http_forward", "notify"):
        try:
            if secret_value:
                ctx._upsert_action_secret(db, action, value=secret_value, which="primary")
            if action_type == "http_forward" and config.get("auth_mode") == "key_secret" and secret2_value:
                ctx._upsert_action_secret(db, action, value=secret2_value, which="secondary")
        except ValueError as e:
            db.rollback()
            return ctx._pipeline_redirect(error=str(e), request=request)

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
        return ctx._pipeline_redirect(error=msg, request=request)

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
        return ctx._pipeline_redirect(error=err, request=request)

    if action_type == "field_push":
        field = db.query(Field).filter(Field.id == config["field_id"]).first()
        if not field:
            msg = "Field not found"
            return ctx._pipeline_redirect(error=msg, request=request)

    action.action_type = action_type
    action.config = config

    if action_type in ("http_forward", "notify"):
        try:
            if secret_value:
                ctx._upsert_action_secret(db, action, value=secret_value, which="primary")
            if action_type == "http_forward" and config.get("auth_mode") == "key_secret" and secret2_value:
                ctx._upsert_action_secret(db, action, value=secret2_value, which="secondary")
        except ValueError as e:
            return ctx._pipeline_redirect(error=str(e), request=request)
        if action_type == "notify":
            action.secret_id_2 = None
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
        .first()
    )
    webhook_secret = None
    if source.webhook_secret_id:
        webhook_secret = db.query(Secret).filter(Secret.id == source.webhook_secret_id).first()
    context = {
        "active": "pipeline",
        "source": source,
        "schedule": schedule,
        "webhook_secret": webhook_secret,
    }
    context.update(_poller_template_context(source, schedule))
    return ctx.templates.TemplateResponse(request, "config/pipeline/_source_edit_form.html", context)



# route: /config/source/{source_id}/edit
@router.post("/config/source/{source_id}/edit")
async def update_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found")

    def _err(msg: str):
        return ctx._pipeline_redirect(error=msg, request=request)

    name = (form.get("name") or "").strip()
    source_type = (form.get("source_type") or source.source_type or "webhook").strip()
    description = (form.get("description") or "").strip()
    secret_value = (form.get("webhook_secret_value") or "").strip()
    clear_secret = form.get("clear_webhook_secret") in ("1", "on", "true")
    webhook_provider = (form.get("webhook_provider") or (source.config or {}).get("webhook_provider") or "generic_hmac").strip()
    paypal_webhook_id = (form.get("paypal_webhook_id") or "").strip()
    paypal_client_id = (form.get("paypal_client_id") or "").strip()
    paypal_environment = (form.get("paypal_environment") or "sandbox").strip() or "sandbox"
    poll_category = (form.get("poll_category") or "url").strip()

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

    source_cfg = dict(source.config or {})
    if source_type == "poll":
        source_cfg["poll_category"] = poll_category
    else:
        source_cfg.pop("poll_category", None)

    if source_type == "webhook":
        from app.webhook_verifiers import get_webhook_provider_slugs
        if webhook_provider not in get_webhook_provider_slugs():
            return _err("Choose a supported webhook verification method")
        source_cfg["webhook_provider"] = webhook_provider
        if webhook_provider == "paypal":
            if paypal_webhook_id:
                source_cfg["paypal_webhook_id"] = paypal_webhook_id
            if paypal_client_id:
                source_cfg["paypal_client_id"] = paypal_client_id
            source_cfg["paypal_environment"] = paypal_environment
        else:
            source_cfg.pop("paypal_webhook_id", None)
            source_cfg.pop("paypal_client_id", None)
            source_cfg.pop("paypal_environment", None)
    else:
        source_cfg.pop("webhook_provider", None)
        source_cfg.pop("paypal_webhook_id", None)
        source_cfg.pop("paypal_client_id", None)
        source_cfg.pop("paypal_environment", None)

    source.name = name
    source.slug = ctx._unique_slug_from_name(db, name, exclude_id=source_id)
    source.source_type = source_type
    source.description = description
    source.config = source_cfg

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
    elif source_type == "poll":
        existing_type_names = {
            et.name
            for et in db.query(EventTypeRecord).filter(EventTypeRecord.source_id == source.id).all()
        }
        for et_name, et_desc in (
            ("on_success", "Poll completed successfully"),
            ("on_failure", "Poll failed (HTTP error or timeout)"),
        ):
            if et_name not in existing_type_names:
                db.add(EventTypeRecord(
                    source_id=source.id, name=et_name, description=et_desc,
                ))

    schedule = None
    if schedule_kwargs:
        secret_updates = dict(schedule_kwargs.pop("_secret_updates", {}))
        all_schedules = (
            db.query(PollingSchedule)
            .filter(PollingSchedule.source_id == source_id)
            .order_by(PollingSchedule.id)
            .all()
        )
        existing = all_schedules[0] if all_schedules else None
        # Defensive: drop any extra rows left from older multi-schedule installs.
        for extra in all_schedules[1:]:
            remove_job(extra.id)
            _delete_schedule_secrets(db, extra.id)
            db.delete(extra)
        if existing:
            previous_params = dict(existing.handler_params or {})
            for key, value in schedule_kwargs.items():
                setattr(existing, key, value)
            _sync_schedule_secrets(
                db,
                existing,
                previous_params=previous_params,
                secret_updates=secret_updates,
            )
            schedule = existing
        else:
            schedule = PollingSchedule(source_id=source.id, **schedule_kwargs)
            db.add(schedule)
            db.flush()
            _sync_schedule_secrets(
                db,
                schedule,
                previous_params={},
                secret_updates=secret_updates,
            )
    elif source_type != "poll":
        schedules = db.query(PollingSchedule).filter(PollingSchedule.source_id == source_id).all()
        for existing in schedules:
            remove_job(existing.id)
            _delete_schedule_secrets(db, existing.id)
            db.delete(existing)

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
    db.query(Secret).filter(
        Secret.scoped_to_type == "schedule",
        Secret.scoped_to_id.in_([sched.id for sched in schedules]),
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


# route: /config/source/{source_id}/poll-now
@router.post("/config/source/{source_id}/poll-now")
async def poll_source_now(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found")
    if source.source_type != "poll":
        return ctx._pipeline_redirect(error="Only poll sources can be run manually")
    if not source.enabled:
        msg = "Enable the source before running a poll"
        return ctx._pipeline_redirect(error=msg, request=request)

    schedule = (
        db.query(PollingSchedule)
        .filter(PollingSchedule.source_id == source_id)
        .first()
    )
    if not schedule:
        msg = "No schedule configured for this source"
        return ctx._pipeline_redirect(error=msg, request=request)
    if not schedule.enabled:
        msg = "Enable the schedule before running a poll"
        return ctx._pipeline_redirect(error=msg, request=request)

    ok = run_schedule(schedule.id)
    ctx._audit_log(
        db, request, "source.poll_now",
        resource_type="source", resource_id=source.id,
        details={"ok": ok, "schedule_id": schedule.id},
    )
    db.refresh(source)

    msg = f"Poll ran for '{source.name}'" if ok else f"Poll failed for '{source.name}'"
    if ctx._is_htmx(request):
        return ctx._source_chain_template(request, db, source)
    if not ok:
        return ctx._pipeline_redirect(error=msg)
    return ctx._pipeline_redirect(success=msg)


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


"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from types import SimpleNamespace
import json

from app.database import get_db
from app.models import (
    Source,
    EventTypeRecord,
    PollingSchedule,
    ActionInstance,
    Rule,
    Secret,
    Event,
    Field,
    FieldLogEntry,
)
from app.security import (
    encrypt_secret,
)
from app.scheduler import add_or_update_job, remove_job
from app.pollers import (
    get_poller_categories, get_poller_category, get_poller_specs, run_schedule,
)
from app.pipeline import EVENT_TYPE_MAX_LEN, normalize_event_type

from app import webctx as ctx

router = APIRouter()


def _validate_event_type_name(
    db: Session,
    source_id: int,
    raw_name: str,
    *,
    exclude_id: int | None = None,
) -> tuple[str | None, str | None]:
    """Return (canonical_name, error). error is set when invalid or duplicate."""
    name = normalize_event_type(raw_name)
    if not name:
        return None, "Event type is required"
    if len(name) > EVENT_TYPE_MAX_LEN:
        return None, f"Event type must be at most {EVENT_TYPE_MAX_LEN} characters"
    q = db.query(EventTypeRecord).filter(EventTypeRecord.source_id == source_id)
    if exclude_id is not None:
        q = q.filter(EventTypeRecord.id != exclude_id)
    for et in q.all():
        if normalize_event_type(et.name) == name:
            return None, f"Event type '{name}' already exists on this source"
    return name, None


def _ensure_event_type(
    db: Session,
    source_id: int,
    name: str,
    description: str,
    existing_names: set[str] | None = None,
) -> None:
    """Add EventTypeRecord if a casefold-equal name is not already present."""
    want = normalize_event_type(name)
    if existing_names is None:
        existing_names = {
            normalize_event_type(et.name)
            for et in db.query(EventTypeRecord).filter(
                EventTypeRecord.source_id == source_id
            ).all()
        }
    if want in existing_names:
        return
    db.add(EventTypeRecord(
        source_id=source_id,
        name=want,
        description=description,
    ))
    existing_names.add(want)


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


def _build_source_dialog_context(
    *,
    source: Source | None = None,
    schedule: PollingSchedule | None = None,
    webhook_secret: Secret | None = None,
    form=None,
    error: str | None = None,
) -> dict:
    source_cfg = dict((source.config if source else None) or {})
    source_type = (form.get("source_type") if form else None) or getattr(source, "source_type", None) or "webhook"
    source_type = source_type.strip() if isinstance(source_type, str) else "webhook"
    webhook_provider = (
        (form.get("webhook_provider") if form else None)
        or source_cfg.get("webhook_provider")
        or "generic_hmac"
    )
    webhook_provider = webhook_provider.strip() if isinstance(webhook_provider, str) else "generic_hmac"
    poll_category = (
        (form.get("poll_category") if form else None)
        or source_cfg.get("poll_category")
        or "url"
    )
    poll_category = poll_category.strip() if isinstance(poll_category, str) else "url"
    draft_config = dict(source_cfg)
    if source_type == "webhook":
        draft_config["webhook_provider"] = webhook_provider
        if webhook_provider == "paypal":
            draft_config["paypal_webhook_id"] = (form.get("paypal_webhook_id") if form else None) or draft_config.get("paypal_webhook_id") or ""
            draft_config["paypal_client_id"] = (form.get("paypal_client_id") if form else None) or draft_config.get("paypal_client_id") or ""
            draft_config["paypal_environment"] = (
                (form.get("paypal_environment") if form else None)
                or draft_config.get("paypal_environment")
                or "sandbox"
            )
        else:
            draft_config.pop("paypal_webhook_id", None)
            draft_config.pop("paypal_client_id", None)
            draft_config.pop("paypal_environment", None)
    else:
        draft_config["poll_category"] = poll_category
        draft_config.pop("webhook_provider", None)
        draft_config.pop("paypal_webhook_id", None)
        draft_config.pop("paypal_client_id", None)
        draft_config.pop("paypal_environment", None)

    draft_source = SimpleNamespace(
        id=getattr(source, "id", None),
        slug=getattr(source, "slug", ""),
        name=((form.get("name") if form else None) or getattr(source, "name", "") or "").strip(),
        description=((form.get("description") if form else None) or getattr(source, "description", "") or "").strip(),
        source_type=source_type,
        config=draft_config,
        webhook_secret_id=getattr(source, "webhook_secret_id", None),
    )

    draft_schedule = schedule
    if form is not None:
        handler_params: dict[str, object] = {}
        handler_url = getattr(schedule, "handler_url", "") or ""
        timeout_seconds = getattr(schedule, "timeout_seconds", None)
        retry_count = getattr(schedule, "retry_count", None)
        for spec in get_poller_specs():
            for field in spec.get("fields", []):
                raw = form.get(field["name"])
                if field["input_type"] == "checkbox":
                    value = raw in ("1", "on", "true")
                else:
                    if raw in (None, ""):
                        continue
                    value = raw
                if field["store"] == "url":
                    handler_url = value
                elif field["store"] == "timeout":
                    timeout_seconds = value
                elif field["store"] == "retry":
                    retry_count = value
                else:
                    handler_params[field["param_key"]] = value
        draft_schedule = SimpleNamespace(
            schedule_type=((form.get("schedule_type") or "interval").strip() or "interval"),
            interval_seconds=((form.get("interval_seconds") or "").strip() or None),
            cron_expression=(form.get("cron_expression") or "").strip(),
            handler_type=((form.get("handler_type") or getattr(schedule, "handler_type", None) or "http_get").strip() or "http_get"),
            handler_url=str(handler_url).strip(),
            timeout_seconds=(str(timeout_seconds).strip() if timeout_seconds not in (None, "") else None),
            retry_count=(str(retry_count).strip() if retry_count not in (None, "") else None),
            handler_params=handler_params,
        )

    context = {
        "active": "pipeline",
        "source": draft_source,
        "schedule": draft_schedule,
        "webhook_secret": webhook_secret,
        "is_edit": getattr(source, "id", None) is not None,
        "error": error,
    }
    context.update(_poller_template_context(draft_source, draft_schedule))
    return context


def _dialog_success_response(
    response: HTMLResponse,
    *,
    retarget: str,
    reswap: str = "outerHTML",
) -> HTMLResponse:
    response.headers["HX-Retarget"] = retarget
    response.headers["HX-Reswap"] = reswap
    response.headers["HX-Trigger"] = "pipeline-dialog-close"
    return response


def _build_field_dialog_context(*, field=None, form=None, error: str | None = None) -> dict:
    from app.fields import FIELD_TYPES

    field_type = (form.get("field_type") if form else None) or getattr(field, "field_type", None) or "logbook"
    field_type = field_type.strip() if isinstance(field_type, str) else "logbook"
    max_entries = (form.get("max_entries") if form else None)
    if max_entries in (None, ""):
        max_entries = ((field.config or {}).get("max_entries", 100) if field else 100)
    draft_field = SimpleNamespace(
        id=getattr(field, "id", None),
        field_type=field_type,
        name=((form.get("name") if form else None) or getattr(field, "name", "") or "").strip(),
        slug=getattr(field, "slug", "") or "",
        config={"max_entries": max_entries} if field_type == "logbook" else {},
    )
    return {
        "field": draft_field,
        "field_types": FIELD_TYPES,
        "is_edit": getattr(field, "id", None) is not None,
        "error": error,
    }


def _build_event_dialog_context(*, source: Source, event=None, form=None, error: str | None = None) -> dict:
    draft_event = SimpleNamespace(
        id=getattr(event, "id", None),
        name=((form.get("name") if form else None) or getattr(event, "name", "") or "").strip(),
        description=((form.get("description") if form else None) or getattr(event, "description", "") or ""),
    )
    return {
        "active": "pipeline",
        "source": source,
        "event": draft_event,
        "is_edit": getattr(event, "id", None) is not None,
        "error": error,
    }


def _build_rule_dialog_context(
    *,
    source: Source,
    event_types: list[EventTypeRecord],
    rule=None,
    selected_event_type_ids: list[int] | None = None,
    form=None,
    error: str | None = None,
) -> dict:
    conditions = getattr(rule, "conditions", {}) if rule else {}
    order_index = getattr(rule, "order_index", 0) if rule else 0
    enabled = getattr(rule, "enabled", True) if rule else True
    if form is not None:
        chosen_ids = ctx._parse_int_list(form, "event_type_ids")
        raw_conditions = (form.get("conditions") or "{}").strip()
        try:
            conditions = json.loads(raw_conditions) if raw_conditions else {}
        except json.JSONDecodeError:
            conditions = {}
        order_index = form.get("order_index") or 0
        enabled = form.get("enabled") in ("1", "on", "true", "True")
    elif getattr(rule, "id", None) is not None:
        # Edit load: use saved types (GET passes empty selected_event_type_ids).
        chosen_ids = []
        for i in (rule.event_type_ids or []):
            try:
                chosen_ids.append(int(i))
            except (TypeError, ValueError):
                continue
    else:
        chosen_ids = list(selected_event_type_ids or [])
    draft_rule = SimpleNamespace(
        id=getattr(rule, "id", None),
        event_type_ids=chosen_ids,
        conditions=conditions,
        order_index=order_index,
        enabled=enabled,
    )
    return {
        "active": "pipeline",
        "source": source,
        "event_types": event_types,
        "rule": draft_rule,
        "is_edit": getattr(rule, "id", None) is not None,
        "selected_event_type_ids": chosen_ids,
        "error": error,
    }


def _trigger_targets(db) -> list:
    from app.widgets import trigger_targets
    return trigger_targets(db)


def _build_action_dialog_context(
    *,
    source: Source,
    fields: list[Field],
    action_types: list[str],
    local_actions_enabled: bool,
    action=None,
    rule=None,
    form=None,
    error: str | None = None,
    trigger_targets: list | None = None,
) -> dict:
    cfg = dict((action.config if action else None) or {})
    headers_text = "\n".join(f"{k}: {v}" for k, v in (cfg.get("headers") or {}).items()) if isinstance(cfg.get("headers"), dict) else ""
    custom_body_text = ""
    custom_body = cfg.get("custom_body")
    if isinstance(custom_body, (dict, list)):
        custom_body_text = json.dumps(custom_body, indent=2)
    elif isinstance(custom_body, str):
        custom_body_text = custom_body

    trigger_payload_text = ""
    payload = cfg.get("payload")
    if isinstance(payload, dict):
        trigger_payload_text = json.dumps(payload, indent=2) if payload else ""
    elif isinstance(payload, str):
        trigger_payload_text = payload

    target_ref = ""
    if cfg.get("target_source_id") is not None:
        if cfg.get("event_type_id") is not None:
            target_ref = f"webhook:{cfg['target_source_id']}:{cfg['event_type_id']}"
        else:
            target_ref = f"poll:{cfg['target_source_id']}"

    action_type = ((form.get("action_type") if form else None) or getattr(action, "action_type", None) or "field_push").strip()
    draft_rule = rule
    if form is not None:
        headers_text = form.get("headers_text") or ""
        custom_body_text = form.get("custom_body") or custom_body_text
        rule_id_raw = (form.get("rule_id") or "").strip()
        if not draft_rule and rule_id_raw.isdigit():
            draft_rule = SimpleNamespace(id=int(rule_id_raw))
        draft_cfg = dict(cfg)
        if action_type == "field_push":
            field_id_raw = (form.get("field_id") or "").strip()
            if field_id_raw:
                try:
                    draft_cfg["field_id"] = int(field_id_raw)
                except ValueError:
                    draft_cfg["field_id"] = field_id_raw
            field_type = (form.get("field_type") or "").strip()
            mode = (form.get("logbook_mode") or "event").strip()
            if field_type == "logbook":
                draft_cfg.pop("op", None)
                if mode == "key":
                    draft_cfg["value_key"] = form.get("value_key") or ""
                    draft_cfg.pop("value", None)
                elif mode == "literal":
                    draft_cfg["value"] = form.get("value") or ""
                    draft_cfg.pop("value_key", None)
                else:
                    draft_cfg.pop("value", None)
                    draft_cfg.pop("value_key", None)
            elif field_type == "value":
                draft_cfg["op"] = (form.get("value_op") or "increment").strip()
                if form.get("delta") is not None:
                    draft_cfg["delta"] = form.get("delta")
            elif field_type == "text":
                draft_cfg["value"] = form.get("value") if form.get("value") is not None else ""
            elif field_type == "toggle":
                if (form.get("toggle_mode") or "literal").strip() == "switch":
                    draft_cfg["op"] = "switch"
                    draft_cfg.pop("value", None)
                else:
                    draft_cfg.pop("op", None)
                    draft_cfg["value"] = (form.get("toggle_value") or "false").strip().lower() in ("1", "true", "yes", "on")
            elif field_type == "data":
                draft_cfg.pop("op", None)
                if (form.get("data_mode") or "event").strip() == "key":
                    draft_cfg["value_key"] = form.get("value_key") or ""
                else:
                    draft_cfg.pop("value_key", None)
        elif action_type == "web_push":
            draft_cfg.update({
                "title": (form.get("title") or "Para-Scope").strip() or "Para-Scope",
                "body": form.get("body") or "",
                "url": (form.get("url") or "/").strip() or "/",
            })
        elif action_type == "notify":
            draft_cfg.update({
                "service": (form.get("service") or "ntfy").strip().lower(),
                "server_url": form.get("server_url") or "",
                "topic": form.get("topic") or "",
                "title": form.get("title") if form.get("title") is not None else "",
                "body": form.get("body") if form.get("body") is not None else "",
                "priority": form.get("priority") or "",
                "auth_mode": (form.get("auth_mode") or "none").strip(),
                "timeout_seconds": form.get("timeout_seconds") or "",
            })
        elif action_type == "local_script":
            draft_cfg.update({
                "command": form.get("command") or "",
                "argv": [ln.strip() for ln in (form.get("argv_text") or "").splitlines() if ln.strip()],
                "timeout_seconds": form.get("timeout_seconds") or "",
            })
        elif action_type == "http_forward":
            draft_cfg.update({
                "preset": (form.get("preset") or "none").strip().lower(),
                "url": form.get("url") or "",
                "method": (form.get("method") or "POST").strip().upper(),
                "timeout_seconds": form.get("timeout_seconds") or "",
                "body_mode": (form.get("body_mode") or "auto").strip(),
                "body_text": form.get("body_text") or "",
                "auth_mode": (form.get("auth_mode") or "none").strip(),
                "auth_header": form.get("auth_header") or "Authorization",
                "auth_prefix": form.get("auth_prefix") or "Bearer ",
                "api_key_header": form.get("api_key_header") or "X-Api-Key",
                "api_secret_header": form.get("api_secret_header") or "X-Api-Secret",
                "signing_mode": (form.get("signing_mode") or "none").strip(),
                "signing_signature_header": form.get("signing_signature_header") or "X-Call-Signature",
                "signing_timestamp_header": form.get("signing_timestamp_header") or "X-Call-Timestamp",
            })
        elif action_type == "trigger_source":
            ref = (form.get("target_ref") or "").strip()
            if ref:
                target_ref = ref
                parts = ref.split(":")
                if parts[0] == "poll" and len(parts) == 2:
                    try:
                        draft_cfg["target_source_id"] = int(parts[1])
                    except ValueError:
                        draft_cfg["target_source_id"] = parts[1]
                    draft_cfg["event_type_id"] = None
                elif parts[0] == "webhook" and len(parts) == 3:
                    try:
                        draft_cfg["target_source_id"] = int(parts[1])
                        draft_cfg["event_type_id"] = int(parts[2])
                    except ValueError:
                        draft_cfg["target_source_id"] = parts[1]
                        draft_cfg["event_type_id"] = parts[2]
            trigger_payload_text = form.get("payload") if form.get("payload") is not None else trigger_payload_text
            raw_payload = (form.get("payload") or "").strip()
            if raw_payload:
                try:
                    draft_cfg["payload"] = json.loads(raw_payload)
                except (json.JSONDecodeError, TypeError):
                    draft_cfg["payload"] = raw_payload
            else:
                draft_cfg["payload"] = {}
        cfg = draft_cfg

    draft_action = SimpleNamespace(
        id=getattr(action, "id", None),
        action_type=action_type,
        config=cfg,
        secret=getattr(action, "secret", None),
        secret_2=getattr(action, "secret_2", None),
    )
    return {
        "active": "pipeline",
        "source": source,
        "action_types": action_types,
        "action": draft_action,
        "is_edit": getattr(action, "id", None) is not None,
        "rule": draft_rule,
        "fields": fields,
        "trigger_targets": trigger_targets or [],
        "target_ref": target_ref,
        "trigger_payload_text": trigger_payload_text,
        "local_actions_enabled": local_actions_enabled,
        "headers_text": headers_text,
        "custom_body_text": custom_body_text,
        "error": error,
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
    sources = db.query(Source).order_by(Source.created_at.desc(), Source.id.desc()).all()
    field_ctx = ctx._fields_list_context(db)
    fields = field_ctx["fields"]
    fields_by_id = {f.id: f for f in fields}
    sources_by_id = {s.id: s for s in sources}
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
        rules_by_event_type_id, orphan_rules = ctx._rules_grouped_by_event_type(
            rules, event_types,
        )
        chains.append({
            "source": source,
            "rules": rules,
            "rules_by_event_type_id": rules_by_event_type_id,
            "orphan_rules": orphan_rules,
            "actions": actions,
            "actions_by_id": actions_by_id,
            "unbound_actions": unbound,
            "event_types": event_types,
            "event_types_by_id": ctx._event_type_map(event_types),
            "fields_by_id": fields_by_id,
            "sources_by_id": sources_by_id,
        })
    return ctx.templates.TemplateResponse(
        request, "config/pipeline.html", {
            "active": "pipeline",
            "chains": chains,
            "fields": fields,
            "logbook_counts": field_ctx["logbook_counts"],
            "success": success,
            "error": error,
        }
    )


# route: /config/pipeline/partials/field-form
@router.get("/config/pipeline/partials/field-form")
async def pipeline_field_form(request: Request, db: Session = Depends(get_db)):
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
        request,
        "config/pipeline/_field_form.html",
        _build_field_dialog_context(field=field),
    )


# route: /config/pipeline/fields
@router.post("/config/pipeline/fields")
async def pipeline_create_field(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    kwargs, err = ctx._parse_field_form(form)
    if err:
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_field_form.html",
                _build_field_dialog_context(form=form, error=err),
            )
        return ctx._pipeline_redirect(error=err, request=request)
    if db.query(Field).filter(Field.name == kwargs["name"]).first():
        msg = f"Field '{kwargs['name']}' already exists"
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_field_form.html",
                _build_field_dialog_context(form=form, error=msg),
            )
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
        return _dialog_success_response(
            ctx._fields_section_template(request, db),
            retarget="#pipeline-fields",
        )
    return ctx._pipeline_redirect(success=f"Field '{field.name}' created", request=request)


# route: /config/pipeline/field/{field_id}
@router.post("/config/pipeline/field/{field_id}")
async def pipeline_update_field(request: Request, field_id: int, db: Session = Depends(get_db)):
    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        if ctx._is_htmx(request):
            return HTMLResponse("Field not found", status_code=404)
        return ctx._pipeline_redirect(error="Field not found", request=request)

    form = await request.form()
    kwargs, err = ctx._parse_field_form(form, existing=field)
    if err:
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_field_form.html",
                _build_field_dialog_context(field=field, form=form, error=err),
            )
        return ctx._pipeline_redirect(error=err, request=request)

    clash = (
        db.query(Field)
        .filter(Field.name == kwargs["name"], Field.id != field_id)
        .first()
    )
    if clash:
        msg = f"Field '{kwargs['name']}' already exists"
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_field_form.html",
                _build_field_dialog_context(field=field, form=form, error=msg),
            )
        return ctx._pipeline_redirect(error=msg, request=request)

    field.name = kwargs["name"]
    previous_slug = field.slug
    field.slug = ctx._unique_field_slug(db, kwargs["name"], exclude_id=field_id)
    field.config = kwargs["config"]
    db.commit()
    audit_details = {"name": field.name, "slug": field.slug}
    if previous_slug != field.slug:
        audit_details["previous_slug"] = previous_slug
    ctx._audit_log(db, request, "field.update", resource_type="field", resource_id=field.id,
               details=audit_details)

    if ctx._is_htmx(request):
        return _dialog_success_response(
            ctx._fields_section_template(request, db),
            retarget="#pipeline-fields",
        )
    return ctx._pipeline_redirect(success=f"Field '{field.name}' updated", request=request)


# route: /config/pipeline/field/{field_id}/delete
@router.post("/config/pipeline/field/{field_id}/delete")
async def pipeline_delete_field(request: Request, field_id: int, db: Session = Depends(get_db)):
    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        if ctx._is_htmx(request):
            return HTMLResponse("Field not found", status_code=404)
        return ctx._pipeline_redirect(error="Field not found", request=request)

    reason = ctx._field_in_use(db, field_id)
    if reason:
        msg = f"Can’t delete “{field.name}” — it’s still in use"
        return ctx._pipeline_redirect(error=msg, request=request)

    name = field.name
    db.query(FieldLogEntry).filter(FieldLogEntry.field_id == field_id).delete(
        synchronize_session=False
    )
    db.delete(field)
    db.commit()
    ctx._audit_log(db, request, "field.delete", resource_type="field", resource_id=field_id,
               details={"name": name})

    if ctx._is_htmx(request):
        return ctx._fields_section_template(request, db)
    return ctx._pipeline_redirect(success=f"Field '{name}' deleted", request=request)


# route: /config/pipeline/field/{field_id}/partials/recent-entries
@router.get("/config/pipeline/field/{field_id}/partials/recent-entries")
async def pipeline_recent_logbook(request: Request, field_id: int, db: Session = Depends(get_db)):
    from app.event_store import RECENT_LIMIT_DEFAULT, RECENT_LIMIT_MAX

    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        return HTMLResponse("Field not found", status_code=404)
    if field.field_type != "logbook":
        return HTMLResponse("Not a logbook", status_code=400)
    try:
        limit = int(request.query_params.get("limit") or RECENT_LIMIT_DEFAULT)
    except ValueError:
        limit = RECENT_LIMIT_DEFAULT
    limit = max(1, min(limit, RECENT_LIMIT_MAX))

    entries = (
        db.query(FieldLogEntry)
        .filter(FieldLogEntry.field_id == field_id)
        .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
        .limit(limit)
        .all()
    )
    return ctx.templates.TemplateResponse(
        request, "config/pipeline/_recent_logbook.html", {
            "field": field,
            "entries": entries,
            "limit": limit,
            "limit_choices": (5, 10, 25, 50),
        }
    )


# route: /config/pipeline/field/{field_id}/clear
@router.post("/config/pipeline/field/{field_id}/clear")
async def pipeline_clear_logbook(request: Request, field_id: int, db: Session = Depends(get_db)):
    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        if ctx._is_htmx(request):
            return HTMLResponse("Field not found", status_code=404)
        return ctx._pipeline_redirect(error="Field not found", request=request)
    if field.field_type != "logbook":
        msg = "Only logbooks can be cleared"
        return ctx._pipeline_redirect(error=msg, request=request)

    deleted = (
        db.query(FieldLogEntry)
        .filter(FieldLogEntry.field_id == field_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    ctx._audit_log(
        db, request, "field.clear", resource_type="field", resource_id=field_id,
        details={"name": field.name, "deleted": deleted},
    )

    if ctx._is_htmx(request):
        return ctx._fields_section_template(request, db)
    return ctx._pipeline_redirect(success=f"Cleared {deleted} entries from '{field.name}'", request=request)


# route: /config/pipeline/partials/source-templates
@router.get("/config/pipeline/partials/source-templates")
async def pipeline_source_templates(request: Request):
    from app.source_templates import list_source_templates
    return ctx.templates.TemplateResponse(
        request,
        "config/pipeline/_source_templates.html",
        {"templates": list_source_templates()},
    )


# route: /config/pipeline/templates/{slug}/apply
@router.post("/config/pipeline/templates/{slug}/apply")
async def pipeline_apply_template(
    request: Request, slug: str, db: Session = Depends(get_db),
):
    from app.source_templates import apply_source_template, get_source_template, list_source_templates

    def _err(msg: str):
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_source_templates.html",
                {"templates": list_source_templates(), "error": msg},
            )
        return ctx._pipeline_redirect(error=msg, request=request)

    if not get_source_template(slug):
        return _err(f"Unknown template '{slug}'")

    try:
        result = apply_source_template(db, slug)
    except ValueError as e:
        return _err(str(e))

    ctx._audit_log(
        db, request, "template.apply",
        resource_type="source", resource_id=result["source_id"],
        details={
            "template": slug,
            "name": result["source_name"],
            "widgets": len(result["widget_ids"]),
        },
    )

    if ctx._is_htmx(request):
        return HTMLResponse("", headers={"HX-Redirect": "/config/pipeline"})
    return ctx._pipeline_redirect(
        success=f"Template '{result['title']}' applied — source '{result['source_name']}' created",
        request=request,
    )


# route: /config/pipeline/partials/source-form
@router.get("/config/pipeline/partials/source-form")
async def pipeline_source_form(request: Request):
    context = _build_source_dialog_context()
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
        if ctx._is_htmx(request):
            context = _build_source_dialog_context(form=form, error=msg)
            return ctx.templates.TemplateResponse(
                request, "config/pipeline/_source_form.html", context
            )
        return ctx._pipeline_redirect(error=msg, request=request)

    if not name:
        return _err("Name is required")
    if source_type not in ctx._SOURCE_TYPES:
        return _err("Choose Webhook or Poll")

    from app.webhook_verifiers import get_webhook_provider, get_webhook_provider_slugs
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
        provider_meta = get_webhook_provider(webhook_provider) or {}
        if provider_meta.get("secret_required") and not secret_value:
            label = provider_meta.get("secret_input_label") or "Credential"
            return _err(f"{label} is required")
        if webhook_provider == "paypal":
            if not paypal_webhook_id:
                return _err("Webhook ID is required")
            if not paypal_client_id:
                return _err("Client ID is required")
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
            _ensure_event_type(db, source.id, et_name, et_desc)
    elif source_type == "webhook":
        _ensure_event_type(
            db, source.id, "always", "Fires on every accepted webhook delivery",
        )

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
    return ctx._pipeline_redirect(success=f"Source '{name}' created", request=request)


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
        request,
        "config/pipeline/_event_form.html",
        _build_event_dialog_context(source=source, event=event),
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
        return ctx._pipeline_redirect(error="Source not found", request=request)

    form = await request.form()
    description = (form.get("description") or "").strip()
    name, name_error = _validate_event_type_name(db, source_id, form.get("name") or "")
    if name_error:
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_event_form.html",
                _build_event_dialog_context(source=source, form=form, error=name_error),
            )
        return ctx._pipeline_redirect(error=name_error, request=request)

    et = EventTypeRecord(source_id=source_id, name=name, description=description)
    db.add(et)
    db.commit()
    ctx._audit_log(db, request, "event_type.create", resource_type="event_type", resource_id=et.id, details={"name": name})

    if ctx._is_htmx(request):
        return _dialog_success_response(
            ctx._source_chain_template(request, db, source),
            retarget=f"#source-chain-{source.id}",
        )
    return ctx._pipeline_redirect(success=f"Event type '{name}' created", request=request)


# route: /config/pipeline/event/{et_id}
@router.post("/config/pipeline/event/{et_id}")
async def pipeline_update_event(request: Request, et_id: int, db: Session = Depends(get_db)):
    et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
    if not et:
        if ctx._is_htmx(request):
            return HTMLResponse("Event not found", status_code=404)
        return ctx._pipeline_redirect(error="Event not found", request=request)
    source = db.query(Source).filter(Source.id == et.source_id).first()
    form = await request.form()
    description = (form.get("description") or "").strip()
    name, name_error = _validate_event_type_name(
        db, et.source_id, form.get("name") or "", exclude_id=et.id,
    )
    if name_error:
        if ctx._is_htmx(request) and source:
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_event_form.html",
                _build_event_dialog_context(source=source, event=et, form=form, error=name_error),
            )
        return ctx._pipeline_redirect(error=name_error, request=request)
    et.name = name
    et.description = description
    db.commit()
    ctx._audit_log(db, request, "event_type.update", resource_type="event_type", resource_id=et.id, details={"name": name})
    if ctx._is_htmx(request) and source:
        return _dialog_success_response(
            ctx._source_chain_template(request, db, source),
            retarget=f"#source-chain-{source.id}",
        )
    return ctx._pipeline_redirect(success=f"Event type '{name}' updated", request=request)


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
        request,
        "config/pipeline/_rule_form.html",
        _build_rule_dialog_context(
            source=source,
            event_types=event_types,
            rule=rule,
            selected_event_type_ids=selected_event_type_ids,
        ),
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
        return ctx._pipeline_redirect(error="Source not found", request=request)

    form = await request.form()
    event_types = (
        db.query(EventTypeRecord)
        .filter(EventTypeRecord.source_id == source_id)
        .order_by(EventTypeRecord.name)
        .all()
    )
    data, err = ctx._parse_rule_form(form)
    if err:
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_rule_form.html",
                _build_rule_dialog_context(
                    source=source,
                    event_types=event_types,
                    form=form,
                    error=err,
                ),
            )
        return ctx._pipeline_redirect(error=err, request=request)

    ref_err = _validate_rule_refs(
        db, source_id, data["event_type_ids"], data.get("action_ids"),
    )
    if ref_err:
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_rule_form.html",
                _build_rule_dialog_context(
                    source=source,
                    event_types=event_types,
                    form=form,
                    error=ref_err,
                ),
            )
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
        return _dialog_success_response(
            ctx._source_chain_template(request, db, source),
            retarget=f"#source-chain-{source.id}",
        )
    return ctx._pipeline_redirect(success="Rule created", request=request)


# route: /config/pipeline/rule/{rule_id}
@router.post("/config/pipeline/rule/{rule_id}")
async def pipeline_update_rule(request: Request, rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule or not rule.source_id:
        if ctx._is_htmx(request):
            return HTMLResponse("Rule not found", status_code=404)
        return ctx._pipeline_redirect(error="Rule not found", request=request)
    source = db.query(Source).filter(Source.id == rule.source_id).first()
    event_types = (
        db.query(EventTypeRecord)
        .filter(EventTypeRecord.source_id == rule.source_id)
        .order_by(EventTypeRecord.name)
        .all()
    )

    form = await request.form()
    data, err = ctx._parse_rule_form(form, for_update=True)
    if err:
        if ctx._is_htmx(request) and source:
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_rule_form.html",
                _build_rule_dialog_context(
                    source=source,
                    event_types=event_types,
                    rule=rule,
                    form=form,
                    error=err,
                ),
            )
        return ctx._pipeline_redirect(error=err, request=request)

    # Edit form does not send action_ids — keep existing bindings
    ref_err = _validate_rule_refs(db, rule.source_id, data["event_type_ids"], None)
    if ref_err:
        if ctx._is_htmx(request) and source:
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_rule_form.html",
                _build_rule_dialog_context(
                    source=source,
                    event_types=event_types,
                    rule=rule,
                    form=form,
                    error=ref_err,
                ),
            )
        return ctx._pipeline_redirect(error=ref_err, request=request)

    rule.conditions = data["conditions"]
    rule.order_index = data["order_index"]
    rule.event_type_ids = data["event_type_ids"]
    rule.enabled = data["enabled"]
    db.commit()
    ctx._audit_log(db, request, "rule.update", resource_type="rule", resource_id=rule.id,
               details={"order_index": rule.order_index})

    if ctx._is_htmx(request) and source:
        return _dialog_success_response(
            ctx._source_chain_template(request, db, source),
            retarget=f"#source-chain-{source.id}",
        )
    return ctx._pipeline_redirect(success="Rule updated", request=request)


# route: /config/pipeline/source/{source_id}/rules/dry-run
@router.post("/config/pipeline/source/{source_id}/rules/dry-run")
async def pipeline_rule_dry_run(request: Request, source_id: int, db: Session = Depends(get_db)):
    """Evaluate rule conditions against a sample event without running actions."""
    import json as _json
    from app.pipeline import match_conditions
    from app.fields import get_by_path
    from app.labels import action_label, operator_label

    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return HTMLResponse("Source not found", status_code=404)

    form = await request.form()
    rule_id_raw = (form.get("rule_id") or "").strip()
    saved_rule = None
    if rule_id_raw.isdigit():
        saved_rule = (
            db.query(Rule)
            .filter(Rule.id == int(rule_id_raw), Rule.source_id == source_id)
            .first()
        )

    # Prefer form draft (mid-edit); fall back to saved rule.
    data, err = ctx._parse_rule_form(form, for_update=bool(saved_rule))
    if err and saved_rule is None:
        return ctx.templates.TemplateResponse(
            request,
            "config/pipeline/_rule_test_result.html",
            {"error": err, "matched": False, "standalone": False},
        )
    if saved_rule is not None and (err or not (form.get("conditions") or "").strip()):
        conditions = saved_rule.conditions or {}
        event_type_ids = list(saved_rule.event_type_ids or [])
        action_ids = list(saved_rule.action_ids or [])
        rule_enabled = bool(saved_rule.enabled)
    else:
        conditions = (data or {}).get("conditions") or {}
        event_type_ids = (data or {}).get("event_type_ids") or []
        action_ids = list(saved_rule.action_ids) if saved_rule else []
        rule_enabled = bool(saved_rule.enabled) if saved_rule else True

    # Sample event: explicit id, else newest for selected type(s), else newest for source.
    event = None
    event_id_raw = (form.get("event_id") or "").strip()
    if event_id_raw.isdigit():
        event = (
            db.query(Event)
            .filter(Event.id == int(event_id_raw), Event.source_id == source_id)
            .first()
        )
    if event is None:
        q = db.query(Event).filter(Event.source_id == source_id)
        if event_type_ids:
            q = q.filter(Event.event_type_id.in_(event_type_ids))
        event = q.order_by(Event.timestamp.desc(), Event.id.desc()).first()

    if event is None:
        return ctx.templates.TemplateResponse(
            request,
            "config/pipeline/_rule_test_result.html",
            {
                "error": None,
                "no_sample": True,
                "matched": False,
                "conditions": conditions,
                "standalone": bool(saved_rule and not (form.get("conditions") or "").strip()),
                "source": source,
            },
        )

    sample_data = event.normalized_data or {}
    from app.pipeline import _match_data
    match_data = _match_data(db, sample_data, conditions)
    uses_fields = match_data is not sample_data
    et = event.event_type
    type_gate = "all"
    type_gate_ok = True
    if event_type_ids:
        type_gate = "selected"
        type_gate_ok = event.event_type_id in event_type_ids
    if et is not None and not et.enabled:
        type_gate = "paused"
        type_gate_ok = False

    overall, bindings = match_conditions(match_data, conditions)
    rows = []
    for key, matcher in (conditions or {}).items():
        ok, _ = match_conditions(match_data, {key: matcher})
        actual = get_by_path(match_data, key)
        correlated = "*" in key
        rows.append({
            "key": key,
            "ok": ok,
            "actual": actual,
            "matcher": matcher,
            "correlated": correlated,
        })

    would_run = []
    fields_by_id = {f.id: f for f in db.query(Field).all()}
    sources_by_id = {s.id: s for s in db.query(Source).all()}
    if overall and type_gate_ok and rule_enabled:
        for aid in action_ids:
            action = (
                db.query(ActionInstance)
                .filter(ActionInstance.id == aid)
                .first()
            )
            if not action or not action.enabled:
                continue
            if action.source_id is not None and action.source_id != source_id:
                continue
            would_run.append({
                "id": action.id,
                "label": action_label(action, fields_by_id, sources_by_id),
            })

    return ctx.templates.TemplateResponse(
        request,
        "config/pipeline/_rule_test_result.html",
        {
            "error": None,
            "no_sample": False,
            "matched": overall and type_gate_ok and rule_enabled,
            "conditions_matched": overall,
            "type_gate": type_gate,
            "type_gate_ok": type_gate_ok,
            "rule_enabled": rule_enabled,
            "rows": rows,
            "bindings": bindings,
            "would_run": would_run,
            "event": event,
            "event_type_name": et.name if et else None,
            "sample_json": _json.dumps(sample_data, indent=2, default=str),
            "uses_fields": uses_fields,
            "operator_label": operator_label,
            "standalone": bool(saved_rule and not (form.get("conditions") or "").strip()),
            "source": source,
        },
    )


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

    return ctx.templates.TemplateResponse(
        request,
        "config/pipeline/_action_form.html",
        _build_action_dialog_context(
            source=source,
            fields=db.query(Field).order_by(Field.name).all(),
            action_types=get_action_types(),
            local_actions_enabled=local_actions_enabled(),
            action=action,
            rule=rule,
            trigger_targets=_trigger_targets(db),
        ),
    )


# route: /config/pipeline/source/{source_id}/actions
@router.post("/config/pipeline/source/{source_id}/actions")
async def pipeline_create_action(request: Request, source_id: int, db: Session = Depends(get_db)):
    from app.actions import get_action_types
    from app.actions import local_actions_enabled
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        if ctx._is_htmx(request):
            return HTMLResponse("Source not found", status_code=404)
        return ctx._pipeline_redirect(error="Source not found", request=request)

    form = await request.form()
    action_type = form.get("action_type", "field_push")
    secret_value = (form.get("secret_value") or "").strip()
    secret2_value = (form.get("secret2_value") or "").strip()
    rule_id_raw = (form.get("rule_id") or "").strip()
    fields = db.query(Field).order_by(Field.name).all()
    trigger_targets = _trigger_targets(db)

    def _render_error(msg: str, *, rule_override=None):
        if ctx._is_htmx(request):
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_action_form.html",
                _build_action_dialog_context(
                    source=source,
                    fields=fields,
                    action_types=get_action_types(),
                    local_actions_enabled=local_actions_enabled(),
                    rule=rule_override,
                    form=form,
                    error=msg,
                    trigger_targets=trigger_targets,
                ),
            )
        return ctx._pipeline_redirect(error=msg, request=request)

    # Creates unused action when no rule references it yet.
    require_rule = ctx._is_htmx(request) or bool(rule_id_raw)
    rule = None
    if rule_id_raw:
        try:
            rid = int(rule_id_raw)
        except ValueError:
            msg = "Invalid rule"
            return _render_error(msg)
        rule = (
            db.query(Rule)
            .filter(Rule.id == rid, Rule.source_id == source_id)
            .first()
        )
        if not rule:
            msg = "Rule not found on this source"
            return _render_error(msg)
    elif require_rule:
        msg = "A rule is required"
        return _render_error(msg)

    if action_type not in get_action_types():
        msg = "That action type isn’t supported"
        return _render_error(msg, rule_override=rule)

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
        return _render_error(err, rule_override=rule)

    if action_type == "field_push":
        field = db.query(Field).filter(Field.id == config["field_id"]).first()
        if not field:
            msg = "Field not found"
            return _render_error(msg, rule_override=rule)

    if action_type == "trigger_source":
        target = db.query(Source).filter(Source.id == config["target_source_id"]).first()
        if not target:
            return _render_error("Target source not found", rule_override=rule)
        if target.source_type == "webhook":
            et = (
                db.query(EventTypeRecord)
                .filter(
                    EventTypeRecord.id == config.get("event_type_id"),
                    EventTypeRecord.source_id == target.id,
                )
                .first()
            )
            if not et:
                return _render_error("Event type not found", rule_override=rule)
        elif target.source_type != "poll":
            return _render_error("Target source cannot be triggered", rule_override=rule)

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
            return _render_error(str(e), rule_override=rule)

    if rule is not None:
        ids = list(rule.action_ids or [])
        ids.append(action.id)
        rule.action_ids = ids

    db.commit()
    ctx._audit_log(db, request, "action.create", resource_type="action", resource_id=action.id,
               details={"action_type": action_type})

    if ctx._is_htmx(request):
        return _dialog_success_response(
            ctx._source_chain_template(request, db, source),
            retarget=f"#source-chain-{source.id}",
        )
    return ctx._pipeline_redirect(success="Action created", request=request)


# route: /config/pipeline/action/{action_id}
@router.post("/config/pipeline/action/{action_id}")
async def pipeline_update_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    from app.actions import get_action_types
    from app.actions import local_actions_enabled
    action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
    if not action:
        if ctx._is_htmx(request):
            return HTMLResponse("Action not found", status_code=404)
        return ctx._pipeline_redirect(error="Action not found", request=request)
    source = db.query(Source).filter(Source.id == action.source_id).first()

    form = await request.form()
    action_type = form.get("action_type", action.action_type)
    secret_value = (form.get("secret_value") or "").strip()
    secret2_value = (form.get("secret2_value") or "").strip()
    fields = db.query(Field).order_by(Field.name).all()
    trigger_targets = _trigger_targets(db)

    def _render_error(msg: str):
        if ctx._is_htmx(request) and source:
            return ctx.templates.TemplateResponse(
                request,
                "config/pipeline/_action_form.html",
                _build_action_dialog_context(
                    source=source,
                    fields=fields,
                    action_types=get_action_types(),
                    local_actions_enabled=local_actions_enabled(),
                    action=action,
                    form=form,
                    error=msg,
                    trigger_targets=trigger_targets,
                ),
            )
        return ctx._pipeline_redirect(error=msg, request=request)

    if action_type not in get_action_types():
        msg = "That action type isn’t supported"
        return _render_error(msg)

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
        return _render_error(err)

    if action_type == "field_push":
        field = db.query(Field).filter(Field.id == config["field_id"]).first()
        if not field:
            msg = "Field not found"
            return _render_error(msg)

    if action_type == "trigger_source":
        target = db.query(Source).filter(Source.id == config["target_source_id"]).first()
        if not target:
            return _render_error("Target source not found")
        if target.source_type == "webhook":
            et = (
                db.query(EventTypeRecord)
                .filter(
                    EventTypeRecord.id == config.get("event_type_id"),
                    EventTypeRecord.source_id == target.id,
                )
                .first()
            )
            if not et:
                return _render_error("Event type not found")
        elif target.source_type != "poll":
            return _render_error("Target source cannot be triggered")

    action.action_type = action_type
    action.config = config

    if action_type in ("http_forward", "notify"):
        try:
            if secret_value:
                ctx._upsert_action_secret(db, action, value=secret_value, which="primary")
            if action_type == "http_forward" and config.get("auth_mode") == "key_secret" and secret2_value:
                ctx._upsert_action_secret(db, action, value=secret2_value, which="secondary")
        except ValueError as e:
            return _render_error(str(e))
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
        return _dialog_success_response(
            ctx._source_chain_template(request, db, source),
            retarget=f"#source-chain-{source.id}",
        )
    return ctx._pipeline_redirect(success="Action updated", request=request)


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
    context = _build_source_dialog_context(
        source=source,
        schedule=schedule,
        webhook_secret=webhook_secret,
    )
    return ctx.templates.TemplateResponse(request, "config/pipeline/_source_form.html", context)


# route: /config/source/{source_id}/edit
@router.post("/config/source/{source_id}/edit")
async def update_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found", request=request)
    schedule = (
        db.query(PollingSchedule)
        .filter(PollingSchedule.source_id == source_id)
        .first()
    )
    webhook_secret = None
    if source.webhook_secret_id:
        webhook_secret = db.query(Secret).filter(Secret.id == source.webhook_secret_id).first()

    def _err(msg: str):
        if ctx._is_htmx(request):
            context = _build_source_dialog_context(
                source=source,
                schedule=schedule,
                webhook_secret=webhook_secret,
                form=form,
                error=msg,
            )
            return ctx.templates.TemplateResponse(
                request, "config/pipeline/_source_form.html", context
            )
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
    previous_slug = source.slug
    previous_type = source.source_type

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
        from app.webhook_verifiers import get_webhook_provider, get_webhook_provider_slugs
        if webhook_provider not in get_webhook_provider_slugs():
            return _err("Choose a supported webhook verification method")
        source_cfg["webhook_provider"] = webhook_provider
        provider_meta = get_webhook_provider(webhook_provider) or {}
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
        provider_meta = get_webhook_provider(webhook_provider) or {}
        if provider_meta.get("secret_required") and not source.webhook_secret_id:
            label = provider_meta.get("secret_input_label") or "Credential"
            return _err(f"{label} is required")
        if previous_type != "webhook":
            _ensure_event_type(
                db, source.id, "always",
                "Fires on every accepted webhook delivery",
            )
    elif source_type == "poll":
        if source.webhook_secret_id:
            old_id = source.webhook_secret_id
            source.webhook_secret_id = None
            orphan = db.query(Secret).filter(Secret.id == old_id).first()
            if orphan:
                db.delete(orphan)
        existing_type_names = {
            normalize_event_type(et.name)
            for et in db.query(EventTypeRecord).filter(EventTypeRecord.source_id == source.id).all()
        }
        for et_name, et_desc in (
            ("on_success", "Poll completed successfully"),
            ("on_failure", "Poll failed (HTTP error or timeout)"),
        ):
            _ensure_event_type(
                db, source.id, et_name, et_desc, existing_names=existing_type_names,
            )

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
    audit_details = {"name": name, "slug": source.slug}
    if previous_slug != source.slug:
        audit_details["previous_slug"] = previous_slug
    ctx._audit_log(
        db, request, "source.update",
        resource_type="source", resource_id=source.id, details=audit_details,
    )

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
        resp.headers["HX-Retarget"] = f"#source-chain-{source.id}"
        resp.headers["HX-Reswap"] = "outerHTML"
        resp.headers["HX-Trigger"] = "pipeline-dialog-close"
        return resp
    return ctx._pipeline_redirect(success=f"Source '{name}' updated", request=request)


# route: /config/source/{source_id}/delete
@router.post("/config/source/{source_id}/delete")
async def delete_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found", request=request)
    name = source.name
    schedules = db.query(PollingSchedule).filter(PollingSchedule.source_id == source_id).all()
    for sched in schedules:
        remove_job(sched.id)

    actions = db.query(ActionInstance).filter(ActionInstance.source_id == source_id).all()
    action_ids = [a.id for a in actions]
    for action in actions:
        action.secret_id = None
        action.secret_id_2 = None
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
    ctx._scrub_trigger_source_refs(db, source_id=source_id)
    db.query(ActionInstance).filter(ActionInstance.source_id == source_id).delete()
    db.query(FieldLogEntry).filter(FieldLogEntry.source_id == source_id).update(
        {FieldLogEntry.source_id: None, FieldLogEntry.event_id: None},
        synchronize_session=False,
    )
    db.query(Event).filter(Event.source_id == source_id).delete()
    db.query(EventTypeRecord).filter(EventTypeRecord.source_id == source_id).delete()
    db.query(PollingSchedule).filter(PollingSchedule.source_id == source_id).delete()
    db.query(Rule).filter(Rule.source_id == source_id).delete()
    db.delete(source)
    db.commit()
    ctx._audit_log(db, request, "source.delete", resource_type="source", resource_id=source_id, details={"name": name})

    if ctx._is_htmx(request):
        return HTMLResponse("")
    return ctx._pipeline_redirect(success=f"Source '{name}' deleted", request=request)


# route: /config/source/{source_id}/toggle
@router.post("/config/source/{source_id}/toggle")
async def toggle_source(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found", request=request)
    source.enabled = not source.enabled
    db.commit()
    status_text = "enabled" if source.enabled else "disabled"
    ctx._audit_log(db, request, "source.toggle", resource_type="source", resource_id=source.id, details={"status": status_text})

    schedules = db.query(PollingSchedule).filter(PollingSchedule.source_id == source_id).all()
    for sched in schedules:
        if source.enabled and sched.enabled:
            add_or_update_job(sched)
        else:
            remove_job(sched.id)

    if ctx._is_htmx(request):
        resp = ctx._source_chain_template(request, db, source)
        return resp
    return ctx._pipeline_redirect(success=f"Source '{source.name}' {status_text}", request=request)


# route: /config/source/{source_id}/poll-now
@router.post("/config/source/{source_id}/poll-now")
async def poll_source_now(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        return ctx._pipeline_redirect(error="Source not found", request=request)
    if source.source_type != "poll":
        return ctx._pipeline_redirect(error="Only poll sources can be run manually", request=request)
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
    if not ok:
        return ctx._pipeline_redirect(error=msg, request=request)
    if ctx._is_htmx(request):
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success=msg, request=request)


# route: /config/event-type/{et_id}/toggle
@router.post("/config/event-type/{et_id}/toggle")
async def toggle_event_type(request: Request, et_id: int, db: Session = Depends(get_db)):
    et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
    if not et:
        return ctx._pipeline_redirect(error="Event type not found", request=request)
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
    return ctx._pipeline_redirect(success=f"Event '{et.name}' {status_text}", request=request)


# route: /config/rule/{rule_id}/toggle
@router.post("/config/rule/{rule_id}/toggle")
async def toggle_rule(request: Request, rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        return ctx._pipeline_redirect(error="Rule not found", request=request)
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
    return ctx._pipeline_redirect(success=f"Rule {status_text}", request=request)


# route: /config/action/{action_id}/toggle
@router.post("/config/action/{action_id}/toggle")
async def toggle_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
    if not action:
        return ctx._pipeline_redirect(error="Action not found", request=request)
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
    return ctx._pipeline_redirect(success=f"Action {status_text}", request=request)


# route: /config/event-type/{et_id}/delete
@router.post("/config/event-type/{et_id}/delete")
async def delete_event_type(request: Request, et_id: int, db: Session = Depends(get_db)):
    et = db.query(EventTypeRecord).filter(EventTypeRecord.id == et_id).first()
    if not et:
        return ctx._pipeline_redirect(error="Event type not found", request=request)
    name = et.name
    source_id = et.source_id
    ctx._cascade_delete_event_type(db, et)
    db.commit()
    ctx._audit_log(db, request, "event_type.delete", resource_type="event_type", resource_id=et_id, details={"name": name})

    source = db.query(Source).filter(Source.id == source_id).first()
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success=f"Event type '{name}' deleted", request=request)


# route: /config/action/{action_id}/delete
@router.post("/config/action/{action_id}/delete")
async def delete_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    action = db.query(ActionInstance).filter(ActionInstance.id == action_id).first()
    if not action:
        return ctx._pipeline_redirect(error="Action not found", request=request)
    action_type = action.action_type
    source_id = action.source_id
    ctx._scrub_action_from_rules(db, source_id, action_id)
    ctx._delete_actions_by_ids(db, [action_id])
    db.commit()
    ctx._audit_log(db, request, "action.delete", resource_type="action", resource_id=action_id,
               details={"action_type": action_type})

    source = db.query(Source).filter(Source.id == source_id).first()
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success="Action deleted", request=request)


# route: /config/rule/{rule_id}/delete
@router.post("/config/rule/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        return ctx._pipeline_redirect(error="Rule not found", request=request)
    source_id = rule.source_id
    ctx._delete_rule_with_actions(db, rule)
    db.commit()
    ctx._audit_log(db, request, "rule.delete", resource_type="rule", resource_id=rule_id, details={})

    source = db.query(Source).filter(Source.id == source_id).first() if source_id else None
    if ctx._is_htmx(request) and source:
        return ctx._source_chain_template(request, db, source)
    return ctx._pipeline_redirect(success="Rule deleted", request=request)


# ── Config: Users ───────────────────────────────────────────────────────────

